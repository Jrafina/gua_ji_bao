#!/usr/bin/env python3
"""
backup_index.py —— 索引灾备工具

把 index.db + journal 打成一个 tar.gz：
  1. 存到服务器本地 /opt/tgpool/backup/（滚动保留 N 份）
  2. （可选）上传一份到 Telegram 存储池会话里，防整盘损坏 —— 即使服务器磁盘全毁，
     只要还有 bot token，就能把备份捞回来。

只用标准库，任何 python3 都能跑。

用法
----
  python3 backup_index.py                 # 备份（本地 + 远程）
  python3 backup_index.py --no-remote     # 只备份到本地
  python3 backup_index.py --list          # 列出本地与远程备份
  python3 backup_index.py --fetch         # 从 Telegram 拉回最新备份并解包
  python3 backup_index.py --fetch --keep-remote
"""
import argparse
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import time
import urllib.request
import uuid
from pathlib import Path

HOME = "/opt/tgpool"
DB = os.environ.get("TG_DB_PATH", HOME + "/index.db")
JOURNAL = os.environ.get("TG_JOURNAL", HOME + "/journal/index.jsonl")
BACKUP_DIR = Path(os.environ.get("TG_BACKUP_DIR", HOME + "/backup"))
ENV_FILE = Path(os.environ.get("TG_ENV_FILE", HOME + "/tgpool.env"))
REMOTE_LOG = BACKUP_DIR / "remote.jsonl"
KEEP = int(os.environ.get("TG_BACKUP_KEEP", "14"))


def load_env(path: Path) -> dict:
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------- 打包 ----------------
def make_bundle() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    snap = BACKUP_DIR / (".snapshot-%s.db" % stamp)

    # 1) 用 sqlite 在线备份 API 取一致性快照（不阻塞、不受 WAL 影响）
    if Path(DB).exists():
        src = sqlite3.connect(DB, timeout=30)
        dst = sqlite3.connect(str(snap))
        with dst:
            src.backup(dst)
        dst.close()
        src.close()
        integrity = sqlite3.connect(str(snap)).execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            snap.unlink(missing_ok=True)
            raise SystemExit("[x] 快照完整性校验失败: %s" % integrity)
    else:
        print("[!] 索引库不存在，仅备份 journal")

    # 2) 统计信息写进 MANIFEST
    counts = {"folders": 0, "files": 0, "bytes": 0}
    if snap.exists():
        c = sqlite3.connect(str(snap))
        counts["folders"] = c.execute("SELECT COUNT(*) FROM folders").fetchone()[0]
        counts["files"] = c.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        counts["bytes"] = c.execute("SELECT COALESCE(SUM(size),0) FROM files").fetchone()[0]
        c.close()

    manifest = {
        "created_at": time.time(),
        "created_at_human": time.strftime("%Y-%m-%d %H:%M:%S"),
        "host": os.uname().nodename if hasattr(os, "uname") else "",
        "counts": counts,
        "has_db": snap.exists(),
        "has_journal": Path(JOURNAL).exists(),
        "db_sha256": sha256(snap) if snap.exists() else None,
        "journal_sha256": sha256(Path(JOURNAL)) if Path(JOURNAL).exists() else None,
        "journal_lines": (sum(1 for _ in open(JOURNAL, encoding="utf-8", errors="replace"))
                          if Path(JOURNAL).exists() else 0),
    }

    bundle = BACKUP_DIR / ("tgpool-backup-%s.tar.gz" % stamp)
    with tarfile.open(bundle, "w:gz") as tar:
        if snap.exists():
            tar.add(str(snap), arcname="index.db")
        if Path(JOURNAL).exists():
            tar.add(JOURNAL, arcname="journal/index.jsonl")
        raw = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
        info = tarfile.TarInfo("MANIFEST.json")
        info.size = len(raw)
        info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(raw))

    snap.unlink(missing_ok=True)
    print("[√] 本地备份: %s (%.1f KB)  目录 %d / 文件 %d / %.2f MB"
          % (bundle, bundle.stat().st_size / 1024,
             counts["folders"], counts["files"], counts["bytes"] / 1024 / 1024))
    return bundle


def prune():
    items = sorted(BACKUP_DIR.glob("tgpool-backup-*.tar.gz"))
    for old in items[:-KEEP] if len(items) > KEEP else []:
        old.unlink(missing_ok=True)
        print("[i] 清理旧备份: %s" % old.name)


# ---------------- Telegram 上传 / 下载 ----------------
def _post_file(url: str, fields: dict, filename: str, data: bytes) -> dict:
    b = "----tgpool" + uuid.uuid4().hex
    buf = io.BytesIO()
    for k, v in fields.items():
        buf.write(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                   % (b, k, v)).encode())
    buf.write(("--%s\r\nContent-Disposition: form-data; name=\"document\"; filename=\"%s\"\r\n"
               "Content-Type: application/gzip\r\n\r\n" % (b, filename)).encode())
    buf.write(data)
    buf.write(("\r\n--%s--\r\n" % b).encode())
    req = urllib.request.Request(
        url, data=buf.getvalue(),
        headers={"Content-Type": "multipart/form-data; boundary=%s" % b})
    with urllib.request.urlopen(req, timeout=1800) as r:
        return json.load(r)


def upload_remote(bundle: Path) -> bool:
    env = load_env(ENV_FILE)
    token = env.get("TG_BOT_TOKEN") or os.environ.get("TG_BOT_TOKEN", "")
    chat = env.get("TG_CHAT_ID") or os.environ.get("TG_CHAT_ID", "")
    base = (env.get("TG_API_BASE") or "http://127.0.0.1:8081").rstrip("/")
    if not token or not chat:
        print("[!] 缺少 TG_BOT_TOKEN / TG_CHAT_ID，跳过远程备份")
        return False

    payload = bundle.read_bytes()
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
        try:
            mf = json.loads(tar.extractfile("MANIFEST.json").read().decode())
            counts = mf.get("counts", {})
        except Exception:  # noqa: BLE001
            counts = {}
    caption = "TGPOOL 索引备份 %s · 目录 %s / 文件 %s" % (
        time.strftime("%Y-%m-%d %H:%M"),
        counts.get("folders", "?"), counts.get("files", "?"))
    try:
        res = _post_file(
            "%s/bot%s/sendDocument" % (base, token),
            {"chat_id": chat, "disable_notification": "true", "caption": caption},
            bundle.name, payload)
    except Exception as e:  # noqa: BLE001
        print("[x] 远程备份失败: %s" % e)
        return False

    if not res.get("ok"):
        print("[x] 远程备份失败: %s" % res.get("description"))
        return False

    doc = res["result"].get("document") or {}
    rec = {"ts": time.time(), "at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "name": bundle.name, "size": doc.get("file_size") or len(payload),
           "file_id": doc.get("file_id"), "message_id": res["result"].get("message_id"),
           "sha256": sha256(bundle)}
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    with open(REMOTE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print("[√] 已上传 Telegram 备份: %s (file_id 已记录)" % bundle.name)
    return True


def list_all():
    print("== 本地备份 (%s) ==" % BACKUP_DIR)
    items = sorted(BACKUP_DIR.glob("tgpool-backup-*.tar.gz"))
    if not items:
        print("  (无)")
    for p in items:
        print("  %-38s %8.1f KB" % (p.name, p.stat().st_size / 1024))
    print("== 远程备份 (Telegram) ==")
    if not REMOTE_LOG.exists():
        print("  (无记录)")
        return
    for line in REMOTE_LOG.read_text(encoding="utf-8").splitlines()[-20:]:
        try:
            r = json.loads(line)
            print("  %s  %-38s %8.1f KB  msg=%s"
                  % (r.get("at"), r.get("name"), (r.get("size") or 0) / 1024,
                     r.get("message_id")))
        except Exception:  # noqa: BLE001
            pass


def fetch_remote(idx: int = -1) -> int:
    env = load_env(ENV_FILE)
    token = env.get("TG_BOT_TOKEN") or os.environ.get("TG_BOT_TOKEN", "")
    base = (env.get("TG_API_BASE") or "http://127.0.0.1:8081").rstrip("/")
    if not token:
        raise SystemExit("[x] 缺少 TG_BOT_TOKEN（可在环境变量里临时提供）")
    if not REMOTE_LOG.exists():
        raise SystemExit("[x] 没有远程备份记录 %s" % REMOTE_LOG)

    recs = [json.loads(x) for x in REMOTE_LOG.read_text(encoding="utf-8").splitlines() if x.strip()]
    if not recs:
        raise SystemExit("[x] 远程备份记录为空")
    rec = recs[idx]
    print("[i] 取回: %s (%s)" % (rec["name"], rec.get("at")))

    up = _post_file_url("%s/bot%s/getFile" % (base, token), {"file_id": rec["file_id"]})
    if not up.get("ok"):
        raise SystemExit("[x] getFile 失败: %s" % up.get("description"))
    fp = up["result"]["file_path"]

    out = BACKUP_DIR / ("fetched-" + rec["name"])
    # bot API server 的 --local 模式下, file_path 是本机绝对路径, 直接读磁盘;
    # 非 local 模式才是相对路径, 走 HTTP 下载。
    cand = Path(fp)
    if not cand.is_absolute():
        cand = Path(os.environ.get("TG_LOCAL_ROOT", "/var/lib/telegram-bot-api")) / fp
    if cand.is_file():
        print("[i] --local 模式：从本机 %s 复制" % cand)
        shutil.copyfile(cand, out)
    else:
        url = "%s/file/bot%s/%s" % (base, token, fp)
        with urllib.request.urlopen(url, timeout=1800) as r, open(out, "wb") as f:
            shutil.copyfileobj(r, f)

    got = sha256(out)
    if rec.get("sha256") and got != rec["sha256"]:
        raise SystemExit("[x] sha256 不匹配，文件可能损坏\n  期望 %s\n  实际 %s"
                         % (rec["sha256"], got))
    print("[√] 下载完成并校验通过: %s" % out)
    return 0


def _post_file_url(url: str, fields: dict) -> dict:
    data = "&".join("%s=%s" % (k, v) for k, v in fields.items()).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)


# ---------------- 入口 ----------------
def prune_remote():
    """远程备份滚动清理：保留最新 KEEP 份，其余从 Telegram 删除"""
    if not REMOTE_LOG.exists():
        print("[i] 无远程备份记录")
        return
    env = load_env(ENV_FILE)
    token = env.get("TG_BOT_TOKEN") or os.environ.get("TG_BOT_TOKEN", "")
    chat = env.get("TG_CHAT_ID") or os.environ.get("TG_CHAT_ID", "")
    base = (env.get("TG_API_BASE") or "http://127.0.0.1:8081").rstrip("/")
    recs = [json.loads(x) for x in REMOTE_LOG.read_text(encoding="utf-8").splitlines() if x.strip()]
    if len(recs) <= KEEP:
        print("[i] 远程备份 %d 份，未超过保留数 %d，无需清理" % (len(recs), KEEP))
        return
    keep, drop = recs[-KEEP:], recs[:-KEEP]
    for r in drop:
        if r.get("message_id"):
            try:
                _post_file_url("%s/bot%s/deleteMessage" % (base, token),
                               {"chat_id": chat, "message_id": r["message_id"]})
                print("[i] 已删除远程备份: %s" % r.get("name"))
            except Exception as e:  # noqa: BLE001
                print("[!] 删除失败 %s: %s" % (r.get("name"), e))
    with open(REMOTE_LOG, "w", encoding="utf-8") as f:
        for r in keep:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("[√] 远程保留最新 %d 份" % len(keep))


def main():
    ap = argparse.ArgumentParser(description="TG 存储池索引灾备")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--fetch", action="store_true", help="从 Telegram 拉回最新备份")
    ap.add_argument("--index", type=int, default=-1, help="取第几条远程备份（-1 最新）")
    ap.add_argument("--no-remote", action="store_true", help="只备份到本地")
    ap.add_argument("--no-local", action="store_true", help="只上传远程")
    ap.add_argument("--prune-remote", action="store_true", help="清理过期的远程备份")
    args = ap.parse_args()

    if args.list:
        list_all()
        return 0
    if args.prune_remote:
        prune_remote()
        return 0
    if args.fetch:
        return fetch_remote(args.index)

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if not args.no_local:
        bundle = make_bundle()
        prune()
    else:
        items = sorted(BACKUP_DIR.glob("tgpool-backup-*.tar.gz"))
        if not items:
            raise SystemExit("[x] 本地没有备份可上传")
        bundle = items[-1]

    if not args.no_remote:
        upload_remote(bundle)
    return 0


if __name__ == "__main__":
    sys.exit(main())
