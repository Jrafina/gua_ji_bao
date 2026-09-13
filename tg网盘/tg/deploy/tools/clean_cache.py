#!/usr/bin/env python3
"""
clean_cache.py —— 清理 bot API server 的文件缓存

背景
----
自建 bot API server 在 `--local` 模式下，**下载过的文件会在服务器本地留一份缓存**，
放在 `<root>/<token>/documents/` 里，而且**没有任何自动清理机制**：

  - 上传      → 不占盘（bot API 直通转发给 Telegram，转发完不留副本）
  - 下载      → 占盘，每个下载过的文件都留一份，永久累积
  - 从网页删文件 → **不会**释放缓存（bot API 不知道你删了索引记录）

删掉缓存是**安全**的：file_id 存在索引里，下次再下载时 bot API 会自动从 Telegram
重新拉取，只是那一遍会退回慢速（受服务器带宽限制）。

绝对不碰的东西
--------------
  - `<root>/<token>/td.binlog`  ← TDLib 的数据库日志，删了可能丢失会话状态
  - 任何不在 `documents/` 和 `temp/` 里的文件

用法
----
  python3 clean_cache.py                    # 只看，不删（报告大小 / 数量 / 年龄分布）
  python3 clean_cache.py --json             # 机器可读输出（给网页接口用）
  python3 clean_cache.py --days 30          # 删除 30 天未改动的缓存
  python3 clean_cache.py --days 30 --dry-run
  python3 clean_cache.py --max-gb 10        # 超过 10GB 就从最旧的删到低于阈值
  python3 clean_cache.py --all              # 全部清空（慎用：之后所有下载都要回源）
  python3 clean_cache.py --all --yes
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("TG_LOCAL_ROOT", "/var/lib/telegram-bot-api"))
# 缓存只可能出现在这两个子目录；其它一律不动
SAFE_SUBDIRS = ("documents", "temp")
# 最近这段时间内被写过的文件跳过，避免删掉正在下载中的文件
RECENT_GUARD_SEC = 600


def find_cache_dirs(root: Path) -> list:
    """定位 <root>/<token>/documents、<root>/<token>/temp。"""
    dirs = []
    if not root.is_dir():
        return dirs
    for token_dir in sorted(root.iterdir()):
        if not token_dir.is_dir():
            continue
        for sub in SAFE_SUBDIRS:
            d = token_dir / sub
            if d.is_dir():
                dirs.append(d)
    return dirs


def scan(dirs: list) -> list:
    """返回 [(path, size, mtime, atime)]。"""
    items = []
    for d in dirs:
        for p in d.rglob("*"):
            if not p.is_file():
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            items.append((p, st.st_size, st.st_mtime, getattr(st, "st_atime", st.st_mtime)))
    return items


def age_of(item) -> float:
    """文件的"最后活动时间"：取 mtime / atime 里较新的那个（atime 可能被 noatime 关掉）。"""
    _, _, mtime, atime = item
    return max(mtime, atime)


def report(items: list, freed: int = 0, removed: int = 0, dry: bool = False) -> dict:
    now = time.time()
    buckets = {"lt_1d": [0, 0], "1_7d": [0, 0], "7_30d": [0, 0], "gt_30d": [0, 0]}
    total = 0
    for p, size, mtime, atime in items:
        total += size
        age = now - age_of((p, size, mtime, atime))
        if age < 86400:
            k = "lt_1d"
        elif age < 7 * 86400:
            k = "1_7d"
        elif age < 30 * 86400:
            k = "7_30d"
        else:
            k = "gt_30d"
        buckets[k][0] += 1
        buckets[k][1] += size
    return {
        "root": str(ROOT),
        "files": len(items),
        "bytes": total,
        "mb": round(total / 1024 / 1024, 1),
        "buckets": {k: {"files": v[0], "bytes": v[1]} for k, v in buckets.items()},
        "dry_run": dry,
        "removed_files": removed,
        "freed_bytes": freed,
        "freed_mb": round(freed / 1024 / 1024, 1),
    }


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0
    return str(n)


def print_report(r: dict, head: str = "缓存现状") -> None:
    print("== %s ==" % head)
    print("  位置   : %s/<token>/documents" % r["root"])
    print("  总量   : %s (%d 个文件)" % (human(r["bytes"]), r["files"]))
    print("  年龄分布:")
    labels = [("lt_1d", "24 小时内"), ("1_7d", "1-7 天"), ("7_30d", "7-30 天"), ("gt_30d", "30 天以上")]
    for k, label in labels:
        b = r["buckets"][k]
        if b["files"]:
            print("    %-10s %4d 个  %10s" % (label, b["files"], human(b["bytes"])))
    if r["removed_files"]:
        print("  ---")
        print("  %s: %d 个文件，%s %s"
              % ("将删除" if r.get("dry_run") else "已删除",
                 r["removed_files"],
                 "预计释放" if r.get("dry_run") else "释放",
                 human(r["freed_bytes"])))
    print()


def do_delete(items: list, dry: bool) -> tuple:
    """执行删除。逐文件日志一律走 stderr，保证 stdout 在 --json 模式下只有 JSON。"""
    freed = 0
    removed = 0
    now = time.time()
    for p, size, mtime, atime in items:
        if now - age_of((p, size, mtime, atime)) < RECENT_GUARD_SEC:
            print("  [跳过] 最近 10 分钟内活跃，可能正在下载: %s" % p.name, file=sys.stderr)
            continue
        if dry:
            print("  [将删] %-28s %10s" % (p.name, human(size)), file=sys.stderr)
            freed += size
            removed += 1
            continue
        try:
            p.unlink()
            freed += size
            removed += 1
        except OSError as e:
            print("  [失败] %s: %s" % (p.name, e), file=sys.stderr)
    return removed, freed


def main() -> int:
    ap = argparse.ArgumentParser(description="清理 bot API server 的文件缓存")
    ap.add_argument("--days", type=float, default=None, help="删除 N 天未活动的缓存")
    ap.add_argument("--all", action="store_true", help="清空全部缓存（慎用）")
    ap.add_argument("--max-gb", type=float, default=None, help="总量超过 N GB 时，从最旧的删到低于阈值")
    ap.add_argument("--dry-run", action="store_true", help="只显示将删除什么，不真删")
    ap.add_argument("--yes", action="store_true", help="跳过 --all 的确认")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出（供网页接口调用）")
    ap.add_argument("--root", default=None, help="覆盖缓存根目录（默认 /var/lib/telegram-bot-api）")
    args = ap.parse_args()

    global ROOT
    if args.root:
        ROOT = Path(args.root)

    dirs = find_cache_dirs(ROOT)
    if not dirs:
        out = {"root": str(ROOT), "files": 0, "bytes": 0, "mb": 0.0, "buckets": {},
               "removed_files": 0, "freed_bytes": 0, "freed_mb": 0.0,
               "error": "未找到缓存目录（bot API 是否运行过？）"}
        print(json.dumps(out, ensure_ascii=False, indent=2) if args.json else "未找到缓存目录: %s" % ROOT)
        return 0 if args.json else 1

    items = scan(dirs)
    now = time.time()
    targets = []
    mode = "查看"

    if args.all:
        mode = "清空全部"
        targets = list(items)
        if not args.dry_run and not args.yes:
            total = sum(i[1] for i in items)
            sys.stderr.write("将删除全部 %d 个缓存文件（%s）。之后每次下载都要从 Telegram 回源。\n"
                             "确认请输入 yes: " % (len(items), human(total)))
            sys.stderr.flush()
            if sys.stdin.readline().strip().lower() != "yes":
                print("已取消")
                return 1
    elif args.max_gb is not None:
        mode = "按容量上限"
        limit = args.max_gb * 1024 ** 3
        total = sum(i[1] for i in items)
        if total <= limit:
            sys.stderr.write("当前 %s，未超过上限 %.1f GB，无需清理\n" % (human(total), args.max_gb))
            if args.json:
                print(json.dumps(report(items), ensure_ascii=False, indent=2))
            return 0
        # 从最旧的开始删，直到降到阈值以下
        ordered = sorted(items, key=age_of)
        acc = total
        for it in ordered:
            if acc <= limit:
                break
            targets.append(it)
            acc -= it[1]
    elif args.days is not None:
        mode = "按时间"
        cutoff = now - args.days * 86400
        targets = [i for i in items if age_of(i) < cutoff]
    else:
        r = report(items)
        if args.json:
            print(json.dumps(r, ensure_ascii=False, indent=2))
        else:
            print_report(r)
            print("提示：--days N 按时间清理，--max-gb N 按容量清理，--all 全清，加 --dry-run 先预览")
        return 0

    sys.stderr.write("== 清理模式：%s%s ==\n" % (mode, "（预览，未真删）" if args.dry_run else ""))
    removed, freed = do_delete(targets, args.dry_run)

    # 删后重新统计（预览模式什么都没删，直接沿用原快照）
    remaining = items if args.dry_run else scan(dirs)
    r = report(remaining, freed, removed, dry=args.dry_run)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        if args.dry_run:
            print("\n预计释放 %s（%d 个文件）—— 预览模式，未真删" % (human(freed), removed))
            print_report(r, "当前（未变更）")
        else:
            print("\n释放 %s（%d 个文件）" % (human(freed), removed))
            print_report(r, "清理后")
    return 0


if __name__ == "__main__":
    sys.exit(main())
