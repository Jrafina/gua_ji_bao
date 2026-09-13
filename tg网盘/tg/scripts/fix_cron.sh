#!/usr/bin/env bash
# 重新写入每日备份的 cron 任务
set -euo pipefail

cat > /etc/cron.d/tgpool-backup <<'CRON'
# TG 存储池索引灾备 —— 每天 03:30 备份（本地 + 一份到 Telegram），并滚动清理过期远程备份
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
30 3 * * * root /opt/tgpool/venv/bin/python /opt/tgpool/tools/backup_index.py >> /var/log/tgpool-backup.log 2>&1; /opt/tgpool/venv/bin/python /opt/tgpool/tools/backup_index.py --prune-remote >> /var/log/tgpool-backup.log 2>&1
CRON

chmod 644 /etc/cron.d/tgpool-backup
chown root:root /etc/cron.d/tgpool-backup

echo "--- /etc/cron.d/tgpool-backup ---"
cat /etc/cron.d/tgpool-backup
echo "--- 权限 ---"
ls -l /etc/cron.d/tgpool-backup
echo "--- cron 服务 ---"
systemctl is-active cron || systemctl is-active crond || echo "(未运行 cron)"
