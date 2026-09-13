#!/bin/bash
set -e
export DEBIAN_FRONTEND=noninteractive

echo "=== 1. create env (only first time) ==="
if [ ! -f /opt/tgpool/tgpool.env ]; then
  PASSWORD=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 16)
  cat > /opt/tgpool/tgpool.env <<EOF
TG_BOT_TOKEN=
TG_CHAT_ID=
TG_API_ID=2040
TG_API_HASH=b18441a1ff607e10a989891a5462e627
TG_API_BASE=http://127.0.0.1:8081
TG_AUTH_USER=admin
TG_AUTH_PASS=${PASSWORD}
TG_TMP_DIR=/opt/tgpool/tmp
TG_DB_PATH=/opt/tgpool/index.db
TG_LOCAL_ROOT=/var/lib/telegram-bot-api
EOF
  chmod 600 /opt/tgpool/tgpool.env
  echo "env created"
else
  echo "env exists, keep it"
fi

echo "=== 2. system deps ==="
apt-get install -y python3-venv python3-pip >/tmp/webapp_apt.log 2>&1 || { tail -20 /tmp/webapp_apt.log; exit 1; }
tail -1 /tmp/webapp_apt.log

echo "=== 3. venv + python deps ==="
if [ ! -d /opt/tgpool/venv ]; then
  python3 -m venv /opt/tgpool/venv
fi
/opt/tgpool/venv/bin/pip install -q --upgrade pip
/opt/tgpool/venv/bin/pip install -q -r /opt/tgpool/app/requirements.txt
/opt/tgpool/venv/bin/pip list 2>/dev/null | grep -iE 'fastapi|uvicorn|httpx|multipart'

echo "=== 4. register systemd unit ==="
cp /opt/tgpool-setup/tgpool.service /etc/systemd/system/tgpool.service
systemctl daemon-reload
systemctl enable tgpool >/dev/null 2>&1

echo "=== 5. start only if token present ==="
if grep -q '^TG_BOT_TOKEN=$' /opt/tgpool/tgpool.env; then
  echo "TG_BOT_TOKEN 为空，暂不启动（等 token 写入后再启动）"
else
  systemctl restart tgpool
  sleep 3
  systemctl is-active tgpool
  journalctl -u tgpool -n 20 --no-pager
fi
echo "=== done ==="
