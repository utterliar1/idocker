# 代码审查报告 · v1.4.0

日期：2026-09-27
范围：`webapp/app/*.py`（后端 2400 行）、`webapp/app/static/*`（前端 2223 行）
方法：通读 + **实证复现**（起假爱快 + 真 server，把每个怀疑点真跑一遍）

> 本次审查**未改动任何代码**。下面 P0 的 4 条全部经端到端复现，不是静态推断。

---

## 〇、修复记录（v1.4.1，2026-09-27）

15 项已全部修复，并**每条都补了能抓到它的回归测试**，且逐条做了反向验证
（把修复改回旧写法，确认新测试会变红）。

| # | 结论 | 修复要点 | 抓它的测试（反向验证时变红） |
|---|---|---|---|
| P0-1 | ✅ 已修 | `wait_for_image` 改用 `tag_matches` | 18 多标签立即命中（旧写法 6.0s 超时失败） |
| P0-2 | ✅ 已修 | `jobs.py` 新增模块级 `_persist_lock`，三处读-改-写全程加锁 | 19 并发写 40 条不丢（旧写法落盘 4/40） |
| P0-3 | ✅ 已修 | `Job.dry` 随任务走，`/api/update` 不再写全局 `CFG["dry_run"]` | 20 干跑不污染全局（旧写法 2 项红） |
| P0-4 | ✅ 已修 | 新增 `_digest_eq()`，比对前统一 `.encode("utf-8")` | 17 中文密码可登录（旧写法 500 / Basic 失败） |
| P1-5 | ✅ 已修 | `t.start()` 包 try，失败时回滚 `_busy` 并从 `jobs`/`order` 摘除 | 21 互斥位回滚（旧写法 `is_busy()` 永久 True） |
| P1-6 | ✅ 已修 | 新增 `_check_busy`，检测任务并发时**复用**同一 job | 22 检测复用（直接构造在跑的 job 断言同一实例） |
| P1-7 | ✅ 已修 | `note_login_fail` 顺手清理过期记录 + `LOGIN_FAILS_MAX_KEYS` 限容 | 23 表大小有上限 |
| P1-8 | ✅ 已修 | 四处 `except: pass` → `_warn()` 写 stderr | 24 写盘失败有告警 |
| P2-9 | ✅ 已修 | `AUTH_USER` 有、`AUTH_PASS` 空 → **fail-closed** 拒绝一切登录 + 启动告警 | 17 空密码不放行 |
| P2-10 | ✅ 已修 | `_serve_static` 改用 `os.path.commonpath` | 27 兄弟目录也挡住 |
| P2-11 | ✅ 已修 | `_do_login` 自身包 try（`ApiError` 透传、其余 500 JSON） | 17 中文密码项（旧写法直接 500） |
| P2-12 | ✅ 已修 | 新增 `MAX_BODY_BYTES=1MiB`，`_read_body_bytes` 统一限长 | 25 超大请求体 413 |
| P2-13 | ✅ 已修 | SSL 关闭校验处补 MITM 风险注释（内网妥协，勿暴露公网） | ——（注释） |
| P2-14 | ✅ 已修 | `_csrf_ok`：带 `Origin` 时必须与 Host 同源（不带 Origin 的脚本照旧放行） | 28 跨站写请求 403 |
| P2-15 | ✅ 已修 | 前端 `pruneSelection()`：渲染前清掉已消失容器的选中键 | H 幽灵容器（旧写法仍显示「更新选中 (3)」） |

回归结果：后端 **131 项全绿**、前端时序 **24 项全绿**、前端 UX **39 项全绿**。
版本号 `1.4.0 → 1.4.1`。

---

## 一、结论摘要

| 级别 | 条数 | 性质 |
|---|---|---|
| **P0** | 4 | 功能失效 / 静默数据丢失，建议立即修 |
| **P1** | 4 | 健壮性，会导致服务不可用或状态悄悄丢失 |
| **P2** | 7 | 加固与代码卫生 |

**最值得注意的一件事**：这 4 个 P0 **全部通过了现有 110 项回归测试**。
原因不是测试写得差，而是三种典型的"测试看着绿、代码其实有病"（详见第四节）。

---

## 二、P0 · 建议立即修

### 1. 多标签镜像更新必然超时 —— 更新功能在某些容器上直接失效

**位置**：`webapp/app/ikuai_api.py:557` `wait_for_image()`

```python
if i.get("name") == repo and str(i.get("tag")) == str(tag):   # ← 精确匹配
```

爱快镜像条目的 `tag` 实测是**逗号分隔的多标签**，如 `"1.37.3,latest"`。
同一个文件里已有 `tag_matches()` 专门处理这种形态（`LocalImages` 也用它），
唯独这里漏用了。

**复现**（假爱快里 vaultwarden 镜像 tag 就是 `"1.37.3,latest"`）：

```
更新 vaultwarden 到 tag=latest
  done    登录爱快
  done    读取容器配置
  done    拉取新镜像        下载任务已下发
  failed  等待下载完成      超时          ← 60 秒后失败
  ok=False  处理 1 个容器，其中 1 个未成功：vaultwarden
```

镜像**早就躺在本地**（tag 字段里就有 `latest`），却因为字符串精确匹配不上，
被判定为"没下载完"，白等整个超时周期后失败。

**为什么测试没抓到**：`test_webapp.py` 更新用的是新 tag `1.9.0`
（本地不存在，走"新建独立镜像条目"分支），恰好绕开了命中多标签条目的路径。
`test_webapp.py:341` 甚至断言了 `tag_matches("1.37.3,latest", "latest") == True`
—— 说明作者知道要拆分，只是这处调用点漏了。

**修复**：改用 `tag_matches(i.get("tag"), tag)`，并补一条"更新到已存在的多标签
tag"的端到端用例。

---

### 2. `state.json` 无锁读-改-写 —— 并发下丢记录

**位置**：`webapp/app/jobs.py:606` `_remember_digest()`（及 `_save_history`、
`_save_last_check` 同一套写法）

三处都是「读整个文件 → 改 → 整个写回」，且**没有任何锁**。
`store.py` 自己有 `_lock`，`jobs.py` 这套持久化没有。

**复现**：

```
30 个线程各写 1 个不同的 key → 落盘 1 条，丢失 29 条
```

**后果**：这个文件是定时任务判断"上游到底变没变"的基准。记录丢了就退化成
"没有历史记录" → 多做一次无谓的拉取 + 重启容器。
check 任务与 update 任务可以并发跑（见 P1-6），撞车概率不低，而且**静默发生**。

**修复**：模块级锁保护读-改-写（照 `store.py` 的 `_lock` 做法）。

---

### 3. 手动「干跑」污染全局 —— 之后的定时任务全部静默变空跑

**位置**：`server.py:573` 写入全局 + `jobs.py:328` 读取全局

```python
# server.py
CFG["dry_run"] = bool(body.get("dry_run"))     # 改的是全局配置
# jobs.py
dry = bool(cfg.get("dry_run"))                 # 定时任务执行时读同一个全局
```

**复现**：

```
干跑前 CFG['dry_run'] = False
提交一次 dry_run=True 的手动更新 → CFG['dry_run'] = True   ← 永久残留
```

**后果**：用户手动勾一次干跑，此后**所有定时任务触发的自动更新都变成演练**，
不真的更新任何容器，而界面上没有任何提示（定时任务面板仍显示"已完成"）。
用户以为有自动更新在保底，实际早就空转了。

**为什么测试没抓到**：`test_webapp.py:199-204` 自己用 `try/finally` 把
`CFG["dry_run"]` 复位了 —— 把生产代码的问题包装成了测试卫生问题。

**修复**：`dry_run` 跟着 Job 走（`Job` 增加 `dry` 字段），不再写全局 `CFG`；
`/api/update` 只把值传给本次任务。

---

### 4. 密码含非 ASCII → 登录直接 500 / Basic 认证永久失败

**位置**：`server.py:405`（`_do_login`）、`server.py:327`（`_auth_ok` 的 Basic 分支）

`hmac.compare_digest` 不接受含非 ASCII 字符的 `str`，直接抛
`TypeError: comparing strings with non-ASCII characters is not supported`。

**复现**：

```
AUTH_PASS = "密码123"，提交正确密码
  → _do_login 无 try 包裹 → TypeError 冒泡 → HTTP 500 + 服务端 traceback
  → _auth_ok 的 Basic 分支有 try/except → 吞掉异常 → 密码正确也认证失败
```

即：密码里有中文/emoji 时，登录页**永远登不进去**，curl/Basic 也**永远失败**。

**修复**：比对前统一 `.encode("utf-8")`（`compare_digest` 接受 `bytes`），
并给 `_do_login` 套上 try。

---

## 三、P1 · 健壮性

### 5. 任务线程起不来 → 更新功能永久不可用

**位置**：`jobs.py:190-205` `create()`

```python
self._busy = job.id
...
t = threading.Thread(target=self._run, args=(job,), daemon=True)
t.start()          # ← 没有 try
```

若 `t.start()` 抛异常（典型：`RuntimeError: can't start new thread`），
`_busy` 已置位但 `_run` 从未执行，**永远不会有人清它**。

**复现**：

```
线程启动失败 → mgr.is_busy() = True
再提交任何更新任务 → BusyError「已有更新任务在执行，请等它跑完再试」
```

一次偶发的线程创建失败 = 更新功能永久失效，**只能重启容器恢复**。

**修复**：`t.start()` 包 try，失败时回滚 `_busy` 并从 `jobs`/`order` 摘除，再抛错。

---

### 6. check 任务没有并发保护

`create()` 只对 `kind == "update"` 做互斥，check 可以无限并发。

**复现**：同时提交 3 个 `/api/check` → 3 个全部被接受。

**后果**：多个标签页、或"自动检测"与手动点击重叠时，会重复打爱快和加速源；
叠加 P0-2 的写盘竞态，记录更容易丢。

**修复**：给 check 也加互斥（同一时刻只允许一个），或返回已有任务的 id 复用。

---

### 7. `_login_fails` 字典只增不减

**位置**：`server.py:106`

每个 IP 一条记录，成功登陆时清自己的那条，但从不做全局清理。
内网场景 IP 有限，问题不大；但部署在反代后面时**所有请求都是同一个 IP**，
5 次失败会锁住全体用户 60 秒（当前注释已说明这是刻意不解析 XFF 的取舍）。

**修复**：定期清理过期记录（或按 LRU 限容）。

---

### 8. 写盘失败被静默吞掉

**位置**：`store._save()`、`jobs._save_history()`、`_save_last_check()`、
`_remember_digest()` —— 四处都是 `except Exception: pass`。

磁盘满 / 权限问题时，界面显示"保存成功"，**重启后设置与历史全丢**，
用户毫无察觉。

**修复**：至少写 stderr，更好的做法是让 API 返回错误让前端提示。

---

## 四、P2 · 加固与卫生

| # | 问题 | 位置 |
|---|---|---|
| 9 | 只设 `AUTH_USER`、不设 `AUTH_PASS` → **空密码即可登录**（已实证：`compare_digest("", "")` 为真） | `server.py:110/405` |
| 10 | `_serve_static` 用 `startswith(STATIC_DIR)` 而非 `commonpath`（当前无法逃逸，属加固） | `server.py:466` |
| 11 | `_do_login` 未捕获异常（非法 `Content-Length` → 500 + traceback）；它在 `do_POST` 的 try 之外 | `server.py:384/554` |
| 12 | 无请求体大小限制，`rfile.read(length)` 可被超大 `Content-Length` 打满内存 | `server.py:457` |
| 13 | SSL 校验全关（`check_hostname=False` + `CERT_NONE`）—— 内网自签必要，但 MITM 风险应在注释里写明 | `ikuai_api.py:145` |
| 14 | 无 CSRF token，写操作靠 `SameSite=Lax` 兜底（基本够用，但值得知晓） | 全局 |
| 15 | 前端 `state.selected` 残留已删除容器的键，点"更新选中"会对不存在的容器发请求 | `app.js` |

---

## 五、方法论：为什么 110 项测试全绿还会有 4 个 P0

审查时优先怀疑这三类地方：

| 类型 | 本次实例 |
|---|---|
| 测试**恰好绕开**出问题的分支 | P0-1：用新 tag 而非 `latest`，绕开了多标签条目匹配 |
| 测试**自己善后**，掩盖生产缺陷 | P0-3：测试手动复位全局状态 |
| 测试只覆盖**单线程** | P0-2：并发写盘丢记录 |

**反向验证**（故意改坏看测试红不红）能证明"测试盯着这个行为"，
但**证明不了没有未知缺陷**。下次审查照这个顺序来：
先问"哪些分支从没被走到"，再问"哪些全局状态要测试自己打扫"，
最后问"哪些可变状态被多线程共享"。

---

## 六、建议的修复顺序

1. **P0-1**（多标签超时）—— 一行改动，影响面最大：某些容器根本更新不了
2. **P0-3**（干跑传染）—— 改动小，但会让自动更新静默失效，最阴
3. **P0-2**（写盘竞态）+ **P0-4**（非 ASCII 密码）—— 前者加锁，后者统一编码
4. **P1-5 / P1-6** —— 互斥与异常安全
5. 其余按性价比排

每修一条，都应**同时补一条能抓到它的回归测试**，
并按老规矩做反向验证（改回旧写法，确认新测试会红）。
