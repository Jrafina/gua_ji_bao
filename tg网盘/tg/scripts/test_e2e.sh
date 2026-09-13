#!/bin/bash
ENV=/opt/tgpool/tgpool.env
PW=$(grep '^TG_AUTH_PASS=' "$ENV" | cut -d= -f2)
BOT=$(grep '^TG_BOT_TOKEN=' "$ENV" | cut -d= -f2)

echo "=== 0. getFile path mode check ==="
FID=$(/opt/tgpool/venv/bin/python - <<'PY'
import sqlite3
c=sqlite3.connect('/opt/tgpool/index.db')
r=c.execute('select file_id from files order by id limit 1').fetchone()
print(r[0] if r else '')
PY
)
if [ -n "$FID" ]; then
  curl -s -X POST "http://127.0.0.1:8081/bot${BOT}/getFile" --data-urlencode "file_id=${FID}" \
    | /opt/tgpool/venv/bin/python -c "import sys,json;d=json.load(sys.stdin);p=d.get('result',{}).get('file_path');import os;print('file_path =',repr(p));print('is_absolute =',os.path.isabs(p) if p else None);print('exists_on_disk =',os.path.exists(p) if p and os.path.isabs(p) else 'n/a')"
fi

echo "=== 1. upload fresh file ==="
head -c 5242880 /dev/urandom > /tmp/t2.bin
UP=$(curl -s -u "admin:${PW}" -F "file=@/tmp/t2.bin;filename=e2e_test.bin" http://127.0.0.1:8080/api/upload)
echo "$UP"
ID=$(echo "$UP" | /opt/tgpool/venv/bin/python -c "import sys,json;print(json.load(sys.stdin)['id'])")

echo "=== 2. full download id=$ID ==="
curl -s -u "admin:${PW}" -o /tmp/t2_dl.bin -w 'http=%{http_code} size=%{size_download}\n' \
  "http://127.0.0.1:8080/api/download/${ID}"
echo "sha256 (should match):"
sha256sum /tmp/t2.bin /tmp/t2_dl.bin

echo "=== 3. range request (bytes 0-1023) ==="
curl -s -u "admin:${PW}" -r 0-1023 -o /tmp/t2_part.bin -D /tmp/t2_hdr.txt \
  -w 'http=%{http_code} size=%{size_download}\n' "http://127.0.0.1:8080/api/download/${ID}"
grep -i 'content-range\|accept-ranges' /tmp/t2_hdr.txt || true
ls -la /tmp/t2_part.bin

echo "=== 4. stats ==="
curl -s -u "admin:${PW}" http://127.0.0.1:8080/api/stats
echo ""
