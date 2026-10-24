#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务结束通知：把结果推到外部，不用打开页面也能知道。

为什么要有：定时任务多在凌晨跑。失败了没人看见，等下次打开页面才发现
「三天前就坏了」—— 而定时更新这件事的价值全在「不用管」。

渠道全部从环境变量读，**不落盘、不下发到前端**（页面只看得到「配了哪些渠道」）：

  ``NOTIFY_WEBHOOK_URL``     通用 webhook，POST 一段 JSON
  ``NOTIFY_WEBHOOK_TOKEN``   可选，放在 ``X-Notify-Token`` 头里
  ``NOTIFY_PUSHPLUS_TOKEN``  pushplus 的 token（微信推送）
  ``NOTIFY_ON``              ``off`` / ``fail`` / ``always``，默认 ``fail``

页面上的「通知」选项存 ``settings.json`` 的 ``notify_mode``，优先级高于环境变量 ——
和「下载超时」是同一套规则。

设计上的两个取舍：

  · 通知**异步发**（后台线程）。webhook 那头慢或挂掉，绝不能拖住任务收尾。
  · 干跑任务不发通知。它没碰任何容器，发出来只是噪音。
"""

import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request

NOTIFY_MODES = ("off", "fail", "always")
DEFAULT_MODE = "fail"
SEND_TIMEOUT = 8                     # 单个渠道的超时（秒）
MAX_TEXT = 1500                      # 推送正文上限，别把渠道打爆


def _say(message):
    """通知失败只写 stderr（docker logs 能看到），绝不抛给调用方。"""
    try:
        sys.stderr.write("[idocker:notify] %s\n" % message)
        sys.stderr.flush()
    except Exception:                                         # noqa: BLE001
        pass


def _env(name):
    return (os.environ.get(name) or "").strip()


def _host_of(url):
    """只暴露主机名给前端 —— 完整 webhook URL 里常带 token，不能下发。"""
    try:
        p = urllib.parse.urlparse(url)
        return p.hostname or "webhook"
    except Exception:                                         # noqa: BLE001
        return "webhook"


def channels():
    """当前配置了哪些渠道（给页面展示用，不含任何密钥）。"""
    out = []
    url = _env("NOTIFY_WEBHOOK_URL")
    if url:
        out.append({"kind": "webhook", "label": "Webhook · " + _host_of(url)})
    if _env("NOTIFY_PUSHPLUS_TOKEN"):
        out.append({"kind": "pushplus", "label": "PushPlus · 微信"})
    return out


def default_mode():
    mode = (_env("NOTIFY_ON") or DEFAULT_MODE).lower()
    return mode if mode in NOTIFY_MODES else DEFAULT_MODE


def effective_mode(store):
    """页面设置优先，没设置过就用环境变量的默认值。"""
    mode = ""
    try:
        mode = str((store.public() or {}).get("notify_mode") or "")
    except Exception:                                         # noqa: BLE001
        mode = ""
    return mode if mode in NOTIFY_MODES else default_mode()


def _schedule_name(store, sid):
    if not sid:
        return ""
    try:
        item = store.get_schedule(sid) or {}
    except Exception:                                         # noqa: BLE001
        return ""
    return str(item.get("name") or "")


def build_message(job, store, version=""):
    """把任务结果整理成 (title, text)。纯文本，渠道各自渲染。"""
    ok = bool(getattr(job, "ok", False))
    kind = "更新" if getattr(job, "kind", "") == "update" else "版本检测"
    targets = list(getattr(job, "targets", []) or [])
    who = "、".join(targets) if targets else "全部容器"
    when = time.strftime("%Y-%m-%d %H:%M:%S")

    title = "%s idocker %s%s" % ("✅" if ok else "❌", kind,
                                 ("：" + who) if targets else "")
    if len(title) > 60:
        title = title[:57] + "…"

    lines = ["%s %s" % ("成功" if ok else "失败", kind), "容器：" + who, "时间：" + when]
    sched = _schedule_name(store, getattr(job, "schedule_id", ""))
    if sched:
        lines.append("定时任务：" + sched)
    if getattr(job, "auto", False):
        lines.append("触发方式：定时任务自动执行")
    msg = str(getattr(job, "final_message", "") or "").strip()
    if msg:
        lines.append("结果：" + msg)
    # 失败的步骤单独点出来 —— 这是「半夜挂了到底挂在哪」的唯一线索
    for s in getattr(job, "steps", []) or []:
        if s.get("state") == "failed":
            lines.append("失败步骤：%s %s" % (s.get("name") or s.get("key"),
                                            s.get("message") or ""))
    if getattr(job, "dry", None):
        lines.append("（干跑模式，未改动任何容器）")
    if version:
        lines.append("版本：v" + str(version))
    text = "\n".join(lines)
    return title, text[:MAX_TEXT]


def _post_json(url, payload, headers=None, timeout=SEND_TIMEOUT):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()


def send(title, text, payload=None):
    """真正发出去。返回 [(渠道, 是否成功, 说明)]，供日志使用。"""
    results = []
    url = _env("NOTIFY_WEBHOOK_URL")
    if url:
        try:
            headers = {}
            token = _env("NOTIFY_WEBHOOK_TOKEN")
            if token:
                headers["X-Notify-Token"] = token
            body = dict(payload or {})
            body.update({"source": "idocker", "title": title, "text": text})
            _post_json(url, body, headers)
            results.append(("webhook", True, ""))
        except Exception as e:                                # noqa: BLE001
            results.append(("webhook", False, str(e)))
    token = _env("NOTIFY_PUSHPLUS_TOKEN")
    if token:
        try:
            _post_json("https://www.pushplus.plus/send",
                       {"token": token, "title": title, "content": text,
                        "template": "txt"})
            results.append(("pushplus", True, ""))
        except Exception as e:                                # noqa: BLE001
            results.append(("pushplus", False, str(e)))
    return results


def notify_job(job, store, version="", wait=False):
    """任务结束后调用。异步发送，绝不阻塞任务收尾。

    ``wait=True`` 仅供测试：同步发完再返回。
    """
    try:
        if not channels():
            return False
        if getattr(job, "dry", None):          # 干跑没碰容器，不发噪音
            return False
        mode = effective_mode(store)
        if mode == "off":
            return False
        if mode == "fail" and getattr(job, "ok", False):
            return False
        title, text = build_message(job, store, version)
        payload = {
            "ok": bool(getattr(job, "ok", False)),
            "kind": getattr(job, "kind", ""),
            "targets": list(getattr(job, "targets", []) or []),
            "message": str(getattr(job, "final_message", "") or ""),
            "job_id": getattr(job, "id", ""),
            "auto": bool(getattr(job, "auto", False)),
            "schedule": _schedule_name(store, getattr(job, "schedule_id", "")),
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "version": str(version or ""),
        }
    except Exception as e:                                    # noqa: BLE001
        _say("准备通知失败：%s" % e)
        return False

    def run():
        for ch, ok, note in send(title, text, payload):
            if not ok:
                _say("%s 推送失败：%s" % (ch, note))

    if wait:
        run()
        return True
    threading.Thread(target=run, name="idocker-notify", daemon=True).start()
    return True
