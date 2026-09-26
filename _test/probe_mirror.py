"""探测：爱快 Docker 服务设置（镜像加速源）+ clash 位置，评估「飞牛做拉取代理」可行性。

只读，不发任何写请求。
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
    """读 .env 并注入环境变量（不回显值）"""
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

    print("=" * 62)
    print("① docker_server 服务设置")
    print("=" * 62)
    d = ik.call("docker_server", "show", {"TYPE": "data"})
    rows = (d.get("results") or {}).get("data") or []
    for r in rows:
        for k in ("id", "enabled", "workdisk", "autostart", "comment"):
            if k in r:
                print("  %-12s %s" % (k, r[k]))
        mirrors = r.get("mirrors") or ""
        print("  mirrors 字段原文：%s" % mirrors)
        print("  拆解后：")
        for i, m in enumerate(mirrors.split(","), 1):
            if m.strip():
                print("    %d) %s" % (i, m.strip()))
        extra = {k: v for k, v in r.items()
                 if k not in ("id", "enabled", "workdisk", "autostart",
                              "comment", "mirrors")}
        print("  其它字段：%s" % json.dumps(extra, ensure_ascii=False))

    print()
    print("=" * 62)
    print("② 容器位置（找 clash / 代理）")
    print("=" * 62)
    for c in A.get_containers(ik):
        ports = ",".join(str(p.get("PrivatePort")) for p in (c.get("ports") or []))
        print("  %-14s %-28s ip=%-14s ports=%s"
              % (c["name"], c["image"], c.get("ipaddr") or "-", ports or "-"))

    print()
    print("=" * 62)
    print("③ Docker 网络")
    print("=" * 62)
    nets = ik.call("docker_network", "show", {"TYPE": "data"})
    for n in (nets.get("results") or {}).get("data") or []:
        print("  %-14s subnet=%-20s gateway=%-14s 容器数=%d"
              % (n.get("name"), n.get("subnet"), n.get("gateway"),
                 len(n.get("containers") or [])))


if __name__ == "__main__":
    main()
