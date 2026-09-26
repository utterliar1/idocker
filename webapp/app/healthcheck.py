#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""容器健康检查。

只要服务能正常应答 HTTP 就算存活。

注意：``/api/health`` 在开启访问认证（``AUTH_PASS``）时会返回 **401**，
这恰恰说明服务在正常工作 —— 所以把 200 和 401 都算作健康，
只有连不上、超时或 5xx 才算失败。

（早期版本直接用 urllib 请求且只认 200，导致一开认证容器就一直是
``unhealthy``，虽然功能完全正常但看着像挂了，这里修掉。）
"""
import os
import sys
import urllib.error
import urllib.request

URL = "http://127.0.0.1:%s/api/health" % os.environ.get("WEB_PORT", "8088")


def main():
    try:
        with urllib.request.urlopen(URL, timeout=5) as resp:
            return 0 if resp.status == 200 else 1
    except urllib.error.HTTPError as e:
        # 401 = 认证已开启且服务在应答，视为存活
        return 0 if e.code in (200, 401) else 1
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
