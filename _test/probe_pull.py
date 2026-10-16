#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""触发爱快拉取一个镜像 tag，并（可选）轮询直到落地。

这是判定「本地记录是否还在挡」的唯一可信实验：
  · 被接受 → 爱快真的会去下载（返回 code 0 且不报「已存在相同内容」）
  · 被拒绝 → 抛「已存在相同内容 该镜像已存在，请勿重复下载」

用法：
    python _test/probe_pull.py <ns> <name> <tag>            # 只提交，不等待
    python _test/probe_pull.py <ns> <name> <tag> --wait 2400
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

NS = sys.argv[1]
NAME = sys.argv[2]
TAG = sys.argv[3]
WAIT = 0
if "--wait" in sys.argv:
    WAIT = int(sys.argv[sys.argv.index("--wait") + 1])

cfg = A.load_config()
ctx = A.Ctx(logger=lambda m, **k: None)
ik = A.IKuai(cfg, ctx)
ik.login()

repo = "%s/%s" % (NS, NAME)


def dump(when):
    print("=== %s：%s 相关记录 ===" % (when, repo))
    found = False
    for im in A.get_local_images(ik):
        if im.get("name") == repo:
            found = True
            print("  tag=%-16s status=%-3s size=%-10s id=%s containers=%s"
                  % (im.get("tag"), im.get("status"), im.get("size"),
                     str(im.get("id"))[:12], len(im.get("containers") or [])))
    if not found:
        print("  （无记录）")


dump("提交前")

print()
print("=== docker_image.install %s:%s ===" % (repo, TAG))
payload = {"namespace": NS, "name": NAME, "tag": TAG,
           "image_logo": "https://static.1ms.run/image/avatar/default.png"}
try:
    res = ik.call("docker_image", "install", payload)
    print("  返回 = %s" % json.dumps(res, ensure_ascii=False)[:400])
except Exception as e:                                              # noqa: BLE001
    msg = str(e)
    print("  抛异常 = %s" % msg)
    print("  is_already_exists = %s" % A.is_already_exists(msg))

if WAIT <= 0:
    sys.exit(0)

print()
print("=== 轮询等待落地（最多 %d 秒）===" % WAIT)
t0 = time.time()
last = ""
while time.time() - t0 < WAIT:
    time.sleep(20)
    for im in A.get_local_images(ik):
        if im.get("name") != repo:
            continue
        line = "tag=%s status=%s size=%s" % (im.get("tag"), im.get("status"), im.get("size"))
        if line != last:
            print("  [%4ds] %s" % (time.time() - t0, line))
            last = line
        if TAG in str(im.get("tag") or "") and (im.get("size") or 0) > 0 and im.get("status") == 0:
            if TAG in [t.strip() for t in str(im.get("tag")).split(",")]:
                print("  ✅ 落地")
                sys.exit(0)
print("  ⏱ 超时，仍未确认落地")
