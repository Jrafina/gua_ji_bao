#!/bin/bash
set -e
export DEBIAN_FRONTEND=noninteractive

echo "=== 1. system deps ==="
apt-get install -y python3-venv python3-pip >/tmp/webapp_apt.log 2>&1 || { tail -20 /tmp/webapp_apt.log; exit 1; }
tail -2 /tmp/webapp_apt.log

echo "=== 2. dirs ==="
mkdir -p /opt/tgpool/app /opt/tgpool/tmp

echo "=== 3. venv + deps ==="
if [ ! -d /opt/tgpool/venv ]; then
  python3 -m venv /opt/tgpool/venv
fi
/opt/tgpool/venv/bin/pip install -q --upgrade pip
/opt/tgpool/venv/bin/pip install -q -r /opt/tgpool/app/requirements.txt
/opt/tgpool/venv/bin/pip list 2>/dev/null | grep -iE 'fastapi|uvicorn|httpx|multipart'

echo "=== 4. systemd ==="
cp /opt/tgpool-setup/tgpool.service /etc/systemd/system/tgpool.service
systemctl daemon-reload
systemctl enable tgpool >/dev/null 2>&1
systemctl restart tgpool
sleep 3
systemctl is-active tgpool
echo "=== webapp logs ==="
journalctl -u tgpool -n 20 --no-pager 2>&1
echo "=== local check ==="
curl -s -o /dev/null -w 'webapp root: %{http_code}\n' -u "admin:$(grep TG_AUTH_PASS /opt/tgpool/tgpool.env | cut -d= -f2)" http://127.0.0.1:8080/ || true
