#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
任务引擎：把容器更新过程拆成六个可视化步骤，逐步发出事件，
前端通过 SSE 订阅，实时渲染「执行流程图 + 日志终端」。
"""

import json
import os
import sys
import threading
import time
import uuid

import ikuai_api as A

# 持久化（history.json / state.json / last_check.json）统一用这把锁保护。
# 这三处都是「读整个文件 → 改 → 整个写回」，而 check 任务与 update 任务可以
# 并发跑（多个标签页、自动检测与手动点击重叠），没有锁就会互相覆盖、静默丢记录。
# 注意：写盘本身用临时文件 + os.replace 保证原子性，但不能防住「读-改-写」整段
# 竞态，所以必须在整段上加锁。
_persist_lock = threading.RLock()


def _warn(message):
    """写盘等非致命错误不再静默吞掉 —— 至少要能在 docker logs 里看到。"""
    try:
        sys.stderr.write("[idocker] %s\n" % message)
        sys.stderr.flush()
    except Exception:                                         # noqa: BLE001
        pass

# 更新流程的六个步骤（前端按这个顺序画流程图）
UPDATE_STEPS = [
    ("login",    "登录爱快",     "建立会话，获取 sess_key"),
    ("inspect",  "读取容器配置", "回读挂载、固定 IP、环境变量"),
    ("pull",     "拉取新镜像",   "触发镜像库下载"),
    ("download", "等待下载完成", "轮询本地镜像确认落地"),
    ("update",   "就地更新容器", "docker_container.update"),
    ("verify",   "验证运行状态", "回查容器是否正常启动"),
]

CHECK_STEPS = [
    ("login", "登录爱快",     "建立会话"),
    ("read",  "读取容器清单", "获取容器与本地镜像"),
    ("query", "查询上游版本", "比对镜像 digest"),
]

HISTORY_LIMIT = 60
# 每条历史保留多少行日志。日志是「事后唯一能还原现场的东西」——
# 只存成功/失败，过几天回头看完全不知道当时发生了什么。
LOG_KEEP = 500


class BusyError(Exception):
    """已有更新任务在执行时抛出。"""


class Job(object):
    def __init__(self, kind, targets, tag=None, auto=False, schedule_id="", dry=None):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind                    # 'update' | 'check'
        self.targets = list(targets)
        self.tag = tag
        # auto=True 表示这次是定时任务触发的：更新前先比对上游 digest，
        # 上游没变就直接跳过，不拉取、不重启容器。手动点击更新时 auto=False，
        # 用户意图是「现在就更新」，就按原样走完整流程。
        self.auto = bool(auto)
        # 干跑开关**跟着任务走**，绝不写全局配置。dry 为 None 时（定时任务 /
        # 不带该字段的请求）回退到环境变量 DRY_RUN 的默认值。早期版本把
        # 手动请求里的 dry_run 写进全局 CFG，导致用户勾一次干跑后，此后所有
        # 定时任务都静默变成空跑 —— 界面上还显示「已完成」，没有任何提示。
        self.dry = None if dry is None else bool(dry)
        self.schedule_id = schedule_id or ""
        self.created = time.time()
        self.finished = False
        self.ok = None
        self.final_message = ""
        self.events = []
        self._cond = threading.Condition()
        self.steps = [{"key": k, "name": n, "desc": d, "state": "pending", "message": ""}
                      for k, n, d in (UPDATE_STEPS if kind == "update" else CHECK_STEPS)]

    # -- 事件 ------------------------------------------------------
    def emit(self, etype, **data):
        with self._cond:
            ev = dict(data)
            ev["type"] = etype
            ev["seq"] = len(self.events)
            ev["ts"] = time.time()
            self.events.append(ev)
            self._cond.notify_all()
        return ev

    def log(self, message, level="info"):
        self.emit("log", level=level, message=str(message))

    def step(self, key, state, message=""):
        with self._cond:
            for s in self.steps:
                if s["key"] == key:
                    s["state"] = state
                    s["message"] = message
                    break
        self.emit("step", key=key, state=state, message=message)

    def finish(self, ok, message=""):
        with self._cond:
            self.finished = True
            self.ok = ok
            self.final_message = message
            self._cond.notify_all()
        self.emit("done", ok=ok, message=message)

    # -- 订阅 ------------------------------------------------------
    def snapshot(self):
        with self._cond:
            return {"id": self.id, "kind": self.kind, "targets": self.targets,
                    "tag": self.tag, "created": self.created, "finished": self.finished,
                    "ok": self.ok, "auto": self.auto, "schedule_id": self.schedule_id,
                    "message": self.final_message,
                    "steps": [dict(s) for s in self.steps],
                    "event_count": len(self.events)}

    def wait_events(self, idx, timeout=15):
        """返回 (从 idx 开始的新事件, 是否已结束)"""
        with self._cond:
            if idx >= len(self.events) and not self.finished:
                self._cond.wait(timeout)
            return list(self.events[idx:]), self.finished


class JobManager(object):
    def __init__(self, cfg):
        self.cfg = cfg
        self.jobs = {}
        self.order = []
        self._lock = threading.Lock()
        self._busy = None                 # 正在执行的更新任务 id（同时只允许一个）
        self._check_busy = None           # 正在执行的检测任务 id（同时只允许一个）
        self._finish_hook = None          # 任务结束回调，调度器用它回写结果
        self.data_dir = cfg.get("data_dir") or "/data"
        try:
            os.makedirs(self.data_dir, exist_ok=True)
        except Exception:
            pass
        self.history_path = os.path.join(self.data_dir, "history.json")
        self.history = self._load_history()

    def set_on_finish(self, fn):
        self._finish_hook = fn

    def is_busy(self):
        with self._lock:
            return bool(self._busy)

    # -- 持久化 ----------------------------------------------------
    def _load_history(self):
        try:
            with open(self.history_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    @staticmethod
    def _extract_logs(job):
        """抽出任务的日志事件随历史一起落盘，供事后回看现场。

        两点取舍：
        - 只留 log 事件（step/progress 是过程量，steps 里已有最终状态）；
        - 丢掉 **API 往返原文**（`POST /Action/...` 与 `← {...}`）。那是实时终端里
          看的调试细节，响应体是原始 JSON（容器 env 等可能带凭据），不该长期落盘。
          其余 debug 行（如「登录成功」）保留，它们对事后排查有用。
        """
        out = []
        for ev in job.events:
            if ev.get("type") != "log":
                continue
            msg = ev.get("message")
            if not msg:
                continue
            lv = ev.get("level") or "info"
            if lv == "debug" and (msg.startswith("POST ") or msg.startswith("← ")):
                continue
            out.append({"ts": ev.get("ts") or 0, "level": lv, "message": msg})
        return out[-LOG_KEEP:]

    def find_history(self, job_id):
        """按任务 id 取一条历史（含日志明细）"""
        for h in self.history:
            if h.get("id") == job_id:
                return h
        return None

    def _save_history(self, job):
        with _persist_lock:
            self.history.insert(0, {
                "id": job.id, "kind": job.kind, "targets": job.targets, "tag": job.tag,
                "created": job.created, "ok": job.ok,
                "finished_at": time.time(), "steps": job.steps,
                "message": job.final_message or "",
                "logs": self._extract_logs(job),
            })
            del self.history[HISTORY_LIMIT:]
            try:
                tmp = self.history_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.history, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.history_path)
            except Exception as e:                            # noqa: BLE001
                _warn("历史记录写入失败（%s）：%s" % (self.history_path, e))

    # -- 生命周期 --------------------------------------------------
    def create(self, kind, targets, tag=None, auto=False, schedule_id="", dry=None):
        with self._lock:
            # 更新任务互斥：两个任务同时改容器会互相打断，也会让「验证」拿错状态
            if kind == "update":
                if self._busy:
                    raise BusyError("已有更新任务在执行，请等它跑完再试")
            # 检测任务也互斥：多个标签页 / 自动检测与手动点击重叠时会重复打
            # 爱快和加速源；而且 check 与 update 并发写 state.json 更容易撞车。
            # 策略是「复用」而不是「拒绝」—— 前端拿到的还是同一个 job id，
            # 照样能订阅，不会报错打断用户。
            elif kind == "check":
                cur = self.jobs.get(self._check_busy) if self._check_busy else None
                if cur is not None and not cur.finished:
                    return cur

            job = Job(kind, targets, tag, auto=auto, schedule_id=schedule_id, dry=dry)
            if kind == "update":
                self._busy = job.id
            else:
                self._check_busy = job.id
            self.jobs[job.id] = job
            self.order.append(job.id)
            while len(self.order) > HISTORY_LIMIT:
                old = self.order.pop(0)
                self.jobs.pop(old, None)
        # 线程创建/启动可能失败（典型：can't start new thread）。若不回滚，
        # 互斥位会永远悬着 —— 更新功能就此永久失效，只能重启容器恢复。
        try:
            t = threading.Thread(target=self._run, args=(job,), daemon=True)
            t.start()
        except Exception:
            with self._lock:
                if kind == "update" and self._busy == job.id:
                    self._busy = None
                elif kind == "check" and self._check_busy == job.id:
                    self._check_busy = None
                self.jobs.pop(job.id, None)
                try:
                    self.order.remove(job.id)
                except ValueError:
                    pass
            raise
        return job

    def get(self, jid):
        return self.jobs.get(jid)

    # -- 实际执行 --------------------------------------------------
    def _run(self, job):
        ctx = A.Ctx(logger=lambda m, level="info": job.log(m, level), verbose=True)
        ok, msg = False, ""
        try:
            if job.kind == "update":
                ok, msg = self._run_update(job, ctx)
            else:
                ok, msg = self._run_check(job, ctx)
        except A.IKuaiError as e:
            msg = str(e)
            job.log("❌ " + msg, level="error")
        except Exception as e:                        # noqa: BLE001
            msg = "%s: %s" % (type(e).__name__, e)
            job.log("❌ 未预期的异常 " + msg, level="error")
        finally:
            if job.kind == "update":
                with self._lock:
                    if self._busy == job.id:
                        self._busy = None
            else:
                with self._lock:
                    if self._check_busy == job.id:
                        self._check_busy = None
            job.finish(ok, msg)
            self._save_history(job)
            hook = self._finish_hook
            if hook:
                try:
                    hook(job)                 # 调度器据此回写定时任务的执行结果
                except Exception as e:        # noqa: BLE001
                    job.log("调度结果回写失败：%s" % e, level="warn")

    # -- 更新流程 --------------------------------------------------
    def _run_update(self, job, ctx):
        cfg = self.cfg
        targets = job.targets

        # 1. 登录
        job.step("login", "running", "正在登录 %s" % cfg["router"]["url"])
        job.log("登录爱快 %s（账号 %s）" % (cfg["router"]["url"], cfg["router"]["username"]))
        ik = A.IKuai(cfg, ctx)
        ik.login()
        job.step("login", "done", "会话已建立")

        # 2. 读取容器与配置
        job.step("inspect", "running", "读取容器清单")
        cs = A.get_containers(ik)
        if not cs:
            job.step("inspect", "failed", "没读到容器")
            raise A.IKuaiError("没读到任何容器，请确认爱快 Docker 服务在运行")
        by_name = {c["name"]: c for c in cs}
        missing = [n for n in targets if n not in by_name]
        if missing:
            job.step("inspect", "failed", "找不到容器")
            raise A.IKuaiError("找不到容器：%s" % "、".join(missing))
        job.log("共 %d 个容器，本次处理 %d 个：%s"
                % (len(cs), len(targets), "、".join(targets)))

        plans = []
        for name in targets:
            c = by_name[name]
            insp = A.get_inspect(ik, c["id"])
            ns, iname, cur_tag = A.split_image(c["image"])
            new_tag = job.tag or cur_tag
            job.log("  %s  当前 %s:%s  → 目标 %s:%s"
                    % (name, A.repo_of(c["image"]), cur_tag, A.repo_of(c["image"]), new_tag))
            job.log("    挂载 %s | IP %s | 自启 %s"
                    % (c.get("mounts") or "无", c.get("ipaddr") or "无", c.get("auto_start")))
            plans.append((c, insp, ns, iname, cur_tag, new_tag))
        job.step("inspect", "done", "已回读 %d 个容器的配置" % len(plans))

        # 上游检测统一用「爱快自己配置的加速源」——检测的源必须和拉取的源一致，
        # 否则会出现「页面说有新版、爱快点下去却拉不到」的错位。
        # 非 auto 任务用不到远程比对，省一次接口调用。
        registries = A.resolve_registries(ik, cfg, job.log) if job.auto else None

        # 一个容器失败不再中断整批：定时任务里最怕「一个卡住整晚白跑」，
        # 所以单个容器出错只记账，继续处理后面的。
        failures = []
        total = len(plans)
        for idx, (c, insp, ns, iname, cur_tag, new_tag) in enumerate(plans, 1):
            head = "[%d/%d] %s" % (idx, total, c["name"])
            job.emit("progress", current=idx, total=total, container=c["name"])
            job.log("", level="plain")
            job.log("═══ %s  %s:%s → %s ═══" % (head, A.repo_of(c["image"]), cur_tag, new_tag))
            repo = A.repo_of(c["image"])
            digest_key = "%s:%s" % (repo, new_tag)
            upstream, upstream_src = "", ""

            # 定时任务（auto）先看上游到底变没变。只有 digest 变化才值得走一遍
            # 拉取 + 重启；否则每次定时跑都重启容器会平白中断服务 —— 爱快对
            # 「镜像已是最新」的判断并不总是可靠，不能指望它来兜底。
            if job.auto:
                job.step("pull", "running", "比对上游版本")
                try:
                    upstream, upstream_src = A.remote_digest(
                        registries, repo, new_tag, ctx, cfg["timeout"])
                except Exception as e:                      # noqa: BLE001
                    upstream, upstream_src = "", ""
                    job.log("上游版本查询失败（%s），退回按实际拉取结果判断" % e, level="warn")
                known = self._seen_digest(digest_key)
                if upstream and known and upstream == known:
                    job.step("pull", "skipped", "上游未变化")
                    job.step("download", "skipped", "无需下载")
                    job.step("update", "skipped", "上游无新版本，跳过重启")
                    job.step("verify", "skipped", "容器保持原状")
                    cur = {x["name"]: x for x in A.get_containers(ik)}.get(c["name"]) or {}
                    job.log("✅ %s 上游镜像未变化（%s），跳过更新，容器不重启（当前：%s）"
                            % (c["name"], upstream_src or "上游", cur.get("status") or "运行中"))
                    continue
                if upstream and not known:
                    job.log("没有 %s 的历史版本记录，按实际拉取结果决定是否重启" % digest_key)

            # 3. 拉镜像
            job.step("pull", "running", "拉取 %s:%s" % (iname, new_tag))
            payload, unmapped = A.container_payload(c, insp, tag=new_tag)
            job.log("容器参数：%s" % json.dumps(payload, ensure_ascii=False))
            if unmapped:
                job.log("容器声明了 %d 个端口但未做宿主映射（doc_docker 路由直连，属正常）"
                        % unmapped, level="warn")
            since = int(time.time())
            # 干跑开关跟随本次任务：手动请求传来什么就是什么；定时任务没传，
            # 回退到环境变量 DRY_RUN 的默认值。绝不读/写全局可变状态。
            dry = job.dry if job.dry is not None else bool(cfg.get("dry_run"))
            already = False
            # 拉取前先记下本地镜像的内容指纹（镜像 ID）
            sig_before = "" if dry else A.image_signature(ik, repo, new_tag)
            try:
                ik.call("docker_image", "install",
                        {"name": iname, "tag": new_tag,
                         "image_logo": c.get("image_logo", ""), "namespace": ns})
            except A.IKuaiError as e:
                # 爱快在镜像已是最新时会返回「已存在相同内容」类错误，这不是失败
                if A.is_already_exists(e):
                    already = True
                    job.log("上游镜像与本地一致（%s:%s），无需重新下载" % (iname, new_tag),
                            level="warn")
                else:
                    raise
            job.step("pull", "done",
                     "镜像已是最新" if already else ("干跑：仅演练" if dry else "下载任务已下发"))

            # 干跑模式：写操作都被拦下了，后面的等待/更新/验证都没有真实意义，直接模拟
            if dry:
                job.step("download", "done", "干跑：跳过等待")
                job.step("update", "done", "干跑：未实际执行")
                try:
                    cur = {x["name"]: x for x in A.get_containers(ik)}.get(c["name"]) or {}
                    job.step("verify", "done", "干跑保持原状：" + (cur.get("status") or "未知"))
                except A.IKuaiError:
                    job.step("verify", "done", "干跑：未验证")
                job.log("🔎 干跑完成，未对路由器做任何修改。关闭「干跑模式」后重跑即会真实执行。",
                        level="warn")
                continue

            # 「本地已有该镜像」不等于「容器正在用它」。
            # 指定 tag 更新时，镜像可能早就下好了、容器却还挂在旧 tag 上
            # （实测 lucky：2.27.2 早已就绪，容器仍在跑 2.20.2）——
            # 这种情况必须继续往下走重启，否则就是「镜像下好了却没更新容器」。
            owner_now = A.image_owner(ik, c["name"])
            using_it = bool(owner_now and A.tag_matches(owner_now.get("tag"), new_tag))

            # 镜像与上游一致**且容器已在用它** → 才谈得上是「已是最新」，不必重启
            if already and using_it:
                job.step("download", "skipped", "无需下载")
                job.step("update", "skipped", "已是最新，跳过重启")
                job.step("verify", "skipped", "容器保持原状")
                self._remember_if_known(digest_key, upstream, upstream_src)
                cur = {x["name"]: x for x in A.get_containers(ik)}.get(c["name"]) or {}
                job.log("✅ %s 已是最新版本，未做任何改动（当前：%s）"
                        % (c["name"], cur.get("status") or "运行中"))
                continue
            if already:
                job.log("镜像 %s:%s 已在本地，但容器当前挂在 %s 上 → 跳过下载，直接重启切过去"
                        % (iname, new_tag, (owner_now or {}).get("tag") or "未知镜像"),
                        level="warn")

            # 4. 等下载（镜像本来就在本地时无需等待）
            if already:
                job.step("download", "done", "镜像已在本地，跳过等待")
            else:
                job.step("download", "running", "等待镜像落地")
                pull_t = int(cfg.get("pull_timeout") or 900)
                job.log("等待镜像下载完成（最长 %ds）…" % pull_t)
                done, note = A.wait_for_image(ik, A.repo_of(c["image"]), new_tag, since,
                                              pull_t, ctx)
                if done:
                    size = (note.get("size") or 0) / 1048576.0 if isinstance(note, dict) else 0
                    job.step("download", "done", "镜像已就绪 %.1f MB" % size)
                    job.log("镜像下载完成（%.1f MB）" % size)
                else:
                    job.step("download", "failed", note or "超时")
                    self._remember_if_known(digest_key, upstream, upstream_src)
                    job.log("❌ %s 的镜像未在 %ds 内下载完成（%s）。本次跳过该容器，"
                            "不会用旧镜像重建。若镜像较大或加速源较慢，"
                            "可在页面的「设置」里把下载超时调大后重试。"
                            % (c["name"], pull_t, note or "超时"), level="error")
                    failures.append(c["name"])
                    continue

            # 爱快对「已是最新」的镜像有时报错、有时只是重新登记并刷新 install 时间，
            # 所以不能只看错误或时间戳。比对镜像 ID（内容指纹）才可靠。
            # 但要带上 using_it：**只有容器本来就在用这个 tag 时**，
            # 「ID 没变」才等价于「无需重启」；容器挂在别的 tag 上时，
            # 哪怕目标镜像 ID 一个字都没变，也必须重启才能切过去。
            sig_after = A.image_signature(ik, repo, new_tag)
            if sig_before and sig_after and sig_after == sig_before and using_it:
                job.step("update", "skipped", "镜像内容未变化，跳过重启")
                job.step("verify", "skipped", "容器保持原状")
                self._remember_if_known(digest_key, upstream, upstream_src)
                cur = {x["name"]: x for x in A.get_containers(ik)}.get(c["name"]) or {}
                job.log("✅ %s 已是最新版本（镜像 ID 未变），未做任何改动（当前：%s）"
                        % (c["name"], cur.get("status") or "运行中"))
                continue

            # 5. 就地更新
            job.step("update", "running", "就地更新容器")
            job.log("就地更新容器（docker_container.update，不删容器、配置保留）")
            ik.call("docker_container", "update", payload)
            job.step("update", "done", "更新指令已下发")

            # 6. 验证
            job.step("verify", "running", "等待容器启动")
            after = None
            # 轮询几次而不是死等固定秒数：容器小的话几百毫秒就起来了，
            # 大的（如 vaultwarden、clash）可能要十几秒。
            for _ in range(8):
                time.sleep(2)
                after = {x["name"]: x for x in A.get_containers(ik)}.get(c["name"])
                if after and after.get("state") == "running":
                    break
            if not after:
                job.step("verify", "failed", "回查不到容器")
                job.log("❌ %s 更新后查不到，请到爱快后台确认" % c["name"], level="error")
                failures.append(c["name"])
                continue
            if after.get("state") != "running":
                job.step("verify", "failed", "状态 %s" % after.get("status"))
                job.log("❌ %s 更新后状态异常：%s（请到爱快后台查看容器日志）"
                        % (c["name"], after.get("status")), level="error")
                failures.append(c["name"])
                continue
            # 关键核对：容器是不是**真的**切到了新镜像。
            # 爱快 update 是异步的，而且容器自身的 image 字段常常不带 tag
            # （实测 lucky 就是 gdy666/lucky），光看「容器 Up」会把
            # 「重建了但仍跑旧镜像」误判成成功 —— 这个坑已经踩过一次。
            # 镜像条目的 containers 字段记录占用关系，是唯一可靠依据；
            # 它有轻微滞后，所以多轮几次。
            owner, ok_owner = None, False
            for _ in range(4):
                owner = A.image_owner(ik, c["name"])
                ok_owner = bool(owner and A.tag_matches(owner.get("tag"), new_tag))
                if ok_owner:
                    break
                time.sleep(2)
            ok_image = A.tag_matches(A.split_image(after.get("image") or "")[2], new_tag)
            if not ok_owner and not ok_image:
                shown = "%s:%s" % (owner.get("repo"), owner.get("tag")) if owner else "未知镜像"
                job.step("verify", "failed", "仍挂在 %s" % (owner.get("tag") if owner else "?"))
                job.log("❌ %s 重建后仍挂在 %s 上，没切到 %s —— 容器可能被用旧镜像重建了，"
                        "请到爱快后台核对容器的镜像 tag"
                        % (c["name"], shown, new_tag), level="error")
                failures.append(c["name"])
                continue
            if not ok_owner:
                job.log("⚠ 镜像占用关系还没刷新（容器已指向 %s），不影响使用" % new_tag,
                        level="warn")
            # 启动日志核查：容器 Up ≠ 进程在干活。
            # 实测踩过的坑：lucky 2.20→2.27 镜像的 entrypoint 从 /app/lucky
            # 换成了 /app/start.sh，而容器里保存的旧 cmd 被原样回填后污染了
            # 启动脚本（它用 ps|grep 判断"lucky 是否在跑"，匹配到了自己），
            # 结果容器一直 Up、内存只有几十 KB、进程从未启动、端口不监听，
            # 工具却因为只看了 state=running 而报成功。补上这道核查。
            logs = A.get_container_logs(ik, after["id"], limit=12)
            if logs:
                job.log("容器启动日志（末 %d 行）：" % len(logs))
                for line in logs[-6:]:
                    job.log("  │ " + line)
            else:
                job.log("⚠ %s 已重建且处于 running，但容器没有任何日志输出，"
                        "无法确认服务真的在跑 —— 请到爱快后台核对容器日志，"
                        "重点看镜像的启动参数（cmd/入口）是否还适用于新版本"
                        % c["name"], level="warn")
            job.step("verify", "done", "%s · %s" % (after.get("status") or "running", new_tag))
            self._remember_if_known(digest_key, upstream, upstream_src)
            job.log("✅ %s → %s:%s   状态：%s"
                    % (c["name"], (owner or {}).get("repo") or c["repo"], new_tag,
                       after.get("status")))

        if failures:
            return False, ("处理 %d 个容器，其中 %d 个未成功：%s"
                           % (len(plans), len(failures), "、".join(failures)))
        return True, "完成 %d 个容器的更新" % len(plans)

    # -- 版本检测流程 ----------------------------------------------
    def _run_check(self, job, ctx):
        cfg = self.cfg
        job.step("login", "running", "正在登录")
        ik = A.IKuai(cfg, ctx)
        ik.login()
        job.step("login", "done", "会话已建立")

        job.step("read", "running", "读取容器与本地镜像")
        cs = A.get_containers(ik)
        local = A.LocalImages(A.get_local_images(ik))
        job.step("read", "done", "%d 个容器 / %d 个镜像" % (len(cs), len(local)))

        job.step("query", "running", "查询上游 digest")
        # 用爱快自己配置的加速源去查，保证「检测的源」和「拉取的源」一致
        registries = A.resolve_registries(ik, cfg, job.log)
        job.log("查询上游用的加速源（来自爱快 Docker 服务设置）：%s" % "、".join(registries))
        results, seen = [], set()
        for c in cs:
            image = c.get("image") or ""
            repo, tag = A.repo_of(image), A.split_image(image)[2]
            key = "%s:%s" % (repo, tag)
            if key in seen:
                continue
            seen.add(key)
            local_ts, _src = local.pulled_at(local.find(repo, tag))
            item = {"container": c["name"], "image": key, "local": local_ts or "—",
                    "status": "unknown", "digest": "", "registry": ""}
            try:
                digest, reg = A.remote_digest(registries, repo, tag, ctx, cfg["timeout"])
                item["digest"], item["registry"] = digest[:19], reg
                old = self._seen_digest(key)
                item["status"] = "unknown" if old is None else ("newer" if old != digest else "latest")
                # 逐个镜像都记一行，并带上命中的加速源 —— 事后回看历史时，
                # 「查到的是哪个源的什么结果」比只看到一句「检测完成」有用得多。
                st = item["status"]
                if st == "newer":
                    job.log("⬆ %s 上游有新版本（命中 %s）" % (key, reg), level="warn")
                elif st == "latest":
                    job.log("✓ %s 已是最新（命中 %s）" % (key, reg))
                else:
                    job.log("· %s 首次检测，已记下基准（命中 %s）" % (key, reg))
                self._remember_digest(key, digest, reg)
                results.append(item)
            except Exception as e:
                item["status"] = "failed"
                item["digest"] = str(e).splitlines()[0][:60]
                job.log("✗ 查询 %s 失败：%s" % (key, item["digest"]), level="error")
                results.append(item)
        job.step("query", "done", "共检查 %d 个镜像" % len(results))
        job.emit("check_result", results=results)
        self._save_last_check(results)
        newer = [r["container"] for r in results if r["status"] == "newer"]
        job.log("检测完成，%d 个镜像有新版%s"
                % (len(newer), ("：" + "、".join(newer)) if newer else ""))
        return True, "已检查 %d 个镜像" % len(results)

    # -- 上次检测结果（页面刷新后仍能显示上游状态）--------------------
    def _last_check_path(self):
        return os.path.join(self.data_dir, "last_check.json")

    def _save_last_check(self, results):
        items = {}
        for r in results:
            items[r["image"]] = {"status": r["status"], "digest": r.get("digest", ""),
                                 "registry": r.get("registry", ""), "local": r.get("local", "")}
        payload = {"checked_at": time.time(),
                   "at_str": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "items": items}
        with _persist_lock:
            try:
                tmp = self._last_check_path() + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self._last_check_path())
            except Exception as e:                            # noqa: BLE001
                _warn("上次检测结果写入失败：%s" % e)

    def last_check(self):
        with _persist_lock:
            try:
                with open(self._last_check_path(), "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {"checked_at": 0, "at_str": "", "items": {}}

    # -- digest 记忆 -------------------------------------------------
    def _state_path(self):
        return os.path.join(self.data_dir, "state.json")

    def _load_state(self):
        with _persist_lock:
            try:
                with open(self._state_path(), "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}

    def _seen_digest(self, key):
        return (self._load_state().get(key) or {}).get("digest")

    def _remember_if_known(self, key, digest, reg):
        """只有真查到上游 digest 才写回，避免用空值盖掉已有基准。

        自动更新就是靠这个基准判断「上游到底变没变」，一旦被空值覆盖，
        下次定时任务就会认为「没有历史记录」而多做一次无谓的拉取。
        """
        if digest:
            self._remember_digest(key, digest, reg)

    def _remember_digest(self, key, digest, reg):
        # 整段「读-改-写」必须加锁：check 与 update 可并发，多个标签页也能同时
        # 触发检测。早期无锁实测 30 个线程各写一个 key，落盘只剩 1 条、丢了 29 条。
        with _persist_lock:
            st = self._load_state()
            st[key] = {"digest": digest, "registry": reg,
                       "checked_at": time.strftime("%Y-%m-%d %H:%M:%S")}
            try:
                tmp = self._state_path() + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(st, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self._state_path())
            except Exception as e:                            # noqa: BLE001
                _warn("digest 记忆写入失败：%s" % e)
