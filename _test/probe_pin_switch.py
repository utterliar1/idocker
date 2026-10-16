#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读诊断（除 install 本身外不改任何东西）：
问爱快「下 vaultwarden/server:latest」，把它的原始回应打出来，
并回查本地镜像条目、容器占用关系、tag 匹配结果。

凭据从工作区 .env 读，绝不回显。
"""
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
ctx = A.Ctx(logger=lambda m, **k: print("   [log] %s" % m))
ik = A.IKuai(cfg, ctx)
ik.login()

NAME, NS, TAG = "server", "vaultwarden", "latest"

print("== 本地镜像条目（vaultwarden）==")
for im in A.get_local_images(ik):
    if "vaultwarden" in (im.get("name") or ""):
        print("  name=%s tag=%r id=%s size=%s install=%s containers=%s"
              % (im.get("name"), im.get("tag"), str(im.get("id"))[:16],
                 im.get("size"), im.get("install"), im.get("containers")))

print()
print("== image_owner('vaultwarden') ==")
own = A.image_owner(ik, "vaultwarden")
print("  ", own)
print("  tag_matches(%r, 'latest') = %s"
      % ((own or {}).get("tag"), A.tag_matches((own or {}).get("tag"), "latest")))

print()
print("== 容器自身声明 ==")
for c in A.get_containers(ik):
    if c.get("name") == "vaultwarden":
        print("  image=%r" % c.get("image"))
        print("  split_image -> %s" % (A.split_image(c.get("image")),))

print()
print("== 调 docker_image.install（name=%s tag=%s ns=%s）==" % (NAME, TAG, NS))
try:
    res = ik.call("docker_image", "install",
                  {"name": NAME, "tag": TAG, "image_logo": "", "namespace": NS})
    print("  ✅ 返回成功：%s" % str(res)[:300])
except Exception as e:                                          # noqa: BLE001
    print("  ⚠ 抛异常：%s" % str(e)[:300])
    print("  is_already_exists -> %s" % A.is_already_exists(e))

print()
print("== 再回查本地条目 ==")
for im in A.get_local_images(ik):
    if "vaultwarden" in (im.get("name") or ""):
        print("  name=%s tag=%r id=%s size=%s install=%s"
              % (im.get("name"), im.get("tag"), str(im.get("id"))[:16],
                 im.get("size"), im.get("install")))
