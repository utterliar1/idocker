#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""让飞牛上的 webapp 真实执行一次容器更新，并打印完整过程。

用法（在飞牛 webapp 目录，需先 . ./.env 拿到 AUTH_*）：
    python3 _do_update.py lucky 2.27.2
    python3 _do_update.py clash            # 不指定 tag 则沿用容器原 tag
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:3001"   # 宿主机端口（容器内是 8088，但宿主 8088 被 filebrowser 占了）
AUTH = "Basic " + base64.b64encode(
    (os.environ["AUTH_USER"] + ":" + os.environ["AUTH_PASS"]).encode()).decode()


def call(path, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method="POST" if data else "GET")
    r.add_header("Authorization", AUTH)
    if data:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode("utf-8"))


names = sys.argv[1].split(",") if len(sys.argv) > 1 else ["lucky"]
tag = sys.argv[2] if len(sys.argv) > 2 else None
print("提交更新：%s → %s" % ("、".join(names), tag or "沿用原 tag"))

res = call("/api/update", {"containers": names, "tag": tag})
if not res.get("ok"):
    print("提交失败：%s" % res.get("error"))
    sys.exit(3)

jid = res["job_id"]
print("任务 %s，等待执行…\n" % jid)
seen = set()
for _ in range(180):
    time.sleep(1.5)
    d = call("/api/jobs/" + jid).get("data") or {}
    for s in d.get("steps") or []:
        m = "%s|%s|%s" % (s["key"], s["state"], s.get("message") or "")
        if m not in seen:
            seen.add(m)
            print("  %-9s %-8s %s" % (s["key"], s["state"], s.get("message") or ""))
    if d.get("finished"):
        print("\n结果：ok=%s   %s" % (d.get("ok"), d.get("message")))
        h = call("/api/history/" + jid).get("data") or {}
        print("\n--- 执行日志（%d 行）---" % len(h.get("logs") or []))
        for l in h.get("logs") or []:
            print("  [%-5s] %s" % (l.get("level"), l.get("message")))
        sys.exit(0 if d.get("ok") else 1)
print("超时仍未结束")
sys.exit(2)
