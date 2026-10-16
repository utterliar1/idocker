#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在爱快上删除一条「本地镜像记录」，用于把被旧记录挡住的 tag 重新拉取。

背景：爱快只按「同名同 tag 的本地记录是否已存在」决定拒不拒绝拉取，**不比对内容**。
所以只要记录在，`latest` 就永远刷不新。前端的「删除镜像」按钮对 status 4/5
（被容器占用）会直接拦下，这里直连接口试。

**安全性**：docker 的 rmi 不会停/删正在运行的容器 —— 最坏情况是接口报错、什么都没变。
所以这里先打印「删前」状态，执行删除，再打印「删后」状态与容器状态，便于对照。

用法：
    python _test/probe_del_image.py                 # 只读，看记录
    python _test/probe_del_image.py --go            # 真的删
    python _test/probe_del_image.py --go --tag latest   # 只删指定 tag 字段
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

WANT_NAME = "vaultwarden/server"
DO = "--go" in sys.argv
TAG_OVERRIDE = None
if "--tag" in sys.argv:
    TAG_OVERRIDE = sys.argv[sys.argv.index("--tag") + 1]

cfg = A.load_config()
ctx = A.Ctx(logger=lambda m, **k: None)
ik = A.IKuai(cfg, ctx)
ik.login()


def show(tagline):
    print("=== %s ===" % tagline)
    hit = None
    for im in A.get_local_images(ik):
        if im.get("name") == WANT_NAME:
            hit = im
            print("  " + json.dumps(im, ensure_ascii=False))
    if not hit:
        print("  （没有 %s 的记录）" % WANT_NAME)
    c = next((x for x in A.get_containers(ik) if x.get("name") == "vaultwarden"), None)
    if c:
        print("  容器 vaultwarden：%s / image=%s / state=%s"
              % (c.get("status"), c.get("image"), c.get("state")))
    else:
        print("  !! 容器 vaultwarden 不在列表里")
    return hit


before = show("删前")
if not before:
    sys.exit(0)

tagval = TAG_OVERRIDE if TAG_OVERRIDE is not None else before.get("tag")
payload = {"id": before["id"], "name": before["name"], "tag": tagval}

if not DO:
    print()
    print("（只读模式。将提交的删除参数：%s）" % json.dumps(payload, ensure_ascii=False))
    sys.exit(0)

print()
print("=== 提交 docker_image.del ===")
print("  param = %s" % json.dumps(payload, ensure_ascii=False))
try:
    res = ik.call("docker_image", "del", payload)
    print("  返回 = %s" % json.dumps(res, ensure_ascii=False)[:600])
except Exception as e:                                              # noqa: BLE001
    print("  抛异常 = %s" % e)

print()
show("删后")
