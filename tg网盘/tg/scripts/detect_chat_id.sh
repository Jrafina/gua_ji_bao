#!/usr/bin/env bash
# 自动识别存储池对应的 chat_id 并写入配置（重建新服务器时用）
#
#   bash detect_chat_id.sh          # 自动取「最近给 bot 发消息的那个会话」
#   bash detect_chat_id.sh -12345   # 手动指定
#
# 前提：先在 Telegram 里给 bot 发一条任意消息（比如 hi）。
# Bot API 只能看到「发给 bot 的消息」，bot 自己发出的文件不在 getUpdates 里。
set -euo pipefail

ENV=/opt/tgpool/tgpool.env
[ -f "$ENV" ] || { echo "[x] 找不到 $ENV"; exit 2; }
# shellcheck disable=SC1090
. "$ENV"
[ -n "${TG_BOT_TOKEN:-}" ] || { echo "[x] $ENV 里 TG_BOT_TOKEN 为空，先写入 token"; exit 2; }

BASE="${TG_API_BASE:-http://127.0.0.1:8081}"

echo "=== 1. 确认 bot 可达 ==="
ME="$(curl -s "${BASE}/bot${TG_BOT_TOKEN}/getMe")"
echo "  $(echo "$ME" | head -c 200)"
echo "$ME" | grep -q '"ok":true' || { echo "[x] getMe 失败，检查 tg-bot-api 容器与 token"; exit 3; }

if [ -n "${1:-}" ]; then
  CID="$1"
  echo "=== 2. 使用命令行指定的 chat_id: $CID ==="
else
  echo "=== 2. 从 getUpdates 识别会话 ==="
  UPD="$(curl -s "${BASE}/bot${TG_BOT_TOKEN}/getUpdates?limit=100")"
  COUNT="$(echo "$UPD" | python3 -c 'import json,sys;print(len(json.load(sys.stdin).get("result",[])))')"
  echo "  收到 $COUNT 条更新"
  if [ "$COUNT" = "0" ]; then
    echo
    echo "  [x] 没有任何更新 —— 说明 bot 还没收到过消息。"
    echo "      请打开 Telegram，搜索你的 bot，给它发一条任意消息（例如 hi），然后重跑本脚本。"
    exit 4
  fi
  echo "  候选会话："
  echo "$UPD" | python3 -c '
import json, sys
d = json.load(sys.stdin)
seen = {}
for u in d.get("result", []):
    m = u.get("message") or u.get("channel_post") or u.get("edited_message") or {}
    c = m.get("chat") or {}
    cid = c.get("id")
    if cid is None:
        continue
    seen[cid] = (c.get("type", "?"), c.get("title") or c.get("username") or c.get("first_name") or "", m.get("date", 0))
for cid, (typ, name, dt) in sorted(seen.items(), key=lambda kv: -kv[1][2]):
    print("    %-16s %-10s %s" % (cid, typ, name))
'
  CID="$(echo "$UPD" | python3 -c '
import json, sys
d = json.load(sys.stdin)
best = None
for u in d.get("result", []):
    m = u.get("message") or u.get("channel_post") or {}
    c = m.get("chat") or {}
    cid = c.get("id")
    if cid is None:
        continue
    # 优先私聊，其次取时间最新的
    score = (1 if c.get("type") == "private" else 0, m.get("date", 0))
    if best is None or score > best[0]:
        best = (score, cid)
print(best[1] if best else "")
')"
  [ -n "$CID" ] || { echo "[x] 未能解析出 chat_id"; exit 5; }
  echo "  选中: $CID"
fi

echo "=== 3. 写入 $ENV ==="
sed -i "s|^TG_CHAT_ID=.*|TG_CHAT_ID=${CID}|" "$ENV"
chmod 600 "$ENV"
grep -E '^TG_CHAT_ID=' "$ENV" | sed 's/^/  /'

echo "=== 4. 重启应用 ==="
systemctl restart tgpool
sleep 3
systemctl is-active tgpool

echo "=== 5. 冒烟测试：上传一个 1KB 文件 ==="
PASS="$(grep '^TG_AUTH_PASS=' "$ENV" | cut -d= -f2)"
head -c 1024 /dev/urandom > /tmp/_cid_probe.bin
curl -s -u "admin:${PASS}" -F "file=@/tmp/_cid_probe.bin;filename=_cid_probe.bin" \
  http://127.0.0.1:8080/api/upload
echo
rm -f /tmp/_cid_probe.bin
echo "若上面返回了 {\"id\":..., ...} 就说明 chat_id 配置正确、上传通道已通。"
echo "（该测试文件可在网页上删掉）"
