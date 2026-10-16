#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读：问爱快 docker 状态接口，看有没有正在进行的镜像下载任务/进度。"""
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

for fn, action, param in (
    ("docker", "show", {"TYPE": "docker_status"}),
    ("docker", "show", {"TYPE": "status"}),
    ("docker_container", "show", {"TYPE": "image,network,memavailable"}),
):
    print("=== %s.%s %s ===" % (fn, action, json.dumps(param, ensure_ascii=False)))
    try:
        res = ik.call(fn, action, param)
        txt = json.dumps(res, ensure_ascii=False)
        print("  " + txt[:900])
    except Exception as e:                                          # noqa: BLE001
        print("  失败：%s" % e)
    print()
