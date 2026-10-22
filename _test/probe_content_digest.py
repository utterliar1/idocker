#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读：验证「本地镜像 ID == 上游 amd64 子清单的 config.digest」这个前提。

idocker 判断「本地这份镜像是不是最新」现在靠这两者比对（见 jobs._judge）。
如果爱快记录的 ``id`` 其实是别的东西（比如清单 digest、或者被截断的短 ID），
那整套判断都会失真 —— 所以要能在真机上一眼看出结论是怎么来的。

对每个运行中的容器，打印：
    · 容器声明的 image:tag
    · 本地镜像记录的 id（原样 / 规范化后）
    · 上游 manifest digest（tag 那一层）
    · 上游 amd64 config digest（= 本地镜像 ID 应当等于的东西）
    · 判定：SAME（本地就是最新）/ DIFF（本地落后）/ UNKNOWN（取不到）

用法（在本工具容器里）：
    python _test/probe_content_digest.py                 # 全部容器
    python _test/probe_content_digest.py metacubex/mihomo # 只看某个 repo
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# 脚本自己所在的目录优先 —— 这样把新版 ikuai_api.py 一起放到 /tmp 时，
# 验证的是**新逻辑**，不受容器里那份线上旧代码影响。
sys.path.insert(0, HERE)
# 其它候选目录一律**追加到末尾** —— 不能用 insert(0)，否则容器里的 /app（线上旧代码）
# 会抢在脚本目录前面，验证到的就不是新逻辑了。
for _p in (os.path.join(os.path.dirname(HERE), "webapp", "app"), "/app", "."):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.append(_p)

import ikuai_api as A  # noqa: E402


def short(x):
    n = A.norm_digest(x)
    return n[:12] if n else (str(x or "")[:12] or "-")


def main():
    want = sys.argv[1] if len(sys.argv) > 1 else ""
    cfg = A.load_config()
    ctx = A.Ctx(verbose=True)
    ik = A.IKuai(cfg, ctx)
    ik.login()

    images = A.LocalImages(A.get_local_images(ik))
    regs = A.resolve_registries(ik, cfg)
    print("加速源：%s\n" % "、".join(regs))

    seen = set()
    for c in A.get_containers(ik):
        repo, tag = A.repo_of(c.get("image") or ""), A.split_image(c.get("image") or "")[2]
        if want and repo != want:
            continue
        if (repo, tag) in seen:
            continue
        seen.add((repo, tag))
        img = images.find(repo, tag)
        raw_id = str((img or {}).get("id") or "")
        print("── %s  %s:%s" % (c.get("name"), repo, tag))
        print("   本地记录 id  = %s   （规范化 %s）" % (raw_id or "-", short(raw_id)))
        print("   记录 tag     = %s   install=%s"
              % ((img or {}).get("tag") or "-", (img or {}).get("install") or 0))
        try:
            man, up_cfg, reg = A.remote_platform_digest(regs, repo, tag, ctx, cfg["timeout"])
        except Exception as e:                                   # noqa: BLE001
            print("   上游查询失败 = %s\n" % e)
            continue
        print("   manifest     = %s   （命中 %s）" % (short(man), reg))
        print("   上游 config  = %s" % short(up_cfg))
        if A.same_content(raw_id, up_cfg):
            verdict = "SAME  ← 本地就是最新内容"
        elif A.norm_digest(raw_id) and A.norm_digest(up_cfg):
            verdict = "DIFF  ← 本地落后（应当更新）"
        else:
            verdict = "UNKNOWN ← 指纹不全，无法比对"
        print("   判定         = %s\n" % verdict)

    # 额外的关键事实：爱快记录里的 id 到底长什么样（长度/前缀），决定能否比对
    ids = [str(i.get("id") or "") for i in A.get_local_images(ik)]
    full = [x for x in ids if A.norm_digest(x)]
    print("本地镜像记录共 %d 条，其中 %d 条是完整 64 位 sha256；样例：%s"
          % (len(ids), len(full), (full[0][:19] + "…") if full else
             ((ids[0][:19] + "…") if ids else "-")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
