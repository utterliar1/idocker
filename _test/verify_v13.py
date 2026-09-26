#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""飞牛真机验证 v1.3.0：自动刷新按钮与静默刷新相关资源是否上线。

在飞牛上执行（需先 . ./.env 拿到 AUTH_USER / AUTH_PASS）：
    cd /vol1/1000/docker/webapp && set -a && . ./.env && set +a && python3 _verify.py
"""
import base64
import os
import urllib.request

BASE = "http://127.0.0.1:3001"
AUTH = "Basic " + base64.b64encode(
    (os.environ["AUTH_USER"] + ":" + os.environ["AUTH_PASS"]).encode()).decode()


def get(path):
    req = urllib.request.Request(BASE + path)
    req.add_header("Authorization", AUTH)
    r = urllib.request.urlopen(req, timeout=20)
    return r.status, r.read()


PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  \u2713 %s" % name)
    else:
        FAIL += 1
        print("  \u2717 %s  %s" % (name, extra))


print("=== 页面 ===")
st, html = get("/")
check("GET / 返回 200", st == 200, st)
check("页面含自动刷新按钮", b'id="btnAuto"' in html and b'id="autoLabel"' in html)
check("页面含最后刷新时间", b'id="lastRefresh"' in html)
check("自动刷新按钮在顶栏（在「设置」之前）",
      html.find(b'id="btnAuto"') < html.find(b'id="btnSettingsTop"'))

print("=== 前端脚本 ===")
st, js = get("/static/app.js")
check("GET /static/app.js 返回 200", st == 200, st)
check("脚本含 AUTO_MS 常量", b"AUTO_MS" in js)
check("脚本支持 ?autorefresh= 覆盖间隔", b"autorefresh" in js)
check("脚本含表格指纹比对（ctSig）", b"ctSig" in js)
check("脚本含静默刷新参数", b"silent" in js)
check("暂停状态写入 localStorage", b"ikuai_auto_refresh" in js)
check("任务执行时让路（autoYieldToJob）", b"autoYieldToJob" in js)
check("切回前台时补刷（visibilitychange）", b"visibilitychange" in js)

print("=== 样式 ===")
st, css = get("/static/style.css")
check("GET /static/style.css 返回 200", st == 200, st)
check("含自动刷新圆点样式", b".auto-dot" in css)
check("含暂停态样式 .btn.auto.off", b".btn.auto.off" in css)

print("=" * 46)
print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
raise SystemExit(1 if FAIL else 0)
