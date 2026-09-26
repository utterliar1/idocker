"""在飞牛上验证 Web 版 v1.2.0 的新接口。

跑之前先加载 /vol1/1000/docker/webapp/.env（提供 AUTH_USER / AUTH_PASS）。
"""
import base64
import json
import os
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:3001"
_tok = base64.b64encode(
    ("%s:%s" % (os.environ.get("AUTH_USER", ""), os.environ.get("AUTH_PASS", ""))).encode()
).decode()


def get(path):
    r = urllib.request.Request(BASE + path)
    r.add_header("Authorization", "Basic " + _tok)
    return json.loads(urllib.request.urlopen(r, timeout=30).read().decode())


ov = get("/api/overview")["data"]
print("overview.mirrors = %s" % (ov.get("mirrors"),))
print("容器数 = %d，本地镜像 = %s" % (len(ov.get("containers") or []), ov.get("images_count")))

h = get("/api/history")["data"]
print("history 条数 = %d" % len(h))
if h:
    f = h[0]
    print("首条：ok=%s  log_count=%s  列表含 logs 字段=%s"
          % (f.get("ok"), f.get("log_count"), "logs" in f))
    d = get("/api/history/" + f["id"])["data"]
    lg = d.get("logs") or []
    print("详情日志行数 = %d" % len(lg))
    for x in lg[:6]:
        print("    [%s] %s" % (x.get("level"), (x.get("message") or "")[:72]))

    bad = [x for x in h if x.get("ok") is False]
    if bad:
        bd = get("/api/history/" + bad[0]["id"])["data"]
        bl = bd.get("logs") or []
        print("失败记录日志行数 = %d" % len(bl))
        for x in bl:
            if x.get("level") == "error":
                print("    错误行：%s" % (x.get("message") or "")[:90])
                break
    else:
        print("（暂无失败记录）")

try:
    get("/api/history/nope")
    print("查不存在的历史 → 没报错（不符合预期）")
except urllib.error.HTTPError as e:
    print("查不存在的历史 → HTTP %s（预期 404）" % e.code)

# ---- 触发一次「只读版本检测」，产生一条带日志的新记录 ----
print("\n触发一次版本检测（只读，不改容器）…")
r = urllib.request.Request(BASE + "/api/check", data=b"{}", method="POST")
r.add_header("Authorization", "Basic " + _tok)
r.add_header("Content-Type", "application/json")
jid = json.loads(urllib.request.urlopen(r, timeout=30).read().decode())["job_id"]

j = {}
for _ in range(90):
    time.sleep(1)
    j = get("/api/jobs/" + jid)["data"]
    if j.get("finished"):
        break
print("检测任务结束：ok=%s  %s" % (j.get("ok"), j.get("message")))

h = get("/api/history")["data"]
f = h[0]
print("\n最新记录：id=%s  ok=%s  log_count=%s  列表含 logs 字段=%s"
      % (f["id"], f.get("ok"), f.get("log_count"), "logs" in f))
d = get("/api/history/" + f["id"])["data"]
lg = d.get("logs") or []
print("详情日志行数 = %d" % len(lg))
for x in lg[:10]:
    print("    [%s] %s" % (x.get("level"), (x.get("message") or "")[:74]))
