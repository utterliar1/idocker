"""探测：爱快 Docker 网络的 IPv6 配置 + 目标容器的 IPv6 归属。

只读，不发任何写请求。用于判断「容器是不是本来就有公网 IPv6」。
用法：python _test/probe_ipv6_net.py [容器名，默认 lucky]
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, "webapp", "app")
sys.path.insert(0, APP)

ENV = os.path.join(os.path.dirname(ROOT), ".env")


def load_env():
    vals = {}
    with open(ENV, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    return vals


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "lucky"

    vals = load_env()
    os.environ["IKUAI_URL"] = vals.get("ikuai_url", "")
    os.environ["IKUAI_USER"] = vals.get("ikuai_id", "")
    os.environ["IKUAI_PASS"] = vals.get("ikuai_pw", "")
    os.environ.setdefault("DATA_DIR", os.path.join(ROOT, "_test", "_data"))

    import ikuai_api as A

    cfg = A.load_config()
    print("爱快地址：%s（凭据已隐藏）" % cfg["router"]["url"])
    ctx = A.Ctx(logger=lambda m, **k: None)
    ik = A.IKuai(cfg, ctx)
    ik.login()
    print("登录成功\n")

    print("=" * 68)
    print("① Docker 网络（看 subnet6 / gateway6 是否配了公网 IPv6）")
    print("=" * 68)
    nets = ik.call("docker_network", "show", {"TYPE": "data"})
    for n in (nets.get("results") or {}).get("data") or []:
        print("  网络名   : %s" % n.get("name"))
        print("  IPv4     : %s  (网关 %s)" % (n.get("subnet"), n.get("gateway")))
        print("  IPv6     : %s  (网关 %s)" % (n.get("subnet6"), n.get("gateway6")))
        print("  容器     : %s" % ", ".join(n.get("containers") or []))
        print("  ----")

    print()
    print("=" * 68)
    print("② 容器列表里的 IPv6 字段")
    print("=" * 68)
    for c in A.get_containers(ik):
        print("  %-16s iface=%-12s ip=%-15s ip6=%s"
              % (c.get("name"), c.get("interface") or "-",
                 c.get("ipaddr") or "-", c.get("ip6addr") or "-"))
        if c.get("name") == target:
            print("      gateway=%s  ip6gateway=%s  image=%s"
                  % (c.get("gateway"), c.get("ip6gateway"), c.get("image")))

    print()
    print("=" * 68)
    print("③ 目标容器 %s 的 inspect（网络原始配置）" % target)
    print("=" * 68)
    cid = None
    for c in A.get_containers(ik):
        if c.get("name") == target:
            cid = c.get("id")
            break
    if not cid:
        print("  未找到容器 %s" % target)
        return
    ins = ik.call("docker_container", "show", {"TYPE": "inspect", "id": cid})
    rows = (ins.get("results") or {}).get("inspect") or []
    for r in rows:
        keep = {k: r.get(k) for k in
                ("name", "image", "interface", "ipaddr", "ip6addr",
                 "gateway", "ip6gateway", "ports") if k in r}
        print("  %s" % json.dumps(keep, ensure_ascii=False, indent=2))
        print("  全部字段：%s" % ", ".join(sorted(r.keys())))


if __name__ == "__main__":
    main()
