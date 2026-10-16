#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读排查：某个镜像「检测不出新版本」到底卡在哪一环。

做四件事（全部只读，不发任何写请求）：
  1. 从爱快读出容器 + 爱快自己配的加速源
  2. 逐个源（爱快加速源 + 官方 Docker Hub）查同一个 repo:tag 的 Docker-Content-Digest
     —— 若官方与加速源的 digest 不同，说明**加速源缓存旧了**
  3. 打印 state.json 里的基准 digest（检测时拿来比对的「旧值」）
  4. 打印地那镜像的本地登记时间

用法：
  python _test/probe_check_why.py vaultwarden/server latest
  python _test/probe_check_why.py gdy666/lucky latest

凭据从 D:/Documents/WorkBuddy/爱快/.env 读，绝不回显。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "webapp", "app"))

ENV = os.path.join(os.path.dirname(ROOT), ".env")

OFFICIAL = "https://registry-1.docker.io"


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
    repo = sys.argv[1] if len(sys.argv) > 1 else "vaultwarden/server"
    tag = sys.argv[2] if len(sys.argv) > 2 else "latest"

    vals = load_env()
    os.environ["IKUAI_URL"] = vals.get("ikuai_url", "")
    os.environ["IKUAI_USER"] = vals.get("ikuai_id", "")
    os.environ["IKUAI_PASS"] = vals.get("ikuai_pw", "")

    import ikuai_api as A

    cfg = A.load_config()
    data_dir = os.environ.get("DATA_DIR") or cfg.get("data_dir") or \
        os.path.join(ROOT, "_test", "_data")
    print("爱快地址：%s（凭据已隐藏）" % cfg["router"]["url"])
    print("目标镜像：%s:%s" % (repo, tag))
    print("DATA_DIR：%s\n" % data_dir)

    ctx = A.Ctx(logger=lambda m, **k: None)
    ik = A.IKuai(cfg, ctx)
    ik.login()

    # ---- 1. 容器侧 ----
    print("=== 1. 容器（与目标镜像相关的） ===")
    hit = []
    for c in A.get_containers(ik):
        image = c.get("image") or ""
        r = c.get("name") or ""
        print("  %-16s image=%-30s state=%-8s" % (r, image, c.get("state")))
        if repo.split("/")[-1].lower() in image.lower():
            hit.append(c)
    if hit:
        print("  → 命中：%s" % json.dumps([{"name": c["name"], "image": c["image"]}
                                           for c in hit], ensure_ascii=False))
    print()

    # ---- 2. 各源 digest 对比 ----
    try:
        mirrors = A.get_mirrors(ik)
        print("=== 2. 爱快配置的加速源 ===")
        print("  %s\n" % (mirrors or "(爱快未配置)"))
    except Exception as e:
        mirrors = []
        print("=== 2. 读取爱快加速源失败：%s ===\n" % e)

    sources = list(mirrors) + [OFFICIAL]
    print("=== 3. 各源查 %s:%s 的 digest ===" % (repo, tag))
    seen = {}
    for reg in sources:
        try:
            d, used = A.remote_digest([reg], repo, tag, ctx, 25)
            seen[reg] = d
            print("  %-34s %s" % (reg, d))
        except Exception as e:
            print("  %-34s ✗ %s" % (reg, str(e).splitlines()[0][:60]))
    print()
    uniq = set(seen.values())
    if len(uniq) > 1:
        print("  ⚠ 各源结果不一致！说明**至少有一个源的缓存是旧的** —— 这就是检测不出的直接原因。")
    elif len(uniq) == 1:
        print("  · 各源结果一致。")
    print()

    # ---- 4. 本工具的基准 ----
    print("=== 4. state.json 里的基准 digest ===")
    key = "%s:%s" % (repo, tag)
    sp = os.path.join(data_dir, "state.json")
    state = {}
    if os.path.isfile(sp):
        try:
            with open(sp, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception as e:
            print("  读取失败：%s" % e)
    rec = state.get(key)
    if rec:
        print("  %s → %s" % (key, json.dumps(rec, ensure_ascii=False)))
        base = rec.get("digest")
        print("  基准 digest：%s" % base)
        if base:
            for reg, d in seen.items():
                if d == base:
                    print("  → 与 %s 一致 → 比对结果是「latest（已最新）」" % reg)
                else:
                    print("  → 与 %s 不一致 → 比对结果是「newer（有新版本）」" % reg)
    else:
        print("  %s 无基准记录（首次检测会报 unknown）" % key)
    print()

    print("=== 5. last_check.json 里该镜像的最近一次结果 ===")
    lp = os.path.join(data_dir, "last_check.json")
    if os.path.isfile(lp):
        try:
            with open(lp, "r", encoding="utf-8") as f:
                lc = json.load(f)
            it = (lc.get("items") or {}).get(key)
            print("  checked_at=%s  %s" % (lc.get("at_str"),
                                           json.dumps(it, ensure_ascii=False)))
        except Exception as e:
            print("  读取失败：%s" % e)
    else:
        print("  （无 last_check.json）")


if __name__ == "__main__":
    main()
