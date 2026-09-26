#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Web 版回归测试。

起一个假爱快（mock_router）+ 真的 server.py，跑一遍：
  设置读写 / 定时任务增删改查 / 立即执行 / 调度器自动触发 / 下载超时跳过 / 上游未变跳过

用法：python _test/test_webapp.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

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

print("\n" + "=" * 56)
print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  未通过：" + f)
print("=" * 56)

shutil.rmtree(DATA, ignore_errors=True)
sys.exit(1 if FAIL else 0)
