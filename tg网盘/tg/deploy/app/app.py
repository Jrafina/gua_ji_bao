#!/usr/bin/env python3
"""
TG Storage Pool - 基于 Telegram Bot API (自建 local server) 的备份存储池
- 上传: 文件 -> 本地 bot API server (--local) -> Telegram 频道
- 下载: 从 Telegram / 本地缓存流式下载, 支持 HTTP Range (断点续传/进度条)
- 索引: SQLite 本地索引 (file_id 永久有效, 本地缓存可清理)
- 目录: 虚拟文件夹, 结构存于本地 SQLite; Telegram 侧仍为扁平存储, 不产生额外上传
"""
import os
import re
import sys
import json
import asyncio
import sqlite3
import time
import logging
import secrets
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import httpx
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Depends, Body
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

# ---------------- 配置 ----------------
BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
API_BASE = os.environ.get("TG_API_BASE", "http://127.0.0.1:8081").rstrip("/")
CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
TMP_DIR = Path(os.environ.get("TG_TMP_DIR", "/opt/tgpool/tmp"))
DB_PATH = Path(os.environ.get("TG_DB_PATH", "/opt/tgpool/index.db"))
AUTH_USER = os.environ.get("TG_AUTH_USER", "admin")
AUTH_PASS = os.environ.get("TG_AUTH_PASS", "")
MAX_UPLOAD = int(os.environ.get("TG_MAX_UPLOAD", 2000 * 1024 * 1024))  # 2GB
# bot API server --local 模式下, getFile 返回的是服务器本地绝对路径
LOCAL_FILE_ROOT = os.environ.get("TG_LOCAL_ROOT", "/var/lib/telegram-bot-api")
# 追加式变更日志: index.db 丢失/损坏时, 用它完整重建索引 (见 tools/rebuild_index.py)
JOURNAL = Path(os.environ.get("TG_JOURNAL", "/opt/tgpool/journal/index.jsonl"))
# 上传时在 Telegram 侧附上文件所在目录的 caption, 让云端消息自带目录信息
CAPTION_PATH = os.environ.get("TG_CAPTION_PATH", "1") not in ("0", "false", "no", "")
# 手动索引备份按钮调用它（与 app/ 同级目录下的 tools/backup_index.py）
BACKUP_SCRIPT = Path(os.environ.get(
    "TG_BACKUP_SCRIPT",
    str(Path(__file__).resolve().parent.parent / "tools" / "backup_index.py"),
))
# 缓存清理工具（网页上的「清理缓存」按钮调用它）
CACHE_SCRIPT = Path(os.environ.get(
    "TG_CACHE_SCRIPT",
    str(Path(__file__).resolve().parent.parent / "tools" / "clean_cache.py"),
))
# 找回密码工具（/pass 提示用户在服务器上怎么查）
PASS_SCRIPT = Path(os.environ.get(
    "TG_PASS_SCRIPT",
    str(Path(__file__).resolve().parent.parent / "tools" / "show_password.py"),
))
# nginx 对外 HTTPS 端口（部署脚本写进 tgpool.env），/pass 用来拼出网页地址
NGINX_PORT = os.environ.get("TG_NGINX_PORT", "8443").strip() or "8443"

if not BOT_TOKEN:
    raise SystemExit("缺少环境变量 TG_BOT_TOKEN")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tgpool")

TMP_DIR.mkdir(parents=True, exist_ok=True)
API = f"{API_BASE}/bot{BOT_TOKEN}"
FILE_API = f"{API_BASE}/file/bot{BOT_TOKEN}"

client = httpx.AsyncClient(timeout=httpx.Timeout(3600.0, connect=30.0))

# ---------------- 下载预热：点下载后先把整份文件拉到服务器，期间显示真实进度 ----------------
# bot API (--local) 的 getFile 必须先让整份文件落到服务器本地，才会返回文件路径；
# 在此之前浏览器收不到任何字节，表现就是"转圈很久"。所以这里把 getFile 放到后台跑，
# 同时轮询 bot API 的 temp/ 目录（下载中的文件先落这里，完成后才改名进 documents/），
# 用真实字节数换算出百分比、速度和剩余时间。
BOT_TEMP_DIR = Path(LOCAL_FILE_ROOT) / BOT_TOKEN / "temp"
PREPARE = {}                 # fid -> {state, bytes, total, started, path, error, task, temp}
PREPARE_TTL = 6 * 3600       # ready / failed 的记录保留 6 小时后清理


# ---------------- 数据库 ----------------
def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with db() as conn:
        conn.execute(
            """
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
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS folders (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                parent_id   INTEGER,
                created_at  REAL NOT NULL
            )
            """
        )
        # 兼容老库: 补充 folder_id 列
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(files)").fetchall()}
        if "folder_id" not in cols:
            conn.execute("ALTER TABLE files ADD COLUMN folder_id INTEGER")
            log.info("db migrated: added files.folder_id")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_folder ON files(folder_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_folders_parent ON folders(parent_id)")
        conn.commit()
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    if JOURNAL.exists():
        log.info("journal: %s (%d 行)", JOURNAL, sum(1 for _ in JOURNAL.open(encoding="utf-8")))
    else:
        log.info("journal: %s (新建)", JOURNAL)


def api_ok(data: dict):
    if not data.get("ok"):
        raise HTTPException(502, f"Telegram API error: {data.get('description', data)}")
    return data["result"]


# ---------------- 目录辅助 ----------------
def clean_folder_name(name: str) -> str:
    name = (name or "").strip()
    name = re.sub(r"[/\\]", "_", name)
    name = name.strip(". ") or "未命名文件夹"
    return name[:120]


def folder_exists(conn, fid) -> bool:
    if fid is None:
        return True
    return conn.execute("SELECT 1 FROM folders WHERE id=?", (fid,)).fetchone() is not None


def folder_path(conn, fid) -> list:
    """从根到目标文件夹的路径 [{id,name}, ...]"""
    if fid is None:
        return []
    rows = conn.execute(
        """
        WITH RECURSIVE up(id, name, parent_id) AS (
            SELECT id, name, parent_id FROM folders WHERE id = ?
            UNION ALL
            SELECT f.id, f.name, f.parent_id FROM folders f JOIN up ON f.id = up.parent_id
        )
        SELECT id, name FROM up
        """,
        (fid,),
    ).fetchall()
    return [{"id": r["id"], "name": r["name"]} for r in reversed(rows)]


def folder_scope(conn, fid) -> list:
    """自身 + 全部后代文件夹 id"""
    if fid is None:
        return []
    rows = conn.execute(
        """
        WITH RECURSIVE d(id) AS (
            SELECT id FROM folders WHERE id = ?
            UNION ALL
            SELECT f.id FROM folders f JOIN d ON f.parent_id = d.id
        )
        SELECT id FROM d
        """,
        (fid,),
    ).fetchall()
    return [r["id"] for r in rows]


async def _tg_delete_messages(chat_id, mids) -> int:
    """从 Telegram 侧删除一批消息，返回确认删掉的条数。

    优先用 deleteMessages（一次最多 100 条）——池子里一个目录动辄上千个文件，
    逐条 deleteMessage 既慢又容易撞限流；批量整体失败（老 API / 权限）才退回逐条。
    """
    ids = [int(m) for m in mids if m]
    done = 0
    for i in range(0, len(ids), 100):
        batch = ids[i:i + 100]
        r = await _tg("deleteMessages", chat_id=chat_id, message_ids=batch)
        if r.get("ok"):
            done += len(batch)
            continue
        log.warning("deleteMessages 批量失败（%d 条）：%s，改为逐条删除",
                    len(batch), r.get("description"))
        for mid in batch:
            rr = await _tg("deleteMessage", chat_id=chat_id, message_id=mid)
            if rr.get("ok"):
                done += 1
            else:
                log.warning("deleteMessage 失败 message_id=%s: %s", mid, rr.get("description"))
    return done


async def delete_messages(rows) -> None:
    """从 Telegram 侧删除消息（失败只记日志，不阻断）。

    按每条记录自带的 chat_id 删除（从别的会话发进池子的文件，原消息不在存储池会话里）；
    记录里没有 chat_id 列时退回全局 CHAT_ID。
    """
    by_chat = {}
    for r in rows:
        mid = r["message_id"]
        if not mid:
            continue
        cid = (r["chat_id"] if "chat_id" in r.keys() else None) or CHAT_ID
        by_chat.setdefault(str(cid), []).append(mid)
    for cid, mids in by_chat.items():
        n = await _tg_delete_messages(cid, mids)
        log.info("delete: chat=%s 请求 %d 条，Telegram 确认 %d 条", cid, len(mids), n)


# ---------------- 变更日志（灾备核心） ----------------
def jlog(evt: dict) -> None:
    """把一次变更追加写入 journal。失败只记日志，绝不影响主流程。

    目录用「路径」而非 id 记录，重建时不依赖任何自增 id，可完整重放。
    """
    try:
        JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        evt["ts"] = round(time.time(), 3)
        line = json.dumps(evt, ensure_ascii=False)
        with open(JOURNAL, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:  # noqa: BLE001
        log.warning("journal write failed: %s", e)


def path_of(conn, fid) -> str:
    """文件夹 id -> 绝对路径字符串，根目录为 '/'"""
    parts = folder_path(conn, fid)
    return "/" + "/".join(p["name"] for p in parts) if parts else "/"


def folder_path_maps(conn):
    """一次性解析整棵目录树，返回两个映射（搜索专用，避免逐文件递归查路径）：

    str_map  {folder_id: "/工作/2026"}        —— 包含匹配用
    list_map {folder_id: [{"id","name"},...]} —— 与 /api/list 的 path 字段同形
    根目录文件（folder_id IS NULL）对应 ("/", [])。
    """
    rows = conn.execute("SELECT id,name,parent_id FROM folders").fetchall()
    parents = {r["id"]: r["parent_id"] for r in rows}
    names = {r["id"]: r["name"] for r in rows}
    memo: dict = {}

    def resolve(fid):
        if fid is None:
            return "/", []
        if fid in memo:
            return memo[fid]
        seg = names.get(fid)
        if seg is None:  # 脏数据兜底：孤儿文件夹按根处理
            return "/", []
        up_s, up_l = resolve(parents.get(fid))
        val = ((up_s.rstrip("/") + "/" + seg) if up_s != "/" else "/" + seg,
               up_l + [{"id": fid, "name": seg}])
        memo[fid] = val
        return val

    for fid in parents:
        resolve(fid)
    str_map = {fid: memo[fid][0] for fid in memo}
    list_map = {fid: memo[fid][1] for fid in memo}
    return str_map, list_map


# ---------------- 搜索（网页与 bot 共用同一套规则） ----------------
def search_files(conn, query: str, limit: int = 500):
    """包含式（模糊）搜索：文件名或所在路径包含全部关键词的文件都会命中。

    - 多关键词以空格分隔，要求**全部命中**，但可分别落在文件名/路径上；
    - 纯 Python 字面量比较，`%`、`_` 不再被当成 SQL LIKE 通配符（搜 2026_09 只命中字面量）；
    - 大小写不敏感（lower() 归一），中文不受影响；
    - 目录名命中自然覆盖：目录在路径里，其下文件全部命中（无需单独查 folders 表）；
    - 排序：文件名全等 > 名称前缀 > 名称包含全部词 > 仅路径包含，同级新的靠前。
    返回 (items, total)：items 最多 limit 条，total 是命中总数（供前端提示截断）。
    """
    toks = [t.lower() for t in (query or "").split() if t]
    if not toks:
        return [], 0
    q = (query or "").strip().lower()
    str_map, list_map = folder_path_maps(conn)
    items, total = [], 0
    for r in conn.execute(
        "SELECT id,name,size,mime,folder_id,created_at FROM files"
    ).fetchall():
        name_l = (r["name"] or "").lower()
        fid = r["folder_id"]
        p_str = str_map.get(fid, "/")
        p_list = list_map.get(fid, [])
        full_l = p_str.rstrip("/") + "/" + name_l
        if not all(t in full_l for t in toks):
            continue
        total += 1
        if total > limit:
            continue
        if name_l == q:
            score = 0
        elif name_l.startswith(q):
            score = 1
        elif all(t in name_l for t in toks):
            score = 2
        else:
            score = 3
        d = dict(r)
        d["path"] = p_list          # 网页契约：[{id,name},...]
        d["path_str"] = p_str       # bot 与高亮用
        d["score"] = score
        items.append(d)
    items.sort(key=lambda d: (d["score"], -d["id"]))
    return items, total


# ---------------- 鉴权 ----------------
security = HTTPBasic()


def auth(cred: HTTPBasicCredentials = Depends(security)):
    import secrets as _s
    ok_user = _s.compare_digest(cred.username, AUTH_USER)
    ok_pass = _s.compare_digest(cred.password, AUTH_PASS) if AUTH_PASS else True
    if not (ok_user and ok_pass):
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Basic"})
    return True


app = FastAPI(title="TG Storage Pool", docs_url=None, redoc_url=None)


@app.on_event("startup")
async def _startup():
    init_db()
    global BOT_TASK
    if BOT_POLL:
        BOT_TASK = asyncio.create_task(_bot_poll_loop())
    else:
        log.info("bot: 命令轮询已关闭 (TG_BOT_POLL=0)")


@app.on_event("shutdown")
async def _shutdown():
    if BOT_TASK is not None and not BOT_TASK.done():
        BOT_TASK.cancel()
        try:
            await BOT_TASK
        except BaseException:  # noqa: BLE001
            pass
    await client.aclose()


# ---------------- 概览 ----------------
@app.get("/api/stats", dependencies=[Depends(auth)])
async def stats():
    with db() as conn:
        r = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(size),0) s FROM files").fetchone()
        f = conn.execute("SELECT COUNT(*) c FROM folders").fetchone()
    return {"count": r["c"], "bytes": r["s"], "folders": f["c"], "chat_id": CHAT_ID}


# ---------------- 浏览 / 搜索 ----------------
@app.get("/api/list", dependencies=[Depends(auth)])
async def list_dir(folder_id: Optional[int] = None, q: str = ""):
    with db() as conn:
        if folder_id is not None and not folder_exists(conn, folder_id):
            raise HTTPException(404, "folder not found")

        # 搜索模式：跨目录，文件名 + 所在路径包含即命中
        if q.strip():
            files, total = search_files(conn, q, limit=500)
            return {"folder_id": None, "path": [], "folders": [], "files": files,
                    "search": q.strip(), "total": total}

        folders = [
            dict(r)
            for r in conn.execute(
                """
                SELECT f.id, f.name, f.created_at,
                       (SELECT COUNT(*) FROM files x   WHERE x.folder_id = f.id) AS files,
                       (SELECT COUNT(*) FROM folders y WHERE y.parent_id = f.id) AS subfolders
                FROM folders f
                WHERE f.parent_id IS ?
                ORDER BY f.name COLLATE NOCASE
                """,
                (folder_id,),
            ).fetchall()
        ]
        files = [
            dict(r)
            for r in conn.execute(
                "SELECT id,name,size,mime,created_at FROM files"
                " WHERE folder_id IS ? ORDER BY id DESC",
                (folder_id,),
            ).fetchall()
        ]
        path = folder_path(conn, folder_id)
    return {"folder_id": folder_id, "path": path, "folders": folders, "files": files}


@app.get("/api/folders/tree", dependencies=[Depends(auth)])
async def folder_tree():
    with db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id,name,parent_id FROM folders ORDER BY name COLLATE NOCASE"
        ).fetchall()]
    kids = {}
    for it in rows:
        kids.setdefault(it["parent_id"], []).append(it)
    out = []

    def walk(pid, depth):
        for it in kids.get(pid, []):
            out.append({"id": it["id"], "name": it["name"],
                        "parent_id": it["parent_id"], "depth": depth})
            walk(it["id"], depth + 1)

    walk(None, 0)
    return {"folders": out}


# ---------------- 文件夹 CRUD ----------------
@app.post("/api/folders", dependencies=[Depends(auth)])
async def create_folder(payload: dict = Body(...)):
    parent = payload.get("parent_id")
    name = clean_folder_name(payload.get("name"))
    with db() as conn:
        if not folder_exists(conn, parent):
            raise HTTPException(404, "父文件夹不存在")
        if conn.execute("SELECT 1 FROM folders WHERE parent_id IS ? AND name=?",
                        (parent, name)).fetchone():
            raise HTTPException(409, f"当前目录下已存在同名文件夹「{name}」")
        cur = conn.execute(
            "INSERT INTO folders(name,parent_id,created_at) VALUES(?,?,?)",
            (name, parent, time.time()),
        )
        conn.commit()
        path = path_of(conn, cur.lastrowid)
    jlog({"t": "mkdir", "path": path})
    return {"id": cur.lastrowid, "name": name, "parent_id": parent}


@app.patch("/api/folders/{fid}", dependencies=[Depends(auth)])
async def update_folder(fid: int, payload: dict = Body(...)):
    evt = None
    with db() as conn:
        if not folder_exists(conn, fid):
            raise HTTPException(404, "not found")
        cur_row = conn.execute("SELECT name,parent_id FROM folders WHERE id=?", (fid,)).fetchone()
        old_path = path_of(conn, fid)
        sets, args = [], []
        final_name = cur_row["name"]
        final_parent = cur_row["parent_id"]
        new_name = None
        if payload.get("name") is not None:
            new_name = clean_folder_name(payload["name"])
            final_name = new_name
            sets.append("name=?")
            args.append(new_name)
        if "parent_id" in payload:
            newp = payload["parent_id"]
            if newp is not None:
                if newp == fid:
                    raise HTTPException(400, "不能移动到自身")
                if newp in folder_scope(conn, fid):
                    raise HTTPException(400, "不能移动到自己的子文件夹")
                if not folder_exists(conn, newp):
                    raise HTTPException(404, "目标文件夹不存在")
            final_parent = newp
            sets.append("parent_id=?")
            args.append(newp)
        # 同级目录不允许同名（保证路径唯一，灾备重建时不会合并）
        if sets and conn.execute(
            "SELECT 1 FROM folders WHERE parent_id IS ? AND name=? AND id<>?",
            (final_parent, final_name, fid),
        ).fetchone():
            raise HTTPException(409, f"目标目录下已存在同名文件夹「{final_name}」")
        if sets:
            args.append(fid)
            conn.execute(f"UPDATE folders SET {', '.join(sets)} WHERE id=?", args)
            conn.commit()
            new_path = path_of(conn, fid)
            if new_path != old_path:
                evt = {"t": "mvd", "path": old_path, "new": new_path}  # 目录改名/移动
    if evt:
        jlog(evt)
    return {"ok": True}


@app.delete("/api/folders/{fid}", dependencies=[Depends(auth)])
async def delete_folder(fid: int, force: bool = False):
    with db() as conn:
        if not folder_exists(conn, fid):
            raise HTTPException(404, "not found")
        target_path = path_of(conn, fid)
        ids = folder_scope(conn, fid)
        ph = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT id,message_id FROM files WHERE folder_id IN ({ph})", ids
        ).fetchall()
        n = len(rows)
        if n and not force:
            raise HTTPException(409, f"文件夹非空（含 {n} 个文件），需递归删除")
        await delete_messages(rows)
        conn.execute(f"DELETE FROM files WHERE folder_id IN ({ph})", ids)
        conn.execute(f"DELETE FROM folders WHERE id IN ({ph})", ids)
        conn.commit()
    jlog({"t": "rmd", "path": target_path, "recursive": True, "files": n, "folders": len(ids)})
    log.info("folder %s deleted: %d folders, %d files", fid, len(ids), n)
    return {"ok": True, "deleted_files": n, "deleted_folders": len(ids)}


# ---------------- 文件 ----------------
@app.post("/api/upload", dependencies=[Depends(auth)])
async def upload(file: UploadFile = File(...), folder_id: Optional[int] = Form(None)):
    name = os.path.basename(file.filename or "unnamed")
    stamp = int(time.time())
    safe = re.sub(r"[^\w.\-]", "_", name)
    tmp = TMP_DIR / f"{stamp}_{safe}"

    with db() as conn:
        if not folder_exists(conn, folder_id):
            raise HTTPException(404, "目标文件夹不存在")
        folder_path_str = path_of(conn, folder_id)

    size = 0
    with open(tmp, "wb") as f:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD:
                f.close()
                tmp.unlink(missing_ok=True)
                raise HTTPException(413, f"文件超过上限 {MAX_UPLOAD // 1024 // 1024} MB")
            f.write(chunk)

    # caption 让 Telegram 侧消息自带目录信息（灾备时可据此恢复目录结构）
    form = {"chat_id": CHAT_ID, "disable_notification": "true"}
    if CAPTION_PATH and folder_path_str != "/":
        form["caption"] = f"TGPOOL {folder_path_str}"

    created = time.time()
    try:
        with open(tmp, "rb") as f:
            resp = await client.post(
                f"{API}/sendDocument",
                data=form,
                files={"document": (name, f)},
            )
        result = api_ok(resp.json())
    finally:
        tmp.unlink(missing_ok=True)

    doc = result.get("document") or {}
    file_id = doc.get("file_id")
    if not file_id:
        raise HTTPException(502, "上传成功但未返回 document.file_id")

    with db() as conn:
        cur = conn.execute(
            "INSERT INTO files(name,size,mime,file_id,file_unique,message_id,chat_id,folder_id,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (name, size, doc.get("mime_type"), file_id, doc.get("file_unique_id"),
             result.get("message_id"), str(CHAT_ID), folder_id, created),
        )
        conn.commit()
        fid = cur.lastrowid
    jlog({"t": "add", "id": fid, "name": name, "size": size, "mime": doc.get("mime_type"),
          "tg_file_id": file_id, "tg_unique": doc.get("file_unique_id"),
          "message_id": result.get("message_id"), "chat_id": str(CHAT_ID),
          "path": folder_path_str, "created_at": created})
    log.info("upload ok: %s (%d bytes) id=%s folder=%s", name, size, fid, folder_path_str)
    return {"id": fid, "name": name, "size": size, "folder_id": folder_id}


@app.patch("/api/files/{fid}", dependencies=[Depends(auth)])
async def move_file(fid: int, payload: dict = Body(...)):
    target = payload.get("folder_id")
    with db() as conn:
        if not conn.execute("SELECT 1 FROM files WHERE id=?", (fid,)).fetchone():
            raise HTTPException(404, "not found")
        if not folder_exists(conn, target):
            raise HTTPException(404, "目标文件夹不存在")
        old_path = path_of(conn, conn.execute(
            "SELECT folder_id FROM files WHERE id=?", (fid,)).fetchone()["folder_id"])
        conn.execute("UPDATE files SET folder_id=? WHERE id=?", (target, fid))
        conn.commit()
        new_path = path_of(conn, target)
    if old_path != new_path:
        jlog({"t": "mv", "id": fid, "path": new_path})
    return {"ok": True, "folder_id": target}


@app.delete("/api/files/{fid}", dependencies=[Depends(auth)])
async def delete_file(fid: int):
    with db() as conn:
        row = conn.execute(
            "SELECT id,message_id,chat_id FROM files WHERE id=?", (fid,)).fetchone()
        if not row:
            raise HTTPException(404, "not found")
        await delete_messages([row])
        conn.execute("DELETE FROM files WHERE id=?", (fid,))
        conn.commit()
    jlog({"t": "del", "id": fid})
    return {"ok": True}


def _range_header(start: int, end: int, total: int) -> dict:
    return {
        "Content-Range": f"bytes {start}-{end}/{total}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
    }


def _content_disposition(name: str) -> str:
    """RFC 6266 / RFC 5987 兼容的 Content-Disposition。

    HTTP 头只能装 latin-1 字节，中文文件名直接塞进去会抛 UnicodeEncodeError → 500。
    标准做法：给一个 ASCII 兜底名，再用 filename*=UTF-8''<百分号编码> 带上真名，
    现代浏览器都会优先用 filename*，于是中文名能正常保存。
    """
    fallback = name.encode("ascii", "replace").decode("ascii")
    fallback = fallback.replace("\\", "_").replace('"', "'").replace("\r", "").replace("\n", "")
    if not fallback.strip("?_. "):
        fallback = "download"
    return "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (fallback, quote(name, safe=""))


# ---------------- 下载预热与进度 ----------------

def _scan_bot_temp() -> dict:
    """列出 bot API temp/ 目录里"正在下载"的文件（名 -> 已落盘字节数）"""
    out = {}
    try:
        for p in BOT_TEMP_DIR.iterdir():
            if p.is_file():
                try:
                    out[p.name] = p.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return out


def _purge_prepare():
    now = time.time()
    for k in list(PREPARE):
        st = PREPARE[k]
        if st.get("state") in ("ready", "failed"):
            if now - st.get("ended", st.get("started", now)) > PREPARE_TTL:
                PREPARE.pop(k, None)


async def _getfile_local(file_id: str) -> str:
    """调 getFile，返回服务器本地绝对路径；未缓存时会阻塞到整份文件下载完成"""
    resp = await client.post(f"{API}/getFile", data={"file_id": file_id})
    info = api_ok(resp.json())
    fpath = info.get("file_path", "")
    p = Path(fpath)
    if not p.is_absolute():
        p = Path(LOCAL_FILE_ROOT) / fpath
    return str(p)


async def _prepare_job(fid: int, file_id: str, total: int):
    st = PREPARE[fid]
    before = set((await asyncio.to_thread(_scan_bot_temp)).keys())
    task = asyncio.create_task(_getfile_local(file_id))
    try:
        while not task.done():
            await asyncio.sleep(0.5)
            cur = await asyncio.to_thread(_scan_bot_temp)
            cand = [(n, s) for n, s in cur.items() if n not in before] or list(cur.items())
            if cand:
                name, size = max(cand, key=lambda kv: kv[1])
                st["temp"] = name
                st["bytes"] = size
        st["path"] = await task
        st["bytes"] = max(int(st.get("bytes") or 0), total)
        st["state"] = "ready"
        st["ended"] = time.time()
        log.info("prepare fid=%s 就绪 %s", fid, st["path"])
    except Exception as e:                        # noqa: BLE001
        st["state"] = "failed"
        st["error"] = str(e) or e.__class__.__name__
        st["ended"] = time.time()
        log.warning("prepare fid=%s 失败: %s", fid, st["error"])


def _prepare_payload(fid: int, row) -> dict:
    total = int(row["size"] or 0)
    st = PREPARE.get(fid)
    if st is None:
        return {"fid": fid, "state": "idle", "total": total, "bytes": 0,
                "percent": 0.0, "speed": 0, "eta": None, "elapsed": 0, "error": None}
    if st.get("state") == "ready" and not Path(st.get("path") or "").is_file():
        st["state"] = "failed"
        st["error"] = "缓存文件已消失，请重试"
    got = int(st.get("bytes") or 0)
    elapsed = max(0.0, time.time() - (st.get("started") or time.time()))
    sp = got / elapsed if elapsed > 0.3 and got else 0.0
    ready = st.get("state") == "ready"
    return {
        "fid": fid,
        "state": st.get("state"),
        "total": total,
        "bytes": total if ready else got,
        "percent": 100.0 if ready else (round(min(100.0, got * 100.0 / total), 1) if total else 0.0),
        "speed": round(sp),
        "eta": None if ready else (round((total - got) / sp, 1) if sp > 0 and total > got else None),
        "elapsed": round(elapsed, 1),
        "error": st.get("error"),
    }


@app.post("/api/download/{fid}/prepare", dependencies=[Depends(auth)])
async def download_prepare(fid: int):
    """把文件拉到服务器本地；幂等 —— 已缓存或已在拉取中就直接返回当前状态"""
    with db() as conn:
        row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
    if not row:
        raise HTTPException(404, "not found")
    _purge_prepare()
    st = PREPARE.get(fid)
    if st and st.get("state") == "ready" and Path(st.get("path") or "").is_file():
        return _prepare_payload(fid, row)
    if st and st.get("state") == "preparing" and st.get("task") and not st["task"].done():
        return _prepare_payload(fid, row)
    PREPARE[fid] = {"state": "preparing", "bytes": 0, "started": time.time(),
                    "path": None, "error": None, "temp": None}
    PREPARE[fid]["task"] = asyncio.create_task(
        _prepare_job(fid, row["file_id"], int(row["size"] or 0)))
    return _prepare_payload(fid, row)


@app.get("/api/download/{fid}/progress", dependencies=[Depends(auth)])
async def download_progress(fid: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
    if not row:
        raise HTTPException(404, "not found")
    return _prepare_payload(fid, row)


@app.get("/api/download/{fid}", dependencies=[Depends(auth)])
async def download(fid: int, request: Request):
    with db() as conn:
        row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
    if not row:
        raise HTTPException(404, "not found")

    resp = await client.post(f"{API}/getFile", data={"file_id": row["file_id"]})
    info = api_ok(resp.json())
    fpath = info.get("file_path", "")

    # --local 模式下 file_path 是服务器本地绝对路径
    local = Path(fpath)
    if not local.is_absolute():
        local = Path(LOCAL_FILE_ROOT) / fpath

    disposition = _content_disposition(row["name"])

    # 情况 1: 本地已有文件 -> 直接支持 Range 读取
    if local.is_file():
        total = local.stat().st_size
        rng = request.headers.get("range")
        start, end = 0, total - 1
        status = 200
        headers = {"Content-Disposition": disposition, "Accept-Ranges": "bytes"}
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = int(m.group(2))
                if start > end or start >= total:
                    raise HTTPException(416, "Range Not Satisfiable")
                status = 206
                headers.update(_range_header(start, end, total))
        length = end - start + 1

        async def iter_local():
            with open(local, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(1024 * 1024, left))
                    if not chunk:
                        break
                    left -= len(chunk)
                    yield chunk

        return StreamingResponse(iter_local(), status_code=status, headers=headers,
                                 media_type=row["mime"] or "application/octet-stream")

    # 情况 2: 本地没有 -> 从 Telegram 代理流式下载
    url = f"{FILE_API}/{fpath}"
    headers = {"Content-Disposition": disposition}
    req_headers = {}
    if request.headers.get("range"):
        req_headers["Range"] = request.headers["range"]
    upstream = await client.send(client.build_request("GET", url, headers=req_headers),
                                 stream=True)
    headers["Accept-Ranges"] = upstream.headers.get("accept-ranges", "bytes")
    if "content-range" in upstream.headers:
        headers["Content-Range"] = upstream.headers["content-range"]
    if "content-length" in upstream.headers:
        headers["Content-Length"] = upstream.headers["content-length"]

    async def iter_up():
        async for chunk in upstream.aiter_bytes(1024 * 1024):
            yield chunk

    return StreamingResponse(iter_up(), status_code=upstream.status_code, headers=headers,
                             media_type=row["mime"] or "application/octet-stream",
                             background=BackgroundTask(upstream.aclose))


# ---------------- 维护：手动索引备份 ----------------
@app.post("/api/backup", dependencies=[Depends(auth)])
async def manual_backup(local_only: bool = False):
    """跑一次 tools/backup_index.py：打包索引 + journal，存本地并（默认）上传一份到 Telegram。

    与每天 03:30 的定时备份用的是同一个脚本、同一套逻辑，只是改成手动触发。
    """
    if not BACKUP_SCRIPT.is_file():
        raise HTTPException(500, f"备份脚本不存在：{BACKUP_SCRIPT}")

    home = Path(DB_PATH).parent
    env = dict(os.environ)
    env.setdefault("TG_API_BASE", API_BASE)
    env.setdefault("TG_BACKUP_DIR", str(home / "backup"))
    env.setdefault("TG_ENV_FILE", str(home / "tgpool.env"))

    cmd = [sys.executable, str(BACKUP_SCRIPT)]
    if local_only:
        cmd.append("--no-remote")
    log.info("manual backup: %s", " ".join(cmd))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(home), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
    except asyncio.TimeoutError:
        raise HTTPException(504, "备份超时（超过 300 秒）")
    except OSError as e:
        raise HTTPException(500, f"无法执行备份脚本：{e}")

    out = raw.decode("utf-8", "replace").strip()
    log.info("manual backup rc=%s\n%s", proc.returncode, out)

    m = re.search(r"(tgpool-backup-\d{8}-\d{6}\.tar\.gz)", out)
    km = re.search(r"\(([\d.]+)\s*KB\)", out)
    cm = re.search(r"目录\s*(\d+)\s*/\s*文件\s*(\d+)", out)
    return {
        "ok": proc.returncode == 0,
        "rc": proc.returncode,
        "bundle": m.group(1) if m else None,
        "size_kb": float(km.group(1)) if km else None,
        "folders": int(cm.group(1)) if cm else None,
        "files": int(cm.group(2)) if cm else None,
        "local": "[√] 本地备份" in out,
        "remote": "[√] 已上传 Telegram 备份" in out,
        "local_only": local_only,
        "log": out[-6000:],
    }


# ---------------- 维护：bot API 本地文件缓存 ----------------
async def _run_cache_script(extra: list) -> dict:
    """调 tools/clean_cache.py --json 并把结果解析成 dict。"""
    if not CACHE_SCRIPT.is_file():
        raise HTTPException(500, f"缓存清理脚本不存在：{CACHE_SCRIPT}")
    env = dict(os.environ)
    env.setdefault("TG_LOCAL_ROOT", LOCAL_FILE_ROOT)
    cmd = [sys.executable, str(CACHE_SCRIPT), "--json", *extra]
    log.info("cache tool: %s", " ".join(cmd))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=600)
    except asyncio.TimeoutError:
        raise HTTPException(504, "缓存清理超时（超过 600 秒）")
    except OSError as e:
        raise HTTPException(500, f"无法执行缓存清理脚本：{e}")

    out = out_b.decode("utf-8", "replace").strip()
    diag = err_b.decode("utf-8", "replace").strip()
    try:
        data = json.loads(out)
    except ValueError:
        raise HTTPException(500, f"缓存脚本输出无法解析：{out[:300]}")
    data["rc"] = proc.returncode
    data["log"] = diag[-4000:]
    return data


@app.get("/api/cache", dependencies=[Depends(auth)])
async def cache_stats():
    """只读：bot API 本地文件缓存占了多少磁盘（按年龄分档）。"""
    return await _run_cache_script([])


@app.post("/api/cache/clean", dependencies=[Depends(auth)])
async def cache_clean(mode: str = "days", days: float = 30, max_gb: float = 10.0,
                      dry_run: bool = False):
    """清理 bot API 本地文件缓存。

    mode=days    删除 N 天未活动的缓存（默认 30 天）
    mode=max_gb  总量超过 max_gb 时，从最旧的删到阈值以下
    mode=all     全清（之后每次下载都要从 Telegram 回源，会变慢）
    只动 <root>/<token>/documents 和 temp，绝不碰 td.binlog。
    """
    if mode not in ("days", "max_gb", "all"):
        raise HTTPException(400, "mode 只能是 days / max_gb / all")
    extra = {"days": ["--days", str(days)],
             "max_gb": ["--max-gb", str(max_gb)],
             "all": ["--all", "--yes"]}[mode]
    if dry_run:
        extra = extra + ["--dry-run"]
    data = await _run_cache_script(extra)
    data["mode"] = mode
    return data


# ---------------- Telegram Bot 命令（在聊天里 /search 查文件） ----------------
# 设计要点：
#   1) 用 getUpdates 长轮询，不需要公网回调地址、不新开端口、不用证书；
#   2) 命中文件后优先 copyMessage —— 字节完全不动，Telegram 侧直接复制原消息，
#      零带宽、零延迟，等于把池子里的文件"捞"回对话；
#   3) 只响应白名单会话。否则 bot 一旦被别人搜到，整个池子的文件名就被翻出来了。
#   4) 必须单进程运行（tgpool.service 已经是 --workers 1）。多个 worker 会各自长轮询，
#      导致同一批 update 被重复处理、回复重复。
#   5) Telegram 侧只允许一个消费者：若曾经给这个 bot 设过 webhook，getUpdates 会被拒，
#      所以启动时先 deleteWebhook。
BOT_POLL = os.environ.get("TG_BOT_POLL", "1") not in ("0", "false", "no", "")
OFFSET_FILE = Path(os.environ.get("TG_BOT_OFFSET", str(Path(DB_PATH).parent / "tg_offset.json")))
ADMIN_IDS = {x.strip() for x in os.environ.get("TG_BOT_ADMIN_IDS", "").split(",") if x.strip()}
INBOX_NAME = os.environ.get("TG_BOT_INBOX", "收件箱")   # 直接发文件给 bot 时的默认落点
# 会被收录为池子文件的消息类型（sticker 不算文件，跳过）
FILE_KEYS = ("document", "video", "audio", "animation", "voice", "video_note")
PAGE_SIZE = 8                 # 每页列出的文件条数
MAX_HITS = 200                # 一次搜索最多收集的候选数
SEARCH_TTL = 1800             # 搜索结果在内存里保留 30 分钟（翻页用）
SEARCH_CACHE = {}             # token -> {"kw", "items", "ts"}
# /rm 是唯一不可逆的操作：删文件夹要先点一次「确认删除」，加 -f 才跳过。
# 待确认的操作放在内存里（不落盘 —— 重启后按钮失效是好事，不会误删）。
RM_PENDING = {}               # token -> {"id","path","files","folders","size","ts"}
RM_TTL = 600                  # 确认按钮 10 分钟内有效
# 是否允许在聊天里查网页密码（/pass）。白名单会话本来就能取走池子里的任何文件，
# 所以默认开着；介意的话设 TG_BOT_SHOW_PASS=0。
SHOW_PASS = os.environ.get("TG_BOT_SHOW_PASS", "1") not in ("0", "false", "no", "")
# 云端备份注册表（tools/backup_index.py 每次上传成功追加一条）。
# 备份包是灾备基础设施，不进池子索引 —— 否则网页/bot 里的删除会连带删掉 Telegram 上的异地副本。
# /backups 只读这份注册表来列出与取回。
BACKUP_DIR_BOT = Path(os.environ.get("TG_BACKUP_DIR") or (Path(DB_PATH).parent / "backup"))
REMOTE_LOG = BACKUP_DIR_BOT / "remote.jsonl"
MAX_BACKUPS = 14              # 最多列出最近 N 份（与 TG_BACKUP_KEEP 默认值一致）
BOT_TASK = None
BOT_STATE = {"enabled": BOT_POLL, "last_poll": 0, "last_update": 0,
             "handled": 0, "last_error": None, "token_ok": None}

HELP_TEXT = """TG 存储池 · 命令一览

/search 关键词   模糊搜索：文件名或所在路径包含即命中
   例：/search 报表　/search 2026 财务

/ls [路径]       列出目录内容，省略路径则列根目录
   例：/ls  /ls /工作/2026

/get 编号        按编号取回文件（编号来自搜索结果）
/move 源 目标    移动文件或文件夹，目标留空 = 挪到根目录
   例：/move /工作/2026 /归档　/move "/我的 报告"
/rm 路径         删除文件；删文件夹会先问一句再动手
   例：/rm /工作/2026/报表.pdf　/rm /工作/2026　/rm #1234
   /rm -f 路径    跳过确认直接删

/backups         列出云端索引备份包，点编号即可取回
/stats           池子统计
/pass            忘记网页密码时把账号密码捞回来
/help            显示本帮助

搜索命中后，点结果下方的数字按钮即可把文件取回本对话。
取回用的是 Telegram 侧的复制，不消耗服务器带宽，也无需等待。

收录文件：直接把文件发给我就行（手机、桌面端都可以）。
默认放进 /收件箱；在说明里写一行路径就放进该目录。
   例：发文件时说明写 /工作/2026
目录不存在会自动创建；同一个文件不会重复收录。"""

RM_USAGE = """用法：/rm [-f] [-dir|-file] 路径

/rm /工作/2026/报表.pdf   删除一个文件
/rm /工作/2026            删除整个文件夹及其全部内容（先确认）
/rm #1234                 按编号删（编号来自 /ls、/search）

  -f      跳过确认，直接删
  -dir    只当文件夹处理　-file  只当文件处理
          （同名文件和文件夹同时存在时才需要）"""

MOVE_USAGE = """用法：/move '源路径' '目标文件夹'

/move /工作/2026 /归档     把 /工作/2026 挪到 /归档 下
/move /报表模板.docx /模板  移动单个文件
/move "/我的 报告"          只写源 = 挪到根目录

源可以是文件或文件夹；目标目录不存在会自动创建。
路径里含空格时用引号包起来。"""


def _human(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024
    return "0 B"


def _form(params: dict) -> dict:
    out = {}
    for k, v in params.items():
        if v is None:
            continue
        out[k] = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
    return out


async def _tg(method: str, **params) -> dict:
    """调 Telegram API，永不抛异常 —— 失败以 {"ok": False, "description": ...} 返回。"""
    try:
        resp = await client.post(f"{API}/{method}", data=_form(params))
        return resp.json()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "description": f"{type(e).__name__}: {e}"}


def _allowed(chat_id, from_id) -> bool:
    ids = {str(chat_id), str(from_id)}
    if CHAT_ID and CHAT_ID in ids:
        return True
    return bool(ids & ADMIN_IDS)


def _load_offset():
    try:
        return int(json.loads(OFFSET_FILE.read_text(encoding="utf-8"))["offset"])
    except Exception:  # noqa: BLE001
        return None


def _save_offset(off: int) -> None:
    try:
        OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
        OFFSET_FILE.write_text(json.dumps({"offset": off}), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.warning("bot: 保存 offset 失败 %s", e)


def _search_bot(conn, kw: str):
    """bot 用的搜索：返回 (items, total)，items 已截断到 MAX_HITS。"""
    raw, total = search_files(conn, kw, limit=MAX_HITS)
    items = [{"id": it["id"], "name": it["name"], "size": it["size"],
              "path": it["path_str"]} for it in raw]
    return items, total


def _collect_hits(conn, kw: str) -> list:
    """bot 搜索结果：文件名或所在路径包含关键词即命中（与网页同一套规则）。"""
    return _search_bot(conn, kw)[0]


def _resolve_path(conn, path: str):
    """'/工作/2026' -> (folder_id, 错误信息)。根目录返回 (None, None)。"""
    cur, segs = None, [s for s in (path or "").split("/") if s]
    if not segs:
        return None, None
    for seg in segs:
        row = conn.execute(
            "SELECT id FROM folders WHERE parent_id IS ? AND name=?", (cur, seg)).fetchone()
        if not row:
            return None, f"路径不存在：{path}"
        cur = row["id"]
    return cur, None


# ---------------- /rm · /move 的路径解析 ----------------
def _norm_path(path: str) -> str:
    """'/a//b/' -> '/a/b'；'' 与 '/' 都归一化成根目录 '/'"""
    return "/" + "/".join(s for s in (path or "").split("/") if s)


def _split_args(raw: str) -> list:
    """按「引号优先」切分用户输入：'a b' "c" -> ['a b', 'c']；无引号则按空白切。

    Telegram 不会替我们保留 argv，路径里有空格时用户会用引号包起来；
    手机输入法常把引号变成中文全角，所以一并接受。
    """
    pairs = {"'": "'", '"': '"', "“": "”", "‘": "’", "「": "」"}
    out, buf, quote = [], [], None
    for ch in raw:
        if quote:
            if ch == pairs[quote]:
                quote = None
            else:
                buf.append(ch)
        elif ch in pairs:
            quote = ch
        elif ch.isspace():
            if buf:
                out.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def _resolve_node(conn, path: str, want: str = ""):
    """把 /a/b/c 解析成 (类型, id)：类型是 'folder' 或 'file'，根目录是 ('folder', None)。

    want='folder' / 'file' 时只认该类型；同名时默认目录优先（回复里会写明删的是哪个，
    要删同名文件加 -file）。找不到返回 (None, None, 错误信息)。
    """
    p = _norm_path(path)
    if p == "/":
        if want == "file":
            return None, None, "根目录不是文件，没法按文件处理"
        return "folder", None, None
    folder_id, _ = _resolve_path(conn, p)          # 目录精确匹配（目录存在时一定命中）
    parent, name = p.rsplit("/", 1)
    pfid, perr = _resolve_path(conn, parent or "/")
    file_id = None
    if not perr:
        row = conn.execute("SELECT id FROM files WHERE folder_id IS ? AND name=?",
                           (pfid, name)).fetchone()
        file_id = row["id"] if row else None
    if want == "folder":
        if folder_id is None:
            return None, None, f"目录不存在：{p}"
        return "folder", folder_id, None
    if want == "file":
        if file_id is None:
            return None, None, f"文件不存在：{p}"
        return "file", file_id, None
    if folder_id is not None:
        return "folder", folder_id, None
    if file_id is not None:
        return "file", file_id, None
    return None, None, f"路径不存在：{p}"


def _same_name_file(conn, path: str):
    """同层是否还有同名文件（同名时 /rm 默认删目录，这里用来提示清楚）"""
    p = _norm_path(path)
    if p == "/":
        return None
    parent, name = p.rsplit("/", 1)
    pfid, perr = _resolve_path(conn, parent or "/")
    if perr:
        return None
    return conn.execute("SELECT id FROM files WHERE folder_id IS ? AND name=?",
                        (pfid, name)).fetchone()



# ---------------- 反向上传：把用户发给 bot 的文件登记进池子 ----------------
# 关键认知：文件发到 bot 时**已经存在 Telegram 上了**，服务器一个字节都不需要传。
# 所谓"上传"在这里只是登记一条索引（name/size/file_id/message_id/路径），并写一条 journal。
# 所以没有大小限制、没有等待、也不占服务器带宽。
def _incoming_file(msg: dict):
    """从消息里取出可收录的文件信息；不是文件则返回 None。"""
    for key in FILE_KEYS:
        obj = msg.get(key)
        if not obj or not obj.get("file_id"):
            continue
        name = (obj.get("file_name") or "").strip()
        if not name and key == "audio":
            title = (obj.get("title") or "").strip()
            if title:
                name = f"{title}.mp3"
        if not name:
            ext = {"voice": ".ogg", "video_note": ".mp4", "video": ".mp4",
                   "audio": ".mp3", "animation": ".mp4"}.get(key, "")
            name = f"{key}-{(obj.get('file_unique_id') or 'x')[:12]}{ext}"
        return {"file_id": obj["file_id"],
                "file_unique": obj.get("file_unique_id"),
                "name": name,
                "size": int(obj.get("file_size") or 0),
                "mime": obj.get("mime_type")}
    photos = msg.get("photo")
    if photos:
        best = max(photos, key=lambda p: int(p.get("file_size") or 0))
        if best.get("file_id"):
            return {"file_id": best["file_id"],
                    "file_unique": best.get("file_unique_id"),
                    "name": f"photo-{(best.get('file_unique_id') or 'x')[:12]}.jpg",
                    "size": int(best.get("file_size") or 0),
                    "mime": "image/jpeg"}
    return None


def _ensure_folder_path(conn, path: str) -> int:
    """按 /a/b/c 逐级查找，缺哪级建哪级，返回末级 folder_id。"""
    cur = None
    for seg in [s for s in (path or "").split("/") if s]:
        name = clean_folder_name(seg)
        row = conn.execute(
            "SELECT id FROM folders WHERE parent_id IS ? AND name=?", (cur, name)).fetchone()
        if row:
            cur = row["id"]
            continue
        new = conn.execute(
            "INSERT INTO folders(name,parent_id,created_at) VALUES(?,?,?)",
            (name, cur, time.time()))
        conn.commit()
        cur = new.lastrowid
        jlog({"t": "mkdir", "path": path_of(conn, cur)})
    return cur


async def _handle_file_message(msg: dict, cid, info: dict) -> None:
    target_path = None
    cap = (msg.get("caption") or "").strip()
    if cap.startswith("/"):                     # 用说明(第一行)当目标路径
        target_path = cap.splitlines()[0].strip()

    with db() as conn:
        if info["file_unique"]:
            dup = conn.execute("SELECT id,name FROM files WHERE file_unique=?",
                               (info["file_unique"],)).fetchone()
            if dup:
                await _tg("sendMessage", chat_id=cid,
                          text=f"这个文件已经在池子里了，未重复收录。\n\n"
                               f"#{dup['id']}  {dup['name']}")
                return

        if target_path:
            fid = _ensure_folder_path(conn, target_path)
        else:
            row = conn.execute("SELECT id FROM folders WHERE parent_id IS NULL AND name=?",
                               (INBOX_NAME,)).fetchone()
            if row:
                fid = row["id"]
            else:
                new = conn.execute(
                    "INSERT INTO folders(name,parent_id,created_at) VALUES(?,?,?)",
                    (INBOX_NAME, None, time.time()))
                conn.commit()
                fid = new.lastrowid
                jlog({"t": "mkdir", "path": "/" + INBOX_NAME})

        created = time.time()
        cur = conn.execute(
            "INSERT INTO files(name,size,mime,file_id,file_unique,message_id,chat_id,"
            "folder_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (info["name"], info["size"], info["mime"], info["file_id"], info["file_unique"],
             msg.get("message_id"), str(cid), fid, created))
        conn.commit()
        new_id = cur.lastrowid
        path = path_of(conn, fid)

    # journal 是灾备重建的唯一依据，必须和网页上传走同一套字段
    jlog({"t": "add", "id": new_id, "name": info["name"], "size": info["size"],
          "mime": info["mime"], "tg_file_id": info["file_id"], "tg_unique": info["file_unique"],
          "message_id": msg.get("message_id"), "chat_id": str(cid),
          "path": path, "created_at": created})

    # 没说明的消息补一个 TGPOOL 标记，让 Telegram 侧自带目录信息；已有说明则不覆盖
    if not cap:
        await _tg("editMessageCaption", chat_id=cid, message_id=msg.get("message_id"),
                  caption=f"TGPOOL {path}")

    log.info("bot: 收录 %s (%s bytes) id=%s path=%s", info["name"], info["size"], new_id, path)
    await _tg("sendMessage", chat_id=cid,
              text=f"已收录 #{new_id}\n{info['name']}\n"
                   f"{_human(info['size'])} · {path}\n\n"
                   f"想放别的目录：发文件时在说明里写一行路径，例：/工作/2026\n"
                   f"（目录不存在会自动创建）")


def _nav_keyboard(kb: list, token: str, page: int, pages: int) -> None:
    if pages <= 1:
        return
    nav = []
    if page > 0:
        nav.append({"text": "« 上一页", "callback_data": f"p:{token}:{page-1}"})
    if page < pages - 1:
        nav.append({"text": "下一页 »", "callback_data": f"p:{token}:{page+1}"})
    if nav:
        kb.append(nav)


async def _reply_search(chat_id, kw: str, page: int = 0, token: str = None, edit_msg=None):
    """发（或原地改写）搜索结果。token 用于翻页时复用同一批结果。"""
    with db() as conn:
        cache = SEARCH_CACHE.get(token) if token else None
        if cache and cache["kw"] == kw:
            items, total = cache["items"], cache["total"]
            cache["ts"] = time.time()
        else:
            items, total = _search_bot(conn, kw)
            token = secrets.token_urlsafe(6)
            SEARCH_CACHE[token] = {"kw": kw, "items": items, "total": total, "ts": time.time()}
            page = 0

    total = len(items)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    chunk = items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    if total == 0:
        text = f"搜索「{kw}」没有命中任何文件。\n\n换个关键词，或用 /ls 按目录浏览。"
    else:
        head = f"搜索「{kw}」命中 {total} 个文件"
        if pages > 1:
            head += f"（第 {page + 1}/{pages} 页）"
        lines = [head, ""]
        for i, it in enumerate(chunk, start=page * PAGE_SIZE + 1):
            lines.append(f"{i}. {it['name']}")
            lines.append(f"    {_human(it['size'])} · {it['path']} · #{it['id']}")
        lines.append("")
        lines.append("点下面的编号即可把文件取回本对话。")
        if total > len(items):
            lines.append(f"（命中 {total} 个，仅列出最新的 {len(items)} 个——"
                         "换个更具体的关键词可缩小范围）")
        text = "\n".join(lines)[:4000]

    kb, row = [], []
    for i, it in enumerate(chunk, start=page * PAGE_SIZE + 1):
        row.append({"text": str(i), "callback_data": f"g:{it['id']}"})
        if len(row) == 4:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    _nav_keyboard(kb, token, page, pages)
    markup = {"inline_keyboard": kb} if kb else None

    if edit_msg:
        r = await _tg("editMessageText", chat_id=edit_msg[0], message_id=edit_msg[1],
                      text=text, reply_markup=(markup or {"inline_keyboard": []}))
        if r.get("ok"):
            return
        log.warning("bot: 改写消息失败，改为新发一条 %s", r.get("description"))
    await _tg("sendMessage", chat_id=chat_id, text=text, reply_markup=markup,
              disable_web_page_preview=True)


def _load_backup_records() -> list:
    """读备份注册表 remote.jsonl（backup_index.py 每次成功上传追加一条）。"""
    try:
        return [json.loads(x) for x in REMOTE_LOG.read_text(encoding="utf-8").splitlines()
                if x.strip()]
    except FileNotFoundError:
        return []
    except Exception as e:  # noqa: BLE001
        log.warning("bot: 读取备份注册表失败 %s: %s", REMOTE_LOG, e)
        return []


async def _reply_backups(chat_id) -> None:
    """列出云端索引备份包；点编号用 copyMessage 零带宽取回，不进池子索引。"""
    recs = _load_backup_records()[-MAX_BACKUPS:]
    if not recs:
        await _tg("sendMessage", chat_id=chat_id,
                  text="还没有云端备份记录。\n\n"
                       "手动备份：网页上的「备份索引」按钮，或 /api/backup。")
        return
    total = len(_load_backup_records())
    lines = [f"云端索引备份（共 {total} 份，列出最近 {len(recs)} 份）", ""]
    for i, r in enumerate(recs, start=1):
        lines.append(f"{i}. {r.get('name', '?')}")
        lines.append(f"    {r.get('at', '?')} · {_human(r.get('size'))}")
    lines += ["", "点编号把备份包复制到本对话（零带宽）。"]
    kb, row = [], []
    for i, r in enumerate(recs, start=1):
        mid = r.get("message_id")
        if not mid:
            continue
        row.append({"text": str(i), "callback_data": f"b:{mid}"})
        if len(row) == 4:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    markup = {"inline_keyboard": kb} if kb else None
    await _tg("sendMessage", chat_id=chat_id, text="\n".join(lines)[:4000],
              reply_markup=markup, disable_web_page_preview=True)


async def _send_backup(chat_id, mid: int) -> bool:
    """把云端备份包投递回对话：copyMessage 直接复制原消息，零带宽。"""
    r = await _tg("copyMessage", chat_id=chat_id, from_chat_id=CHAT_ID, message_id=mid)
    if r.get("ok"):
        log.info("bot: copyMessage 备份包 mid=%s", mid)
        return True
    log.warning("bot: 取回备份失败 mid=%s: %s", mid, r.get("description"))
    return False


async def _reply_ls(chat_id, path: str):
    with db() as conn:
        fid, err = _resolve_path(conn, path)
        if err:
            await _tg("sendMessage", chat_id=chat_id,
                      text=f"{err}\n\n用 /ls 查看根目录。")
            return
        subs = [dict(r) for r in conn.execute(
            "SELECT f.id, f.name,"
            " (SELECT COUNT(*) FROM files x WHERE x.folder_id=f.id) AS files,"
            " (SELECT COUNT(*) FROM folders y WHERE y.parent_id=f.id) AS subs"
            " FROM folders f WHERE f.parent_id IS ? ORDER BY f.name COLLATE NOCASE", (fid,))]
        files_ = [dict(r) for r in conn.execute(
            "SELECT id,name,size FROM files WHERE folder_id IS ?"
            " ORDER BY id DESC LIMIT 100", (fid,))]
        here = path_of(conn, fid)

    lines = [f"目录 {here}", ""]
    if subs:
        lines.append(f"子目录（{len(subs)}）")
        for s in subs[:40]:
            lines.append(f"  {s['name']}/   {s['files']} 文件 · {s['subs']} 子目录")
        lines.append("")
    if files_:
        lines.append(f"文件（{len(files_)}）")
        for f_ in files_:
            lines.append(f"  {f_['name']}   {_human(f_['size'])} · #{f_['id']}")
    if not subs and not files_:
        lines.append("（空目录）")
    lines += ["", "取文件：/get 编号　进目录：/ls 完整路径",
              "删：/rm 路径　移：/move 源路径 目标目录"]
    await _tg("sendMessage", chat_id=chat_id, text="\n".join(lines)[:4000],
              disable_web_page_preview=True)


async def _send_file(chat_id, fid: int) -> bool:
    """把池子里的文件投递到对话。copyMessage 零带宽；失败退回 file_id 重发。"""
    with db() as conn:
        row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
    if not row:
        return False
    if row["message_id"]:
        r = await _tg("copyMessage", chat_id=chat_id,
                      from_chat_id=(row["chat_id"] or CHAT_ID),
                      message_id=row["message_id"])
        if r.get("ok"):
            log.info("bot: copyMessage 投递 fid=%s", fid)
            return True
        log.warning("bot: copyMessage 失败 fid=%s: %s", fid, r.get("description"))
    # 回退：直接用已存在的 file_id 发送，Telegram 复用同一份文件，同样不重新上传
    r = await _tg("sendDocument", chat_id=chat_id, document=row["file_id"])
    if r.get("ok"):
        log.info("bot: sendDocument(file_id) 投递 fid=%s", fid)
        return True
    log.warning("bot: 投递失败 fid=%s: %s", fid, r.get("description"))
    return False


# ---------------- /rm：在聊天里删文件 / 删目录 ----------------
# 与网页端的删除走同一套 journal 事件（del / rmd），所以删完依然能被
# rebuild_index.py 完整重放 —— 这是池子敢让人随手删的前提。
# 顺序固定：先删 Telegram 侧消息，再删索引，最后写 journal。
async def _do_delete_file(cid, fid: int) -> None:
    with db() as conn:
        row = conn.execute(
            "SELECT id,name,size,message_id,chat_id,folder_id FROM files WHERE id=?",
            (fid,)).fetchone()
        if not row:
            await _tg("sendMessage", chat_id=cid, text=f"文件 #{fid} 不存在（可能已被删除）")
            return
        where = path_of(conn, row["folder_id"])
        await delete_messages([row])
        conn.execute("DELETE FROM files WHERE id=?", (fid,))
        conn.commit()
    jlog({"t": "del", "id": fid, "name": row["name"], "size": row["size"], "path": where})
    log.info("bot: /rm 删除文件 %s #%s (%s)", row["name"], fid, where)
    await _tg("sendMessage", chat_id=cid,
              text=f"已删除文件\n\n{row['name']}\n{_human(row['size'])} · {where}\n\n"
                   f"Telegram 上的原消息已一并清除，无法找回。")


async def _do_delete_folder(cid, fid: int, edit_msg=None) -> None:
    """递归删除目录。edit_msg=(chat_id, message_id) 时把那条消息改成进度/结果。"""
    with db() as conn:
        ids = folder_scope(conn, fid)
        if not ids:
            await _tg("sendMessage", chat_id=cid, text="这个目录已经不存在了（可能已被删除）")
            return
        ph = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT id,name,size,message_id,chat_id FROM files"
            f" WHERE folder_id IN ({ph})", ids).fetchall()
        target = path_of(conn, fid)
        n, nf = len(rows), len(ids)
        total = sum(r["size"] or 0 for r in rows)

        if edit_msg:
            await _tg("editMessageText", chat_id=edit_msg[0], message_id=edit_msg[1],
                      text=f"正在删除 {target}（{n} 个文件）…")
        elif n > 100:      # 大目录先吱一声，别让用户以为卡住了
            await _tg("sendMessage", chat_id=cid,
                      text=f"正在删除 {target}（{n} 个文件）…")

        await delete_messages(rows)
        conn.execute(f"DELETE FROM files WHERE folder_id IN ({ph})", ids)
        conn.execute(f"DELETE FROM folders WHERE id IN ({ph})", ids)
        conn.commit()
    jlog({"t": "rmd", "path": target, "recursive": True, "files": n, "folders": nf})
    log.info("bot: /rm 删除目录 %s：%d 个子目录 / %d 个文件", target, nf - 1, n)

    text = (f"已删除文件夹\n\n{target}\n"
            f"{_human(total)} · {n} 个文件 · {nf - 1} 个子目录\n\n"
            f"Telegram 上的原消息已一并清除，无法找回。")
    if edit_msg:
        r = await _tg("editMessageText", chat_id=edit_msg[0], message_id=edit_msg[1],
                      text=text, reply_markup={"inline_keyboard": []})
        if r.get("ok"):
            return
    await _tg("sendMessage", chat_id=cid, text=text)


async def _reply_rm(cid, body: str) -> None:
    force, want, paths = False, "", []
    for a in _split_args(body):
        if a in ("-f", "--force"):
            force = True
        elif a in ("-dir", "--dir", "-d"):
            want = "folder"
        elif a in ("-file", "--file"):
            want = "file"
        else:
            paths.append(a)
    if len(paths) != 1:
        await _tg("sendMessage", chat_id=cid, text=RM_USAGE)
        return
    arg = paths[0]

    if arg.startswith("#") and arg[1:].isdigit():     # /rm #1234 —— 按编号删，最不容易认错
        with db() as conn:
            row = conn.execute("SELECT id FROM files WHERE id=?", (int(arg[1:]),)).fetchone()
        if not row:
            await _tg("sendMessage", chat_id=cid,
                      text=f"编号 #{arg[1:]} 不存在（可能已被删除）")
            return
        await _do_delete_file(cid, row["id"])
        return

    with db() as conn:
        kind, nid, err = _resolve_node(conn, arg, want)
        if err:
            await _tg("sendMessage", chat_id=cid, text=f"{err}\n\n{RM_USAGE}")
            return
        if kind == "folder" and nid is None:
            await _tg("sendMessage", chat_id=cid,
                      text="根目录不能删。要删哪个目录就把完整路径写全，例：/rm /工作/2026")
            return
        if kind == "file":
            await _do_delete_file(cid, nid)
            return

        target = path_of(conn, nid)
        ids = folder_scope(conn, nid)
        ph = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT id,size FROM files WHERE folder_id IN ({ph})", ids).fetchall()
        n, nf = len(rows), len(ids)
        total = sum(r["size"] or 0 for r in rows)
        ambiguous = bool(_same_name_file(conn, target))

    if force:
        await _do_delete_folder(cid, nid)
        return

    tok = secrets.token_urlsafe(9)[:12]
    RM_PENDING[tok] = {"id": nid, "path": target, "files": n, "folders": nf,
                       "size": total, "ts": time.time()}
    lines = ["要删除整个文件夹吗？", "",
             f"目录   {target}",
             f"内容   {nf - 1} 个子目录 · {n} 个文件 · 共 {_human(total)}", ""]
    if ambiguous:
        lines.append("（同层还有一个同名文件，这次删的是文件夹；"
                     "只删文件请加 -file）\n")
    lines.append("删掉会连 Telegram 上的原消息一起清除，删完无法找回。")
    lines.append("确认请点下面的按钮（10 分钟内有效）。")
    kb = {"inline_keyboard": [[
        {"text": "确认删除", "callback_data": f"r:{tok}:y"},
        {"text": "取消", "callback_data": f"r:{tok}:n"}]]}
    await _tg("sendMessage", chat_id=cid, text="\n".join(lines), reply_markup=kb)
    log.info("bot: /rm 待确认 path=%s files=%d folders=%d", target, n, nf)


# ---------------- /move：在聊天里挪文件 / 挪目录 ----------------
# 目录移动写 journal 的 mvd（{path, new}），文件移动写 mv（{id, path}），
# 与网页端一致，重建时照旧能还原。
async def _reply_move(cid, body: str) -> None:
    args = _split_args(body)
    if not args or len(args) > 2:
        await _tg("sendMessage", chat_id=cid, text=MOVE_USAGE)
        return
    src = args[0]
    dst = args[1] if len(args) > 1 else ""
    sp, dp = _norm_path(src), _norm_path(dst)

    with db() as conn:
        kind, nid, err = _resolve_node(conn, src)
        if err:
            await _tg("sendMessage", chat_id=cid, text=f"{err}\n\n{MOVE_USAGE}")
            return
        if kind == "folder" and nid is None:
            await _tg("sendMessage", chat_id=cid, text="根目录不能移动。")
            return
        # 自环检查放在建目录之前：否则会先在源目录里凭空造出一个目标目录
        if kind == "folder" and (dp == sp or dp.startswith(sp + "/")):
            await _tg("sendMessage", chat_id=cid,
                      text=f"不能把 {sp} 挪到它自己或它的子目录（{dp}）里。")
            return

        made = []
        target_id, terr = _resolve_path(conn, dp)
        if terr:
            # 目标目录不存在就照上传的惯例逐级自动创建（每级都写 journal）
            parent = "/" + "/".join(dp.split("/")[1:-1])
            name = dp.rsplit("/", 1)[-1]
            pfid, perr = _resolve_path(conn, parent or "/")
            if not perr and conn.execute(
                    "SELECT 1 FROM files WHERE folder_id IS ? AND name=?",
                    (pfid, name)).fetchone():
                await _tg("sendMessage", chat_id=cid,
                          text=f"目标位置已经有个同名文件：{dp}\n"
                               f"换个目录名，或者先把那个文件挪走。")
                return
            before = {r["id"] for r in conn.execute("SELECT id FROM folders").fetchall()}
            target_id = _ensure_folder_path(conn, dp)
            made = sorted(path_of(conn, r["id"]) for r in
                          conn.execute("SELECT id FROM folders").fetchall()
                          if r["id"] not in before)

        if kind == "folder":
            old = path_of(conn, nid)
            if target_id == nid:
                await _tg("sendMessage", chat_id=cid,
                          text=f"{old} 已经在 {dp} 里了，没动。")
                return
            if target_id is not None and target_id in folder_scope(conn, nid):
                await _tg("sendMessage", chat_id=cid,
                          text="不能把文件夹挪进它自己的子目录里。")
                return
            base = old.rsplit("/", 1)[-1]
            if conn.execute("SELECT 1 FROM folders WHERE parent_id IS ? AND name=? AND id<>?",
                            (target_id, base, nid)).fetchone():
                await _tg("sendMessage", chat_id=cid,
                          text=f"{dp.rstrip('/')}/{base} 已经有同名文件夹了，"
                               f"先改个名或换个目标目录。")
                return
            conn.execute("UPDATE folders SET parent_id=? WHERE id=?", (target_id, nid))
            conn.commit()
            new = path_of(conn, nid)
            ids = folder_scope(conn, nid)
            ph = ",".join("?" * len(ids))
            nf = len(ids)
            n = conn.execute(f"SELECT COUNT(*) c FROM files WHERE folder_id IN ({ph})",
                             ids).fetchone()["c"]
        else:
            row = conn.execute("SELECT name,folder_id FROM files WHERE id=?",
                               (nid,)).fetchone()
            if row["folder_id"] == target_id:
                await _tg("sendMessage", chat_id=cid,
                          text=f"{src} 已经在 {dp} 里了，没动。")
                return
            old = path_of(conn, row["folder_id"])
            conn.execute("UPDATE files SET folder_id=? WHERE id=?", (target_id, nid))
            conn.commit()
            new_dir = path_of(conn, target_id)
            old = old.rstrip("/") + "/" + row["name"]
            new = new_dir.rstrip("/") + "/" + row["name"]

    if kind == "folder":
        jlog({"t": "mvd", "path": old, "new": new, "by": "bot"})
        text = f"已移动文件夹\n\n{old}  →  {new}\n{n} 个文件 · {nf - 1} 个子目录"
    else:
        # journal 约定：mv 的 path 记的是**所在目录**（不是文件全路径），
        # rebuild_index.py 重放时用它定位 folder_id；写成文件全路径会被当成目录树。
        jlog({"t": "mv", "id": nid, "path": new_dir, "by": "bot"})
        text = f"已移动文件\n\n{old}  →  {new}"
    if made:
        text += "\n\n（目标目录原本不存在，已自动创建：" + "、".join(made) + "）"
    log.info("bot: /move %s -> %s", old, new)
    await _tg("sendMessage", chat_id=cid, text=text)


# ---------------- /pass：忘记网页密码时把它捞回来 ----------------
def _local_ip() -> str:
    """取本机对外 IP（UDP connect 不发包）；拿不到就退回 127.0.0.1。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:  # noqa: BLE001
        return "127.0.0.1"
    finally:
        s.close()


async def _reply_pass(cid) -> None:
    if not SHOW_PASS:
        await _tg("sendMessage", chat_id=cid,
                  text="已经在聊天里关掉了密码查询（TG_BOT_SHOW_PASS=0）。\n\n"
                       "到服务器上执行下面这行即可：\n"
                       f"python3 {PASS_SCRIPT}")
        return
    ip = _local_ip()
    text = "\n".join([
        "网页登录信息",
        "",
        f"外网   https://{ip}:{NGINX_PORT}/",
        f"本机   http://127.0.0.1:8080/",
        f"账号   {AUTH_USER}",
        f"密码   {AUTH_PASS or '（TG_AUTH_PASS 没设，任意密码都能进）'}",
        "",
        "服务器上随时可查：",
        f"  python3 {PASS_SCRIPT}",
        "",
        "这条消息里有密码，看完可以点下面的按钮把它删掉。",
    ])
    kb = {"inline_keyboard": [[{"text": "删掉这条消息", "callback_data": "q"}]]}
    await _tg("sendMessage", chat_id=cid, text=text, reply_markup=kb)
    log.info("bot: /pass 已回复网页登录信息")


async def _handle_message(msg: dict) -> None:
    chat = msg.get("chat") or {}
    frm = msg.get("from") or {}
    cid = chat.get("id")
    if not _allowed(cid, frm.get("id")):
        # 未授权会话一律静默（不确认、不收录），避免对外暴露这是个存储池
        log.warning("bot: 忽略未授权会话 chat=%s from=%s", cid, frm.get("id"))
        return

    text = (msg.get("text") or "").strip()
    if not text:
        # 不是文字 -> 可能是用户直接发来的文件，登记进池子
        info = _incoming_file(msg)
        if info:
            BOT_STATE["handled"] += 1
            await _handle_file_message(msg, cid, info)
        return
    if not text.startswith("/"):
        return

    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    args = parts[1:]
    # /move、/rm 的路径里可能带空格和引号，所以额外保留命令后的原始文本
    body = text.split(None, 1)[1].strip() if len(parts) > 1 else ""
    BOT_STATE["handled"] += 1

    if cmd in ("/search", "/s", "/find"):
        if not args:
            await _tg("sendMessage", chat_id=cid, text="用法：/search 关键词\n例：/search 报表")
            return
        await _reply_search(cid, " ".join(args))
        log.info("bot: /search %s", " ".join(args))

    elif cmd in ("/ls", "/dir", "/list"):
        await _reply_ls(cid, " ".join(args))
        log.info("bot: /ls %s", " ".join(args))

    elif cmd == "/get":
        raw = (args[0].lstrip("#") if args else "")
        if not raw.isdigit():
            await _tg("sendMessage", chat_id=cid,
                      text="用法：/get 编号\n编号来自 /search 或 /ls 结果里的 #数字")
            return
        if not await _send_file(cid, int(raw)):
            await _tg("sendMessage", chat_id=cid, text=f"取回 #{raw} 失败，可能已被删除。")

    elif cmd in ("/rm", "/del", "/delete"):
        await _reply_rm(cid, body)

    elif cmd in ("/move", "/mv"):
        await _reply_move(cid, body)

    elif cmd in ("/pass", "/pwd", "/password"):
        await _reply_pass(cid)

    elif cmd in ("/backups", "/bk"):
        await _reply_backups(cid)
        log.info("bot: /backups")

    elif cmd == "/stats":
        with db() as conn:
            r = conn.execute(
                "SELECT COUNT(*) c, COALESCE(SUM(size),0) s FROM files").fetchone()
            nf = conn.execute("SELECT COUNT(*) c FROM folders").fetchone()["c"]
        await _tg("sendMessage", chat_id=cid,
                  text=f"池子统计\n\n文件 {r['c']} 个 · 共 {_human(r['s'])}\n"
                       f"文件夹 {nf} 个")

    elif cmd in ("/start", "/help", "/h"):
        await _tg("sendMessage", chat_id=cid, text=HELP_TEXT)

    else:
        await _tg("sendMessage", chat_id=cid,
                  text=f"未知命令 {cmd}\n\n" + HELP_TEXT)


async def _handle_callback(cb: dict) -> None:
    cbid = cb.get("id")
    frm = cb.get("from") or {}
    msg = cb.get("message") or {}
    chat = msg.get("chat") or {}
    cid = chat.get("id")
    if not _allowed(cid, frm.get("id")):
        await _tg("answerCallbackQuery", callback_query_id=cbid, text="无权限")
        return

    data = cb.get("data") or ""
    if data.startswith("g:"):
        fid = int(data[2:] or 0)
        await _tg("answerCallbackQuery", callback_query_id=cbid, text="正在取回…")
        if not await _send_file(cid, fid):
            await _tg("sendMessage", chat_id=cid, text=f"取回 #{fid} 失败，可能已被删除。")

    elif data.startswith("b:"):
        mid = int(data[2:] or 0)
        await _tg("answerCallbackQuery", callback_query_id=cbid, text="正在取回备份…")
        if not await _send_backup(cid, mid):
            await _tg("sendMessage", chat_id=cid,
                      text="取回备份失败，消息可能已被清理（超过 14 份的旧备份会被滚动删除）。")

    elif data.startswith("p:"):
        _, token, pg = (data.split(":", 2) + ["0"])[:3]
        cache = SEARCH_CACHE.get(token)
        if not cache:
            await _tg("answerCallbackQuery", callback_query_id=cbid,
                      text="这组结果已过期，请重新搜索")
            return
        await _tg("answerCallbackQuery", callback_query_id=cbid)
        await _reply_search(cid, cache["kw"], page=int(pg or 0), token=token,
                            edit_msg=(cid, msg.get("message_id")))

    elif data.startswith("r:"):
        _, tok, act = (data.split(":", 2) + ["", ""])[:3]
        p = RM_PENDING.pop(tok, None)          # 用完即弃，避免重复点造成二次删除
        if not p:
            await _tg("answerCallbackQuery", callback_query_id=cbid,
                      text="这个确认已经失效，重新发一次 /rm 吧")
            return
        if act != "y":
            await _tg("answerCallbackQuery", callback_query_id=cbid, text="已取消")
            await _tg("editMessageText", chat_id=cid, message_id=msg.get("message_id"),
                      text=f"已取消，什么都没删：{p['path']}",
                      reply_markup={"inline_keyboard": []})
            return
        await _tg("answerCallbackQuery", callback_query_id=cbid, text="开始删除…")
        await _do_delete_folder(cid, p["id"], edit_msg=(cid, msg.get("message_id")))

    elif data == "q":
        # /pass 那类含敏感内容的消息：点一下就由 bot 自己删掉
        await _tg("answerCallbackQuery", callback_query_id=cbid, text="已删除")
        await _tg("deleteMessage", chat_id=cid, message_id=msg.get("message_id"))

    else:
        await _tg("answerCallbackQuery", callback_query_id=cbid)


async def _bot_poll_loop() -> None:
    log.info("bot: 命令轮询启动 (api=%s)", API_BASE)
    r = await _tg("deleteWebhook", drop_pending_updates="false")
    if not r.get("ok"):
        log.warning("bot: deleteWebhook 失败：%s（若曾设置过 webhook，getUpdates 会被拒）",
                    r.get("description"))

    offset = _load_offset()
    if offset is None:
        # 首次启动：先摸一下最后一条 update，跳过历史积压，只处理从现在开始的消息
        r = await _tg("getUpdates", offset=-1, timeout=0)
        res = r.get("result") or []
        offset = (res[-1]["update_id"] + 1) if res else 0
        _save_offset(offset)
        log.info("bot: 首次启动，跳过历史消息，从 offset=%s 开始", offset)

    while True:
        try:
            r = await _tg("getUpdates", offset=offset, timeout=25,
                          allowed_updates=["message", "callback_query"])
            BOT_STATE["last_poll"] = time.time()
            if not r.get("ok"):
                desc = str(r.get("description"))
                BOT_STATE["last_error"] = desc
                code = r.get("error_code")
                if code == 401:
                    BOT_STATE["token_ok"] = False
                    log.error("bot: token 被 Telegram 拒绝 (401 Unauthorized)。"
                              "请到 BotFather 确认 token 是否已重置，"
                              "并更新 tgpool.env 里的 TG_BOT_TOKEN 后重跑部署脚本。")
                    await asyncio.sleep(120)
                else:
                    log.warning("bot: getUpdates 失败：%s", desc)
                    await asyncio.sleep(10)
                continue

            BOT_STATE["token_ok"] = True
            BOT_STATE["last_error"] = None
            for upd in r.get("result") or []:
                offset = upd["update_id"] + 1
                _save_offset(offset)
                BOT_STATE["last_update"] = upd["update_id"]
                try:
                    if "message" in upd:
                        await _handle_message(upd["message"])
                    elif "callback_query" in upd:
                        await _handle_callback(upd["callback_query"])
                except Exception as e:  # noqa: BLE001
                    log.warning("bot: 处理更新失败 %s: %s", type(e).__name__, e)

            # 顺手清理过期的搜索结果缓存 / 待确认的删除
            now = time.time()
            for k in [k for k, v in SEARCH_CACHE.items() if now - v["ts"] > SEARCH_TTL]:
                SEARCH_CACHE.pop(k, None)
            for k in [k for k, v in RM_PENDING.items() if now - v["ts"] > RM_TTL]:
                RM_PENDING.pop(k, None)

        except asyncio.CancelledError:
            log.info("bot: 命令轮询停止")
            raise
        except Exception as e:  # noqa: BLE001
            BOT_STATE["last_error"] = f"{type(e).__name__}: {e}"
            log.warning("bot: 轮询异常 %s: %s", type(e).__name__, e)
            await asyncio.sleep(5)


@app.get("/api/bot/status", dependencies=[Depends(auth)])
async def bot_status():
    """诊断：轮询是否活着、token 是否有效。"""
    probe = await _tg("getMe")
    return {
        **BOT_STATE,
        "poll_enabled": BOT_POLL,
        "offset": _load_offset(),
        "chat_id": CHAT_ID,
        "admins": sorted(ADMIN_IDS),
        "getme_ok": bool(probe.get("ok")),
        "getme": probe.get("result") or probe.get("description"),
        "searches_cached": len(SEARCH_CACHE),
    }


# ---------------- 前端 ----------------
STATIC = Path(__file__).parent / "static"
if STATIC.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(auth)])
async def index():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)
