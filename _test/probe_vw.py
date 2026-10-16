#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读：为「删掉重建保数据」做前置勘察。

拉三样东西：
  1. vaultwarden 容器在爱快上的**完整配置**（含 mounts / ports / env / cmd），
     原样存到仓库外的备份文件，供重建时 1:1 复刻；
  2. 该容器的 inspect（真实挂载点、镜像 ID）；
  3. 爱快本地镜像里 vaultwarden/server 那条记录的全字段（status / containers）。

凭据从工作区根 .env 读，绝不回显。env 变量只打印**键名**，值一律脱敏。
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

TARGET = sys.argv[1] if len(sys.argv) > 1 else "vaultwarden"

cfg = A.load_config()
ctx = A.Ctx(logger=lambda m, **k: None)
ik = A.IKuai(cfg, ctx)
ik.login()

conts = A.get_containers(ik)
c = next((x for x in conts if x.get("name") == TARGET), None)
if not c:
    print("!! 没找到容器 %s，现有：%s" % (TARGET, ", ".join(x.get("name", "?") for x in conts)))
    sys.exit(1)

insp = A.get_inspect(ik, c["id"]) or {}

print("=== 容器 %s 关键字段 ===" % TARGET)
print("  id          : %s" % c.get("id"))
print("  image       : %s" % c.get("image"))
print("  state/status: %s / %s" % (c.get("state"), c.get("status")))
print("  auto_start  : %s" % c.get("auto_start"))
print("  interface   : %s" % c.get("interface"))
print("  ipaddr      : %s" % c.get("ipaddr"))
print("  ip6addr     : %s" % c.get("ip6addr"))
print("  mac         : %s" % c.get("mac"))
print("  mounts      : %s" % c.get("mounts"))
print("  ports       : %s" % json.dumps(c.get("ports"), ensure_ascii=False))
print("  cmd         : %s" % c.get("cmd"))
print("  comment     : %s" % c.get("comment"))

env_raw = c.get("env") or ""
env_items = [x for x in str(env_raw).split(",") if x.strip()]
print("  env         : %d 项" % len(env_items))
for item in env_items:
    k = item.split("=", 1)[0].strip()
    print("      · %s = ******" % k)

print()
print("=== inspect 里的挂载（真实数据落点）===")
for m in (insp.get("Mounts") or insp.get("mounts") or []) or []:
    print("  %s" % json.dumps(m, ensure_ascii=False))
print("  Image(sha)  : %s" % str(insp.get("Image"))[:24])
print("  RestartPol. : %s" % (insp.get("HostConfig") or {}).get("RestartPolicy"))

print()
print("=== 爱快本地镜像记录：vaultwarden/server ===")
for im in A.get_local_images(ik):
    if "vaultwarden" in (im.get("name") or ""):
        print(json.dumps(im, ensure_ascii=False))

print()
print("=== 该容器用的镜像（image_owner）===")
own = A.image_owner(ik, TARGET)
print(json.dumps(own, ensure_ascii=False) if own else "  (无归属，容器可能挂在别的镜像条目上)")

# 备份完整配置到仓库外
BK = os.path.join(os.path.dirname(ROOT), "_acl_backup")
os.makedirs(BK, exist_ok=True)
out = os.path.join(BK, "container_%s.json" % TARGET)
with open(out, "w", encoding="utf-8") as f:
    json.dump({"container": c, "inspect": insp}, f, ensure_ascii=False, indent=2)
print()
print("完整配置（含 env 原值）已存到仓库外：%s" % out)
