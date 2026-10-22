#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证爱快 `docker_container.update` 的两条语义（v1.5.1 的立论依据）。

结论（2026-10-22 真机实测）：

1. **update = 拆了重建，容器 id 会变**；
2. 拿**旧 id** 再下发，爱快回 ``{"code":0,"message":"Success"}`` 却**什么都不做**。
   这和 ``docker_image.del`` 一个脾气 —— 只看返回值会以为是干的。

搬运流程「切到版本号 tag → 删旧记录 → 重拉 → 切回 latest」里有两次 update，
中间 id 已经变了，所以第二次必须重读容器拿当前 id。这个脚本就是复验那一幕。

默认**只读**（打印现状）。加 ``--go`` 才会真的下发 update —— 它会重启容器几秒，
所以一定要指定一个可以放心重启的容器，并保证两个 tag 内容相同（切过去再切回来）。

用法：
    python probe_update_semantics.py                       # 只看
    python probe_update_semantics.py --go --container sub --tag latest
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
for _p in (os.path.join(os.path.dirname(HERE), "webapp", "app"), "/app", "."):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.append(_p)

import ikuai_api as A  # noqa: E402


def find(ik, name):
    return {x["name"]: x for x in A.get_containers(ik)}.get(name) or {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--container", default="", help="要拿来做实验的容器名")
    ap.add_argument("--tag", default="", help="来回切换用的目标 tag（必须与当前内容相同）")
    ap.add_argument("--go", action="store_true", help="真的下发 update（会重启容器）")
    args = ap.parse_args()

    ik = A.IKuai(A.load_config(), A.Ctx(verbose=args.go))
    ik.login()

    print("%-12s %-30s %s" % ("容器", "image", "id"))
    for c in sorted(A.get_containers(ik), key=lambda x: x.get("name") or ""):
        print("%-12s %-30s %s" % (c.get("name"), c.get("image"),
                                  str(c.get("id"))[:12]))

    if not args.go:
        print("\n（只读模式；加 --go 才会真的切 tag）")
        return 0
    if not args.container or not args.tag:
        print("--go 需要同时给 --container 和 --tag")
        return 2

    c0 = find(ik, args.container)
    if not c0:
        print("找不到容器 %s" % args.container)
        return 2
    print("\n== 1. 当前：%s  id=%s" % (c0.get("image"), str(c0.get("id"))[:12]))

    p = A.container_payload(c0, A.get_inspect(ik, c0.get("id")), tag=args.tag)[0]
    ik.call("docker_container", "update", p)
    time.sleep(4)
    c1 = find(ik, args.container)
    print("== 2. update → %s 后：image=%s id=%s"
          % (args.tag, c1.get("image"), str(c1.get("id"))[:12]))
    print("      id 是否变化：%s"
          % ("变了（重建容器）" if c1.get("id") != c0.get("id") else "没变"))

    orig_tag = A.split_image(c0.get("image") or "")[2]
    # 故意用旧 id 下发「切回原 tag」
    p_bad = A.container_payload(c1, A.get_inspect(ik, c1.get("id")),
                                tag=orig_tag)[0]
    p_bad["id"] = c0.get("id")
    res = ik.call("docker_container", "update", p_bad)
    time.sleep(4)
    c2 = find(ik, args.container)
    print("== 3. 用【旧 id】切回 %s：返回 %r" % (orig_tag, res.get("code")))
    print("      容器 image=%s（预期：仍是 %s，说明这一步是空操作）"
          % (c2.get("image"), args.tag))

    # 用当前 id 再来一次（恢复原状）
    p_ok = A.container_payload(c2, A.get_inspect(ik, c2.get("id")),
                               tag=orig_tag)[0]
    ik.call("docker_container", "update", p_ok)
    time.sleep(4)
    c3 = find(ik, args.container)
    print("== 4. 用【当前 id】切回 %s：容器 image=%s" % (orig_tag, c3.get("image")))
    return 0 if c3.get("image") == c0.get("image") else 1


if __name__ == "__main__":
    sys.exit(main())
