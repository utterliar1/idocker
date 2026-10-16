#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读排查：哪些容器「锁定在固定版本 tag」，导致检测永远报「已是最新」。

背景：idocker 检测的是容器**当前挂的那个 tag**。若容器被指定 tag 更新过
（如 vaultwarden/server:1.37.3），它就锁在那个 tag 上、不再跟随 latest，
而版本 tag 的上游内容永远不变 → 检测永远报「最新」，用户却以为漏检了。

本探针只读，对每个「锁定在非 latest tag」的容器，额外查一次该仓库的 latest，
比较两者 digest，标出「latest 已经更新、但容器还锁在旧 tag」的情况。

用法：python _test/probe_tag_drift.py
凭据从 D:/Documents/WorkBuddy/爱快/.env 读，绝不回显。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "webapp", "app"))

ENV = os.path.join(os.path.dirname(ROOT), ".env")


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

    import ikuai_api as A

    cfg = A.load_config()
    ctx = A.Ctx(logger=lambda m, **k: None)
    ik = A.IKuai(cfg, ctx)
    ik.login()

    regs = A.resolve_registries(ik, cfg)
    print("用的加速源：%s\n" % "、".join(regs))

    rows = []
    for c in A.get_containers(ik):
        image = c.get("image") or ""
        repo, tag = A.repo_of(image), A.split_image(image)[2]
        if not repo or repo in [r[0] for r in rows]:
            continue
        rows.append((repo, tag, c.get("name")))

    print("%-22s %-10s %-14s %s" % ("仓库", "当前tag", "latest摘要", "判定"))
    print("-" * 78)
    locked = []
    for repo, tag, name in rows:
        try:
            d_cur, _ = A.remote_digest(regs, repo, tag, ctx, 25)
        except Exception as e:
            print("%-22s %-10s %-14s ✗ 查不到当前 tag（%s）"
                  % (repo, tag, "—", str(e).splitlines()[0][:30]))
            continue
        if tag == "latest":
            print("%-22s %-10s %-14s 跟随 latest（正常）" % (repo, tag, "—"))
            continue
        try:
            d_latest, _ = A.remote_digest(regs, repo, "latest", ctx, 25)
        except Exception as e:
            print("%-22s %-10s %-14s 锁定；查 latest 失败（%s）"
                  % (repo, tag, "—", str(e).splitlines()[0][:20]))
            continue
        if d_latest == d_cur:
            print("%-22s %-10s %-14s 锁定；但与 latest 内容相同" % (repo, tag, d_latest[:12]))
        else:
            locked.append((name, repo, tag, d_cur, d_latest))
            print("%-22s %-10s %-14s ⚠ 锁定在旧 tag，latest 已更新"
                  % (repo, tag, d_latest[:12]))

    print()
    if locked:
        print("=== 需要处理（%d 个）===" % len(locked))
        for name, repo, tag, d_cur, d_latest in locked:
            print("  · 容器 %s：锁在 %s:%s，但 %s:latest 内容已变"
                  % (name, repo, tag, repo))
            print("      切到 latest：idocker 里对该容器执行更新并显式指定 tag=latest")
            print("      留在固定版本：更新到最新版本号 tag（不跟随 latest）")
    else:
        print("没有「锁在旧 tag」的容器。")


if __name__ == "__main__":
    main()
