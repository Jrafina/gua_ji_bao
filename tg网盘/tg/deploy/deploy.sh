#!/usr/bin/env bash
# shellcheck shell=bash disable=SC1090,SC2086,SC2015
# =============================================================================
#  TG 存储池 —— 一键部署 / 灾难重建（幂等）
#
#  在目标服务器（Ubuntu，root shell）里执行：
#      tar xzf tgpool-deploy.tar.gz
#      cd tgpool-deploy
#      bash deploy.sh
#
#  参数：
#      --check          只体检：检测现状并打印将要做的事，不改动任何东西
#      --yes            非交互：值取环境变量或已有配置，缺失即报错
#      --bundle FILE    顺手从备份包恢复索引（tgpool-backup-*.tar.gz）
#      --port N         对外 HTTPS 端口（默认 8443）
#      --app-dir DIR    安装目录（默认 /opt/tgpool；指定成别的目录 = 演练模式，
#                       只操作该目录，不碰 systemd / nginx / cron）
#
#  特点：可反复运行。每一步先检测，已经配置好的就跳过，不会破坏线上数据。
# =============================================================================
set -uo pipefail

SCRIPT_VER="1.5"
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="${TG_APP_DIR:-/opt/tgpool}"
PORT="${TG_NGINX_PORT:-8443}"
BUNDLE="${TG_BUNDLE:-}"
# bot API 本地文件缓存的上限（GB）。超过才从最旧的删起，平时不动，保证重复下载依然秒开。
CACHE_MAX_GB="${TG_CACHE_MAX_GB:-8}"
CHECK=0
ASSUME_YES=0

usage() {
  cat <<'USAGE'
TG 存储池 一键部署 / 重建

用法:
  bash deploy.sh [参数]

参数:
  --check          只体检：检测现状并打印将要做的事，不改动任何东西
  --yes            非交互：值取环境变量或已有配置，缺失即报错
  --bundle FILE    顺手从备份包恢复索引（tgpool-backup-*.tar.gz）
                   不指定时：若机器上（/root、当前目录、包所在目录等）能找到备份包
                   且还没装过，会问一句「用它恢复索引？」，回车/yes 即自动恢复
  --port N         对外 HTTPS 端口（默认 8443）
  --app-dir DIR    安装目录（默认 /opt/tgpool）;
                   指定成别处 = 演练模式，只操作该目录，不碰 systemd/nginx/cron
  -h, --help       显示本帮助

也可以用环境变量预填（配合 --yes）:
  TG_BOT_TOKEN  TG_CHAT_ID  TG_API_ID  TG_API_HASH
  TG_AUTH_USER  TG_AUTH_PASS  TG_BUNDLE  TG_NGINX_PORT  TG_APP_DIR
  TG_CACHE_MAX_GB     缓存瘦身阈值（GB，默认 8；超过才从最旧的删起）
  TG_FORCE_MIRROR=1   强制改用国内 apt 镜像（默认源可达时不换源）

示例:
  bash deploy.sh                                  # 交互式全新部署
  bash deploy.sh --check                          # 只体检
  bash deploy.sh --bundle /root/tgpool-backup-*.tar.gz
  TG_BOT_TOKEN=123:AA... bash deploy.sh --yes     # 无人值守
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --check)   CHECK=1; shift ;;
    --yes|-y)  ASSUME_YES=1; shift ;;
    --app-dir) APP="${2:?--app-dir 需要一个目录}"; shift 2 ;;
    --port)    PORT="${2:?--port 需要一个端口号}"; shift 2 ;;
    --bundle)  BUNDLE="${2:?--bundle 需要一个文件路径}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *)         printf '未知参数: %s\n\n' "$1"; usage; exit 2 ;;
  esac
done

if [ "$CHECK" = 1 ]; then ASSUME_YES=1; fi     # 体检模式不交互
case "$APP" in /opt/tgpool) SIM=0 ;; *) SIM=1 ;; esac

ENV="$APP/tgpool.env"
TOTAL=10

# ----------------------------------------------------------------------------
# 输出
# ----------------------------------------------------------------------------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_R=$'\033[0m'; C_B=$'\033[1m'; C_D=$'\033[2m'
  C_G=$'\033[32m'; C_Y=$'\033[33m'; C_RD=$'\033[31m'; C_C=$'\033[36m'
else
  C_R=; C_B=; C_D=; C_G=; C_Y=; C_RD=; C_C=
fi
hr()   { printf '%s%s%s\n' "$C_D" "────────────────────────────────────────────────────────────" "$C_R"; }
step() { printf '\n%s[%s/%s] %s%s\n' "$C_B$C_C" "$1" "$TOTAL" "$2" "$C_R"; }
ok()   { printf '      %s✔%s %s\n' "$C_G" "$C_R" "$1"; }
skip() { printf '      %s─%s %s\n' "$C_D" "$C_R" "$1"; }
warn() { printf '      %s! %s%s\n' "$C_Y" "$1" "$C_R"; }
bad()  { printf '      %s✖ %s%s\n' "$C_RD" "$1" "$C_R"; }
info() { printf '      %s%s%s\n' "$C_D" "$1" "$C_R"; }
die()  { printf '\n%s✖ %s%s\n\n' "$C_RD" "$1" "$C_R"; exit 1; }

# ----------------------------------------------------------------------------
# 执行包装（--check 下只打印，不动手）
# ----------------------------------------------------------------------------
run() {              # run <cmd...>
  if [ "$CHECK" = 1 ]; then info "[check] 将执行: $*"; return 0; fi
  "$@"
}
putfile() {          # putfile <path> [mode]   内容从 stdin 来；0=已写入 1=无需改动
  local p="$1" m="${2:-644}" t
  t="$(mktemp)"
  cat > "$t"
  if [ -f "$p" ] && cmp -s "$t" "$p"; then rm -f "$t"; return 1; fi
  if [ "$CHECK" = 1 ]; then info "[check] 将写入 $p"; rm -f "$t"; return 0; fi
  mkdir -p "$(dirname "$p")"
  install -m "$m" "$t" "$p"
  rm -f "$t"
  return 0
}

# ----------------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------------
mask() {             # mask <secret>
  # 注意：local 的参数会先展开再赋值，所以 n 必须单独一行，否则 set -u 下 $s 未定义
  local s="${1:-}"
  local n="${#s}"
  if [ "$n" -eq 0 ]; then printf '(空)'
  elif [ "$n" -le 18 ]; then printf '%*s' "$n" '' | tr ' ' '*'
  else printf '%s...%s' "${s:0:10}" "${s: -6}"; fi
}
have() { command -v "$1" >/dev/null 2>&1; }
os_field() {         # os_field <KEY>   安全读取 /etc/os-release（不用 source，避免污染变量）
  awk -F= -v k="$1" '$1==k{sub(/^[^=]*=/,"");gsub(/"/,"");print;exit}' /etc/os-release 2>/dev/null
}
mem_mb() { awk '/MemTotal/{print int($2/1024)}' /proc/meminfo 2>/dev/null; }
local_ip() { hostname -I 2>/dev/null | awk '{print $1}'; }

clean_input() {      # 去首尾空白 + 去一层成对引号（用户给含空格的路径加引号是本能）
  local v="$1"
  v="${v#"${v%%[![:space:]]*}"}"; v="${v%"${v##*[![:space:]]}"}"
  if [ "${#v}" -ge 2 ]; then
    local a="${v:0:1}" b="${v: -1}"
    if { [ "$a" = '"' ] && [ "$b" = '"' ]; } || { [ "$a" = "'" ] && [ "$b" = "'" ]; }; then
      v="${v:1:${#v}-2}"
    fi
  fi
  printf '%s' "$v"
}

find_bundle() {      # 自动找索引备份包；仅在「还没装过」时启用，避免覆盖线上索引
  [ -f "$APP/index.db" ] && return 0
  # 兼容浏览器对重复下载的自动改名：xxx.tar.gz -> xxx.tar (2).gz，
  # 所以按 tgpool-backup-*.gz 匹配（不要求 .tar.gz 结尾），同目录取 mtime 最新的。
  local d p
  for d in "$SELF/.." "$SELF" "$PWD" "$PWD/.." /root; do
    [ -d "$d" ] || continue
    p="$(find "$d" -maxdepth 1 -type f -name 'tgpool-backup-*.gz' -printf '%T@ %p\n' 2>/dev/null \
         | sort -rn | head -1 | cut -d' ' -f2-)"
    if [ -n "$p" ]; then printf '%s' "$p"; return 0; fi
  done
  return 0
}

env_get() {          # env_get <KEY>
  [ -f "$ENV" ] || return 0
  sed -n "s/^$1=//p" "$ENV" | head -1
}
api_base() { echo "http://127.0.0.1:8081"; }
api_raw() { curl -s --max-time 15 "$@" 2>/dev/null; }
api_ok() {           # api_ok <path>   判断 bot API 是否返回 ok:true
  local r
  r="$(curl -s --max-time 15 "$(api_base)/bot${IN_TOKEN}$1" 2>/dev/null)"
  case "$r" in *'"ok":true'*) return 0 ;; *) return 1 ;; esac
}

set_env_kv() {       # set_env_kv <KEY> <VALUE>
  local k="$1" v="$2"
  if [ "$CHECK" = 1 ]; then info "[check] 将设置 $k"; return 0; fi
  mkdir -p "$APP"
  python3 - "$ENV" "$k" "$v" <<'PY'
import sys, pathlib
f, k, val = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(f)
lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
out, hit = [], False
for ln in lines:
    if ln.startswith(k + "="):
        out.append("%s=%s" % (k, val)); hit = True
    else:
        out.append(ln)
if not hit:
    out.append("%s=%s" % (k, val))
p.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
p.chmod(0o600)
PY
}
set_env_if_diff() {  # 只在值变化时写入；0=已改 1=没变
  local k="$1" v="$2"
  [ "$(env_get "$k")" = "$v" ] && return 1
  set_env_kv "$k" "$v"
  return 0
}
ask_secret() {       # ask_secret <标题> <默认值>   → $INPUT
  local title="$1" def="$2"
  printf '      %s\n' "$title"
  [ -n "$def" ] && printf '      当前 %s  %s(回车保持不变)%s\n' "$(mask "$def")" "$C_D" "$C_R"
  printf '      %s> %s' "$C_C" "$C_R"
  if [ "$ASSUME_YES" = 1 ]; then printf '%s\n' "${C_D}(非交互，取默认值)${C_R}"; INPUT="$def"; return; fi
  read -rs INPUT; printf '\n'
  INPUT="$(clean_input "$INPUT")"
  [ -z "$INPUT" ] && INPUT="$def"
}
ask_text() {         # ask_text <标题> <默认值>    → $INPUT
  local title="$1" def="$2"
  printf '      %s' "$title"
  [ -n "$def" ] && printf ' %s[%s]%s' "$C_D" "$def" "$C_R"
  printf '\n      %s> %s' "$C_C" "$C_R"
  if [ "$ASSUME_YES" = 1 ]; then printf '%s\n' "${C_D}(非交互)${C_R}"; INPUT="$def"; return; fi
  read -r INPUT; printf '\n'
  INPUT="$(clean_input "$INPUT")"
  [ -z "$INPUT" ] && INPUT="$def"
}
confirm() {          # confirm <问题>  0=是
  [ "$ASSUME_YES" = 1 ] && return 0
  local a
  printf '\n      %s [y/N] ' "$1"
  read -r a
  case "$a" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

# ============================================================================
#  输入收集
# ============================================================================
collect() {
  local cur
  printf '\n%s需要你提供下面几项。直接回车 = 用默认值 / 保持现有配置。%s\n' "$C_B" "$C_R"

  # ---- 1. Bot Token（必需）----
  cur="${TG_BOT_TOKEN:-$(env_get TG_BOT_TOKEN)}"
  printf '\n%s(1/5) Bot Token%s  %s【必需】Telegram 里找 @BotFather → /newbot，形如 123456789:AAH...%s\n' \
    "$C_B" "$C_R" "$C_D" "$C_R"
  while :; do
    ask_secret "      " "$cur"
    IN_TOKEN="$INPUT"
    if [ -z "$IN_TOKEN" ]; then
      if [ "$CHECK" = 1 ]; then IN_TOKEN="<未提供>"; break; fi
      bad "token 不能为空。"
      die "非交互模式缺少 TG_BOT_TOKEN（用法: TG_BOT_TOKEN=xxx bash deploy.sh --yes）"
    fi
    if [ "$CHECK" = 1 ] && [ "$IN_TOKEN" = "<未提供>" ]; then break; fi
    if ! printf '%s' "$IN_TOKEN" | grep -Eq '^[0-9]{5,}:[A-Za-z0-9_-]{25,}$'; then
      bad "格式不像 bot token（应是「数字:一长串字母数字_-」），请重新粘贴。"
      [ "$ASSUME_YES" = 1 ] && die "TG_BOT_TOKEN 格式不正确: $(mask "$IN_TOKEN")"
      cur=""; continue
    fi
    break
  done

  # ---- 2. chat_id（可留空，服务起来后自动探测）----
  cur="${TG_CHAT_ID:-$(env_get TG_CHAT_ID)}"
  printf '\n%s(2/5) 存储池会话 chat_id%s  %s【可留空】留空则稍后自动探测（需你先给 bot 发一条消息）%s\n' \
    "$C_B" "$C_R" "$C_D" "$C_R"
  ask_text "      " "$cur"
  IN_CHAT="$INPUT"

  # ---- 3. api_id / api_hash ----
  cur="${TG_API_ID:-$(env_get TG_API_ID)}"; [ -z "$cur" ] && cur="2040"
  printf '\n%s(3/5) api_id%s  %s【默认 2040】Telegram Desktop 官方公开值；自己申请到可覆盖%s\n' \
    "$C_B" "$C_R" "$C_D" "$C_R"
  ask_text "      " "$cur"
  IN_API_ID="$INPUT"

  cur="${TG_API_HASH:-$(env_get TG_API_HASH)}"; [ -z "$cur" ] && cur="b18441a1ff607e10a989891a5462e627"
  printf '\n%s(4/5) api_hash%s  %s【默认即下面这串，回车即可】%s\n' "$C_B" "$C_R" "$C_D" "$C_R"
  ask_secret "      " "$cur"
  IN_API_HASH="$INPUT"

  # ---- 4. 网页账号 / 密码 ----
  cur="${TG_AUTH_USER:-$(env_get TG_AUTH_USER)}"; [ -z "$cur" ] && cur="admin"
  printf '\n%s(5/5) 网页访问账号 / 密码%s\n' "$C_B" "$C_R"
  ask_text "      账号" "$cur"
  [ -z "$INPUT" ] && INPUT="admin"
  IN_AUTH_USER="$INPUT"

  cur="${TG_AUTH_PASS:-$(env_get TG_AUTH_PASS)}"
  if [ -z "$cur" ]; then
    cur="$(tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 16)"
    info "已自动生成 16 位随机密码（想自己定就直接输入）"
  fi
  ask_text "      密码" "$cur"
  [ -z "$INPUT" ] && INPUT="$cur"
  IN_AUTH_PASS="$INPUT"

  # ---- 备份包（可选；没填就在机上自动找）----
  if [ -n "$BUNDLE" ] && [ ! -f "$BUNDLE" ]; then
    warn "指定的备份包不存在: $BUNDLE（已跳过恢复）"
    BUNDLE=""
  fi
  if [ -z "$BUNDLE" ]; then
    local auto
    auto="$(find_bundle)"
    if [ -n "$auto" ]; then
      if [ "$CHECK" = 1 ]; then
        BUNDLE="$auto"
      elif confirm "检测到索引备份包 $(basename "$auto")，用它恢复索引？"; then
        BUNDLE="$auto"; ok "将用该备份包恢复索引"
      else
        info "已跳过恢复（按全新安装处理，索引从空开始）"
      fi
    fi
  fi
  if [ -z "$BUNDLE" ] && [ "$CHECK" = 0 ] && [ "$ASSUME_YES" = 0 ]; then
    printf '\n%s可选：索引备份包%s  %s从 Telegram 下载的 tgpool-backup-*.tar.gz 的完整路径%s\n' \
      "$C_B" "$C_R" "$C_D" "$C_R"
    printf '      %s新装可留空；重建时建议填，能直接恢复目录与文件索引%s\n' "$C_D" "$C_R"
    printf '      %s路径可带引号；tgpool-backup-*.tar (2).gz 这类浏览器改名副本也能识别%s\n' "$C_D" "$C_R"
    ask_text "      路径" ""
    BUNDLE="$INPUT"
    if [ -n "$BUNDLE" ] && [ ! -f "$BUNDLE" ]; then
      warn "文件不存在，已忽略：$BUNDLE"; BUNDLE=""
    fi
  fi
}

# ============================================================================
#  前置检查 & 现状
# ============================================================================
preflight() {
  [ "$(id -u)" = 0 ] || die "请以 root 运行（sudo bash deploy.sh）"
  [ -f "$SELF/app/app.py" ] || die "部署包不完整：找不到 $SELF/app/app.py

  请确认已完整解压 tgpool-deploy.tar.gz，并在解压出来的目录里运行本脚本。"
  local f
  for f in app/static/index.html app/static/background.jpg app/requirements.txt \
           tools/rebuild_index.py tools/backup_index.py tools/clean_cache.py \
           tools/show_password.py tools/test_search_logic.py tgpool.service; do
    [ -f "$SELF/$f" ] || die "部署包不完整：缺少 $f"
  done
  if [ -r /etc/os-release ]; then
    if ! grep -qi ubuntu /etc/os-release; then
      warn "本脚本在 Ubuntu 上验证过；当前系统是 $(os_field PRETTY_NAME)，可能不适用"
    fi
  fi
}

show_plan() {
  printf '\n%s现状%s\n' "$C_B" "$C_R"
  hr
  [ -r /etc/os-release ] && printf '  系统      %s (%s)\n' "$(os_field PRETTY_NAME)" "$(os_field VERSION_CODENAME)"
  printf '  CPU/内存  %s 核 / %s MB\n' "$(nproc 2>/dev/null || echo ?)" "$(mem_mb)"
  printf '  磁盘可用  %s\n' "$(df -h / 2>/dev/null | awk 'NR==2{print $4}')"
  if [ -n "$(swapon --show --noheadings 2>/dev/null)" ]; then
    printf '  swap      %s\n' "$(free -h | awk '/Swap/{print $2}')"
  else
    printf '  swap      %s无%s\n' "$C_Y" "$C_R"
  fi
  printf '  Docker    %s\n' "$(have docker && docker --version 2>/dev/null | awk '{print $3}' | tr -d , || echo '未安装')"
  printf '  nginx     %s\n' "$(have nginx && nginx -v 2>&1 | sed 's/^nginx version: nginx\///' || echo '未安装')"
  if [ -f "$ENV" ]; then
    printf '  已有配置  %s有%s  %s\n' "$C_G" "$C_R" "$ENV"
  else
    printf '  已有配置  %s无 —— 按全新安装处理%s\n' "$C_Y" "$C_R"
  fi
  printf '  安装目录  %s' "$APP"
  [ "$SIM" = 1 ] && printf '  %s[演练模式：不碰 systemd/nginx/cron]%s' "$C_Y" "$C_R"
  printf '\n  HTTPS端口 %s\n' "$PORT"
  [ -n "$BUNDLE" ] && printf '  备份包    %s\n' "$BUNDLE"
  hr
}

# ============================================================================
#  1. swap
# ============================================================================
step_swap() {
  step 1 "交换空间 (swap)"
  if [ -n "$(swapon --show --noheadings 2>/dev/null)" ]; then
    skip "已有 swap（$(free -h | awk '/Swap/{print $2}')），不动"
    return 0
  fi
  local m avail size
  m="$(mem_mb)"
  avail="$(df -BG / 2>/dev/null | awk 'NR==2{gsub("G","",$4); print $4}')"
  [ -z "$avail" ] && avail=0
  if [ "$m" -ge 4096 ]; then
    skip "内存 ${m}MB 充足，不需要 swap"
    return 0
  fi
  size=4
  [ "$avail" -lt 20 ] && size=2
  [ "$avail" -lt 8 ] && size=1
  info "内存仅 ${m}MB，加 ${size}G swap（Docker 与运行更稳）"
  if [ "$CHECK" = 1 ]; then info "[check] 将创建 /swapfile (${size}G) 并写入 /etc/fstab"; return 0; fi
  fallocate -l "${size}G" /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=$((size*1024)) status=none
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile || die "swap 启用失败（磁盘空间不足？）"
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  ok "swap 就绪：$(free -h | awk '/Swap/{print $2}')"
}

# ============================================================================
#  2. apt 源
# ============================================================================
_probe() {           # _probe <host> <codename>  → 成功回显耗时秒数；失败回显 FAIL:原因
  local host="$1" cn="$2" t0 t1 rc
  t0="$(date +%s%N)"
  if have curl; then
    timeout 12 curl -s -o /dev/null "http://$host/ubuntu/dists/$cn/Release" >/dev/null 2>&1
    rc=$?
    t1="$(date +%s%N)"
    case "$rc" in
      0)      awk "BEGIN{printf \"%.2f\", ($t1-$t0)/1000000000}" ;;
      6)      printf 'FAIL:DNS 解析失败' ;;
      5|7)    printf 'FAIL:连接被拒' ;;
      28|124) printf 'FAIL:超时(12s)' ;;
      *)      printf 'FAIL:curl 退出码 %s' "$rc" ;;
    esac
    return 0
  fi
  if have wget; then
    timeout 12 wget -q -O /dev/null "http://$host/ubuntu/dists/$cn/Release" >/dev/null 2>&1
    rc=$?
    t1="$(date +%s%N)"
    if [ "$rc" -eq 0 ]; then
      awk "BEGIN{printf \"%.2f\", ($t1-$t0)/1000000000}"
    else
      printf 'FAIL:wget 失败(码 %s)' "$rc"
    fi
    return 0
  fi
  if timeout 8 bash -c "exec 3<>/dev/tcp/$host/80" >/dev/null 2>&1; then
    t1="$(date +%s%N)"
    awk "BEGIN{printf \"%.2f\", ($t1-$t0)/1000000000}"
  else
    printf 'FAIL:TCP 80 不通'
  fi
  return 0
}

step_apt() {
  step 2 "apt 软件源"
  export DEBIAN_FRONTEND=noninteractive
  local cn; cn="$(os_field VERSION_CODENAME)"
  [ -z "$cn" ] && cn="$(lsb_release -cs 2>/dev/null || true)"
  [ -z "$cn" ] && cn="jammy"
  info "发行版代号: $cn"

  local pr; pr="$(_probe archive.ubuntu.com "$cn")"
  [ "${TG_FORCE_MIRROR:-0}" = 1 ] && pr="FAIL:TG_FORCE_MIRROR=1（强制换源）"
  if [ "${pr%%:*}" != FAIL ]; then
    skip "默认源 archive.ubuntu.com 可达（${pr}s），不换源"
  else
    info "默认源不可达（${pr#FAIL:}），逐个测试镜像…"
    local best="" best_t="999" m r
    for m in mirrors.aliyun.com mirrors.tuna.tsinghua.edu.cn mirrors.ustc.edu.cn; do
      r="$(_probe "$m" "$cn")"
      if [ "${r%%:*}" = FAIL ]; then
        info "  $m → ${r#FAIL:}"
      else
        info "  $m → ${r}s"
        if awk "BEGIN{exit !($r < $best_t)}"; then best="$m"; best_t="$r"; fi
      fi
    done
    if [ -z "$best" ]; then
      bad "所有 apt 镜像都不可达，无法继续。请先在本机确认出网（在服务器上执行）："
      info "  command -v curl wget;  cat /etc/resolv.conf"
      info "  getent hosts mirrors.aliyun.com"
      info "  timeout 6 bash -c 'exec 3<>/dev/tcp/223.5.5.5/80 && echo TCP-OK'"
      die "网络 / DNS 不通（每条镜像的失败原因见上）"
    fi
    ok "采用镜像 $best (${best_t}s)"
    if grep -q "http://${best}/ubuntu/ ${cn} " /etc/apt/sources.list 2>/dev/null; then
      skip "apt 源已经是 $best，无需改动"
    elif [ "$CHECK" = 1 ]; then
      info "[check] 将把 /etc/apt/sources.list 换成 $best"
    else
      [ -f /etc/apt/sources.list ] && cp -n /etc/apt/sources.list /etc/apt/sources.list.orig 2>/dev/null
      cat > /etc/apt/sources.list <<EOF
deb http://${best}/ubuntu/ ${cn} main restricted universe multiverse
deb http://${best}/ubuntu/ ${cn}-updates main restricted universe multiverse
deb http://${best}/ubuntu/ ${cn}-backports main restricted universe multiverse
deb http://${best}/ubuntu/ ${cn}-security main restricted universe multiverse
EOF
      # Ubuntu 24.04+ 用 deb822 源，不挪走会与上面这份并存，apt 仍去连不通的默认源
      if [ -f /etc/apt/sources.list.d/ubuntu.sources ]; then
        mv /etc/apt/sources.list.d/ubuntu.sources /etc/apt/sources.list.d/ubuntu.sources.disabled
        info "已停用 /etc/apt/sources.list.d/ubuntu.sources"
      fi
    fi
  fi

  if [ "$CHECK" = 1 ]; then
    info "[check] 将执行 apt-get update"
  else
    if ! apt-get update -qq >/tmp/.tgpool-apt.log 2>&1; then
      bad "apt-get update 失败。日志末尾："
      tail -6 /tmp/.tgpool-apt.log 2>/dev/null | sed 's/^/      /'
      info "  排查命令：cat /etc/resolv.conf ; getent hosts mirrors.aliyun.com"
      info "  若确认本机能上网，可强制换源后重跑：TG_FORCE_MIRROR=1 bash deploy.sh"
      rm -f /tmp/.tgpool-apt.log
      die "apt 源不可用"
    fi
    rm -f /tmp/.tgpool-apt.log
    ok "apt 索引已更新"
  fi
  if ! have curl; then
    info "安装 curl / ca-certificates"
    run apt-get install -y -qq curl ca-certificates
  fi
}

# ============================================================================
#  3. Docker
# ============================================================================
step_docker() {
  step 3 "Docker"
  export DEBIAN_FRONTEND=noninteractive
  local need_restart=0

  if have docker; then
    skip "Docker 已安装（$(docker --version 2>/dev/null | awk '{print $3}' | tr -d ,)）"
  else
    info "安装 docker.io（apt）"
    run apt-get install -y -qq docker.io
    [ "$CHECK" = 1 ] || have docker || die "Docker 安装失败，请查看上面 apt 的输出"
    need_restart=1
  fi

  if [ ! -f /etc/docker/daemon.json ]; then
    if putfile /etc/docker/daemon.json 644 <<'JSON'
{
  "registry-mirrors": [
    "https://docker.m.daocloud.io",
    "https://dockerproxy.com",
    "https://mirror.baidubce.com",
    "https://hub-mirror.c.163.com"
  ],
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
JSON
    then ok "已写入 Docker 镜像加速配置"; need_restart=1
    else skip "daemon.json 无需改动"; fi
  else
    skip "已有 /etc/docker/daemon.json，保持不动"
  fi
  if [ "$CHECK" = 1 ]; then info "[check] 将确保 docker 服务在运行"; return 0; fi

  systemctl enable docker >/dev/null 2>&1
  if systemctl is-active --quiet docker; then
    if [ "$need_restart" = 1 ]; then
      systemctl restart docker; sleep 3
      ok "Docker 已重启以应用配置"
    else
      skip "Docker 已在运行"
    fi
  else
    systemctl start docker; sleep 3
    ok "Docker 已启动"
  fi
  systemctl is-active --quiet docker || die "Docker 未能启动（systemctl status docker 看详情）"
  docker ps >/dev/null 2>&1 || die "Docker 守护进程不可用"
}

# ============================================================================
#  4. 应用文件 + 配置
# ============================================================================
step_files() {
  step 4 "应用文件与配置"

  if [ "$CHECK" = 0 ]; then
    mkdir -p "$APP/app/static" "$APP/tools" "$APP/tmp" "$APP/journal" "$APP/backup"
    chmod 700 "$APP/backup"
  fi

  local pair src dst n=0
  for pair in \
      "app/app.py:$APP/app/app.py" \
      "app/static/index.html:$APP/app/static/index.html" \
      "app/static/background.jpg:$APP/app/static/background.jpg" \
      "app/requirements.txt:$APP/app/requirements.txt" \
      "tools/rebuild_index.py:$APP/tools/rebuild_index.py" \
      "tools/backup_index.py:$APP/tools/backup_index.py" \
      "tools/clean_cache.py:$APP/tools/clean_cache.py" \
      "tools/show_password.py:$APP/tools/show_password.py" \
      "tools/test_search_logic.py:$APP/tools/test_search_logic.py" \
      "tgpool.service:$APP/tgpool.service"; do
    src="${pair%%:*}"; dst="${pair##*:}"
    if [ -f "$dst" ] && cmp -s "$SELF/$src" "$dst"; then continue; fi
    n=$((n+1))
    if [ "$CHECK" = 1 ]; then info "[check] 将更新 $dst"; continue; fi
    install -m 644 "$SELF/$src" "$dst"
    info "已更新 $dst"
  done
  if [ "$n" = 0 ]; then
    skip "文件与部署包一致，无需改动"
  elif [ "$CHECK" = 1 ]; then
    info "↑ 共 $n 个文件需要更新"
  else
    ok "$n 个文件已同步"
  fi
  # 找回密码的小工具要能直接执行（sudo /opt/tgpool/tools/show_password.py）
  if [ "$CHECK" = 0 ] && [ -f "$APP/tools/show_password.py" ]; then
    chmod 755 "$APP/tools/show_password.py"
  fi

  local had_env=0
  [ -f "$ENV" ] && had_env=1
  set_env_if_diff TG_BOT_TOKEN   "$IN_TOKEN"     && info "TG_BOT_TOKEN 已更新"
  set_env_if_diff TG_CHAT_ID     "$IN_CHAT"      && info "TG_CHAT_ID 已更新"
  set_env_if_diff TG_API_ID      "$IN_API_ID"    && info "TG_API_ID 已更新"
  set_env_if_diff TG_API_HASH    "$IN_API_HASH"  && info "TG_API_HASH 已更新"
  set_env_if_diff TG_AUTH_USER   "$IN_AUTH_USER" && info "TG_AUTH_USER 已更新"
  set_env_if_diff TG_AUTH_PASS   "$IN_AUTH_PASS" && info "TG_AUTH_PASS 已更新"
  set_env_if_diff TG_API_BASE    "http://127.0.0.1:8081"
  set_env_if_diff TG_TMP_DIR     "$APP/tmp"
  set_env_if_diff TG_DB_PATH     "$APP/index.db"
  set_env_if_diff TG_JOURNAL     "$APP/journal/index.jsonl"
  set_env_if_diff TG_LOCAL_ROOT  "/var/lib/telegram-bot-api"
  set_env_if_diff TG_NGINX_PORT  "$PORT"
  set_env_if_diff TG_ENV_FILE    "$ENV"
  if [ "$CHECK" = 1 ]; then
    info "（$ENV 已核对：上面未列出的配置项均无需改动）"
    return 0
  fi
  chmod 600 "$ENV"
  if [ "$had_env" = 0 ]; then ok "已创建 $ENV（权限 600）"; else ok "配置已核对（权限 600）"; fi
}

# ============================================================================
#  5. Python 环境 + systemd
# ============================================================================
step_venv() {
  step 5 "Python 环境与服务"
  export DEBIAN_FRONTEND=noninteractive

  if [ "$CHECK" = 1 ]; then
    if dpkg -s python3-venv >/dev/null 2>&1 && dpkg -s python3-pip >/dev/null 2>&1; then
      skip "python3-venv / python3-pip 已安装"
    else
      info "[check] 将安装 python3-venv python3-pip"
    fi
  else
    apt-get install -y -qq python3-venv python3-pip >/dev/null 2>&1 || true
  fi

  if [ -d "$APP/venv" ] && [ -x "$APP/venv/bin/python" ]; then
    skip "虚拟环境已存在"
  else
    info "创建虚拟环境 $APP/venv"
    run python3 -m venv "$APP/venv"
  fi

  if [ -x "$APP/venv/bin/pip" ] && \
     "$APP/venv/bin/pip" show fastapi uvicorn httpx python-multipart >/dev/null 2>&1; then
    skip "Python 依赖已就绪"
  elif [ "$CHECK" = 1 ]; then
    info "[check] 将安装 requirements.txt 里的依赖"
  else
    info "安装依赖（首次约 1 分钟）"
    "$APP/venv/bin/pip" install -q --upgrade pip >/dev/null 2>&1
    "$APP/venv/bin/pip" install -q -r "$APP/app/requirements.txt" \
      || die "依赖安装失败（检查网络 / pip 源）"
    ok "依赖安装完成"
  fi

  if [ "$SIM" = 1 ]; then skip "演练模式（--app-dir），不注册 systemd 服务"; return 0; fi

  if [ -f /etc/systemd/system/tgpool.service ] && \
     cmp -s "$SELF/tgpool.service" /etc/systemd/system/tgpool.service; then
    skip "systemd 服务已存在且一致"
    if [ "$CHECK" = 1 ]; then return 0; fi
  elif [ "$CHECK" = 1 ]; then
    info "[check] 将注册 /etc/systemd/system/tgpool.service"
    return 0
  else
    install -m 644 "$APP/tgpool.service" /etc/systemd/system/tgpool.service
    ok "systemd 服务已注册/更新"
  fi
  systemctl daemon-reload
  systemctl enable tgpool >/dev/null 2>&1

  if [ -z "$(env_get TG_BOT_TOKEN)" ]; then
    warn "token 为空，暂不启动服务"
  else
    systemctl restart tgpool; sleep 3
    if systemctl is-active --quiet tgpool; then
      ok "tgpool 服务运行中"
    else
      bad "tgpool 启动失败，最近日志："
      journalctl -u tgpool -n 15 --no-pager 2>&1 | sed 's/^/        /'
    fi
  fi
}

# ============================================================================
#  6. bot API server 容器
# ============================================================================
container_ok() {     # 容器在跑，且 token / api_id / local 模式都对得上
  have docker || return 1
  local ids envs cur_api cur_local
  ids="$(docker ps --filter 'name=^tg-bot-api$' --filter status=running -q 2>/dev/null)"
  [ -n "$ids" ] || return 1
  envs="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' tg-bot-api 2>/dev/null)"
  cur_api="$(printf '%s\n' "$envs" | sed -n 's/^TELEGRAM_API_ID=//p')"
  cur_local="$(printf '%s\n' "$envs" | sed -n 's/^TELEGRAM_LOCAL=//p')"
  [ "$cur_api" = "$IN_API_ID" ] || return 1
  [ "$cur_local" = "1" ] || return 1
  api_ok /getMe
}

step_botapi() {
  step 6 "Telegram bot API server"
  have docker || die "Docker 不可用，无法启动 bot API 容器"

  if docker image inspect aiogram/telegram-bot-api:latest >/dev/null 2>&1; then
    skip "镜像已存在"
  else
    info "拉取镜像 aiogram/telegram-bot-api:latest"
    run docker pull aiogram/telegram-bot-api:latest
    [ "$CHECK" = 1 ] || docker image inspect aiogram/telegram-bot-api:latest >/dev/null 2>&1 \
      || die "镜像拉取失败（可先给 Docker 配好镜像加速再重来）"
  fi

  if [ "$CHECK" = 1 ]; then
    if container_ok; then skip "容器运行中，token 有效"
    else info "[check] 容器缺失或配置不一致，将重建 tg-bot-api"; fi
    return 0
  fi

  if container_ok; then
    skip "容器运行中，token 有效"
    return 0
  fi

  info "创建/重建容器 tg-bot-api（--local 模式）"
  mkdir -p /var/lib/telegram-bot-api
  docker rm -f tg-bot-api >/dev/null 2>&1 || true
  docker run -d --name tg-bot-api \
    --restart always --memory 420m \
    -p 127.0.0.1:8081:8081 \
    -e TELEGRAM_API_ID="$IN_API_ID" \
    -e TELEGRAM_API_HASH="$IN_API_HASH" \
    -e TELEGRAM_LOCAL=1 \
    -e TELEGRAM_WORK_DIR=/var/lib/telegram-bot-api \
    -e TELEGRAM_TEMP_DIR=/tmp/telegram-bot-api \
    -v /var/lib/telegram-bot-api:/var/lib/telegram-bot-api \
    aiogram/telegram-bot-api:latest >/dev/null || die "容器启动失败"

  local i
  for i in 1 2 3 4 5 6 7 8 9 10; do
    sleep 3
    if api_ok /getMe; then ok "容器就绪，getMe 正常"; return 0; fi
  done
  bad "容器 30 秒内未就绪，日志："
  docker logs --tail 20 tg-bot-api 2>&1 | sed 's/^/        /'
  die "bot API 容器未就绪"
}

# ============================================================================
#  7. 恢复索引备份（可选）
# ============================================================================
step_restore() {
  step 7 "恢复索引备份"
  if [ -z "$BUNDLE" ]; then
    skip "未提供备份包（重建时用 --bundle /path/tgpool-backup-*.tar.gz）"
    return 0
  fi
  [ -f "$BUNDLE" ] || { warn "备份包不存在：$BUNDLE"; return 0; }
  if [ "$CHECK" = 1 ]; then info "[check] 将解包、校验 sha256 并恢复到 $APP"; return 0; fi

  local work; work="$(mktemp -d)"
  info "解包 $(basename "$BUNDLE") ($(du -h "$BUNDLE" | cut -f1))"
  tar xzf "$BUNDLE" -C "$work" || { rm -rf "$work"; die "解包失败，文件可能已损坏"; }
  if [ ! -f "$work/MANIFEST.json" ]; then
    rm -rf "$work"; die "包里没有 MANIFEST.json，不是有效的备份包"
  fi

  local vy=0
  python3 - "$work" <<'PY' || vy=$?
import hashlib, json, pathlib, sys
w = pathlib.Path(sys.argv[1])
m = json.loads((w / "MANIFEST.json").read_text(encoding="utf-8"))
c = m.get("counts", {}) or {}
print("      备份时间 %s / 来源主机 %s" % (m.get("created_at_human"), m.get("host")))
print("      包内     %d 目录 / %d 文件 / %.2f MB"
      % (c.get("folders", 0), c.get("files", 0), c.get("bytes", 0) / 1048576))
def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for k in iter(lambda: f.read(1 << 20), b""):
            h.update(k)
    return h.hexdigest()
ok = True
for label, rel, key in (("index.db", "index.db", "db_sha256"),
                        ("journal ", "journal/index.jsonl", "journal_sha256")):
    want, p = m.get(key), w / rel
    if not want:
        print("      %s : 包内无此文件" % label); continue
    good = p.exists() and sha(p) == want
    print("      %s : %s" % (label, "sha256 一致" if good else "sha256 不一致"))
    ok = ok and good
sys.exit(0 if ok else 3)
PY
  if [ "$vy" != 0 ]; then rm -rf "$work"; die "备份包 sha256 校验未通过（文件损坏或被篡改）"; fi

  if [ "$SIM" = 0 ]; then systemctl stop tgpool 2>/dev/null || true; fi
  if [ -f "$APP/index.db" ]; then
    local bak="$APP/index.db.pre-restore-$(date +%Y%m%d-%H%M%S)"
    cp -a "$APP/index.db" "$bak"; info "原索引已另存 $(basename "$bak")"
  fi
  install -m 600 "$work/index.db" "$APP/index.db"
  rm -f "$APP/index.db-wal" "$APP/index.db-shm"
  if [ -f "$work/journal/index.jsonl" ]; then
    install -m 644 "$work/journal/index.jsonl" "$APP/journal/index.jsonl"
    info "journal 已恢复（$(wc -l < "$APP/journal/index.jsonl") 行）"
  fi
  rm -rf "$work"
  ok "索引已恢复"

  if [ "$SIM" = 0 ]; then
    systemctl start tgpool; sleep 3
    if systemctl is-active --quiet tgpool; then ok "tgpool 已重启"; else warn "tgpool 未启动，稍后看日志"; fi
  fi
}

# ============================================================================
#  8. 绑定 chat_id
# ============================================================================
step_chatid() {
  step 8 "绑定存储池会话 chat_id"
  local cur; cur="$(env_get TG_CHAT_ID)"
  if [ -n "$cur" ] && [ "$cur" != "0" ]; then
    skip "已配置：$cur"
    return 0
  fi
  if [ "$CHECK" = 1 ]; then
    info "[check] chat_id 为空，将自动探测（需先给 bot 发一条消息）"
    return 0
  fi

  local cid="${IN_CHAT:-}" upd n attempt stopped=""
  # 同一个 bot token 只允许一个 getUpdates 消费者。本机已装的 tgpool（重部署场景）
  # 会把用户刚发的消息秒抢走，探测永远为空 —— 先把它停了，稍后再拉起。
  if systemctl is-active --quiet tgpool 2>/dev/null; then
    systemctl stop tgpool; stopped=1
    warn "已临时停止本机正在运行的 tgpool（它在抢 getUpdates 消息），稍后自动重启"
    sleep 2
  fi
  for attempt in 1 2 3; do
    if [ -z "$cid" ]; then
      upd="$(api_raw "$(api_base)/bot${IN_TOKEN}/getUpdates?limit=100")"
      n="$(printf '%s' "$upd" | python3 -c 'import json,sys
try: print(len(json.load(sys.stdin).get("result",[])))
except Exception: print(0)')"
      if [ "$n" != "0" ]; then
        cid="$(printf '%s' "$upd" | python3 -c '
import json, sys
d = json.load(sys.stdin)
best = None
for u in d.get("result", []):
    m = u.get("message") or u.get("channel_post") or u.get("edited_message") or {}
    c = m.get("chat") or {}
    if c.get("id") is None:
        continue
    score = (1 if c.get("type") == "private" else 0, m.get("date", 0))
    if best is None or score > best[0]:
        best = (score, c.get("id"))
print(best[1] if best else "")')"
      fi
    fi

    if [ -n "$cid" ]; then
      set_env_kv TG_CHAT_ID "$cid"
      ok "已写入 TG_CHAT_ID=$cid"
      if [ "$SIM" = 1 ]; then info "演练模式：跳过重启与上传测试"; return 0; fi
      systemctl restart tgpool; sleep 3
      if systemctl is-active --quiet tgpool; then ok "tgpool 已重启"; else warn "tgpool 未启动"; fi
      head -c 1024 /dev/urandom > "$APP/tmp/_probe.bin"
      local res
      res="$(curl -s --max-time 60 -u "${IN_AUTH_USER}:${IN_AUTH_PASS}" \
        -F "file=@$APP/tmp/_probe.bin;filename=_probe.bin" \
        http://127.0.0.1:8080/api/upload)"
      rm -f "$APP/tmp/_probe.bin"
      case "$res" in
        *'"id"'*) ok "上传通道已打通（探测文件可在网页上删掉）" ;;
        *) warn "上传测试未通过：$(printf '%s' "$res" | head -c 200)" ;;
      esac
      return 0
    fi

    if [ "$ASSUME_YES" = 1 ]; then
      if [ -n "$stopped" ]; then systemctl start tgpool 2>/dev/null || true; fi
      die "非交互模式下无法自动探测 chat_id。请先在 Telegram 给 bot 发一条消息，再用 TG_CHAT_ID=xxx 重跑。"
    fi
    printf '\n      %s还没收到任何消息。%s\n' "$C_Y" "$C_R"
    printf '      请打开 Telegram → 搜索你的 bot → 给它发一条任意消息（如 hi）\n'
    printf '      %s发了一条仍收不到？另一台旧机器若还在跑同一个 bot 的存储池，%s\n' "$C_Y" "$C_R"
    printf '      %s会把消息抢走。先去旧机器执行：%s\n' "$C_Y" "$C_R"
    printf '        systemctl stop tgpool && docker stop tg-bot-api\n'
    printf '      %s（同一 token 不允许两台机器同时轮询）然后回这里再发一条新消息%s\n' "$C_D" "$C_R"
    printf '      发完回车重试（第 %s/3 次）：' "$attempt"
    read -r _
  done
  if [ -n "$stopped" ]; then
    warn "探测失败，恢复本机 tgpool"
    systemctl start tgpool 2>/dev/null
  fi
  die "3 次都没探测到。可手动指定：TG_CHAT_ID=xxx bash deploy.sh --yes"
}

# ============================================================================
#  9. HTTPS 入口 (nginx)
# ============================================================================
step_nginx() {
  step 9 "HTTPS 入口 (nginx :$PORT)"
  if [ "$SIM" = 1 ]; then skip "演练模式，跳过 nginx"; return 0; fi
  export DEBIAN_FRONTEND=noninteractive

  if have nginx; then
    skip "nginx 已安装"
  else
    info "安装 nginx"
    run apt-get install -y -qq nginx
    if [ "$CHECK" = 1 ]; then
      info "[check] 将安装并启动 nginx"
    else
      have nginx || die "nginx 安装失败"
      systemctl enable nginx >/dev/null 2>&1
      systemctl start nginx
    fi
  fi
  if [ "$CHECK" = 0 ]; then
    systemctl is-active --quiet nginx || { systemctl enable nginx >/dev/null 2>&1; systemctl start nginx; }
  fi

  # ---- 证书：沿用已有的，否则生成自签名 ----
  local cert="" key="" base
  for base in /etc/nginx/ssl/o.jrafina.top /etc/nginx/ssl/server /etc/nginx/ssl/tgpool; do
    if [ -f "${base}.crt" ] && [ -f "${base}.key" ]; then cert="${base}.crt"; key="${base}.key"; break; fi
  done
  if [ -n "$cert" ]; then
    skip "沿用已有证书 $cert"
  elif [ "$CHECK" = 1 ]; then
    info "[check] 未找到证书，将生成 10 年自签名证书"
  else
    local ip; ip="$(local_ip)"; [ -z "$ip" ] && ip="127.0.0.1"
    mkdir -p /etc/nginx/ssl
    cat > /tmp/tgpool-openssl.cnf <<EOF
[req]
distinguished_name = dn
x509_extensions    = v3
prompt             = no
[dn]
C  = CN
O  = tgpool
CN = ${ip}
[v3]
subjectAltName = IP:${ip}
EOF
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
      -config /tmp/tgpool-openssl.cnf \
      -keyout /etc/nginx/ssl/tgpool.key -out /etc/nginx/ssl/tgpool.crt 2>/dev/null
    rm -f /tmp/tgpool-openssl.cnf
    cert=/etc/nginx/ssl/tgpool.crt; key=/etc/nginx/ssl/tgpool.key
    chmod 600 "$key"
    ok "已生成自签名证书（CN/SAN=$ip，10 年）—— 浏览器首次访问点「继续访问」即可"
  fi

  # ---- server block ----
  if [ -f /etc/nginx/sites-available/tgpool ]; then
    skip "server block 已存在（/etc/nginx/sites-available/tgpool）"
  elif [ "$CHECK" = 1 ]; then
    info "[check] 将写入 server block（监听 $PORT）"
  else
    [ -n "$cert" ] || die "没有可用证书，无法写 nginx 配置"
    putfile /etc/nginx/sites-available/tgpool <<EOF
# TG Storage Pool —— 独立端口直连，绕过 Cloudflare 免费版 100MB 上传限制
server {
    listen ${PORT} ssl;
    listen [::]:${PORT} ssl;
    server_name _;

    ssl_certificate     ${cert};
    ssl_certificate_key ${key};
    ssl_protocols TLSv1.2 TLSv1.3;

    client_max_body_size 2000m;
    proxy_read_timeout   3600s;
    proxy_send_timeout   3600s;
    send_timeout         3600s;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host              \$host;
        proxy_set_header X-Real-IP         \$remote_addr;
        proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_http_version 1.1;
        proxy_request_buffering off;
        proxy_buffering         off;
    }
}
EOF
    ln -sf /etc/nginx/sites-available/tgpool /etc/nginx/sites-enabled/tgpool
    ok "server block 已写入并启用"
  fi

  if [ "$CHECK" = 1 ]; then info "[check] 将执行 nginx -t 并重载"; return 0; fi
  nginx -t >/dev/null 2>&1 || { nginx -t; die "nginx 配置检查失败"; }
  systemctl reload nginx 2>/dev/null || systemctl restart nginx
  ok "nginx 已重载"
}

# ============================================================================
#  10. 灾备
# ============================================================================
step_backup() {
  step 10 "索引灾备（journal + 每日备份）"
  if [ "$SIM" = 1 ]; then skip "演练模式，跳过 cron 与备份"; return 0; fi
  local py="$APP/venv/bin/python"

  if [ -s "$APP/journal/index.jsonl" ]; then
    skip "journal 已存在（$(wc -l < "$APP/journal/index.jsonl") 行）"
  elif [ "$CHECK" = 1 ]; then
    info "[check] journal 为空，将从现有 index.db 导出起点（--seed）"
  elif [ -f "$APP/index.db" ]; then
    if "$py" "$APP/tools/rebuild_index.py" --seed 2>&1 | sed 's/^/      /'; then
      ok "journal 起点已导出"
    else
      warn "journal 起点导出失败"
    fi
  else
    warn "尚无 index.db，journal 会在首次写入时自然生成"
  fi

  local CRONF=/etc/cron.d/tgpool-backup
  if [ -f "$CRONF" ] && grep -q "clean_cache.py" "$CRONF"; then
    skip "定时任务已存在且已含缓存清理（$CRONF）"
  elif [ "$CHECK" = 1 ]; then
    info "[check] 将写入 $CRONF（索引备份 每天 03:30 / 缓存瘦身 每周日 05:00）"
  else
    [ -f "$CRONF" ] && info "已有定时任务，补齐缓存清理条目"
    putfile "$CRONF" 644 <<CRON
# TG 存储池日常维护
#   每天 03:30  —— 索引备份（本地 + 一份到 Telegram），并滚动清理过期远程备份
#   每周日 05:00 —— bot API 本地文件缓存瘦身：仅在超过 ${CACHE_MAX_GB}GB 时从最旧的删起
#                  （删缓存是安全的：file_id 在索引里，下次下载会自动从 Telegram 回源）
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
30 3 * * * root $APP/venv/bin/python $APP/tools/backup_index.py >> /var/log/tgpool-backup.log 2>&1; $APP/venv/bin/python $APP/tools/backup_index.py --prune-remote >> /var/log/tgpool-backup.log 2>&1
0 5 * * 0 root $APP/venv/bin/python $APP/tools/clean_cache.py --max-gb ${CACHE_MAX_GB} >> /var/log/tgpool-cache.log 2>&1
CRON
    ok "定时任务已安装（备份 每天 03:30 / 缓存瘦身 每周日 05:00，上限 ${CACHE_MAX_GB}GB）"
  fi
  # 注意：Ubuntu 的 /var/log 是 root:syslog 且组可写，logrotate 会因「权限不安全」静默跳过，
  # 必须显式写 su root root，否则日志永远不轮转。用 su 是否存在来判断要不要重写。
  if [ "$CHECK" = 0 ] && ! grep -qs "su root root" /etc/logrotate.d/tgpool-backup 2>/dev/null; then
    putfile /etc/logrotate.d/tgpool-backup 644 <<'LR'
/var/log/tgpool-backup.log
/var/log/tgpool-cache.log {
    su root root
    weekly
    rotate 8
    compress
    missingok
    notifempty
    copytruncate
}
LR
  fi

  if [ "$CHECK" = 1 ]; then info "[check] 将立即备份一次并校验日志一致性"; return 0; fi
  info "立即执行一次备份"
  "$py" "$APP/tools/backup_index.py" 2>&1 | tail -4 | sed 's/^/      /'
  info "校验 journal 与索引是否一致"
  "$py" "$APP/tools/rebuild_index.py" --verify \
    --db "$APP/index.db" --journal "$APP/journal/index.jsonl" 2>&1 | tail -3 | sed 's/^/      /'
}

# ============================================================================
#  验收
# ============================================================================
step_verify() {
  printf '\n%s验收%s\n' "$C_B" "$C_R"
  hr
  if [ "$CHECK" = 1 ]; then
    info "体检模式，不做验收测试"
    hr
    return 0
  fi

  if [ "$SIM" = 0 ]; then
    printf '  tgpool 服务   %s\n' "$(systemctl is-active tgpool 2>/dev/null || echo unknown)"
    printf '  bot API 容器  %s\n' "$(docker ps --filter name=tg-bot-api --format '{{.Status}}' 2>/dev/null | head -1 || echo '未运行')"
    printf '  nginx         %s\n' "$(systemctl is-active nginx 2>/dev/null || echo unknown)"
  fi
  if api_ok /getMe; then printf '  getMe         OK\n'; else printf '  getMe         %sFAIL%s\n' "$C_RD" "$C_R"; fi

  local user pass py out
  user="$(env_get TG_AUTH_USER)"; pass="$(env_get TG_AUTH_PASS)"
  printf '  本地 :8080    %s\n' "$(curl -s --max-time 10 -o /dev/null -w '%{http_code}' \
    -u "${user}:${pass}" http://127.0.0.1:8080/ 2>/dev/null || echo ERR)"
  if [ "$SIM" = 0 ]; then
    printf '  HTTPS :%s   %s\n' "$PORT" "$(curl -sk --max-time 10 -o /dev/null -w '%{http_code}' \
      -u "${user}:${pass}" "https://127.0.0.1:${PORT}/" 2>/dev/null || echo ERR)"
  fi

  py="$APP/venv/bin/python"
  if [ -x "$py" ] && [ -f "$APP/index.db" ]; then
    out="$("$py" "$APP/tools/rebuild_index.py" --verify \
      --db "$APP/index.db" --journal "$APP/journal/index.jsonl" 2>&1 | tail -2)"
    case "$out" in
      *校验通过*) printf '  索引一致性    %s\n' "校验通过" ;;
      *)          printf '  索引一致性    %s\n' "见上（journal 为空或尚未备份，属正常）" ;;
    esac
  fi
  hr
}

summary() {
  local ip user pass
  ip="$(local_ip)"
  user="$(env_get TG_AUTH_USER)"; pass="$(env_get TG_AUTH_PASS)"

  printf '\n%s✓ 完成%s\n\n' "$C_B$C_G" "$C_R"
  if [ "$CHECK" = 1 ]; then
    printf '  以上是体检结果，未做任何改动。去掉 --check 即可真正执行。\n\n'; return 0
  fi
  if [ "$SIM" = 1 ]; then
    printf '  演练模式（--app-dir %s）已完成，线上服务未受影响。\n\n' "$APP"; return 0
  fi
  printf '  网页地址   %shttps://%s:%s/%s\n' "$C_B" "$ip" "$PORT" "$C_R"
  printf '  用户名     %s\n' "$user"
  printf '  密码       %s%s%s\n' "$C_B" "$pass" "$C_R"
  printf '\n  %s忘记密码怎么办%s\n' "$C_B" "$C_R"
  printf '   · Telegram 里给 bot 发 %s/pass%s，账号密码直接回给你\n' "$C_B" "$C_R"
  printf '   · 或在服务器上执行：%spython3 %s/tools/show_password.py%s\n' "$C_B" "$APP" "$C_R"
  printf '       （后端了也能用；加 --token 连 bot token 一起看，加 --reset --yes 换个新密码）\n'
  printf '\n  %s注意事项%s\n' "$C_B" "$C_R"
  printf '   · 首次打开会提示证书不受信任（自签名），点「高级 → 继续访问」即可\n'
  printf '   · 外网打不开就去云服务商安全组放行 %s 端口\n' "$PORT"
  printf '   · 别用 Cloudflare 域名上传：免费版会拦掉 >100MB 的请求\n'
  printf '   · bot token 不在备份包里，请单独另存一份（%s/tools/show_password.py --token）\n' "$APP"
  printf '   · 随时自检：%s %s/tools/rebuild_index.py --verify\n' "$APP/venv/bin/python" "$APP"
  printf '\n'
}

# ============================================================================
banner() {
  printf '\n%s════════════════════════════════════════════════════════════%s\n' "$C_C" "$C_R"
  printf '  %sTG 存储池 · 一键部署 / 灾难重建%s   %sv%s%s\n' "$C_B" "$C_R" "$C_D" "$SCRIPT_VER" "$C_R"
  printf '  %sTelegram 当备份池 · 网页上传下载 · 单文件 ≤2GB%s\n' "$C_D" "$C_R"
  printf '%s════════════════════════════════════════════════════════════%s\n' "$C_C" "$C_R"
}

main() {
  banner
  preflight
  collect
  show_plan

  if [ "$CHECK" = 1 ]; then
    printf '\n%s—— 体检模式：下面只打印将要执行的操作，不会改动系统 ——%s\n' "$C_Y" "$C_R"
  else
    confirm "确认按上面的信息开始执行？" || { printf '\n已取消，未做任何改动。\n\n'; exit 0; }
  fi

  step_swap
  step_apt
  step_docker
  step_files
  step_venv
  step_botapi
  step_restore
  step_chatid
  step_nginx
  step_backup
  step_verify
  summary
}

main
