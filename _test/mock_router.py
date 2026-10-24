#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地回归测试用的「假爱快」。

同时扮演两个角色：
  1. 爱快 4.0 的 /Action/login 与 /Action/call（容器、镜像、服务设置）
  2. 一个极简 registry（/v2/<repo>/manifests/<tag>），让上游 digest 查询能走通

只用于本地测试，不参与镜像构建（.dockerignore 已排除 _test）。
"""

import hashlib
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIGEST = "sha256:" + "c" * 64
OLD_ID = "sha256:" + "0" * 64
NEW_ID = "sha256:" + "1" * 64
MID_ID = "sha256:" + "3" * 64


def split_full(image):
    """'gdy666/lucky:2.27.2' → ('gdy666/lucky', '2.27.2')"""
    image = image or ""
    tail = image.rsplit("/", 1)[-1]
    if ":" in tail:
        return image.rpartition(":")[0], image.rpartition(":")[2]
    return image, "latest"


class Fake(object):
    def __init__(self):
        self.containers = [
            {"id": "aaaa1111bbbb", "name": "clash", "image": "dreamacro/clash:latest",
             "state": "running", "status": "Up 3 days", "interface": "doc_docker",
             "ipaddr": "192.168.3.2", "ip6addr": "", "auto_start": 1,
             "mounts": "/vol1/1000/clash:/root/.config",
             "ports": [{"PublicPort": "7890", "PrivatePort": 7890, "Type": "tcp"},
                       {"PublicPort": "", "PrivatePort": 9090, "Type": "tcp"}],
             "env": "TZ=Asia%2FShanghai", "cmd": "", "comment": "", "image_logo": ""},
            {"id": "cccc2222dddd", "name": "vaultwarden",
             "image": "vaultwarden/server:1.37.3",
             "state": "running", "status": "Up 3 days (healthy)", "interface": "doc_docker",
             "ipaddr": "192.168.3.3", "ip6addr": "", "auto_start": 1,
             "mounts": "/vol1/1000/vw:/data", "ports": [], "env": "",
             "cmd": "", "comment": "", "image_logo": ""},
        ]
        self.images = [
            {"name": "dreamacro/clash", "tag": "latest", "id": OLD_ID,
             "install": int(time.time()) - 86400, "created": int(time.time()) - 864000,
             "size": 87000000, "namespace": "dreamacro",
             "containers": [{"name": "clash", "state": "running"}]},
            {"name": "vaultwarden/server", "tag": "1.37.3,latest", "id": "sha256:" + "2" * 64,
             "install": 0, "created": int(time.time()) - 8640000,
             "size": 36000000, "namespace": "vaultwarden",
             "containers": [{"name": "vaultwarden", "state": "running"}]},
        ]
        self.update_calls = []
        self.install_calls = []
        # 爱快 Docker 服务设置里的镜像加速源。默认给一个本机地址，
        # 这样 get_mirrors() 能读到；想测「读不到就回退」时置空即可。
        self.mirrors = []
        # 模拟「上游真的有新版本」：下载后会换一个新镜像 ID
        self.image_changed = True
        # 模拟「下载永远完不成」：install 不刷新时间戳
        self.stall = set()
        # 更细的「爱快回成功但什么都不下」：只静默**一次**，按 "repo:tag" 或 repo 名匹配。
        # 真实爱快对同名同 tag 的记录就是这脾气 —— 回 {"code":0} 却一个字节不下，
        # 页面只会看到「拉取时间没更新」。一次性是为了能把「先静默、后真下」的
        # 搬运流程完整跑通。
        self.silent_once = set()
        # 模拟容器日志。置空可复现「容器重建了、状态 running，却没有任何输出」
        # 的场景（实测 lucky 换 entrypoint 后启动脚本空转即如此）。
        self.container_logs = ["starting service...", "listening on :8080"]
        # 模拟真实爱快：镜像已存在时返回「已存在相同内容，请勿重复下载」错误
        # （实测爱快两种行为都有：报错、或安静地重新登记）
        self.install_existing_error = False
        # 上游 digest：默认所有 repo/tag 都是同一个值。测试可往 self.digests 里塞
        # "repo:tag" → digest 来模拟「某仓库的 latest 已经前进，但某个版本 tag 没动」
        # —— 这正是「容器锁在固定版本，检测永远说已是最新」那个场景。
        self.digests = {}
        self.digest_default = DIGEST
        # 上游「内容指纹」（amd64 子清单里的 config.digest，等于本地镜像 ID 那一层）。
        # 不显式指定时：若本地已有同名同 tag 的记录就当作「上游和它一致」——
        # 「没变化」是绝大多数情况，这样每个测试不用逐条铺数据。
        self.configs = {}
        self.config_default = NEW_ID
        # 内存里的「子清单 digest → (repo, tag)」映射，模拟多架构 index 下钻
        self.sub_map = {}
        # 各仓库的上游 tag 列表；不指定时按本地镜像记录推出来
        self.tags = {}
        # 上游**不存在**的 tag（"repo:tag"）。真 registry 回 404，而爱快对这种情况
        # 不报错，只会下发一个永不完成的下载任务 —— 正是「白等 600 秒超时」的来源。
        self.missing_tags = set()
        # 爱快自己那个「可用版本」接口返回什么（None 表示恒为空，实测 mihomo 就是）
        self.ikuai_tags = ["latest", "1.7.2", "1.8.0"]
        self.del_calls = []
        # 实测：爱快 update 会重建容器（id 变），拿旧 id 再 update 会被静默忽略
        # （回 Success 却什么都不做）。默认关，测搬运流程时打开。
        self.id_changes_on_update = False
        self.strict_update_id = False
        self.update_ignored = []
        self._nid = 0

    def digest_for(self, repo, tag):
        return self.digests.get("%s:%s" % (repo, tag), self.digest_default)

    def config_for(self, repo, tag):
        key = "%s:%s" % (repo, tag)
        if key in self.configs:
            return self.configs[key]
        for img in self.images:
            if img.get("name") != repo:
                continue
            if tag in [t.strip() for t in str(img.get("tag") or "").split(",")]:
                return img.get("id") or self.config_default
        return self.config_default

    def tags_for(self, repo):
        if repo in self.tags:
            return list(self.tags[repo])
        out = []
        for img in self.images:
            if img.get("name") != repo:
                continue
            for t in str(img.get("tag") or "").split(","):
                t = t.strip()
                if t and t not in out:
                    out.append(t)
        return out

    # -- 动作 ------------------------------------------------------
    def _attach_owner(self, container_name, full_image):
        """按镜像全名把容器挂到对应镜像条目上（模拟爱快的 containers 占用字段）。

        先把它从所有镜像上摘下来，再挂到 (repo, tag) 匹配的那条；
        匹配不到就谁也不挂 —— 与真实爱快一致（新 tag 是独立条目）。
        """
        repo, tag = split_full(full_image)
        for img in self.images:
            img["containers"] = [c for c in (img.get("containers") or [])
                                 if c.get("name") != container_name]
        for img in self.images:
            if img["name"] != repo:
                continue
            tags = [t.strip() for t in str(img.get("tag") or "").split(",")]
            if tag in tags:
                img.setdefault("containers", []).append(
                    {"name": container_name, "state": "running"})
                return img
        return None

    def call(self, func, action, param):
        if func == "docker_container":
            if action == "show":
                if param.get("TYPE") == "inspect":
                    return {"code": 0, "results": {"inspect": [{"memory": 0, "cpushares": 0}]}}
                if param.get("TYPE") == "log":
                    return {"code": 0, "results": {"log": list(self.container_logs)}}
                return {"code": 0, "results": {"data": self.containers}}
            if action == "update":
                self.update_calls.append(param)
                # 真实爱快只认 image 字段（**必须带 tag**），单独的 tag 字段会被忽略。
                # 这里如实模拟：image 不带 tag 时容器就仍挂在 latest 上 ——
                # 正是「镜像下好了却没切过去」那个 bug 的成因。
                full = param.get("image") or ""
                for c in self.containers:
                    if c["name"] != param.get("name"):
                        continue
                    # 真实爱快的两个脾气（2026-10-22 真机实测）：
                    #   ① update = 拆除重建，容器 **id 会变**；
                    #   ② 拿旧 id 下发 → 回 `code:0 Success` 却**什么都不做**。
                    # 默认关闭，免得影响既有断言；测搬运时打开。
                    if (self.strict_update_id and param.get("id")
                            and c.get("id") and param["id"] != c.get("id")):
                        self.update_ignored.append(dict(param))
                        return {"code": 0, "message": "Success"}
                    c["image"] = full
                    c["state"] = "running"
                    c["status"] = "Up 1 second"
                    if self.id_changes_on_update:
                        self._nid += 1
                        c["id"] = "sha256:" + ("%064x" % (0xA0B000 + self._nid))
                    self._attach_owner(c["name"], full)
                return {}
            if action in ("up", "down", "restart"):
                return {}
        if func == "docker_image":
            if action == "show":
                if param.get("TYPE") == "tag":
                    tags = list(self.ikuai_tags or [])
                    return {"code": 0, "results": {"tag_list": tags}}
                return {"code": 0, "results": {"data": self.images}}
            if action == "del":
                # 真实爱快只认「完整 repo 名 + 真实 tag」，对不上就静默成功但什么都不删。
                # 这里如实模拟：匹配不到时也回 Success，让上层必须回读确认。
                self.del_calls.append(dict(param))
                name, tag = param.get("name") or "", param.get("tag") or ""
                keep = []
                for img in self.images:
                    if img["name"] == name and tag in [
                            t.strip() for t in str(img.get("tag") or "").split(",")]:
                        continue
                    keep.append(img)
                self.images = keep
                return {"code": 0, "message": "Success"}
            if action == "install":
                # 爱快的 param 是 namespace + name 分开传的，这里要拼回去
                ns = param.get("namespace") or ""
                name = param.get("name") or ""
                tag = param.get("tag") or "latest"
                full = (ns + "/" + name) if ns else name
                self.install_calls.append((full, tag))
                if full in self.stall:
                    return {}                    # 只下发任务，不产生任何变化
                # 「回成功、实际不下」：命中一次就消耗掉
                for _k in ("%s:%s" % (full, tag), full):
                    if _k in self.silent_once:
                        self.silent_once.discard(_k)
                        return {}
                # 同 repo + 同 tag（tag 可能是逗号多标签）→ 视为已存在，静默刷新时间戳
                for img in self.images:
                    if img["name"] != full:
                        continue
                    tags = [t.strip() for t in str(img.get("tag") or "").split(",")]
                    if tag in tags:
                        if self.install_existing_error:
                            return {"code": 1,
                                    "message": "已存在相同内容 该镜像已存在，请勿重复下载"}
                        img["install"] = int(time.time())
                        if self.image_changed:
                            img["id"] = self.config_for(full, tag)
                        return {}
                # 新 tag → 单独一条镜像（真实爱快就是这样：
                # lucky 的 "2.27.2" 和 "2.20.2,latest" 是两条独立记录、两个不同 ID）
                self.images.append({"name": full, "tag": tag,
                                    "id": self.config_for(full, tag),
                                    "install": int(time.time()),
                                    "created": int(time.time()), "size": 1024,
                                    "namespace": ns, "containers": []})
                return {}
        if func == "docker_server":
            if param.get("TYPE") == "overview":
                return {"code": 0, "results": {"docker_version": "28.3.3", "status": 1}}
            if param.get("TYPE") == "data":
                return {"code": 0, "results": {"data": [
                    {"id": 1, "enabled": "yes", "workdisk": "docker", "autostart": 1,
                     "mirrors": ",".join(self.mirrors), "comment": ""}]}}
            return {"code": 0, "results": {"data": [{"enabled": 1, "path": "/vol1/docker"}]}}
        return {"code": 0, "results": {}}


FAKE = Fake()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _raw(self, body, headers=None):
        self.send_response(200)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if not (headers or {}).get("Content-Type"):
            self.send_header("Content-Type", "application/vnd.docker.distribution.manifest.v2+json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        # 极简 registry：/v2/<repo...>/manifests/<tag 或 digest>、/v2/<repo...>/tags/list
        m = re.match(r"^/v2/(.+)/tags/list", self.path)
        if m:
            repo = m.group(1)
            return self._json({"name": repo, "tags": FAKE.tags_for(repo)})
        m = re.match(r"^/v2/(.+)/manifests/([^/?]+)", self.path)
        if m:
            repo, ref = m.group(1), m.group(2)
            # 模拟「上游根本没有这个 tag」：真 registry 回 404，而爱快对这种情况
            # 不会报错，只会下发一个永不完成的下载任务。
            if ("%s:%s" % (repo, ref)) in FAKE.missing_tags:
                body = json.dumps({"errors": [{"code": "MANIFEST_UNKNOWN",
                                               "message": "manifest unknown"}]}).encode()
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            # 子清单（多架构里某个平台的 manifest）：正文里带本平台那份的 config.digest
            if ref in FAKE.sub_map:
                srepo, stag = FAKE.sub_map[ref]
                body = json.dumps({
                    "schemaVersion": 2,
                    "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
                    "config": {"mediaType": "application/vnd.docker.container.image.v1+json",
                               "size": 1, "digest": FAKE.config_for(srepo, stag)},
                    "layers": []}).encode()
                return self._raw(body)
            # tag → index（多架构清单），指向本测试桩里合成的 amd64 子清单
            sub = "sha256:" + hashlib.sha256(
                ("sub|%s|%s" % (repo, ref)).encode()).hexdigest()
            FAKE.sub_map[sub] = (repo, ref)
            body = json.dumps({
                "schemaVersion": 2,
                "mediaType": "application/vnd.docker.distribution.manifest.list.v2+json",
                "manifests": [{
                    "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
                    "digest": sub, "size": 1,
                    "platform": {"architecture": "amd64", "os": "linux"}}]}).encode()
            return self._raw(body, {"Docker-Content-Digest": FAKE.digest_for(repo, ref)})
        self._json({"code": 0})

    def do_POST(self):
        if self.path.endswith("/Action/login"):
            self._body()
            return self._json({"Result": 10000, "code": 0})
        if self.path.endswith("/Action/call"):
            b = self._body()
            return self._json(FAKE.call(b.get("func_name"), b.get("action"),
                                        b.get("param") or {}))
        self._json({"code": 0})


def serve(port=0):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


if __name__ == "__main__":
    srv, port = serve(18099)
    print("假爱快已启动 http://127.0.0.1:%d" % port)
    while True:
        time.sleep(3600)
