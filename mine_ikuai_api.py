#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
爱快前端接口挖掘工具

思路：不靠人工抓包，直接把爱快 Web 前端的 JS 全部下载下来，从里面提出
所有 /Action/call 的调用点（func_name / action / param），从而得到完整、
准确的接口清单。

用法：
    python mine_ikuai_api.py                  # 挖全部，结果存到 _mine/
    python mine_ikuai_api.py -k docker        # 只关心 docker 相关
    python mine_ikuai_api.py --list-chunks    # 只列出前端有哪些 JS 分片
"""

import argparse
import base64
import gzip
import hashlib
import http.cookiejar
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "_mine")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

# 关键：爱快的静态资源不带 Accept-Encoding 会直接 404，且返回的是压缩内容
ASSET_HEADERS = {
    "User-Agent": UA,
    "Accept": "*/*",
    "Accept-Encoding": "gzip",
}

try:
    import brotli  # noqa: F401
    _HAS_BROTLI = True
except ImportError:
    _HAS_BROTLI = False


def decompress(body, encoding):
    enc = (encoding or "").lower()
    if "br" in enc and _HAS_BROTLI:
        import brotli
        return brotli.decompress(body)
    if "gzip" in enc:
        return gzip.decompress(body)
    if "deflate" in enc:
        try:
            return zlib.decompress(body)
        except zlib.error:
            return zlib.decompress(body, -zlib.MAX_WBITS)
    return body


def load_env(path=None):
    path = path or os.path.join(HERE, "..", ".env")
    env = {}
    if not os.path.exists(path):
        return env
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


class Client(object):
    def __init__(self, host, user, pw):
        self.host = host
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=ctx))

    def login(self, user, pw):
        payload = {
            "username": user,
            "passwd": hashlib.md5(pw.encode()).hexdigest(),
            "pass": base64.b64encode(("salt_11" + pw).encode()).decode(),
            "remember_password": "",
        }
        data = json.dumps(payload).encode()
        req = urllib.request.Request("https://%s/Action/login" % self.host,
                                     data=data, method="POST")
        req.add_header("Content-Type", "application/json;charset=utf-8")
        req.add_header("User-Agent", UA)
        req.add_header("Referer", "https://%s/" % self.host)
        with self.op.open(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def get(self, path, headers=None, timeout=40):
        req = urllib.request.Request("https://%s%s" % (self.host, path))
        for k, v in (headers or ASSET_HEADERS).items():
            req.add_header(k, v)
        req.add_header("Referer", "https://%s/" % self.host)
        with self.op.open(req, timeout=timeout) as r:
            body = r.read()
            enc = r.headers.get("Content-Encoding")
        return decompress(body, enc)


def extract_chunks(js):
    """从 Vite 打包产物里找出所有分片路径（含动态 import 的 chunk）

    分片里引用同目录文件写的是 ``static/js/xxx.js``（不带前导斜杠），
    早先直接补 ``/static/js/`` 会拼成 ``/static/js/static/js/xxx.js`` 而 404，
    所以先识别 ``static/`` 开头的形式。
    """
    found = set()
    for m in re.finditer(r'["\'`]([^"\'`\s]{1,120}?\.js)["\'`]', js):
        p = m.group(1)
        if p.startswith("http") or p.startswith("data:"):
            continue
        if p.startswith("./"):
            p = p[1:]
        if p.startswith("static/"):
            p = "/" + p
        elif not p.startswith("/"):
            p = "/static/js/" + p
        if p.endswith(".js"):
            found.add(p)
    return sorted(found)


def find_call_sites(js, chunk_name):
    """在一个 JS 文件里找出 /Action/call 的调用点"""
    hits = []
    for kw in ("func_name", "funcName", "Action/call", "action:"):
        for m in re.finditer(re.escape(kw), js):
            s = max(0, m.start() - 320)
            e = min(len(js), m.end() + 420)
            hits.append({"chunk": chunk_name, "kw": kw, "snippet": js[s:e]})
            if len(hits) > 4000:
                return hits
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", "--keyword", default="docker", help="只保留含该关键词的片段")
    ap.add_argument("--list-chunks", action="store_true", help="只列出分片")
    ap.add_argument("--max-chunks", type=int, default=400)
    args = ap.parse_args()

    env = load_env()
    host = env.get("ikuai_url") or env.get("IKUAI_URL")
    user = env.get("ikuai_id") or env.get("IKUAI_USERNAME")
    pw = env.get("ikuai_pw") or env.get("IKUAI_PASSWORD")
    if not (host and user and pw):
        print("没找到 .env 里的 ikuai_url / ikuai_id / ikuai_pw")
        return 1

    os.makedirs(OUT_DIR, exist_ok=True)
    c = Client(host, user, pw)
    resp = c.login(user, pw)
    print("登录：%s" % json.dumps(resp, ensure_ascii=False)[:120])

    html = c.get("/").decode("utf-8", "replace")
    entries = re.findall(r'<script[^>]+src=["\']([^"\']+\.js)["\']', html)
    print("入口脚本：%s" % ", ".join(entries))

    all_chunks = set(entries)
    main_js = ""
    for e in entries:
        try:
            js = c.get(e).decode("utf-8", "replace")
        except Exception as ex:
            print("  下载失败 %s: %s" % (e, ex))
            continue
        with open(os.path.join(OUT_DIR, os.path.basename(e)), "w", encoding="utf-8") as f:
            f.write(js)
        main_js += js
        all_chunks |= set(extract_chunks(js))

    print("共发现 %d 个 JS 分片" % len(all_chunks))
    if args.list_chunks:
        for p in sorted(all_chunks):
            print("  " + p)
        return 0

    # 继续下载所有分片
    more_js = {}
    for i, p in enumerate(sorted(all_chunks), 1):
        if i > args.max_chunks:
            print("达到 --max-chunks 上限，停止")
            break
        name = os.path.basename(p)
        if name in more_js:
            continue
        try:
            js = c.get(p).decode("utf-8", "replace")
            more_js[name] = js
            with open(os.path.join(OUT_DIR, name), "w", encoding="utf-8") as f:
                f.write(js)
        except Exception as ex:
            print("  跳过 %s: %s" % (p, ex))

    print("分片下载完成，合计 %d 个文件" % len(more_js))

    kw = args.keyword
    report = []
    for name, js in list(more_js.items()) + [("entry", main_js)]:
        for h in find_call_sites(js, name):
            if kw and kw.lower() not in h["snippet"].lower():
                continue
            report.append(h)

    out = os.path.join(OUT_DIR, "call_sites_%s.json" % (kw or "all"))
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("命中 %d 条含 %r 的调用片段 → %s" % (len(report), kw, out))

    # 精简打印：把片段里出现的 action / func_name 值抓出来
    pairs = set()
    for h in report:
        for m in re.finditer(r'(?:func_name|funcName)\s*[:=]\s*["\']([^"\']+)["\']', h["snippet"]):
            pairs.add(("func_name", m.group(1)))
        for m in re.finditer(r'\baction\s*[:=]\s*["\']([^"\']+)["\']', h["snippet"]):
            pairs.add(("action", m.group(1)))
    if pairs:
        print("\n提取到的字段值：")
        for k, v in sorted(pairs):
            print("  %-10s %s" % (k, v))
    return 0


if __name__ == "__main__":
    sys.exit(main())
