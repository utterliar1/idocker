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
# 让上游内容真的比本地新一份（本地 clash 记录是 OLD_ID，上游是 NEW_ID）。
# 不设的话 mock 会把本地镜像 ID 当上游内容，就成了「本来就没有新版」。
mock_router.FAKE.configs["dreamacro/clash:latest"] = mock_router.NEW_ID
# 上游同内容还挂着一个版本号 tag —— 爱快对同名 latest 一律不肯重下，
# 所以「本地 latest 落后」时流程会走搬运，得给它一个能拉的版本号 tag。
mock_router.FAKE.configs["dreamacro/clash:2.9.0"] = mock_router.NEW_ID
mock_router.FAKE.tags["dreamacro/clash"] = ["latest", "2.9.0"]
# 定时任务先比对上游内容，命中「内容变了」才拉取 + 重启
mock_router.FAKE.images[0]["id"] = mock_router.OLD_ID
d, _ = req("/api/schedules", {"action": "run", "id": sc_upd["id"]})
ok("返回 job_id", bool(d["data"].get("job_id")))
job = wait_job(d["data"]["job_id"])
st = step_states(job)
ok("更新任务成功", job["ok"] is True, job.get("message"))
ok("六步全部走完", all(st[k] in ("done", "skipped") for k in
                       ("login", "inspect", "pull", "download", "update", "verify")), st)
# 本地 latest 落后时走的是搬运：先把容器切到版本号 tag、再切回 latest，
# 所以 update 会是两次（而不是就地重建一次）。
ok("确实调用了 docker_container.update", len(mock_router.FAKE.update_calls) >= 1,
   mock_router.FAKE.update_calls)
_latest4 = [i for i in mock_router.FAKE.images
            if i["name"] == "dreamacro/clash" and i.get("tag") == "latest"]
ok("容器最终跟随 latest", [c for c in mock_router.FAKE.containers
                        if c["name"] == "clash"][0]["image"] == "dreamacro/clash:latest")
ok("latest 记录的内容已换成上游最新",
   len(_latest4) == 1 and _latest4[0]["id"] == mock_router.NEW_ID, _latest4)
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

print("\n=== 7. 下载卡住：跳过该容器且不影响整批 ===")
# 让所有 install 都变成「回成功但一个字节不下」（下载引擎卡死 / 加速源不通就这表现），
# 并把超时压到 3 秒（测试专用，接口下限是 60）。
# 本地 latest 落后 → 预检走搬运 → 搬运要拉的版本号 tag 同样下不来 → 如实失败。
mock_router.FAKE.images = [i for i in mock_router.FAKE.images
                           if i["name"] != "dreamacro/clash"]
mock_router.FAKE.images.append({
    "name": "dreamacro/clash", "tag": "latest", "id": mock_router.OLD_ID,
    "install": int(time.time()) - 3600, "created": int(time.time()) - 3600,
    "size": 1, "namespace": "dreamacro",
    "containers": [{"name": "clash", "state": "running"}]})
mock_router.FAKE.configs["dreamacro/clash:latest"] = mock_router.NEW_ID
mock_router.FAKE.configs["dreamacro/clash:2.9.0"] = mock_router.NEW_ID
mock_router.FAKE.tags["dreamacro/clash"] = ["latest", "2.9.0"]
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

print("\n=== 29. 敏感容器参数不进入历史日志 ===")
_safe_job = _jobs_mod.Job("update", ["demo"])
_safe_job.log("容器参数摘要：镜像=repo:1，环境变量=已保留")
_safe_logs = _jobs_mod.JobManager._extract_logs(_safe_job)
ok("历史日志不含环境变量密钥内容", all("API_TOKEN" not in x["message"] and "secret-value" not in x["message"] for x in _safe_logs))

print("\n=== 30. 复用检测任务会回写所有定时计划 ===")
_fake_check = _jobs_mod.Job("check", [], schedule_id="schedule-a")
_check_mgr = server.MANAGER
_check_mgr._check_busy = _fake_check.id
_check_mgr.jobs[_fake_check.id] = _fake_check
try:
    _reused2 = _check_mgr.create("check", [], schedule_id="schedule-b")
finally:
    _check_mgr.jobs.pop(_fake_check.id, None)
    _check_mgr._check_busy = None
ok("复用检测任务登记第二个计划", _reused2 is _fake_check and "schedule-b" in _fake_check.schedule_ids)
class _CaptureStore(object):
    def __init__(self): self.results = []
    def mark_result(self, sid, status, message): self.results.append((sid, status, message))
from scheduler import Scheduler as _Scheduler
_capture = _CaptureStore()
_fake_check.ok = True
_fake_check.final_message = "检测完成"
_Scheduler(_capture, _check_mgr).on_job_finish(_fake_check)
ok("任务完成时两个计划都收到结果", [x[0] for x in _capture.results] == ["schedule-a", "schedule-b"], _capture.results)

print("\n=== 31. Store 写盘失败会向调用方报告 ===")
from store import Store as _Store
_bad_store = _Store(os.path.join(DATA, "store-failure", "settings.json"))
def _fail_store_save():
    raise OSError("disk full")
_bad_store._save = _fail_store_save
_raised_store = False
try:
    _bad_store.set_pull_timeout(600)
except OSError:
    _raised_store = True
ok("设置写盘失败不会伪装成成功", _raised_store)

print("\n=== 32. 锁定在固定版本 tag：必须提示「latest 已更新」===")
# 背景：容器一旦被用具体版本 tag 更新过（vaultwarden/server:1.37.3），它就锁在
# 那个 tag 上、不再跟随 latest；而版本号 tag 的上游内容是固定的 —— 于是检测
# 永远报「已是最新」，用户却眼睁睁看着 Docker Hub 上 latest 变成 1.37.4。
# 所以检测要额外看一眼该仓库的 latest，把「锁着但上游已前进」显式标出来。
ok("纯版本号 tag 判为「锁定」",
   A.is_version_tag("1.37.3") and A.is_version_tag("v2.7") and A.is_version_tag("3"))
ok("变体/语义 tag 不误判（ssh 与 latest 是并列变体，不是版本落后）",
   not A.is_version_tag("ssh") and not A.is_version_tag("latest")
   and not A.is_version_tag("1.37.3-alpine") and not A.is_version_tag("slim"))

_AHEAD = "sha256:" + "d" * 64
# 前面的测试已经把 vaultwarden 切到过别的 tag，这里显式还原成「锁定在 1.37.3」，
# 让本段自包含、不依赖前序副作用。
for _c29 in mock_router.FAKE.containers:
    if _c29["name"] == "vaultwarden":
        _c29["image"] = "vaultwarden/server:1.37.3"
# 造一个「锁在变体 tag」的容器：即便上游 latest 内容不同，也不该被标记
mock_router.FAKE.containers.append({
    "id": "eeee4444ffff", "name": "webssh", "image": "lihaixin/webssh2:ssh",
    "state": "running", "status": "Up 1 day", "interface": "doc_docker",
    "ipaddr": "192.168.3.9", "ip6addr": "", "auto_start": 1,
    "mounts": "", "ports": [], "env": "", "cmd": "", "comment": ""})
mock_router.FAKE.digests = {"vaultwarden/server:latest": _AHEAD,
                            "lihaixin/webssh2:latest": _AHEAD}
try:
    _j29 = server.MANAGER.create("check", [], None)
    wait_job(_j29.id)
    _res29 = [e for e in _j29.events if e.get("type") == "check_result"]
    _rs29 = {r["image"]: r for r in (_res29[-1]["results"] if _res29 else [])}
finally:
    mock_router.FAKE.digests = {}
    mock_router.FAKE.containers = [c for c in mock_router.FAKE.containers
                                   if c["name"] != "webssh"]

_vw29 = _rs29.get("vaultwarden/server:1.37.3") or {}
ok("锁定在版本号 tag 的容器被标为 pinned=ahead", _vw29.get("pinned") == "ahead", _vw29)
ok("它的 status 仍不是 newer（挂的 tag 内容确实没变）",
   _vw29.get("status") != "newer", _vw29)
_cl29 = _rs29.get("dreamacro/clash:latest") or {}
ok("跟随 latest 的容器不会被标 pinned", not _cl29.get("pinned"), _cl29)
_ws29 = _rs29.get("lihaixin/webssh2:ssh") or {}
ok("变体 tag（ssh）即使 latest 不同也不误报", not _ws29.get("pinned"), _ws29)

with open(os.path.join(DATA, "last_check.json"), encoding="utf-8") as _f29:
    _lc29 = json.load(_f29)
ok("pinned 已落盘（刷新页面后仍能显示）",
   ((_lc29.get("items") or {}).get("vaultwarden/server:1.37.3") or {}).get("pinned") == "ahead",
   (_lc29.get("items") or {}).get("vaultwarden/server:1.37.3"))

# 端到端：从「锁定在 1.37.3」切回跟随 latest
mock_router.FAKE.update_calls = []
_j30 = server.MANAGER.create("update", ["vaultwarden"], "latest")
wait_job(_j30.id)
_vw30 = [c for c in mock_router.FAKE.containers if c["name"] == "vaultwarden"][0]
ok("切到 latest 后容器 image 变成 repo:latest",
   _vw30["image"] == "vaultwarden/server:latest", _vw30["image"])

print("\n=== 33. 「切到 latest」：内容一样就切标签，内容落后就搬运 ===")
# 真实事故（2026-10-16 线上）：用户点「切到 latest」把 vaultwarden 切回跟随 latest，
# 结果什么都没发生、任务还只报了另一个容器的失败。
#
# 成因有两层：
#   1) 容器声明跑 vaultwarden/server:1.37.3，但它在爱快上的镜像记录是**一条**
#      tag 为 "1.37.3,latest" 的记录（两个标签、同一份内容）。旧代码拿这条记录的
#      **标签集合**去判断「容器是不是已经在用 latest」→ 得到 True → 判定为
#      「已是最新」直接跳过。可容器压根没在用 latest。
#   2) 爱快对「同名同 tag 的本地记录已存在」一律回「已存在相同内容」、拒绝重新拉取，
#      **不比对内容**，所以那个 latest 标签上的旧内容永远刷不新。
#
# 现在的行为要分两种情况（一刀切报失败、一刀切报成功，都是假话）：
#   · 记录里的内容**已经等于上游** → 本来就是换个标签，重启切过去就该成功；
#   · 内容确实是旧的 → 走「搬运」（第 35 段）。
# 这里先把状态摆成「只有一条 1.37.3,latest 记录、内容已是最新」。
for _c33 in mock_router.FAKE.containers:
    if _c33["name"] == "vaultwarden":
        _c33["image"] = "vaultwarden/server:1.37.3"
mock_router.FAKE.images = [
    i for i in mock_router.FAKE.images
    if i["name"] != "vaultwarden/server" or "1.37.3" in str(i.get("tag") or "")]
for _i33 in mock_router.FAKE.images:
    if _i33["name"] == "vaultwarden/server":
        _i33["tag"] = "1.37.3,latest"
        _i33["id"] = "sha256:" + "2" * 64
mock_router.FAKE.configs.pop("vaultwarden/server:latest", None)
mock_router.FAKE.update_calls = []
mock_router.FAKE.install_existing_error = True
try:
    _j33 = server.MANAGER.create("update", ["vaultwarden"], "latest", auto=False)
    _job33 = wait_job(_j33.id)
finally:
    mock_router.FAKE.install_existing_error = False
_st33 = step_states(_job33)
_log33 = " ".join(x.get("message") or ""
                  for x in _jobs_mod.JobManager._extract_logs(
                      server.MANAGER.jobs[_j33.id]))
_vw33 = [c for c in mock_router.FAKE.containers if c["name"] == "vaultwarden"][0]

ok("多标签记录不会被当成「容器已在使用 latest」而静默跳过", _st33["update"] != "skipped", _st33)
ok("内容已是最新时，只换标签就该真的切过去（重启一次）",
   len(mock_router.FAKE.update_calls) == 1, mock_router.FAKE.update_calls)
ok("容器 image 变成 repo:latest", _vw33["image"] == "vaultwarden/server:latest",
   _vw33["image"])
ok("任务如实判成功", _job33["ok"] is True, _job33.get("message"))
ok("日志说明了「记录本来就含 latest，只是把容器切过去」",
   "本来就含" in _log33, _log33[-260:])

print("\n=== 33b. 同一个 tag 的内容真的落后：能删就删了重下，删不掉才报失败 ===")
# 「同名同 tag 但内容不同」在真实世界少见（源覆盖了 tag），一旦发生，爱快不会重下。
# 处理要分两种，全看这条记录上还挂着容器没有：
#   · 空着 → 删掉重下，把新内容拿进来（旧代码只会报失败，白让用户手动去爱快后台删）；
#   · 被容器占用 → del 会被安全阀拦下（删记录会连带删容器，10-22 出过事故），
#     此时如实报失败并指路，绝不拿旧镜像重建假装成功。
_clash33 = [c for c in mock_router.FAKE.containers if c["name"] == "clash"][0]
_clash33["image"] = "dreamacro/clash:2.0.0"
mock_router.FAKE.images = [i for i in mock_router.FAKE.images
                           if i["name"] != "dreamacro/clash"]
mock_router.FAKE.images.append({
    "name": "dreamacro/clash", "tag": "2.0.0", "id": mock_router.OLD_ID,
    "install": int(time.time()) - 3600, "created": int(time.time()) - 3600,
    "size": 1, "namespace": "dreamacro", "containers": []})
mock_router.FAKE.configs["dreamacro/clash:2.0.0"] = mock_router.MID_ID
mock_router.FAKE.update_calls = []
mock_router.FAKE.install_calls = []
mock_router.FAKE.del_calls = []
mock_router.FAKE.install_existing_error = True
try:
    _j33b = server.MANAGER.create("update", ["clash"], "2.0.0", auto=False)
    _job33b = wait_job(_j33b.id)
finally:
    mock_router.FAKE.install_existing_error = False
_st33b = step_states(_job33b)
_log33b = " ".join(x.get("message") or ""
                   for x in _jobs_mod.JobManager._extract_logs(
                       server.MANAGER.jobs[_j33b.id]))
_inst33b = list(mock_router.FAKE.install_calls)

ok("记录空着时先把旧记录删掉再重下", any(
    d.get("name") == "dreamacro/clash" and d.get("tag") == "2.0.0"
    for d in mock_router.FAKE.del_calls), mock_router.FAKE.del_calls)
ok("日志说明了这一步", "改为重新下载" in _log33b, _log33b[-300:])
ok("重下之后确实重建了容器（拿到的是新内容，不是旧的）",
   len(mock_router.FAKE.update_calls) == 1, mock_router.FAKE.update_calls)
ok("任务成功", _job33b["ok"] is True, _job33b.get("message"))

print("\n=== 33c. 同 tag 记录被容器占着：删不掉就如实报失败，绝不用旧镜像重建 ===")
mock_router.FAKE.images = [i for i in mock_router.FAKE.images
                           if i["name"] != "dreamacro/clash"]
mock_router.FAKE.images.append({
    "name": "dreamacro/clash", "tag": "2.0.0", "id": mock_router.OLD_ID,
    "install": int(time.time()) - 3600, "created": int(time.time()) - 3600,
    "size": 1, "namespace": "dreamacro",
    "containers": [{"name": "clash", "state": "running"}]})
mock_router.FAKE.configs["dreamacro/clash:2.0.0"] = mock_router.MID_ID
mock_router.FAKE.update_calls = []
mock_router.FAKE.del_calls = []
mock_router.FAKE.install_existing_error = True
try:
    _j33c = server.MANAGER.create("update", ["clash"], "2.0.0", auto=False)
    _job33c = wait_job(_j33c.id)
finally:
    mock_router.FAKE.install_existing_error = False
_st33c = step_states(_job33c)
_log33c = " ".join(x.get("message") or ""
                   for x in _jobs_mod.JobManager._extract_logs(
                       server.MANAGER.jobs[_j33c.id]))
ok("占用中的记录不会被删", any(
    i["name"] == "dreamacro/clash" for i in mock_router.FAKE.images))
ok("如实报失败（下载 failed、容器一步不动）",
   _st33c["download"] == "failed" and _job33c["ok"] is False, _st33c)
ok("不去用旧镜像重建容器", len(mock_router.FAKE.update_calls) == 0,
   mock_router.FAKE.update_calls)
ok("指路去爱快「本地镜像」手动删", "本地镜像" in _log33c, _log33c[-300:])
ok("没有把「拒绝拉取」说成「已是最新」", "已是最新" not in _log33c, _log33c[-300:])
# 还原，免得影响后面
mock_router.FAKE.configs.pop("dreamacro/clash:2.0.0", None)
mock_router.FAKE.images = [i for i in mock_router.FAKE.images
                           if i["name"] != "dreamacro/clash"]
mock_router.FAKE.images.append({
    "name": "dreamacro/clash", "tag": "latest", "id": mock_router.NEW_ID,
    "install": int(time.time()), "created": int(time.time()), "size": 1,
    "namespace": "dreamacro",
    "containers": [{"name": "clash", "state": "running"}]})
_clash33["image"] = "dreamacro/clash:latest"

print("\n=== 34. 检测基准不再「自我消化」：本地内容没变，就一直报有新版本 ===")
# 线上事故的根：旧代码比的是「上游 digest vs 自己上次记的 digest」，而且每次检测
# 都无条件刷新基准 —— 上游一变，第一次报「有新版本」，第二次拿新 digest 跟自己比
# 当然相等，报告自我消化，于是永远报「已是最新」（mihomo 报过一次后永久安静）。
# 现在比的是「本地镜像 ID vs 上游 amd64 config.digest」。
_clash34 = [c for c in mock_router.FAKE.containers if c["name"] == "clash"][0]
_clash34["image"] = "dreamacro/clash:latest"
_img34 = [i for i in mock_router.FAKE.images
          if i["name"] == "dreamacro/clash" and "latest" in str(i.get("tag") or "")][0]
_img34["id"] = mock_router.NEW_ID
mock_router.FAKE.configs["dreamacro/clash:latest"] = mock_router.MID_ID


def _check_now():
    _j = server.MANAGER.create("check", [], None)
    wait_job(_j.id)
    _ev = [e for e in _j.events if e.get("type") == "check_result"]
    _rs = (_ev[-1]["results"] if _ev else [])
    return {r["image"]: r for r in _rs}


_r1 = _check_now().get("dreamacro/clash:latest") or {}
_r2 = _check_now().get("dreamacro/clash:latest") or {}
ok("第一次检测报有新版本", _r1.get("status") == "newer", _r1)
ok("第二次检测仍然报有新版本（基准不会被自己刷掉）",
   _r2.get("status") == "newer", _r2)
ok("判定依据是内容指纹（basis=sig）", _r2.get("basis") == "sig", _r2)
ok("结论文档里带了本地/上游两个内容指纹",
   _r2.get("local_id") == mock_router.NEW_ID[7:19]
   and _r2.get("upstream_id") == mock_router.MID_ID[7:19], _r2)
mock_router.FAKE.configs["dreamacro/clash:latest"] = mock_router.NEW_ID
_r3 = _check_now().get("dreamacro/clash:latest") or {}
ok("上游内容与本地一致时判为已是最新", _r3.get("status") == "latest", _r3)
_r4 = _check_now().get("dreamacro/clash:latest") or {}
ok("已是最新时也稳定（不会来回跳）", _r4.get("status") == "latest", _r4)
ok("去掉容器锁定时 pinned 不误报", not _r4.get("pinned"), _r4)

print("\n=== 35. latest 落后且爱快拒绝重拉：自动「搬运」到最新内容 ===")
# 这是 mihomo 那类容器的死结：本地 latest 是旧内容，爱快只要看到同名同 tag 的
# 记录在，就回「已存在相同内容」拒绝下载 —— 新内容永远进不来。
# 搬运路径：拉一个内容相同的版本号 tag → 容器切过去 → 删旧 latest 记录 →
#           重新拉 latest（这才会真下载）→ 容器切回 latest。
_clash35 = [c for c in mock_router.FAKE.containers if c["name"] == "clash"][0]
_clash35["image"] = "dreamacro/clash:latest"
for _i35 in mock_router.FAKE.images:
    if _i35["name"] == "dreamacro/clash" and "latest" in str(_i35.get("tag") or ""):
        _i35["tag"] = "latest"
        _i35["id"] = mock_router.OLD_ID
        _i35["containers"] = [{"name": "clash", "state": "running"}]
# 上游：latest 的内容是 NEW_ID，2.9.0 与它同一份内容，2.8.0 是旧的
mock_router.FAKE.configs["dreamacro/clash:latest"] = mock_router.NEW_ID
mock_router.FAKE.configs["dreamacro/clash:2.9.0"] = mock_router.NEW_ID
mock_router.FAKE.configs["dreamacro/clash:2.8.0"] = mock_router.OLD_ID
mock_router.FAKE.tags["dreamacro/clash"] = ["latest", "2.9.0", "2.8.0"]
mock_router.FAKE.update_calls = []
mock_router.FAKE.install_calls = []
mock_router.FAKE.del_calls = []
mock_router.FAKE.install_existing_error = True
try:
    _j35 = server.MANAGER.create("update", ["clash"], None, auto=False)
    _job35 = wait_job(_j35.id)
finally:
    mock_router.FAKE.install_existing_error = False
_st35 = step_states(_job35)
_log35 = " ".join(x.get("message") or ""
                  for x in _jobs_mod.JobManager._extract_logs(
                      server.MANAGER.jobs[_j35.id]))
_inst35 = list(mock_router.FAKE.install_calls)
_lt35 = [i for i in mock_router.FAKE.images
         if i["name"] == "dreamacro/clash" and i.get("tag") == "latest"]
_clash35b = [c for c in mock_router.FAKE.containers if c["name"] == "clash"][0]

ok("任务整体成功", _job35["ok"] is True, _job35.get("message"))
ok("确实去拉了「内容相同的版本号 tag」", ("dreamacro/clash", "2.9.0") in _inst35, _inst35)
ok("拉的是内容对得上的 2.9.0，不是旧的 2.8.0",
   ("dreamacro/clash", "2.8.0") not in _inst35, _inst35)
ok("容器先被切到版本号 tag 再切回 latest",
   [x.get("image") for x in mock_router.FAKE.update_calls] ==
   ["dreamacro/clash:2.9.0", "dreamacro/clash:latest"],
   [x.get("image") for x in mock_router.FAKE.update_calls])
ok("删除过旧的 latest 记录（并用完整 repo 名 + 真实 tag）",
   any(d.get("name") == "dreamacro/clash" and d.get("tag") == "latest"
       for d in mock_router.FAKE.del_calls), mock_router.FAKE.del_calls)
# 预检已经知道「本地 latest 落后」，所以不会再傻乎乎先 install 一次 latest 撞墙；
# 搬运里那次重新 install latest 才是唯一一次（记录刚被删，这次是真下载）。
ok("重新 install 过 latest（删掉旧记录之后）", sum(1 for x in _inst35
                                          if x == ("dreamacro/clash", "latest")) >= 1,
   _inst35)
ok("最终 latest 记录的内容是上游最新那份",
   len(_lt35) == 1 and _lt35[0]["id"] == mock_router.NEW_ID, _lt35)
ok("容器最终仍跟随 latest", _clash35b["image"] == "dreamacro/clash:latest",
   _clash35b["image"])
_ow35 = A.image_owner(_ik, "clash")
ok("占用关系落在 latest 记录上", bool(_ow35) and _ow35["tag"] == "latest", _ow35)
ok("六步都走完了（没有 skipped 的假成功）",
   all(_st35[k] == "done" for k in
       ("login", "inspect", "pull", "download", "update", "verify")), _st35)
ok("日志里说明了走的是「搬运」", "搬运" in _log35, _log35[-300:])
# 让状态回到「已是最新」，不影响后面
mock_router.FAKE.tags.pop("dreamacro/clash", None)
mock_router.FAKE.configs.pop("dreamacro/clash:2.9.0", None)
mock_router.FAKE.configs.pop("dreamacro/clash:2.8.0", None)
mock_router.FAKE.images = [i for i in mock_router.FAKE.images
                           if i["name"] != "dreamacro/clash" or i.get("tag") == "latest"]

print("\n=== 35b. 第 6 步必须用「最新的容器 id」：update 会重建容器 ===")
# 真机 10:57 那次纯假成功（用户当场指出）：日志写着「clash 现在跑 mihomo:latest」，
# 实际爱快里容器声明的还是 v1.19.33。两个错叠在一起：
#   ① 第 3 步切到版本号 tag 时爱快**重建了容器、id 变了**，第 6 步「切回 latest」
#      还拿流程开始时的旧 id 下发 → 爱快回 code:0 却什么都没做（静默空操作）；
#   ② `_wait_running` 拿**镜像记录的标签集合**当证据 —— 那条记录是
#      `tag="latest,v1.19.33"`，里面当然有 latest，于是白跑也判成功。
# 这两个开关就是为它加的：id 会变 + 旧 id 静默忽略。
mock_router.FAKE.id_changes_on_update = True
mock_router.FAKE.strict_update_id = True
mock_router.FAKE.update_ignored = []
_clash35c = [c for c in mock_router.FAKE.containers if c["name"] == "clash"][0]
_clash35c["image"] = "dreamacro/clash:latest"
mock_router.FAKE.images = [i for i in mock_router.FAKE.images
                           if i["name"] != "dreamacro/clash"]
mock_router.FAKE.images.append({
    "name": "dreamacro/clash", "tag": "latest", "id": mock_router.OLD_ID,
    "install": int(time.time()) - 86400, "created": int(time.time()) - 86400,
    "size": 1, "namespace": "dreamacro",
    "containers": [{"name": "clash", "state": "running"}]})
mock_router.FAKE.configs["dreamacro/clash:latest"] = mock_router.NEW_ID
mock_router.FAKE.configs["dreamacro/clash:2.9.0"] = mock_router.NEW_ID
mock_router.FAKE.tags["dreamacro/clash"] = ["latest", "2.9.0"]
mock_router.FAKE.update_calls = []
mock_router.FAKE.install_calls = []
mock_router.FAKE.del_calls = []
mock_router.FAKE.install_existing_error = True
try:
    _j35c = server.MANAGER.create("update", ["clash"], None, auto=False)
    _job35c = wait_job(_j35c.id)
finally:
    mock_router.FAKE.install_existing_error = False
    mock_router.FAKE.id_changes_on_update = False
    mock_router.FAKE.strict_update_id = False
_log35c = " ".join(x.get("message") or ""
                   for x in _jobs_mod.JobManager._extract_logs(
                       server.MANAGER.jobs[_j35c.id]))
_clash35d = [c for c in mock_router.FAKE.containers if c["name"] == "clash"][0]

ok("容器最终真的声明 latest（不是停在版本号 tag 上）",
   _clash35d["image"] == "dreamacro/clash:latest", _clash35d["image"])
ok("没有一次 update 是被旧 id 静默吃掉的", not mock_router.FAKE.update_ignored,
   [x.get("image") for x in mock_router.FAKE.update_ignored])
ok("任务判成功且如实反映", _job35c["ok"] is True, _job35c.get("message"))
ok("不再承诺「临时记录可当回滚点」（爱快会并成一条记录）",
   "可当回滚点" not in _log35c, _log35c[-300:])
ok("说清了爱快把同内容多标签并成一条记录", "并成" in _log35c, _log35c[-300:])
ok("给出了真正的回滚方式（把镜像改成版本号 tag）",
   "要回滚" in _log35c, _log35c[-300:])
mock_router.FAKE.tags.pop("dreamacro/clash", None)
mock_router.FAKE.configs.pop("dreamacro/clash:2.9.0", None)
mock_router.FAKE.images = [i for i in mock_router.FAKE.images
                           if i["name"] != "dreamacro/clash" or i.get("tag") == "latest"]

print("\n=== 35c. 判定「切没切过去」只能看容器自己声明的 tag ===")
# 线上出现的那条记录长这样：tag="latest,v1.19.33"（同内容多标签被并成一条），
# 而容器声明的是 v1.19.33。旧代码拿**记录标签集合**去匹配，看到里面有 latest
# 就判成功 —— 白跑一趟还报 ✅。容器用哪个 tag，只认它自己的 image。
mock_router.FAKE.images = [i for i in mock_router.FAKE.images
                           if i["name"] != "multi/app"]
# 一个容器只能挂在一条记录上：先把 clash 从别的记录上摘干净，
# 否则 image_owner 会返回列表里靠前的那条，测不出想测的东西。
for _img35e in mock_router.FAKE.images:
    _img35e["containers"] = [x for x in (_img35e.get("containers") or [])
                             if x.get("name") != "clash"]
mock_router.FAKE.images.append({
    "name": "multi/app", "tag": "latest,2.9.0", "id": mock_router.NEW_ID,
    "install": 1, "created": 1, "size": 1, "namespace": "multi",
    "containers": [{"name": "clash", "state": "running"}]})
_clash35e = [c for c in mock_router.FAKE.containers if c["name"] == "clash"][0]
_clash35e["image"] = "multi/app:2.9.0"
_okw, _why = server.MANAGER._wait_running(_ik, "clash", "latest", tries=1, gap=0)
ok("容器声明 2.9.0 时不判成功（记录里有 latest 也没用）", _okw is False, _why)
ok("说明里点出容器实际声明的 tag", "2.9.0" in _why, _why)

_clash35e["image"] = "multi/app:latest"
_okw2, _why2 = server.MANAGER._wait_running(_ik, "clash", "latest", tries=1, gap=0)
ok("容器真的声明 latest 时才判成功", _okw2 is True, _why2)

# 老数据：容器 image 完全没有 tag（早期 lucky 那种），才退回看记录 ——
# 但必须要求记录的 repo 和容器 image 的 repo 一致，不给错配留缝。
_clash35e["image"] = "multi/app"
_okw3, _why3 = server.MANAGER._wait_running(_ik, "clash", "latest", tries=1, gap=0)
ok("image 无 tag 时退回看记录（repo 一致才认）", _okw3 is True, _why3)
_clash35e["image"] = "other/app"
_okw4, _why4 = server.MANAGER._wait_running(_ik, "clash", "latest", tries=1, gap=0)
ok("repo 都对不上时绝不认", _okw4 is False, _why4)
_clash35e["image"] = "dreamacro/clash:latest"
mock_router.FAKE.images = [i for i in mock_router.FAKE.images
                           if i["name"] != "multi/app"]

print("\n=== 36. 「指定版本」：爱快没给列表时退回加速源 + 支持手填 ===")
# 实测 metacubex/mihomo 的 tag_list 恒为空 → 弹窗只有一句「没查到版本列表」，
# 用户既选不了也填不了，只能干瞪眼。
mock_router.FAKE.ikuai_tags = []
mock_router.FAKE.tags["metacubex/mihomo"] = [
    "latest", "1.19.33", "1.19.32", "v1.19.31", "alpha-git"]
d, code = req("/api/tags?image=" + urllib.parse.quote("metacubex/mihomo"))
_tags36 = d.get("data") or []
ok("GET /api/tags 正常返回", code == 200 and d.get("ok"), code)
ok("爱快给空时退回加速源读 tag 列表", d.get("source") == "registry", d.get("source"))
ok("列表按版本号倒序（新的在前）",
   _tags36[:3] == ["1.19.33", "1.19.32", "v1.19.31"], _tags36)
ok("语义 tag 仍保留在列表里", "latest" in _tags36 and "alpha-git" in _tags36, _tags36)

mock_router.FAKE.ikuai_tags = ["latest", "1.0"]
d, _ = req("/api/tags?image=" + urllib.parse.quote("metacubex/mihomo"))
ok("爱快有列表时优先用它并标明来源", d.get("source") == "ikuai", d.get("source"))
mock_router.FAKE.ikuai_tags = ["latest", "1.7.2", "1.8.0"]
mock_router.FAKE.tags.pop("metacubex/mihomo", None)

_htm = open(os.path.join(APP, "static", "index.html"), encoding="utf-8").read()
ok("弹窗里有手填 tag 的输入框", 'id="tagManual"' in _htm)
_js = open(os.path.join(APP, "static", "app.js"), encoding="utf-8").read()
ok("确认时手填优先于点选", "manual || t.picked || t.tag" in _js)
ok("非法 tag 会被拦下（防手滑打出路径/空格）",
   "[A-Za-z0-9][A-Za-z0-9._-]{0,127}" in _js)

print("\n=== 37. 删除本地镜像记录：回读确认，不信 Success ===")
# 爱快的 del 会静默成功（参数对不上也回 Success 却什么都不删），所以删完必须回读。
mock_router.FAKE.images.append({
    "name": "demo/app", "tag": "9.9.9", "id": mock_router.MID_ID,
    "install": int(time.time()), "created": int(time.time()), "size": 1,
    "namespace": "demo", "containers": []})
mock_router.FAKE.del_calls = []
_okd, _detail = A.del_local_image(_ik, "demo/app", "9.9.9")
ok("删掉后回读确实不在清单里", _okd is True and not [
    i for i in mock_router.FAKE.images if i["name"] == "demo/app"], _detail)
ok("del 用的是完整 repo 名 + 真实 tag",
   mock_router.FAKE.del_calls and mock_router.FAKE.del_calls[0].get("name") == "demo/app"
   and mock_router.FAKE.del_calls[0].get("tag") == "9.9.9", mock_router.FAKE.del_calls)
mock_router.FAKE.del_calls = []
_okd2, _detail2 = A.del_local_image(_ik, "demo/app", "9.9.9")
ok("记录本来就不存在时直接算成功", _okd2 is True and "已无" in _detail2, _detail2)
ok("不存在时不会白发删除请求", not mock_router.FAKE.del_calls, mock_router.FAKE.del_calls)
ok("内容指纹规范化（sha256: 前缀/大小写都能对上）",
   A.same_content("sha256:" + "A" * 64, "a" * 64) and not A.same_content("", "a" * 64))
ok("截断的短 ID 不算「完整指纹」（不敢下结论）", A.norm_digest("sha256:abc123") == "")

# 🔴 安全阀：被容器占用的记录绝不删
# 2026-10-22 真机事故：搬运途中并发删掉「正被切过去」的镜像记录，爱快删了容器却没
# 重建，容器直接消失（clash 就这么没的）。工具层面必须堵死这条路。
mock_router.FAKE.images.append({
    "name": "demo/busy", "tag": "latest", "id": mock_router.NEW_ID,
    "install": int(time.time()), "created": int(time.time()), "size": 1,
    "namespace": "demo", "containers": [{"name": "ghost", "state": "running"}]})
mock_router.FAKE.del_calls = []
_okb, _db = A.del_local_image(_ik, "demo/busy", "latest")
ok("记录还被容器占用时拒绝删除", _okb is False and "占用" in _db, _db)
ok("拒绝时一个删除请求都不发（连试探都不试）",
   not mock_router.FAKE.del_calls, mock_router.FAKE.del_calls)
ok("记录原样还在", any(i["name"] == "demo/busy" for i in mock_router.FAKE.images))
mock_router.FAKE.images = [i for i in mock_router.FAKE.images if i["name"] != "demo/busy"]

print("\n=== 38. 搬运重试：上一轮留下的「下载失败」残留不能把重试挡死 ===")
# 真机实测踩到：上一轮搬运被中断，`v1.19.33` 留下一条 id=0 的失败残留记录
# （task_queue 里 status=3 progress=10 长期不动）。再点更新时，爱快看到同名同 tag
# 的记录还在，照样回「已存在相同内容」—— 于是重试永远原地踏步。
# 正确做法：这条记录的内容明显对不上（id=0 / 内容不同）就先删掉再拉。
_clash38 = [c for c in mock_router.FAKE.containers if c["name"] == "clash"][0]
_clash38["image"] = "dreamacro/clash:latest"
mock_router.FAKE.images = [
    i for i in mock_router.FAKE.images if i["name"] != "dreamacro/clash"]
mock_router.FAKE.images.append({
    "name": "dreamacro/clash", "tag": "latest", "id": mock_router.OLD_ID,
    "install": int(time.time()) - 86400, "created": int(time.time()) - 86400,
    "size": 1, "namespace": "dreamacro",
    "containers": [{"name": "clash", "state": "running"}]})
# 上一轮失败留下的残留：id=0、没有容器在用
mock_router.FAKE.images.append({
    "name": "dreamacro/clash", "tag": "2.9.0", "id": "0",
    "install": 0, "created": 0, "size": 0, "namespace": "dreamacro",
    "containers": []})
mock_router.FAKE.configs["dreamacro/clash:latest"] = mock_router.NEW_ID
mock_router.FAKE.configs["dreamacro/clash:2.9.0"] = mock_router.NEW_ID
mock_router.FAKE.tags["dreamacro/clash"] = ["latest", "2.9.0"]
mock_router.FAKE.update_calls = []
mock_router.FAKE.install_calls = []
mock_router.FAKE.del_calls = []
mock_router.FAKE.install_existing_error = True
try:
    _j38 = server.MANAGER.create("update", ["clash"], None, auto=False)
    _job38 = wait_job(_j38.id)
finally:
    mock_router.FAKE.install_existing_error = False
_st38 = step_states(_job38)
_log38 = " ".join(x.get("message") or ""
                  for x in _jobs_mod.JobManager._extract_logs(
                      server.MANAGER.jobs[_j38.id]))
_lt38 = [i for i in mock_router.FAKE.images
         if i["name"] == "dreamacro/clash" and i.get("tag") == "latest"]

ok("残留记录被识别出来（日志点明 id=0 / 内容对不上）",
   "先删掉这条残留" in _log38, _log38[-320:])
ok("确实先删了那条残留（不是直接 install 撞墙）",
   any(d.get("tag") == "2.9.0" for d in mock_router.FAKE.del_calls),
   mock_router.FAKE.del_calls)
ok("删完重新拉成功、搬运走完", _job38["ok"] is True, _job38.get("message"))
ok("最终 latest 记录是上游最新内容",
   len(_lt38) == 1 and _lt38[0]["id"] == mock_router.NEW_ID, _lt38)
ok("容器仍跟随 latest",
   [c for c in mock_router.FAKE.containers if c["name"] == "clash"][0]["image"]
   == "dreamacro/clash:latest")
ok("六步全 done", all(_st38[k] == "done" for k in
                      ("login", "inspect", "pull", "download", "update", "verify")), _st38)
mock_router.FAKE.tags.pop("dreamacro/clash", None)
mock_router.FAKE.configs.pop("dreamacro/clash:2.9.0", None)

print("\n=== 39. 精确查 vs 带回退的查：别把同仓库另一条记录当成目标 ===")
# `LocalImages.find()` 有「同仓库只有一条记录就按仓库认领」的回退，那是给
# 「容器在跑什么镜像」用的。判断「这条 tag 的记录在不在」必须精确查，
# 否则会把 latest 那条记录当成 v1.19.33 的内容，进而误判内容一致、甚至删错记录。
mock_router.FAKE.images = [i for i in mock_router.FAKE.images
                           if i["name"] != "onlyrepo/app"]
mock_router.FAKE.images.append({
    "name": "onlyrepo/app", "tag": "latest", "id": mock_router.NEW_ID,
    "install": int(time.time()), "created": int(time.time()), "size": 1,
    "namespace": "onlyrepo", "containers": []})
ok("同仓库唯一记录时，带回退的查会认领它",
   (A.LocalImages(mock_router.FAKE.images).find("onlyrepo/app", "9.9.9") or {}).get("id")
   == mock_router.NEW_ID)
ok("精确查不会认领（目标 tag 确实不存在）",
   A.exact_image(_ik, "onlyrepo/app", "9.9.9") is None)
ok("精确查能查到真实存在的那条",
   (A.exact_image(_ik, "onlyrepo/app", "latest") or {}).get("id") == mock_router.NEW_ID)

print("\n=== 40. 爱快「回成功但一个字节不下」：预检直接搬运，不空等下载超时 ===")
# 线上 2026-10-22 11:37 ddnsto 就是这么「失败」的：
#   install 没报「已存在相同内容」，爱快回成功（还下发了个任务），实际什么都没下。
#   于是 already=False → 流程进 wait_for_image 盲等，直到 pull_timeout 耗尽，
#   最后吐一句「镜像已存在但拉取时间未更新（可能本来就是最新）」——
#   可检测刚说过有新版本，这句纯属误导。
# 现在 install 之前先核内容：确实落后 + latest → 直接搬运，根本不给它空跑的机会。
_clash40 = [c for c in mock_router.FAKE.containers if c["name"] == "clash"][0]
_clash40["image"] = "dreamacro/clash:latest"
mock_router.FAKE.images = [i for i in mock_router.FAKE.images
                           if i["name"] != "dreamacro/clash"]
mock_router.FAKE.images.append({
    "name": "dreamacro/clash", "tag": "latest", "id": mock_router.OLD_ID,
    "install": int(time.time()) - 86400, "created": int(time.time()) - 86400,
    "size": 1, "namespace": "dreamacro",
    "containers": [{"name": "clash", "state": "running"}]})
mock_router.FAKE.configs["dreamacro/clash:latest"] = mock_router.NEW_ID
mock_router.FAKE.configs["dreamacro/clash:2.9.0"] = mock_router.NEW_ID
mock_router.FAKE.tags["dreamacro/clash"] = ["latest", "2.9.0"]
mock_router.FAKE.update_calls = []
mock_router.FAKE.install_calls = []
mock_router.FAKE.del_calls = []
mock_router.FAKE.install_existing_error = False   # 关键：这次爱快**不报错**
_old_pt40 = server.MANAGER.cfg.get("pull_timeout")
server.MANAGER.cfg["pull_timeout"] = 1
try:
    _j40 = server.MANAGER.create("update", ["clash"], None, auto=False)
    _job40 = wait_job(_j40.id)
finally:
    server.MANAGER.cfg["pull_timeout"] = _old_pt40
_st40 = step_states(_job40)
_log40 = " ".join(x.get("message") or ""
                  for x in _jobs_mod.JobManager._extract_logs(
                      server.MANAGER.jobs[_j40.id]))
_inst40 = list(mock_router.FAKE.install_calls)
_lt40 = [i for i in mock_router.FAKE.images
         if i["name"] == "dreamacro/clash" and i.get("tag") == "latest"]
_clash40b = [c for c in mock_router.FAKE.containers if c["name"] == "clash"][0]

ok("预检抢在 install 之前：第一次拉的就是版本号 tag，不是 latest",
   _inst40 and _inst40[0] == ("dreamacro/clash", "2.9.0"), _inst40)
ok("日志点明「直接走搬运，不空等下载超时」", "直接走搬运" in _log40, _log40[-320:])
ok("任务成功、latest 记录换成上游最新内容",
   _job40["ok"] is True and len(_lt40) == 1 and _lt40[0]["id"] == mock_router.NEW_ID,
   (_job40.get("message"), _lt40))
ok("容器仍跟随 latest", _clash40b["image"] == "dreamacro/clash:latest",
   _clash40b["image"])
ok("不再出现「可能本来就是最新」这种误导文案", "可能本来就是最新" not in _log40,
   _log40[-320:])
mock_router.FAKE.tags.pop("dreamacro/clash", None)
mock_router.FAKE.configs.pop("dreamacro/clash:2.9.0", None)
mock_router.FAKE.images = [i for i in mock_router.FAKE.images
                           if i["name"] != "dreamacro/clash" or i.get("tag") == "latest"]

print("\n=== 41. 等到超时但内容本来就一致：这是假失败，应当继续而不是报错 ===")
# 爱快「重新登记但不刷新 install 时间」时，wait_for_image 会超时并吐出
# 「拉取时间没更新」。可内容明明和上游一致 —— 此时报失败毫无道理，
# 正确反应是「无需下载」，接着核对容器。
_clash41 = [c for c in mock_router.FAKE.containers if c["name"] == "clash"][0]
_clash41["image"] = "dreamacro/clash:latest"
for _i41 in mock_router.FAKE.images:
    if _i41["name"] == "dreamacro/clash":
        _i41["id"] = mock_router.NEW_ID
        _i41["tag"] = "latest"
        _i41["containers"] = [{"name": "clash", "state": "running"}]
mock_router.FAKE.configs["dreamacro/clash:latest"] = mock_router.NEW_ID
mock_router.FAKE.update_calls = []
mock_router.FAKE.install_calls = []
mock_router.FAKE.silent_once = {"dreamacro/clash:latest"}
_old_pt41 = server.MANAGER.cfg.get("pull_timeout")
server.MANAGER.cfg["pull_timeout"] = 1
try:
    _j41 = server.MANAGER.create("update", ["clash"], None, auto=False)
    _job41 = wait_job(_j41.id)
finally:
    server.MANAGER.cfg["pull_timeout"] = _old_pt41
    mock_router.FAKE.silent_once = set()
_st41 = step_states(_job41)
_log41 = " ".join(x.get("message") or ""
                  for x in _jobs_mod.JobManager._extract_logs(
                      server.MANAGER.jobs[_j41.id]))

ok("任务判成功（内容一致就不该报失败）", _job41["ok"] is True, _job41.get("message"))
ok("下载步骤不是 failed", _st41["download"] != "failed", _st41)
ok("日志说明「本地内容已与上游一致，无需下载」", "无需下载" in _log41, _log41[-320:])
ok("没有重启容器（内容没变、容器本来就在用）", not mock_router.FAKE.update_calls,
   mock_router.FAKE.update_calls)

print("\n=== 42. 总览里的历史必须是摘要：有 log_count、不带日志正文 ===")
d, _code = req("/api/overview")
_ov42 = d["data"]
_hist42 = _ov42.get("history") or []
ok("总览带了历史记录", len(_hist42) > 0, len(_hist42))
ok("每条都有 log_count（前端靠它决定「日志」按钮能不能点）",
   all("log_count" in h for h in _hist42),
   [sorted(h.keys()) for h in _hist42[:2]])
ok("每条都不带日志正文",
   all("logs" not in h for h in _hist42), [sorted(h.keys()) for h in _hist42[:2]])
ok("log_count 是真数（不是恒 0）", any((h.get("log_count") or 0) > 0 for h in _hist42),
   [h.get("log_count") for h in _hist42[:5]])
# 老实现把 10 条完整日志塞进总览，实测占 75% 的体积；这里量一下别回退：
# 拿同一条记录的「摘要」和「含日志的详情」比，摘要必须明显更小。
_hist_bytes = len(json.dumps(_hist42, ensure_ascii=False).encode("utf-8"))
ok("历史摘要体积很小（10 条 < 20KB）", _hist_bytes < 20480, _hist_bytes)
_full42, _c42 = req("/api/history/" + _hist42[0]["id"])
ok("详情接口仍能拿到日志正文", _c42 == 200 and (_full42["data"].get("logs") is not None))
_brief_one = len(json.dumps(_hist42[0], ensure_ascii=False).encode("utf-8"))
_full_one = len(json.dumps(_full42["data"], ensure_ascii=False).encode("utf-8"))
ok("单条摘要远小于含日志的详情", _brief_one < _full_one, (_brief_one, _full_one))

print("\n=== 43. 加速源健康：坏源排到队尾，404 不算坏源 ===")
_A43 = server.A
_A43._source_health.clear()
_GOOD, _BAD = "https://good.example", "https://bad.example"
ok("一开始按原顺序回退", _A43.order_sources([_BAD, _GOOD]) == [_BAD, _GOOD])
_A43.mark_source_bad(_BAD, "HTTP 403（不是标准 registry，缺少 Bearer 认证头）")
ok("失败过的源排到队尾（不删除，仍可兜底）",
   _A43.order_sources([_BAD, _GOOD]) == [_GOOD, _BAD], _A43.order_sources([_BAD, _GOOD]))
ok("冷却状态可查", _A43.source_cooling(_BAD) is True)
ok("没失败过的源不受影响", _A43.source_cooling(_GOOD) is False)
_A43.mark_source_bad("https://missing.example", "HTTP 404")
ok("404 不算坏源（那是「这个源没这个镜像」）",
   _A43.source_cooling("https://missing.example") is False)
_A43.mark_source_ok(_BAD)
ok("成功一次立即解除冷却",
   _A43.source_cooling(_BAD) is False
   and _A43.order_sources([_BAD, _GOOD]) == [_BAD, _GOOD])

# 端到端：把爱快加速源配成「先一个连不上的、再真桩」，检测必须照样成功，
# 并把坏源记进冷却（下一次检测就不用再陪它等）。
_DEAD = "http://127.0.0.1:1"
mock_router.FAKE.mirrors = [_DEAD, "http://127.0.0.1:%d" % PORT]
_d43, _ = req("/api/check", {})
_job43 = wait_job(_d43["job_id"])
ok("有坏源时检测仍然成功（回退到好源）", _job43["ok"] is True, _job43.get("message"))
_ov43, _ = req("/api/overview")
_items43 = ((_ov43["data"].get("check") or {}).get("items") or {})
ok("坏源被记进冷却，下一轮直接排到队尾", _A43.source_cooling(_DEAD) is True)
ok("没有镜像被判查询失败",
   bool(_items43) and all(v.get("status") != "failed" for v in _items43.values()),
   {k: v.get("status") for k, v in _items43.items()})
mock_router.FAKE.mirrors = ["http://127.0.0.1:%d" % PORT]

print("\n=== 44. 任务结束通知：渠道、时机、不泄漏 URL ===")
_N44 = server.N


class _Recv(object):
    """最小的 webhook 接收端，记下收到的每个请求。"""
    def __init__(self):
        import http.server as _hs
        outer = self
        self.got = []

        class H(_hs.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n).decode("utf-8")
                try:
                    body = json.loads(raw or "{}")
                except ValueError:
                    body = {"_raw": raw}
                outer.got.append({"path": self.path, "body": body,
                                  "token": self.headers.get("X-Notify-Token")})
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):                       # noqa: A003
                pass

        self.httpd = _hs.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/hook?token=SECRET-XYZ" % self.httpd.server_address[1]


class _FakeJob(object):
    def __init__(self, ok=True, dry=None, auto=False, targets=None, kind="update"):
        self.id = "fake-notify"
        self.kind = kind
        self.targets = targets if targets is not None else ["demo"]
        self.ok = ok
        self.final_message = "完成 1 个容器的更新" if ok else "1 个容器未成功：demo"
        self.steps = [{"key": "pull", "name": "拉取新镜像", "state": "failed",
                       "message": "超时"}] if not ok else []
        self.auto = auto
        self.schedule_id = ""
        self.dry = dry


_recv = _Recv()
os.environ["NOTIFY_WEBHOOK_URL"] = _recv.url
os.environ["NOTIFY_WEBHOOK_TOKEN"] = "shared-token"
ok("渠道能从环境变量读出来", [c["kind"] for c in _N44.channels()] == ["webhook"],
   _N44.channels())
ok("渠道信息里只有主机名，不含路径里的密钥",
   "SECRET-XYZ" not in json.dumps(_N44.channels(), ensure_ascii=False))

server.STORE.set_notify_mode("fail")
ok("默认「仅失败」时，成功的任务不发通知",
   _N44.notify_job(_FakeJob(ok=True), server.STORE, "1.5.3", wait=True) is False
   and not _recv.got, _recv.got)
ok("失败的任务会发", _N44.notify_job(_FakeJob(ok=False), server.STORE, "1.5.3",
                                    wait=True) is True)
ok("webhook 收到了一个请求", len(_recv.got) == 1, _recv.got)
if _recv.got:
    _body44 = _recv.got[0]["body"]
    ok("请求带 X-Notify-Token 头", _recv.got[0]["token"] == "shared-token")
    ok("payload 标了失败", _body44.get("ok") is False, _body44.get("ok"))
    ok("payload 带容器与结果", _body44.get("targets") == ["demo"] and _body44.get("message"))
    ok("正文点了失败步骤", "拉取新镜像" in (_body44.get("text") or ""), _body44.get("text"))
    ok("标题带 ❌", "❌" in (_body44.get("title") or ""), _body44.get("title"))

server.STORE.set_notify_mode("always")
_recv.got.clear()
_N44.notify_job(_FakeJob(ok=True), server.STORE, "1.5.3", wait=True)
ok("「每次都发」时成功的任务也发", len(_recv.got) == 1, len(_recv.got))
ok("标题带 ✅", _recv.got and "✅" in (_recv.got[0]["body"].get("title") or ""))
_recv.got.clear()
_N44.notify_job(_FakeJob(ok=True, dry=True), server.STORE, "1.5.3", wait=True)
ok("干跑任务不发通知（没碰容器，纯噪音）", not _recv.got, _recv.got)

server.STORE.set_notify_mode("off")
_recv.got.clear()
_N44.notify_job(_FakeJob(ok=False), server.STORE, "1.5.3", wait=True)
ok("「关闭」时失败也不发", not _recv.got, _recv.got)

# 页面设置：能改、能读回、非法值报错、不回传密钥
_d44, _c44 = req("/api/settings", {"notify_mode": "always"})
ok("POST /api/settings 能改通知时机", _c44 == 200 and _d44["data"]["notify_mode"] == "always",
   _d44.get("data", {}).get("notify_mode"))
ok("设置里能看到已配置渠道",
   [c["kind"] for c in (_d44["data"].get("notify_channels") or [])] == ["webhook"])
ok("设置响应不泄漏 webhook URL / token",
   "SECRET-XYZ" not in json.dumps(_d44, ensure_ascii=False)
   and "shared-token" not in json.dumps(_d44, ensure_ascii=False))
_d44b, _c44b = req("/api/settings", {"notify_mode": "sometimes"})
ok("非法值被拒（400）", _c44b == 400, _c44b)
server.STORE.set_notify_mode("fail")
os.environ.pop("NOTIFY_WEBHOOK_URL", None)
os.environ.pop("NOTIFY_WEBHOOK_TOKEN", None)
try:
    _recv.httpd.shutdown()
    _recv.httpd.server_close()
except Exception:                                           # noqa: BLE001
    pass

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
