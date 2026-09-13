#!/bin/bash
TOKEN="8888005262:AAGh8xQA1tk3ZT7DMjG6Z8A_NEyUcxNUiFU"

echo "=== 1. file_id from db ==="
/opt/tgpool/venv/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect('/opt/tgpool/index.db')
row = c.execute('select id,name,file_id from files where id=1').fetchone()
print(row)
open('/tmp/fid.txt','w').write(row[2])
PY

FID=$(cat /tmp/fid.txt)

echo "=== 2. getFile response ==="
curl -s -X POST "http://127.0.0.1:8081/bot${TOKEN}/getFile" --data-urlencode "file_id=${FID}"
echo ""

echo "=== 3. what does the API think file_path is ==="
curl -s -X POST "http://127.0.0.1:8081/bot${TOKEN}/getFile" --data-urlencode "file_id=${FID}" \
  | /opt/tgpool/venv/bin/python -c "import sys,json; d=json.load(sys.stdin); p=d.get('result',{}).get('file_path'); print('file_path =', repr(p)); import os; print('is_abs =', os.path.isabs(p) if p else None); print('exists =', os.path.exists(p) if p else None)"

echo "=== 4. app logs (last 20) ==="
journalctl -u tgpool -n 20 --no-pager | tail -20
