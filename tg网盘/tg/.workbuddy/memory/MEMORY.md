# 项目长期记忆 · TG 存储池

> 跨会话的稳定事实与约定。日常细节写入 `YYYY-MM-DD.md`。

## 项目身份

- 目标：用 Telegram 当网盘——自建 Bot API（`--local`）+ FastAPI，本地 SQLite 当索引，Telegram 私聊当存储本体。
- 源码目录 `C:\Users\Jrafina\Desktop\tg\`：`server/`（工作副本）→ `deploy/`（打包副本）→ `tgpool-deploy.tar.gz`。
- 服务器 `82.158.224.64`，应用装在 `/opt/tgpool/`。
- Bot：@storage_pool_bot（名称「存储池」），`chat_id 8575978784` 即用户本人私聊——**存储池就是用户与 bot 的私聊**。

## 铁律（改代码前必读）

1. **改完必须同步 `server/` → `deploy/`**，两者 md5 必须一致，再重新打包 tar.gz。
2. **打包顺序**：所有改动做完 → 最后打包 → 再验证。先打包后改脚本会造成"测试假失败"。
3. **长轮询必须单进程**。`tgpool.service` 必须保持 `--workers 1`，否则同一批 update 被多 worker 重复处理、回复重复。
4. 每次部署后校验 `md5sum /opt/tgpool/app/app.py` 与本地一致；原文件留 `app.py.bak-<用途>-<日期>`。
5. `.workbuddy/` 是项目数据目录，**不可删除**。

## 部署包结构

```
tgpool-deploy/
├── deploy.sh          一键部署（含 preflight 文件清单 + step_files 拷贝清单，两处都要加文件）
├── app/{app.py, requirements.txt, static/{index.html, background.jpg}}
├── tools/{rebuild_index.py, backup_index.py, clean_cache.py, test_search_logic.py}
└── tgpool.service
```
文档（README / DEPLOY / DISASTER / REBUILD）**不在包内**，只存在于本机仓库。

## 本地工具链

- SSH：`ssh_run.py`（执行命令）/ `ssh_put.py`（二进制安全上传）/ `ssh_upload.py`（文本，会规范化换行）。
- Python：`C:\Users\Jrafina\.workbuddy\binaries\python\envs\tg-server\Scripts\python.exe`（含 paramiko；**不含 fastapi**）。
- 服务器 venv：`/opt/tgpool/venv/bin/python`（Python 3.10.12，含 fastapi/httpx）。
- 因本地无 fastapi，**逻辑测试一律在服务器上跑**（上传到 `/tmp/tgtest/`，不碰生产）。

## 关键实测数字

- 下载 TTFB ≈ 总耗时（`getFile` 阻塞至整份落盘）；预热后 TTFB 0.014s。
- 服务器上行带宽 ~580 KB/s；备份耗时 ≈ 索引体积线性（9000 文件 ~3s）。
- 上传不占服务器磁盘，下载占（`documents/`）；清理用 `clean_cache.py`。

## 聊天命令层（v1.5/v1.6，`app.py` 内）

- 在 Telegram 里可用：`/search`（模糊搜索：文件名或**所在路径**包含即命中，网页与 bot 同一套 `search_files()`）、`/backups`（云端备份包列出/取回，读 remote.jsonl，不进索引）、`/ls`、`/get`、`/stats`、`/help`；
  **直接把文件发给 bot 即自动收录**。
- 长轮询 `getUpdates`，不依赖公网回调；`offset` 存 `/opt/tgpool/tg_offset.json`。
- 取回文件用 **`copyMessage`（零带宽）**，失败回退 `sendDocument(file_id)`；
  反向上传**根本不传输字节**（文件已在 Telegram 上），故无大小限制。
- 白名单：`TG_CHAT_ID` + `TG_BOT_ADMIN_IDS`；`TG_BOT_POLL=0` 可整体关闭。
- 诊断：`GET /api/bot/status`（看 `token_ok` / `last_poll` / `handled` / `getme`）。
- ⚠️ **`rebuild_index.py` 只认 journal、不认 caption**。任何新的入库入口都必须写
  `journal/index.jsonl` 且字段与网页上传一致（含 `path`），目录自动创建要逐级写 `mkdir` 事件。
- 关键代码位置：`_bot_poll_loop` / `_handle_message` / `_handle_file_message` /
  `_incoming_file` / `_collect_hits` / `_send_file`。
- 自测：`APP_DIR=/opt/tgpool/app /opt/tgpool/venv/bin/python tools/test_search_logic.py`
  （93 项，临时库 + 打桩 `_tg`，不联网、不需要有效 token）。

## 踩坑速查

- `Content-Disposition` 塞中文会 500 → 用 ASCII 兜底名 + `filename*=UTF-8''`。
- logrotate 需要显式 `su root root`，否则静默跳过。
- `telegram-bot-api` 的 systemd 单元是 inactive 属**正常**，bot API 实际跑在 docker 容器 `tg-bot-api`（127.0.0.1:8081）。
- 服务端路由是 `/api/list`、`/api/stats`（**不是** `/api/files`）；均需 Basic auth。
- `api.telegram.org` 偶发返回 `401 Unauthorized`（2026-09-11 出现过一次，次日自愈）。
  **单次 401 不足以判定 token 失效**——必须以 `sendMessage`/`getMe` 能成功为准再下结论。
