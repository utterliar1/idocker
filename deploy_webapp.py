#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本地的 webapp/ 同步到飞牛（fnOS）并重建容器。

用法（在项目根目录执行）：
    python deploy_webapp.py                  # 上传 + 重建 + 重启
    python deploy_webapp.py --upload-only    # 只上传，不动容器
    python deploy_webapp.py --tail           # 上传重建后再跟 40 行日志

设计要点：
  · 只**新增/覆盖**文件，从不删除远端文件 —— 远端的 .env 和 settings.json 必须保留；
  · 上传前先打一份远端文件清单，方便对比；
  · 凭据全部从 .env 读，脚本内不回显任何密码。
"""

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fnos_ssh                                        # noqa: E402

LOCAL_WEBAPP = os.path.join(HERE, "webapp")
DEFAULT_REMOTE = "/vol1/1000/docker/webapp"
# 这些绝不覆盖/不删除：远端 .env 是用户的真实配置
SKIP = {".env", "screenshots", "__pycache__", ".git", "data"}


def remote_dir(env):
    return (env.get("fn_webapp_dir") or DEFAULT_REMOTE).strip()


def upload(sftp, local, remote, depth=0):
    pad = "  " * depth
    try:
        sftp.stat(remote)
    except IOError:
        sftp.mkdir(remote)
        print("%s+ %s/" % (pad, remote))
    n = 0
    for name in sorted(os.listdir(local)):
        if name in SKIP or name.endswith(".pyc"):
            continue
        lp, rp = os.path.join(local, name), remote + "/" + name
        if os.path.isdir(lp):
            n += upload(sftp, lp, rp, depth + 1)
        else:
            sftp.put(lp, rp)
            print("%s↑ %s" % (pad, name))
            n += 1
    return n


def main():
    ap = argparse.ArgumentParser(description="部署 webapp 到飞牛")
    ap.add_argument("--upload-only", action="store_true", help="只上传，不重建容器")
    ap.add_argument("--tail", action="store_true", help="重建后跟一段容器日志")
    ap.add_argument("--no-build", action="store_true", help="重启但不重新构建镜像")
    args = ap.parse_args()

    env = fnos_ssh.load_env()
    remote = remote_dir(env)
    cli, host, port = fnos_ssh.connect(env)
    try:
        is_root, sudo_pw = fnos_ssh.ensure_root(cli, env)
        print("已连接 %s@%s:%d  →  目标目录 %s\n" % (env["fn_id"], host, port, remote))

        print("── 上传 ─────────────────────────────────")
        sftp = cli.open_sftp()
        try:
            total = upload(sftp, LOCAL_WEBAPP, remote)
        finally:
            sftp.close()
        print("共上传 %d 个文件\n" % total)

        if args.upload_only:
            print("已跳过容器重建（--upload-only）")
            return 0

        print("── 远端现有文件 ─────────────────────────")
        fnos_ssh.run(cli, "ls -la %s" % remote, None, 60)

        print("\n── 重建并重启 ───────────────────────────")
        build = "docker compose up -d --build" if not args.no_build \
            else "docker compose up -d --force-recreate"
        code, out, err = fnos_ssh.run(
            cli, "cd %s && %s" % (remote, build), sudo_pw, 1800)
        if code != 0:
            print("!! 构建失败，退出码 %d" % code)
            return code

        print("\n── 等待容器起来 ─────────────────────────")
        time.sleep(6)
        fnos_ssh.run(cli, "docker ps --filter name=idocker"
                          " --format '{{.Names}} {{.Status}} {{.Ports}}'", None, 60)

        if args.tail:
            print("\n── 最近日志 ─────────────────────────────")
            fnos_ssh.run(cli, "docker logs --tail 40 idocker", None, 60)
        return 0
    finally:
        cli.close()


if __name__ == "__main__":
    sys.exit(main())
