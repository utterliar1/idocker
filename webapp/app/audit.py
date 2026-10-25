#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""操作审计：谁在什么时候对哪个容器/镜像/网络做了什么。

为什么要单独记一份：更新流程有 history.json，但「删了个容器」「停了个服务」
这类一次性动作以前只留在 docker logs 里，出事之后没人翻得到。管理台把这些
动作都落盘成一行，事后能回答「昨天谁把 clash 停了」。

落盘在 ``/data/audit.json``，最多保留 ``AUDIT_LIMIT`` 条（越新越靠前）。
"""

import json
import os
import re
import threading
import time

AUDIT_LIMIT = 300
_SECRET_KEY = re.compile(r"(pass|passwd|password|token|secret|key|cookie)", re.I)


def mask_params(obj, depth=0):
    """把疑似凭据的字段打码 —— 审计记录不该成为第二份密码本。"""
    if depth > 4:
        return obj
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _SECRET_KEY.search(str(k)):
                out[k] = "***"
            elif str(k) == "env" and isinstance(v, str):
                # "A=1,B=2" → "A=***,B=***"：容器环境变量里十有八九有 token
                out[k] = ",".join((part.split("=")[0] + "=***")
                                  for part in v.split(",") if part.strip())
            else:
                out[k] = mask_params(v, depth + 1)
        return out
    if isinstance(obj, list):
        return [mask_params(x, depth + 1) for x in obj]
    return obj


class Audit(object):
    def __init__(self, path, limit=AUDIT_LIMIT):
        self.path = path
        self.limit = limit
        self._lock = threading.Lock()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:                                     # noqa: BLE001
            return []

    def add(self, action, target="", detail="", ok=True, params=None, user=""):
        row = {
            "ts": time.time(),
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "action": str(action),
            "target": str(target),
            "detail": str(detail)[:300],
            "ok": bool(ok),
            "params": mask_params(params or {}),
            "user": str(user or ""),
        }
        with self._lock:
            rows = self._load()
            rows.insert(0, row)
            del rows[self.limit:]
            try:
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(rows, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)
            except Exception:                                 # noqa: BLE001
                pass
        return row

    def recent(self, limit=50):
        with self._lock:
            return self._load()[:limit]
