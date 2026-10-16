#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读：爱快 Docker 存储/概览，排查「拉取卡住」是否因为磁盘空间或引擎异常。"""
import json
import os
import sys

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

cfg = A.load_config()
ctx = A.Ctx(logger=lambda m, **k: None)
ik = A.IKuai(cfg, ctx)
ik.login()

srv = A.get_server(ik)
print("=== docker_server overview ===")
print("  " + json.dumps(srv.get("overview"), ensure_ascii=False)[:800])
print()
print("=== docker_server settings（mirrors/workdisk/…）===")
st = srv.get("settings") or {}
st2 = dict(st)
print("  " + json.dumps(st2, ensure_ascii=False)[:800])
print()
print("=== docker_server show TYPE=disks ===")
try:
    res = ik.call("docker_server", "show", {"TYPE": "disks"})
    print("  " + json.dumps(res, ensure_ascii=False)[:1200])
except Exception as e:                                              # noqa: BLE001
    print("  失败：%s" % e)
print()
print("=== docker show TYPE=disks path=/ ===")
try:
    res = ik.call("docker", "show", {"TYPE": "disks", "path": "/"})
    print("  " + json.dumps(res, ensure_ascii=False)[:800])
except Exception as e:                                              # noqa: BLE001
    print("  失败：%s" % e)
