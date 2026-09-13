#!/bin/bash
set -e
ENV=/opt/tgpool/tgpool.env
CHAT="8575978784"
PW=$(grep '^TG_AUTH_PASS=' "$ENV" | cut -d= -f2)

echo "=== 1. write TG_CHAT_ID ==="
sed -i "s|^TG_CHAT_ID=.*|TG_CHAT_ID=${CHAT}|" "$ENV"
grep '^TG_CHAT_ID=' "$ENV"

echo "=== 2. restart webapp ==="
systemctl restart tgpool
sleep 3
systemctl is-active tgpool

echo "=== 3. stats ==="
curl -s -u "admin:${PW}" http://127.0.0.1:8080/api/stats
echo ""

echo "=== 4. create test file (~5MB) ==="
head -c 5242880 /dev/urandom > /tmp/test_upload.bin
ls -la /tmp/test_upload.bin

echo "=== 5. upload ==="
curl -s -u "admin:${PW}" -F "file=@/tmp/test_upload.bin;filename=tgpool_test_5mb.bin" \
  http://127.0.0.1:8080/api/upload
echo ""

echo "=== 6. list ==="
curl -s -u "admin:${PW}" http://127.0.0.1:8080/api/files
echo ""

echo "=== 7. download back & verify ==="
curl -s -u "admin:${PW}" -o /tmp/test_download.bin -w 'http=%{http_code} size=%{size_download}\n' \
  http://127.0.0.1:8080/api/download/1
echo "sha256 compare:"
sha256sum /tmp/test_upload.bin /tmp/test_download.bin

echo "=== 8. local cache file ==="
find /var/lib/telegram-bot-api -type f -size +1M 2>/dev/null | head -5
echo "=== done ==="
