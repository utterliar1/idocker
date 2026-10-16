#!/usr/bin/env python3
"""从公网 IPv6 验证「IPv6 地址 + 端口」是否真的可达。

为什么需要它：本机通常在 LAN 内，流量走 lan1、不经 wan1 入站链，
所以「外网能不能访问」这一跳本机测不到。这个脚本借 Globalping 的开放 API
（无需 API key）从全球 IPv6 节点跑三层探测：

    1. ICMP ping   —— 网络层通不通（能区分「上游没路由这个前缀」）
    2. mtr TCP     —— 传输层通不通（能看到完整路径，最后一跳有无响应）
    3. HTTPS + SNI —— 应用层通不通（拿不拿得到 200 OK、证书对不对）

三层全绿 = 服务端没问题，问题在客户端（DNS 选了 IPv4 / 本机无 IPv6）；
某层红 = 断点就在那一层，照着定位即可。

用法:
    python _test/check_public_v6.py <目标> <端口> [SNI域名] [--ipv4|--ipv6]

示例:
    # 目标写 IPv6 字面量，SNI 写真实域名（HTTPS 必须带域名 SNI，见下）
    python _test/check_public_v6.py 2001:db8::1 4433 example.com
    # 目标写域名，强制只走 IPv6 / 只走 IPv4，用来判定客户端侧该走哪条路
    python _test/check_public_v6.py example.com 4433 example.com --ipv6
    python _test/check_public_v6.py example.com 4433 example.com --ipv4

⚠️ 重要：**HTTPS 不能用 IP 字面量访问**。
RFC 6066 规定 TLS 的 SNI 只能是域名，浏览器/客户端对 IP 字面量**不发 SNI**；
反代（如 lucky）按 SNI 匹配站点，匹配不到就**静默断连**（表现为 SSLEOFError /
连接被重置，且服务端一个字节都不回），极易被误判成「服务没起来 / 证书没加载」。
所以本脚本第 3 层始终用「IP 做 target + 域名做 request.host」，
即在 IP 直连的同时把 SNI 补上；**自验证一律用域名，不要用 `https://[IP]:端口/`**。

注意: 只读。不改任何设备配置，不读也不回显任何凭据。
"""
import sys
import json
import time
import urllib.error
import urllib.request

API = "https://api.globalping.io/v1/measurements"
HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "idocker-v6check/1.0",
}
NODES = 3
POLL_INTERVAL = 3
POLL_MAX = 20

USAGE = __doc__


def submit(payload):
    """提交一次测量，返回 measurement id；失败返回 None 并打印原因。"""
    req = urllib.request.Request(
        API, data=json.dumps(payload).encode(), headers=HEADERS, method="POST"
    )
    try:
        return json.loads(urllib.request.urlopen(req, timeout=25).read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:300]
        print("    提交被拒 (HTTP %s): %s" % (exc.code, detail))
        return None
    except Exception as exc:
        print("    提交失败: %s: %s" % (type(exc).__name__, exc))
        return None


def wait_result(mid):
    """轮询测量结果，返回 finished 的结果列表；超时返回 None。"""
    for _ in range(POLL_MAX):
        time.sleep(POLL_INTERVAL)
        try:
            raw = urllib.request.urlopen(API + "/" + mid, timeout=25).read().decode()
            data = json.loads(raw)
        except Exception:
            continue
        if data.get("status") == "finished":
            return data.get("results", [])
    return None


def probe(payload, label):
    """跑一次探测并把结果打成人类可读的行。"""
    print("\n[%s]" % label)
    submitted = submit(payload)
    if not submitted or not submitted.get("id"):
        print("    -> 无法执行")
        return
    results = wait_result(submitted["id"])
    if results is None:
        print("    -> 轮询超时，未取到结果")
        return
    for item in results:
        probe_info = item.get("probe", {})
        result = item.get("result", {})
        where = "%s/%s" % (probe_info.get("country"), probe_info.get("city") or "?")
        if result.get("statusCode") is not None or "tls" in result:
            # http 类型：优先亮出状态码与证书校验结论（最关键的两个信号）
            print(
                "    %-16s %-9s HTTP=%s tlsAuth=%s resolved=%s failSrc=%s"
                % (
                    where,
                    result.get("status"),
                    result.get("statusCode"),
                    (result.get("tls") or {}).get("authorized"),
                    result.get("resolvedAddress"),
                    result.get("failureSource") or "-",
                )
            )
        elif "rawOutput" in result:
            body = (result.get("rawOutput") or "").strip()
            last = body.splitlines()[-1] if body else ""
            print("    %-16s %-9s %s" % (where, result.get("status"), last[:120]))
        else:
            print("    %-16s %-9s %s" % (where, result.get("status"), json.dumps(result)[:120]))


def main():
    argv = sys.argv[1:]
    if "--help" in argv or "-h" in argv:
        print(USAGE)
        return 0

    # 可选：强制协议栈。不传则交给节点自行选择（域名通常优先 v4）。
    ip_version = None
    for flag, ver in (("--ipv4", 4), ("--ipv6", 6)):
        if flag in argv:
            argv.remove(flag)
            ip_version = ver

    if len(argv) < 2:
        print(USAGE)
        return 2

    target = argv[0]
    try:
        port = int(argv[1])
    except ValueError:
        print("端口必须是数字：%s" % argv[1])
        return 2
    sni = argv[2] if len(argv) > 2 else target

    def opts(**extra):
        """按需注入 ipVersion；Globalping 的字段名是 measurementOptions。"""
        base = dict(extra)
        if ip_version:
            base["ipVersion"] = ip_version
        return base

    tag = {4: " (强制 IPv4)", 6: " (强制 IPv6)"}.get(ip_version, "")
    print("目标: %s:%d   SNI: %s%s" % (target, port, sni, tag))
    print("说明: 三层全绿 => 服务端正常，问题在客户端侧")
    print("注意: HTTPS 自验证请用域名，IP 字面量没有 SNI，反代会静默断连。")

    probe(
        {
            "type": "ping",
            "target": target,
            "locations": [{"magic": "World"}],
            "limit": NODES,
            "measurementOptions": opts(packets=3),
        },
        "第 1 层  ICMP ping —— 网络层（前缀有没有被上游路由）",
    )

    probe(
        {
            "type": "mtr",
            "target": target,
            "locations": [{"magic": "World"}],
            "limit": NODES,
            "measurementOptions": opts(protocol="TCP", port=port, packets=3),
        },
        "第 2 层  mtr TCP/%d —— 传输层（端口有没有被防火墙丢包）" % port,
    )

    probe(
        {
            "type": "http",
            "target": target,
            "locations": [{"magic": "World"}],
            "limit": NODES,
            "measurementOptions": opts(
                protocol="HTTPS",
                port=port,
                request={"path": "/", "host": sni},
            ),
        },
        "第 3 层  HTTPS —— 应用层（拿不拿得到 200、证书对不对）",
    )

    print("\n判读:")
    print("  - 第 1 层就全红  -> 上游没路由这个前缀（换 PD 前缀了？或 ipVersion 选错了）")
    print("  - 第 1 层绿、第 2 层红 -> 入站被防火墙/ACL 拦了")
    print("  - 三层全绿、手机仍打不开 -> 客户端问题")
    print("      · 先确认客户端真的走了 v6（`--ipv4`/`--ipv6` 各跑一次对比）")
    print("      · 客户端侧 DNS 若选了 IPv4，而 v4 路径没映射/没监听，就必然失败")
    print("  - 只有「https://[IP]:端口/」打不开 -> 正常！IP 字面量无 SNI，反代静默断连，")
    print("      与配置无关。请改用域名访问。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
