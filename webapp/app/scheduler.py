#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定时调度器。

一个后台线程，每 15 秒醒一次，检查有没有到点的定时任务：

  · 到点后**先推进 next_run 再执行**，这样即使任务执行很久、或者调度线程
    卡住，也不会重复触发同一个任务（补跑风暴）。
  · 如果此刻已有更新任务在跑，本次直接记一条「跳过」，不会排队堆积。
  · 任务跑完由 JobManager 回调 ``on_job_finish``，把结果写回任务记录。

为什么不用 cron 表达式：用户只需要「每隔几小时」和「每天几点」两种，
两个字段就够了；手写 cron 解析反而容易在时区/夏令时上出错。
"""

import sys
import threading
import time

import store as S


class Scheduler(threading.Thread):
    TICK_SECONDS = 15
    START_DELAY = 4              # 等服务端口起来再开始，避免启动日志互相干扰

    def __init__(self, store, manager):
        super(Scheduler, self).__init__(daemon=True, name="ikuai-scheduler")
        self.store = store
        self.manager = manager
        self._stop = threading.Event()
        self.last_tick = 0.0
        self.triggered = 0

    # -- 生命周期 --------------------------------------------------
    def stop(self):
        self._stop.set()

    def run(self):
        self._stop.wait(self.START_DELAY)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:                      # noqa: BLE001
                # 调度线程绝不能因为单次异常整个退出
                self._say("[调度] 异常 %s: %s" % (type(e).__name__, e))
            self._stop.wait(self.TICK_SECONDS)

    @staticmethod
    def _say(msg):
        try:
            sys.stdout.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
            sys.stdout.flush()
        except Exception:
            pass

    # -- 一轮检查 --------------------------------------------------
    def tick(self):
        now = time.time()
        self.last_tick = now
        for item in self.store.schedules():
            if not item.get("enabled"):
                continue
            nxt = float(item.get("next_run") or 0)
            if not nxt:
                # 老数据或手动改过文件：补一个排期，不立刻触发
                self.store.set_next_run(item["id"], S.next_run_after(item, now))
                continue
            if now < nxt:
                continue
            self._fire(item, now)

    def _fire(self, item, now):
        sid = item["id"]
        # 先把排期推到下一个点，保证「无论后面发生什么都只触发一次」
        self.store.set_next_run(sid, S.next_run_after(item, now))

        if self.manager.is_busy():
            self.store.mark_result(sid, "skip", "此刻已有任务在执行，本次跳过")
            self._say("[调度] %s 跳过：已有任务在执行" % item["name"])
            return
        if item["mode"] == "update" and not item["targets"]:
            self.store.mark_result(sid, "skip", "未选择目标容器，已跳过")
            return

        try:
            job = self.manager.create(
                item["mode"],
                item["targets"],
                item.get("tag") or None,
                auto=True,
                schedule_id=sid,
            )
        except Exception as e:                          # noqa: BLE001
            self.store.mark_result(sid, "fail", str(e))
            self._say("[调度] %s 启动失败：%s" % (item["name"], e))
            return

        self.store.mark_run(sid, job.id)
        self.triggered += 1
        self._say("[调度] 触发「%s」→ %s（任务 %s）"
                  % (item["name"], S.describe_frequency(item), job.id))

    # -- 任务结束回调（由 JobManager 调用）---------------------------
    def on_job_finish(self, job):
        sids = list(getattr(job, "schedule_ids", []) or [])
        if not sids:
            sid = getattr(job, "schedule_id", "")
            sids = [sid] if sid else []
        if not sids:
            return
        status = "ok" if job.ok else "fail"
        message = job.final_message or ("已完成" if job.ok else "执行失败")
        for sid in sids:
            self.store.mark_result(sid, status, message)
