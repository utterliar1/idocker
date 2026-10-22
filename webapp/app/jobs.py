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
        self.schedule_ids = [self.schedule_id] if self.schedule_id else []
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

    def add_schedule_id(self, schedule_id):
        schedule_id = str(schedule_id or "")
        if not schedule_id:
            return
        with self._cond:
            if schedule_id not in self.schedule_ids:
                self.schedule_ids.append(schedule_id)

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
                    "schedule_ids": list(self.schedule_ids),
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
                    cur.add_schedule_id(schedule_id)
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
        # 手动点击时只有在「爱快拒绝重新拉取」时才需要查上游，所以做成用到才查。
        _regs = {"v": None}

        def registries():
            if _regs["v"] is None:
                try:
                    _regs["v"] = A.resolve_registries(ik, cfg, job.log)
                except Exception as e:                      # noqa: BLE001
                    _regs["v"] = []
                    job.log("读取加速源失败（%s），本轮不查上游" % e, level="warn")
            return _regs["v"]

        def local_sig(tag):
            """本地 repo:tag 记录的镜像 ID —— 和上游 config.digest 同层可比。"""
            return A.image_signature(ik, repo, tag)

        def upstream_of(tag):
            """查上游，返回 (manifest digest, amd64 config digest, 命中的源)。"""
            try:
                return A.remote_platform_digest(registries(), repo, tag, ctx,
                                                cfg["timeout"])
            except Exception as e:                          # noqa: BLE001
                job.log("上游内容查询失败（%s）" % e, level="warn")
                return "", "", ""

        def stale_of(tag):
            """本地内容 vs 上游内容，返回 True=落后 / False=一致 / None=判不了。"""
            _man, _cfg, _src = upstream_of(tag)
            _ls = local_sig(tag)
            if not (A.norm_digest(_ls) and A.norm_digest(_cfg)):
                return None, _man, _cfg, _src
            if not A.same_content(_ls, _cfg):
                job.log("本地镜像 %s ≠ 上游内容 %s（命中 %s）—— 本地这份是旧的"
                        % (A.norm_digest(_ls)[:12], A.norm_digest(_cfg)[:12],
                           _src or "上游"), level="warn")
            return A.same_content(_ls, _cfg) is False, _man, _cfg, _src

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
            pull_t = int(cfg.get("pull_timeout") or 900)
            # 容器在不在用目标 tag —— 只看容器**自己声明的 tag**（理由见下方长注释）。
            # 提前到这里，是因为上面「定时先看上游变没变」也要用它。
            using_it = (cur_tag == new_tag)

            # 定时任务（auto）先看本地镜像和上游**内容**是不是同一份。只有真的不同
            # 才值得走一遍拉取 + 重启；否则每次定时跑都重启容器会平白中断服务。
            #
            # 判据从「上游 digest vs 自己上次记的 digest」改成「本地镜像 ID vs 上游
            # amd64 的 config.digest」—— 前者是拿自己记的数字跟自己比：上游一变就
            # 把新 digest 记成基准，报告被自我消化，于是永远报「已是最新」（线上
            # 踩过：mihomo 报过一次之后永久安静）。后者才回答得了「本地这份落后没有」。
            if job.auto:
                job.step("pull", "running", "比对上游内容")
                _stale, upstream, _up_cfg, upstream_src = stale_of(new_tag)
                if _stale is None and upstream:
                    # 源没给清单正文 → 退回「上游清单 digest 有没有变」
                    _known = self._seen_digest(digest_key)
                    if _known is None:
                        job.log("没有 %s 的历史记录，按实际拉取结果决定是否重启"
                                % digest_key)
                    elif _known == upstream:
                        _stale = False
                if _stale is False and using_it:
                    job.step("pull", "skipped", "内容未变化")
                    job.step("download", "skipped", "无需下载")
                    job.step("update", "skipped", "内容无变化，跳过重启")
                    job.step("verify", "skipped", "容器保持原状")
                    cur = {x["name"]: x for x in A.get_containers(ik)}.get(c["name"]) or {}
                    job.log("✅ %s 本地镜像与上游是同一份内容（%s），跳过更新，容器不重启"
                            "（当前：%s）" % (c["name"], upstream_src or "上游",
                                             cur.get("status") or "运行中"))
                    continue
                if _stale is False and not using_it:
                    job.log("内容没有变化，但容器还挂在 %s 上 → 仍然要重启才能切到 %s"
                            % (cur_tag, new_tag), level="warn")

            # 3. 拉镜像
            job.step("pull", "running", "拉取 %s:%s" % (iname, new_tag))
            payload, unmapped = A.container_payload(c, insp, tag=new_tag)
            # 不把 env/cmd 写入普通日志；它们可能包含 API 密钥、密码和内部路径。
            job.log("容器参数摘要：镜像=%s，挂载=%s，环境变量=%s，启动命令=%s" % (
                payload.get("image") or "未设置",
                "已保留" if payload.get("mounts") else "无",
                "已保留" if payload.get("env") else "无",
                "已保留" if payload.get("cmd") else "无"))
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
                    job.log("爱快表示 %s:%s 已在本地、拒绝重新下载（它只按记录在不在判断，"
                            "不比对内容）" % (iname, new_tag), level="warn")
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
            #
            # ⚠️ 判断「容器在不在用目标镜像」只看容器**自己声明的 tag**。
            # 曾经用 `tag_matches(image_owner(..)["tag"], new_tag)`，但那是镜像条目的
            # **标签集合**，一条记录可以同时挂多个标签（实测 vaultwarden 的条目就是
            # `"1.37.3,latest"`）。容器明明跑 1.37.3，用集合去匹配 latest 得到 True，
            # 于是被判成「已在用 latest」→ 静默跳过更新，用户点了「切到 latest」
            # 什么也没发生，任务结果还报成功。这是 v1.4.2 的真实事故。
            # `cur_tag` 来自容器声明的镜像名，没写 tag 时 `split_image` 返回 latest，
            # 与爱快前端的口径一致。（`using_it` 已在上面算好。）

            # 镜像与上游一致**且容器已在用它** → 才谈得上是「已是最新」，不必重启。
            #
            # ⚠️ 但「爱快回『已存在相同内容』」**不能**当成「内容一致」：它只检查
            # 同名同 tag 的本地记录在不在，压根不比对内容。旧代码就这么把「拒绝
            # 重新拉取」误判成「已是最新」，于是 latest 容器一旦落后就永远刷不新，
            # 任务还报成功（mihomo / vaultwarden 线上事故）。这里必须再核一次内容。
            if already and using_it:
                _stale, upstream, _up_cfg, upstream_src = stale_of(new_tag)
                if _stale is False:
                    job.step("download", "skipped", "无需下载")
                    job.step("update", "skipped", "内容一致，跳过重启")
                    job.step("verify", "skipped", "容器保持原状")
                    self._remember_if_known(digest_key, upstream, upstream_src)
                    cur = {x["name"]: x for x in A.get_containers(ik)}.get(c["name"]) or {}
                    job.log("✅ %s 已是最新（本地镜像 == 上游内容指纹），未做任何改动"
                            "（当前：%s）" % (c["name"], cur.get("status") or "运行中"))
                    continue
                if _stale is None:
                    # 拿不到上游内容指纹，就只剩爱快那句话可依。别把它说成
                    # 「已是最新」—— 说清楚依据是什么，用户才知道能不能信。
                    job.step("download", "skipped", "无需下载")
                    job.step("update", "skipped", "无法核对内容，按爱快判断跳过")
                    job.step("verify", "skipped", "容器保持原状")
                    self._remember_if_known(digest_key, upstream, upstream_src)
                    job.log("⚠ %s 爱快表示 %s:%s 已在本地、拒绝重新拉取，但没取到上游内容"
                            "指纹，无法确认内容是否真的最新。想强制刷新可用「指定版本」"
                            "填一个新的 tag。" % (c["name"], iname, new_tag), level="warn")
                    continue
                # _stale is True：本地那份确实是旧的，而爱快拒绝重拉 → 只能「搬运」。
                if new_tag == "latest":
                    _ok, _msg = self._hoist_latest(job, ik, ctx, c, insp, repo, ns, iname,
                                                   new_tag, _up_cfg, upstream_src, pull_t)
                    if _ok:
                        self._remember_if_known(digest_key, upstream, upstream_src)
                    else:
                        failures.append(c["name"])
                    continue
                job.step("download", "failed", "爱快拒绝重新拉取")
                job.step("update", "failed", "内容落后且无法重新拉取")
                job.step("verify", "skipped", "容器保持原状")
                self._remember_if_known(digest_key, upstream, upstream_src)
                job.log("❌ %s 本地 %s:%s 的内容是旧的，但爱快认为同 tag 的记录已存在、"
                        "拒绝重新拉取 —— 同名同 tag 的镜像内容无法原地刷新。\n"
                        "   请到爱快后台「Docker → 本地镜像」删掉 %s 这条记录后重试；"
                        "或改用「指定版本」换一个 tag。"
                        % (c["name"], iname, new_tag, iname), level="error")
                failures.append(c["name"])
                continue

            # 「切换 tag」但切换原地打转：容器的镜像记录上**同时挂着当前 tag 和目标 tag**
            # （实测 vaultwarden 的条目就是 `"1.37.3,latest"` —— 容器声明跑 1.37.3，
            # 这条记录另外还挂着 latest）。这时把容器改成 repo:latest，落到的是
            # **同一条记录、同一份内容**；爱快又只按「同名同 tag 的记录是否已存在」
            # 拒拉、**不比对内容**，于是新内容永远进不来。
            #
            # 这里要分两种情况，不能一刀切报失败（v1.4.2 的教训是「什么都报失败」，
            # 反过来「什么都报成功」同样是假话）：
            #   · 记录里的内容**已经等于上游** → 只是换个标签，重启切过去就是对的；
            #   · 内容确实是旧的 → 走「搬运」（拉同内容的版本号 tag 再拐回来）。
            #
            # 注意与「镜像早已下好、只是容器没挂上去」区分开：那种情况目标 tag 是
            # **另一条独立记录**（owner 的 tag 串里没有 new_tag），重启是真能切过去的，
            # 必须继续走原路径，不能误报失败。
            if already and not using_it:
                own_tag = (A.image_owner(ik, c["name"]) or {}).get("tag") or ""
                if A.tag_matches(own_tag, cur_tag) and A.tag_matches(own_tag, new_tag):
                    _stale2, upstream, _up_cfg2, upstream_src = stale_of(new_tag)
                    if _stale2 is False:
                        job.log("ℹ %s 这条镜像记录（%s）本来就含 %s，而且内容已是最新 ——"
                                " 只需把容器切过去，不必重新下载"
                                % (iname, own_tag, new_tag))
                    elif _stale2 is True and new_tag == "latest":
                        _ok, _msg = self._hoist_latest(job, ik, ctx, c, insp, repo, ns,
                                                       iname, new_tag, _up_cfg2,
                                                       upstream_src, pull_t)
                        if _ok:
                            self._remember_if_known(digest_key, upstream, upstream_src)
                        else:
                            failures.append(c["name"])
                        continue
                    else:
                        job.step("download", "skipped", "爱快拒绝重新拉取")
                        job.step("update", "failed", "未能切到 %s" % new_tag)
                        job.step("verify", "skipped", "容器保持原状")
                        self._remember_if_known(digest_key, upstream, upstream_src)
                        job.log(
                            "❌ %s 没能切到 %s：它在爱快上的镜像记录（%s）已经同时挂着 "
                            "%s 和 %s 两个标签，且是**同一份内容** —— 改成 %s:%s 只是换个"
                            "标签，容器不会真的变新。而爱快认为「%s」本地已存在、拒绝重新"
                            "拉取（已存在相同内容），新内容也就永远进不来。\n"
                            "   请到爱快后台「Docker → 本地镜像」里删掉 %s 这条记录后重试；"
                            "或者改成更新到具体版本号 tag。"
                            % (c["name"], new_tag, own_tag, cur_tag, new_tag, iname,
                               new_tag, iname, iname),
                            level="error")
                        failures.append(c["name"])
                        continue

            if already:
                job.log("镜像 %s:%s 已在本地，但容器当前挂在 %s 上 → 跳过下载，直接重启切过去"
                        % (iname, new_tag, cur_tag),
                        level="warn")

            # 4. 等下载（镜像本来就在本地时无需等待）
            if already:
                job.step("download", "done", "镜像已在本地，跳过等待")
            else:
                job.step("download", "running", "等待镜像落地")
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

    # -- 搬运：把 latest 落后的内容弄进本地 ---------------------------------
    def _wait_running(self, ik, name, want_tag, tries=8, gap=2):
        """等容器起来并确认它真的挂在 ``want_tag`` 上，返回 ``(ok, 说明)``。

        只看 state=running 是不够的：爱快的 update 是异步的，容器自身的 image
        字段常常不带 tag，重建了却仍跑旧镜像的情况实测出现过（lucky）。
        """
        after, owner = None, None
        for _ in range(tries):
            time.sleep(gap)
            after = {x["name"]: x for x in A.get_containers(ik)}.get(name)
            if after and after.get("state") == "running":
                break
        if not after:
            return False, "更新后查不到容器"
        if after.get("state") != "running":
            return False, "容器状态异常（%s）" % (after.get("status") or "未知")
        for _ in range(4):
            owner = A.image_owner(ik, name)
            if owner and A.tag_matches(owner.get("tag"), want_tag):
                return True, after.get("status") or "running"
            time.sleep(2)
        ok_image = A.tag_matches(A.split_image(after.get("image") or "")[2], want_tag)
        if ok_image:
            return True, after.get("status") or "running"
        return False, "仍挂在 %s 上，没切到 %s" % (
            (owner or {}).get("tag") or "未知", want_tag)

    def _hoist_latest(self, job, ik, ctx, c, insp, repo, ns, iname, new_tag,
                      target_cfg, upstream_src, pull_t):
        """爱快拒绝重拉 latest 时，绕道把最新内容搬进本地。

        爱快判断「要不要下载」只看**同名同 tag 的本地记录在不在**，不比对内容：
        只要 ``repo:latest`` 这条记录还在，install 就一律回「已存在相同内容」，
        哪怕它是半年前的镜像。于是 latest 容器一旦落后就再也刷不新。

        可行路径（每一步都在真机上验证过）：
          1. 在上游 tag 列表里找一个**内容等于 latest 内容**的纯版本号 tag，拉它；
          2. 把容器切到这个版本号 tag（内容已经是最新的，服务不降级）；
          3. 删掉旧的 ``repo:latest`` 记录（此时已无容器占用它）；
          4. 重新 install latest —— 同名记录不在，爱快这才真的下载；
          5. 把容器切回 latest。

        任何一步失败就停下并如实报告。最坏结果是容器停在那个版本号 tag 上 ——
        内容一样新，只是不再「跟随 latest」，服务不受影响。

        返回 ``(ok, message)``。
        """
        name = c["name"]
        job.log("⚠ %s 本地 %s:%s 的内容落后于上游，而爱快拒绝重新拉取"
                "（它只按「同名同 tag 的记录在不在」判断，不比对内容）"
                % (name, iname, new_tag), level="warn")
        job.log("   → 改为「搬运」：拉一个内容相同的版本号 tag，把容器切过去、删掉旧的 "
                "%s 记录，再重新拉 %s 并切回来" % (new_tag, new_tag))

        # 1. 找同内容的版本号 tag
        job.step("download", "running", "寻找内容相同的版本号 tag")
        try:
            regs = A.resolve_registries(ik, self.cfg, job.log)
            vt, vreg = A.find_version_tag(regs, repo, target_cfg, ctx,
                                          self.cfg["timeout"], skip=(new_tag,))
        except Exception as e:                              # noqa: BLE001
            vt, vreg = "", ""
            job.log("查询上游 tag 列表失败：%s" % e, level="warn")
        if not vt:
            job.step("download", "failed", "上游没有内容相同的版本号 tag")
            job.step("update", "skipped", "未改动容器")
            job.step("verify", "skipped", "容器保持原状")
            job.log("❌ 找不到与 %s 内容相同的版本号 tag（上游可能只推 latest，"
                    "也可能加速源不提供 tag 列表），没法搬运。\n"
                    "   请到爱快后台「Docker → 本地镜像」删掉 %s 这条记录后重试；"
                    "或者用「指定版本」填一个明确的版本号 tag。"
                    % (new_tag, iname), level="error")
            return False, "找不到可用的版本号 tag"
        job.log("✓ 找到 %s:%s，内容与 %s:%s 相同（命中 %s），开始搬运"
                % (repo, vt, repo, new_tag, vreg or upstream_src or "上游"))

        # 2. 拉这个版本号 tag（它是独立记录，爱快会真的下载）
        job.step("download", "running", "拉取 %s:%s" % (iname, vt))
        since = int(time.time())
        try:
            ik.call("docker_image", "install",
                    {"name": iname, "tag": vt, "image_logo": c.get("image_logo", ""),
                     "namespace": ns})
        except A.IKuaiError as e:
            if not A.is_already_exists(e):
                job.step("download", "failed", "拉取 %s:%s 失败" % (iname, vt))
                job.step("update", "skipped", "未改动容器")
                job.step("verify", "skipped", "容器保持原状")
                job.log("❌ 拉取 %s:%s 失败：%s" % (iname, vt, e), level="error")
                return False, "拉取版本号 tag 失败"
        done, note = A.wait_for_image(ik, repo, vt, since, pull_t, ctx)
        if not done:
            msg = note if isinstance(note, str) else "超时"
            job.step("download", "failed", "搬运用的 %s 未就绪" % vt)
            job.step("update", "skipped", "未改动容器")
            job.step("verify", "skipped", "容器保持原状")
            job.log("❌ %s:%s 未在 %ds 内落地（%s）。容器没动，仍是原来的 %s。"
                    % (iname, vt, pull_t, msg or "超时", new_tag), level="error")
            job.log("   若镜像较大或加速源慢，可在「设置」里把下载超时调大后重试。",
                    level="warn")
            return False, "搬运用的镜像没下载完"
        sig_vt = A.image_signature(ik, repo, vt)
        if A.norm_digest(sig_vt) and not A.same_content(sig_vt, target_cfg):
            job.log("⚠ %s:%s 落地后的内容指纹（%s）与上游 %s 对不上，可能源缓存滞后；"
                    "继续搬运，最后会再核一次" % (iname, vt, A.norm_digest(sig_vt)[:12],
                                                new_tag), level="warn")
        job.step("download", "done", "已备好 %s:%s" % (iname, vt))

        # 3. 容器临时切到版本号 tag
        job.step("update", "running", "临时切到 %s:%s" % (iname, vt))
        p_vt, _ = A.container_payload(c, insp, tag=vt)
        ik.call("docker_container", "update", p_vt)
        ok_vt, why = self._wait_running(ik, name, vt)
        if not ok_vt:
            job.step("update", "failed", "没能临时切到 %s" % vt)
            job.step("verify", "skipped", "容器可能停在 %s" % vt)
            job.log("❌ 容器没能切到 %s:%s（%s）。请到爱快后台核对容器状态 —— "
                    "搬运在第一步就停下了，latest 还没修好" % (iname, vt, why),
                    level="error")
            return False, why

        # 4. 删掉旧的 latest 记录（此刻没有容器占用它）
        job.log("容器已临时切到 %s:%s，现在删掉本地旧的 %s 记录" % (iname, vt, new_tag))
        okd, detail = A.del_local_image(ik, repo, new_tag)
        if not okd:
            job.step("update", "failed", "删不掉旧的 %s 记录" % new_tag)
            job.step("verify", "skipped", "容器停在 %s" % vt)
            job.log("❌ %s。容器现在跑 %s:%s（内容已是最新，服务正常），但 latest 的"
                    "旧记录还在，最新内容仍无法「跟随 latest」。\n"
                    "   请到爱快后台「Docker → 本地镜像」手动删掉 %s 后重试。"
                    % (detail, iname, vt, iname), level="error")
            return False, "删除旧记录失败"

        # 5. 重新拉 latest —— 同名记录已不在，这次会真的下载
        job.step("download", "running", "重新拉取 %s:%s" % (iname, new_tag))
        since2 = int(time.time())
        try:
            ik.call("docker_image", "install",
                    {"name": iname, "tag": new_tag, "image_logo": c.get("image_logo", ""),
                     "namespace": ns})
        except A.IKuaiError as e:
            if A.is_already_exists(e):
                job.log("⚠ 爱快仍认为 %s 已存在（旧记录可能没删干净）" % new_tag,
                        level="warn")
            else:
                job.step("download", "failed", "重新拉取失败")
                job.step("verify", "skipped", "容器停在 %s" % vt)
                job.log("❌ 重新拉取 %s:%s 失败：%s。容器现在跑 %s:%s，内容已是最新，"
                        "服务正常；latest 的旧记录已删，请重试一次更新。"
                        % (iname, new_tag, e, iname, vt), level="error")
                return False, "重新拉取 latest 失败"
        done2, note2 = A.wait_for_image(ik, repo, new_tag, since2, pull_t, ctx)
        sig_lt = A.image_signature(ik, repo, new_tag)
        if not done2:
            job.step("download", "failed", "latest 未就绪")
            job.step("verify", "skipped", "容器停在 %s" % vt)
            job.log("❌ 重新拉取 %s:%s 未在 %ds 内落地（%s）。容器现在跑 %s:%s，"
                    "内容已是最新，服务正常；latest 记录已重置，重跑一次更新即可跟上。"
                    % (iname, new_tag, pull_t, note2 if isinstance(note2, str) else "超时",
                       iname, vt), level="error")
            return False, "重新拉取 latest 未完成"
        if A.norm_digest(sig_lt) and not A.same_content(sig_lt, target_cfg):
            job.log("⚠ 新落地的 %s 内容指纹（%s）仍与上游 %s 不一致，可能命中了缓存不"
                    "同步的加速源" % (new_tag, A.norm_digest(sig_lt)[:12],
                                    A.norm_digest(target_cfg)[:12]), level="warn")
        job.step("download", "done", "%s:%s 已就绪" % (iname, new_tag))

        # 6. 容器切回 latest
        job.step("update", "running", "切回 %s:%s" % (iname, new_tag))
        p_lt, _ = A.container_payload(c, insp, tag=new_tag)
        ik.call("docker_container", "update", p_lt)
        ok_lt, why_lt = self._wait_running(ik, name, new_tag)
        if not ok_lt:
            job.step("update", "failed", "没能切回 %s" % new_tag)
            job.step("verify", "failed", why_lt)
            job.log("❌ 容器没能切回 %s:%s（%s）。镜像已经是最新的了，容器目前挂在 "
                    "%s:%s 上，请到爱快后台核对或再点一次更新。"
                    % (iname, new_tag, why_lt, iname, vt), level="error")
            return False, why_lt

        job.step("update", "done", "已切回 %s:%s" % (iname, new_tag))
        job.step("verify", "done", "搬运完成 · %s" % new_tag)
        job.log("✅ 搬运完成：%s 现在跑 %s:%s，内容为上游最新（%s）；本地 %s 记录已重置。"
                "搬运用的临时记录 %s:%s 保留着，可当回滚点，也可在爱快「本地镜像」里手动清掉。"
                % (name, iname, new_tag, A.norm_digest(target_cfg)[:12],
                   new_tag, iname, vt))
        return True, "已搬运到最新内容"

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

        job.step("query", "running", "查询上游内容指纹")
        # 用爱快自己配置的加速源去查，保证「检测的源」和「拉取的源」一致
        registries = A.resolve_registries(ik, cfg, job.log)
        job.log("查询上游用的加速源（来自爱快 Docker 服务设置）：%s" % "、".join(registries))
        results, seen = [], set()
        latest_cache = {}          # repo → latest digest，避免同一仓库重复查
        for c in cs:
            image = c.get("image") or ""
            repo, tag = A.repo_of(image), A.split_image(image)[2]
            key = "%s:%s" % (repo, tag)
            if key in seen:
                continue
            seen.add(key)
            img = local.find(repo, tag)
            local_ts, _src = local.pulled_at(img)
            # 本地镜像是「哪一份内容」的唯一依据：爱快记录的镜像 ID 就是
            # config blob 的 sha256，和上游 amd64 子清单里的 config.digest 同层。
            local_sig = str((img or {}).get("id") or "")
            item = {"container": c["name"], "image": key, "local": local_ts or "—",
                    "status": "unknown", "digest": "", "registry": "", "pinned": "",
                    "local_id": A.norm_digest(local_sig)[:12],
                    "upstream_id": "", "basis": ""}
            try:
                digest, up_cfg, reg = A.remote_platform_digest(
                    registries, repo, tag, ctx, cfg["timeout"])
                item["digest"], item["registry"] = digest[:19], reg
                item["upstream_id"] = A.norm_digest(up_cfg)[:12]
                item["status"], item["basis"] = self._judge(
                    local_sig, up_cfg, digest, key)
                # 容器锁在具体版本号 tag 上时（如 vaultwarden/server:1.37.3），该 tag 的
                # 上游内容是固定的，上面必然得出「latest」。但那不等于没有新版 ——
                # 该仓库的 latest 可能早就走远了。额外查一次 latest 作对比，把
                # 「锁着、但上游已经前进」这件事显式暴露出来，否则用户看到的就是
                # 「Docker Hub 明明有新版，idocker 却说已是最新」（实测踩过）。
                if A.is_version_tag(tag):
                    if repo not in latest_cache:
                        try:
                            latest_cache[repo] = A.remote_digest(
                                registries, repo, "latest", ctx, cfg["timeout"])[0]
                        except Exception as e:              # noqa: BLE001
                            latest_cache[repo] = ""
                            job.log("· 查询 %s:latest 失败：%s" % (repo, e), level="debug")
                    lv = latest_cache[repo]
                    if lv and lv != digest:
                        item["pinned"] = "ahead"
                # 逐个镜像都记一行，并带上命中的加速源 —— 事后回看历史时，
                # 「查到的是哪个源的什么结果」比只看到一句「检测完成」有用得多。
                st = item["status"]
                if st == "newer":
                    job.log("⬆ %s 上游有新版本（本地镜像 %s ≠ 上游 amd64 内容 %s，命中 %s）"
                            % (key, item["local_id"] or "?", item["upstream_id"] or "?",
                               reg), level="warn")
                elif st == "latest":
                    job.log("✓ %s 已是最新（本地镜像 %s == 上游 amd64 内容，命中 %s）"
                            % (key, item["local_id"], reg))
                elif st == "unknown":
                    if item["basis"] == "digest":
                        job.log("· %s 取不到上游内容指纹，只能按清单 digest 记基准"
                                "（命中 %s）" % (key, reg), level="warn")
                    else:
                        job.log("· %s 首次检测，已记下上游指纹 %s（命中 %s）"
                                % (key, item["upstream_id"] or digest[:19], reg))
                if item["pinned"] == "ahead":
                    job.log("⚑ %s 锁定在 %s，但 %s:latest 已更新"
                            "（可用行内「指定版本」填 latest 切回跟随）"
                            % (key, tag, repo), level="warn")
                # ⚠️ 只有「确认已是最新」才刷新基准。
                # 旧代码这里无条件覆盖，于是「有新版本」只活一个检测周期 ——
                # 下次检测拿新 digest 跟自己比当然相等，报告自我消化掉，
                # 用户看到的就是「明明有新版本却说已是最新」（线上踩过）。
                if st != "newer":
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
        pinned = [r["container"] for r in results if r.get("pinned") == "ahead"]
        job.log("检测完成，%d 个镜像有新版%s"
                % (len(newer), ("：" + "、".join(newer)) if newer else ""))
        if pinned:
            job.log("另有 %d 个容器锁定在固定版本 tag，latest 已更新：%s"
                    % (len(pinned), "、".join(pinned)), level="warn")
        return True, "已检查 %d 个镜像" % len(results)

    # -- 上次检测结果（页面刷新后仍能显示上游状态）--------------------
    def _last_check_path(self):
        return os.path.join(self.data_dir, "last_check.json")

    def _save_last_check(self, results):
        items = {}
        for r in results:
            items[r["image"]] = {"status": r["status"], "digest": r.get("digest", ""),
                                 "registry": r.get("registry", ""), "local": r.get("local", ""),
                                 "pinned": r.get("pinned", ""),
                                 "local_id": r.get("local_id", ""),
                                 "upstream_id": r.get("upstream_id", ""),
                                 "basis": r.get("basis", "")}
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
    def _judge(self, local_sig, up_cfg, upstream, key):
        """判定「本地这份镜像 vs 上游」，返回 ``(status, basis)``。

        ``basis`` 说明这个结论有多硬：

        - ``sig``    —— 拿本地镜像 ID 和上游 amd64 子清单的 ``config.digest`` 直接
          比。这是唯一可信的口径：爱快说「已存在相同内容」时压根没比对过内容，
          拿它当判据就会「永远已是最新」（mihomo 线上事故）。
        - ``digest`` —— 源没给清单正文、取不到 config digest，只好退回「上游清单
          digest 有没有变」。能发现上游动过，但不能确认本地真的落后。
        - ``first``  —— 首次检测、什么都没有，无从判断。
        """
        if A.norm_digest(local_sig) and A.norm_digest(up_cfg):
            return ("latest" if A.same_content(local_sig, up_cfg) else "newer"), "sig"
        old = self._seen_digest(key)
        if old is None:
            return "unknown", "first"
        return ("newer" if old != upstream else "latest"), "digest"

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
