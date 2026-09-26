#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
爱快 4.0 Docker 容器自动更新工具（原生 API 版）

直接调用爱快自己的 /Action/call 接口完成容器更新，不依赖任何漏洞、
不需要 SSH、不需要提权，也不会脱离爱快的编排层。
接口细节见同目录 API_REFERENCE.md。

--------------------------------------------------------------------
常用命令
--------------------------------------------------------------------
  list                        列出容器（名称/镜像/网卡/IP/状态）
  images                      列出本地镜像（含拉取时间，便于判断新旧）
  tags <镜像>                 查询某个镜像有哪些可用版本
  check                       比对上游镜像是否有新版（不碰路由器）
  update <容器名> [--tag x]    拉取新镜像并就地更新容器
  server                      查看 Docker 服务设置（含镜像加速源）
  raw '<json>'                任意调用，排查用

参数
  -c/--config   配置文件，默认读 ../.env，可用 config.json 覆盖
  -n/--dry-run  只打印将要发出的请求，不执行
  -y/--yes      跳过确认
  -v/--verbose  打印详细过程（凭据已脱敏）
--------------------------------------------------------------------
"""

import argparse
import base64
import hashlib
import http.cookiejar
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(HERE, "..", ".env")
CONFIG_FILE = os.path.join(HERE, "config.json")
STATE_FILE = os.path.join(HERE, "state.json")

ACCEPT_MANIFEST = ", ".join([
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
])

DEFAULT_REGISTRIES = [
    "https://docker.1ms.run",
    "https://docker.m.daocloud.io",
    "https://docker.nju.edu.cn",
]


def log(msg=""):
    print(msg, flush=True)


class Ctx(object):
    def __init__(self, verbose=False, dry_run=False):
        self.verbose = verbose
        self.dry_run = dry_run

    def vlog(self, msg):
        if self.verbose:
            print("    " + str(msg), flush=True)


# --------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------
def load_env(path=ENV_FILE):
    env = {}
    if not os.path.exists(path):
        return env
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def mask_secret(text):
    """把请求/响应里的凭据打码，避免日志泄漏"""
    text = re.sub(r'("passwd"\s*:\s*")[^"]*(")', r"\1******\2", text)
    text = re.sub(r'("pass"\s*:\s*")[^"]*(")', r"\1******\2", text)
    text = re.sub(r'("password"\s*:\s*")[^"]*(")', r"\1******\2", text)
    return text


def load_config(path=CONFIG_FILE):
    env = load_env()
    cfg = {
        "router": {
            "url": env.get("ikuai_url", ""),
            "username": env.get("ikuai_id", ""),
            "password": env.get("ikuai_pw", ""),
            "login_path": "/Action/login",
        },
        "registries": list(DEFAULT_REGISTRIES),
        "timeout": 20,
        "notify_webhook": "",
        "notify_body": {"msgtype": "text", "text": {"content": "{message}"}},
    }
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        for k, v in user_cfg.items():
            if k == "router" and isinstance(v, dict):
                cfg["router"].update(v)
            else:
                cfg[k] = v
    return cfg


# --------------------------------------------------------------------
# API 客户端
# --------------------------------------------------------------------
class IKuaiError(Exception):
    pass


class IKuai(object):
    def __init__(self, cfg, ctx):
        self.cfg = cfg
        self.ctx = ctx
        r = cfg["router"]
        host = (r.get("url") or "").strip()
        if not host:
            raise IKuaiError("没找到爱快地址：请在 %s 里填 ikuai_url，或在 config.json 里填 router.url"
                             % ENV_FILE)
        for scheme in ("https://", "http://"):
            if host.startswith(scheme):
                host = host[len(scheme):]
        self.host = host
        self.base = "https://" + host
        self.timeout = cfg.get("timeout", 20)
        ctx_ssl = ssl.create_default_context()
        ctx_ssl.check_hostname = False
        ctx_ssl.verify_mode = ssl.CERT_NONE
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=ctx_ssl),
        )

    def _post(self, path, payload, write=False):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=body, method="POST")
        req.add_header("Content-Type", "application/json;charset=utf-8")
        req.add_header("Accept", "application/json, text/plain, */*")
        req.add_header("Referer", self.base + "/")
        req.add_header("Origin", self.base)
        self.ctx.vlog("POST %s  %s" % (path, mask_secret(json.dumps(payload, ensure_ascii=False))))
        if self.ctx.dry_run and write:
            self.ctx.vlog("    ↑ --dry-run：已跳过该写操作")
            return {"_dry_run": True}
        try:
            with self.op.open(req, timeout=self.timeout) as resp:
                text = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raw = ""
            try:
                raw = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            raise IKuaiError("HTTP %s %s %s" % (e.code, path, mask_secret(raw[:300])))
        except urllib.error.URLError as e:
            raise IKuaiError("连不上 %s：%s" % (self.base, e.reason))
        self.ctx.vlog("← %s" % mask_secret(text[:600]))
        if not text.strip():
            return {}                     # add/update 成功时就是空响应
        try:
            return json.loads(text)
        except ValueError:
            raise IKuaiError("返回不是 JSON：%s" % mask_secret(text[:300]))

    def login(self):
        r = self.cfg["router"]
        user = r.get("username")
        pw = r.get("password") or ""
        if not user:
            raise IKuaiError("没找到爱快账号：请在 %s 里填 ikuai_id / ikuai_pw" % ENV_FILE)
        body = r.get("login_body") or {
            "username": "{username}",
            "passwd": "{md5}",
            "pass": "{b64salt}",
            "remember_password": "",
        }
        vals = {
            "username": user,
            "md5": hashlib.md5(pw.encode()).hexdigest(),
            "b64salt": base64.b64encode(("salt_11" + pw).encode()).decode(),
        }
        payload = {}
        for k, v in body.items():
            if isinstance(v, str) and v.startswith("{") and v.endswith("}"):
                payload[k] = vals.get(v.strip("{}"), v)
            else:
                payload[k] = v
        resp = self._post(r.get("login_path", "/Action/login"), payload)
        if not (resp.get("Result") == 10000 or resp.get("code") == 0):
            raise IKuaiError("登录失败：%s" % mask_secret(json.dumps(resp, ensure_ascii=False)[:200]))
        self.ctx.vlog("登录成功，cookies=%s" % [c.name for c in self.jar])
        return resp

    def call(self, func_name, action, param=None):
        write = is_write(func_name, action)
        resp = self._post("/Action/call",
                          {"func_name": func_name, "action": action, "param": param or {}},
                          write=write)
        if self.ctx.dry_run and write:
            return {"code": 0, "message": "(dry-run 未执行)", "_dry_run": True}
        if not resp:
            return {"code": 0, "message": "Success (empty response)", "_empty": True}
        if resp.get("code") not in (0, None):
            raise IKuaiError("%s.%s 失败：%s %s"
                             % (func_name, action, resp.get("message"),
                                mask_secret(str(resp.get("errors", "")))))
        return resp


WRITE_ACTIONS = {"install", "add", "update", "del", "delete", "remove",
                 "save", "up", "down", "restart", "clean", "export"}


def is_write(func_name, action):
    """区分读写：只有写操作会被 --dry-run 拦下，读操作照常执行"""
    return str(action).lower() in WRITE_ACTIONS


# --------------------------------------------------------------------
# 容器 / 镜像辅助
# --------------------------------------------------------------------
def split_image(image):
    """'gdy666/lucky:latest' → ('gdy666', 'lucky', 'latest')"""
    tail = image.rsplit("/", 1)[-1]
    if ":" in tail:
        repo, _, tag = image.rpartition(":")
    else:
        repo, tag = image, "latest"
    if "/" in repo:
        ns, _, name = repo.rpartition("/")
    else:
        ns, name = "library", repo
    return ns, name, tag


def repo_of(image):
    """去掉 tag，保留完整仓库路径：'ghcr.io/a/b:1' → 'ghcr.io/a/b'"""
    tail = image.rsplit("/", 1)[-1]
    return image.rsplit(":", 1)[0] if ":" in tail else image


def container_payload(c, insp, tag=None, start=None):
    """用容器现有配置 + 新 tag 拼出 add/update 的 param"""
    ports, unmapped = [], 0
    for p in c.get("ports") or []:
        pub = p.get("PublicPort")
        if pub:
            ports.append("%s:%s/%s" % (pub, p.get("PrivatePort"),
                                       (p.get("Type") or "tcp").lower()))
        else:
            unmapped += 1
    i = insp or {}
    if start is None:
        # 保持容器原来的运行状态，不擅自启动/停止
        start = "yes" if c.get("state") == "running" else "no"
    return {
        "id": c["id"],
        "name": c["name"],
        "image": repo_of(c["image"]),
        "tag": tag or split_image(c["image"])[2],
        "interface": c.get("interface", ""),
        "ipaddr": c.get("ipaddr", ""),
        "ip6addr": c.get("ip6addr", ""),
        "mounts": c.get("mounts", ""),
        "ports": ",".join(ports),
        "env": c.get("env", ""),
        "cmd": c.get("cmd", ""),
        "comment": c.get("comment", ""),
        "auto_start": 1 if str(c.get("auto_start", "0")) in ("1", "true") else 0,
        "memory": int(i.get("memory") or 0),
        "file_size": int(i.get("file_size") or 1048576),
        "file_num": 1,
        "cpushares": int(i.get("cpushares") or 0),
        "enabled": start,
    }, unmapped


# --------------------------------------------------------------------
# 镜像仓库 digest（不依赖路由器）
# --------------------------------------------------------------------
def _fetch(url, headers=None, timeout=20):
    req = urllib.request.Request(url)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    return urllib.request.urlopen(req, timeout=timeout)


def _token(www, repo, timeout):
    realm = re.search(r'realm="([^"]+)"', www)
    if not realm:
        raise IKuaiError("无法解析认证地址")
    q = ["scope=repository:%s:pull" % repo]
    svc = re.search(r'service="([^"]+)"', www)
    if svc:
        q.insert(0, "service=" + svc.group(1))
    with _fetch(realm.group(1) + "?" + "&".join(q), timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8"))
    return d.get("token") or d.get("access_token") or ""


def remote_digest(registries, image, tag, ctx, timeout=20):
    if isinstance(registries, str):
        registries = [registries]
    repo = image if "/" in image else "library/" + image
    errors = []
    for reg in registries:
        url = "%s/v2/%s/manifests/%s" % (reg.rstrip("/"), repo, tag)
        headers = {"Accept": ACCEPT_MANIFEST}
        try:
            try:
                resp = _fetch(url, headers, timeout)
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    www = e.headers.get("WWW-Authenticate", "")
                    if not www.lower().startswith("bearer"):
                        raise IKuaiError("HTTP %s" % e.code)
                    headers["Authorization"] = "Bearer " + _token(www, repo, timeout)
                    resp = _fetch(url, headers, timeout)
                else:
                    raise IKuaiError("HTTP %s" % e.code)
            with resp:
                d = resp.headers.get("Docker-Content-Digest") or (
                    "sha256:" + hashlib.sha256(resp.read()).hexdigest())
            ctx.vlog("%s 命中 %s:%s" % (reg, repo, tag))
            return d, reg
        except Exception as e:
            errors.append("%s → %s" % (reg, e))
            ctx.vlog("%s 失败：%s" % (reg, e))
    raise IKuaiError("所有仓库都查不到 %s:%s\n    %s" % (image, tag, "\n    ".join(errors)))


def notify(cfg, message):
    url = cfg.get("notify_webhook")
    if not url:
        return
    tpl = json.dumps(cfg.get("notify_body") or
                     {"msgtype": "text", "text": {"content": "{message}"}}, ensure_ascii=False)
    body = tpl.replace("{message}", message)
    try:
        req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/json;charset=utf-8")
        with urllib.request.urlopen(req, timeout=10) as r:
            log("  · 通知已发送（HTTP %s）" % r.status)
    except Exception as e:
        log("  · 通知失败：%s" % e)


# --------------------------------------------------------------------
# 命令
# --------------------------------------------------------------------
def get_containers(ik):
    return (ik.call("docker_container", "show", {"TYPE": "data"}).get("results") or {}).get("data") or []


def get_local_images(ik):
    return (ik.call("docker_image", "show", {"TYPE": "data"}).get("results") or {}).get("data") or []


def get_inspect(ik, cid):
    try:
        res = ik.call("docker_container", "show", {"TYPE": "inspect", "id": cid})
        lst = (res.get("results") or {}).get("inspect") or []
        return lst[0] if lst else {}
    except IKuaiError:
        return {}


def cmd_list(args, cfg, ctx):
    ik = IKuai(cfg, ctx)
    ik.login()
    cs = get_containers(ik)
    if not cs:
        log("没有容器（或 Docker 服务未启动）")
        return 0
    w = max([len(c["name"]) for c in cs] + [6])
    log("\n%-*s  %-34s  %-13s  %-15s  %s" % (w, "容器", "镜像", "网卡", "IP", "状态"))
    log("-" * (w + 84))
    for c in cs:
        log("%-*s  %-34s  %-13s  %-15s  %s"
            % (w, c["name"], c.get("image", ""), c.get("interface", ""),
               c.get("ipaddr", ""), c.get("status", "")))
    log("\n共 %d 个容器" % len(cs))
    return 0


def cmd_images(args, cfg, ctx):
    ik = IKuai(cfg, ctx)
    ik.login()
    ims = get_local_images(ik)
    w = max([len(i.get("name", "")) for i in ims] + [6])
    log("\n%-*s  %-14s  %-11s  %-18s  %s" % (w, "镜像", "tag", "大小", "拉取时间", "使用中"))
    log("-" * (w + 64))
    for i in ims:
        ts = i.get("install")
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"
        log("%-*s  %-14s  %-11s  %-18s  %s"
            % (w, i.get("name", ""), i.get("tag", ""),
               "%.1f MB" % ((i.get("size") or 0) / 1048576.0), when,
               "%d 个容器" % (i.get("container_count") or 0)))
    log("\n共 %d 个镜像" % len(ims))
    return 0


def cmd_tags(args, cfg, ctx):
    ik = IKuai(cfg, ctx)
    ik.login()
    target = args.image
    if "/" in target:
        ns, name = target.rpartition("/")[0], target.rpartition("/")[2]
    else:
        ns, name = "library", target
    res = ik.call("docker_image", "show",
                  {"TYPE": "tag", "name": name, "namespace": ns, "page": 1, "page_size": 100})
    tags = (res.get("results") or {}).get("tag_list") or []
    if not tags:
        log("没查到 tag，检查镜像名是否正确（官方镜像的 namespace 是 library）")
        return 1
    log("\n%s 共 %d 个 tag：" % (target, len(tags)))
    for i in range(0, len(tags), 6):
        log("  " + "  ".join("%-16s" % t for t in tags[i:i + 6]))
    return 0


def cmd_server(args, cfg, ctx):
    ik = IKuai(cfg, ctx)
    ik.login()
    ov = ik.call("docker_server", "show", {"TYPE": "overview"}).get("results", {})
    log("\n===== Docker 服务 =====")
    log("  状态: %s   Docker 版本: %s   API: %s"
        % ("运行中" if ov.get("status") == 1 else "已停止",
           ov.get("docker_version"), ov.get("api_version")))
    o = ov.get("overview") or {}
    log("  容器: %s" % json.dumps(o.get("containers"), ensure_ascii=False))
    log("  镜像: %s" % json.dumps(o.get("images"), ensure_ascii=False))
    for d in (ik.call("docker_server", "show", {"TYPE": "data"}).get("results") or {}).get("data") or []:
        log("\n===== 服务设置 =====")
        log("  存储盘: %s   自启: %s   启用: %s"
            % (d.get("workdisk"), d.get("autostart"), d.get("enabled")))
        log("  镜像加速源:")
        for m in (d.get("mirrors") or "").split(","):
            if m.strip():
                log("    - " + m.strip())
    return 0


def cmd_check(args, cfg, ctx):
    ik = IKuai(cfg, ctx)
    ik.login()
    registries = cfg.get("registries") or DEFAULT_REGISTRIES
    state = json.load(open(STATE_FILE, encoding="utf-8")) if os.path.exists(STATE_FILE) else {}

    local = {}
    for i in get_local_images(ik):
        local["%s:%s" % (i.get("name"), i.get("tag"))] = i

    rows, changed, failed, seen = [], [], [], set()
    for c in get_containers(ik):
        image = c.get("image") or ""
        repo, tag = repo_of(image), split_image(image)[2]
        key = "%s:%s" % (repo, tag)
        if key in seen:
            continue
        seen.add(key)
        li = local.get(key) or {}
        local_ts = (time.strftime("%m-%d %H:%M", time.localtime(li["install"]))
                    if li.get("install") else "未安装")
        try:
            digest, reg = remote_digest(registries, repo, tag, ctx, cfg.get("timeout", 20))
        except Exception as e:
            rows.append((c["name"], key, "查询失败", local_ts, str(e).splitlines()[0][:30]))
            failed.append(c["name"])
            continue
        old = state.get(key, {}).get("digest")
        sig = "首次记录" if old is None else ("⬆ 有新版本" if old != digest else "已是最新")
        if old is not None and old != digest:
            changed.append(c["name"])
        state[key] = {"digest": digest, "registry": reg,
                      "checked_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        rows.append((c["name"], key, sig, local_ts, digest[:19]))

    w1 = max([len(r[0]) for r in rows] + [6])
    w2 = max([len(r[1]) for r in rows] + [6])
    log("\n%-*s  %-*s  %-12s  %-12s  %s" % (w1, "容器", w2, "镜像", "上游状态", "本地拉取于", "上游digest"))
    log("-" * (w1 + w2 + 56))
    for r in rows:
        log("%-*s  %-*s  %-12s  %-12s  %s" % (w1, r[0], w2, r[1], r[2], r[3], r[4]))
    if not ctx.dry_run:
        json.dump(state, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    if changed:
        log("\n发现 %d 个容器有新版本：%s" % (len(changed), "、".join(changed)))
        log("更新命令：python %s update %s" % (os.path.basename(__file__), " ".join(changed)))
        notify(cfg, "爱快容器有新版本：" + "、".join(changed))
    else:
        log("\n没有检测到新版本")
    if failed:
        log("\n%d 个查询失败（加 -v 看原因，或调整 config.json 的 registries）" % len(failed))
    return 0


def wait_for_image(ik, repo, tag, since, timeout, ctx):
    """轮询本地镜像列表，等待新镜像下载完成"""
    deadline = time.time() + timeout
    note = ""
    while time.time() < deadline:
        for i in get_local_images(ik):
            if i.get("name") == repo and str(i.get("tag")) == str(tag):
                if (i.get("install") or 0) >= since:
                    return True, i
                note = "镜像已存在但拉取时间未更新（可能本来就是最新）"
        time.sleep(3)
        if ctx.verbose:
            print("    ... 等待下载，剩余 %ds" % int(deadline - time.time()), flush=True)
    return False, note


def cmd_update(args, cfg, ctx):
    ik = IKuai(cfg, ctx)
    ik.login()
    cs = get_containers(ik)
    if not cs:
        raise IKuaiError("没读到任何容器，先确认 Docker 服务在运行（可用 server 命令查看）")

    want = [c for c in cs if c["name"] in args.names]
    missing = set(args.names) - set(c["name"] for c in want)
    if missing:
        raise IKuaiError("找不到容器：%s\n现有容器：%s"
                         % ("、".join(missing), "、".join(c["name"] for c in cs)))

    plan = [(c, split_image(c["image"])[0], split_image(c["image"])[1],
             split_image(c["image"])[2], args.tag or split_image(c["image"])[2]) for c in want]

    log("\n将要执行的更新：")
    for c, ns, name, cur, new in plan:
        log("  · %-14s %s:%s → %s:%s" % (c["name"], repo_of(c["image"]), cur, repo_of(c["image"]), new))
    if not args.yes and not ctx.dry_run:
        try:
            if input("\n确认执行？[y/N] ").strip().lower() not in ("y", "yes"):
                log("已取消")
                return 0
        except EOFError:
            log("已取消")
            return 0

    for c, ns, name, cur, new in plan:
        log("\n========== %s ==========" % c["name"])
        payload, unmapped = container_payload(c, get_inspect(ik, c["id"]), tag=new)
        if ctx.verbose or ctx.dry_run:
            log("  容器参数：%s" % json.dumps(payload, ensure_ascii=False))
        if unmapped:
            log("  · 端口：容器声明了 %d 个端口但未做宿主映射，"
                "爱快 doc_docker 网络是路由直连，属正常" % unmapped)

        log("  [1/2] 拉取镜像 %s/%s:%s" % (ns, name, new))
        if ctx.dry_run:
            ik.call("docker_image", "install",
                    {"name": name, "tag": new, "image_logo": c.get("image_logo", ""),
                     "namespace": ns})
        else:
            since = int(time.time())
            try:
                ik.call("docker_image", "install",
                        {"name": name, "tag": new, "image_logo": c.get("image_logo", ""),
                         "namespace": ns})
            except IKuaiError as e:
                if "was installed" in str(e) or "already" in str(e).lower():
                    log("  · 镜像已是最新，跳过下载")
                else:
                    raise
            else:
                ok, note = wait_for_image(ik, repo_of(c["image"]), new, since, args.wait, ctx)
                if ok:
                    log("  · 镜像下载完成")
                else:
                    log("  · ⚠ 未在 %ds 内确认下载完成（%s）" % (args.wait, note or "超时"))
                    if not args.force:
                        log("  · 已跳过容器更新，避免用旧镜像重建。确认镜像已下载后重跑，或加 --force")
                        continue
                    log("  · --force 已指定，继续更新容器")

        log("  [2/2] 就地更新容器（docker_container.update）")
        ik.call("docker_container", "update", payload)
        if not ctx.dry_run:
            time.sleep(3)
            after = {x["name"]: x for x in get_containers(ik)}.get(c["name"])
            if after:
                log("  ✅ %s → %s   状态：%s" % (c["name"], after.get("image"), after.get("status")))
            else:
                log("  ⚠ %s 更新后没查到，请到爱快后台确认" % c["name"])

    if not ctx.dry_run and not args.no_notify:
        notify(cfg, "爱快容器已更新：" + "、".join(p[0]["name"] for p in plan))
    return 0


def cmd_raw(args, cfg, ctx):
    ik = IKuai(cfg, ctx)
    ik.login()
    try:
        payload = json.loads(args.json)
    except ValueError as e:
        raise IKuaiError("json 解析失败：%s" % e)
    res = ik.call(payload.get("func_name"), payload.get("action"), payload.get("param"))
    log(json.dumps(res, ensure_ascii=False, indent=2)[:6000])
    return 0


# --------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(
        description="爱快 4.0 Docker 容器自动更新工具（原生 API）",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("-c", "--config", default=CONFIG_FILE)
    p.add_argument("-n", "--dry-run", action="store_true", help="只打印请求不执行")
    p.add_argument("-y", "--yes", action="store_true", help="跳过确认")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd")

    for name, fn, help_text in [("list", cmd_list, "列出容器"),
                                ("images", cmd_images, "列出本地镜像"),
                                ("server", cmd_server, "Docker 服务设置"),
                                ("check", cmd_check, "检测上游是否有新版本")]:
        sp = sub.add_parser(name, help=help_text)
        sp.set_defaults(func=fn)

    sp = sub.add_parser("tags", help="查询镜像可用 tag")
    sp.add_argument("image")
    sp.set_defaults(func=cmd_tags)

    sp = sub.add_parser("update", help="拉新镜像并就地更新容器")
    sp.add_argument("names", nargs="+")
    sp.add_argument("--tag", help="指定目标 tag，默认沿用容器当前 tag")
    sp.add_argument("--wait", type=int, default=180, help="等待镜像下载完成的秒数")
    sp.add_argument("--force", action="store_true", help="未确认下载完成也继续更新容器")
    sp.add_argument("--no-notify", action="store_true")
    sp.set_defaults(func=cmd_update)

    sp = sub.add_parser("raw", help="任意调用，排查用")
    sp.add_argument("json")
    sp.set_defaults(func=cmd_raw)

    args = p.parse_args()
    if not getattr(args, "func", None):
        p.print_help()
        return 0
    ctx = Ctx(verbose=args.verbose, dry_run=args.dry_run)
    try:
        cfg = load_config(args.config)
        return args.func(args, cfg, ctx)
    except IKuaiError as e:
        log("\n❌ " + str(e))
        return 2
    except KeyboardInterrupt:
        log("\n已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
