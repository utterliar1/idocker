#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读：逐层探测镜像站是否真的持有该 tag 的**全部 blob**。

动机：镜像站常有「元数据在、blob 没缓存」的情况。爱快拉取会因某个层拿不到而
status=-1（下载失败），但只查 manifest 是看不出来的。

用法：
  python _test/probe_blobs.py vaultwarden/server 1.37.4
  python _test/probe_blobs.py vaultwarden/server 1.37.3   # 对照：本地已有的那版
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
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
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip()
    return vals


def _req(url, headers, timeout=30):
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                  timeout=timeout)


def token_for(reg, repo):
    url = "%s/v2/%s/manifests/latest" % (reg.rstrip("/"), repo)
    try:
        with _req(url, {"Accept": ACCEPT, "User-Agent": UA}):
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
        with _req(turl, {"User-Agent": UA}) as tr:
            return (json.loads(tr.read().decode("utf-8")) or {}).get("token")


def mf(reg, repo, ref, tok):
    url = "%s/v2/%s/manifests/%s" % (reg.rstrip("/"), repo, ref)
    h = {"Accept": ACCEPT, "User-Agent": UA}
    if tok:
        h["Authorization"] = "Bearer " + tok
    with _req(url, h) as r:
        return json.loads(r.read().decode("utf-8"))


def head_blob(reg, repo, digest, tok):
    url = "%s/v2/%s/blobs/%s" % (reg.rstrip("/"), repo, digest)
    h = {"Accept": "*/*", "User-Agent": UA, "Range": "bytes=0-4095"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    t0 = time.time()
    with _req(url, h, timeout=45) as r:
        r.read(4096)
        return r.status, time.time() - t0


vals = load_env()
os.environ["IKUAI_URL"] = vals.get("ikuai_url", "")
os.environ["IKUAI_USER"] = vals.get("ikuai_id", "")
os.environ["IKUAI_PASS"] = vals.get("ikuai_pw", "")

import ikuai_api as A  # noqa: E402

repo = sys.argv[1] if len(sys.argv) > 1 else "vaultwarden/server"
tag = sys.argv[2] if len(sys.argv) > 2 else "1.37.4"

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
        idx = mf(reg, repo, tag, tok)
    except Exception as e:                                   # noqa: BLE001
        print("  索引取不到：%s" % str(e).splitlines()[0][:80])
        continue
    kids = idx.get("manifests") or []
    sub = idx
    if kids:
        pick = next((k for k in kids
                     if (k.get("platform") or {}).get("os") == "linux"
                     and (k.get("platform") or {}).get("architecture") == "amd64"), None)
        if not pick:
            print("  无 linux/amd64")
            continue
        try:
            sub = mf(reg, repo, pick["digest"], tok)
        except Exception as e:                               # noqa: BLE001
            print("  子清单取不到：%s" % str(e).splitlines()[0][:80])
            continue
    layers = (sub.get("layers") or [])
    cfg_blob = (sub.get("config") or {}).get("digest")
    print("  共 %d 层 + 1 config" % len(layers))
    bad = 0
    for i, l in enumerate(sorted(layers, key=lambda x: -x.get("size", 0))[:6]):
        try:
            st, dt = head_blob(reg, repo, l["digest"], tok)
            ok = st in (200, 206)
            if not ok:
                bad += 1
            print("    #%d %8.1f MB  %s  HTTP %s  %.2fs"
                  % (i, l.get("size", 0) / 1048576.0, l["digest"][:20],
                     st, dt))
        except urllib.error.HTTPError as e:
            bad += 1
            print("    #%d %8.1f MB  %s  ✗ HTTP %s"
                  % (i, l.get("size", 0) / 1048576.0, l["digest"][:20], e.code))
        except Exception as e:                               # noqa: BLE001
            bad += 1
            print("    #%d %8.1f MB  ✗ %s"
                  % (i, l.get("size", 0) / 1048576.0, str(e).splitlines()[0][:60]))
    if cfg_blob:
        try:
            st, dt = head_blob(reg, repo, cfg_blob, tok)
            print("    config %s  HTTP %s  %.2fs" % (cfg_blob[:20], st, dt))
        except Exception as e:                               # noqa: BLE001
            print("    config ✗ %s" % str(e).splitlines()[0][:60])
    print("  → 上面 %s" % ("存在拿不到的层 ⚠" if bad else "最大的几层都能拿到 ✓"))
