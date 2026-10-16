#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读排查（第二层）：manifest 是**多架构索引**时，子清单与层能不能真的下下来。

背景：probe_registry_tags.py 只看到「索引本身」可下载。爱快实际拉取要：
  索引 → linux/amd64 子清单 → 各层 blob。
任一层拿不到，表现就是「拉不动」。

用法：
  python _test/probe_registry_deep.py vaultwarden/server 1.37.4
"""
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


def _req(url, headers, timeout=30):
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                  timeout=timeout)


def token_for(reg, repo, timeout=30):
    import urllib.parse
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
        p = {}
        for part in www[6:].split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                p[k.strip()] = v.strip().strip('"')
        q = []
        if p.get("service"):
            q.append("service=" + urllib.parse.quote(p["service"]))
        if p.get("scope"):
            q.append("scope=" + urllib.parse.quote(p["scope"]))
        turl = p["realm"] + ("&" if "?" in p["realm"] else "?") + "&".join(q)
        with _req(turl, {"User-Agent": UA}, timeout=timeout) as tr:
            return (json.loads(tr.read().decode("utf-8")) or {}).get("token")


def fetch_manifest(reg, repo, ref, tok, timeout=30):
    url = "%s/v2/%s/manifests/%s" % (reg.rstrip("/"), repo, ref)
    h = {"Accept": ACCEPT, "User-Agent": UA}
    if tok:
        h["Authorization"] = "Bearer " + tok
    with _req(url, h, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def try_blob(reg, repo, digest, size, tok, timeout=40):
    url = "%s/v2/%s/blobs/%s" % (reg.rstrip("/"), repo, digest)
    h = {"Accept": "*/*", "User-Agent": UA, "Range": "bytes=0-2047"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    with _req(url, h, timeout=timeout) as r:
        n = len(r.read(2048))
        return r.status, n


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
    mirrors = A.get_mirrors(ik)
    print("加速源：%s   目标：%s:%s\n" % (mirrors, repo, tag))

    for reg in mirrors:
        print("=" * 64)
        print("源 %s" % reg)
        try:
            tok = token_for(reg, repo)
        except Exception as e:                               # noqa: BLE001
            tok = None
            print("  token：%s" % str(e).splitlines()[0][:60])
        try:
            idx = fetch_manifest(reg, repo, tag, tok)
        except Exception as e:                               # noqa: BLE001
            print("  索引：✗ %s" % str(e).splitlines()[0][:80])
            continue
        kids = idx.get("manifests")
        if not kids:
            print("  非索引（单清单），跳过")
            continue
        pick = None
        for k in kids:
            p = k.get("platform") or {}
            if p.get("os") == "linux" and p.get("architecture") == "amd64":
                pick = k
                break
        if not pick:
            print("  索引里没有 linux/amd64！")
            continue
        print("  linux/amd64 子清单 %s (%.1f MB)" % (pick["digest"][:24],
              pick.get("size", 0) / 1048576.0))
        try:
            sub = fetch_manifest(reg, repo, pick["digest"], tok)
        except Exception as e:                               # noqa: BLE001
            print("    子清单：✗ %s" % str(e).splitlines()[0][:80])
            continue
        layers = sub.get("layers") or []
        cfgd = (sub.get("config") or {}).get("digest")
        print("    子清单 OK：config=%s，%d 层" % ((cfgd or "")[:20], len(layers)))
        # config blob
        if cfgd:
            try:
                st, n = try_blob(reg, repo, cfgd, 0, tok)
                print("    config blob → HTTP %s / %d 字节 %s"
                      % (st, n, "✓" if n > 0 else "✗"))
            except Exception as e:                           # noqa: BLE001
                print("    config blob → ✗ %s" % str(e).splitlines()[0][:70])
        # 最小层
        if layers:
            sm = min(layers, key=lambda l: l.get("size", 0))
            try:
                st, n = try_blob(reg, repo, sm["digest"], sm.get("size", 0), tok)
                print("    最小层 %.1fMB → HTTP %s / %d 字节 %s"
                      % (sm.get("size", 0) / 1048576.0, st, n,
                         "✓ 可下" if n > 0 else "✗ 空"))
            except Exception as e:                           # noqa: BLE001
                print("    最小层 → ✗ %s" % str(e).splitlines()[0][:70])


if __name__ == "__main__":
    main()
