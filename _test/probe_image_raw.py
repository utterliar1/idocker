#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读：把爱快镜像相关的原始记录全字段打出来（含分页 image 列表），
看有没有 status / error / size 之类的落地状态。"""
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

print("=== docker_image.show TYPE=data（全字段）===")
for im in A.get_local_images(ik):
    if any(k in (im.get("name") or "") for k in ("lucky", "vaultwarden")):
        print(json.dumps(im, ensure_ascii=False)[:700])
        print()

print("=== docker_image.show TYPE=image（分页列表，原始响应）===")
try:
    res = ik.call("docker_image", "show",
                  {"TYPE": "image", "page": 1, "page_size": 30, "keyword": ""})
    print(json.dumps(res, ensure_ascii=False)[:2500])
except Exception as e:                                          # noqa: BLE001
    print("  失败：%s" % e)

print()
print("=== docker_server.show TYPE=overview ===")
try:
    print(json.dumps(A.get_server(ik).get("overview"), ensure_ascii=False)[:1200])
except Exception as e:                                          # noqa: BLE001
    print("  失败：%s" % e)

print()
print("=== docker_server.show TYPE=data ===")
try:
    print(json.dumps(A.get_server(ik).get("settings"), ensure_ascii=False)[:1500])
except Exception as e:                                          # noqa: BLE001
    print("  失败：%s" % e)
