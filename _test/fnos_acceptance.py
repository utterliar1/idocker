#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在飞牛宿主机上跑的真机验收脚本。

从 .env 读访问认证（不回显），然后通过 127.0.0.1:3001 调本工具的 API：
读取设置 → 新建定时任务 → 立即执行一次 → 打印六步/三步结果。
"""
import base64
import json
import time
import urllib.request

ENV_PATH = "/vol1/1000/docker/webapp/.env"
BASE = "http://127.0.0.1:3001"

env = {}
with open(ENV_PATH, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()

AUTH = base64.b64encode((env.get("AUTH_USER", "") + ":" + env.get("AUTH_PASS", "")).encode()).decode()


def req(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method="POST" if data else "GET")
    r.add_header("Authorization", "Basic " + AUTH)
    if data:
        r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r, timeout=300) as resp:
        return json.loads(resp.read().decode())


print("== 设置 ==")
print(json.dumps(req("/api/settings")["data"], ensure_ascii=False))

print("\n== 新建定时任务：每天 04:00 仅检测全部容器 ==")
d = req("/api/schedules", {"action": "save", "item": {
    "name": "每天检测全部容器", "mode": "check", "targets": [],
    "every": 0, "at": "04:00"}})
for s in d["data"]["schedules"]:
    print("   id=%s  %s  mode=%s  %s  下次=%s"
          % (s["id"], s["name"], s["mode"], s["frequency"],
             time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s["next_run"]))))
sid = d["data"]["schedules"][0]["id"]

print("\n== 立即执行一次 ==")
r = req("/api/schedules", {"action": "run", "id": sid})
jid = r["data"]["job_id"]
j = {}
for _ in range(300):
    j = req("/api/jobs/" + jid)["data"]
    if j["finished"]:
        break
    time.sleep(1)
print("   任务 %s  ok=%s  %s" % (jid, j["ok"], j.get("message")))
for s in j["steps"]:
    print("     %-14s %-8s %s" % (s["name"], s["state"], s["message"]))

print("\n== 任务结果回写 ==")
for s in req("/api/schedules")["data"]["schedules"]:
    print("   %s  last_status=%s  last_message=%s"
          % (s["name"], s["last_status"], s["last_message"]))

print("\n== 总览里的容器上游状态 ==")
ov = req("/api/overview")["data"]
ck = ov.get("check", {}).get("items", {})
for k, v in ck.items():
    print("   %-42s %s" % (k, v.get("status")))
print("   本地镜像 %d 个 / 容器 %d 个" % (ov["images_count"], len(ov["containers"])))
