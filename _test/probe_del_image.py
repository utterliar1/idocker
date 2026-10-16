#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查看 / 删除爱快上的「本地镜像记录」（含下载失败残留）。

两件实测出来的爱快怪癖，本脚本就是来绕开的：

1. **`docker_image.del` 只认「完整 repo 名 + 真实 tag」，且不看 `id`。**
   `{id:0,name:"gdy666/lucky",tag:"v3"}` 会回 `{"code":0,"message":"Success"}`
   却**一字不改**（静默空操作）。必须写成 `{name:"gdy666/lucky",tag:"<真实tag>"}`。

2. **失败记录在清单里显示的 tag 会被「截到第一个下划线为止」。**
   实测：真实 tag `v3_xiaojv_waf` → 清单显示 `v3`；`zzprobe_isolate_v9` → 显示 `zzprobe`。
   而 `del` 要的是**未截断的真实 tag**。真实 tag 只能从 `task_queue` 的
   `task_params`（base64）里解出来 —— 也就是本脚本 `--from-task` 干的事。

**注意**：`task_queue` 里那条失败任务**被删掉不会**让镜像记录消失（实测：队列清空、记录仍在）。
所以真实 tag 一旦丢失（队列里的任务也没了）就只能靠你自己记着当初填的是什么。

**安全性**：docker 的 rmi 不会停 / 删正在运行的容器；del 匹配不到就是空操作，最坏情况什么也没变。
依旧先打印「删前 / 删后」对照。

用法：
    python _test/probe_del_image.py                                  # 列出全部本地镜像记录（只读）
    python _test/probe_del_image.py --name gdy666/lucky              # 只看该 repo
    python _test/probe_del_image.py --name gdy666/lucky --only-failed --from-task          # 预览
    python _test/probe_del_image.py --name gdy666/lucky --only-failed --from-task --go     # 真删
    python _test/probe_del_image.py --name gdy666/lucky --id 0 --tag v3_xiaojv_waf --go    # 手动指定 tag
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
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        vals[k.strip()] = v.strip()

os.environ["IKUAI_URL"] = vals.get("ikuai_url", "")
os.environ["IKUAI_USER"] = vals.get("ikuai_id", "")
os.environ["IKUAI_PASS"] = vals.get("ikuai_pw", "")

import ikuai_api as A  # noqa: E402

# status 图例（核自前端 bundle）：-1 失败 / 0 已下载 / 1 下载中 / 2 任务列表 / 3 排队 / 4,5 被占用
_STATUS = ["download_fail", "", "downloading", "task_list", "waiting", "", ""]


def status_label(s):
    try:
        return _STATUS[int(s) + 1] or str(s)
    except (TypeError, ValueError, IndexError):
        return str(s)


def argval(flag):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else None


DO = "--go" in sys.argv
WANT_NAME = argval("--name")
WANT_ID = argval("--id")
TAG_OVERRIDE = argval("--tag")
ONLY_FAILED = "--only-failed" in sys.argv
FROM_TASK = "--from-task" in sys.argv

ik = A.IKuai(A.load_config(), A.Ctx(logger=lambda m, **k: None))
ik.login()


def decode_params(p):
    try:
        return base64.b64decode(p).decode("utf-8", "replace")
    except Exception:                                            # noqa: BLE001
        return ""


def task_tags():
    """{repo 全名: 真实 tag}，取自 task_queue 里**失败**任务解码后的 params。"""
    out = {}
    try:
        r = ik.call("task_queue", "show", {"TYPE": "total,data"})
    except Exception:                                            # noqa: BLE001
        return out
    for t in (((r or {}).get("results") or {}).get("data")) or []:
        if int(t.get("status") or 0) >= 0:
            continue
        kv = dict(p.split("=", 1) for p in decode_params(t.get("task_params")).split()
                  if "=" in p)
        ns, name, tag = kv.get("namespace", ""), kv.get("name", ""), kv.get("tag", "")
        if not (ns and name):
            continue
        out["%s/%s" % (ns, name) if ns != "library" else name] = tag
    return out


def pick():
    rows = []
    for im in A.get_local_images(ik):
        if WANT_NAME and im.get("name") != WANT_NAME:
            continue
        if WANT_ID and str(im.get("id")) != str(WANT_ID):
            continue
        if ONLY_FAILED and int(im.get("status") or 0) >= 0:
            continue
        rows.append(im)
    return rows


def dump(rows):
    print("=== 本地镜像记录 ===")
    for im in rows:
        cs = im.get("containers")
        if isinstance(cs, dict):
            cs = list(cs.values())
        used = [c.get("name") for c in (cs or []) if isinstance(c, dict)]
        print("  id=%-4s name=%-22s tag=%-16s status=%-16s size=%-10s containers=%s"
              % (im.get("id"), im.get("name"), im.get("tag"),
                 "%s(%s)" % (status_label(im.get("status")), im.get("status")),
                 im.get("size"), used or "-"))
    if not rows:
        print("  （没有匹配的记录）")
    print("  共 %d 条" % len(rows))


rows = pick()
dump(rows)
if not rows:
    sys.exit(0)

# 组装每个待删记录的「真实 tag」
tags = task_tags() if FROM_TASK else {}
plan = []
for im in rows:
    real = TAG_OVERRIDE or tags.get(im.get("name")) or im.get("tag")
    src = ("--tag" if TAG_OVERRIDE else
           "task_queue" if (FROM_TASK and tags.get(im.get("name"))) else "清单(可能被截断)")
    plan.append((im, real, src))

print()
print("=== 计划提交的 del 参数（tag 来源见括号）===")
for im, real, src in plan:
    print("  %s" % json.dumps({"name": im.get("name"), "tag": real}, ensure_ascii=False)
          + "   ← tag 来源：%s" % src)
    if src.startswith("清单") and im.get("tag") and "_" not in str(im.get("tag")):
        pass  # 无下划线时清单 tag 通常就是真实 tag
if any(s.startswith("清单") for _, _, s in plan):
    print("  ⚠️ 有记录的 tag 取自清单（可能被截断到第一个下划线）。若 del 无效，")
    print("     用 --from-task（需失败任务还在 task_queue 里）或 --tag <真实tag> 重试。")

if not DO:
    print()
    print("（只读模式。加 --go 才真的删。）")
    sys.exit(0)

print()
print("=== 提交 docker_image.del ===")
for im, real, src in plan:
    payload = {"name": im.get("name"), "tag": real}       # 关键：完整名 + 真实 tag，不带 id
    print("  param = %s" % json.dumps(payload, ensure_ascii=False))
    try:
        res = ik.call("docker_image", "del", payload)
        print("  返回 = %s" % json.dumps(res, ensure_ascii=False)[:300])
    except Exception as e:                                        # noqa: BLE001
        print("  抛异常 = %s" % e)

print()
dump(pick())
