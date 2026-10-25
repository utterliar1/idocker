#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""爱快 Docker 管理操作：容器 / 镜像 / 网络 / Compose / 服务设置。

这一层只做两件事：

1. 把前端的意图翻译成爱快 API 的 ``param``；
2. 写操作之后**回读确认**——爱快的写接口是异步的，``update`` / ``add`` 甚至
   直接返回空响应体，回 Success 不等于生效。

业务判断（占用保护、危险操作预览、审计）留在调用方（server.py）。

爱快接口的三个脾气（都是实测踩出来的，改动前先读）：

* 写操作没有「重启容器」这个动作，只能 ``down`` + ``up``；
* ``docker_image del`` 只认「完整 repo 名 + 真实 tag」，对不上会静默回 Success
  却什么都不删 —— 所以删镜像必须回读；
* 容器的 ``state`` 在重建过程中会出现 ``created``，别把它当成「已启动」。
"""

import time
from concurrent.futures import ThreadPoolExecutor

import ikuai_api as A

# 写操作之后等状态稳定：容器重建通常 1~3 秒，慢的（大镜像/健康检查）十几秒
STATE_TIMEOUT = 25
STATE_TICK = 1.0

CONTAINER_ACTIONS = ("start", "stop", "restart", "delete")
COMPOSE_ACTIONS = ("up", "down", "restart", "clean")


class OpError(Exception):
    """管理操作的业务错误（参数不合法、目标不存在、爱快拒绝等）。"""


# --------------------------------------------------------------------
# 容器
# --------------------------------------------------------------------
def find_container(ik, name):
    for c in A.get_containers(ik):
        if c.get("name") == name:
            return c
    return None


def wait_state(ik, name, want, timeout=STATE_TIMEOUT):
    """等容器进入期望状态，返回 ``(ok, 实际状态)``。

    ``want``：``running`` / ``stopped``（非 running 且非 created）/ ``gone``。
    """
    deadline = time.time() + timeout
    last = "unknown"
    while time.time() < deadline:
        c = find_container(ik, name)
        last = (c or {}).get("state") or "gone"
        if want == "running" and last == "running":
            return True, last
        if want == "stopped" and last not in ("running", "created"):
            return True, last
        if want == "gone" and c is None:
            return True, last
        time.sleep(STATE_TICK)
    return False, last


def container_action(ik, name, action):
    """启停/重启/删除。返回 ``(ok, 说明)``，失败不抛异常，交给调用方展示。"""
    if action not in CONTAINER_ACTIONS:
        raise OpError("不支持的容器操作：%s" % action)
    c = find_container(ik, name)
    if not c:
        raise OpError("找不到容器 %s" % name)
    cid = c["id"]

    if action == "start":
        ik.call("docker_container", "up", {"id": cid})
        ok, st = wait_state(ik, name, "running")
        return ok, ("已启动" if ok else "已下发启动，但状态仍是 %s" % st)

    if action == "stop":
        ik.call("docker_container", "down", {"id": cid})
        ok, st = wait_state(ik, name, "stopped")
        return ok, ("已停止" if ok else "已下发停止，但状态仍是 %s" % st)

    if action == "restart":
        # 爱快没有 restart 动作：先停再起。中间留一拍，避免端点没释放干净
        ik.call("docker_container", "down", {"id": cid})
        time.sleep(1.5)
        now = find_container(ik, name) or c
        ik.call("docker_container", "up", {"id": now.get("id") or cid})
        ok, st = wait_state(ik, name, "running")
        return ok, ("已重启" if ok else "已下发重启，但状态仍是 %s" % st)

    # delete：爱快会连容器带网络端点一起删，IP 可能被短暂占用
    ik.call("docker_container", "del", {"id": cid})
    ok, st = wait_state(ik, name, "gone")
    return ok, ("已删除" if ok else "删除后容器仍在（%s）" % st)


def container_detail(ik, name):
    """容器详情：列表字段 + inspect + 实时资源，一次给全。"""
    c = find_container(ik, name)
    if not c:
        raise OpError("找不到容器 %s" % name)
    # 列表接口只给 image（"repo:tag"），前端要 repo / tag 分开用：
    # 上游状态是按 "repo:tag" 做键的，不拆开这里就会显示成「未检测」。
    c["repo"] = A.repo_of(c.get("image"))
    c["tag"] = A.split_image(c.get("image"))[2]
    insp = {}
    try:
        lst = (ik.call("docker_container", "show",
                       {"TYPE": "inspect", "id": c["id"]}).get("results") or {}).get("inspect") or []
        insp = lst[0] if lst else {}
    except Exception:                                        # noqa: BLE001
        insp = {}
    stats = {}
    try:
        stats = (ik.call("docker_container", "show",
                         {"TYPE": "source_used", "id": c["id"]}).get("results") or {}).get("source_used") or {}
    except Exception:                                        # noqa: BLE001
        stats = {}
    return {"container": c, "inspect": insp, "stats": stats,
            "image": A.image_owner(ik, name) or {}}


STAT_WORKERS = 4


def stats_all(ik, names=None):
    """批量取容器实时资源。

    爱快没有「一次给我全部容器 CPU/内存」的接口，只有按 id 查的
    ``docker_container TYPE=source_used``。7 个容器串行要 2 秒左右，
    并发 4 路就够快，也不至于把路由器打满。
    """
    items = A.get_containers(ik)
    if names:
        want = set(names)
        items = [c for c in items if c.get("name") in want]
    out = {}

    def one(c):
        try:
            r = (ik.call("docker_container", "show",
                         {"TYPE": "source_used", "id": c["id"]}).get("results") or {})
            return c.get("name"), r.get("source_used") or {}
        except Exception:                                   # noqa: BLE001
            return c.get("name"), {}

    with ThreadPoolExecutor(max_workers=STAT_WORKERS) as pool:
        for name, st in pool.map(one, items):
            if not name:
                continue
            out[name] = {
                "cpu": st.get("cpu_used") or "",
                "mem": int(st.get("memused") or 0),
                "down": int(st.get("down_speed") or 0),
                "up": int(st.get("up_speed") or 0),
            }
    return out


def container_logs(ik, name, minutes=10, limit=300):
    c = find_container(ik, name)
    if not c:
        raise OpError("找不到容器 %s" % name)
    return A.get_container_logs(ik, c["id"], limit=limit, span=int(minutes) * 60)


# ---- 新建容器 -------------------------------------------------------
# 爱快对**新填**的字符串要按它的表做一次字符替换（不是加密，见 API_REFERENCE §7）。
# 从接口读回来再回填的值已经是编码过的，**不能再编码一次**，否则 %20 变 %2520。
_ENCODE = (("%", "%25"), (" ", "%20"), ('"', "%22"), ("`", "%60"), ("$", "%24"),
           ("&", "%26"), (";", "%3B"), ("\\", "%5C"), ("|", "%7C"), ("'", "%27"))


def encode_field(value):
    out = str(value if value is not None else "")
    for raw, enc in _ENCODE:
        out = out.replace(raw, enc)
    return out


def build_create_payload(form):
    """把前端表单变成 ``docker_container add`` 的 param。

    字段与爱快前端表单一一对应；``image`` 必须是 ``repo:tag`` 全名（爱快只认这个），
    单独的 ``tag`` 字段会被忽略 —— 这是「镜像下好了却没切过去」那个 bug 的根因。
    """
    name = str(form.get("name") or "").strip()
    repo = str(form.get("image") or "").strip()
    tag = str(form.get("tag") or "latest").strip() or "latest"
    if not name:
        raise OpError("容器名不能为空")
    if not repo:
        raise OpError("镜像不能为空")
    if ":" in repo.rsplit("/", 1)[-1]:
        raise OpError("镜像请只填仓库名（例如 gdy666/lucky），版本填在 tag 里")
    if repo.startswith("sha256:"):
        raise OpError("镜像是按 digest 固定的，请改填仓库名 + tag")

    def as_int(key, default=0):
        try:
            return int(form.get(key) or default)
        except (TypeError, ValueError):
            return default

    return {
        "name": name,
        "image": "%s:%s" % (repo, tag),
        "tag": tag,
        "interface": str(form.get("interface") or "doc_docker"),
        "ipaddr": str(form.get("ipaddr") or ""),
        "ip6addr": str(form.get("ip6addr") or ""),
        "mounts": encode_field(form.get("mounts") or ""),
        "ports": encode_field(form.get("ports") or ""),
        "env": encode_field(form.get("env") or ""),
        "cmd": encode_field(form.get("cmd") or ""),
        "comment": encode_field(form.get("comment") or ""),
        "auto_start": 1 if str(form.get("auto_start", 0)) in ("1", "true", "True", "yes") else 0,
        "memory": as_int("memory"),
        "file_size": as_int("file_size", 1048576),
        "file_num": as_int("file_num", 1),
        "cpushares": as_int("cpushares"),
        "enabled": "yes" if str(form.get("enabled", "yes")) in ("1", "yes", "true", "True") else "no",
    }


def container_create(ik, form):
    payload = build_create_payload(form)
    if find_container(ik, payload["name"]):
        raise OpError("容器 %s 已经存在" % payload["name"])
    ik.call("docker_container", "add", payload)
    ok, st = wait_state(ik, payload["name"], "running", timeout=STATE_TIMEOUT)
    if not ok:
        # 建出来了但没起来，也要如实告诉调用方（可能是镜像不存在/IP 冲突）
        raise OpError("容器已创建，但没进入运行状态（当前 %s）。"
                      "常见原因：镜像还没下载完、固定 IP 被占用、端口冲突。" % st)
    return payload


# --------------------------------------------------------------------
# 镜像
# --------------------------------------------------------------------
def image_search(ik, keyword, page=1, size=24):
    """在线搜镜像（爱快自家的镜像库索引）。"""
    res = ik.call("docker_image", "show",
                  {"TYPE": "image", "page": int(page), "page_size": int(size),
                   "keyword": str(keyword or "")})
    data = (res.get("results") or {}).get("image") or {}
    return {"total": data.get("total") or 0,
            "page": data.get("page") or page,
            "page_size": data.get("page_size") or size,
            "list": data.get("list") or []}


def image_pull(ik, repo, tag="latest", wait_seconds=20):
    """下发拉取任务。``wait_seconds`` 内落地就返回 done，否则返回 queued。

    爱快的下载是后台任务，可能几分钟；这里不做长阻塞，前端刷新列表即可看到。
    """
    repo = str(repo or "").strip()
    tag = str(tag or "latest").strip() or "latest"
    if not repo:
        raise OpError("镜像不能为空")
    ns, _, name = repo.rpartition("/")
    ns = ns or "library"
    since = int(time.time())
    try:
        ik.call("docker_image", "install",
                {"name": name, "tag": tag, "namespace": ns, "image_logo": ""})
    except A.IKuaiError as e:
        if A.is_already_exists(e):
            return {"state": "exists", "message": "本地已有 %s:%s" % (repo, tag)}
        raise

    deadline = time.time() + max(0, int(wait_seconds))
    while time.time() < deadline:
        img = A.exact_image(ik, repo, tag)
        if img and (img.get("install") or 0) >= since:
            return {"state": "done",
                    "message": "已下载完成（%.1f MB）" % ((img.get("size") or 0) / 1048576.0)}
        time.sleep(2)
    return {"state": "queued", "message": "下载已提交，稍后刷新镜像列表查看进度"}


def image_delete(ik, repo, tag):
    """删镜像。已被容器占用的记录一律拒绝（A.del_local_image 里有安全阀）。"""
    return A.del_local_image(ik, repo, tag)


# --------------------------------------------------------------------
# 网络
# --------------------------------------------------------------------
def networks(ik):
    res = ik.call("docker_network", "show", {"TYPE": "data"})
    return (res.get("results") or {}).get("data") or []


def network_add(ik, form):
    name = str(form.get("name") or "").strip()
    if not name:
        raise OpError("网络名不能为空")
    subnet = str(form.get("subnet") or "").strip()
    payload = {
        "name": name,
        "subnet": subnet,
        "gateway": str(form.get("gateway") or "").strip(),
        "subnet6": str(form.get("subnet6") or "").strip(),
        "gateway6": str(form.get("gateway6") or "").strip(),
        "comment": encode_field(form.get("comment") or ""),
    }
    ik.call("docker_network", "add", payload)
    for n in networks(ik):
        if n.get("name") == name:
            return n
    raise OpError("爱快回了成功，但网络列表里没有 %s —— 请到爱快后台确认" % name)


def network_delete(ik, nid):
    before = {n.get("id") for n in networks(ik)}
    ik.call("docker_network", "del", {"id": nid})
    after = {n.get("id") for n in networks(ik)}
    return nid not in after


# --------------------------------------------------------------------
# Compose
# --------------------------------------------------------------------
def compose_list(ik):
    res = ik.call("docker_compose", "show", {"TYPE": "data"})
    return (res.get("results") or {}).get("data") or []


def compose_details(ik, cid):
    res = ik.call("docker_compose", "show", {"TYPE": "details", "id": cid})
    return res.get("results") or {}


def compose_logs(ik, cid, minutes=30):
    now = int(time.time())
    res = ik.call("docker_compose", "show",
                  {"TYPE": "log", "id": cid,
                   "starttime": now - int(minutes) * 60, "stoptime": now})
    return ((res.get("results") or {}).get("log") or [])


def compose_action(ik, cid, action):
    if action not in COMPOSE_ACTIONS:
        raise OpError("不支持的编排操作：%s" % action)
    ik.call("docker_compose", action, {"id": cid})
    return True, "已下发 %s" % action


def compose_create(ik, form):
    """新建编排项目。

    ⚠️ 爱快这个接口的完整字段没有官方文档，实测空参数时报缺 ``tagname``。
    这里按「项目名 + compose 内容」提交，失败时把爱快原话抛给用户。
    """
    name = str(form.get("tagname") or form.get("name") or "").strip()
    content = form.get("content") or ""
    if not name:
        raise OpError("项目名不能为空")
    if not str(content).strip():
        raise OpError("compose 内容不能为空")
    payload = {"tagname": name, "content": content}
    ik.call("docker_compose", "add", payload)
    for it in compose_list(ik):
        if it.get("tagname") == name or it.get("name") == name:
            return it
    raise OpError("爱快回了成功，但编排列表里没有 %s —— 该接口字段未完全公开，"
                  "请到爱快后台确认是否需要补充其它参数" % name)


# --------------------------------------------------------------------
# 服务设置
# --------------------------------------------------------------------
def server_disks(ik):
    res = ik.call("docker_server", "show", {"TYPE": "disks"})
    return (res.get("results") or {}).get("disks") or []


def save_mirrors(ik, mirrors, current=None):
    """只改镜像加速源，其余字段原样回填（爱快的 save 是全量覆盖）。"""
    cur = dict(current or {})
    if not cur:
        rows = (ik.call("docker_server", "show", {"TYPE": "data"}).get("results") or {}).get("data") or []
        cur = rows[0] if rows else {}
    payload = {
        "mirrors": ",".join([str(m).strip().rstrip("/") for m in (mirrors or []) if str(m).strip()]),
        "workdisk": cur.get("workdisk") or "docker",
        "autostart": cur.get("autostart", 1),
        "enabled": cur.get("enabled") or "yes",
        "comment": cur.get("comment") or "",
    }
    ik.call("docker_server", "save", payload)
    return A.get_mirrors(ik)


def service_action(ik, action):
    """启停爱快自身的 Docker 服务（会连带停/起所有容器，危险）。"""
    if action not in ("up", "down"):
        raise OpError("不支持的服务操作：%s" % action)
    ik.call("docker_server", action, {})
    return True, ("已下发启动" if action == "up" else "已下发停止（所有容器会一起停）")
