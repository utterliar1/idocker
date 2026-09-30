#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读排查：容器实际引用的镜像 vs 本地镜像库。

用途：更新流程报「成功」但容器看起来没换版本时，用它看真相 ——
      容器对象里的 image/tag/id 到底是哪个，本地镜像库里有哪些 tag/ID。

只读：只发 docker_container.show / docker_image.show / docker_server.show。
凭据从仓库根目录上一级的 .env 读取，绝不回显。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "webapp", "app"))

ENV = next((p for p in (os.path.join(ROOT, ".env"), os.path.join(os.path.dirname(ROOT), ".env")) if os.path.isfile(p)), os.path.join(ROOT, ".env"))


def load_env():
    vals = {}
    with open(ENV, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    return vals


def main():
    vals = load_env()
    os.environ["IKUAI_URL"] = vals.get("ikuai_url", "")
    os.environ["IKUAI_USER"] = vals.get("ikuai_id", "")
    os.environ["IKUAI_PASS"] = vals.get("ikuai_pw", "")
    os.environ.setdefault("DATA_DIR", os.path.join(ROOT, "_test", "_data"))

    import ikuai_api as A

    cfg = A.load_config()
    print("爱快地址：%s（凭据已隐藏）\n" % cfg["router"]["url"])
    ctx = A.Ctx(logger=lambda m, **k: None)
    ik = A.IKuai(cfg, ctx)
    ik.login()

    srv = A.get_server(ik)
    print("Docker 版本：%s   状态：%s"
          % (srv["overview"].get("docker_version"), srv["overview"].get("status")))
    print("加速源：%s\n" % srv.get("settings", {}).get("mirrors"))

    raw_containers = A.get_containers(ik)
    print("=== 容器（%d 个）===" % len(raw_containers))
    for c in raw_containers:
        print("  %-14s image=%-28s tag=%-12s state=%-8s status=%s"
              % (c.get("name"), c.get("image"), c.get("tag"),
                 c.get("state"), c.get("status")))
    print()

    raw_images = A.get_local_images(ik)
    print("=== 本地镜像（%d 条）===" % len(raw_images))
    for img in raw_images:
        print("  name=%-26s tag=%-18s id=%s install=%s created=%s"
              % (img.get("name"), img.get("tag"),
                 str(img.get("id"))[:16], img.get("install"), img.get("created")))
    print()

    # 重点比对 lucky
    print("=== lucky 专项 ===")
    lucky = [c for c in raw_containers if c.get("name") == "lucky"]
    if lucky:
        c = lucky[0]
        print("  容器原始字段：%s" % json.dumps(c, ensure_ascii=False))
        insp = A.get_inspect(ik, c.get("id"))
        print("  inspect：%s" % (json.dumps(insp, ensure_ascii=False)[:600] if insp else "(空)"))
    print("  gdy666/lucky 相关镜像：")
    for img in raw_images:
        if "lucky" in str(img.get("name") or ""):
            print("    %s" % json.dumps(img, ensure_ascii=False))

    idx = A.LocalImages(raw_images)
    print("\n  LocalImages.find('gdy666/lucky','latest') → %s"
          % json.dumps(idx.find("gdy666/lucky", "latest"), ensure_ascii=False))
    print("  LocalImages.find('gdy666/lucky','2.27.2') → %s"
          % json.dumps(idx.find("gdy666/lucky", "2.27.2"), ensure_ascii=False))
    print("  image_signature(lucky,latest) → %s" % A.image_signature(ik, "gdy666/lucky", "latest"))
    print("  image_signature(lucky,2.27.2) → %s" % A.image_signature(ik, "gdy666/lucky", "2.27.2"))


if __name__ == "__main__":
    main()
