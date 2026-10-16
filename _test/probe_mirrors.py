#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量测镜像加速源可用性（只读）。

对每个候选源请求 /v2/vaultwarden/server/tags/list，报告：
  HTTP 状态、tag 总数、是否含 1.37.4 / latest、耗时。
跑之前请绕开本机代理（否则测的是代理）：
  env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY no_proxy='*' python …
"""
import json
import sys
import time
import urllib.error
import urllib.request

CANDIDATES = [
    "https://docker.1ms.run",
    "https://docker.m.daocloud.io",
    "https://docker.xuanyuan.me",
    "https://docker.1panel.live",
    "https://hub.rat.dev",
    "https://dockerpull.org",
    "https://docker.nju.edu.cn",
    "https://docker.rainbond.cc",
    "https://hub.littlediary.cn",
    "https://docker.ketches.cn",
    "https://docker.hlmirror.com",
    "https://dockerproxy.net",
    "https://mirror.ccs.tencentyun.com",
    "https://docker.m.daocloud.io/",
    "https://docker.1ms.run/",
]
REPO = "vaultwarden/server"
UA = "idocker-probe/1.0"

uniq = []
for c in CANDIDATES:
    if c not in uniq:
        uniq.append(c)

print("目标 repo：%s\n" % REPO)
rows = []
for reg in uniq:
    url = "%s/v2/%s/tags/list" % (reg.rstrip("/"), REPO)
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers={"Accept": "*/*", "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read().decode("utf-8", "replace")
            st = r.status
        dt = time.time() - t0
        try:
            tags = (json.loads(body).get("tags")) or []
        except Exception:                                       # noqa: BLE001
            tags = []
        ok = st == 200 and bool(tags)
        rows.append((ok, reg, st, len(tags), "1.37.4" in tags, "latest" in tags, dt))
    except urllib.error.HTTPError as e:
        rows.append((False, reg, e.code, 0, False, False, time.time() - t0))
    except Exception as e:                                      # noqa: BLE001
        rows.append((False, reg, str(e).splitlines()[0][:36], 0, False, False,
                     time.time() - t0))

print("%-38s %-6s %-6s %-8s %-8s %s" % ("源", "HTTP", "tag数", "有1.37.4", "有latest", "耗时"))
print("-" * 92)
for ok, reg, st, n, h4, hl, dt in sorted(rows, key=lambda x: (not x[0], x[6])):
    mark = "✓" if ok else " "
    print("%s %-36s %-6s %-6s %-8s %-8s %.2fs"
          % (mark, reg, st, n if n else "-", h4 if n else "-", hl if n else "-", dt))

good = [r[1] for r in rows if r[0]]
print()
print("可用源（%d 个）：%s" % (len(good), good))
