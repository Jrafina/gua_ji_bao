#!/usr/bin/env bash
# 启用索引灾备：部署工具 + 导出 journal 起点 + 安装每日定时备份
set -euo pipefail

APP=/opt/tgpool
TOOLS=$APP/tools
PY=$APP/venv/bin/python

echo "=== 1. 建目录 ==="
mkdir -p "$TOOLS" "$APP/journal" "$APP/backup"
chmod 700 "$APP/backup"

echo "=== 2. 首次导出 journal 起点（从现有 index.db）==="
if [ -s "$APP/journal/index.jsonl" ]; then
  echo "   journal 已存在，跳过（如需重来：--seed --force）"
else
  "$PY" "$TOOLS/rebuild_index.py" --seed
fi

echo "=== 3. 安装每日定时备份 (/etc/cron.d/tgpool-backup) ==="
cat > /etc/cron.d/tgpool-backup <<'CRON'
# TG 存储池索引灾备 —— 每天 03:30 备份（本地 + 一份到 Telegram），并滚动清理过期远程备份
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
30 3 * * * root /opt/tgpool/venv/bin/python /opt/tgpool/tools/backup_index.py >> /var/log/tgpool-backup.log 2>&1; /opt/tgpool/venv/bin/python /opt/tgpool/tools/backup_index.py --prune-remote >> /var/log/tgpool-backup.log 2>&1
CRON
chmod 644 /etc/cron.d/tgpool-backup
# 日志轮转，避免无限增长
cat > /etc/logrotate.d/tgpool-backup <<'LR'
/var/log/tgpool-backup.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    copytruncate
}
LR

echo "=== 4. 立即跑一次备份 ==="
"$PY" "$TOOLS/backup_index.py"

echo "=== 5. 校验：journal 重放结果 vs 现库 ==="
"$PY" "$TOOLS/rebuild_index.py" --verify

echo
echo "完成。定时任务："
grep -v '^#' /etc/cron.d/tgpool-backup | grep .
