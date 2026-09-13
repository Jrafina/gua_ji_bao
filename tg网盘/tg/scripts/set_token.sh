#!/bin/bash
set -e
ENV=/opt/tgpool/tgpool.env
TOKEN="8888005262:AAGh8xQA1tk3ZT7DMjG6Z8A_NEyUcxNUiFU"

sed -i "s|^TG_BOT_TOKEN=.*|TG_BOT_TOKEN=${TOKEN}|" "$ENV"
chmod 600 "$ENV"

echo "=== env (token masked) ==="
sed -E 's/^(TG_BOT_TOKEN=).*/\1***masked***/; s/^(TG_API_HASH=).*/\1***masked***/; s/^(TG_AUTH_PASS=).*/\1***masked***/' "$ENV"
echo "=== verify token saved (length check) ==="
grep -o 'TG_BOT_TOKEN=.*' "$ENV" | sed -E 's/(TG_BOT_TOKEN=.{12}).*/\1.../' 
