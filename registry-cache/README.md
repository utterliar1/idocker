# 让飞牛替爱快拉镜像（pull-through cache 方案）

## 为什么需要它

爱快的 Docker 引擎**没有任何代理配置入口**——`docker_server` 的 `save` 参数只有
`{mirrors, workdisk, autostart, enabled, comment}`，没有 proxy，也没有 insecure-registries。
所以爱快只能走公共加速源，大镜像容易超时。

而飞牛具备爱快没有的能力：**能通过爱快上的 clash 出网**（实测见下）。于是把"下载"
这件事交给飞牛，爱快只从内网取。

```
爱快 docker_image.install
        │
        ▼
飞牛 registry 缓存 ──(经 clash 192.168.3.4:7890)──▶ Docker Hub
        │ 命中缓存则直接内网返回
        ▼
爱快本地镜像库
```

**爱快侧无需改代码**：现有 webapp 的 `install` + `wait_for_image` 流程原样可用。

## 实测依据（2026-09-24，在飞牛上执行）

| 路径 | 结果 | 结论 |
|---|---|---|
| 飞牛 → `-x http://192.168.3.4:7890` → registry-1.docker.io | 401 / 2.38s | 通（401 = 已连上，仅缺 token） |
| 飞牛直连 registry-1.docker.io | 000 / 8s 超时 | 直连被墙 |
| 飞牛直连 docker.1ms.run | 401 / 0.17s | 加速源本身可用 |

爱快三个加速源也都是活的（1ms.run 0.17s / daocloud 0.10s / xuanyuan.me 0.95s），
所以**问题不在源挂了，而在公共源的带宽与限速**。本方案治的是这个本。

## 部署

```bash
# 在飞牛上
mkdir -p /vol1/1000/docker/registry-cache
# 把 docker-compose.yml 放进去
cd /vol1/1000/docker/registry-cache
docker compose up -d
docker logs --tail 20 ikuai-registry-cache
```

## 验证（按顺序做，每步都能独立判断）

**1. 缓存服务是否活着**

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5000/v2/
# 期望 200
```

**2. 它能否经代理拉到镜像**（第一次会慢，是真去 Docker Hub 拉）

```bash
curl -s -o /dev/null -w "%{http_code} %{time_total}s\n" \
  http://127.0.0.1:5000/v2/library/hello-world/manifests/latest
# 期望 200；再执行一次应该明显更快（命中缓存）
```

**3. 爱快能否从它拉**（关键，也是唯一未验证点）

在爱快后台 **Docker → 服务设置 → 镜像加速源**，把
`http://192.168.123.109:5000` **追加到列表最后**（保留原有三个源兜底），保存。
然后在爱快上拉一个小镜像试（用本仓库的命令行工具即可）：

```bash
python ikuai_docker_updater.py update --container <某个小容器> --dry-run   # 先干跑
```

或用爱快后台的「镜像」页面手动下载 `hello-world` 观察。

- ✅ 成功 → 说明爱快接受 http 内网 mirror，方案全线打通
- ❌ 失败（多半报 `http: server gave HTTP response to HTTPS client`）→ 爱快不接受
  http mirror，且它没有 insecure-registries 入口。此时只能给 registry 配
  **受信任的 HTTPS 证书**（需要域名 + Let's Encrypt，可复用飞牛上已有的 lucky），
  或者放弃本方案，回到"传输 tar + docker load"那条笨路。

## 回滚

改爱快 mirrors 是唯一有风险的写入操作，回滚很简单：**把追加的那条删掉、保存**即可。
原始值备份在此，出问题直接贴回去：

```
https://docker.1ms.run,https://docker.m.daocloud.io,https://docker.xuanyuan.me
```

飞牛侧回滚：`docker compose down`，缓存数据在 `data/` 目录，可直接删。

## 注意事项

- **磁盘占用**：镜像缓存会持续增长。`data/` 目录需要定期清理，或挂到空间充裕的盘。
- **单点依赖**：飞牛关机时，若 `mirrors` 里只剩它一个源，爱快将无法拉镜像。
  所以**保留原有三个源**，只把飞牛追加在末尾。
- **不是万灵药**：只有 Docker Hub（`docker.io`）的镜像能走这条缓存。
  从其他 registry 拉的镜像（如 `ghcr.io`、`quay.io`）需要另配一个
  `REGISTRY_PROXY_REMOTEURL` 指向对应上游，一个 registry 实例只能代理一个上游。
- **首次仍慢**：缓存是"第二次才快"。第一次拉取依旧受上游速度和代理带宽限制。
