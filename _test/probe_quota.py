#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对照排查：比较各容器的资源配额字段（file_size / file_num / memory 等）。

假设：更新流程回填 payload 时把磁盘配额写错了（file_size 被 `x or 默认` 吃掉、
file_num 硬编码为 1），导致容器可写层受限、进程起不来。
对照对象是**未被本工具更新过**的容器（clash / ssh / DDNSTO 等）。

只读：只发 docker_container.show（TYPE=data / TYPE=inspect）。
凭据从 D:/Documents/WorkBuddy/爱快/.env 读，绝不回显。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "webapp", "app"))

ENV = os.path.join(os.path.dirname(ROOT), ".env")

KEYS = ("file_size", "file_num", "memory", "cpushares", "auto_start",
        "enabled", "interface", "cmd", "env")


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
    ctx = A.Ctx(logger=lambda m, **k: None)
    ik = A.IKuai(cfg, ctx)
    ik.login()

    print("=== 各容器资源配额对照（更新过的 vs 没更新过的）===\n")
    rows = []
    for c in A.get_containers(ik):
        insp = A.get_inspect(ik, c["id"]) or {}
        rows.append({
            "name": c.get("name"),
            "image": c.get("image"),
            "file_size": insp.get("file_size"),
            "memory": insp.get("memory"),
            "cpushares": insp.get("cpushares"),
            "auto_start": insp.get("auto_start"),
            "file_num": insp.get("file_num"),
            "interface": insp.get("interface"),
        })

    hdr = "%-14s %-30s %-12s %-8s %-10s %-8s" % (
        "name", "image", "file_size", "memory", "cpushares", "file_num")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print("%-14s %-30s %-12s %-8s %-10s %-8s" % (
            r["name"], str(r["image"])[:30], str(r["file_size"]),
            str(r["memory"]), str(r["cpushares"]), str(r["file_num"])))

    print("\n（file_size 单位：字节；1MB=1048576。若只有 lucky 是 1048576 而其余为 0/None，")
    print("  说明是更新流程把配额写错了。）")

    # 爱快新建容器表单的默认值
    print("\n=== 爱快新建容器表单的默认数据 ===")
    try:
        res = ik.call("docker_container", "show", {"TYPE": "image,network,memavailable"})
        txt = json.dumps(res, ensure_ascii=False)
        print(txt[:1500])
    except Exception as e:
        print("取失败：%s" % e)


if __name__ == "__main__":
    main()
