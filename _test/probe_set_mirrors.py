#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读写爱快的 Docker 加速源设置（docker_server save）。

为什么需要它：爱快下载引擎卡死后，**光 down/up 不一定够** —— 2026-10-16 实测，
能救回来的那次是「先 save 改过加速源，再 down→up」。改 save 会让爱快重写
docker 配置并重新初始化下载组件，所以把这一步做成可复用的工具。

用法：
    python _test/probe_set_mirrors.py                          # 只读：看当前加速源
    python _test/probe_set_mirrors.py --mirrors "https://a,https://b"          # 预览要提交什么
    python _test/probe_set_mirrors.py --mirrors "https://a,https://b" --go     # 真的写

凭据从工作区根 .env 读，绝不回显。
"""
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

DO = "--go" in sys.argv
NEW = None
for i, a in enumerate(sys.argv):
    if a == "--mirrors" and i + 1 < len(sys.argv):
        NEW = sys.argv[i + 1]

cfg = A.load_config()
ik = A.IKuai(cfg, A.Ctx(logger=lambda m, **k: None))
ik.login()


def read_row():
    d = ik.call("docker_server", "show", {"TYPE": "data"})
    rows = ((d.get("results") or {}).get("data")) or []
    return rows[0] if rows else {}


row = read_row()
print("=== 当前 docker_server 设置 ===")
print("  " + json.dumps(row, ensure_ascii=False))
print("  当前加速源：%s" % (row.get("mirrors") or "（空）"))

if not NEW:
    print()
    print("（未指定 --mirrors，只读。）")
    sys.exit(0)

payload = dict(row)
payload["mirrors"] = NEW
print()
print("=== 将要提交 ===")
print("  " + json.dumps(payload, ensure_ascii=False))
if not DO:
    print()
    print("（只读模式。加 --go 才真的写。）")
    sys.exit(0)

print()
print("=== docker_server.save 提交 ===")
try:
    resp = ik.call("docker_server", "save", payload)
    print("  返回 = %s" % json.dumps(resp, ensure_ascii=False))
except Exception as e:                                       # noqa: BLE001
    print("  ✗ 失败：%s" % e)
    sys.exit(1)

back = read_row()
print()
print("=== 回读确认 ===")
print("  " + json.dumps(back, ensure_ascii=False))
print("  加速源：%s" % (back.get("mirrors") or "（空）"))
print("  %s" % ("✓ 已生效" if back.get("mirrors") == NEW else "⚠ 回读与提交不一致，请到爱快后台确认"))
