#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pkmkv.com 「电影推荐」列表监控 + 邮件通知
==================================================

功能
----
1. 每天 08:00 / 12:00 / 18:00 / 24:00（默认 Asia/Shanghai 时区）定时抓取
   https://www.pkmkv.com/ 首页中
   //[@id="change-ul-1"]/li[i]/div[1]/a/img   （默认取前 6 项）
2. 记录电影名称 + 资质（HD / TC / 超清 …），持久化到 state.json，并把每次
   快照追加到 history/history.jsonl 留档。
3. 与上一次快照对比，出现「新增 / 下架 / 画质变化 / 片名变化」时用
   Resend API 发邮件提醒（发件人 notify@1795857.xyz，收件人 1795857787@qq.com）。
   Resend 是第三方代发，不需要 SMTP 授权码，直接用 API Key 调用即可。

依赖
----
Python >= 3.8 标准库 + requests（requests 缺失时自动退回 urllib，可无依赖运行）。

用法
----
    python3 movie_notify.py --once        # 立即抓取一次
    python3 movie_notify.py               # 守护进程，常驻按时间点调度
    python3 movie_notify.py --baseline    # 强制刷新基线（不发邮件）
    python3 movie_notify.py --test-email  # 发一封测试邮件，验证 Resend Key
    python3 movie_notify.py --status      # 查看当前快照

配置文件：同目录 config.json（也可用环境变量覆盖，见下方 CONFIG 说明）。
"""

from __future__ import annotations

import argparse
import copy
import html
import hashlib
import json
import logging
import logging.handlers
import os
import random
import signal
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

try:  # requests 可选
    import requests  # type: ignore
except Exception:  # pragma: no cover
    requests = None

# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config.json"

#: 内置默认配置。可用 config.json 覆盖，再用环境变量覆盖。
DEFAULT_CONFIG: Dict[str, Any] = {
    # ---- 抓取 ----
    "url": "https://www.pkmkv.com/",
    # 只取前 N 项（对应 //[@id="change-ul-1"]/li[i]/div[1]/a/img 的前 N 个 li）
    "count": 6,
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "http_timeout": 25,          # 单次请求超时（秒）
    "max_retries": 3,            # 抓取失败重试次数
    "retry_backoff": 5,          # 重试间隔基数（秒），逐次线性递增

    # ---- 调度（24 点即 0 点）----
    "tz": "Asia/Shanghai",
    "schedule_hours": [8, 12, 18, 0],

    # ---- 邮件 ----
    "from": "notify@1795857.xyz",
    "to": ["1795857787@qq.com"],
    "subject_prefix": "[pkmkv 更新]",
    "resend_base_url": "https://api.resend.com/emails",
    "resend_api_key": "",        # 建议用环境变量 RESEND_API_KEY 提供，别写死在文件里
    "dry_run": False,            # True = 只打印邮件内容，不真正发送
    "notify_on_order_change": False,  # 仅顺序变化（内容不变）时是否也发邮件

    # ---- 其它 ----
    "cover_image_in_email": True, # 邮件里是否内嵌海报图
    "state_file": "state.json",
    "history_file": "history/history.jsonl",
    "log_file": "logs/movie_notify.log",
    "log_max_bytes": 2 * 1024 * 1024,
    "log_backup_count": 3,
}

#: 环境变量 → 配置项 映射（环境变量优先级最高）
ENV_MAP: Dict[str, str] = {
    "RESEND_API_KEY": "resend_api_key",
    "NOTIFY_FROM": "from",
    "NOTIFY_TO": "to",          # 逗号分隔
    "NOTIFY_URL": "url",
    "NOTIFY_COUNT": "count",
    "NOTIFY_TZ": "tz",
    "NOTIFY_HOURS": "schedule_hours",   # 逗号分隔
    "NOTIFY_DRY_RUN": "dry_run",
    "NOTIFY_STATE": "state_file",
}

# --------------------------------------------------------------------------
# 极简 HTML DOM（标准库实现，避免依赖 lxml / bs4）
# --------------------------------------------------------------------------

#: 自闭合标签（不需要 </tag>）
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
#: 遇到新标签时会自动关闭的标签（浏览器容错行为）
AUTO_CLOSE = {
    "p": {"p"},
    "li": {"li"},
    "td": {"td"},
    "th": {"th"},
    "tr": {"tr", "td", "th"},
    "dt": {"dt", "dd"},
    "dd": {"dt", "dd"},
    "option": {"option"},
    "thead": {"thead", "tbody", "tfoot"},
    "tbody": {"thead", "tbody", "tfoot"},
    "tfoot": {"thead", "tbody", "tfoot"},
}


@dataclass
class Node:
    """轻量 DOM 节点。"""

    tag: Optional[str] = None
    attrs: List[Tuple[str, str]] = field(default_factory=list)
    children: List[Any] = field(default_factory=list)  # Node 或 str(文本)
    parent: Optional["Node"] = field(default=None, repr=False)

    # -- 属性 --------------------------------------------------------------
    @property
    def attr_dict(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for k, v in self.attrs:
            out.setdefault(k.lower(), v)
        return out

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        return self.attr_dict.get(name.lower(), default)

    def classes(self) -> List[str]:
        c = self.get("class")
        return c.split() if c else []

    def has_class(self, *names: str) -> bool:
        cls = set(self.classes())
        return all(n in cls for n in names)

    # -- 遍历 --------------------------------------------------------------
    @property
    def element_children(self) -> List["Node"]:
        return [c for c in self.children if isinstance(c, Node)]

    def find_by_id(self, eid: str) -> Optional["Node"]:
        for node in self.iter_all():
            if node.get("id") == eid:
                return node
        return None

    def iter_all(self) -> Iterator["Node"]:
        """深度优先遍历所有后代元素（含自身之外的全部）。"""
        stack = [self]
        while stack:
            n = stack.pop()
            for c in n.children:
                if isinstance(c, Node):
                    yield c
                    stack.append(c)

    def direct(self, tag: Optional[str] = None, cls: Optional[str] = None) -> List["Node"]:
        """直接子元素，可按标签名 / class（单个或多个，空格分隔）过滤。"""
        out = []
        want_cls = cls.split() if cls else []
        for c in self.children:
            if not isinstance(c, Node):
                continue
            if tag and c.tag != tag.lower():
                continue
            if want_cls and not set(want_cls) <= set(c.classes()):
                continue
            out.append(c)
        return out

    def descendants(self, tag: Optional[str] = None, cls: Optional[str] = None) -> List["Node"]:
        """所有后代元素（深度优先），可按标签 / class 过滤。"""
        want_cls = cls.split() if cls else []
        out = []
        for n in self.iter_all():
            if tag and n.tag != tag.lower():
                continue
            if want_cls and not set(want_cls) <= set(n.classes()):
                continue
            out.append(n)
        return out

    def first(self, tag: Optional[str] = None, cls: Optional[str] = None) -> Optional["Node"]:
        got = self.direct(tag, cls)
        return got[0] if got else None

    def first_desc(self, tag: Optional[str] = None, cls: Optional[str] = None) -> Optional["Node"]:
        got = self.descendants(tag, cls)
        return got[0] if got else None

    # -- 文本 --------------------------------------------------------------
    def text(self, collapse: bool = True) -> str:
        """节点内的全部文本（含子元素文本）。"""
        parts: List[str] = []

        def walk(n: "Node") -> None:
            for c in n.children:
                if isinstance(c, str):
                    parts.append(c)
                else:
                    walk(c)

        walk(self)
        s = "".join(parts)
        if collapse:
            s = " ".join(s.split())
        return s.strip()


class TreeBuilder(HTMLParser):
    """用 html.parser 建出一棵可查询的 DOM 树；注释 / doctype 全部忽略。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node(tag="#document")
        self.stack: List[Node] = [self.root]

    def _open(self, tag: str, attrs: List[Tuple[str, str]]) -> Node:
        # 浏览器容错：遇到 <li> 时自动关掉上一个未闭合 <li>
        for other in AUTO_CLOSE.get(tag, ()):
            if any(n.tag == other for n in reversed(self.stack[1:])):
                del self.stack[self.stack.index(
                    next(n for n in reversed(self.stack) if n.tag == other)):]
                break
        node = Node(tag=tag, attrs=list(attrs), parent=self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)
        return node

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, str]]) -> None:
        self._open(tag.lower(), attrs)

    def handle_startendtag(self, tag: str, attrs: Sequence[Tuple[str, str]]) -> None:
        self._open(tag.lower(), attrs)  # 自闭合写法 <img/> 走同一条路

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return
        # 找不到匹配的闭合标签时忽略（浏览器也这样容错）

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1].children.append(data)


def parse_html(text: str) -> Node:
    builder = TreeBuilder()
    builder.feed(text)
    builder.close()
    return builder.root


# --------------------------------------------------------------------------
# 抓取
# --------------------------------------------------------------------------


@dataclass
class Movie:
    """列表里的一部电影。"""

    name: str
    quality: str
    url: str
    img: str = ""
    score: str = ""

    @property
    def key(self) -> str:
        """稳定标识：详情页 URL 优先，缺失时用图床地址。"""
        return self.url or self.img or self.name

    def fingerprint(self) -> str:
        return f"{self.key}|{self.name}|{self.quality}"


def http_get(url: str, cfg: Dict[str, Any]) -> str:
    """带 UA / 超时 / 重试的 GET，返回响应正文（utf-8）。"""
    headers = {
        "User-Agent": cfg["user_agent"],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
    }
    last_err: Optional[Exception] = None
    attempts = 1 + int(cfg.get("max_retries", 3))
    for i in range(attempts):
        try:
            if requests is not None:
                r = requests.get(url, headers=headers, timeout=cfg["http_timeout"])
                r.raise_for_status()
                return r.content.decode("utf-8", errors="replace")
            # ---- urllib 回退 ----
            import urllib.request
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=cfg["http_timeout"]) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if i < attempts - 1:
                sleep_s = int(cfg.get("retry_backoff", 5)) * (i + 1)
                LOG.warning("抓取失败，%ds 后重试（%d/%d）：%s", sleep_s, i + 1, attempts, exc)
                time.sleep(sleep_s)
    raise RuntimeError(f"抓取 {url} 失败：{last_err}")


def extract_movies(root: Node, count: int) -> List[Movie]:
    """
    按 //[@id="change-ul-1"]/li[i]/div[1]/a/img 的路径取前 count 项。

    实际结构：
      <ul id="change-ul-1">
        <li>
          <div class="li-img">
            <a href="/mv/xxxx.html" title="片名">
              <img alt="片名" src="海报地址">
              <span class="bottom"><span class="bottom1"></span>
                <span class="bottom2"><i class="icon-play"></i>HD</span></span>
            </a>
          </div>
          <div class="li-bottom">...</div>
        </li>
    """
    ul = root.find_by_id("change-ul-1")
    if ul is None:
        raise RuntimeError('页面中找不到 id="change-ul-1" 的元素列表，页面结构可能已变化')

    items: List[Movie] = []
    for li in ul.direct("li")[:count]:
        div = li.direct("div")
        if not div:
            LOG.warning("li 里没有 div，跳过：%s", li.text()[:60])
            continue
        a = div[0].first("a")
        if a is None:
            LOG.warning("div[1] 里没有 a，跳过：%s", div[0].text()[:60])
            continue
        img = a.first("img")
        if img is None:
            LOG.warning("a 里没有 img，跳过：%s", a.text()[:60])
            continue

        # 片名：img 的 alt / a 的 title 优先
        name = (img.get("alt") or a.get("title") or a.text() or "").strip()
        # 资质：a 内部 span.bottom2 的文本（<i> 图标没有文本，直接取文本即可）
        bottom2 = a.first_desc("span", "bottom2")
        quality = (bottom2.text() if bottom2 is not None else "").strip() or "未知"

        items.append(Movie(
            name=name,
            quality=quality,
            url=(a.get("href") or "").strip(),
            img=(img.get("src") or "").strip(),
        ))
    if not items:
        raise RuntimeError("解析到 0 部电影，页面结构可能已变化")
    return items


# --------------------------------------------------------------------------
# 持久化
# --------------------------------------------------------------------------


def load_state(cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    p = BASE_DIR / cfg["state_file"]
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        LOG.warning("读取快照失败（%s），当作无历史处理：%s", p, exc)
        return None


def save_state(cfg: Dict[str, Any], items: Sequence[Movie], ts: datetime) -> None:
    p = BASE_DIR / cfg["state_file"]
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": ts.isoformat(timespec="seconds"),
        "count": len(items),
        "items": [asdict(m) for m in items],
    }
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)  # 原子替换，避免写一半崩溃


def append_history(cfg: Dict[str, Any], items: Sequence[Movie], ts: datetime) -> None:
    p = BASE_DIR / cfg["history_file"]
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": ts.isoformat(timespec="seconds"),
        "items": [{"name": m.name, "quality": m.quality, "url": m.url} for m in items],
    }
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------
# 差异对比
# --------------------------------------------------------------------------


def diff_movies(prev: Sequence[Movie], cur: Sequence[Movie]) -> Dict[str, List[Any]]:
    prev_map = {m.key: m for m in prev}
    cur_map = {m.key: m for m in cur}

    added = [cur_map[k] for k in cur_map if k not in prev_map]
    removed = [prev_map[k] for k in prev_map if k not in cur_map]
    quality_changed, name_changed = [], []
    for k in cur_map:
        if k in prev_map:
            if cur_map[k].quality != prev_map[k].quality:
                quality_changed.append((prev_map[k], cur_map[k]))
            if cur_map[k].name != prev_map[k].name:
                name_changed.append((prev_map[k], cur_map[k]))

    order_changed = [m.key for m in prev] != [m.key for m in cur]
    return {
        "added": added,
        "removed": removed,
        "quality_changed": quality_changed,
        "name_changed": name_changed,
        "order_changed": bool(order_changed),
    }


def has_content_change(diff: Dict[str, List[Any]]) -> bool:
    return bool(diff["added"] or diff["removed"] or diff["quality_changed"] or diff["name_changed"])


# --------------------------------------------------------------------------
# Resend 邮件
# --------------------------------------------------------------------------


def _esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def abs_url(cfg: Dict[str, Any], href: str) -> str:
    """把 /mv/xxx.html 这样的站内相对路径补成绝对 URL（邮件里点得开）。"""
    if not href:
        return ""
    if href.startswith(("http://", "https://", "#", "mailto:")):
        return href
    base = str(cfg.get("url") or "").rstrip("/")
    return f"{base}{href if href.startswith('/') else '/' + href}"


def build_email(cfg: Dict[str, Any], items: Sequence[Movie],
                diff: Optional[Dict[str, List[Any]]], ts: datetime,
                note: Optional[str] = None) -> Tuple[str, str, str]:
    """返回 (subject, text_body, html_body)。diff 为 None 时是测试邮件。"""
    prefix = cfg.get("subject_prefix", "[pkmkv 更新]")
    when = ts.strftime("%Y-%m-%d %H:%M %Z")

    if diff is None:
        subject = f"{prefix}测试邮件 - {when}"
    elif has_content_change(diff):
        parts = []
        if diff["added"]:
            parts.append(f"新增 {len(diff['added'])}")
        if diff["removed"]:
            parts.append(f"下架 {len(diff['removed'])}")
        if diff["quality_changed"]:
            parts.append(f"画质变化 {len(diff['quality_changed'])}")
        if diff["name_changed"]:
            parts.append(f"片名变化 {len(diff['name_changed'])}")
        subject = f"{prefix}有更新：{'、'.join(parts)} - {when}"
    else:
        subject = f"{prefix}无变化 - {when}"

    # ---- 文本版 ----
    lines = [f"监控时间：{when}", "", "当前列表（前 %d 项）：" % len(items), ""]
    for i, m in enumerate(items, 1):
        lines.append(f"{i}. {m.name}  [{m.quality}]  {abs_url(cfg, m.url)}")
    text = "\n".join(lines) + "\n"

    if diff is not None:
        lines2 = ["", "—— 变化明细 ——", ""]
        for m in diff["added"]:
            lines2.append(f"+ 新增：{m.name}  [{m.quality}]  {abs_url(cfg, m.url)}")
        for m in diff["removed"]:
            lines2.append(f"- 下架：{m.name}  [{m.quality}]  {abs_url(cfg, m.url)}")
        for old, new in diff["quality_changed"]:
            lines2.append(f"~ 画质：{new.name}  {old.quality} -> {new.quality}")
        for old, new in diff["name_changed"]:
            lines2.append(f"~ 片名：{old.name} -> {new.name}")
        if diff["order_changed"] and not has_content_change(diff):
            lines2.append("~ 顺序变化（内容相同）")
        if note:
            lines2.append("")
            lines2.append(note)
        if lines2:
            text += "\n".join(lines2) + "\n"

    # ---- HTML 版 ----
    rows = ""
    for i, m in enumerate(items, 1):
        img_tag = ""
        if cfg.get("cover_image_in_email") and m.img:
            link = abs_url(cfg, m.url)
            img_tag = (f'<a href="{_esc(link)}" target="_blank">'
                       f'<img src="{_esc(m.img)}" width="90" alt="{_esc(m.name)}" '
                       f'style="width:90px;height:120px;object-fit:cover;border-radius:6px">'
                       f'</a>')
        rows += (
            "<tr>"
            f'<td style="padding:8px;color:#888">{i}</td>'
            f'<td style="padding:8px">{img_tag}</td>'
            f'<td style="padding:8px"><a href="{_esc(abs_url(cfg, m.url))}" '
            f'style="color:#1a73e8;font-weight:600">{_esc(m.name)}</a></td>'
            f'<td style="padding:8px"><span style="background:#1a73e8;color:#fff;'
            f'padding:2px 8px;border-radius:4px;font-size:12px">{_esc(m.quality)}</span></td>'
            "</tr>"
        )

    diff_html = ""
    if diff is not None:
        blocks = []
        for label, rows_html, color in (
            ("新增", "".join(
                f'<li>+ <b>{_esc(m.name)}</b> <span style="color:#1a73e8">[{_esc(m.quality)}]</span> '
                f'<a href="{_esc(abs_url(cfg, m.url))}">{_esc(abs_url(cfg, m.url))}</a></li>' for m in diff["added"]), "#e8f5e9"),
            ("下架", "".join(
                f'<li>- <b>{_esc(m.name)}</b> <span style="color:#d93025">[{_esc(m.quality)}]</span> '
                f'<a href="{_esc(abs_url(cfg, m.url))}">{_esc(abs_url(cfg, m.url))}</a></li>' for m in diff["removed"]), "#fce8e6"),
        ):
            if rows_html:
                blocks.append(f'<div style="background:{color};padding:10px;margin:8px 0">'
                              f'<b>{label}</b><ul style="margin:6px 0 0 18px;padding:0">{rows_html}</ul></div>')
        qc = "".join(
            f'<li>~ <b>{_esc(n.name)}</b>：{_esc(o.quality)} → <b>{_esc(n.quality)}</b></li>'
            for o, n in diff["quality_changed"])
        if qc:
            blocks.append('<div style="background:#e8f0fe;padding:10px;margin:8px 0"><b>画质变化</b>'
                          f'<ul style="margin:6px 0 0 18px;padding:0">{qc}</ul></div>')
        nc = "".join(
            f'<li>~ 片名：{_esc(o.name)} → <b>{_esc(n.name)}</b></li>'
            for o, n in diff["name_changed"])
        if nc:
            blocks.append('<div style="background:#fff3e0;padding:10px;margin:8px 0"><b>片名变化</b>'
                          f'<ul style="margin:6px 0 0 18px;padding:0">{nc}</ul></div>')
        if diff["order_changed"] and not has_content_change(diff):
            blocks.append('<div style="background:#f1f3f4;padding:10px;margin:8px 0">'
                          '<b>顺序变化</b>（内容相同，未触发内容变更）</div>')
        if blocks:
            diff_html = ('<div style="font-size:14px">' + "".join(blocks) + "</div>")
        if note:
            diff_html += f'<p style="color:#888;font-size:12px">{_esc(note)}</p>'

    html_body = f"""\
<!DOCTYPE html><html><body style="font-family:-apple-system,'Helvetica Neue',Arial,'Microsoft YaHei',sans-serif;
color:#202124;max-width:680px;margin:0 auto;padding:16px">
<h2 style="margin-bottom:4px">电影推荐列表监控</h2>
<p style="color:#5f6368;margin:0 0 12px">{when}　·　<a href="{_esc(cfg['url'])}">www.pkmkv.com</a>
　·　共 {len(items)} 项</p>
{diff_html}
<table style="width:100%;border-collapse:collapse;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.1)">
<thead><tr style="background:#f1f3f4">
<th style="text-align:left;padding:10px;width:30px">#</th>
<th style="text-align:left;padding:10px;width:110px">封面</th>
<th style="text-align:left;padding:10px">片名</th>
<th style="text-align:left;padding:10px;width:70px">资质</th></tr></thead>
<tbody>{rows}</tbody></table>
<p style="color:#9aa0a6;font-size:12px;margin-top:14px">由 movie_notify.py 自动生成</p>
</body></html>"""

    return subject, text, html_body


def send_email(cfg: Dict[str, Any], subject: str, text: str, html_body: str) -> Tuple[bool, str]:
    """通过 Resend API 发送。返回 (是否成功, 说明)。"""
    if cfg.get("dry_run") or not cfg.get("resend_api_key"):
        preview = json.dumps({
            "to": cfg["to"], "from": cfg["from"], "subject": subject,
            "text_len": len(text), "html_len": len(html_body),
        }, ensure_ascii=False)
        LOG.info("[dry-run] 不实际发送。请求体预览：%s", preview)
        print(f"[dry-run] 未发送邮件：{preview}")
        return True, "dry-run（未实际发送）"

    payload = {
        "from": cfg["from"],
        "to": cfg["to"],
        "subject": subject,
        "text": text,
        "html": html_body,
    }
    headers = {
        "Authorization": f"Bearer {cfg['resend_api_key']}",
        "Content-Type": "application/json",
    }
    url = cfg.get("resend_base_url", "https://api.resend.com/emails")
    try:
        if requests is not None:
            r = requests.post(url, json=payload, headers=headers, timeout=30)
        else:
            import urllib.request
            req = urllib.request.Request(
                url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=headers, method="POST")
            r = urllib.request.urlopen(req, timeout=30)
            body = r.read().decode("utf-8", errors="replace")
            status = r.status if hasattr(r, "status") else r.getcode()
            if status < 400:
                return True, f"已发送：{body[:200]}"
            return False, f"Resend 返回 {status}：{body[:500]}"
        body = r.text
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            data = {}
        if r.status_code < 400 and data.get("id"):
            return True, f"已发送，Resend id={data.get('id')}"
        return False, f"Resend 返回 {r.status_code}：{body[:500]}"
    except Exception as exc:  # noqa: BLE001
        return False, f"发送异常：{exc}"


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


LOG = logging.getLogger("movie_notify")


def setup_logging(cfg: Dict[str, Any]) -> None:
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    lp = BASE_DIR / cfg["log_file"]
    try:
        lp.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            lp, maxBytes=int(cfg["log_max_bytes"]), backupCount=int(cfg["log_backup_count"]),
            encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"无法写入日志文件 {lp}：{exc}", file=sys.stderr)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def load_config(config_path: Path = DEFAULT_CONFIG_PATH) -> Dict[str, Any]:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if config_path.exists():
        try:
            user_cfg = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
            cfg.update(user_cfg)
            LOG.debug("已合并 config.json：%s", list(user_cfg))
        except Exception as exc:  # noqa: BLE001
            print(f"config.json 解析失败，忽略该文件：{exc}", file=sys.stderr)
    # 环境变量覆盖
    for env, key in ENV_MAP.items():
        val = os.environ.get(env)
        if val is None or val == "":
            continue
        if key in ("count",):
            cfg[key] = int(val)
        elif key == "to":
            cfg[key] = [x.strip() for x in val.split(",") if x.strip()]
        elif key == "schedule_hours":
            cfg[key] = [int(x.strip()) % 24 for x in val.split(",") if x.strip()]
        elif key in ("dry_run",):
            cfg[key] = val.lower() in ("1", "true", "yes", "on")
        else:
            cfg[key] = val
    # 相对路径统一落到 BASE_DIR
    for k in ("state_file", "history_file", "log_file"):
        p = cfg[k]
        if not Path(p).is_absolute():
            cfg[k] = str((BASE_DIR / p).resolve())
    return cfg


def next_run(now: datetime, hours: Sequence[int], tz: timezone) -> datetime:
    """返回 tz 时区里下一个到达的调度时刻。"""
    candidates = []
    for h in {int(x) % 24 for x in hours}:
        for base in (now, now + timedelta(days=1)):
            cand = base.replace(hour=h, minute=0, second=0, microsecond=0)
            candidates.append(cand)
    future = [c for c in candidates if c > now]
    return min(future)


_STOP = False
_STOP_EVENT = threading.Event()   # 用于立即唤醒调度等待


def _sig_handler(signum, frame):  # noqa: ARG001
    global _STOP
    LOG.info("收到信号 %s，准备退出…", signum)
    _STOP = True
    _STOP_EVENT.set()


def run_once(cfg: Dict[str, Any], force_baseline: bool = False,
             test_email: bool = False) -> int:
    """执行一次抓取 + 对比 + 通知。返回退出码。"""
    tz = ZoneInfo(cfg.get("tz", "Asia/Shanghai"))
    now = datetime.now(tz)
    LOG.info("=" * 60)
    LOG.info("开始抓取 %s （%s）", cfg["url"], now.isoformat(timespec="seconds"))

    # ---- 测试邮件模式 ----
    if test_email:
        subject, text, html_body = build_email(
            cfg, [], None, now, note="这是一封测试邮件，用于验证 Resend 配置是否正常。")
        subject = f"{cfg.get('subject_prefix', '[pkmkv 更新]')}测试邮件"
        ok, msg = send_email(cfg, subject, text, html_body)
        LOG.info("测试邮件结果：%s", msg)
        return 0 if ok else 1

    # ---- 抓取 ----
    try:
        html_text = http_get(cfg["url"], cfg)
        root = parse_html(html_text)
        items = extract_movies(root, int(cfg["count"]))
    except Exception as exc:  # noqa: BLE001
        LOG.error("抓取/解析失败：%s", exc)
        return 1

    LOG.info("抓到 %d 项：", len(items))
    for i, m in enumerate(items, 1):
        LOG.info("  %d. %-22s [%s] %s", i, m.name, m.quality, m.url)

    prev_state = load_state(cfg)
    prev_items = [Movie(**{k: v for k, v in d.items() if k in Movie.__dataclass_fields__})
                  for d in (prev_state or {}).get("items", [])] if prev_state else []
    prev_ts = (prev_state or {}).get("updated_at", "无")

    # ---- 对比 ----
    cur_fp = hashlib.sha1("|".join(m.fingerprint() for m in items).encode()).hexdigest()[:12]
    if not prev_items:
        LOG.info("首次运行，记录基线快照，不发送提醒邮件。")
        diff = None
    else:
        diff = diff_movies(prev_items, items)
        LOG.info("上次快照时间：%s", prev_ts)
        if not has_content_change(diff):
            LOG.info("内容无变化%s", "（仅顺序变化）" if diff["order_changed"] else "。")
        else:
            LOG.info("检测到变化：新增 %d / 下架 %d / 画质 %d / 片名 %d",
                     len(diff["added"]), len(diff["removed"]),
                     len(diff["quality_changed"]), len(diff["name_changed"]))

    # ---- 是否需要通知 ----
    should_notify = (
        force_baseline is False
        and diff is not None
        and (has_content_change(diff) or (diff["order_changed"] and cfg.get("notify_on_order_change")))
    )
    # 同一份内容不要重复发（避免来回抖动刷邮件）
    last_notified = (prev_state or {}).get("last_notified_fp")
    if should_notify and cur_fp == last_notified:
        LOG.info("该内容指纹已通知过，跳过发送。")
        should_notify = False

    note = None
    if force_baseline:
        note = "本次为人工强制刷新基线（--baseline），不代表网站有更新。"
        should_notify = False
        LOG.info("强制刷新基线模式，不发送邮件。")

    # ---- 发邮件 ----
    if should_notify:
        subject, text, html_body = build_email(cfg, items, diff, now, note)
        LOG.info("发送提醒邮件：%s", subject)
        ok, msg = send_email(cfg, subject, text, html_body)
        if ok:
            LOG.info("邮件发送成功：%s", msg)
        else:
            LOG.error("邮件发送失败：%s", msg)
    else:
        LOG.info("本次不需要发送提醒邮件。")

    # ---- 落盘 ----
    save_state(cfg, items, now)
    append_history(cfg, items, now)
    # 记录已通知指纹（只有真正发出去才记录，方便下次判断）
    if should_notify:
        st = load_state(cfg) or {}
        st["last_notified_fp"] = cur_fp
        st["last_notified_at"] = now.isoformat(timespec="seconds")
        p = BASE_DIR / cfg["state_file"]
        p.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    LOG.info("快照已写入 %s", BASE_DIR / cfg["state_file"])
    LOG.info("本次内容指纹：%s", cur_fp)
    return 0


def _apply_config(cfg: Dict[str, Any], config_path: Path) -> Tuple[str, str, List[int]]:
    """把 config.json 重新读进 cfg（原地更新）。

    返回 (sig, tz_name, hours)：sig 是配置指纹，用来判断配置有没有变化。
    读文件失败时不动 cfg，沿用现有值。
    """
    try:
        fresh = load_config(config_path)
        cfg.clear()
        cfg.update(fresh)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("重新加载配置失败，沿用现有配置：%s", exc)
    tz_name = str(cfg.get("tz", "Asia/Shanghai"))
    hours = sorted({int(h) % 24 for h in cfg.get("schedule_hours") or []})
    sig = "|".join([
        tz_name,
        ",".join(str(h) for h in hours),
        str(cfg.get("resend_api_key") or ""),
        str(cfg.get("url")),
        ",".join(cfg.get("to") or []),
        str(cfg.get("from")),
    ])
    return sig, tz_name, hours


def _log_cfg_state(cfg: Dict[str, Any], sig: str, tz_name: str, hours: List[int],
                   first: bool) -> None:
    """打印配置概要，配置变化时也能看出来。"""
    LOG.info("守护进程%s：时区=%s 调度时刻=%s 目标=%s 发件=%s 收件=%s Key=%s",
             "启动" if first else "配置已更新（自动热重载，无需重启）",
             tz_name,
             "、".join(f"{h:02d}:00" for h in hours),
             cfg.get("url"), cfg.get("from"),
             ",".join(cfg.get("to") or []),
             "已配置" if cfg.get("resend_api_key") else "未配置(走 dry-run)")


def run_daemon(cfg: Dict[str, Any],
               config_path: Path = DEFAULT_CONFIG_PATH) -> int:
    """常驻守护进程。

    每 5 秒重新读一次 config.json，所以改 API Key / 时间点 / 时区 / 收件人后
    不需要重启服务，几秒内自动生效。
    """
    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)
    sig, tz_name, hours = _apply_config(cfg, config_path)
    _log_cfg_state(cfg, sig, tz_name, hours, first=True)
    while not _STOP:
        tz = ZoneInfo(tz_name)
        now = datetime.now(tz)
        nxt = next_run(now, hours, tz)
        wait_s = (nxt - now).total_seconds()
        LOG.info("下次执行时间：%s（%.1f 分钟后）", nxt.strftime("%Y-%m-%d %H:%M %Z"), wait_s / 60)
        # ---- 等待到下一个调度时刻，期间每 5 秒热重载一次配置 ----
        while not _STOP:
            if datetime.now(tz) >= nxt:
                break
            _STOP_EVENT.wait(min(5.0, max(0.1, (nxt - datetime.now(tz)).total_seconds())))
            if _STOP:
                break
            new_sig, new_tz, new_hours = _apply_config(cfg, config_path)
            if new_sig != sig:
                sig, tz_name, hours = new_sig, new_tz, new_hours
                tz = ZoneInfo(tz_name)
                _log_cfg_state(cfg, sig, tz_name, hours, first=False)
                nxt = next_run(datetime.now(tz), hours, tz)
                LOG.info("下次执行时间更新为：%s", nxt.strftime("%Y-%m-%d %H:%M %Z"))
        if _STOP:
            break
        LOG.info("定时触发。")
        # 触发前再读一次，保证用到最新配置（Key / 收件人 / count 等）
        _apply_config(cfg, config_path)
        run_once(cfg)
        if _STOP:
            break
        # 执行完小睡一会，避免同一分钟被反复触发
        _STOP_EVENT.wait(65)
    LOG.info("守护进程已退出。")
    return 0


def show_status(cfg: Dict[str, Any]) -> int:
    st = load_state(cfg)
    if not st:
        print("尚无快照。")
        return 0
    print(json.dumps(st, ensure_ascii=False, indent=2))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="pkmkv.com 电影推荐列表监控 + Resend 邮件提醒")
    ap.add_argument("--once", action="store_true", help="只执行一次抓取后立即退出")
    ap.add_argument("--baseline", action="store_true", help="强制刷新基线快照，不发邮件")
    ap.add_argument("--test-email", action="store_true", help="发送测试邮件验证 Resend 配置")
    ap.add_argument("--status", action="store_true", help="打印当前快照内容")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="配置文件路径")
    ap.add_argument("--verbose", "-v", action="store_true", help="输出 DEBUG 日志")
    args = ap.parse_args(argv)

    cfg = load_config(Path(args.config).resolve() if args.config else DEFAULT_CONFIG_PATH)
    setup_logging(cfg)
    if args.verbose:
        LOG.setLevel(logging.DEBUG)
    if not cfg.get("resend_api_key") and not cfg.get("dry_run"):
        LOG.warning("未配置 resend_api_key 且 dry_run=false：邮件将被跳过。"
                    "请设置环境变量 RESEND_API_KEY 或在 config.json 填写 resend_api_key。")

    if args.status:
        return show_status(cfg)
    if args.test_email:
        return run_once(cfg, test_email=True)
    if args.baseline:
        return run_once(cfg, force_baseline=True)
    if args.once:
        return run_once(cfg)
    return run_daemon(cfg, Path(args.config).resolve() if args.config else DEFAULT_CONFIG_PATH)


if __name__ == "__main__":
    sys.exit(main())
