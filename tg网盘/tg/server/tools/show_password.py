#!/usr/bin/env python3
"""show_password.py —— 找回（或重设）网页登录密码

部署完过几天，密码忘了是常态。密码就存在 tgpool.env 里（chmod 600，只有 root 读得到，
也不在备份包里），本脚本把它读出来打印。不联网、不依赖任何第三方库，
系统自带的 python3 就能跑，服务挂了也能用。

用法
----
  sudo python3 /opt/tgpool/tools/show_password.py            # 账号 / 密码 / 访问地址
  sudo python3 /opt/tgpool/tools/show_password.py --token    # 连 bot token 一起看（灾备要用）
  sudo python3 /opt/tgpool/tools/show_password.py --json     # 机器可读
  sudo python3 /opt/tgpool/tools/show_password.py --reset --yes
                                                             # 换成新的 16 位随机密码
  python3 show_password.py --env-file /root/tgpool.env       # 换一份环境文件

在 Telegram 里直接发 /pass 也能拿到账号密码（等价于本脚本的默认输出）。
"""
import argparse
import json
import os
import re
import socket
import sys
import time
from pathlib import Path

# 依次尝试：--env-file → $TG_ENV_FILE → 常见安装位置
CANDIDATES = (
    "/opt/tgpool/tgpool.env",
    "/etc/tgpool.env",
    "/root/tgpool.env",
    "tgpool.env",
)
NEEDED = ("TG_AUTH_USER", "TG_AUTH_PASS", "TG_BOT_TOKEN", "TG_CHAT_ID", "TG_NGINX_PORT")


def die(msg: str, code: int = 2):
    print("[x] %s" % msg)
    print("    提示：部署脚本会生成 /opt/tgpool/tgpool.env，可用下面的命令确认它在哪：")
    print("      systemctl cat tgpool | grep EnvironmentFile")
    print("      find / -maxdepth 4 -name 'tgpool.env' 2>/dev/null")
    sys.exit(code)


def find_env(explicit=None) -> Path:
    cands = []
    if explicit:
        cands.append(explicit)
    if os.environ.get("TG_ENV_FILE"):
        cands.append(os.environ["TG_ENV_FILE"])
    cands.extend(CANDIDATES)
    cands.append(str(Path(__file__).resolve().parent.parent / "tgpool.env"))
    for c in cands:
        p = Path(c)
        if p.is_file():
            return p
    die("找不到 tgpool.env（试过：%s）" % "、".join(cands))
    return Path()  # 到不了


def read_env(path: Path):
    """返回 (dict, 原始行列表)。解析 KEY=VALUE，容忍 export 前缀和引号。"""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except PermissionError:
        die("读不了 %s —— 请用 root 运行（sudo python3 %s）" % (path, __file__))
    except OSError as e:
        die("读不了 %s：%s" % (path, e))
    data, lines = {}, raw.splitlines()
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip()
        if k.startswith("export "):
            k = k[7:].strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        data[k] = v
    return data, lines


def local_ip() -> str:
    """本机对外 IP（UDP connect 不发包）；失败退回 127.0.0.1。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:  # noqa: BLE001
        return "127.0.0.1"
    finally:
        s.close()


def public_ip() -> str:
    """有就顺手带上公网 IP（超时 2 秒，失败就算了）。"""
    import urllib.request
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=2) as r:
            return r.read().decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001
        return ""


def gen_pass(n: int = 16) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return "".join(alphabet[b % len(alphabet)] for b in os.urandom(n))


def do_reset(path: Path, env: dict, lines: list, yes: bool) -> int:
    new = gen_pass()
    print("准备把 %s 里的 TG_AUTH_PASS 换成新的 16 位随机密码。" % path)
    if not yes:
        print("确认要改就加 --yes 再跑一次（改完需要 systemctl restart tgpool 生效）。")
        return 0
    bak = "%s.bak-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
    try:
        Path(bak).write_bytes(path.read_bytes())
        os.chmod(bak, 0o600)
        found = False
        out = []
        for ln in lines:
            if re.match(r"^\s*(export\s+)?TG_AUTH_PASS\s*=", ln):
                out.append("TG_AUTH_PASS=%s" % new)
                found = True
            else:
                out.append(ln)
        if not found:
            out.append("TG_AUTH_PASS=%s" % new)
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError as e:
        die("写入失败：%s（原文件已备份到 %s）" % (e, bak), 1)
    print("[√] 新密码已写入（原文件备份：%s）" % bak)
    print()
    print("  账号   %s" % env.get("TG_AUTH_USER", "admin"))
    print("  新密码 %s" % new)
    print()
    print("让它生效：systemctl restart tgpool")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="查看 / 重置 TG 存储池的网页登录密码")
    ap.add_argument("--env-file", help="tgpool.env 路径（默认自动找）")
    ap.add_argument("--port", help="对外 HTTPS 端口（默认取 TG_NGINX_PORT，再不行 8443）")
    ap.add_argument("--token", action="store_true", help="把 bot token 也打印出来")
    ap.add_argument("--reset", action="store_true", help="生成新的随机密码并写入")
    ap.add_argument("--yes", action="store_true", help="配合 --reset，跳过二次确认")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    path = find_env(args.env_file)
    env, lines = read_env(path)

    if args.reset:
        return do_reset(path, env, lines, args.yes)

    port = args.port or env.get("TG_NGINX_PORT") or "8443"
    ip = local_ip()
    user = env.get("TG_AUTH_USER") or "admin"
    pwd = env.get("TG_AUTH_PASS") or ""
    token = env.get("TG_BOT_TOKEN") or ""

    if args.json:
        print(json.dumps({
            "env_file": str(path), "url": "https://%s:%s/" % (ip, port),
            "local_url": "http://127.0.0.1:8080/",
            "user": user, "password": pwd,
            "bot_token": token if args.token else "",
            "chat_id": env.get("TG_CHAT_ID", ""),
        }, ensure_ascii=False, indent=2))
        return 0

    pub = public_ip()
    print("TG 存储池 · 网页登录信息")
    print("┌ 取自 %s" % path)
    print("│ 外网   https://%s:%s/" % (ip, port))
    if pub and pub != ip:
        print("│        https://%s:%s/   （云主机公网 IP）" % (pub, port))
    print("│ 本机   http://127.0.0.1:8080/")
    print("│ 账号   %s" % user)
    if pwd:
        print("│ 密码   %s" % pwd)
    else:
        print("│ 密码   （空）")
        print("│        ! TG_AUTH_PASS 是空的，网页现在任意密码都能进，建议 --reset 设一个")
    print("│ 自助验证：curl -s -o /dev/null -w '%%{http_code}\\n' -u %s:%s http://127.0.0.1:8080/"
          % (user, pwd or "x"))
    print("└")
    if args.token:
        print()
        print("bot token（勿外传；不在备份包里，灾备重建要用）")
        print("  %s" % (token or "（没配 TG_BOT_TOKEN）"))
        print("  会话 chat_id  %s" % (env.get("TG_CHAT_ID") or "（未探测）"))
    else:
        print()
        print("还想看 bot token（灾备重建要用）：加 --token")
    print()
    print("在 Telegram 里发 /pass 也能随时查（聊天是白名单私聊，不会外泄）。")
    print("忘记密码又找不回：sudo python3 %s --reset --yes" % __file__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
