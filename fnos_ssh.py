#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""飞牛 fnOS 远程执行助手。

从项目根目录的 .env 读取飞牛地址与凭据（凭据绝不回显），
通过 paramiko 登录并执行命令；默认校验 known_hosts 中的主机密钥，已带 sudo 前缀的命令会自动补密码。
首次连接请先把可信主机指纹写入用户 known_hosts，或在 .env 中显式设置 FN_KNOWN_HOSTS / FN_AUTO_ADD_HOST_KEY。

用法：
    python fnos_ssh.py "docker ps"                  # 单条
    python fnos_ssh.py --sudo "cat /etc/docker/daemon.json"
    python fnos_ssh.py --file cmds.sh               # 从文件批量执行
    python fnos_ssh.py --put local.txt /remote/dir  # 上传

⚠ 坑（Git Bash / MSYS 环境）：命令行里的 ``/vol1/1000/...`` 这类**以斜杠开头的
独立参数**会被 MSYS 自动改写成 Windows 本地路径，传到远端就变成
``C:/Program Files/Git/vol1/...``，报 ``FileNotFoundError: [Errno 2] No such file``。
上传/下载时请加上 ``MSYS_NO_PATHCONV=1``：

    MSYS_NO_PATHCONV=1 python fnos_ssh.py --put local.txt /vol1/1000/docker/webapp/

（写在命令字符串里的路径不受影响，因为它不以斜杠开头。）
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_CANDIDATES = [
    os.path.join(HERE, ".env"),
    os.path.join(HERE, "..", ".env"),
]

import paramiko  # noqa: E402


def load_env():
    env = {}
    for path in ENV_CANDIDATES:
        path = os.path.abspath(path)
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
        break
    need = ("fn_url", "fn_id", "fn_pw")
    missing = [k for k in need if not env.get(k)]
    if missing:
        raise SystemExit("配置缺失：%s 未在 .env 中找到" % "、".join(missing))
    return env


def parse_target(raw):
    """接受 192.168.1.10 / 192.168.1.10:22 / ssh://u@h:22 三种写法"""
    raw = raw.replace("ssh://", "")
    if "@" in raw:
        raw = raw.split("@", 1)[1]
    if ":" in raw:
        host, port = raw.rsplit(":", 1)
        return host, int(port)
    return raw, 22


def connect(env, timeout=20):
    host, port = parse_target(env["fn_url"])
    cli = paramiko.SSHClient()
    known_hosts = env.get("fn_known_hosts") or env.get("FN_KNOWN_HOSTS") or os.path.expanduser("~/.ssh/known_hosts")
    if os.path.exists(known_hosts):
        cli.load_host_keys(known_hosts)
    cli.load_system_host_keys()
    auto_add = env.get("fn_auto_add_host_key") or env.get("FN_AUTO_ADD_HOST_KEY") or ""
    if auto_add.lower() in ("1", "true", "yes"):
        cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    else:
        cli.set_missing_host_key_policy(paramiko.RejectPolicy())
    cli.connect(hostname=host, port=port, username=env["fn_id"],
                password=env["fn_pw"], timeout=timeout,
                allow_agent=False, look_for_keys=False)
    return cli, host, port


def ensure_root(cli, env):
    """检测当前用户是否 root；非 root 则把 sudo 密码注入 stdin 前缀。"""
    _, out, _ = cli.exec_command("id -u", timeout=15)
    uid = out.read().decode().strip()
    if uid == "0":
        return True, None
    return False, env["fn_pw"]


def run(cli, cmd, sudo_pw=None, timeout=600, quiet=False):
    """执行命令。若是 sudo 命令且非 root，用 -S 从 stdin 读密码。"""
    real = cmd
    stdin_data = ""
    if sudo_pw is not None and cmd.strip().startswith("sudo"):
        real = cmd.replace("sudo ", "sudo -S -p '' ", 1)
        stdin_data = sudo_pw + "\n"
    if not quiet:
        print("$ " + cmd)
    stdin, stdout, stderr = cli.exec_command(real, timeout=timeout, get_pty=False)
    if stdin_data:
        stdin.write(stdin_data)
        stdin.flush()
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    # sudo 密码可能被回显到 stderr，过滤掉
    if sudo_pw:
        err = err.replace(sudo_pw, "******")
    if out and not quiet:
        sys.stdout.write(out if out.endswith("\n") else out + "\n")
    if err.strip() and not quiet:
        sys.stdout.write("[stderr] " + err.rstrip() + "\n")
    if not quiet:
        print("[exit %d]" % code)
    return code, out, err


def main():
    ap = argparse.ArgumentParser(description="飞牛 fnOS 远程执行")
    ap.add_argument("cmd", nargs="?", help="要执行的命令")
    ap.add_argument("--sudo", action="store_true", help="以 sudo 执行")
    ap.add_argument("--file", help="从文件读取命令（每行一条，# 开头跳过）")
    ap.add_argument("--put", nargs=2, metavar=("LOCAL", "REMOTE"),
                    help="上传：本地路径 远端目录或路径")
    ap.add_argument("--get", nargs=2, metavar=("REMOTE", "LOCAL"),
                    help="下载：远端路径 本地路径")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--privileged", action="store_true",
                    help="强制用 sudo（默认自动检测非 root 才加）")
    args = ap.parse_args()

    env = load_env()
    host, port = parse_target(env["fn_url"])
    print("连接 %s@%s:%d ..." % (env["fn_id"], host, port))

    cli, _, _ = connect(env)
    try:
        is_root, sudo_pw = ensure_root(cli, env)
        print("已登录：%s\n" % ("root" if is_root else "普通用户（需要时自动 sudo）"))
        if args.privileged and not is_root:
            sudo_pw = env["fn_pw"]
        elif is_root:
            sudo_pw = None

        if args.put:
            local, remote = args.put
            sftp = cli.open_sftp()
            sftp.put(local, remote)
            sftp.close()
            print("已上传 %s -> %s" % (local, remote))
            return 0

        if args.get:
            remote, local = args.get
            sftp = cli.open_sftp()
            sftp.get(remote, local)
            sftp.close()
            print("已下载 %s -> %s" % (remote, local))
            return 0

        if args.file:
            with open(args.file, "r", encoding="utf-8") as f:
                cmds = [l.strip() for l in f
                        if l.strip() and not l.strip().startswith("#")]
            for c in cmds:
                if "[sudo]" in c:
                    c = c.replace("[sudo]", "sudo")
                    run(cli, c, sudo_pw, args.timeout)
                else:
                    run(cli, c, None, args.timeout)
                print()
            return 0

        if not args.cmd:
            ap.error("需要提供命令，或用 --file / --put / --get")

        cmd = args.cmd
        if args.sudo and not cmd.strip().startswith("sudo"):
            cmd = "sudo " + cmd
        code, _, _ = run(cli, cmd, sudo_pw, args.timeout)
        return code
    finally:
        cli.close()


if __name__ == "__main__":
    sys.exit(main())
