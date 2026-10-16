#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读：查爱快的 task_queue（镜像下载就在这个队列里跑），拿失败原因。"""
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
        if line and not line.startswith("#") and "=" in line:
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

shapes = [
    {"TYPE": "data"},
    {"TYPE": "total,data"},
    {"TYPE": "total,data", "page": 1, "page_size": 50},
    {},
    {"TYPE": "status"},
]
for sh in shapes:
    print("=== task_queue show %s ===" % json.dumps(sh, ensure_ascii=False))
    try:
        r = ik.call("task_queue", "show", sh)
        s = json.dumps(r, ensure_ascii=False)
        print("  %s" % s[:2000])
    except Exception as e:                                      # noqa: BLE001
        print("  ✗ %s" % str(e).splitlines()[0][:110])
    print()
