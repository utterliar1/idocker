#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""设置与定时任务的持久化存储。

所有内容放在 ``/data/settings.json``（Docker 卷），重新构建镜像不会丢。
涉及两块：

  ``pull_timeout``  等待镜像下载完成的最长秒数。
                    它同时可以被环境变量 PULL_TIMEOUT 给默认值，
                    但**页面上改过的值优先级更高**（因为页面改动会落盘）。

  ``schedules``     定时任务列表。每条任务描述「多久跑一次」「跑哪些容器」
                    「只检测还是直接更新」。

关于「多久跑一次」，只用两个字段表达，避免引入 cron 解析：

  ``every`` > 0  → 每 N 分钟
  ``every`` = 0  → 每天 ``at`` 时刻（HH:MM，本机时区）

``next_run`` 是**绝对时间戳**，由调度器每次触发后重新推算。
这样即使容器重启，下一次触发时间也不会漂移成「重启后 N 分钟」。
"""

import json
import os
import re
import threading
import time
import uuid

# 下载等待时长的允许范围。上限给到 2 小时，足够拉几个 GB 的大镜像。
MIN_PULL_TIMEOUT = 60
MAX_PULL_TIMEOUT = 7200
# 兜底默认值。真正生效的默认来自环境变量 PULL_TIMEOUT（见 ikuai_api.load_config），
# 这里只在两者都没有时才用得上。
DEFAULT_PULL_TIMEOUT = 900
# 前端快捷按钮用的推荐值
SUGGESTED_TIMEOUTS = [600, 1800, 3600]

SCHEDULE_MODES = ("check", "update")     # 仅检测 / 自动更新
MAX_SCHEDULES = 30

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{1,2})$")


def clamp_timeout(value, fallback=DEFAULT_PULL_TIMEOUT):
    """把用户填的秒数收进合法区间。非数字一律回退到默认值。"""
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return fallback
    if n <= 0:
        return fallback
    return max(MIN_PULL_TIMEOUT, min(MAX_PULL_TIMEOUT, n))


def next_run_after(item, now=None):
    """算出一条任务的下次运行时刻（时间戳）。

    注意：这是在「本次触发之后」调用的，所以「每天 HH:MM」要取**下一个**
    该时刻（今天已过就顺延到明天），否则会出现补跑风暴。
    """
    now = time.time() if now is None else now
    try:
        every = int(item.get("every") or 0)
    except (TypeError, ValueError):
        every = 0
    if every > 0:
        return now + every * 60
    m = _TIME_RE.match(str(item.get("at") or "").strip())
    if not m:
        return now + 86400
    hh = max(0, min(23, int(m.group(1))))
    mm = max(0, min(59, int(m.group(2))))
    lt = time.localtime(now)
    target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))
    if target <= now:                 # 今天这个点已经过了 -> 顺延到明天
        target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, hh, mm, 0, 0, 0, -1))
    return target


def describe_frequency(item):
    """给前端展示用的频率文案。"""
    try:
        every = int(item.get("every") or 0)
    except (TypeError, ValueError):
        every = 0
    if every <= 0:
        return "每天 " + str(item.get("at") or "03:30")
    if every % 1440 == 0:
        return "每 %d 天" % (every // 1440)
    if every % 60 == 0:
        return "每 %d 小时" % (every // 60)
    return "每 %d 分钟" % every


class Store(object):
    def __init__(self, path, default_timeout=DEFAULT_PULL_TIMEOUT):
        self.path = path
        # 页面上没设置过时用环境变量给的默认值 —— 否则用户明明在 compose 里
        # 写了 PULL_TIMEOUT，却会被这里的硬编码默认值悄悄盖掉。
        self.default_timeout = clamp_timeout(default_timeout)
        self._lock = threading.RLock()
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        except Exception:
            pass
        self.data = self._load()

    # -- 读写 ------------------------------------------------------
    def _load(self):
        raw = {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raw = {}
        except Exception:
            raw = {}
        data = {
            "pull_timeout": clamp_timeout(raw.get("pull_timeout"), self.default_timeout),
            "schedules": [],
        }
        for it in raw.get("schedules") or []:
            if isinstance(it, dict):
                data["schedules"].append(self._normalize(it))
        return data

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            return True
        except Exception as e:
            try:
                import sys
                sys.stderr.write("[idocker] 设置写入失败（%s）：%s\n" % (self.path, e))
                sys.stderr.flush()
            except Exception:
                pass
            raise OSError("设置写入失败：%s" % e) from e

    @staticmethod
    def _normalize(it):
        every = 0
        try:
            every = max(0, int(it.get("every") or 0))
        except (TypeError, ValueError):
            every = 0
        at = str(it.get("at") or "03:30").strip()
        if not _TIME_RE.match(at):
            at = "03:30"
        targets = [str(t).strip() for t in (it.get("targets") or []) if str(t).strip()]
        return {
            "id": str(it.get("id") or uuid.uuid4().hex[:8]),
            "name": (str(it.get("name") or "").strip() or "未命名任务")[:40],
            "enabled": bool(it.get("enabled", True)),
            "mode": it.get("mode") if it.get("mode") in SCHEDULE_MODES else "check",
            "targets": targets,
            "tag": str(it.get("tag") or "").strip(),
            "every": every,
            "at": at,
            "next_run": float(it.get("next_run") or 0),
            "last_run": float(it.get("last_run") or 0),
            "last_status": str(it.get("last_status") or ""),
            "last_message": str(it.get("last_message") or ""),
            "last_job": str(it.get("last_job") or ""),
        }

    # -- 超时 ------------------------------------------------------
    def get_pull_timeout(self):
        with self._lock:
            return int(self.data.get("pull_timeout") or self.default_timeout)

    def set_pull_timeout(self, value):
        with self._lock:
            self.data["pull_timeout"] = clamp_timeout(value)
            self._save()
            return self.data["pull_timeout"]

    # -- 定时任务 --------------------------------------------------
    def schedules(self):
        with self._lock:
            return [dict(x) for x in self.data.get("schedules") or []]

    def get_schedule(self, sid):
        with self._lock:
            for it in self.data.get("schedules") or []:
                if it["id"] == sid:
                    return dict(it)
        return None

    def save_schedule(self, payload):
        """新增（无 id / id 不存在）或更新。返回保存后的任务。"""
        with self._lock:
            items = self.data.setdefault("schedules", [])
            sid = str(payload.get("id") or "").strip()
            old = None
            if sid:
                for it in items:
                    if it["id"] == sid:
                        old = it
                        break
            item = self._normalize(payload)
            if old is not None:
                item["id"] = old["id"]
                # 时间相关的运行时状态必须沿用，不能让用户改个名字就把排期重置
                item["next_run"] = old.get("next_run") or 0
                item["last_run"] = old.get("last_run") or 0
                item["last_status"] = old.get("last_status") or ""
                item["last_message"] = old.get("last_message") or ""
                item["last_job"] = old.get("last_job") or ""
                # 频率改了就要立刻重排，否则用户得等到下次触发才生效
                freq_changed = (item["every"] != old.get("every")) or (item["at"] != old.get("at"))
                if freq_changed or not item["next_run"]:
                    item["next_run"] = next_run_after(item)
                items[items.index(old)] = item
            else:
                if len(items) >= MAX_SCHEDULES:
                    raise ValueError("定时任务最多 %d 条" % MAX_SCHEDULES)
                item["next_run"] = next_run_after(item)
                items.append(item)
            self._save()
            return dict(item)

    def delete_schedule(self, sid):
        with self._lock:
            items = self.data.setdefault("schedules", [])
            before = len(items)
            self.data["schedules"] = [x for x in items if x["id"] != sid]
            changed = len(self.data["schedules"]) != before
            if changed:
                self._save()
            return changed

    def toggle_schedule(self, sid, enabled):
        with self._lock:
            for it in self.data.get("schedules") or []:
                if it["id"] == sid:
                    it["enabled"] = bool(enabled)
                    # 重新启用时立刻重排，不该补跑停用期间的任务
                    if it["enabled"]:
                        it["next_run"] = next_run_after(it)
                        it["last_status"] = ""
                    self._save()
                    return dict(it)
        return None

    def set_next_run(self, sid, ts):
        with self._lock:
            for it in self.data.get("schedules") or []:
                if it["id"] == sid:
                    it["next_run"] = float(ts)
                    self._save()
                    return True
        return False

    def mark_run(self, sid, job_id):
        with self._lock:
            for it in self.data.get("schedules") or []:
                if it["id"] == sid:
                    it["last_run"] = time.time()
                    it["last_job"] = job_id
                    it["last_status"] = "running"
                    it["last_message"] = "任务已下发"
                    self._save()
                    return True
        return False

    def mark_result(self, sid, status, message=""):
        with self._lock:
            for it in self.data.get("schedules") or []:
                if it["id"] == sid:
                    it["last_status"] = status
                    it["last_message"] = str(message or "")[:200]
                    if status == "skip":
                        it["last_run"] = time.time()
                    self._save()
                    return True
        return False

    # -- 给前端 ----------------------------------------------------
    def public(self):
        with self._lock:
            items = []
            for it in self.data.get("schedules") or []:
                d = dict(it)
                d["frequency"] = describe_frequency(it)
                items.append(d)
            return {
                "pull_timeout": int(self.data.get("pull_timeout") or self.default_timeout),
                "pull_timeout_min": MIN_PULL_TIMEOUT,
                "pull_timeout_max": MAX_PULL_TIMEOUT,
                "suggested_timeouts": SUGGESTED_TIMEOUTS,
                "schedules": items,
            }
