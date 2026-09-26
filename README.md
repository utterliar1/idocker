# 爱快 Docker 容器自动更新工具（4.0 原生 API 版）

[![ci](https://github.com/utterliar1/idocker/actions/workflows/ci.yml/badge.svg)](https://github.com/utterliar1/idocker/actions/workflows/ci.yml)
[![ghcr](https://img.shields.io/badge/ghcr.io-utterliar1%2Fidocker-blue)](https://github.com/utterliar1/idocker/pkgs/container/idocker)

直接调用爱快自己的 `POST /Action/call` 接口完成容器更新：**不依赖任何漏洞、不需要 SSH、
不需要提权 root、也不会脱离爱快的编排层**（爱快后台里看到的配置始终一致）。

接口细节全部记录在 [`API_REFERENCE.md`](API_REFERENCE.md)。

> **懒得看代码？** 带可视化界面的 Web 版镜像已自动构建发布在
> `ghcr.io/utterliar1/idocker`，一段 compose 就能在 NAS 上跑起来，
> 见 [`webapp/README.md`](webapp/README.md)。

---

## 一句话原理

爱快 4.0 的容器界面背后只有两个动作，工具做的就是把它自动化：

```
① docker_image.install    拉取新版本镜像
② docker_container.update 用原配置 + 新 tag 就地更新容器
```

关键是 **② 的配置不用你手填**——直接从 `docker_container show` / `inspect` 把现有容器的
挂载、环境变量、启动命令、网络、IP、自启状态原样读回来，只把 tag 换掉。

```
读容器现有配置 ─┐
                ├──▶ docker_container.update ──▶ 容器用新镜像重启，配置一字不改
拉取新版本镜像 ─┘
```

---

## 目录

| 文件 | 作用 |
|---|---|
| `ikuai_docker_updater.py` | 主程序，纯标准库无需安装依赖 |
| `API_REFERENCE.md` | 爱快 4.0 Docker 接口完整参考（实测） |
| `mine_ikuai_api.py` | 接口挖掘工具，固件升级后用来重新提取接口 |
| `capture-ikuai-api.js` | 浏览器抓包脚本（3.7 或接口大改时的兜底手段） |
| `config.example.json` | 可选配置（默认直接读 `.env`，不需要它） |
| `state.json` | 自动生成，记录上次检测到的上游 digest（已 gitignore） |
| `webapp/` | **Web 可视化版**，Docker / 飞牛部署用，见 `webapp/README.md` |
| `_test/` | 本地回归测试（假爱快 + DOM 桩，零依赖），CI 里跑的就是它 |
| `deploy_webapp.py` | 一键把 `webapp/` 同步到飞牛并重建容器 |
| `.github/workflows/ci.yml` | CI：验证 → 构建推 ghcr.io → 冒烟测试 |

---

## 快速开始

凭据放在上层的 `.env` 里就行，脚本会自己读：

```ini
ikuai_url=192.168.123.1
ikuai_id=wlup
ikuai_pw=你的密码
```

先看一眼现状（这三条都是只读的，随便跑）：

```bash
python ikuai_docker_updater.py list      # 容器列表
python ikuai_docker_updater.py server    # Docker 服务设置 + 镜像加速源
python ikuai_docker_updater.py images    # 本地镜像（含拉取时间）
```

---

## 命令

| 命令 | 说明 | 读写 |
|---|---|---|
| `list` | 列出容器：名称 / 镜像 / 网卡 / IP / 状态 | 读 |
| `images` | 列出本地镜像：tag / 大小 / 拉取时间 / 被几个容器使用 | 读 |
| `tags <镜像>` | 查某个镜像有哪些可用版本，如 `tags gdy666/lucky` | 读 |
| `server` | Docker 版本、容器/镜像统计、服务设置、镜像加速源 | 读 |
| `check` | 比对上游镜像 digest，标出哪些容器有新版本 | 读 |
| `update <名称...>` | 拉新镜像 + 更新容器 | **写** |
| `raw '<json>'` | 任意调用，排查用 | 看情况 |

通用参数：`-n/--dry-run` 只打印请求不执行（读操作照常）、`-y/--yes` 跳过确认、`-v` 详细输出。

`update` 额外参数：

| 参数 | 说明 |
|---|---|
| `--tag <t>` | 指定目标版本，默认沿用容器当前的 tag |
| `--wait <秒>` | 等待镜像下载完成的秒数，默认 180 |
| `--force` | 下载未确认完成也继续更新容器（不建议） |
| `--no-notify` | 本次不发通知 |

---

## 典型用法

### 1. 先检测，不改任何东西

```bash
python ikuai_docker_updater.py check
```

```
容器           镜像                             上游状态     本地拉取于     上游digest
--------------------------------------------------------------------------------
vaultwarden  vaultwarden/server:1.37.3      已是最新     09-23 23:00   sha256:1587c45feaa4
flatnas      qdnas/flatnas:latest           已是最新     06-15 20:02   sha256:3006810eee8b
lucky        gdy666/lucky:latest            ⬆ 有新版本   未安装        sha256:94bef557d1a8
```

### 2. 干跑，确认要发出的请求

```bash
python ikuai_docker_updater.py --dry-run -v update lucky
```

会打印完整的 `docker_container.update` 请求体，逐字段核对挂载、IP、命令有没有跑偏。

### 3. 正式更新

```bash
python ikuai_docker_updater.py update lucky            # 单个
python ikuai_docker_updater.py update lucky flatnas    # 多个
python ikuai_docker_updater.py -y update lucky         # 跳过确认，适合脚本
```

### 4. 锁定版本升级（比如 lucky 2.20.2 → 2.27.2）

```bash
python ikuai_docker_updater.py tags gdy666/lucky          # 先看有哪些版本
python ikuai_docker_updater.py update lucky --tag 2.27.2
```

---

## 挂定时任务

### Windows 计划任务

```bat
schtasks /create /tn "iKuai容器自动更新" /sc daily /st 04:00 ^
  /tr "C:\Path\python.exe D:\Documents\WorkBuddy\爱快\ikuai-docker-auto\ikuai_docker_updater.py -y update lucky flatnas"
```

### Linux / NAS crontab

```cron
# 每天中午只检测并发通知
0 12 * * * cd /volume1/idocker && /usr/bin/python3 ikuai_docker_updater.py check >> check.log 2>&1

# 每天凌晨 4 点自动更新指定容器
0 4 * * * cd /volume1/idocker && /usr/bin/python3 ikuai_docker_updater.py -y update lucky flatnas >> update.log 2>&1
```

### 通知

在 `.env` 同级放 `config.json`（参考 `config.example.json`）填：

```json
"notify_webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx"
```

企业微信机器人、Server酱直接用；钉钉的 body 结构不同，改 `notify_body` 即可。

---

## 首次使用建议

`docker_container.update` 会**重启容器**，所以第一次请这样验证：

1. 挑一个**不关键、能接受短暂中断**的容器
2. `--dry-run -v` 看请求体，重点核对：挂载路径、环境变量、启动命令、IP、自启
3. 真正跑一次，然后到爱快后台核对容器配置有没有变化
4. 确认没问题，再放开给其他容器和定时任务

---

## 常见问题

**Q：`update` 报「未在 180s 内确认下载完成」**

镜像还在后台拉（大镜像走国内加速源也可能很慢）。加 `--wait 600` 再试，或者先手动在爱快
后台把镜像拉好，然后 `update --force` 直接做容器更新。**不要盲目加 `--force`**，
否则容器可能被旧镜像重建。

**Q：端口映射那一栏是空的**

接口只返回容器侧端口（`PrivatePort`），不返回宿主端口。爱快的 `doc_docker` 网络
（`192.168.3.0/24`，网关是路由器 LAN 地址）是**路由直连**的，本来就访问 `容器IP:端口`，
不需要映射。如果你确实做过端口映射，更新前请在爱快后台截图记下来。

**Q：`check` 一直是「首次记录」**

`state.json` 是刚建的，第一次跑只记录基线，第二次开始才能判断"有没有新版本"。

**Q：某个镜像一直「查询失败」**

单个 Docker 加速源常对部分镜像返回 403。编辑 `config.json` 的 `registries` 调换顺序或增删。
你路由器上现在配置的三个加速源是：`docker.1ms.run`、`docker.m.daocloud.io`、`docker.xuanyuan.me`。

**Q：容器更新后标签/配置不对**

`update` 用的是"读现有配置 → 原样回填"的策略，理论上不会改配置。真出问题就到爱快后台
手工改回来（挂载目录里的数据一直都在，不会丢）。

**Q：爱快固件升级后接口变了怎么办**

```bash
python mine_ikuai_api.py -k docker
```

它会登录爱快、拉下全部前端 JS 分片、把 `/Action/call` 的调用点连同上下文导到 `_mine/`，
直接就能读出新的 `func_name` / `action` / `param`。把结果贴给我，我来更新工具。

---

## 风险与边界

- **`update` 会重启容器**，有短暂服务中断，建议放低峰期。
- **`add` / `update` 是异步的**：成功时爱快返回**空响应体**，不是 JSON。所以工具靠
  "之后重新查容器列表"来确认结果，而不是靠返回值。
- **API 是未公开接口**。3.7 → 4.0 之间改过一次（`func_name` 从 `docker` 拆成了
  `docker_container` / `docker_image` / `docker_server` 等）。固件升级后先跑 `--dry-run`。
- **凭据**：密码在 `.env` 里明文，脚本默认只读它、不外传，日志里的 `passwd` / `pass`
  已自动打码。别把 `.env` 提交到 Git。

---

## 附：其他路线（已评估，不推荐）

| 方案 | 结论 |
|---|---|
| 容器内跑 Watchtower 全自动重建 | 需要利用 `..` 路径穿越挂载宿主机 `docker.sock`；4.0 已在 UI 层修补，要手工改 `config.v2.json`；且 Watchtower 重建的容器会脱离爱快编排层，后台状态会乱。**不推荐** |
| 提权 root 后用 docker CLI / compose | 非官方支持，固件升级可能失效，改错容易把路由搞挂。**不推荐** |
| 3.7 及更早固件的 API 路线 | 用 `capture-ikuai-api.js` 抓一次包，照着 `API_REFERENCE.md` 的格式手工套模板 |
