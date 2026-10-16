#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读：验证「爱快的 tag_list 是不是来自 Docker Hub 官方 tags API」。

对比两个端点的原始顺序：
  A) https://hub.docker.com/v2/repositories/<ns>/<nm>/tags/?page_size=25
  B) https://registry-1.docker.io/v2/<ns>/<nm>/tags/list

用法：
  python _test/probe_hub_tags.py vaultwarden server
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
UA = "Mozilla/5.0 (idocker-probe)"


def get(url, timeout=25):
    req = urllib.request.Request(url, headers={"Accept": "*/*", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


cfg = A.load_config()
ctx = A.Ctx(logger=lambda m, **k: None)
ik = A.IKuai(cfg, ctx)
ik.login()

ikuai_tags = []
try:
    res = ik.call("docker_image", "show", {"TYPE": "tag", "name": nm, "namespace": ns})
    ikuai_tags = ((res.get("results") or {}).get("tag_list")) or []
except Exception as e:                                          # noqa: BLE001
    print("爱快失败：%s" % e)

print("=== 爱快给的（%d 条）===" % len(ikuai_tags))
print("  %s\n" % ikuai_tags)

candidates = [
    "https://hub.docker.com/v2/repositories/%s/%s/tags/?page_size=25" % (ns, nm),
    "https://hub.docker.com/v2/repositories/%s/%s/tags/?page_size=25&ordering=last_updated" % (ns, nm),
    "https://hub.docker.com/v2/repositories/%s/%s/tags/?page_size=10" % (ns, nm),
]
for url in candidates:
    print("=== %s ===" % url)
    try:
        st, body = get(url)
        d = json.loads(body)
        names = [t.get("name") for t in (d.get("results") or [])]
        print("  HTTP %s，results=%d，count=%s，next=%s"
              % (st, len(names), d.get("count"), d.get("next")))
        print("  前 25：%s" % names[:25])
        print("  顺序与爱快一致？ %s" % ("是" if names[:len(ikuai_tags)] == ikuai_tags else "否"))
    except Exception as e:                                      # noqa: BLE001
        print("  失败：%s" % str(e).splitlines()[0][:100])
    print()
