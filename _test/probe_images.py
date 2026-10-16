#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读：打印爱快本地镜像库里跟 vaultwarden / lucky 相关的条目（大小、install 时间）。
凭据从工作区 .env 读，绝不回显。"""
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

cfg = A.load_config()
ctx = A.Ctx(logger=lambda m, **k: None)
ik = A.IKuai(cfg, ctx)
ik.login()

data = A.get_local_images(ik)
print("本地共 %d 个镜像条目\n" % len(data))
print("%-34s %-12s %-9s %-12s %s" % ("镜像", "tag", "大小MB", "install", "ID"))
print("-" * 92)
now = int(time.time())
for im in data:
    name = im.get("name") or ""
    if not any(k in name for k in ("vaultwarden", "lucky")):
        continue
    inst = im.get("install") or im.get("install_time") or 0
    try:
        inst = int(inst)
    except Exception:
        inst = 0
    ago = ("%ds 前" % (now - inst)) if inst else "—"
    size = 0
    try:
        size = int(im.get("size") or 0) / 1048576.0
    except Exception:
        pass
    print("%-34s %-12s %-9.1f %-12s %s"
          % (name, im.get("tag") or "", size, ago, (im.get("id") or "")[:20]))
