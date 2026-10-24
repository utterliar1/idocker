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
        # ⚠ 刻意关闭证书校验：爱快后台用的是自签证书，内网直连时无法通过 CA 校验，
        #   不开就只能连不上。代价是这条链路对 MITM 不设防 —— 因此本工具只应部署
        #   在受信任的局域网内，不要把它暴露到公网。若爱快换了可信证书，
        #   应改回 create_default_context() 的默认校验。
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


# 「纯版本号」tag：1.37.3 / v2.27 / 3 之类。
# 刻意**不匹配**带后缀的变体（1.37.3-alpine）和语义 tag（latest / ssh / slim）——
# 它们和 latest 是并列的不同东西，不是版本新旧关系，切过去等于换了产品。
_VERSION_TAG_RE = re.compile(r"^v?\d+(?:\.\d+)*$")


def is_version_tag(tag):
    """tag 是不是「锁定的具体版本号」形式。

    为什么要单独认这个：容器一旦被用 ``--tag 1.37.3`` 更新过，它的 image 就变成
    ``repo:1.37.3``、**永久锁在该 tag 上，不再跟随 latest**。而版本号 tag 的上游内容
    是固定的，所以检测永远报「已是最新」—— 实测 vaultwarden 锁在 1.37.3、latest
    其实早已是 1.37.4，用户看到的就是「明明有新版却检测不出来」。
    识别出来才能把这件事显式告诉用户（见 ``jobs._run_check`` 的 ``pinned`` 字段）。
    """
    return bool(_VERSION_TAG_RE.match(str(tag or "").strip()))



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


_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def norm_digest(value):
    """把各种写法的指纹统一成 64 位小写十六进制。

    ``sha256:AB…`` / ``AB…`` / ``sha256:ab…`` → ``ab…``。
    取不到、或不是完整的 sha256（例如被截断成 12 位短 ID）时返回空串 ——
    「不知道」必须能和「不一样」区分开，否则会把截断 ID 当成内容不同，
    每次检测都误报有新版本。
    """
    s = str(value or "").strip().lower()
    if s.startswith("sha256:"):
        s = s[len("sha256:"):]
    return s if _HEX64.match(s) else ""


def same_content(a, b):
    """两处指纹是否指向同一份镜像内容。任一侧不完整就一律 False（不敢下结论）。"""
    na, nb = norm_digest(a), norm_digest(b)
    return bool(na) and na == nb


def del_local_image(ik, repo, tag):
    """删除一条本地镜像记录，并**回读确认真的删掉了**。

    爱快的 ``docker_image.del`` 会静默失败：参数对不上时照样回
    ``{"code":0,"message":"Success"}`` 而记录一动不动（实测）。
    能对上号的写法是「**完整 repo 名** + **未截断的真实 tag**」，``name``/``namespace``
    拆开传无效；``id`` 一般不参与匹配。所以这里既**不发 id 优先**，也**发 id 兜底** ——
    最后一定回读清单确认，绝不把 Success 当成功。

    返回 ``(ok, detail)``。
    """
    def matched():
        out = []
        for img in get_local_images(ik):
            if img.get("name") != repo:
                continue
            if tag_matches(img.get("tag"), tag):
                out.append(img)
        return out

    rows = matched()
    if not rows:
        return True, "本地已无 %s:%s 记录" % (repo, tag)
    # 🔴 安全阀：还有容器挂着的记录**一律不删**。
    # 2026-10-22 真机事故：搬运途中并发删了「正被切过去」的镜像记录，爱快删容器后
    # 没能重建，容器直接消失（clash 就是这么没的）。宁可报错让人工处理，
    # 也不能在工具里留下「删一条记录顺带删掉一个容器」的可能。
    busy = []
    for r in rows:
        cs = r.get("containers")
        if isinstance(cs, dict):
            cs = list(cs.values())
        for c in (cs or []):
            if isinstance(c, dict) and c.get("name"):
                busy.append(str(c["name"]))
    if busy:
        return False, ("记录仍被容器占用（%s），拒绝删除 —— 先让容器切到别的 tag 再来"
                       % "、".join(sorted(set(busy))))
    ids = [str(r.get("id") or "") for r in rows if r.get("id")]
    shapes = [{"name": repo, "tag": tag}]
    for i in ids:
        shapes.append({"name": repo, "tag": tag, "id": i})
    last = ""
    for shape in shapes:
        try:
            last = json.dumps(ik.call("docker_image", "del", shape), ensure_ascii=False)
        except Exception as e:                                   # noqa: BLE001
            last = str(e)
        if not matched():
            return True, "已删除（参数 %s）" % json.dumps(shape, ensure_ascii=False)
    left = len(matched())
    return False, ("爱快回了 Success 但记录仍在（%d 条，试过 %d 种参数，末次返回 %s）"
                   % (left, len(shapes), last[:160]))


def exact_image(ik, repo, tag):
    """按 ``repo:tag`` **精确**查一条本地记录，没查到就返回 ``None``。

    不能直接用 ``LocalImages.find()``：它带「同仓库只有一条记录就按仓库认领」的回退，
    那是为「容器在跑哪个镜像」准备的；而这里要回答的是「**这条 tag** 的记录在不在」，
    一旦回退就会把别的 tag 的记录认成目标 —— 真机上足以导致误判、甚至删错记录。
    """
    for img in get_local_images(ik):
        if img.get("name") == repo and tag_matches(img.get("tag"), tag):
            return img
    return None


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


def _as_list(registries):
    if isinstance(registries, str):
        return [registries]
    return list(registries or [])


def _repo_path(image):
    """'metacubex/mihomo' → 'metacubex/mihomo'；'busybox' → 'library/busybox'"""
    image = image or ""
    return image if "/" in image else "library/" + image


# --------------------------------------------------------------------
# 加速源健康记忆
# --------------------------------------------------------------------
# 为什么要记：一个源坏掉（TLS 握不上手、403、连不上）时，回退链上**每个镜像**
# 都要陪它等满一次超时。实测线上：7 个镜像 × 一个 20 秒握不上手的源 = 检测
# 118 秒，其中 104 秒（88%）全是白等，而打开页面就会自动跑一次检测。
#
# 做法：失败过的源在冷却期内排到队尾，健康源先试 —— 于是坏源在第一个镜像之后
# 就再也不会被碰到。冷却结束自动复活，不写死黑名单，源修好了不用改配置。
SOURCE_COOLDOWN = 600          # 失败后多少秒内排到队尾（秒）

_source_health = {}            # key -> {"fails", "until", "reason", "ok_at"}
_source_lock = threading.Lock()


def _source_key(reg):
    return str(reg or "").strip().rstrip("/").lower()


def mark_source_ok(reg):
    """这个源刚成功过一次 —— 立刻解除冷却。"""
    key = _source_key(reg)
    if not key:
        return
    with _source_lock:
        st = _source_health.setdefault(key, {})
        st["fails"] = 0
        st["until"] = 0.0
        st["reason"] = ""
        st["ok_at"] = time.time()


def mark_source_bad(reg, reason):
    """记一次失败并进入冷却。

    ``HTTP 404`` 不算：那是「这个源没有这个镜像」，源本身是健康的，换别的镜像
    它照样能用 —— 把它冷却掉反而会让后面的镜像少一条回退路径。
    """
    text = str(reason or "")
    if "HTTP 404" in text:
        return
    key = _source_key(reg)
    if not key:
        return
    now = time.time()
    with _source_lock:
        st = _source_health.setdefault(key, {"fails": 0, "until": 0.0,
                                             "reason": "", "ok_at": 0.0})
        st["fails"] = int(st.get("fails") or 0) + 1
        st["reason"] = text[:160]
        st["until"] = now + SOURCE_COOLDOWN


def source_cooling(reg, now=None):
    """这个源此刻是否处于冷却期。"""
    key = _source_key(reg)
    now = time.time() if now is None else now
    with _source_lock:
        st = _source_health.get(key) or {}
        return float(st.get("until") or 0) > now


def order_sources(registries):
    """健康源在前、冷却中的源在后（**不删除** —— 全坏了还得靠它们兜底）。

    顺序就是回退顺序，所以把坏源排到最后，等于让它在下一次请求里就被健康源
    抢先命中，不用再等它一次超时。
    """
    regs = _as_list(registries)
    now = time.time()
    good, cooling = [], []
    for r in regs:
        (cooling if source_cooling(r, now) else good).append(r)
    return good + cooling


def source_health():
    """排查用：当前各源的冷却状态（不含任何凭据）。"""
    now = time.time()
    with _source_lock:
        return {k: {"fails": v.get("fails") or 0,
                    "cooling": float(v.get("until") or 0) > now,
                    "left": max(0, int(float(v.get("until") or 0) - now)),
                    "reason": v.get("reason") or "",
                    "ok_at": v.get("ok_at") or 0}
                for k, v in _source_health.items()}



def _registry_get(reg, repo, path, ctx=None, timeout=20, headers=None):
    """带 Bearer 认证的 registry GET，返回 (body_bytes, headers_dict)。

    401 时必须看 ``WWW-Authenticate``：真 registry 给的是
    ``Bearer realm=…``；某些「看起来像加速源」的地址只回一个 HTML 错误页、
    连这个头都没有 —— 那种源拉不动镜像，早点报错比后面卡死强。
    """
    url = "%s/v2/%s/%s" % (reg.rstrip("/"), repo, path)
    h = dict(headers or {})
    try:
        resp = _fetch(url, h, timeout)
    except urllib.error.HTTPError as e:
        if e.code not in (401, 403):
            raise IKuaiError("HTTP %s" % e.code)
        www = e.headers.get("WWW-Authenticate", "") or ""
        if not www.lower().startswith("bearer"):
            raise IKuaiError("HTTP %s（不是标准 registry，缺少 Bearer 认证头）" % e.code)
        h["Authorization"] = "Bearer " + _token(www, repo, timeout)
        try:
            resp = _fetch(url, h, timeout)
        except urllib.error.HTTPError as e2:
            raise IKuaiError("HTTP %s" % e2.code)
    with resp:
        return resp.read(), dict(resp.headers)


def remote_digest(registries, image, tag, ctx=None, timeout=20):
    """多加速源依次回退，返回 (digest, 命中的加速源)"""
    repo = _repo_path(image)
    errors = []
    for reg in order_sources(registries):
        try:
            body, hd = _registry_get(reg, repo, "manifests/" + tag, ctx, timeout,
                                     {"Accept": ACCEPT_MANIFEST})
            d = hd.get("Docker-Content-Digest") or (
                "sha256:" + hashlib.sha256(body).hexdigest())
            mark_source_ok(reg)
            if ctx:
                ctx.vlog("%s 命中 %s:%s" % (reg, repo, tag))
            return d, reg
        except Exception as e:
            mark_source_bad(reg, e)
            errors.append("%s → %s" % (reg, e))
            if ctx:
                ctx.vlog("%s 失败：%s" % (reg, e))
    raise IKuaiError("所有加速源都查不到 %s:%s（%s）"
                     % (image, tag, "；".join(errors) if errors else "无可用源"))


def _config_digest_of(reg, repo, body, ctx, timeout, arch, os_name, depth=0):
    """从 manifest 里取出「指定平台那份镜像」的 config digest。

    为什么要绕这一圈：本地镜像 ID（``docker images`` 里那串）**等于 config blob 的
    sha256**，而 manifest / index 的 digest 是另一层东西 —— 拿它们直接比，永远不
    相等。多架构镜像更麻烦：index 的 digest 一变，可能只是 arm64 那份重新构建了，
    amd64 的内容没动。所以必须下钻到 amd64 子清单里取 ``config.digest``，
    才能和本地镜像 ID 做同层比较。
    """
    if depth > 2:
        return ""
    try:
        obj = json.loads((body or b"").decode("utf-8"))
    except Exception:                                            # noqa: BLE001
        return ""
    if not isinstance(obj, dict):
        return ""
    cfg = obj.get("config")
    if isinstance(cfg, dict) and cfg.get("digest"):
        return str(cfg["digest"])
    subs = obj.get("manifests")
    if not isinstance(subs, list) or not subs:
        return ""
    picked = ""
    for m in subs:
        if not isinstance(m, dict):
            continue
        p = m.get("platform") or {}
        if str(p.get("architecture") or "") != arch or str(p.get("os") or "") != os_name:
            continue
        picked = str(m.get("digest") or "")
        if not p.get("variant"):            # 同架构优先不带 variant（arm/v7 之类才带）
            break
    if not picked:
        return ""
    try:
        sub_body, _hd = _registry_get(reg, repo, "manifests/" + picked, ctx, timeout,
                                      {"Accept": ACCEPT_MANIFEST})
    except Exception as e:                                       # noqa: BLE001
        if ctx:
            ctx.vlog("  子清单 %s 取不到：%s" % (picked[:19], e))
        return ""
    return _config_digest_of(reg, repo, sub_body, ctx, timeout, arch, os_name, depth + 1)


def remote_platform_digest(registries, image, tag, ctx=None, timeout=20,
                           arch="amd64", os_name="linux"):
    """查上游，返回 (manifest_digest, config_digest, 命中的加速源)。

    - ``manifest_digest``：tag 上那一层清单的 digest，用来判断「上游有没有动过」；
    - ``config_digest``：**本机这台机器（amd64）那份镜像**的 config digest，
      可以直接和本地镜像 ID 比对，判断「本地内容是不是最新」。

    ``config_digest`` 取不到时返回空串（某些源只给 header 不给正文），
    此时只能退回按 manifest digest 判断 —— 调用方要能区分这两种精度。
    """
    repo = _repo_path(image)
    errors = []
    for reg in order_sources(registries):
        try:
            body, hd = _registry_get(reg, repo, "manifests/" + tag, ctx, timeout,
                                     {"Accept": ACCEPT_MANIFEST})
            man = hd.get("Docker-Content-Digest") or (
                "sha256:" + hashlib.sha256(body).hexdigest())
            cfg = _config_digest_of(reg, repo, body, ctx, timeout, arch, os_name)
            mark_source_ok(reg)
            if ctx:
                ctx.vlog("%s 命中 %s:%s（manifest %s / config %s）"
                         % (reg, repo, tag, str(man)[:19], (cfg or "-")[:19]))
            return man, cfg, reg
        except Exception as e:                                   # noqa: BLE001
            mark_source_bad(reg, e)
            errors.append("%s → %s" % (reg, e))
            if ctx:
                ctx.vlog("%s 失败：%s" % (reg, e))
    raise IKuaiError("所有加速源都查不到 %s:%s（%s）"
                     % (image, tag, "；".join(errors) if errors else "无可用源"))


_VERSION_SPLIT = re.compile(r"[._-]")


def version_key(tag):
    """版本号 tag 的排序键：'v1.19.33' → (1, 19, 33)，越大越新。

    拿来做「最近的版本排前面」，不是拿来做语义判断 —— 能不能比大小仍由
    ``is_version_tag`` 决定。
    """
    s = str(tag or "").strip().lstrip("vV")
    parts = []
    for p in _VERSION_SPLIT.split(s):
        if not p.isdigit():
            break
        parts.append(int(p))
    return tuple(parts or [0])


def remote_tags(registries, image, ctx=None, timeout=20):
    """直接从加速源读 tag 列表（``/v2/<repo>/tags/list``）。

    爱快自己那个 ``docker_image.show {TYPE:"tag"}`` 有两个毛病（实测）：
    对某些仓库**恒返回空列表**（metacubex/mihomo 就是），而且会落后一版。
    所以「指定版本」下拉框不能只依赖它。
    """
    repo = _repo_path(image)
    errors = []
    for reg in order_sources(registries):
        try:
            body, _hd = _registry_get(reg, repo, "tags/list?n=200", ctx, timeout)
            tags = json.loads(body.decode("utf-8")).get("tags") or []
            tags = [str(t) for t in tags if str(t).strip()]
            if tags:
                mark_source_ok(reg)
                return _sort_tags(tags), reg
            errors.append("%s → 返回空列表" % reg)
        except Exception as e:                                   # noqa: BLE001
            mark_source_bad(reg, e)
            errors.append("%s → %s" % (reg, e))
    raise IKuaiError("所有加速源都读不到 %s 的 tag 列表（%s）"
                     % (image, "；".join(errors) if errors else "无可用源"))


def _sort_tags(tags):
    """版本号倒序在前，其余（latest/ssh/slim 之类）按原顺序跟在后面。"""
    seen, vers, others = set(), [], []
    for t in tags or []:
        t = str(t or "").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        (vers if is_version_tag(t) else others).append(t)
    vers.sort(key=version_key, reverse=True)
    return vers + others


def list_tags(ik, cfg, image, log=None, timeout=20):
    """「指定版本」下拉框的数据源，返回 ``(tags, source)``。

    ``source``：``ikuai`` = 爱快自己的列表；``registry`` = 加速源直读；
    ``none`` = 都没拿到。空列表就退回加速源，是因为爱快那个接口对部分仓库
    恒返回空 —— 界面上表现为「没查到版本列表」，用户只能干瞪眼。
    """
    def say(msg, level="info"):
        if log:
            try:
                log(msg, level=level)
            except Exception:                                    # noqa: BLE001
                pass

    try:
        tags = [str(t) for t in (get_tags(ik, image) or []) if str(t).strip()]
    except Exception as e:                                       # noqa: BLE001
        tags = []
        say("读取爱快的版本列表失败（%s）" % e, "warn")
    if tags:
        return _sort_tags(tags), "ikuai"
    try:
        regs = resolve_registries(ik, cfg, log)
        tags, reg = remote_tags(regs, image)
        say("爱快没给出 %s 的版本列表，已从加速源 %s 直接读取 %d 个 tag"
            % (image, reg, len(tags)))
        return tags, "registry"
    except Exception as e:                                       # noqa: BLE001
        say("加速源也读不到 %s 的 tag 列表：%s" % (image, e), "warn")
        return [], "none"


def find_version_tag(registries, repo, target_cfg, ctx=None, timeout=20, limit=12,
                     skip=()):
    """在上游 tag 里找「内容 == target_cfg」的**纯版本号** tag，返回 ``(tag, reg)``。

    这是「把 latest 落后的内容搬进来」的关键一步：爱快拒绝重拉 ``latest``，
    但同一个内容通常还挂着一个版本号 tag（Docker Hub 上 latest 与最新版本号
    往往指向同一份镜像）。拉那个版本号 tag 就能把内容拿到本地。

    只看最近 ``limit`` 个版本号 tag —— 越老的越不可能等于 latest 的内容，
    每个都要打一次清单请求，全查一遍太慢。
    """
    if not norm_digest(target_cfg):
        return "", ""
    try:
        tags, reg0 = remote_tags(registries, repo, ctx, timeout)
    except Exception:                                            # noqa: BLE001
        return "", ""
    cand = [t for t in tags if is_version_tag(t) and t not in set(skip or ())]
    for tag in cand[:limit]:
        try:
            _man, cfg, reg = remote_platform_digest(registries, repo, tag, ctx, timeout)
        except Exception:                                        # noqa: BLE001
            continue
        if cfg and norm_digest(cfg) == norm_digest(target_cfg):
            return tag, reg
    return "", ""


def wait_for_image(ik, repo, tag, since, timeout, ctx):
    """轮询本地镜像列表，等待新镜像下载完成"""
    deadline = time.time() + timeout
    note = ""
    last_left = None
    while time.time() < deadline:
        for i in get_local_images(ik):
            # 爱快镜像条目的 tag 可能是逗号分隔多标签（如 "1.37.3,latest"），
            # 不能用字符串精确匹配 —— 否则「镜像早就在本地」也会被判定为
            # 「没下载完」，白等整个超时周期后失败（实测 vaultwarden 就是）。
            if i.get("name") == repo and tag_matches(i.get("tag"), tag):
                if (i.get("install") or 0) >= since:
                    return True, i
                note = ("镜像记录已存在，但拉取时间没更新 —— 爱快没有真的下载"
                        "（它对同名同 tag 一律不肯重下，且不比对内容）")
        left = int(deadline - time.time())
        if ctx and last_left is not None and last_left - left >= 15:
            ctx.logger("等待镜像下载中，剩余约 %ds" % left, level="debug")
        if last_left is None or last_left - left >= 15:
            last_left = left
        time.sleep(3)
    return False, note
