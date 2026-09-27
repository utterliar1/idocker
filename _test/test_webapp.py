#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Web 版回归测试。

起一个假爱快（mock_router）+ 真的 server.py，跑一遍：
  设置读写 / 定时任务增删改查 / 立即执行 / 调度器自动触发 / 下载超时跳过 / 上游未变跳过
  / 更新后启动日志核查 / 访问认证（登录页表单 + 会话 cookie）

用法：python _test/test_webapp.py
"""
import base64
import faulthandler
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# 万一解释器在退出阶段崩了，让它把栈打出来，而不是只留一个 exit code 139。
faulthandler.enable()

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "webapp", "app")
sys.path.insert(0, HERE)
sys.path.insert(0, APP)

import mock_router                                    # noqa: E402

_srv, PORT = mock_router.serve(0)
DATA = tempfile.mkdtemp(prefix="ikuai-test-")
# 爱快 Docker 服务设置里的加速源指向本测试桩 —— 用来验证
# 「上游检测走的是爱快配的源，而不是本工具另配的一套」。
mock_router.FAKE.mirrors = ["http://127.0.0.1:%d" % PORT]

os.environ.update({
    "IKUAI_URL": "http://127.0.0.1:%d" % PORT,
    "IKUAI_USER": "admin",
    "IKUAI_PASS": "test-only",
    "REGISTRIES": "http://127.0.0.1:%d" % PORT,
    "DATA_DIR": DATA,
    "WEB_PORT": "18088",
    "PULL_TIMEOUT": "60",
    "HTTP_TIMEOUT": "10",
    "DRY_RUN": "0",
})

import server                                         # noqa: E402
from http.server import ThreadingHTTPServer           # noqa: E402

HTTP = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
HTTP.daemon_threads = True
BASE = "http://127.0.0.1:%d" % HTTP.server_address[1]
threading.Thread(target=HTTP.serve_forever, daemon=True).start()

PASS, FAIL = [], []


def ok(name, cond, extra=""):
    (PASS if cond else FAIL).append(name + (("  <" + str(extra) + ">") if extra and not cond else ""))
    print(("  ✓ " if cond else "  ✗ ") + name + ("" if cond else "   " + str(extra)))


def req(path, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data,
                               method=method or ("POST" if data else "GET"))
    if data:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8")), resp.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode("utf-8")), e.code


def wait_job(jid, timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j, _ = req("/api/jobs/" + jid)
        if j.get("data", {}).get("finished"):
            return j["data"]
        time.sleep(0.3)
    raise AssertionError("任务 %s 超过 %ds 仍未结束" % (jid, timeout))


def step_states(job):
    return {s["key"]: s["state"] for s in job["steps"]}


print("\n=== 1. 总览 ===")
d, code = req("/api/overview")
ok("GET /api/overview 正常", code == 200 and d["ok"])
ov = d["data"]
ok("读到 2 个容器", len(ov["containers"]) == 2, ov["containers"])
ok("读到 2 个镜像", ov["images_count"] == 2)
ok("settings 随总览下发", "settings" in ov and ov["settings"]["pull_timeout"] == 60)
ok("容器补充了 repo/tag 字段", ov["containers"][0]["repo"] == "dreamacro/clash")
ok("总览下发爱快配置的镜像加速源", ov.get("mirrors") == mock_router.FAKE.mirrors,
   ov.get("mirrors"))

print("\n=== 2. 超时设置 ===")
d, _ = req("/api/settings", {"pull_timeout": 1800})
ok("保存 pull_timeout=1800", d["data"]["pull_timeout"] == 1800)
ok("内存配置同步生效", server.CFG["pull_timeout"] == 1800)
d, _ = req("/api/settings")
ok("重新读取仍是 1800", d["data"]["pull_timeout"] == 1800)
d, _ = req("/api/settings", {"pull_timeout": 5})
ok("低于下限被抬到 60", d["data"]["pull_timeout"] == 60, d["data"])
d, _ = req("/api/settings", {"pull_timeout": 99999})
ok("高于上限被压到 7200", d["data"]["pull_timeout"] == 7200, d["data"])
d, _ = req("/api/settings", {"pull_timeout": 1800})

print("\n=== 3. 定时任务增删改查 ===")
d, _ = req("/api/schedules", {"action": "save", "item": {
    "name": "每6小时检测全部", "mode": "check", "targets": [],
    "every": 360, "at": "03:30"}})
sc_check = d["data"]["schedules"][0]
ok("新建「仅检测」任务", len(d["data"]["schedules"]) == 1 and sc_check["mode"] == "check")
ok("频率文案 = 每 6 小时", sc_check["frequency"] == "每 6 小时", sc_check["frequency"])
ok("已排下次运行", sc_check["next_run"] > time.time())

d, _ = req("/api/schedules", {"action": "save", "item": {
    "name": "自动更新 clash", "mode": "update", "targets": ["clash"],
    "every": 0, "at": "03:30"}})
sc_upd = [x for x in d["data"]["schedules"] if x["mode"] == "update"][0]
ok("新建「自动更新」任务", sc_upd["targets"] == ["clash"])
ok("频率文案 = 每天 03:30", sc_upd["frequency"] == "每天 03:30", sc_upd["frequency"])
ok("total 2 条", len(d["data"]["schedules"]) == 2)

_, code = req("/api/schedules", {"action": "save", "item": {
    "name": "非法", "mode": "update", "targets": [], "every": 60}})
ok("自动更新不选容器被拒绝(400)", code == 400, code)

d, _ = req("/api/schedules", {"action": "toggle", "id": sc_check["id"], "enabled": False})
ok("停用后 enabled=False", d["data"]["schedules"][0]["enabled"] is False)
d, _ = req("/api/schedules", {"action": "toggle", "id": sc_check["id"], "enabled": True})
ok("启用后重新排期", d["data"]["schedules"][0]["enabled"] is True
   and d["data"]["schedules"][0]["next_run"] > time.time())

print("\n=== 4. 手动「立即执行」自动更新任务 ===")
mock_router.FAKE.update_calls = []
mock_router.FAKE.install_calls = []
d, _ = req("/api/schedules", {"action": "run", "id": sc_upd["id"]})
ok("返回 job_id", bool(d["data"].get("job_id")))
job = wait_job(d["data"]["job_id"])
st = step_states(job)
ok("更新任务成功", job["ok"] is True, job.get("message"))
ok("六步全部走完", all(st[k] in ("done", "skipped") for k in
                       ("login", "inspect", "pull", "download", "update", "verify")), st)
ok("确实调用了 docker_container.update", len(mock_router.FAKE.update_calls) == 1,
   mock_router.FAKE.update_calls)
ok("容器已切到新镜像 ID", mock_router.FAKE.images[0]["id"] == mock_router.NEW_ID)
d, _ = req("/api/schedules")
sc_upd = [x for x in d["data"]["schedules"] if x["id"] == sc_upd["id"]][0]
ok("任务结果回写为 ok", sc_upd["last_status"] == "ok", sc_upd)

print("\n=== 5. 再跑一次：上游未变化应跳过重启 ===")
mock_router.FAKE.update_calls = []
d, _ = req("/api/schedules", {"action": "run", "id": sc_upd["id"]})
job = wait_job(d["data"]["job_id"])
st = step_states(job)
ok("未调用 update", len(mock_router.FAKE.update_calls) == 0, mock_router.FAKE.update_calls)
ok("pull/download/update 均标记 skipped",
   st["pull"] == "skipped" and st["download"] == "skipped" and st["update"] == "skipped", st)
ok("任务整体仍算成功", job["ok"] is True, job.get("message"))

print("\n=== 6. 调度器自动触发 ===")
before = len(server.MANAGER.history)
server.STORE.set_next_run(sc_upd["id"], time.time() - 5)
server.SCHEDULER.tick()
time.sleep(1.5)
d, _ = req("/api/schedules")
after = len(server.MANAGER.history)
ok("调度器触发了任务", after > before, "history %d -> %d" % (before, after))
item = [x for x in d["data"]["schedules"] if x["id"] == sc_upd["id"]][0]
ok("触发后已重排下一次", item["next_run"] > time.time())

print("\n=== 7. 下载超时：跳过该容器且不影响整批 ===")
# 让 clash 的下载永远不完成，并把超时压到 3 秒（测试专用，接口下限是 60）
mock_router.FAKE.stall = {"dreamacro/clash"}
mock_router.FAKE.update_calls = []
server.CFG["pull_timeout"] = 3
j7 = server.MANAGER.create("update", ["clash"], None, auto=False)
job = wait_job(j7.id)
st = step_states(job)
ok("任务判为失败（有容器未成功）", job["ok"] is False, job.get("message"))
ok("download 步骤标记 failed", st["download"] == "failed", st)
ok("没有用旧镜像去重建容器", len(mock_router.FAKE.update_calls) == 0)
ok("日志给出调大超时的提示",
   any("下载超时调大" in (e.get("message") or "") for e in j7.events))
ok("失败容器不再中断整批（任务确实结束）", job["finished"] is True)
mock_router.FAKE.stall = set()
server.CFG["pull_timeout"] = 1800

print("\n=== 8. 干跑模式不产生写操作 ===")
mock_router.FAKE.update_calls = []
mock_router.FAKE.install_calls = []
server.CFG["dry_run"] = True
try:
    job = server.MANAGER.create("update", ["clash"], None, auto=False)
    job = wait_job(job.id)
finally:
    server.CFG["dry_run"] = False
ok("干跑未调用 update", len(mock_router.FAKE.update_calls) == 0)
ok("干跑未调用 install", len(mock_router.FAKE.install_calls) == 0)
ok("干跑任务仍判成功", job["ok"] is True, job.get("message"))

print("\n=== 9. 并发保护 ===")
mock_router.FAKE.stall = {"dreamacro/clash"}
server.CFG["pull_timeout"] = 3
j1 = server.MANAGER.create("update", ["clash"], None, auto=False)
time.sleep(0.4)
try:
    server.MANAGER.create("update", ["vaultwarden"], None, auto=False)
    busy_blocked = False
except Exception:
    busy_blocked = True
ok("第二个更新任务被拒绝", busy_blocked)
wait_job(j1.id)
mock_router.FAKE.stall = set()
server.CFG["pull_timeout"] = 1800

print("\n=== 10. 删除任务 ===")
d, _ = req("/api/schedules", {"action": "delete", "id": sc_check["id"]})
ok("删除后剩 1 条", len(d["data"]["schedules"]) == 1, d["data"]["schedules"])
_, code = req("/api/schedules", {"action": "delete", "id": "nope"})
ok("删除不存在的任务返回 404", code == 404, code)

print("\n=== 11. 任务历史（含日志明细）===")
d, _ = req("/api/history")
hist = d["data"]
ok("历史里有记录", len(hist) > 0, len(hist))
ok("历史条目带 steps", bool(hist[0].get("steps")))
ok("列表不带日志正文（避免一次回几百 KB）", "logs" not in hist[0])
ok("列表给出日志行数", (hist[0].get("log_count") or 0) > 0, hist[0].get("log_count"))
ok("每条历史都有日志行数", all((h.get("log_count") or 0) > 0 for h in hist),
   [(h.get("id"), h.get("log_count")) for h in hist])

det, code = req("/api/history/" + hist[0]["id"])
ok("GET /api/history/<id> 返回 200", code == 200 and det["ok"], code)
logs = (det.get("data") or {}).get("logs") or []
ok("详情带完整日志", len(logs) > 0, len(logs))
ok("日志行结构完整（ts/level/message）",
   all(x.get("ts") and x.get("level") and x.get("message") for x in logs), logs[:1])
ok("日志内容是可读的流程说明",
   any("═══" in (x.get("message") or "") for x in logs),
   [x.get("message") for x in logs[:3]])
_apiline = [x for x in logs if (x.get("message") or "").startswith(("POST ", "\u2190 "))]
ok("历史日志不含 API 往返原文（避免原始响应/凭据落盘）", not _apiline,
   [_apiline[0].get("message", "")[:60]] if _apiline else "")

failed = [h for h in hist if h.get("ok") is False]
ok("历史里有失败记录", bool(failed), len(hist))
if failed:
    fd, _ = req("/api/history/" + failed[0]["id"])
    flogs = (fd.get("data") or {}).get("logs") or []
    ok("失败任务同样保留日志明细", len(flogs) > 0, len(flogs))
    ok("日志里能看到失败原因", any(x.get("level") == "error" for x in flogs),
       [x.get("message") for x in flogs if x.get("level") == "error"][:1])

_, code = req("/api/history/nonexistent-id")
ok("查不存在的历史返回 404", code == 404, code)

print("\n=== 12. 设置文件持久化 ===")
with open(os.path.join(DATA, "settings.json"), encoding="utf-8") as f:
    saved = json.load(f)
ok("settings.json 落盘", saved["pull_timeout"] == 1800 and len(saved["schedules"]) == 1, saved)

print("\n=== 13. 上游检测用的是爱快配置的加速源 ===")
import ikuai_api as A                                     # noqa: E402

_ik = A.IKuai(server.CFG, A.Ctx())
_ik.login()

mock_router.FAKE.mirrors = []
ok("爱快没配加速源时回退到本工具配置",
   A.resolve_registries(_ik, server.CFG) == server.CFG["registries"],
   A.resolve_registries(_ik, server.CFG))

mock_router.FAKE.mirrors = ["https://mirror-a.test", "https://mirror-b.test/"]
ok("有配置时用爱快的源，并去掉尾部斜杠",
   A.resolve_registries(_ik, server.CFG) == ["https://mirror-a.test", "https://mirror-b.test"],
   A.resolve_registries(_ik, server.CFG))

# 把爱快的源换成一个必然连不上的地址：若检测仍走 REGISTRIES（本测试桩，可用）
# 就会全部成功 —— 那样说明代码没用爱快的源。全部失败才证明用对了。
mock_router.FAKE.mirrors = ["http://127.0.0.1:1"]
try:
    _j13 = server.MANAGER.create("check", [], None)
    _job13 = wait_job(_j13.id)
finally:
    mock_router.FAKE.mirrors = ["http://127.0.0.1:%d" % PORT]
_res = [e for e in _j13.events if e.get("type") == "check_result"]
_rs = (_res[-1]["results"] if _res else [])
ok("检测确实用了爱快那个坏源（而非本工具可用的源）",
   bool(_rs) and all(r["status"] == "failed" for r in _rs), _rs[:1])

print("\n=== 14. 指定 tag 更新：容器必须真的切到新镜像 ===")
# 背景：爱快 `docker_container.update` 只认 `image` 字段，而且**必须带 tag**
# ——爱快前端提交的就是下拉里选中的 `repo:tag` 全名，payload 里压根没有
# 单独的 tag 字段。早期这里只传仓库名，于是「镜像下好了、容器也重建了，
# 但仍在跑旧镜像」（实测踩过 lucky → 2.27.2）。下面把它钉死。
_hist = {"name": "demo", "image": "gdy666/lucky", "id": "x", "state": "running"}
_p, _ = A.container_payload(_hist, {}, tag="2.27.2")
ok("payload.image 是 repo:tag 全名", _p["image"] == "gdy666/lucky:2.27.2", _p["image"])
ok("payload.tag 同步", _p["tag"] == "2.27.2", _p["tag"])
_p2, _ = A.container_payload(_hist, {}, tag=None)
ok("不给 tag 时沿用容器原 tag（省略即 latest）",
   _p2["image"] == "gdy666/lucky:latest", _p2["image"])
_p3, _ = A.container_payload({"name": "vw", "id": "y", "image": "vaultwarden/server:1.37.3"},
                             {}, tag=None)
ok("容器原本就带 tag 时保持不变",
   _p3["image"] == "vaultwarden/server:1.37.3", _p3["image"])

# 端到端：vaultwarden 从 1.37.3 切到 1.9.0（新 tag 在本地是独立镜像条目）
mock_router.FAKE.update_calls = []
mock_router.FAKE.install_calls = []
mock_router.FAKE.image_changed = True
_j14 = server.MANAGER.create("update", ["vaultwarden"], "1.9.0", auto=False)
_job14 = wait_job(_j14.id)
ok("指定 tag 的更新任务成功", _job14["ok"] is True, _job14.get("message"))
_c14 = mock_router.FAKE.update_calls[0] if mock_router.FAKE.update_calls else {}
ok("update 请求里的 image 是全名",
   _c14.get("image") == "vaultwarden/server:1.9.0", _c14.get("image"))
_vw = [c for c in mock_router.FAKE.containers if c["name"] == "vaultwarden"][0]
ok("容器实际指向新 tag", _vw["image"] == "vaultwarden/server:1.9.0", _vw["image"])
_owner = A.image_owner(_ik, "vaultwarden")
ok("镜像占用关系已转移到新条目", bool(_owner) and _owner["tag"] == "1.9.0", _owner)
ok("旧条目不再挂着该容器",
   all(not [c for c in (i.get("containers") or []) if c.get("name") == "vaultwarden"]
       for i in mock_router.FAKE.images
       if i["name"] == "vaultwarden/server" and i["tag"] != "1.9.0"))
ok("verify 步骤如实反映目标 tag",
   any("1.9.0" in (s.get("message") or "") for s in _job14["steps"] if s["key"] == "verify"),
   [s.get("message") for s in _job14["steps"] if s["key"] == "verify"])

# 反向验证：真出现「没切过去」时，核对逻辑必须判不通过。
# （1.9.0 是新 tag，旧条目是 "1.37.3,latest" —— 多标签也要能正确拆分）
ok("没切过去时核对会判不通过",
   (not A.tag_matches("1.37.3,latest", "1.9.0")) and A.tag_matches("1.37.3,latest", "latest"))

print("\n=== 15. 镜像早已在本地、但容器没切过去：必须照样重启 ===")
# 真实事故的第二层：lucky 的 2.27.2 早就下好了，容器却还挂在 2.20.2 上。
# 旧流程把「本地已有该镜像」直接当成「已是最新」跳过重启 —— 于是
# 「镜像下好了但容器没更新」。正确判据是：容器**此刻是否真的挂在这个 tag 上**。
mock_router.FAKE.images.append({
    "name": "dreamacro/clash", "tag": "2.0.0", "id": mock_router.NEW_ID,
    "install": int(time.time()) - 600, "created": int(time.time()) - 600,
    "size": 1234, "namespace": "dreamacro", "containers": [],
})
mock_router.FAKE.update_calls = []
mock_router.FAKE.install_existing_error = True     # 爱快回「已存在相同内容」
try:
    _j15 = server.MANAGER.create("update", ["clash"], "2.0.0", auto=False)
    _job15 = wait_job(_j15.id)
finally:
    mock_router.FAKE.install_existing_error = False
_st15 = step_states(_job15)
ok("爱快说「镜像已存在」时不再草率跳过", len(mock_router.FAKE.update_calls) == 1,
   _st15)
ok("download 记为「已在本地」而不是干等超时", _st15["download"] == "done", _st15)
ok("update 步骤真的执行了（不是 skipped）", _st15["update"] == "done", _st15)
ok("verify 也真的跑了", _st15["verify"] == "done", _st15)
_c15 = mock_router.FAKE.update_calls[0] if mock_router.FAKE.update_calls else {}
ok("重启时用的是新的 tag", _c15.get("image") == "dreamacro/clash:2.0.0", _c15.get("image"))
_ow15 = A.image_owner(_ik, "clash")
ok("容器真的切到了 2.0.0", bool(_ow15) and _ow15["tag"] == "2.0.0", _ow15)
ok("任务整体判成功", _job15["ok"] is True, _job15.get("message"))

# 反向对照：容器本来就在用目标 tag 时，「已存在」才该跳过重启（不白白中断服务）
mock_router.FAKE.update_calls = []
mock_router.FAKE.install_existing_error = True
try:
    _j15b = server.MANAGER.create("update", ["clash"], "2.0.0", auto=False)
    _job15b = wait_job(_j15b.id)
finally:
    mock_router.FAKE.install_existing_error = False
_st15b = step_states(_job15b)
ok("已在使用该 tag 时仍然跳过重启（保留原有防误重启能力）",
   len(mock_router.FAKE.update_calls) == 0 and _st15b["update"] == "skipped", _st15b)

print("\n=== 16. 更新后核实容器真的在干活（启动日志核查）===")
# 真实事故：lucky 换 entrypoint 后启动脚本空转，容器一直 Up、进程从未启动，
# 旧流程只看 state=running 就报了成功。现在必须把启动日志拉出来核实。
mock_router.FAKE.container_logs = ["Lucky Docker Start Script", "Listen on :16601"]
_j16 = server.MANAGER.create("update", ["clash"], "2.1.0", auto=False)
_job16 = wait_job(_j16.id)
_msg16 = [e.get("message") or "" for e in list(_j16.events) if e.get("type") == "log"]
ok("更新后会把容器启动日志写进执行日志",
   any("容器启动日志" in m for m in _msg16), _msg16[-3:])
ok("日志内容确实被带出来给人看",
   any("Listen on" in m for m in _msg16), _msg16[-4:])
ok("这一趟确实走完了 verify", step_states(_job16)["verify"] == "done", step_states(_job16))

# 边界：容器重建了、状态 running，却没有任何输出 —— 必须告警，不能再报「一切正常」
mock_router.FAKE.container_logs = []
_j16b = server.MANAGER.create("update", ["clash"], "2.2.0", auto=False)
_job16b = wait_job(_j16b.id)
_ev16b = [e for e in list(_j16b.events) if e.get("type") == "log"]
ok("容器无任何日志输出时给出明确告警",
   any("没有任何日志输出" in (e.get("message") or "") and e.get("level") == "warn"
       for e in _ev16b),
   [(e.get("level"), (e.get("message") or "")[:44]) for e in _ev16b[-3:]])
mock_router.FAKE.container_logs = ["starting service...", "listening on :8080"]

print("\n=== 17. 访问认证（登录页表单 + 会话 cookie）===")
# 真实痛点：Basic 认证弹的是**浏览器原生账号框**，密码管理器识别不了，
# 既不提示保存也没法自动填充，每次访问都得手打。
# 现在改成标准登录页 + 真实 form + 会话 cookie，同时保留 Basic 头给脚本用。


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随重定向 —— 否则看不到 302 与 Set-Cookie。"""

    def redirect_request(self, *a, **kw):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def raw_req(path, form=None, json_body=None, method=None, cookie=None, basic=None):
    """发原始请求，返回 (状态码, 响应头 dict[小写], 文本)。"""
    data, headers = None, {}
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=data,
                               method=method or ("POST" if data else "GET"),
                               headers=headers)
    if cookie:
        r.add_header("Cookie", cookie)
    if basic:
        r.add_header("Authorization", "Basic " +
                     base64.b64encode(("%s:%s" % basic).encode()).decode())
    try:
        with _OPENER.open(r, timeout=30) as resp:
            return (resp.status,
                    {k.lower(): v for k, v in resp.headers.items()},
                    resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return (e.code, {k.lower(): v for k, v in e.headers.items()},
                e.read().decode("utf-8"))


server._login_fails.clear()
os.environ["AUTH_USER"] = "admin"
os.environ["AUTH_PASS"] = "s3cret-pw"

_code, _h, _ = raw_req("/")
ok("未登录访问首页会被引导到登录页",
   _code == 302 and _h.get("location", "").startswith("/login"), (_code, _h.get("location")))

_code, _h, _b = raw_req("/api/containers")
ok("未登录调接口返回 401（前端据此跳登录页）",
   _code == 401 and json.loads(_b)["ok"] is False, _code)
ok("401 不再下发 WWW-Authenticate（那正是浏览器弹原生框的开关）",
   "www-authenticate" not in _h, list(_h))

_code, _h, _b = raw_req("/login")
ok("登录页本身可匿名打开，不会死循环", _code == 200, _code)
ok("登录页是真实 form 表单（密码管理器识别的前提）",
   '<form method="post" action="/api/login"' in _b)
ok("账号框带 name + autocomplete=\"username\"",
   'name="username"' in _b and 'autocomplete="username"' in _b)
ok("密码框带 name + type=password + autocomplete=\"current-password\"",
   'name="password"' in _b and 'type="password"' in _b
   and 'autocomplete="current-password"' in _b)
ok("登录页不引用需要认证的静态资源（否则样式加载不出来）", "/static/" not in _b)

_code, _h, _b = raw_req("/api/login", json_body={"username": "admin", "password": "bad"})
ok("JSON 客户端（curl / 脚本）拿到 401 JSON 而不是 HTML 页",
   _code == 401 and bool(json.loads(_b).get("error")), _b[:80])

_code, _h, _b = raw_req("/api/login",
                        form={"username": "admin", "password": "wrong", "next": "/"})
ok("密码错误不放行，也不下发 cookie", _code == 401 and "set-cookie" not in _h, list(_h))
ok("密码错误时页面回显提示", "不正确" in _b, _b[:80])
ok("密码错误会保留已填的账号（省得重打）", 'value="admin"' in _b)

_code, _h, _b = raw_req("/api/login",
                        form={"username": "admin", "password": "s3cret-pw", "next": "/"})
_ck = _h.get("set-cookie", "")
ok("登录成功跳回原页面", _code == 302 and _h.get("location") == "/",
   (_code, _h.get("location")))
ok("下发会话 cookie，且带 HttpOnly + SameSite=Lax",
   "idocker_session=" in _ck and "HttpOnly" in _ck and "SameSite=Lax" in _ck, _ck)
ok("cookie 是签名令牌，不含明文密码", "s3cret-pw" not in _ck, _ck)
_token = _ck.split(";")[0]

_code, _h, _b = raw_req("/api/overview", cookie=_token)
ok("带会话 cookie 就能正常用接口",
   _code == 200 and json.loads(_b)["ok"] is True, _code)

_code, _h, _b = raw_req("/login", cookie=_token)
ok("已登录再打开登录页会直接跳回首页",
   _code == 302 and _h.get("location") == "/", (_code, _h.get("location")))

_tampered = _token[:-6] + ("AAAAAA" if not _token.endswith("AAAAAA") else "BBBBBB")
ok("被改过的 cookie 不通过（签名校验生效）",
   raw_req("/api/health", cookie="idocker_session=" + _tampered)[0] == 401)
ok("自己伪造的 cookie 不通过",
   raw_req("/api/health",
           cookie="idocker_session=" + base64.urlsafe_b64encode(
               b"me|9999999999|deadbeef").decode())[0] == 401)

_code, _, _ = raw_req("/api/health", basic=("admin", "s3cret-pw"))
ok("Authorization: Basic 仍然可用（curl / 脚本 / CI 不受影响）", _code == 200, _code)
ok("Basic 密码错照样拒", raw_req("/api/health", basic=("admin", "nope"))[0] == 401)

_, _, _b = raw_req("/login?next=//evil.com")
ok("next 只接受站内路径（防开放重定向）",
   'value="//evil.com"' not in _b and 'value="/"' in _b)
_code, _h, _ = raw_req("/api/login",
                       form={"username": "admin", "password": "s3cret-pw",
                             "next": "//evil.com"})
ok("登录成功后也不会被跳到站外", _h.get("location") == "/", _h.get("location"))

_code, _h, _ = raw_req("/api/logout", json_body={}, cookie=_token)
ok("退出登录会清掉会话 cookie",
   _code == 200 and "Max-Age=0" in _h.get("set-cookie", ""), _h.get("set-cookie"))
_code, _h, _ = raw_req("/api/logout", cookie=_token)
ok("也支持直接访问 /api/logout 退出（方便放个退出链接）",
   _code == 302 and _h.get("location") == "/login", (_code, _h.get("location")))

server._login_fails.clear()
for _ in range(server.LOGIN_MAX_FAILS):
    raw_req("/api/login", form={"username": "admin", "password": "bad", "next": "/"})
_code, _, _b = raw_req("/api/login",
                       form={"username": "admin", "password": "s3cret-pw", "next": "/"})
ok("连续输错达上限后临时锁定，正确密码也先挡住",
   _code == 401 and "次数过多" in _b, (_code, _b[:60]))
server._login_fails.clear()
os.environ["AUTH_PASS"] = "s3cret-pw"
_code, _h, _ = raw_req("/api/login",
                       form={"username": "admin", "password": "s3cret-pw", "next": "/"})
ok("解除锁定后能正常登录", _code == 302 and "set-cookie" in _h, _code)

# 非 ASCII 密码：hmac.compare_digest 不接受含非 ASCII 字符的 str，会抛
# TypeError → 登录页 500、Basic 认证永久失败。修复办法是比对前统一 encode。
server._login_fails.clear()
os.environ["AUTH_PASS"] = "密码123"
_code, _h, _b = raw_req("/api/login",
                        form={"username": "admin", "password": "密码123", "next": "/"})
ok("中文密码也能登录成功（不再 500）",
   _code == 302 and "set-cookie" in _h, (_code, _b[:60]))
ok("中文密码通过 Basic 认证也能过",
   raw_req("/api/health", basic=("admin", "密码123"))[0] == 200)
ok("中文密码错误照样被拒",
   raw_req("/api/health", basic=("admin", "不是这个"))[0] == 401)
os.environ["AUTH_PASS"] = "s3cret-pw"

# 只设账号、不设密码 = 配置错误，必须 fail-closed（否则空密码即可登录）
server._login_fails.clear()
os.environ.pop("AUTH_PASS", None)
_code, _h, _b = raw_req("/api/login",
                        form={"username": "admin", "password": "", "next": "/"})
ok("只设账号不设密码时拒绝登录（不留空密码后门）", _code == 401, (_code, _b[:60]))
ok("空密码 Basic 也不放行",
   raw_req("/api/health", basic=("admin", ""))[0] == 401)
os.environ["AUTH_PASS"] = "s3cret-pw"
server._login_fails.clear()

os.environ.pop("AUTH_USER", None)
os.environ.pop("AUTH_PASS", None)

print("\n=== 18. 多标签镜像：等待下载必须能识别逗号分隔 tag ===")
# 爱快镜像条目的 tag 实测是逗号分隔多标签（如 "1.37.3,latest"）。wait_for_image
# 若用字符串精确匹配，就会把「镜像早就在本地」误判成「还没下载完」，
# 白等整个超时周期后失败 —— vaultwarden 就是被这一点卡住的。
mock_router.FAKE.images.append({
    "name": "testrepo/multi", "tag": "2.5.0,latest", "id": mock_router.NEW_ID,
    "install": int(time.time()), "created": int(time.time()),
    "size": 4096, "namespace": "testrepo", "containers": [],
})
_t18 = time.time()
_ok18, _note18 = A.wait_for_image(_ik, "testrepo/multi", "latest", since=0,
                                  timeout=6, ctx=A.Ctx())
ok("多标签条目能立刻命中，不再白等到超时",
   _ok18 is True and (time.time() - _t18) < 3,
   (_ok18, round(time.time() - _t18, 1)))

print("\n=== 19. state.json 并发写不丢记录 ===")
# 这套持久化是「读整个文件 → 改 → 整个写回」。早期无锁，30 个线程并发写实测
# 只落盘 1 条、丢了 29 条。加锁后必须一条不丢。
_STATE_KEYS = 40
_threads = [threading.Thread(target=server.MANAGER._remember_digest,
                             args=("concur:%d" % i, "digest-%d" % i, "reg"))
            for i in range(_STATE_KEYS)]
for _t in _threads:
    _t.start()
for _t in _threads:
    _t.join()
with open(os.path.join(DATA, "state.json"), encoding="utf-8") as _f:
    _st = json.load(_f)
_have = [k for k in _st if k.startswith("concur:")]
ok("并发写 %d 条一个都不丢" % _STATE_KEYS, len(_have) == _STATE_KEYS,
   "落盘 %d/%d" % (len(_have), _STATE_KEYS))

print("\n=== 20. 手动「干跑」不再污染全局（定时任务不会被悄悄变空跑）===")
server.CFG["dry_run"] = False
mock_router.FAKE.update_calls = []
d, _ = req("/api/update", {"containers": ["clash"], "dry_run": True})
_j20 = wait_job(d["job_id"])
ok("干跑任务本身没有真的改容器", len(mock_router.FAKE.update_calls) == 0)
ok("干跑后全局配置没有被写脏（仍为 False）", server.CFG["dry_run"] is False,
   server.CFG["dry_run"])
# 紧接着一次「不带 dry 字段」的更新（等同定时任务），必须真的执行
mock_router.FAKE.update_calls = []
server.CFG["pull_timeout"] = 20
_j20b = server.MANAGER.create("update", ["clash"], "7.7.7", auto=False, dry=None)
_j20b = wait_job(_j20b.id)
ok("后续不带 dry 的更新仍真实执行（未被干跑传染）",
   len(mock_router.FAKE.update_calls) == 1, _j20b.get("message"))

print("\n=== 21. 线程起不来时不能把更新功能永久卡死 ===")
import jobs as _jobs_mod                                    # noqa: E402
_real_threading = _jobs_mod.threading


class _BoomThread(object):
    def __init__(self, *a, **kw):
        pass

    def start(self):
        raise RuntimeError("can't start new thread")


class _ThreadingShim(object):
    """只把 Thread 换成会失败的桩，其余属性（Condition/Lock 等）透传真实模块。"""

    Thread = _BoomThread

    def __getattr__(self, name):
        return getattr(_real_threading, name)


_jobs_mod.threading = _ThreadingShim()
_raised = False
try:
    server.MANAGER.create("update", ["clash"], None, auto=False)
except RuntimeError:
    _raised = True
finally:
    _jobs_mod.threading = _real_threading
ok("线程启动失败会抛错", _raised)
ok("互斥位已回滚，更新功能不会永久卡死", server.MANAGER.is_busy() is False)
_j21 = server.MANAGER.create("update", ["clash"], "7.7.8", auto=False)
ok("随后仍能正常提交更新任务", bool(_j21.id) and server.MANAGER.is_busy() is True)
wait_job(_j21.id)

print("\n=== 22. 检测任务并发保护（复用而非重复打爱快）===")
import jobs as _jobs2                                       # noqa: E402
_fake_check = _jobs2.Job("check", [])
server.MANAGER._check_busy = _fake_check.id
server.MANAGER.jobs[_fake_check.id] = _fake_check
try:
    _reused = server.MANAGER.create("check", [])
finally:
    server.MANAGER.jobs.pop(_fake_check.id, None)
    server.MANAGER._check_busy = None
ok("已有检测在跑时复用同一个任务（不重复打爱快和加速源）", _reused is _fake_check)

print("\n=== 23. 登录失败记录表不会无界增长 ===")
server._login_fails.clear()
for _i in range(server.LOGIN_FAILS_MAX_KEYS + 50):
    server._login_fails["10.%d.%d.%d" % (_i // 65025, (_i // 255) % 255, _i % 255)] = \
        {"n": 0, "until": 0}
server.note_login_fail("9.9.9.9")
ok("过期记录被清理，表大小有上限",
   len(server._login_fails) <= server.LOGIN_FAILS_MAX_KEYS + 1,
   len(server._login_fails))
server._login_fails.clear()

print("\n=== 24. 写盘失败不再静默吞掉 ===")
import io as _io                                            # noqa: E402
_real_state_path = server.MANAGER._state_path
server.MANAGER._state_path = lambda: os.path.join(DATA, "no_such_dir", "state.json")
_buf = _io.StringIO()
_old_err = sys.stderr
sys.stderr = _buf
try:
    server.MANAGER._remember_digest("boom:1", "d", "r")
finally:
    sys.stderr = _old_err
    server.MANAGER._state_path = _real_state_path
ok("写盘失败会留下告警（可在 docker logs 里看到）",
   "写入失败" in _buf.getvalue(), _buf.getvalue()[:80])

print("\n=== 25. 请求体大小上限 ===")
_real_max = server.MAX_BODY_BYTES
server.MAX_BODY_BYTES = 100
try:
    _code, _, _ = raw_req("/api/update",
                          json_body={"containers": ["clash"], "pad": "x" * 500})
finally:
    server.MAX_BODY_BYTES = _real_max
ok("超大请求体被拒绝(413)", _code == 413, _code)

print("\n=== 27. 静态资源不能穿越到 static 目录之外 ===")
import http.client                                          # noqa: E402
_sib = os.path.normpath(os.path.join(server.STATIC_DIR, "..", "static-evil"))
os.makedirs(_sib, exist_ok=True)
with open(os.path.join(_sib, "probe.txt"), "w", encoding="utf-8") as _f:
    _f.write("PWNED")
try:
    _conn = http.client.HTTPConnection("127.0.0.1", HTTP.server_address[1], timeout=10)
    _conn.request("GET", "/static/../static-evil/probe.txt")
    _resp = _conn.getresponse()
    _body27 = _resp.read().decode("utf-8", "replace")
    _code27 = _resp.status
    _conn.close()
finally:
    shutil.rmtree(_sib, ignore_errors=True)
ok("前缀相同的兄弟目录也挡住（commonpath 生效）",
   _code27 == 404 and "PWNED" not in _body27, (_code27, _body27[:40]))

print("\n=== 28. 跨站写请求被拒（Origin 校验）===")
_r28 = urllib.request.Request(BASE + "/api/update",
                              data=json.dumps({"containers": ["clash"]}).encode(),
                              method="POST")
_r28.add_header("Content-Type", "application/json")
_r28.add_header("Origin", "http://evil.example.com")
try:
    with urllib.request.urlopen(_r28, timeout=15) as _resp28:
        _code28 = _resp28.status
except urllib.error.HTTPError as _e28:
    _code28 = _e28.code
ok("带站外 Origin 的写请求被拒(403)", _code28 == 403, _code28)

_r28b = urllib.request.Request(BASE + "/api/update", data=b"{}", method="POST")
_r28b.add_header("Content-Type", "application/json")
_r28b.add_header("Origin", BASE)
try:
    with urllib.request.urlopen(_r28b, timeout=15) as _resp28b:
        _code28b = _resp28b.status
except urllib.error.HTTPError as _e28b:
    _code28b = _e28b.code
ok("同源 Origin 正常放行（不是 403）", _code28b != 403, _code28b)
ok("不带 Origin 的脚本请求照旧放行（不是 403）",
   raw_req("/api/settings", json_body={})[0] != 403)

# ---- 收尾：把还在跑的任务等完、把两个 HTTP 服务关掉再退出 ----
# 否则守护线程会在解释器最终化时被硬杀，容易在退出阶段炸掉（CI 上实测 exit 139，
# 而 131 项断言其实全过了 —— 问题出在收尾，不是测试本身）。
_t_drain = time.time()
while time.time() - _t_drain < 30:
    if all(j.finished for j in list(server.MANAGER.jobs.values())):
        break
    time.sleep(0.2)
for _srv_obj in (HTTP, _srv):
    try:
        _srv_obj.shutdown()
        _srv_obj.server_close()
    except Exception:                                       # noqa: BLE001
        pass

print("\n" + "=" * 56)
print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  未通过：" + f)
print("=" * 56)

shutil.rmtree(DATA, ignore_errors=True)
sys.exit(1 if FAIL else 0)
