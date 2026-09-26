#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
爱快 Docker 容器更新器 —— Web 服务（纯标准库，零第三方依赖）

为什么要零依赖：目标环境是飞牛 NAS 的 Docker，构建时若需要 pip 联网装包
容易失败。纯标准库可以让镜像用 python:alpine 直接跑，构建不依赖 PyPI。

接口
  GET  /                        前端页面
  GET  /login                   登录页（未登录时页面会被引导到这里）
  POST /api/login               提交账号密码，成功后下发会话 cookie
  POST /api/logout              退出登录，清掉会话 cookie
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
import hashlib
import hmac
import html
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

VERSION = "1.4.0"
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


# =================================================================
# 访问认证
#
# 早期版本用 HTTP Basic：浏览器弹出的是**原生账号框**，密码管理器
# （Chrome / Edge / 1Password / Bitwarden……）识别不了这种框，所以既不会
# 提示保存、也没法自动填充，每次都得手打一遍。
#
# 现在换成标准的「登录页 + 表单 POST + 会话 cookie」：页面上是真正的
# <form>，账号/密码框带 autocomplete="username" / "current-password"，
# 浏览器和密码管理器都能正常识别、保存、一键填充。
#
# 兼容：Authorization: Basic 头照旧接受（curl、脚本、旧书签、CI 都不受影响）。
# =================================================================
COOKIE_NAME = "idocker_session"
SESSION_TTL = 7 * 24 * 3600        # 登录状态保持 7 天，期间免登录
LOGIN_MAX_FAILS = 5                # 同一来源连续失败次数上限
LOGIN_LOCK_SECONDS = 60            # 触发上限后锁多久

_login_fails = {}                  # ip -> {"n": 失败次数, "until": 解锁时间戳}
_login_lock = threading.Lock()


def auth_enabled():
    return bool(os.environ.get("AUTH_USER") or "")


def _session_secret():
    """会话签名密钥：从当前账号密码派生。

    这样不用新增配置项、也不用落盘；改了密码，所有已发出的 cookie 立即失效。
    """
    seed = "idocker-session-v1|%s|%s" % (os.environ.get("AUTH_USER") or "",
                                         os.environ.get("AUTH_PASS") or "")
    return hashlib.sha256(seed.encode("utf-8")).digest()


def make_session_token(user, ttl=SESSION_TTL):
    """生成「用户名|过期时间|HMAC 签名」并 base64 编码 —— 无状态，重启也不掉线。"""
    exp = int(time.time()) + int(ttl)
    payload = "%s|%d" % (user, exp)
    sig = hmac.new(_session_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(("%s|%s" % (payload, sig)).encode("utf-8")).decode("ascii")


def read_session_token(token):
    """校验签名与有效期，通过则返回用户名，否则 None。"""
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
    except Exception:                                           # noqa: BLE001
        return None
    user, _, rest = raw.partition("|")
    exp_s, _, sig = rest.partition("|")
    if not user or not exp_s or not sig:
        return None
    payload = "%s|%s" % (user, exp_s)
    want = hmac.new(_session_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, want):                      # 防时序攻击
        return None
    try:
        if int(exp_s) < time.time():
            return None                                         # 过期
    except ValueError:
        return None
    return user


def parse_cookies(header):
    out = {}
    for part in (header or "").split(";"):
        key, _, val = part.partition("=")
        key = key.strip()
        if key:
            out[key] = val.strip()
    return out


def safe_next(value):
    """只接受站内相对路径，挡掉 //evil.com 这类开放重定向。"""
    value = (value or "").strip()
    if not value.startswith("/") or value.startswith("//") or value.startswith("/\\"):
        return "/"
    return value


def login_locked_for(ip):
    """返回剩余锁定秒数（0 = 没被锁）。"""
    with _login_lock:
        rec = _login_fails.get(ip)
        if not rec:
            return 0
        left = rec.get("until", 0) - time.time()
        return int(left) + 1 if left > 0 else 0


def note_login_fail(ip):
    with _login_lock:
        rec = _login_fails.setdefault(ip, {"n": 0, "until": 0})
        rec["n"] += 1
        if rec["n"] >= LOGIN_MAX_FAILS:
            rec["until"] = time.time() + LOGIN_LOCK_SECONDS
            rec["n"] = 0
        return rec


def clear_login_fails(ip):
    with _login_lock:
        _login_fails.pop(ip, None)


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>登录 · 爱快 Docker 容器更新器</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>&#128051;</text></svg>">
<style>
:root {
  --bg: #f5f7fa; --card: #fff; --border: #e4e7ec; --text: #101828;
  --muted: #667085; --primary: #2563eb; --danger: #dc2626; --danger-soft: #fef3f2;
}
* { box-sizing: border-box; }
body {
  margin: 0; min-height: 100vh; display: flex; align-items: center;
  justify-content: center; padding: 24px;
  background: var(--bg); color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
               "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  font-size: 14px; line-height: 1.5; -webkit-font-smoothing: antialiased;
}
.card {
  width: 100%; max-width: 360px; background: var(--card);
  border: 1px solid var(--border); border-radius: 12px; padding: 28px 26px 26px;
  box-shadow: 0 1px 3px rgba(16,24,40,.06), 0 1px 2px rgba(16,24,40,.04);
}
.logo { font-size: 30px; line-height: 1; }
h1 { font-size: 17px; margin: 12px 0 4px; }
.sub { margin: 0 0 20px; color: var(--muted); font-size: 13px; }
.err {
  background: var(--danger-soft); border: 1px solid #fecdca; color: #b42318;
  border-radius: 8px; padding: 9px 11px; font-size: 13px; margin-bottom: 16px;
}
label { display: block; font-size: 12px; color: var(--muted); margin-bottom: 6px; }
input[type=text], input[type=password] {
  width: 100%; padding: 9px 11px; margin-bottom: 14px; font-size: 14px;
  color: var(--text); background: #fff;
  border: 1px solid #d0d5dd; border-radius: 8px; outline: none;
}
input[type=text]:focus, input[type=password]:focus {
  border-color: var(--primary); box-shadow: 0 0 0 3px rgba(37,99,235,.12);
}
button {
  width: 100%; padding: 10px; font-size: 14px; font-weight: 500; color: #fff;
  background: var(--primary); border: 0; border-radius: 8px; cursor: pointer;
}
button:hover { background: #1d4ed8; }
.tip { margin: 16px 0 0; font-size: 12px; color: var(--muted); }
</style>
</head>
<body>
<main class="card">
  <div class="logo">&#128051;</div>
  <h1>爱快 Docker 容器更新器</h1>
  <p class="sub">请登录后继续</p>
  {{ERROR}}
  <form method="post" action="/api/login" autocomplete="on">
    <label for="username">账号</label>
    <input id="username" name="username" type="text" value="{{USERNAME}}"
           autocomplete="username" autocapitalize="none" autocorrect="off"
           spellcheck="false" required autofocus>
    <label for="password">密码</label>
    <input id="password" name="password" type="password"
           autocomplete="current-password" required>
    <input type="hidden" name="next" value="{{NEXT}}">
    <button type="submit">登录</button>
  </form>
  <p class="tip">登录状态保持 7 天。浏览器 / 密码管理器会提示保存，下次可自动填充。</p>
</main>
</body>
</html>
"""


def render_login_page(error="", username="", next_url="/"):
    """渲染登录页。

    刻意用占位符替换而不是 %-格式化：页面里的 CSS 有大量 `100%`，
    走 %-格式化会被当成格式符直接抛异常。
    """
    block = '<div class="err">%s</div>' % html.escape(error) if error else ""
    return (LOGIN_PAGE
            .replace("{{ERROR}}", block)
            .replace("{{USERNAME}}", html.escape(username or "", quote=True))
            .replace("{{NEXT}}", html.escape(safe_next(next_url), quote=True)))


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

    def _client_ip(self):
        # 内网直连场景，不解析 X-Forwarded-For：那个头客户端可以随便编，
        # 拿它做限速等于把限速关掉。
        return self.client_address[0] if self.client_address else "?"

    def _auth_ok(self):
        if not auth_enabled():
            return True
        want_user = os.environ.get("AUTH_USER") or ""
        want_pass = os.environ.get("AUTH_PASS") or ""

        # 1) 会话 cookie —— 登录页表单写入的，浏览器会自动带上
        token = parse_cookies(self.headers.get("Cookie")).get(COOKIE_NAME)
        if token:
            user = read_session_token(token)
            if user and hmac.compare_digest(user, want_user):
                return True

        # 2) 仍然接受 Basic 头：curl、脚本、旧书签、CI 都不受影响
        header = self.headers.get("Authorization") or ""
        if header.startswith("Basic "):
            try:
                raw = base64.b64decode(header[6:]).decode("utf-8", "replace")
                user, _, pw = raw.partition(":")
                if (hmac.compare_digest(user, want_user)
                        and hmac.compare_digest(pw, want_pass)):
                    return True
            except Exception:                                   # noqa: BLE001
                return False
        return False

    # -- 认证相关的响应工具 ----------------------------------------
    def _send_html(self, body, status=200):
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _redirect(self, location, status=302):
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _deny(self):
        """未认证：接口回 401 JSON（前端据此跳登录页），页面直接引导到登录页。

        注意这里**不下发 WWW-Authenticate** —— 那正是浏览器弹原生账号框的
        触发条件，也是密码管理器没法自动填充的根源。
        """
        if urllib.parse.urlparse(self.path).path.startswith("/api/"):
            return self._send_error_json("未登录或登录已过期，请重新登录", 401)
        target = self.path if self.path.startswith("/") else "/"
        return self._redirect("/login?next=" + urllib.parse.quote(target, safe="/"))

    def _set_session_cookie(self, user):
        self.send_header(
            "Set-Cookie",
            "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax"
            % (COOKIE_NAME, make_session_token(user), SESSION_TTL))

    def _clear_session_cookie(self):
        self.send_header("Set-Cookie",
                         "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax" % COOKIE_NAME)

    def _login_page(self, error="", username="", next_url="/"):
        return self._send_html(render_login_page(error, username, next_url),
                               status=200 if not error else 401)

    def _is_json_client(self):
        return (self.headers.get("Content-Type") or "").lower().startswith("application/json")

    def _do_login(self):
        ip = self._client_ip()
        left = login_locked_for(ip)
        if left:
            return self._login_failed(ip, "尝试次数过多，请 %d 秒后再试" % left, count=False)

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        if self._is_json_client():
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                data = {}
        else:
            # 浏览器原生表单提交（application/x-www-form-urlencoded）
            data = {k: v[0] for k, v in
                    urllib.parse.parse_qs(raw.decode("utf-8", "replace")).items()}

        user = str(data.get("username") or "")
        pw = str(data.get("password") or "")
        next_url = safe_next(data.get("next"))

        if not auth_enabled():                  # 没开认证就不用登录
            return self._redirect(next_url)

        want_user = os.environ.get("AUTH_USER") or ""
        want_pass = os.environ.get("AUTH_PASS") or ""
        if (hmac.compare_digest(user, want_user)
                and hmac.compare_digest(pw, want_pass)):
            clear_login_fails(ip)
            self.send_response(302)
            self._set_session_cookie(user)
            self.send_header("Location", next_url)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return self._login_failed(ip, "账号或密码不正确", user, next_url)

    def _login_failed(self, ip, message, username="", next_url="/", count=True):
        if count:
            note_login_fail(ip)
        if self._is_json_client():
            return self._send_error_json(message, 401)
        # 表单提交：把登录页原样送回去并带上错误提示，用户名留着省得重打
        return self._login_page(message, username, next_url)

    def _do_logout(self):
        if self._is_json_client():
            self.send_response(200)
            self._clear_session_cookie()
            body = json.dumps({"ok": True}).encode("utf-8")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(302)
        self._clear_session_cookie()
        self.send_header("Location", "/login")
        self.send_header("Content-Length", "0")
        self.end_headers()

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
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)

        # 登录页必须放行，否则会陷入「要登录得先开页面、要开页面得先登录」的死循环
        if path == "/login":
            next_url = (query.get("next") or ["/"])[0]
            if self._auth_ok():
                return self._redirect(safe_next(next_url))
            return self._login_page("", next_url=next_url)

        if path == "/api/logout":               # 也支持 GET，方便直接放一个退出链接
            return self._do_logout()

        if not self._auth_ok():
            return self._deny()

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
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/login":
            return self._do_login()
        if path == "/api/logout":
            return self._do_logout()
        if not self._auth_ok():
            return self._deny()
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
            # 前端据此决定要不要显示「退出登录」按钮
            "auth_enabled": auth_enabled(),
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
    print("  访问认证    %s" % ("登录页（账号 %s）" % os.environ["AUTH_USER"]
                              if auth_enabled() else "关闭"), flush=True)
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
