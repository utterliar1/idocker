#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读：问**爱快自己**去仓库要某镜像的 tag 列表。

这是决定性测试：idocker / 本机走的是各自的网络出口，而 `docker_image.show
{TYPE:"tag"}` 是爱快**用它自己的网络**去问仓库 —— 爱快后台「添加镜像」时下拉框
里的 tag 就是它给的。若这里只到 1.37.3，就坐实「爱快侧拿不到 1.37.4」。

全部只读，只发 show 请求，不 install、不 del。

用法：
  python _test/probe_ikuai_tags.py vaultwarden server
  python _test/probe_ikuai_tags.py gdy666 lucky
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

ns = sys.argv[1] if len(sys.argv) > 1 else "vaultwarden"
nm = sys.argv[2] if len(sys.argv) > 2 else "server"

cfg = A.load_config()
ctx = A.Ctx(logger=lambda m, **k: None)
ik = A.IKuai(cfg, ctx)
ik.login()

# 先看爱快自己的 mirrors 设置里有没有额外开关
print("=== docker_server.show TYPE=data（mirrors 等）===")
try:
    st = A.get_server(ik)
    ov = st.get("settings") or {}
    if isinstance(ov, list) and ov:
        for r in ov:
            print("  mirrors=%s" % r.get("mirrors"))
            for k, v in r.items():
                if k != "mirrors":
                    print("  %s = %s" % (k, json.dumps(v, ensure_ascii=False)[:120]))
    else:
        print(json.dumps(ov, ensure_ascii=False)[:800])
except Exception as e:                                          # noqa: BLE001
    print("  失败：%s" % e)
print()

shapes = [
    {"TYPE": "tag", "name": nm, "namespace": ns},
    {"TYPE": "tag", "name": nm, "ns": ns},
    {"TYPE": "tag", "image": "%s/%s" % (ns, nm)},
    {"TYPE": "tag", "name": "%s/%s" % (ns, nm)},
]
for sh in shapes:
    print("=== docker_image.show %s ===" % json.dumps(sh, ensure_ascii=False))
    try:
        res = ik.call("docker_image", "show", sh)
        s = json.dumps(res, ensure_ascii=False)
        print("  %s" % s[:900])
    except Exception as e:                                      # noqa: BLE001
        print("  失败：%s" % str(e).splitlines()[0][:120])
    print()
