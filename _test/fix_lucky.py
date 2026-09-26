#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修复 lucky 容器：清空 cmd + 把配置目录挂到 /app/conf。

背景（实测确认）：
  lucky 2.20.2  Entrypoint=/app/lucky      镜像默认 Cmd=-c /goodluck/lucky.conf -runInDocker
  lucky 2.27.2  Entrypoint=/app/start.sh   镜像默认 Cmd=（空），配置目录写死 /app/conf
  爱快容器里保存的 cmd 是旧镜像的默认值，update 时被原样回填，
  于是新 entrypoint 收到它：/bin/sh /app/start.sh -c /goodluck/lucky.conf -runInDocker。
  start.sh 的 is_lucky_running() 用 `ps aux | grep "lucky.*-runInDocker"` 判断，
  匹配到了 start.sh 自己的命令行 → 永远认为 lucky 在跑 → 无限 sleep 空转，从不启动 lucky。

修法：
  1. cmd 清空（新版不需要，且是污染源）
  2. mounts 加挂 /app/conf（新版配置目录），保留 /goodluck 以兼容配置内的绝对路径引用

只读预演：python fix_lucky.py
实际执行：python fix_lucky.py --apply
凭据从 D:/Documents/WorkBuddy/爱快/.env 读，绝不回显。
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "webapp", "app"))

ENV = os.path.join(os.path.dirname(ROOT), ".env")
TARGET = "lucky"
TAG = "2.27.2"
HOST_DIR = "/docker/ikuai/lucky"
KEEP_MOUNT = "/goodluck"


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
    apply = "--apply" in sys.argv
    vals = load_env()
    os.environ["IKUAI_URL"] = vals.get("ikuai_url", "")
    os.environ["IKUAI_USER"] = vals.get("ikuai_id", "")
    os.environ["IKUAI_PASS"] = vals.get("ikuai_pw", "")
    os.environ.setdefault("DATA_DIR", os.path.join(ROOT, "_test", "_data"))

    import ikuai_api as A

    cfg = A.load_config()
    ik = A.IKuai(cfg, A.Ctx(logger=lambda m, **k: None))
    ik.login()

    c = [x for x in A.get_containers(ik) if x.get("name") == TARGET][0]
    insp = A.get_inspect(ik, c["id"]) or {}

    # 备份当前完整配置（用于回退）
    backup = {"container": c, "inspect": insp, "saved_at": time.time()}
    bpath = os.path.join(HERE, "lucky_backup_%d.json" % int(time.time()))
    with open(bpath, "w", encoding="utf-8") as f:
        json.dump(backup, f, ensure_ascii=False, indent=2)
    print("已备份当前配置 → %s\n" % os.path.basename(bpath))

    payload, unmapped = A.container_payload(c, insp, tag=TAG)
    before_cmd, before_mounts = payload.get("cmd"), payload.get("mounts")

    payload["cmd"] = ""                                   # 1) 清空（污染源）
    payload["mounts"] = "%s:/app/conf,%s:%s" % (HOST_DIR, HOST_DIR, KEEP_MOUNT)  # 2) 加挂 /app/conf

    print("=== 变更对照 ===")
    print("  %-10s %s  →  %s" % ("cmd", before_cmd or "(空)", payload["cmd"] or "(空)"))
    print("  %-10s %s  →  %s" % ("mounts", before_mounts, payload["mounts"]))
    print("  %-10s %s  （带 tag，爱快靠它选镜像）" % ("image", payload["image"]))
    print("  %-10s %s" % ("ipaddr", payload.get("ipaddr")))
    print("  %-10s %s" % ("interface", payload.get("interface")))
    print("  %-10s %s" % ("enabled", payload.get("enabled")))
    if unmapped:
        print("  （%d 个端口未做宿主映射，doc_docker 路由直连属正常）" % unmapped)

    if not apply:
        print("\n【预演结束】未提交任何改动。加 --apply 实际执行。")
        return

    print("\n=== 提交 update ===")
    ik.call("docker_container", "update", payload)
    print("已提交（写接口成功时返回空响应体，需事后核查）")

    # 等待并核查真实结果
    print("\n等待 18 秒后核查…")
    time.sleep(18)
    k = None
    for x in A.get_containers(ik):
        if x.get("name") == TARGET:
            k = x
            break
    if not k:
        print("❌ 查不到容器！")
        return
    print("  image   = %s" % k.get("image"))
    print("  state   = %s   status = %s" % (k.get("state"), k.get("status")))
    print("  ipaddr  = %s   memused = %s   cpu = %s"
          % (k.get("ipaddr"), k.get("memused"), k.get("cpu_used")))
    print("  cmd     = %r" % k.get("cmd"))
    print("  mounts  = %s" % k.get("mounts"))

    try:
        res = ik.call("docker_container", "show",
                      {"TYPE": "log", "id": k["id"],
                       "starttime": int(time.time()) - 900, "endtime": int(time.time())})
        logs = ((res or {}).get("results") or {}).get("log") or []
        print("\n=== 容器日志（最近 %d 行）===" % len(logs))
        for line in logs[-18:]:
            print("  " + line)
        started = any("Starting Lucky from" in x for x in logs)
        listened = any("Listen on" in x for x in logs)
        print("\n关键判据：")
        print("  出现 'Starting Lucky from' → %s" % ("✅ 是（start.sh 正常走到启动分支）" if started else "❌ 否（仍在空转）"))
        print("  出现 'Listen on'          → %s" % ("✅ 是（lucky 已监听端口）" if listened else "❌ 否"))
    except Exception as e:
        print("取日志失败：%s" % e)


if __name__ == "__main__":
    main()
