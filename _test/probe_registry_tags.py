#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读排查：某镜像的 tag 在**加速源**上到底存不存在、层能不能下。

背景：idocker 的检测只看 manifest digest（有 digest 就算「源认识这个 tag」），
但爱快拉取时还要真的把**层 blob** 拉下来。也可能爱快在下载前会校验
tags/list 里有没有这个 tag。两者任一为假，都会表现为「爱快拉不到」。

做四件事（全部只读，只发 GET/HEAD）：
  1. 读爱快自己配置的加速源
  2. 逐个源请求 `/v2/<repo>/tags/list`，看里面有没有目标 tag
  3. 逐个源请求 manifest 本体，解析出 config + layers
  4. 对最小的那层发 Range 请求，看 blob 能否真的下下来

用法：
  python _test/probe_registry_tags.py vaultwarden/server 1.37.4
  python _test/probe_registry_tags.py vaultwarden/server latest

凭据从 D:/Documents/WorkBuddy/爱快/.env 读，绝不回显。
"""
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "webapp", "app"))

ENV = os.path.join(os.path.dirname(ROOT), ".env")

ACCEPT = ", ".join([
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
])
UA = "idocker-probe/1.0"


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


def _req(url, headers, method="GET", timeout=25):
    r = urllib.request.Request(url, headers=headers, method=method)
    return urllib.request.urlopen(r, timeout=timeout)


def get_token(reg, repo, timeout=25):
    """从 401 挑战里取 bearer token（若源要求）。无挑战返回 None。"""
    url = "%s/v2/%s/manifests/latest" % (reg.rstrip("/"), repo)
    try:
        with _req(url, {"Accept": ACCEPT, "User-Agent": UA}, timeout=timeout):
            return None
    except urllib.error.HTTPError as e:
        if e.code not in (401, 403):
            return None
        www = e.headers.get("WWW-Authenticate", "")
        if not www.lower().startswith("bearer"):
            return None
        params = {}
        for part in www[6:].split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                params[k.strip()] = v.strip().strip('"')
        realm = params.get("realm")
        if not realm:
            return None
        q = []
        if params.get("service"):
            q.append("service=" + urllib.parse.quote(params["service"]))
        if params.get("scope"):
            q.append("scope=" + urllib.parse.quote(params["scope"]))
        turl = realm + ("&" if "?" in realm else "?") + "&".join(q)
        with _req(turl, {"User-Agent": UA}, timeout=timeout) as tr:
            return (json.loads(tr.read().decode("utf-8")) or {}).get("token")


def tags_list(reg, repo, tok, timeout=25):
    url = "%s/v2/%s/tags/list" % (reg.rstrip("/"), repo)
    h = {"Accept": ACCEPT, "User-Agent": UA}
    if tok:
        h["Authorization"] = "Bearer " + tok
    with _req(url, h, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def manifest(reg, repo, tag, tok, timeout=25):
    url = "%s/v2/%s/manifests/%s" % (reg.rstrip("/"), repo, tag)
    h = {"Accept": ACCEPT, "User-Agent": UA}
    if tok:
        h["Authorization"] = "Bearer " + tok
    with _req(url, h, timeout=timeout) as r:
        raw = r.read()
        return r.headers.get("Content-Type", ""), raw


def blob_head(reg, repo, digest, tok, timeout=25):
    url = "%s/v2/%s/blobs/%s" % (reg.rstrip("/"), repo, digest)
    h = {"Accept": "*/*", "User-Agent": UA, "Range": "bytes=0-1023"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    with _req(url, h, timeout=timeout) as r:
        data = r.read(1024)
        return r.status, len(data)


def main():
    repo = sys.argv[1] if len(sys.argv) > 1 else "vaultwarden/server"
    tag = sys.argv[2] if len(sys.argv) > 2 else "1.37.4"

    vals = load_env()
    os.environ["IKUAI_URL"] = vals.get("ikuai_url", "")
    os.environ["IKUAI_USER"] = vals.get("ikuai_id", "")
    os.environ["IKUAI_PASS"] = vals.get("ikuai_pw", "")

    import ikuai_api as A

    cfg = A.load_config()
    ctx = A.Ctx(logger=lambda m, **k: None)
    ik = A.IKuai(cfg, ctx)
    ik.login()

    try:
        mirrors = A.get_mirrors(ik)
    except Exception as e:                                   # noqa: BLE001
        mirrors = []
        print("读取爱快加速源失败：%s" % e)
    print("爱快配置的加速源：%s" % (mirrors or "(无)"))
    print("目标：%s:%s\n" % (repo, tag))

    for reg in mirrors:
        print("=" * 64)
        print("源 %s" % reg)
        try:
            tok = get_token(reg, repo)
        except Exception as e:                               # noqa: BLE001
            tok = None
            print("  token 获取异常（不影响匿名源）：%s" % str(e).splitlines()[0][:70])

        # 1) tags/list
        try:
            tl = tags_list(reg, repo, tok)
            tags = tl.get("tags") or []
            has = tag in tags
            print("  tags/list：共 %d 个 tag，含 %s → %s"
                  % (len(tags), tag, "✓ 有" if has else "✗ 没有"))
            if not has and tags:
                vs = sorted([t for t in tags if t[:1].isdigit()],
                            key=lambda s: [int(x) for x in s.split(".") if x.isdigit()] or [0])
                print("    其中最大的几个版本号 tag：%s" % vs[-6:])
        except Exception as e:                               # noqa: BLE001
            print("  tags/list：✗ %s" % str(e).splitlines()[0][:70])

        # 2) manifest 本体
        try:
            ctype, raw = manifest(reg, repo, tag, tok)
            body = json.loads(raw.decode("utf-8"))
            kind = body.get("mediaType") or ctype
            if "manifests" in body:                          # 多架构索引
                kids = body["manifests"]
                print("  manifest：索引（多架构），%d 个子清单" % len(kids))
                for k in kids[:4]:
                    p = k.get("platform") or {}
                    print("    - %s %s %s" % (p.get("os"), p.get("architecture"),
                                              k.get("digest", "")[:20]))
                print("  （索引本身可下载；具体架构清单需再请求一层）")
            else:
                layers = body.get("layers") or []
                cfgd = (body.get("config") or {}).get("digest")
                total = sum(l.get("size", 0) for l in layers)
                print("  manifest：单架构，config=%s，%d 层，合计 %.1f MB"
                      % ((cfgd or "")[:20], len(layers), total / 1048576.0))
                # 3) 试下最小的一层
                if layers:
                    sm = min(layers, key=lambda l: l.get("size", 0))
                    try:
                        st, n = blob_head(reg, repo, sm["digest"], tok)
                        print("    最小层 %s (%.1f MB) → HTTP %s，读到 %d 字节 %s"
                              % (sm["digest"][:20], sm.get("size", 0) / 1048576.0,
                                 st, n, "✓ 可下" if n > 0 else "✗ 空"))
                    except Exception as e:                   # noqa: BLE001
                        print("    最小层 %s → ✗ %s"
                              % (sm["digest"][:20], str(e).splitlines()[0][:70]))
        except Exception as e:                               # noqa: BLE001
            print("  manifest：✗ %s" % str(e).splitlines()[0][:90])
    print("=" * 64)


if __name__ == "__main__":
    import urllib.parse  # noqa: E402  （get_token 里用到）
    main()
