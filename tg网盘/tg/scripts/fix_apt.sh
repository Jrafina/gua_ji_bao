#!/bin/bash
# 换用可达的 apt 源。只在默认源不通时才动手，避免无谓改动。
#   bash fix_apt.sh            # 自动判断（推荐）
#   FORCE=1 bash fix_apt.sh    # 强制换源
set -u

echo "=== 1. 探测发行版代号 ==="
CODENAME="${VERSION_CODENAME:-}"
if [ -z "$CODENAME" ] && [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  CODENAME="${VERSION_CODENAME:-}"
fi
[ -z "$CODENAME" ] && CODENAME="$(lsb_release -cs 2>/dev/null || true)"
[ -z "$CODENAME" ] && CODENAME="jammy"
echo "  代号: $CODENAME"
grep -E '^(NAME|VERSION)=' /etc/os-release 2>/dev/null | sed 's/^/  /'

echo "=== 2. 测试默认源 ==="
if timeout 15 curl -fsS -o /dev/null "http://archive.ubuntu.com/ubuntu/dists/${CODENAME}/Release"; then
  echo "  archive.ubuntu.com 可达"
  if [ "${FORCE:-0}" != "1" ]; then
    echo "→ 默认源可用，无需换源。如需强制：FORCE=1 bash $0"
    exit 0
  fi
else
  echo "  archive.ubuntu.com 不可达 —— 需要换源"
fi

echo "=== 3. 清理占锁的 apt 进程 ==="
pkill -9 -x apt-get 2>/dev/null || true
pkill -9 -x dpkg 2>/dev/null || true
sleep 2
ps aux | grep -E '[a]pt-get|[d]pkg' || echo "  无残留进程"

echo "=== 4. 挑最快镜像 ==="
BEST=""
BEST_T="999"
for m in mirrors.aliyun.com mirrors.tuna.tsinghua.edu.cn mirrors.ustc.edu.cn archive.ubuntu.com; do
  T="$(timeout 12 curl -s -o /dev/null -w '%{time_total}' "http://$m/ubuntu/dists/${CODENAME}/Release" 2>/dev/null || echo 99)"
  printf '  %-34s %ss\n' "$m" "$T"
  if awk "BEGIN{exit !($T < $BEST_T)}"; then BEST="$m"; BEST_T="$T"; fi
done
if [ -z "$BEST" ]; then
  echo "  [x] 所有镜像都不通，先查网络/防火墙"
  exit 1
fi
echo "  采用: $BEST (${BEST_T}s)"

echo "=== 5. 替换源（旧文件留备份）==="
[ -f /etc/apt/sources.list ] && cp -n /etc/apt/sources.list /etc/apt/sources.list.orig 2>/dev/null
cat > /etc/apt/sources.list <<EOF
deb http://${BEST}/ubuntu/ ${CODENAME} main restricted universe multiverse
deb http://${BEST}/ubuntu/ ${CODENAME}-updates main restricted universe multiverse
deb http://${BEST}/ubuntu/ ${CODENAME}-backports main restricted universe multiverse
deb http://${BEST}/ubuntu/ ${CODENAME}-security main restricted universe multiverse
EOF
cat /etc/apt/sources.list
# Ubuntu 24.04+ 默认用 /etc/apt/sources.list.d/ubuntu.sources（deb822），
# 不挪走会和上面这份并存，apt 仍去连不通的默认源而报错。
if [ -f /etc/apt/sources.list.d/ubuntu.sources ]; then
  mv /etc/apt/sources.list.d/ubuntu.sources /etc/apt/sources.list.d/ubuntu.sources.disabled
  echo "  已停用 /etc/apt/sources.list.d/ubuntu.sources（改为 .disabled）"
fi

echo "=== 6. apt-get update ==="
export DEBIAN_FRONTEND=noninteractive
apt-get update 2>&1 | tail -12
echo "=== done ==="
