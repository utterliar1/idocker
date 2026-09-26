#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读排查：lucky 容器是否在崩溃重启，并取容器日志。

用途：更新后容器状态显示 running 但服务不可用 / 版本没生效时用。
只读：只发 docker_container.show（含 TYPE=log / TYPE=source_used）。
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
TARGET = sys.argv[1] if len(sys.argv) > 1 else "lucky"


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
    vals = load_env()
    os.environ["IKUAI_URL"] = vals.get("ikuai_url", "")
    os.environ["IKUAI_USER"] = vals.get("ikuai_id", "")
    os.environ["IKUAI_PASS"] = vals.get("ikuai_pw", "")
    os.environ.setdefault("DATA_DIR", os.path.join(ROOT, "_test", "_data"))

    import ikuai_api as A

    cfg = A.load_config()
    print("爱快地址：%s（凭据已隐藏）\n" % cfg["router"]["url"])
    ctx = A.Ctx(logger=lambda m, **k: None)
    ik = A.IKuai(cfg, ctx)
    ik.login()

    def find():
        for c in A.get_containers(ik):
            if c.get("name") == TARGET:
                return c
        return None

    c = find()
    if not c:
        print("找不到容器 %s" % TARGET)
        return
    cid = c["id"]

    print("=== 第 1 次采样 ===")
    print("  status=%s  state=%s  created=%s  exitcode=%s  memused=%s  cpu=%s"
          % (c.get("status"), c.get("state"), c.get("created"),
             c.get("exitcode"), c.get("memused"), c.get("cpu_used")))
    first_created = c.get("created")
    now = int(time.time())

    # 容器日志：从容器创建时刻往前放宽 1 天，覆盖重启的历史输出
    for (label, span) in (("近 1 小时", 3600), ("近 1 天", 86400), ("近 7 天", 604800)):
        try:
            res = ik.call("docker_container", "show",
                          {"TYPE": "log", "id": cid,
                           "starttime": now - span, "endtime": now})
        except Exception as e:
            print("\n[%s] 取日志失败：%s" % (label, e))
            continue
        text = json.dumps(res, ensure_ascii=False)
        if not text or text in ("{}", "null", '{"code":0,"results":{}}'):
            continue
        print("\n=== 容器日志（%s）===" % label)
        print(text[:6000])
        break
    else:
        print("\n（三个时间窗都没取到日志内容）")

    # 等 25 秒再采样，判断是否在重启
    print("\n等待 25 秒后再采样，判断是否重启循环…")
    time.sleep(25)
    c2 = find()
    if c2:
        print("=== 第 2 次采样 ===")
        print("  status=%s  state=%s  created=%s  exitcode=%s  memused=%s  cpu=%s"
              % (c2.get("status"), c2.get("state"), c2.get("created"),
                 c2.get("exitcode"), c2.get("memused"), c2.get("cpu_used")))
        restarted = (c2.get("created") != first_created)
        print("  容器创建时间是否变化：%s  → %s"
              % (restarted, "正在反复重启" if restarted else "未重建"))
        up = str(c2.get("status") or "")
        if "second" in up.lower() or "About a minute" in up:
            print("  运行时长很短（%s）→ 强烈提示崩溃重启循环" % up)


if __name__ == "__main__":
    main()
