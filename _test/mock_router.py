#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地回归测试用的「假爱快」。

同时扮演两个角色：
  1. 爱快 4.0 的 /Action/login 与 /Action/call（容器、镜像、服务设置）
  2. 一个极简 registry（/v2/<repo>/manifests/<tag>），让上游 digest 查询能走通

只用于本地测试，不参与镜像构建（.dockerignore 已排除 _test）。
"""

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIGEST = "sha256:" + "c" * 64
OLD_ID = "sha256:" + "0" * 64
NEW_ID = "sha256:" + "1" * 64


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
        # 模拟容器日志。置空可复现「容器重建了、状态 running，却没有任何输出」
        # 的场景（实测 lucky 换 entrypoint 后启动脚本空转即如此）。
        self.container_logs = ["starting service...", "listening on :8080"]
        # 模拟真实爱快：镜像已存在时返回「已存在相同内容，请勿重复下载」错误
        # （实测爱快两种行为都有：报错、或安静地重新登记）
        self.install_existing_error = False

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
                    if c["name"] == param.get("name"):
                        c["image"] = full
                        c["state"] = "running"
                        c["status"] = "Up 1 second"
                        self._attach_owner(c["name"], full)
                return {}
            if action in ("up", "down", "restart"):
                return {}
        if func == "docker_image":
            if action == "show":
                if param.get("TYPE") == "tag":
                    return {"code": 0, "results": {"tag_list": ["latest", "1.7.2", "1.8.0"]}}
                return {"code": 0, "results": {"data": self.images}}
            if action == "install":
                # 爱快的 param 是 namespace + name 分开传的，这里要拼回去
                ns = param.get("namespace") or ""
                name = param.get("name") or ""
                tag = param.get("tag") or "latest"
                full = (ns + "/" + name) if ns else name
                self.install_calls.append((full, tag))
                if full in self.stall:
                    return {}                    # 只下发任务，不产生任何变化
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
                            img["id"] = NEW_ID
                        return {}
                # 新 tag → 单独一条镜像（真实爱快就是这样：
                # lucky 的 "2.27.2" 和 "2.20.2,latest" 是两条独立记录、两个不同 ID）
                self.images.append({"name": full, "tag": tag, "id": NEW_ID,
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

    def do_GET(self):
        # 极简 registry：/v2/<repo...>/manifests/<tag>
        if "/manifests/" in self.path:
            self.send_response(200)
            self.send_header("Docker-Content-Digest", DIGEST)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
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
