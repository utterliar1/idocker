#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重启爱快 Docker 服务（down → up），用来解冻卡死的下载引擎。

背景：爱快的镜像下载引擎会卡死。症状是**任何**新镜像 install 之后
很快 `status=-1`、`task_queue` 里 `errexit=1`、镜像记录 `size` 恒为 0，
换加速源也救不回来（2026-10-16 实测两次，换源无效、重启立修）。

用法：
    python _test/probe_docker_restart.py          # 只读：看当前状态
    python _test/probe_docker_restart.py --go     # 真的 down → up

⚠ `--go` 会重启路由器上**所有**容器（clash / lucky / vaultwarden / …），
   约 1 分钟。凭据从工作区根 .env 读，绝不回显。
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "webapp", "app"))

ENV = os.path.join(os.path.dirname(ROOT), ".env")
vals = {}
with open(ENV, "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        vals[k.strip()] = v.strip()

os.environ["IKUAI_URL"] = vals.get("ikuai_url", "")
os.environ["IKUAI_USER"] = vals.get("ikuai_id", "")
os.environ["IKUAI_PASS"] = vals.get("ikuai_pw", "")

import ikuai_api as A  # noqa: E402

DO = "--go" in sys.argv

cfg = A.load_config()
ctx = A.Ctx(logger=lambda m, **k: None)
ik = A.IKuai(cfg, ctx)
ik.login()


def docker_status():
    """1=服务在跑 0=已停。"""
    try:
        r = ik.call("docker", "show", {"TYPE": "docker_status"}).get("results") or {}
        return int(r.get("docker_status", -1))
    except Exception:                                        # noqa: BLE001
        return -1


def containers():
    try:
        return A.get_containers(ik)
    except Exception:                                        # noqa: BLE001
        return []


def snapshot():
    st = docker_status()
    cs = containers()
    up = [c["name"] for c in cs if str(c.get("state")) == "running"]
    return st, len(cs), up


def show(tag):
    st, total, up = snapshot()
    print("  [%s] docker_status=%s  容器 %d 个，运行中 %d 个%s"
          % (tag, st, total, len(up), ("：" + "、".join(up)) if up else ""))
    return st


print("=== 重启前 ===")
st0 = show("前")
if st0 < 0:
    print("  ⚠ 读不到 docker_status，先确认爱快可达 / 凭据正确。")
    sys.exit(1)

if not DO:
    print()
    print("（只读模式，什么都没做。加 --go 才会 down → up。）")
    print("  将要执行：docker_server.down  →  等到 docker_status=0")
    print("            docker_server.up    →  等到 docker_status=1 且容器起来")
    print("  ⚠ 会重启上面列出的**所有**容器，约 1 分钟。")
    sys.exit(0)

print()
print("=== 1) docker_server.down ===")
print("  返回 =", ik.call("docker_server", "down", {}))
for i in range(30):
    time.sleep(2)
    if docker_status() == 0:
        print("  服务已停（等了 %d 秒）" % ((i + 1) * 2))
        break
else:
    print("  ⚠ 30 秒内没读到 docker_status=0，仍然继续 up。")

print()
print("=== 2) docker_server.up ===")
print("  返回 =", ik.call("docker_server", "up", {}))
ok = False
for i in range(60):
    time.sleep(2)
    st, total, up = snapshot()
    if st == 1 and total and len(up) == total:
        print("  服务已起、容器全部运行（等了 %d 秒）" % ((i + 1) * 2))
        ok = True
        break

print()
print("=== 重启后 ===")
show("后")
if not ok:
    print("  ⚠ 90 秒内没等到「服务在跑且容器全起来」，请手工看一眼爱快的 Docker 页面。")
sys.exit(0 if ok else 2)
