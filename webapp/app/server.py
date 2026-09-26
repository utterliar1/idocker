#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
爱快 Docker 容器更新器 —— Web 服务（纯标准库，零第三方依赖）

为什么要零依赖：目标环境是飞牛 NAS 的 Docker，构建时若需要 pip 联网装包
容易失败。纯标准库可以让镜像用 python:alpine 直接跑，构建不依赖 PyPI。

接口
  GET  /                        前端页面
  GET  /static/<file>           静态资源
  GET  /api/health              健康检查（Docker HEALTHCHECK 用）
  GET  /api/overview            一次性拿容器/镜像/服务设置/配置/定时任务
  GET  /api/containers          容器列表
  GET  /api/images              本地镜像
  GET  /api/server              Docker 服务设置
  GET  /api/tags?image=xxx      查询镜像可用版本
  GET  /api/history             历史任务列表（摘要，不含日志明细）
  GET  /api/history/<id>        单条历史详情（含完整日志，供事后回看）
  GET  /api/settings            设置（下载超时）+ 定时任务
  POST /api/settings            保存设置 {pull_timeout: 1800}
  POST /api/schedules           定时任务增删改查/启停/立即执行
  GET  /api/jobs/<id>           任务快照
  GET  /api/jobs/<id>/stream    任务的 SSE 事件流（流程图实时驱动）
  POST /api/check               启动版本检测任务
  POST /api/update              启动更新任务 {containers:[...], tag:""}
"""

import base64
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")

sys.path.insert(0, HERE)
import ikuai_api as A          # noqa: E402
import jobs as J               # noqa: E402
import scheduler as SC         # noqa: E402
import store as S              # noqa: E402

VERSION = "1.3.3"
CFG = A.load_config()
MANAGER = J.JobManager(CFG)
# 持久化设置：页面里改的下载超时、定时任务都落在这里（Docker 卷 /data）
STORE = S.Store(os.path.join(CFG.get("data_dir") or "/data", "settings.json"),
                default_timeout=CFG.get("pull_timeout"))
# 页面上的设置优先级高于环境变量，这样不改容器就能调参数
CFG["pull_timeout"] = STORE.get_pull_timeout()
SCHEDULER = SC.Scheduler(STORE, MANAGER)
MANAGER.set_on_finish(SCHEDULER.on_job_finish)

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".json": "application/json; charset=utf-8",
}


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status
        self.message = message


def history_brief(h):
    """历史列表用的摘要：不带日志明细，只给行数。

    明细单独走 /api/history/<id> 取 —— 否则列表一次要回几百 KB。
    """
    out = {k: v for k, v in h.items() if k != "logs"}
    out["log_count"] = len(h.get("logs") or [])
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "IKuaiDockerUpdater/" + VERSION
    protocol_version = "HTTP/1.0"          # SSE 需要流式响应，1.0 更省心

    # -- 基础工具 --------------------------------------------------
    def log_message(self, fmt, *args):
        if os.environ.get("DEBUG"):
            sys.stderr.write("[%s] %s\n" % (now_str(), fmt % args))

    def _auth_ok(self):
        want_user = os.environ.get("AUTH_USER") or ""
        if not want_user:
            return True
        want_pass = os.environ.get("AUTH_PASS") or ""
        header = self.headers.get("Authorization") or ""
        if not header.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(header[6:]).decode("utf-8", "replace")
            user, _, pw = raw.partition(":")
            ok = (user == want_user) and (pw == want_pass)
            return ok
        except Exception:
            return False

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, message, status=400):
        self._send_json({"ok": False, "error": str(message)}, status=status)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ApiError("请求体不是合法 JSON")

    def _serve_static(self, relpath):
        relpath = relpath.lstrip("/") or "index.html"
        target = os.path.normpath(os.path.join(STATIC_DIR, relpath))
        if not target.startswith(STATIC_DIR) or not os.path.isfile(target):
            self._send_error_json("资源不存在", 404)
            return
        with open(target, "rb") as f:
            body = f.read()
        ext = os.path.splitext(target)[1].lower()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # -- 入口 ------------------------------------------------------
    def do_GET(self):
        if not self._auth_ok():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="idocker"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                return self._serve_static("index.html")
            if path.startswith("/static/"):
                return self._serve_static(path[len("/static/"):])
            if path == "/api/health":
                return self._send_json({"ok": True, "version": VERSION, "time": now_str()})
            if path == "/api/overview":
                return self._send_json({"ok": True, "data": self._overview()})
            if path == "/api/containers":
                return self._send_json({"ok": True, "data": with_client(lambda ik: A.get_containers(ik))})
            if path == "/api/images":
                return self._send_json({"ok": True, "data": with_client(lambda ik: A.get_local_images(ik))})
            if path == "/api/server":
                return self._send_json({"ok": True, "data": with_client(lambda ik: A.get_server(ik))})
            if path == "/api/tags":
                image = (query.get("image") or [""])[0]
                if not image:
                    raise ApiError("缺少 image 参数")
                return self._send_json({"ok": True, "data": with_client(lambda ik: A.get_tags(ik, image))})
            if path == "/api/history":
                return self._send_json({"ok": True,
                                        "data": [history_brief(h) for h in MANAGER.history]})
            if path.startswith("/api/history/"):
                rec = MANAGER.find_history(path[len("/api/history/"):])
                if not rec:
                    raise ApiError("找不到这条历史记录（可能已被新的记录挤掉）", 404)
                return self._send_json({"ok": True, "data": rec})
            if path == "/api/settings":
                return self._send_json({"ok": True, "data": STORE.public()})
            if path == "/api/schedules":
                return self._send_json({"ok": True, "data": self._schedules_view()})
            if path == "/api/config":
                return self._send_json({"ok": True, "data": self._public_config()})
            m = re.fullmatch(r"/api/jobs/([0-9a-f]+)", path)
            if m:
                job = MANAGER.get(m.group(1))
                if not job:
                    raise ApiError("任务不存在", 404)
                return self._send_json({"ok": True, "data": job.snapshot()})
            m = re.fullmatch(r"/api/jobs/([0-9a-f]+)/stream", path)
            if m:
                job = MANAGER.get(m.group(1))
                if not job:
                    raise ApiError("任务不存在", 404)
                return self._stream_job(job)
            self._send_error_json("未知接口 %s" % path, 404)
        except ApiError as e:
            self._send_error_json(e.message, e.status)
        except A.IKuaiError as e:
            self._send_error_json(str(e), 502)
        except Exception as e:                                  # noqa: BLE001
            self._send_error_json("%s: %s" % (type(e).__name__, e), 500)

    def do_POST(self):
        if not self._auth_ok():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="idocker"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        path = urllib.parse.urlparse(self.path).path
        try:
            body = self._read_json_body()
            if path == "/api/check":
                job = MANAGER.create("check", [])
                return self._send_json({"ok": True, "job_id": job.id})
            if path == "/api/update":
                names = body.get("containers") or ([body["container"]] if body.get("container") else [])
                names = [str(n) for n in names if str(n).strip()]
                if not names:
                    raise ApiError("请至少选择一个容器")
                if MANAGER.is_busy():
                    raise ApiError("已有更新任务在执行，请等它跑完再试", 409)
                # 干跑开关由前端传入；任务按执行时的配置读取，每次请求覆盖一次。
                # 定时任务不带这个字段，因此沿用当前配置，不会偷改用户的选择。
                if "dry_run" in body:
                    CFG["dry_run"] = bool(body.get("dry_run"))
                tag = (body.get("tag") or "").strip() or None
                try:
                    job = MANAGER.create("update", names, tag)
                except J.BusyError as e:
                    raise ApiError(str(e), 409)
                return self._send_json({"ok": True, "job_id": job.id})
            if path == "/api/settings":
                if body.get("pull_timeout") is not None:
                    CFG["pull_timeout"] = STORE.set_pull_timeout(body.get("pull_timeout"))
                return self._send_json({"ok": True, "data": self._schedules_view()})
            if path == "/api/schedules":
                return self._send_json({"ok": True, "data": self._schedule_action(body)})
            self._send_error_json("未知接口 %s" % path, 404)
        except ApiError as e:
            self._send_error_json(e.message, e.status)
        except Exception as e:                                  # noqa: BLE001
            self._send_error_json("%s: %s" % (type(e).__name__, e), 500)

    # -- 设置与定时任务 ---------------------------------------------
    def _schedules_view(self):
        data = STORE.public()
        data["busy"] = MANAGER.is_busy()
        data["server_time"] = time.time()
        return data

    def _schedule_action(self, body):
        action = str(body.get("action") or "save").lower()
        sid = str(body.get("id") or "")
        if action == "save":
            item = body.get("item")
            if not isinstance(item, dict):
                raise ApiError("缺少任务内容")
            targets = [str(t).strip() for t in (item.get("targets") or []) if str(t).strip()]
            if item.get("mode") == "update" and not targets:
                raise ApiError("「自动更新」必须至少选择一个目标容器")
            if item.get("mode") == "check" and not targets:
                # 仅检测不选容器 = 检测全部，这是最常用的用法
                item["targets"] = []
            else:
                item["targets"] = targets
            try:
                STORE.save_schedule(item)
            except ValueError as e:
                raise ApiError(str(e))
        elif action == "delete":
            if not STORE.delete_schedule(sid):
                raise ApiError("任务不存在", 404)
        elif action == "toggle":
            if not STORE.toggle_schedule(sid, bool(body.get("enabled"))):
                raise ApiError("任务不存在", 404)
        elif action == "run":
            return self._run_schedule_now(sid)
        else:
            raise ApiError("未知操作 %s" % action)
        return self._schedules_view()

    def _run_schedule_now(self, sid):
        """按定时任务的配置立即跑一次，不影响它原有的排期。"""
        item = STORE.get_schedule(sid)
        if not item:
            raise ApiError("任务不存在", 404)
        if MANAGER.is_busy():
            raise ApiError("已有任务在执行，请等它跑完再试", 409)
        if item["mode"] == "update" and not item["targets"]:
            raise ApiError("这条任务没有选择目标容器")
        job = MANAGER.create(item["mode"], item["targets"], item.get("tag") or None,
                             auto=True, schedule_id=sid)
        STORE.mark_run(sid, job.id)
        view = self._schedules_view()
        view["job_id"] = job.id
        return view

    # -- 业务 ------------------------------------------------------
    def _public_config(self):
        """给前端看的配置，凭据一律不下发"""
        return {
            "router_url": CFG["router"]["url"],
            "username": CFG["router"]["username"],
            "password_set": bool(CFG["router"]["password"]),
            "registries": CFG["registries"],
            "dry_run": bool(CFG.get("dry_run")),
            "pull_timeout": CFG.get("pull_timeout"),
            "version": VERSION,
        }

    def _overview(self):
        def work(ik):
            containers = A.get_containers(ik)
            local = A.LocalImages(A.get_local_images(ik))
            for c in containers:
                img = local.find_for(c)
                pulled, src = local.pulled_at(img)
                c["local_pulled"] = pulled
                c["local_pulled_src"] = src
                c["repo"] = A.repo_of(c.get("image"))
                c["tag"] = A.split_image(c.get("image"))[2]
            try:
                srv = A.get_server(ik)
            except A.IKuaiError:
                srv = {}
            # 爱快自己配置的镜像加速源：这才是真正决定下载速度的地方，
            # 也是上游版本检测实际使用的源（本工具不再自配一套）。
            try:
                mirrors = A.get_mirrors(ik)
            except Exception:                               # noqa: BLE001
                mirrors = []
            return {"containers": containers, "images_count": len(local),
                    "server": srv, "mirrors": mirrors}
        data = with_client(work)
        data["config"] = self._public_config()
        data["history"] = MANAGER.history[:10]
        data["check"] = MANAGER.last_check()      # 上次检测结果，页面刷新后仍能显示上游状态
        data["settings"] = self._schedules_view()  # 下载超时 + 定时任务
        return data

    # -- SSE -------------------------------------------------------
    def _stream_job(self, job):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def write(payload):
            self.wfile.write(payload)
            self.wfile.flush()

        try:
            write(("event: snapshot\ndata: %s\n\n"
                   % json.dumps(job.snapshot(), ensure_ascii=False)).encode("utf-8"))
            idx = 0
            idle = 0
            while True:
                events, finished = job.wait_events(idx, timeout=10)
                for ev in events:
                    write(("event: %s\ndata: %s\n\n"
                           % (ev.get("type", "message"),
                              json.dumps(ev, ensure_ascii=False))).encode("utf-8"))
                    idx += 1
                if not events:
                    idle += 1
                    write(b": keep-alive\n\n")
                else:
                    idle = 0
                if finished and idx >= len(job.events):
                    break
                if idle > 60:                       # 超过 10 分钟无事件则收尾
                    break
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        finally:
            try:
                self.wfile.flush()
            except Exception:
                pass


def with_client(fn):
    """每个请求独立建会话（爱快会话不长期有效，避免复用过期 cookie）"""
    ctx = A.Ctx(logger=lambda m, level="info": None, verbose=False)
    ik = A.IKuai(CFG, ctx)
    ik.login()
    return fn(ik)


def main():
    port = int(os.environ.get("WEB_PORT") or 8088)
    bind = os.environ.get("WEB_HOST") or "0.0.0.0"
    print("=" * 62, flush=True)
    print("爱快 Docker 容器更新器 v%s" % VERSION, flush=True)
    print("  监听        http://%s:%d" % (bind, port), flush=True)
    print("  爱快地址    %s" % (CFG["router"]["url"] or "(未配置 IKUAI_URL)"), flush=True)
    print("  登录账号    %s" % (CFG["router"]["username"] or "(未配置 IKUAI_USER)"), flush=True)
    print("  登录密码    %s" % ("已配置(******)" if CFG["router"]["password"] else "(未配置)"), flush=True)
    print("  加速源兜底  %s" % ", ".join(CFG["registries"]), flush=True)
    print("              （上游检测优先用爱快 Docker 服务设置里配的加速源）", flush=True)
    print("  下载超时    %d 秒（可在页面上调整）" % CFG["pull_timeout"], flush=True)
    print("  定时任务    %d 条" % len(STORE.schedules()), flush=True)
    print("  干跑模式    %s" % ("开启" if CFG.get("dry_run") else "关闭"), flush=True)
    print("  访问认证    %s" % ("开启" if os.environ.get("AUTH_USER") else "关闭"), flush=True)
    print("=" * 62, flush=True)
    if not CFG["router"]["url"]:
        print("⚠ 未配置 IKUAI_URL，页面能打开但无法读取容器", flush=True)

    SCHEDULER.start()                              # 定时任务从这一刻开始计时
    for it in STORE.schedules():
        if it.get("enabled"):
            print("  ⏱ %s → %s" % (it["name"], S.describe_frequency(it)), flush=True)

    ThreadingHTTPServer.allow_reuse_address = True
    ThreadingHTTPServer.daemon_threads = True
    srv = ThreadingHTTPServer((bind, port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止", flush=True)
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
