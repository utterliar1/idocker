#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对照实验：install 的两种 payload 形状，看哪种能被爱快真正下载。

  A) UI 形状        {"name": "vaultwarden/server", "tag": "1.37.4", "image_logo": ...}
  B) idocker 形状   {"namespace": "vaultwarden", "name": "server", "tag": "...", ...}
  C) 已知 tag 对照  idocker 形状 + tag = "1.37.3-alpine"（在爱快 tag 列表里）

每个实验：提交 → 每 20 秒打印该记录的 status/size/id，最多 6 分钟。
status 图例：-1=下载失败  0=已下载  1=下载中  2=加入任务列表  3=排队  4/5=被占用

用法：
  python _test/probe_install_shapes.py A
  python _test/probe_install_shapes.py B
  python _test/probe_install_shapes.py C
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
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()

os.environ["IKUAI_URL"] = vals.get("ikuai_url", "")
os.environ["IKUAI_USER"] = vals.get("ikuai_id", "")
os.environ["IKUAI_PASS"] = vals.get("ikuai_pw", "")

import ikuai_api as A  # noqa: E402

which = (sys.argv[1] if len(sys.argv) > 1 else "A").upper()
LOGO = "https://static.1ms.run/image/avatar/default.png"

if which == "A":
    payload = {"name": "vaultwarden/server", "tag": "1.37.4", "image_logo": LOGO}
    watch_repo, watch_tag = "vaultwarden/server", "1.37.4"
elif which == "B":
    payload = {"namespace": "vaultwarden", "name": "server", "tag": "1.37.4",
               "image_logo": LOGO}
    watch_repo, watch_tag = "vaultwarden/server", "1.37.4"
elif which == "C":
    payload = {"namespace": "vaultwarden", "name": "server", "tag": "1.37.3-alpine",
               "image_logo": LOGO}
    watch_repo, watch_tag = "vaultwarden/server", "1.37.3-alpine"
else:
    # D) 对照：lucky 是个一小时前刚下载成功的仓库，这个 tag 也在它的列表里
    payload = {"namespace": "gdy666", "name": "lucky", "tag": "2.27.1",
               "image_logo": LOGO}
    watch_repo, watch_tag = "gdy666/lucky", "2.27.1"

cfg = A.load_config()
ctx = A.Ctx(logger=lambda m, **k: None)
ik = A.IKuai(cfg, ctx)
ik.login()


def rec():
    for im in A.get_local_images(ik):
        if im.get("name") == watch_repo and watch_tag in str(im.get("tag") or ""):
            return im
    return None


print("=== 实验 %s ===" % which)
print("  payload = %s" % json.dumps(payload, ensure_ascii=False))
before = rec()
print("  提交前 : %s" % (json.dumps({"tag": before.get("tag"), "status": before.get("status"),
                                      "size": before.get("size")}, ensure_ascii=False)
                          if before else "无记录"))
print()
print("=== 提交 install ===")
try:
    res = ik.call("docker_image", "install", payload)
    print("  返回 = %s" % json.dumps(res, ensure_ascii=False)[:300])
except Exception as e:                                              # noqa: BLE001
    print("  抛异常 = %s" % e)
    print("  is_already_exists = %s" % A.is_already_exists(str(e)))

print()
print("=== 盯 6 分钟 ===")
t0 = time.time()
last = None
while time.time() - t0 < 360:
    time.sleep(20)
    r = rec()
    cur = (r.get("status"), r.get("size"), str(r.get("id"))[:10]) if r else None
    if cur != last:
        print("  [%4ds] %s" % (time.time() - t0,
                               ("status=%s size=%s id=%s" % cur) if cur else "（记录消失）"))
        last = cur
    if r and r.get("status") == 0 and (r.get("size") or 0) > 0:
        print("  ✅ 落地成功")
        sys.exit(0)
    if r and r.get("status") == -1:
        print("  ✗ 下载失败（status=-1）")
        sys.exit(2)
print("  ⏱ 超时")
