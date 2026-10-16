#!/bin/sh
# 变更后验收：触发一次检测，读回 last_check.json。
# 在飞牛上跑： sh verify_after_update.sh [关键字]
# 凭据从 .env 读，绝不回显。只读 + 一次检测任务，不发任何写请求。
cd /vol1/1000/docker/webapp || exit 1
KEY="${1:-vaultwarden}"
AU=$(grep '^AUTH_USER=' .env | head -1 | cut -d= -f2- | tr -d '\r')
AP=$(grep '^AUTH_PASS=' .env | head -1 | cut -d= -f2- | tr -d '\r')

echo "=== 触发检测 ==="
JID=$(curl -s -u "$AU:$AP" -X POST http://127.0.0.1:3001/api/check | sed -n 's/.*"job_id"[: ]*"\([0-9a-f]*\)".*/\1/p')
echo "  job_id=$JID"
i=0
ST=""
while [ $i -lt 60 ]; do
  sleep 5
  i=$((i+1))
  ST=$(curl -s -u "$AU:$AP" "http://127.0.0.1:3001/api/jobs/$JID" | sed -n 's/.*"finished"[: ]*\(true\|false\).*/\1/p')
  if [ "$ST" = "true" ]; then break; fi
done
echo "  finished=$ST （等了 $((i*5)) 秒）"
echo
echo "=== last_check.json（含 $KEY 的条目）==="
docker exec idocker python -c "import json;d=json.load(open('/data/last_check.json'));print('at =',d.get('at_str'));[print('  ',k,'->',json.dumps(v,ensure_ascii=False)) for k,v in (d.get('items') or {}).items()]" | grep -E "at =|$KEY" || echo "  （没有匹配条目）"
echo
echo "=== 版本 ==="
docker exec idocker python -c "import server;print('  idocker',server.VERSION)"
