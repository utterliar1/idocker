# 爱快 4.0 Docker 接口参考（实测）

> 来源：从爱快 4.0 前端打包产物 `docker-BgGMTJ9H.js`（构建于 2026/09/09）中提取，
> 并在真实设备上逐条调用验证。
> 固件：iKuaiOS 4.x / Docker 28.3.3 / API 1.51

---

## 1. 通用约定

| 项 | 值 |
|---|---|
| 登录 | `POST /Action/login` |
| 业务调用 | `POST /Action/call` |
| 请求体 | `{"func_name":"...","action":"...","param":{...}}` |
| 成功 | `{"code":0,"message":"Success","results":{...}}` |
| 参数不合法 | `{"code":3001,"message":"请求参数不合法","errors":"<具体字段>"}` |
| 模块不存在 | `{"code":1003,"message":"Access denied"}` |

登录请求体：

```json
{
  "username": "admin",
  "passwd": "<md5(密码) 32位小写>",
  "pass": "<base64('salt_11' + 密码)>",
  "remember_password": ""
}
```

成功后返回 `Set-Cookie: sess_key=...`，后续请求带上即可。

**注意**：`docker_container` 的 `add` / `update` 是**异步**的——成功时返回**空响应体**（不是 JSON）。
所以不要靠响应判断结果，要在之后重新查容器列表确认。

---

## 2. func_name 清单

| func_name | 用途 |
|---|---|
| `docker` | Docker 插件本身（安装状态、磁盘） |
| `docker_server` | Docker 服务设置（镜像加速源、存储盘、启停） |
| `docker_container` | 容器增删改查、启停 |
| `docker_image` | 镜像搜索 / 下载 / 删除 / tag 查询 |
| `docker_network` | 网络增删查 |
| `docker_compose` | Compose 项目 |

---

## 3. docker_server —— 服务设置

```json
{"func_name":"docker_server","action":"show","param":{"TYPE":"overview"}}
```
返回总览：`status` / `docker_version` / `api_version` / `overview.{containers,compose,images}` + `running[]`（含每个容器的 cpu、内存、IP、挂载、端口）。

```json
{"func_name":"docker_server","action":"show","param":{"TYPE":"data"}}
```
返回服务设置（**镜像加速源在这里**）：

```json
{"results":{"data":[{
  "id":1,"enabled":"yes","workdisk":"docker","autostart":1,
  "mirrors":"https://docker.1ms.run,https://docker.m.daocloud.io,https://docker.xuanyuan.me",
  "comment":",daocloud,xuanyuan"
}]}}
```

| action | param | 说明 |
|---|---|---|
| `show` | `{"TYPE":"overview"}` | 总览 |
| `show` | `{"TYPE":"data"}` | 服务设置 |
| `show` | `{"TYPE":"disks"}` | 可用磁盘 |
| `save` | `{mirrors, workdisk, autostart, enabled, comment}` | 保存设置 |
| `up` / `down` | `{}` | 启动 / 停止 Docker 服务 |
| `install` | `{src_dir}` | 安装 Docker 插件 |

---

## 4. docker_container —— 容器

### 4.1 只读

| action | param | 说明 |
|---|---|---|
| `show` | `{"TYPE":"data"}` | 容器列表 |
| `show` | `{"TYPE":"image,network,memavailable"}` | 新建容器表单的下拉数据 |
| `show` | `{"TYPE":"inspect","id":<id>}` | 容器配置详情 |
| `show` | `{"TYPE":"source_used","id":<id>}` | 实时 CPU/内存/速率 |
| `show` | `{"TYPE":"log","id":<id>,"starttime":<ts>,"endtime":<ts>}` | 日志 |

容器列表单条记录（实测字段）：

```json
{
  "name": "lucky",
  "id": "8bd9392d830fdaf0537bf797de6347e67977f2e3cbd86307a2e852b59b104aaf",
  "image": "gdy666/lucky:latest",          // 注意：带 tag
  "state": "running",
  "status": "Up 10 days",
  "interface": "doc_docker",               // 网络接口名
  "ipaddr": "192.168.3.2", "ip6addr": "2408:823c:2010:85::3",
  "gateway": "192.168.3.1", "ip6gateway": "2408:823c:2010:85::1001",
  "mounts": "/docker/ikuai/lucky:/goodluck",        // "源:目标" 逗号分隔
  "env": "",                                        // "K=V" 逗号分隔
  "cmd": "-c%20/goodluck/lucky.conf%20-runInDocker", // 已编码，空格=%20
  "ports": [{"Type":"tcp","PrivatePort":16601}],
  "auto_start": "1", "created": 1782477553,
  "memory": 4117090304, "memused": 318754816,
  "cpu_used": "0.45%", "down_speed": 85, "up_speed": 81,
  "image_logo": "", "comment": "", "exitcode": 0, "mac": ""
}
```

`TYPE:"inspect"` 补充了表单需要的原始值：

```json
{"inspect":[{
  "id":"...","name":"lucky","image":"gdy666/lucky:latest",
  "cmd":["-c","/goodluck/lucky.conf","-runInDocker"],
  "mounts":[{"Source":"/docker/ikuai/lucky","Destination":"/goodluck","Type":"bind","RW":true}],
  "env":["PATH=...","TZ=Asia/Shanghai"],
  "memory":0,"file_size":"1048576","cpushares":0,
  "auto_start":"1","interface":"doc_docker","appname":"","ports":{}
}]}
```

### 4.2 写操作

| action | param | 说明 |
|---|---|---|
| `add` | 容器参数对象 | 新建容器 |
| `update` | 容器参数对象（含 `id`） | 修改容器（**不会脱离爱快编排层**） |
| `del` | `{"id":<id>}` | 删除容器 |
| `up` / `down` | `{"id":<id>}` | 启动 / 停止 |
| `EXPORT` | `{"id":<id>}` | 导出 |

**容器参数对象**（`add` / `update` 同一套字段）：

| 字段 | 类型 | 说明 | 示例 |
|---|---|---|---|
| `name` | str | 容器名 | `"lucky"` |
| `image` | str | 镜像名，**不含 tag** | `"gdy666/lucky"` |
| `tag` | str | 版本 | `"latest"` |
| `interface` | str | 网络接口 | `"doc_docker"` |
| `ipaddr` / `ip6addr` | str | 固定 IP，留空自动分配 | `"192.168.3.2"` |
| `mounts` | str | `"源:目标"`，逗号分隔 | `"/docker/ikuai/lucky:/goodluck"` |
| `ports` | str | `"宿主:容器/协议"`，逗号分隔 | `"8080:80/tcp,443:443/tcp"` |
| `env` | str | `"K=V"`，逗号分隔 | `"TZ=Asia/Shanghai,TOKEN=abc"` |
| `cmd` | str | 启动命令（空格写成 `%20`） | `"-c%20/goodluck/lucky.conf"` |
| `comment` | str | 备注 | `""` |
| `auto_start` | int | 开机自启 | `0` / `1` |
| `memory` | int | 内存上限，**字节**；0 = 不限制 | `0` |
| `file_size` | int | 磁盘上限，**字节** | `1048576` |
| `file_num` | int | 文件数上限 | `1` |
| `cpushares` | int | CPU 权重 | `0` |
| `enabled` | str | 是否启用 | `"yes"` / `"no"` |

其中 `ports` / `mounts` / `env` 在表单里是数组，提交前才拼成字符串，拼法就是：

```js
ports = ports.filter(非空).map(p => `${p.local_port}:${p.container_port}/${p.protocol}`).join(",")
mounts = mounts.filter(非空).map(m => `${m.local_path}:${m.load_path}`).join(",")
env    = env.filter(非空).map(e => `${e.key}=${e.value}`).join(",")
```

---

## 5. docker_image —— 镜像

| action | param | 说明 |
|---|---|---|
| `show` | `{"TYPE":"data"}` | 本地已有镜像 |
| `show` | `{"TYPE":"image","page":1,"page_size":30,"keyword":"lucky"}` | 在线搜索（爱快自家的镜像库） |
| `show` | `{"TYPE":"tag","name":"lucky","namespace":"gdy666","page":1,"page_size":100}` | 查询可用 tag |
| `install` | `{name,tag,image_logo,namespace}` | **下载镜像**（异步） |
| `del` | `{...}` | 删除镜像 |

本地镜像单条：

```json
{"name":"qdnas/flatnas","tag":"latest","id":"bec730a8...","size":72652154,
 "created":1779285878,"install":1781524978,"status":4,"container_count":1,
 "containers":[{"state":"running","id":"...","name":"flatnas"}],
 "image_logo":"https://static.1ms.run/image/avatar/default.png"}
```

在线搜索结果单条：

```json
{"id":24893,"namespace":"qdnas","name":"flatnas","registry":"docker.io",
 "repository_name":"qdnas/flatnas","pull_domain":"docker.1ms.run",
 "logo_url":"https://static.1ms.run/image/avatar/...","description":"...",
 "pull_count":123,"star_count":4,"is_official":false,"last_modified":"..."}
```

**namespace / name 的拆法**：镜像全名 `namespace/name:tag`，取 `image` 字段按最后一个 `:` 拆 tag，
剩下按最后一个 `/` 拆——最后一段是 `name`，前面是 `namespace`。

- `gdy666/lucky:latest` → `namespace=gdy666`, `name=lucky`, `tag=latest`
- `qdnas/flatnas:latest` → `namespace=qdnas`, `name=flatnas`, `tag=latest`
- `vaultwarden/server:1.37.3` → `namespace=vaultwarden`, `name=server`, `tag=1.37.3`
- `nginx:latest` → `namespace=library`, `name=nginx`, `tag=latest`

`install` 返回 `{"code":0}` 表示已提交（后台下载）；若镜像已存在会返回错误，`errors` 为 `image was installed`。

---

## 6. docker_network / docker_compose

```json
// 网络列表
{"func_name":"docker_network","action":"show","param":{"TYPE":"data"}}
// → {"data":[{"id":"...","name":"doc_docker","subnet":"192.168.3.0/24",
//            "gateway":"192.168.3.1","subnet6":"...","gateway6":"...",
//            "comment":"","containers":["lucky","clash",...]}]}

{"func_name":"docker_network","action":"add","param":{"name","subnet","gateway","subnet6","gateway6","comment"}}
{"func_name":"docker_network","action":"del","param":{"id"}}
```

```json
{"func_name":"docker_compose","action":"show","param":{"TYPE":"data"}}            // 项目列表
{"func_name":"docker_compose","action":"show","param":{"TYPE":"details","id":1}}  // 单项目详情
{"func_name":"docker_compose","action":"add","param":{...}}                       // 新建项目
{"func_name":"docker_compose","action":"up"/"down"/"restart"/"clean","param":{"id":1}}
```

---

## 7. 字符串编码（重要）

爱快提交前会对字符串做一次**字符替换**（不是加密，可逆）：

```
% → %25     空格 → %20     " → %22     ` → %60     $ → %24
& → %26     ; → %3B        \ → %5C     | → %7C     ' → %27
```

（另一张扩展表还会处理 `!` `(` `)`，用在特定场景）

规则是**逐字符映射**，所以不会二次编码：`%` 只会变成一次 `%25`。

**这一点的实际影响**：
- 从接口**读回来的** `cmd` / `env` 已经是编码后的形式（例如 `lucky` 的 `cmd` 是
  `-c%20/goodluck/lucky.conf%20-runInDocker`）
- **回填时不要再编码一次**，否则 `%20` 会变成 `%2520`，容器起来就是错的
- 只有你**自己手写**的新值时才需要编码

---

## 8. 两个实测踩坑记录

### 8.1 静态资源必须带 `Accept-Encoding`

抓取前端 JS 时，不带 `Accept-Encoding: gzip` 会直接 **404**，带上才返回内容且内容是 gzip 压缩的。
（这也是 `mine_ikuai_api.py` 里那段解压逻辑存在的原因。）

### 8.2 容器网络不需要端口映射

如果 Docker 网络是 `192.168.3.0/24`、网关是路由器自身的 LAN 地址（如 `192.168.3.1`），
那么**容器是被路由直接可达的**，访问 `192.168.3.2:16601` 即可，完全不需要端口映射。
这解释了为什么很多容器的 `ports` 里只有 `PrivatePort` 没有 `PublicPort`。

---

## 9. 复现方法

固件升级后接口可能变化，用仓库里的 `mine_ikuai_api.py` 重新挖一遍：

```bash
python mine_ikuai_api.py -k docker
```

它会登录爱快、拉下全部前端分片、把 `/Action/call` 的调用点连同上下文导出到 `_mine/`，
再从里面直接读出 `func_name` / `action` / `param` 结构。
