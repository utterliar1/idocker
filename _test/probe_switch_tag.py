#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把某个容器的镜像**就地**切到指定 tag（走 docker_container.update，不删容器）。

用的是 idocker 平时的更新路径与同一套 payload 拼法（``A.container_payload``），
所以挂载 / IP / env / cmd / auto_start 全部原样保留，只有镜像 tag 变。

用法：
    python _test/probe_switch_tag.py vaultwarden 1.37.4        # 只读，看会提交什么
    python _test/probe_switch_tag.py vaultwarden 1.37.4 --go   # 真的切
"""
import json
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

NAME = sys.argv[1]
TAG = sys.argv[2]
DO = "--go" in sys.argv

cfg = A.load_config()
ctx = A.Ctx(logger=lambda m, **k: None)
ik = A.IKuai(cfg, ctx)
ik.login()


def snap():
    c = next((x for x in A.get_containers(ik) if x.get("name") == NAME), None)
    own = A.image_owner(ik, NAME)
    return c, own


c, own = snap()
if not c:
    print("!! 找不到容器 %s" % NAME)
    sys.exit(1)

print("=== 切前 ===")
print("  container.image  : %s" % c.get("image"))
print("  state/status     : %s / %s" % (c.get("state"), c.get("status")))
print("  mounts           : %s" % c.get("mounts"))
print("  ipaddr / ip6addr : %s / %s" % (c.get("ipaddr"), c.get("ip6addr")))
print("  auto_start       : %s" % c.get("auto_start"))
print("  owner(镜像记录)  : %s" % (json.dumps(own, ensure_ascii=False) if own else "无"))

insp = A.get_inspect(ik, c["id"]) or {}
payload, unmapped = A.container_payload(c, insp, tag=TAG)

safe = dict(payload)
safe["env"] = "******" if safe.get("env") else ""
print()
print("=== 将提交的 payload（env 已脱敏）===")
print("  %s" % json.dumps(safe, ensure_ascii=False))
print("  未映射端口数 = %d" % unmapped)

if not DO:
    print()
    print("（只读模式，未提交。加 --go 才真的切）")
    sys.exit(0)

print()
print("=== docker_container.update ===")
try:
    res = ik.call("docker_container", "update", payload)
    print("  返回 = %s" % json.dumps(res, ensure_ascii=False)[:300])
except Exception as e:                                              # noqa: BLE001
    print("  抛异常 = %s" % e)
    sys.exit(1)

print()
print("=== 等容器起来 ===")
after = None
for i in range(10):
    time.sleep(3)
    after = next((x for x in A.get_containers(ik) if x.get("name") == NAME), None)
    if after and after.get("state") == "running":
        print("  [%2ds] %s / %s" % ((i + 1) * 3, after.get("state"), after.get("status")))
        break
    print("  [%2ds] %s" % ((i + 1) * 3, (after or {}).get("state")))

print()
print("=== 切后 ===")
for _ in range(6):
    c2, own2 = snap()
    print("  image=%s state=%s owner.tag=%s"
          % ((c2 or {}).get("image"), (c2 or {}).get("state"),
             (own2 or {}).get("tag")))
    if own2 and A.tag_matches(own2.get("tag"), TAG):
        print("  ✅ 容器已挂到 %s" % TAG)
        break
    time.sleep(5)
