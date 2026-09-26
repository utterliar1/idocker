#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""调试：直接跑一次更新任务并把事件流打出来，看它卡在哪一步。"""
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "webapp", "app")
sys.path.insert(0, HERE)
sys.path.insert(0, APP)

import mock_router                                     # noqa: E402

_srv, PORT = mock_router.serve(0)
DATA = tempfile.mkdtemp(prefix="ikuai-dbg-")
os.environ.update({
    "IKUAI_URL": "http://127.0.0.1:%d" % PORT,
    "IKUAI_USER": "admin", "IKUAI_PASS": "x",
    "REGISTRIES": "http://127.0.0.1:%d" % PORT,
    "DATA_DIR": DATA, "PULL_TIMEOUT": "60", "HTTP_TIMEOUT": "8",
})
import server                                          # noqa: E402

job = server.MANAGER.create("update", ["clash"], None, auto=True)
t0 = time.time()
seen = 0
while time.time() - t0 < 25:
    evs = job.events[seen:]
    for e in evs:
        msg = (e.get("message") or e.get("state") or "")
        print("%6.2fs %-8s %s" % (time.time() - t0, e.get("type"), msg))
    seen = len(job.events)
    if job.finished:
        break
    time.sleep(0.2)
print("finished=%s ok=%s msg=%s  (%.1fs)" % (job.finished, job.ok, job.final_message, time.time() - t0))
print("steps:", {s["key"]: s["state"] for s in job.steps})
