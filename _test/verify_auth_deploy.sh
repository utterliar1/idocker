#!/bin/sh
# ============================================================
# 飞牛部署后验收：访问认证改成「登录页 + 会话 cookie」之后，整条链路是否正常
#
# 用法（本地）：
#   python fnos_ssh.py --put _test/verify_auth_deploy.sh /tmp/verify_auth_deploy.sh
#   python fnos_ssh.py "sh /tmp/verify_auth_deploy.sh"
#
# 只输出状态码与统计，绝不回显密码明文。
# ============================================================
set -u

B=http://127.0.0.1:3001
DIR=/vol1/1000/docker/webapp
FAIL=0

say() { echo ""; echo "── $* ──"; }
bad() { echo "  ✗ $*"; FAIL=$((FAIL + 1)); }
good() { echo "  ✓ $*"; }

cd "$DIR" || { echo "找不到部署目录 $DIR"; exit 1; }

say "容器状态"
docker ps --format '{{.Names}} | {{.Image}} | {{.Status}}' | grep -E '^idocker' || bad "没看到 idocker 容器"
echo -n "  镜像版本标签: "
docker inspect idocker --format '{{index .Config.Labels "org.opencontainers.image.version"}}'
echo -n "  容器内 app 版本: "
docker exec idocker python -c "import server,sys;sys.stdout.write(server.VERSION)" 2>/dev/null || echo "(读取失败)"

say "未登录访问首页"
LINE=$(curl -sS -o /dev/null -D - "$B/" 2>/dev/null | tr -d '\r' | grep -iE '^HTTP/|^location:')
echo "$LINE" | grep -q ' 302' && good "返回 302" || bad "没有 302 重定向"
echo "$LINE" | grep -qi 'location: /login?next=/' && good "跳到 /login?next=/（登录后原路返回）" \
    || bad "重定向目标不对：$(echo "$LINE" | grep -i location)"

say "未登录调接口"
HDR=$(curl -sS -o /dev/null -D - "$B/api/health" 2>/dev/null | tr -d '\r')
CODE=$(curl -sS -o /dev/null -w '%{http_code}' "$B/api/health" 2>/dev/null)
[ "$CODE" = "401" ] && good "返回 401" || bad "状态码是 $CODE，期望 401"
if echo "$HDR" | grep -qi '^www-authenticate:'; then
    bad "下发了 WWW-Authenticate —— 浏览器又会弹原生账号框"
else
    good "没有 WWW-Authenticate（不会再弹原生框）"
fi

say "登录页要素（密码管理器识别依据）"
PAGE=$(curl -sS "$B/login" 2>/dev/null)
for pat in '<form method="post" action="/api/login"' 'name="username"' \
           'autocomplete="username"' 'type="password"' 'autocomplete="current-password"'; do
    if echo "$PAGE" | grep -qF "$pat"; then good "含 $pat"; else bad "缺少 $pat"; fi
done
echo "$PAGE" | grep -q '/static/' && bad "登录页引用了需要认证的静态资源" \
    || good "登录页样式内联，不依赖需要认证的静态资源"

say "登录流程（用 .env 里的凭据，不回显）"
U=$(sed -n 's/^AUTH_USER=//p' .env | head -1)
P=$(sed -n 's/^AUTH_PASS=//p' .env | head -1)
if [ -z "$U" ] || [ -z "$P" ]; then
    echo "  （.env 里没配 AUTH_USER / AUTH_PASS，跳过登录流程检查）"
else
    echo -n "  密码打错时: "
    C=$(curl -sS -o /dev/null -w '%{http_code}' --data-urlencode "username=$U" \
        --data-urlencode "password=definitely-wrong" --data-urlencode "next=/" \
        "$B/api/login" 2>/dev/null)
    [ "$C" = "401" ] && good "被拒（401）" || bad "竟然放行，返回 $C"

    echo -n "  密码正确时: "
    C=$(curl -sS -c /tmp/_ck.txt -o /dev/null -w '%{http_code}' --data-urlencode "username=$U" \
        --data-urlencode "password=$P" --data-urlencode "next=/" "$B/api/login" 2>/dev/null)
    [ "$C" = "302" ] && good "跳转（302）" || bad "返回 $C，期望 302"
    if grep -q idocker_session /tmp/_ck.txt 2>/dev/null; then
        good "拿到会话 cookie"
    else
        bad "没有下发会话 cookie"
    fi
    C=$(curl -sS -b /tmp/_ck.txt -o /dev/null -w '%{http_code}' "$B/api/overview" 2>/dev/null)
    [ "$C" = "200" ] && good "带 cookie 能读 /api/overview" || bad "带 cookie 读接口返回 $C"
    C=$(curl -sS -b /tmp/_ck.txt -o /dev/null -w '%{http_code}' "$B/api/health" 2>/dev/null)
    [ "$C" = "200" ] && good "带 cookie 能读 /api/health" || bad "带 cookie 读 /api/health 返回 $C"

    echo -n "  Basic 头兼容（脚本/CI 用）: "
    C=$(curl -sS -u "$U:$P" -o /dev/null -w '%{http_code}' "$B/api/health" 2>/dev/null)
    [ "$C" = "200" ] && good "仍然可用" || bad "返回 $C，脚本会受影响"
    rm -f /tmp/_ck.txt
fi

say "页面控件（v1.4.0 的概览条 / 一键更新 / 确认框 / 轻提示）"
# 前端资源漏打是最隐蔽的一类构建事故：页面能开，但新加的控件没了。
fetch() {
    if [ -n "$U" ] && [ -n "$P" ]; then
        curl -sS -u "$U:$P" "$1" 2>/dev/null
    else
        curl -sS "$1" 2>/dev/null
    fi
}
HOME_HTML=$(fetch "$B/")
for want in updBar updStat btnUpdateAll btnCheckInline cfmModal toastWrap; do
    if echo "$HOME_HTML" | grep -qF "id=\"$want\""; then good "首页有 #$want"
    else bad "首页缺少 #$want"; fi
done
APP_JS=$(fetch "$B/static/app.js")
for fn in confirmUpdate updatableContainers renderUpdBar maybeAutoCheck; do
    if echo "$APP_JS" | grep -qF "function $fn"; then good "app.js 有 $fn()"
    else bad "app.js 缺少 $fn()"; fi
done
if fetch "$B/static/style.css" | grep -qF 'prefers-color-scheme: dark'; then
    good "style.css 带深色主题"
else
    bad "style.css 没有深色主题"
fi

say "数据卷有没有被复用（设置与历史不能丢）"
echo -n "  挂载的卷: "
docker inspect idocker --format '{{range .Mounts}}{{.Name}} -> {{.Destination}} {{end}}'
docker exec idocker sh -c 'ls /data' >/dev/null 2>&1 && good "/data 可读" || bad "/data 读不到"
docker exec idocker python -c "
import json
h = json.load(open('/data/history.json'))
s = json.load(open('/data/settings.json'))
print('  执行历史 %d 条 / 下载超时 %s 秒 / 定时任务 %d 个'
      % (len(h), s.get('pull_timeout'), len(s.get('schedules') or [])))
" 2>/dev/null || bad "读不到 /data 里的数据"

say "容器健康"
docker inspect idocker --format '{{.State.Health.Status}}' 2>/dev/null | grep -q healthy \
    && good "healthy" || bad "健康状态不是 healthy"

echo ""
if [ "$FAIL" = "0" ]; then
    echo "===== 全部通过 ====="
else
    echo "===== 有 $FAIL 项没通过 ====="
fi
exit 0
