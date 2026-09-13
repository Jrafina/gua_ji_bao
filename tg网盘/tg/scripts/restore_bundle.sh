#!/usr/bin/env bash
# 从备份包恢复索引与日志（重建新服务器时用）
#
#   bash restore_bundle.sh /root/tgpool-backup-20260911-080137.tar.gz
#   bash restore_bundle.sh <包> --app-dir /tmp/dryrun     # 只试跑，不碰线上服务
#
# 备份包从哪来：在 Telegram 客户端里手动下载 `tgpool-backup-*.tar.gz`
# （每天 03:30 自动发到你的存储池会话），或者旧服务器还活着时 `--fetch` 取回。
set -euo pipefail

BUNDLE="${1:-}"
APP="/opt/tgpool"
DRYRUN=0
if [ "${2:-}" = "--app-dir" ] && [ -n "${3:-}" ]; then
  APP="$3"
  [ "$APP" != "/opt/tgpool" ] && DRYRUN=1
fi
if [ -z "$BUNDLE" ] || [ ! -f "$BUNDLE" ]; then
  echo "用法: $0 <tgpool-backup-*.tar.gz> [--app-dir 目标目录]"
  echo "  备份包需先从 Telegram 会话里下载下来"
  exit 2
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "=== 1. 解包 ==="
echo "  来源: $BUNDLE  ($(du -h "$BUNDLE" | cut -f1))"
tar xzf "$BUNDLE" -C "$WORK"
find "$WORK" -type f | sed "s|$WORK|  .|"

echo "=== 2. 校验 MANIFEST 里的 sha256 ==="
[ -f "$WORK/MANIFEST.json" ] || { echo "  [x] 包里没有 MANIFEST.json，文件不完整"; exit 3; }
python3 - "$WORK" <<'PY'
import hashlib, json, pathlib, sys
w = pathlib.Path(sys.argv[1])
m = json.loads((w / "MANIFEST.json").read_text(encoding="utf-8"))
c = m.get("counts", {}) or {}
print("  备份时间 : %s" % m.get("created_at_human"))
print("  来源主机 : %s" % m.get("host"))
print("  包内统计 : %d 个目录 / %d 个文件 / %.2f MB"
      % (c.get("folders", 0), c.get("files", 0), c.get("bytes", 0) / 1024 / 1024))

def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for k in iter(lambda: f.read(1 << 20), b""):
            h.update(k)
    return h.hexdigest()

ok = True
for label, rel, key in (("index.db", "index.db", "db_sha256"),
                        ("journal ", "journal/index.jsonl", "journal_sha256")):
    want = m.get(key)
    p = w / rel
    if not want:
        print("  %s : 包内无此文件（备份时可能就不存在）" % label)
        continue
    if not p.exists():
        print("  %s : [x] 清单里有、包里没有" % label)
        ok = False
        continue
    good = sha(p) == want
    print("  %s : %s" % (label, "sha256 一致" if good else "[x] sha256 不一致"))
    ok = ok and good
sys.exit(0 if ok else 3)
PY

echo "=== 3. 停服务 ==="
if [ "$DRYRUN" = "1" ]; then
  echo "  试跑模式（--app-dir），不动线上服务"
else
  systemctl stop tgpool 2>/dev/null && echo "  tgpool 已停" || echo "  （服务未运行）"
fi

echo "=== 4. 落盘到 $APP ==="
mkdir -p "$APP/journal"
if [ -f "$APP/index.db" ]; then
  BAK="$APP/index.db.pre-restore-$(date +%Y%m%d-%H%M%S)"
  cp -a "$APP/index.db" "$BAK"
  echo "  原库已另存: $BAK"
fi
install -m 600 "$WORK/index.db" "$APP/index.db"
rm -f "$APP/index.db-wal" "$APP/index.db-shm"
echo "  index.db 已恢复"
if [ -f "$WORK/journal/index.jsonl" ]; then
  install -m 644 "$WORK/journal/index.jsonl" "$APP/journal/index.jsonl"
  echo "  journal 已恢复（$(wc -l < "$APP/journal/index.jsonl") 行）"
fi

PY="$APP/venv/bin/python"
[ -x "$PY" ] || PY="python3"

echo "=== 5. 起服务 ==="
if [ "$DRYRUN" = "1" ]; then
  echo "  试跑模式，跳过"
else
  systemctl start tgpool
  sleep 3
  echo "  tgpool: $(systemctl is-active tgpool)"
fi

echo "=== 6. 校验日志与索引是否一致 ==="
"$PY" "$APP/tools/rebuild_index.py" --verify \
  --db "$APP/index.db" --journal "$APP/journal/index.jsonl"

echo
echo "完成。若上面显示「校验通过」，说明索引已完整恢复。"
if [ "$DRYRUN" != "1" ]; then
  echo "接下来确认网页能打开并列出文件："
  echo "  grep TG_AUTH_PASS $APP/tgpool.env"
fi
