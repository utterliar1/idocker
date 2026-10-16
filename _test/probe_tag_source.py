#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读对比：三个加速源的 tags/list 原始顺序 vs 爱快自己给的 tag_list。

目的：定位爱快的 tag_list 到底来自哪个源（这样才能知道该换哪个源才能拿到 1.37.4）。

用法：
  python _test/probe_tag_source.py vaultwarden server
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

ns = sys.argv[1] if len(sys.argv) > 1 else "vaultwarden"
nm = sys.argv[2] if len(sys.argv) > 2 else "server"
repo = "%s/%s" % (ns, nm)

UA = "idocker-probe/1.0"


def get(url, timeout=25):
    req = urllib.request.Request(url, headers={"Accept": "*/*", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


cfg = A.load_config()
ctx = A.Ctx(logger=lambda m, **k: None)
ik = A.IKuai(cfg, ctx)
ik.login()
mirrors = A.get_mirrors(ik)

# 爱快自己给的
ikuai_tags = []
try:
    res = ik.call("docker_image", "show", {"TYPE": "tag", "name": nm, "namespace": ns})
    ikuai_tags = ((res.get("results") or {}).get("tag_list")) or []
except Exception as e:                                          # noqa: BLE001
    print("爱快 tag_list 失败：%s" % e)

print("=== 爱快自己给的 tag_list（%d 条）===" % len(ikuai_tags))
print("  %s" % ikuai_tags)
print()

for reg in mirrors:
    print("=== %s/v2/%s/tags/list ===" % (reg.rstrip("/"), repo))
    try:
        st, body = get("%s/v2/%s/tags/list" % (reg.rstrip("/"), repo))
        print("  HTTP %s，长度 %d" % (st, len(body)))
        try:
            d = json.loads(body)
            tags = d.get("tags") or []
            print("  共 %d 个 tag；原始前 30 条：" % len(tags))
            print("  %s" % tags[:30])
            print("  含 1.37.4 → %s ；含 latest → %s"
                  % ("1.37.4" in tags, "latest" in tags))
        except Exception:                                       # noqa: BLE001
            print("  （非 JSON）前 300 字：%s" % body[:300])
    except Exception as e:                                      # noqa: BLE001
        print("  失败：%s" % str(e).splitlines()[0][:100])
    print()
