#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
爱快（iKuai）4.0 Docker API 客户端 —— 自包含、零第三方依赖。

从 ikuai_docker_updater.py 抽出，改动点：
  · 配置改为从环境变量读取（Docker 友好），也支持 /data/config.json 覆盖
  · 日志改为可注入回调，便于把过程实时推送到 Web 前端
  · 所有网络动作都通过 Callback 报告进度，方便做可视化流程

接口细节见同目录 ../../API_REFERENCE.md
"""

import base64
import hashlib
import http.cookiejar
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request

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

WRITE_ACTIONS = {"install", "add", "update", "del", "delete", "remove",
                 "save", "up", "down", "restart", "clean", "export"}


class IKuaiError(Exception):
    pass


def mask_secret(text):
    """把请求/响应里的凭据打码，避免日志泄漏"""
    text = str(text)
    text = re.sub(r'("passwd"\s*:\s*")[^"]*(")', r"\1******\2", text)
    text = re.sub(r'("pass"\s*:\s*")[^"]*(")', r"\1******\2", text)
    text = re.sub(r'("password"\s*:\s*")[^"]*(")', r"\1******\2", text)
    return text


def is_write(action):
    """只有写操作需要走 dry-run 拦截"""
    return str(action).lower() in WRITE_ACTIONS


# 爱快对「镜像已是最新、无需重复下载」返回的提示（中英文都要覆盖）。
# 实测 4.0 返回中文：已存在相同内容 / 该镜像已存在，请勿重复下载
ALREADY_EXISTS_MARKERS = (
    "was installed", "already", "已存在", "请勿重复下载", "相同内容", "无需下载",
)


def is_already_exists(err):
    """判断错误是否只是「镜像已是最新」——这不算失败，应放行"""
    text = str(err).lower()
    return any(m.lower() in text for m in ALREADY_EXISTS_MARKERS)


# --------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------
def load_config():
    """环境变量优先，/data/config.json 可覆盖（便于不改镜像改配置）"""
    cfg = {
        "router": {
            "url": (os.environ.get("IKUAI_URL") or "").strip(),
            "username": (os.environ.get("IKUAI_USER") or "").strip(),
            "password": os.environ.get("IKUAI_PASS") or "",
            "login_path": "/Action/login",
        },
        "registries": [r.strip() for r in
                       (os.environ.get("REGISTRIES") or "").split(",") if r.strip()]
                      or list(DEFAULT_REGISTRIES),
        "timeout": int(os.environ.get("HTTP_TIMEOUT") or 20),
        # 等待镜像下载完成的最长秒数。页面上改过的值会落盘到 settings.json，
        # 并覆盖这里的默认值（见 server.py 里 STORE 的初始化）。
        "pull_timeout": int(os.environ.get("PULL_TIMEOUT") or 900),
        "dry_run": (os.environ.get("DRY_RUN") or "").lower() in ("1", "true", "yes"),
        "data_dir": os.environ.get("DATA_DIR") or "/data",
    }
    path = os.environ.get("CONFIG_FILE") or os.path.join(cfg["data_dir"], "config.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                user = json.load(f)
            for k, v in user.items():
                if k == "router" and isinstance(v, dict):
                    cfg["router"].update(v)
                else:
                    cfg[k] = v
        except Exception:
            pass
    return cfg


class Ctx(object):
    """把 CLI 的过程输出改成可注入回调，Web 端据此推送日志"""

    def __init__(self, logger=None, verbose=False):
        self.logger = logger or (lambda msg, **kw: None)
        self.verbose = verbose

    def vlog(self, msg):
        if self.verbose:
            self.logger(str(msg), level="debug")


# --------------------------------------------------------------------
# API 客户端
# --------------------------------------------------------------------
class IKuai(object):
    def __init__(self, cfg, ctx):
        self.cfg = cfg
        self.ctx = ctx
        r = cfg["router"]
        host = (r.get("url") or "").strip()
        if not host:
            raise IKuaiError("未配置爱快地址，请设置环境变量 IKUAI_URL")
        # 默认 https（爱快后台默认是 https）；若用户显式写了 http:// 就尊重它，
        # 方便在反代 / 内网调试场景下直连。
        scheme = "https"
        for s in ("https://", "http://"):
            if host.startswith(s):
                scheme = s[:-3]
                host = host[len(s):]
                break
        self.host = host
        self.base = scheme + "://" + host
        self.timeout = int(cfg.get("timeout", 20))
        sctx = ssl.create_default_context()
        sctx.check_hostname = False
        sctx.verify_mode = ssl.CERT_NONE
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=sctx),
        )
        self._lock = threading.Lock()

    # -- 底层请求 ---------------------------------------------------
    def _post(self, path, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=body, method="POST")
        req.add_header("Content-Type", "application/json;charset=utf-8")
        req.add_header("Accept", "application/json, text/plain, */*")
        req.add_header("Referer", self.base + "/")
        req.add_header("Origin", self.base)
        self.ctx.vlog("POST %s  %s" % (path, mask_secret(json.dumps(payload, ensure_ascii=False))))
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
        except Exception as e:
            raise IKuaiError("请求 %s 异常：%s" % (path, e))
        self.ctx.vlog("← %s" % mask_secret(text[:600]))
        if not text.strip():
            return {}                       # add/update/install 成功时是空响应
        try:
            return json.loads(text)
        except ValueError:
            raise IKuaiError("返回不是 JSON：%s" % mask_secret(text[:300]))

    def login(self):
        r = self.cfg["router"]
        user = r.get("username")
        pw = r.get("password") or ""
        if not user:
            raise IKuaiError("未配置爱快账号，请设置环境变量 IKUAI_USER / IKUAI_PASS")
        payload = {
            "username": user,
            "passwd": hashlib.md5(pw.encode()).hexdigest(),
            "pass": base64.b64encode(("salt_11" + pw).encode()).decode(),
            "remember_password": "",
        }
        resp = self._post(r.get("login_path", "/Action/login"), payload)
        if not (resp.get("Result") == 10000 or resp.get("code") == 0):
            raise IKuaiError("登录失败：%s" % mask_secret(json.dumps(resp, ensure_ascii=False)[:200]))
        self.ctx.vlog("登录成功，cookies=%s" % [c.name for c in self.jar])
        return resp

    def call(self, func_name, action, param=None):
        write = is_write(action)
        if write and self.cfg.get("dry_run"):
            self.ctx.logger("  ↑ DRY-RUN：已跳过写操作 %s.%s" % (func_name, action), level="warn")
            return {"code": 0, "message": "(dry-run 未执行)", "_dry_run": True}
        with self._lock:
            resp = self._post("/Action/call",
                              {"func_name": func_name, "action": action, "param": param or {}})
        if not resp:
            return {"code": 0, "message": "Success (empty response)", "_empty": True}
        if resp.get("code") not in (0, None):
            raise IKuaiError("%s.%s 失败：%s %s"
                             % (func_name, action, resp.get("message"),
                                mask_secret(str(resp.get("errors", "")))))
        return resp


# --------------------------------------------------------------------
# 容器 / 镜像辅助
# --------------------------------------------------------------------
def split_image(image):
    """'gdy666/lucky:latest' → ('gdy666', 'lucky', 'latest')"""
    image = image or ""
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
    """去掉 tag：'ghcr.io/a/b:1' → 'ghcr.io/a/b'"""
    image = image or ""
    tail = image.rsplit("/", 1)[-1]
    return image.rsplit(":", 1)[0] if ":" in tail else image


def container_payload(c, insp, tag=None, start=None):
    """用容器现有配置 + 新 tag 拼出 add/update 的 param

    **``image`` 必须写成 ``repo:tag`` 全名**。爱快前端提交容器表单时，镜像是
    一个下拉选择，其 value 就是 ``image + ":" + tag``，payload 里**没有单独的
    tag 字段**；服务端只认 ``image``。
    早期这里只传仓库名（``repo_of(c["image"])``）、把 tag 塞进 ``tag`` 字段，
    结果是：容器确实被重建了（``created`` 刷新），但**仍在跑旧镜像** ——
    实测 ``lucky`` 指定 2.27.2 更新后，容器依旧挂在 2.20.2 上，新镜像的
    ``containers`` 为空。改成全名后才会真正切换。
    """
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
        start = "yes" if c.get("state") == "running" else "no"
    repo = repo_of(c["image"])
    use_tag = (tag or "").strip() or split_image(c["image"])[2] or "latest"
    return {
        "id": c["id"],
        "name": c["name"],
        "image": "%s:%s" % (repo, use_tag),
        "tag": use_tag,
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
# 读接口
# --------------------------------------------------------------------
def get_containers(ik):
    """容器列表。爱快在容器状态变化的瞬间可能返回重复条目，这里按 id 去重（保留后者）"""
    raw = (ik.call("docker_container", "show", {"TYPE": "data"}).get("results") or {}).get("data") or []
    seen, out = {}, []
    for c in raw:
        key = c.get("id") or c.get("name")
        if key in seen:
            out[seen[key]] = c            # 后出现的覆盖先出现的
        else:
            seen[key] = len(out)
            out.append(c)
    return out


def get_local_images(ik):
    return (ik.call("docker_image", "show", {"TYPE": "data"}).get("results") or {}).get("data") or []


class LocalImages(object):
    """本地镜像索引。

    两个必须绕开的爱快数据特性（实测）：
      · 镜像的 ``tag`` 可能是**逗号分隔的多标签**，如 ``"2.20.2,latest"``，
        因此不能用 ``repo:tag`` 直接精确匹配，必须把 tag 拆开逐个建索引，
        否则 ``repo:latest`` 这类容器会误判成「本地未安装」。
      · ``install`` 为 0 表示爱快没有拉取记录（镜像由导入/引用而来），
        此时退回 ``created``（镜像构建时间）并标记来源，不要显示成「未安装」。
    """

    __slots__ = ("images", "_by_tag", "_by_repo")

    def __init__(self, images):
        self.images = list(images or [])
        self._by_tag, self._by_repo = {}, {}
        for img in self.images:
            name = img.get("name") or ""
            self._by_repo.setdefault(name, []).append(img)
            for t in str(img.get("tag") or "").split(","):
                t = t.strip()
                if t:
                    self._by_tag["%s:%s" % (name, t)] = img

    def find(self, repo, tag):
        """按 repo:tag 查镜像；精确匹配不到且同仓库只有一个镜像时按仓库认领。"""
        img = self._by_tag.get("%s:%s" % (repo, tag))
        if img is not None:
            return img
        cand = self._by_repo.get(repo) or []
        return cand[0] if len(cand) == 1 else None

    def find_for(self, container):
        image = container.get("image") or ""
        return self.find(repo_of(image), split_image(image)[2])

    @staticmethod
    def pulled_at(img):
        """返回 (格式化时间, 来源)。来源 'pull'=拉取时间，'build'=镜像构建时间。"""
        if not img:
            return "", ""
        for field, src in (("install", "pull"), ("created", "build")):
            ts = img.get(field) or 0
            if ts:
                return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)), src
        return "", ""

    def __len__(self):
        return len(self.images)


def image_signature(ik, repo, tag):
    """本地镜像的内容指纹（镜像 ID）。

    判断「拉到的镜像和本地是不是同一份」不能看错误信息，也不能看 ``install``
    时间戳 —— 爱快对已存在的镜像有时返回「已存在相同内容」错误，有时只是安静地
    重新登记并把 ``install`` 刷成当前时间（实测两者都会出现）。只有镜像 ID
    变化才代表内容真的变了。返回空串表示取不到。
    """
    img = LocalImages(get_local_images(ik)).find(repo, tag)
    return str((img or {}).get("id") or "")


def image_owner(ik, container_name):
    """反查某容器**实际**挂在哪条镜像上。

    容器自身的 ``image`` 字段可能不带 tag（实测 ``lucky`` 就是 ``gdy666/lucky``），
    光看它分不出跑的是哪个版本。镜像条目的 ``containers`` 字段记录了占用关系，
    这是唯一可靠的依据。返回 ``{"repo","tag","id"}`` 或 ``None``。
    """
    for img in get_local_images(ik):
        cs = img.get("containers")
        if isinstance(cs, dict):
            cs = list(cs.values())
        for c in cs or []:
            if isinstance(c, dict) and c.get("name") == container_name:
                return {"repo": img.get("name") or "",
                        "tag": img.get("tag") or "",
                        "id": str(img.get("id") or "")}
    return None


def tag_matches(owner_tag, want_tag):
    """镜像条目的 tag 可能是逗号分隔多标签（如 "1.37.3,latest"）"""
    if not owner_tag or not want_tag:
        return False
    return want_tag.strip() in [t.strip() for t in str(owner_tag).split(",") if t.strip()]


def get_container_logs(ik, cid, limit=30, span=3600):
    """取容器日志（只读）。

    用来确认「容器 Up，但主进程其实没在干活」这类问题：容器状态 running
    只说明 PID 1 还活着。实测踩过的坑 —— lucky 换 entrypoint 后启动脚本
    空转，容器一直 Up、内存仅几十 KB、端口不监听，而工具当时只看了
    state=running 就判定成功。返回行数上限 limit，取不到时返回空列表。
    """
    try:
        now = int(time.time())
        res = ik.call("docker_container", "show",
                      {"TYPE": "log", "id": cid,
                       "starttime": now - int(span), "endtime": now})
    except Exception:                                           # noqa: BLE001
        return []
    logs = ((res or {}).get("results") or {}).get("log") or []
    out = [str(x).strip() for x in logs if str(x).strip()]
    return out[-limit:] if limit else out


def get_inspect(ik, cid):
    try:
        res = ik.call("docker_container", "show", {"TYPE": "inspect", "id": cid})
        lst = (res.get("results") or {}).get("inspect") or []
        return lst[0] if lst else {}
    except IKuaiError:
        return {}


def get_server(ik):
    ov = ik.call("docker_server", "show", {"TYPE": "overview"}).get("results", {}) or {}
    data = (ik.call("docker_server", "show", {"TYPE": "data"}).get("results") or {}).get("data") or []
    return {"overview": ov, "settings": data[0] if data else {}}


def get_tags(ik, image):
    if "/" in image:
        ns, _, name = image.rpartition("/")
    else:
        ns, name = "library", image
    res = ik.call("docker_image", "show",
                  {"TYPE": "tag", "name": name, "namespace": ns, "page": 1, "page_size": 100})
    return (res.get("results") or {}).get("tag_list") or []


# --------------------------------------------------------------------
# 上游镜像 digest（不依赖路由器，用来判断是否有新版）
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


def get_mirrors(ik):
    """读取爱快自己配置的镜像加速源。

    为什么要用它、而不是本工具另配一套：**查版本的源必须和爱快拉取时用的源一致**。
    不同加速源的缓存同步时间并不一致，用另一个源去查就会出现
    「页面说有新版、爱快点下去却拉不到」或者反过来的错位。
    """
    d = ik.call("docker_server", "show", {"TYPE": "data"})
    rows = (d.get("results") or {}).get("data") or []
    out = []
    for r in rows:
        for m in str(r.get("mirrors") or "").split(","):
            m = m.strip().rstrip("/")
            if not m:
                continue
            if not m.startswith(("http://", "https://")):
                m = "https://" + m
            if m not in out:
                out.append(m)
    return out


def resolve_registries(ik, cfg, log=None):
    """查上游 digest 该用哪些源：优先爱快自己配的，读不到再退回本地配置。

    好处是改加速源只需在爱快后台改一处，本工具自动跟随，不用两边各维护一份。
    """
    def say(msg, level="info"):
        if log:
            try:
                log(msg, level=level)
            except Exception:                               # noqa: BLE001
                pass

    fallback = list(cfg.get("registries") or DEFAULT_REGISTRIES)
    try:
        mirrors = get_mirrors(ik)
    except Exception as e:                                  # noqa: BLE001
        say("读取爱快加速源失败（%s），改用本工具配置的加速源" % e, "warn")
        return fallback
    if mirrors:
        return mirrors
    say("爱快未配置镜像加速源，改用本工具配置的加速源", "warn")
    return fallback


def remote_digest(registries, image, tag, ctx=None, timeout=20):
    """多加速源依次回退，返回 (digest, 命中的加速源)"""
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
            if ctx:
                ctx.vlog("%s 命中 %s:%s" % (reg, repo, tag))
            return d, reg
        except Exception as e:
            errors.append("%s → %s" % (reg, e))
            if ctx:
                ctx.vlog("%s 失败：%s" % (reg, e))
    raise IKuaiError("所有加速源都查不到 %s:%s" % (image, tag))


def wait_for_image(ik, repo, tag, since, timeout, ctx):
    """轮询本地镜像列表，等待新镜像下载完成"""
    deadline = time.time() + timeout
    note = ""
    last_left = None
    while time.time() < deadline:
        for i in get_local_images(ik):
            if i.get("name") == repo and str(i.get("tag")) == str(tag):
                if (i.get("install") or 0) >= since:
                    return True, i
                note = "镜像已存在但拉取时间未更新（可能本来就是最新）"
        left = int(deadline - time.time())
        if ctx and last_left is not None and last_left - left >= 15:
            ctx.logger("等待镜像下载中，剩余约 %ds" % left, level="debug")
        if last_left is None or last_left - left >= 15:
            last_left = left
        time.sleep(3)
    return False, note
