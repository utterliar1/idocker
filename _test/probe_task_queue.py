#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查看（并可操作）爱快的 task_queue —— 镜像下载就在这个队列里跑。

队列里的 **失败任务**（status=-1 / errexit=1）会在「本地镜像」页留下一行
**下载失败** 的红色记录；那条记录的 `id` 往往是 `0`，于是前端点「删除」时
`docker_image.del {id:0,…}` 匹配不到任何东西、**静默成功但行不消失**。
本脚本可以改从**任务侧**清掉它。

用法：
    python _test/probe_task_queue.py                        # 只读：按多种形状列出队列
    python _test/probe_task_queue.py --del    <task_id>     # 只读预览要提交的参数
    python _test/probe_task_queue.py --del    <task_id> --go    # 真删该任务
    python _test/probe_task_queue.py --abort  <task_id> --go    # 中止
    python _test/probe_task_queue.py --retry  <task_id> --go    # 重试
"""
import base64
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "webapp", "app"))
ENV = os.path.join(os.path.dirname(ROOT), ".env")

vals = {}
with open(ENV, "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()

os.environ["IKUAI_URL"] = vals.get("ikuai_url", "")
os.environ["IKUAI_USER"] = vals.get("ikuai_id", "")
os.environ["IKUAI_PASS"] = vals.get("ikuai_pw", "")

import ikuai_api as A  # noqa: E402

DO = "--go" in sys.argv


def argval(flag):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else None


ACTION = next((a for a in ("--del", "--abort", "--retry") if a in sys.argv), None)
TASK_ID = argval(ACTION) if ACTION else None

cfg = A.load_config()
ctx = A.Ctx(logger=lambda m, **k: None)
ik = A.IKuai(cfg, ctx)
ik.login()


def decode_params(p):
    try:
        return base64.b64decode(p).decode("utf-8", "replace")
    except Exception:                                            # noqa: BLE001
        return "(解码失败)"


def list_queue():
    print("=== task_queue.show {TYPE:total,data} ===")
    r = ik.call("task_queue", "show", {"TYPE": "total,data"})
    data = (((r or {}).get("results") or {}).get("data")) or []
    print("  total = %s" % (((r or {}).get("results") or {}).get("total")))
    for t in data:
        print("  ----")
        print("  task_id=%s  name=%s  module=%s" % (t.get("task_id"), t.get("task_name"), t.get("module_name")))
        print("  status=%s  install_status=%s  errexit=%s  is_run=%s  progress=%s  ts=%s"
              % (t.get("status"), t.get("install_status"), t.get("errexit"),
                 t.get("is_run"), t.get("progress"), t.get("timestamp")))
        print("  params(解码)=%s" % decode_params(t.get("task_params")))
    return data


list_queue()

if ACTION:
    payload = {"task_id": TASK_ID}
    verb = ACTION.lstrip("-")
    print()
    print("=== 将调用 task_queue.%s %s ===" % (verb, json.dumps(payload, ensure_ascii=False)))
    if not DO:
        print("  （只读模式。加 --go 才真的执行。）")
        sys.exit(0)
    try:
        res = ik.call("task_queue", verb, payload)
        print("  返回 = %s" % json.dumps(res, ensure_ascii=False)[:600])
    except Exception as e:                                        # noqa: BLE001
        print("  抛异常 = %s" % e)
    print()
    list_queue()
