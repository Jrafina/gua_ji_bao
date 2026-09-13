#!/usr/bin/env python3
"""
rebuild_index.py —— 从变更日志(journal)完整重建 index.db

背景
----
Telegram Bot API **无法读取历史消息**（没有列举历史/按 id 取消息的接口，bot 自己也
收不到自己发出的消息），所以索引一旦丢失，无法从 Telegram 反向构建。本工具的思路是：
应用每次改动都往 journal 追加一行，索引因此**永远可重放**。

journal 里的目录用「路径字符串」记录而非自增 id，所以重建不依赖任何 id 映射。

用法
----
  # 预演：只统计，不写文件
  python3 rebuild_index.py --stats

  # 校验：把 journal 重放结果与当前 index.db 对比（不写文件）
  python3 rebuild_index.py --verify

  # 重建：生成全新的 index.db（原库自动另存为 index.db.bak-<时间>）
  python3 rebuild_index.py

  # 输出到别的路径，不动现有库
  python3 rebuild_index.py --out /tmp/rebuilt.db

  # journal 也丢了，只剩备份：直接恢复
  python3 rebuild_index.py --restore /opt/tgpool/backup/index-20260911-033000.db
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

DEFAULT_DB = os.environ.get("TG_DB_PATH", "/opt/tgpool/index.db")
DEFAULT_JOURNAL = os.environ.get("TG_JOURNAL", "/opt/tgpool/journal/index.jsonl")

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    mime        TEXT,
    file_id     TEXT NOT NULL,
    file_unique TEXT,
    message_id  INTEGER,
    chat_id     TEXT,
    folder_id   INTEGER,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS folders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    parent_id   INTEGER,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_folder ON files(folder_id);
CREATE INDEX IF NOT EXISTS idx_folders_parent ON folders(parent_id);
"""


# ---------------- 路径工具 ----------------
def norm(p) -> str:
    """归一化成以 / 开头的绝对路径，根为 '/'"""
    if not p or p == "/":
        return "/"
    return "/" + str(p).strip("/")


def parent_of(p: str):
    if p == "/":
        return None
    q = p.rsplit("/", 1)[0]
    return q if q else "/"


def base_of(p: str) -> str:
    return p.rsplit("/", 1)[-1]


def under(path: str, root: str) -> bool:
    """path 是否等于 root 或位于 root 之下"""
    if root == "/":
        return True
    return path == root or path.startswith(root + "/")


def chain(path: str) -> list:
    """返回该路径上所有祖先目录（不含根），从浅到深"""
    path = norm(path)
    if path == "/":
        return []
    parts = [x for x in path.split("/") if x]
    return ["/" + "/".join(parts[: i + 1]) for i in range(len(parts))]


# ---------------- 重放 ----------------
def replay(journal_path: Path, verbose: bool = False):
    """按顺序重放 journal，返回 (folders, files, 统计)"""
    folders = {}   # path -> created_at
    files = {}     # id -> record
    stat = {"lines": 0, "applied": 0, "bad": 0, "unknown": 0}
    kinds = {}

    if not journal_path.exists():
        return folders, files, stat

    with open(journal_path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw or raw.startswith("#"):   # 空行与注释头
                continue
            stat["lines"] += 1
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                # 只容忍最后一行被截断（进程被杀）；中间坏行也跳过但计数
                stat["bad"] += 1
                continue

            t = ev.get("t")
            kinds[t] = kinds.get(t, 0) + 1
            ts = ev.get("ts") or 0

            if t == "mkdir":
                for p in chain(ev.get("path", "/")):
                    folders.setdefault(p, ts)
                stat["applied"] += 1

            elif t == "mvd":            # 目录改名 / 移动
                old, new = norm(ev.get("path")), norm(ev.get("new"))
                if old != "/" and new != "/" and old != new:
                    for p in [p for p in folders if under(p, old)]:
                        n = new + p[len(old):]
                        if p not in folders:
                            continue
                        folders[n] = folders.pop(p)
                    for r in files.values():
                        if under(r["_path"], old):
                            r["_path"] = new + r["_path"][len(old):]
                    for p in chain(new):
                        folders.setdefault(p, ts)
                stat["applied"] += 1

            elif t == "rmd":            # 目录删除（递归）
                old = norm(ev.get("path"))
                if old == "/":          # 根目录不可删，忽略异常事件
                    stat["unknown"] += 1
                    continue
                dead = [p for p in folders if under(p, old)]
                for p in dead:
                    folders.pop(p, None)
                gone = [k for k, r in files.items() if under(r["_path"], old)]
                for k in gone:
                    files.pop(k, None)
                stat["applied"] += 1

            elif t == "add":
                fid = ev.get("id")
                if fid is None:
                    stat["bad"] += 1
                    continue
                path = norm(ev.get("path", "/"))
                for p in chain(path):
                    folders.setdefault(p, ts)
                files[fid] = {
                    "name": ev.get("name") or "unnamed",
                    "size": int(ev.get("size") or 0),
                    "mime": ev.get("mime"),
                    "tg_file_id": ev.get("tg_file_id") or "",
                    "tg_unique": ev.get("tg_unique"),
                    "message_id": ev.get("message_id"),
                    "chat_id": ev.get("chat_id"),
                    "created_at": ev.get("created_at") or ts,
                    "_path": path,
                }
                stat["applied"] += 1

            elif t == "mv":             # 文件移动
                fid = ev.get("id")
                path = norm(ev.get("path", "/"))
                if fid in files:
                    for p in chain(path):
                        folders.setdefault(p, ts)
                    files[fid]["_path"] = path
                stat["applied"] += 1

            elif t == "del":            # 文件删除
                files.pop(ev.get("id"), None)
                stat["applied"] += 1

            else:
                stat["unknown"] += 1
                if verbose:
                    print("  跳过未知事件:", t)

    stat["kinds"] = kinds
    return folders, files, stat


def materialize(folders, files, out_db: Path):
    """把重放结果写成全新的 index.db"""
    tmp = Path(str(out_db) + ".tmp")
    if tmp.exists():
        tmp.unlink()
    conn = sqlite3.connect(tmp)
    conn.executescript(SCHEMA)

    idmap = {"/": None}
    # 按深度排序，保证父目录先插入
    for p in sorted(folders.keys(), key=lambda x: (x.count("/"), x)):
        if p == "/":
            continue
        cur = conn.execute(
            "INSERT INTO folders(name,parent_id,created_at) VALUES(?,?,?)",
            (base_of(p), idmap.get(parent_of(p)), folders[p]),
        )
        idmap[p] = cur.lastrowid

    for fid, r in sorted(files.items()):
        conn.execute(
            "INSERT INTO files(id,name,size,mime,file_id,file_unique,message_id,"
            "chat_id,folder_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (fid, r["name"], r["size"], r["mime"], r["tg_file_id"], r["tg_unique"],
             r["message_id"], r["chat_id"], idmap.get(r["_path"], None),
             r["created_at"]),
        )
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    tmp.replace(out_db)


# ---------------- 读取现有库，用于校验 ----------------
def read_db(path: Path):
    if not path.exists():
        return None, None
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id,name,parent_id,created_at FROM folders").fetchall()
    by_id = {r["id"]: r for r in rows}

    def path_of(fid):
        parts, cur = [], fid
        seen = set()
        while cur is not None and cur in by_id and cur not in seen:
            seen.add(cur)
            parts.append(by_id[cur]["name"])
            cur = by_id[cur]["parent_id"]
        return "/" + "/".join(reversed(parts)) if parts else "/"

    folders = {path_of(r["id"]): r["created_at"] for r in rows}
    files = {}
    for r in conn.execute("SELECT * FROM files").fetchall():
        d = dict(r)
        d["_path"] = path_of(d["folder_id"])
        files[d["id"]] = {k: d[k] for k in
                          ("name", "size", "mime", "file_id", "created_at")} | {"_path": d["_path"]}
    conn.close()
    return folders, files


def summarize(folders, files, stat):
    total = sum(r["size"] for r in files.values())
    print("  journal 行数    : %d（可解析 %d，损坏 %d，未知事件 %d）"
          % (stat["lines"], stat["applied"], stat["bad"], stat["unknown"]))
    print("  重放后目录数    : %d" % len(folders))
    print("  重放后文件数    : %d" % len(files))
    print("  重放后总容量    : %.2f MB" % (total / 1024 / 1024))
    if stat.get("kinds"):
        print("  事件类型分布    :",
              ", ".join("%s=%d" % (k, v) for k, v in sorted(stat["kinds"].items())))


def seed_from_db(db_path: Path, journal_path: Path, force: bool) -> int:
    """把现有 index.db 导出成 journal 事件，作为日志的起点。

    只在首次启用 journal 时需要跑一次（否则历史文件不在日志里，重建会丢）。
    """
    if not db_path.exists():
        print("[x] 索引库不存在: %s" % db_path)
        return 2
    if journal_path.exists() and journal_path.stat().st_size > 0 and not force:
        print("[!] journal 已有内容，拒绝覆盖: %s" % journal_path)
        print("    确认要重建日志请加 --force")
        return 2

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id,name,parent_id,created_at FROM folders").fetchall()
    by_id = {r["id"]: r for r in rows}

    def path_of(fid):
        parts, cur, seen = [], fid, set()
        while cur is not None and cur in by_id and cur not in seen:
            seen.add(cur)
            parts.append(by_id[cur]["name"])
            cur = by_id[cur]["parent_id"]
        return "/" + "/".join(reversed(parts)) if parts else "/"

    events = []
    # 先目录，浅的在前
    for r in sorted(rows, key=lambda x: path_of(x["id"]).count("/")):
        events.append({"t": "mkdir", "path": path_of(r["id"]), "ts": r["created_at"]})
    # 再文件，按 id 升序
    for r in conn.execute("SELECT * FROM files ORDER BY id").fetchall():
        events.append({
            "t": "add", "id": r["id"], "name": r["name"], "size": r["size"],
            "mime": r["mime"], "tg_file_id": r["file_id"], "tg_unique": r["file_unique"],
            "message_id": r["message_id"], "chat_id": r["chat_id"],
            "path": path_of(r["folder_id"]), "created_at": r["created_at"],
            "ts": r["created_at"],
        })
    conn.close()

    journal_path.parent.mkdir(parents=True, exist_ok=True)
    with open(journal_path, "w", encoding="utf-8") as f:
        f.write("# TGPOOL journal 起点：由 index.db 导出 %s\n"
                % time.strftime("%Y-%m-%d %H:%M:%S"))
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print("[√] 已导出 journal: %s（%d 条事件）" % (journal_path, len(events)))
    return 0


def main():
    ap = argparse.ArgumentParser(description="从 journal 重建 TG 存储池索引")
    ap.add_argument("--journal", default=DEFAULT_JOURNAL)
    ap.add_argument("--db", default=DEFAULT_DB, help="目标索引库（默认会被备份后覆盖）")
    ap.add_argument("--out", help="只输出到该路径，不碰现有库")
    ap.add_argument("--verify", action="store_true", help="与现有库对比，不写文件")
    ap.add_argument("--stats", action="store_true", help="只打印统计")
    ap.add_argument("--restore", metavar="BACKUP_DB", help="直接用备份库恢复")
    ap.add_argument("--seed", action="store_true", help="把现有 index.db 导出为 journal 起点")
    ap.add_argument("--force", action="store_true", help="配合 --seed，覆盖已有 journal")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    db_path = Path(args.db)
    jpath = Path(args.journal)

    if args.seed:
        return seed_from_db(db_path, jpath, args.force)

    # ---- 直接恢复备份 ----
    if args.restore:
        src = Path(args.restore)
        if not src.exists():
            print("[x] 备份文件不存在: %s" % src)
            return 1
        if db_path.exists():
            bak = str(db_path) + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
            shutil.copy2(db_path, bak)
            print("[i] 现有库已另存: %s" % bak)
        shutil.copy2(src, db_path)
        conn = sqlite3.connect(db_path)
        n = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        m = conn.execute("SELECT COUNT(*) FROM folders").fetchone()[0]
        conn.close()
        print("[√] 已从备份恢复: %s（%d 个文件 / %d 个目录）" % (src, n, m))
        return 0

    print("[i] journal : %s" % jpath)
    if not jpath.exists():
        print("[x] journal 不存在 —— 无法重放。")
        print("    如果只是索引坏了，可用备份恢复：")
        print("      python3 %s --restore /opt/tgpool/backup/index-<时间戳>.db" % sys.argv[0])
        return 2

    folders, files, stat = replay(jpath, args.verbose)

    if args.stats:
        print("[i] 仅统计：")
        summarize(folders, files, stat)
        return 0

    if args.verify:
        cur_f, cur_x = read_db(db_path)
        if cur_f is None:
            print("[x] 目标库不存在，无法校验")
            return 2
        print("[i] 重放结果：")
        summarize(folders, files, stat)
        print("[i] 现有库    : %d 个目录 / %d 个文件"
              % (len(cur_f), len(cur_x)))

        ok = True
        only_j = set(folders) - set(cur_f)
        only_d = set(cur_f) - set(folders)
        if only_j:
            ok = False
            print("[!] 仅存在于 journal 的目录 (%d): %s" % (len(only_j), sorted(only_j)[:10]))
        if only_d:
            ok = False
            print("[!] 仅存在于现库的目录 (%d): %s" % (len(only_d), sorted(only_d)[:10]))

        ids = set(files) | set(cur_x)
        diff = 0
        for i in sorted(ids):
            a, b = files.get(i), cur_x.get(i)
            if a is None or b is None:
                diff += 1
                if diff <= 6:
                    print("[!] 文件 %s 只在%s" % (i, "journal" if b is None else "现库"))
                continue
            if (a["name"], a["size"], a["_path"]) != (b["name"], b["size"], b["_path"]):
                diff += 1
                if diff <= 6:
                    print("[!] 文件 %s 不一致: journal=%s 现库=%s"
                          % (i, (a["name"], a["_path"]), (b["name"], b["_path"])))
        if diff:
            ok = False
            print("[!] 文件差异共 %d 个" % diff)
        print("[√] 校验通过，索引与 journal 完全一致" if ok else "[x] 校验不一致（见上）")
        return 0 if ok else 3

    # ---- 正式重建 ----
    out = Path(args.out) if args.out else db_path
    if out.exists() and not args.out:
        bak = str(out) + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(out, bak)
        print("[i] 现有库已另存: %s" % bak)
    materialize(folders, files, out)
    print("[i] 重放结果：")
    summarize(folders, files, stat)
    print("[√] 已重建: %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
