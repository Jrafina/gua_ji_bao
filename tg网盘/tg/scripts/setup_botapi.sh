#!/bin/bash
set -e
source /opt/tgpool/tgpool.env

echo "=== prepare data dir ==="
mkdir -p /var/lib/telegram-bot-api

echo "=== pull image ==="
docker pull aiogram/telegram-bot-api:latest

echo "=== (re)create container ==="
# 注意: 该镜像的 entrypoint 只读取环境变量, 命令行参数会被忽略
# 开启本地模式必须用 TELEGRAM_LOCAL=1
docker rm -f tg-bot-api 2>/dev/null || true
docker run -d --name tg-bot-api \
  --restart always \
  --memory 420m \
  -p 127.0.0.1:8081:8081 \
  -e TELEGRAM_API_ID="$TG_API_ID" \
  -e TELEGRAM_API_HASH="$TG_API_HASH" \
  -e TELEGRAM_LOCAL=1 \
  -e TELEGRAM_WORK_DIR=/var/lib/telegram-bot-api \
  -e TELEGRAM_TEMP_DIR=/tmp/telegram-bot-api \
  -v /var/lib/telegram-bot-api:/var/lib/telegram-bot-api \
  aiogram/telegram-bot-api:latest

sleep 4
echo "=== container status ==="
docker ps --filter name=tg-bot-api --format '{{.Names}} {{.Status}} {{.Ports}}'
echo "=== logs (check --local present) ==="
docker logs --tail 5 tg-bot-api 2>&1
echo "=== getMe ==="
curl -s "http://127.0.0.1:8081/bot${TG_BOT_TOKEN}/getMe" | head -c 300
echo ""
