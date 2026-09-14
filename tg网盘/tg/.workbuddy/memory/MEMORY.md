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
├── tools/{rebuild_index.py, backup_index.py, clean_cache.py, show_password.py, test_search_logic.py}
└── tgpool.service
```
文档（README / DEPLOY / DISASTER / REBUILD）**不在包内**，只存在于本机仓库。

打包命令（在临时目录里做，避免把 __pycache__ 和多余目录带进包）：

```bash
D=$(mktemp -d)/tgpool-deploy && mkdir -p "$D"
cp -r deploy/app deploy/tools deploy/deploy.sh deploy/tgpool.service "$D"/
find "$D" -name __pycache__ -type d -exec rm -rf {} +
chmod 755 "$D/deploy.sh"
(cd "$(dirname "$D")" && tar czf tgpool-deploy.tar.gz tgpool-deploy)
```

## 本地工具链

- SSH：`ssh_run.py`（执行命令）/ `ssh_put.py`（二进制安全上传）/ `ssh_upload.py`（文本，会规范化换行）。
- Python：`C:\Users\Jrafina\.workbuddy\binaries\python\envs\tg-server\Scripts\python.exe`（含 paramiko；**不含 fastapi**）。
- **测试用 venv：`C:\Users\Jrafina\.workbuddy\binaries\python\envs\tgtest\Scripts\python.exe`**
  （含 fastapi 0.141 / httpx / python-multipart）。**逻辑测试本地就能跑，不必上服务器**：
  `cd <副本>/tools && APP_DIR=<副本> .../envs/tgtest/Scripts/python.exe test_search_logic.py`
  （`APP_DIR` 指到含 app.py 的目录；脚本同目录要有 `rebuild_index.py`，测试会 import 它做重放校验）。
- 服务器 venv：`/opt/tgpool/venv/bin/python`（Python 3.10.12，含 fastapi/httpx）。
- ⚠️ 2026-09-14：`ssh_run.py` 里的 root 密码被服务器拒绝（`AuthenticationException`），
  22/8443 端口都通、站点仍是我们这套（`/static/index.html` md5 `589afca3…` 与本机一致）。
  **根因未定，需要用户给当前 root 凭据**。改密码前先确认 ssh_run.py 是否过期。

## 关键实测数字

- 下载 TTFB ≈ 总耗时（`getFile` 阻塞至整份落盘）；预热后 TTFB 0.014s。
- 服务器上行带宽 ~580 KB/s；备份耗时 ≈ 索引体积线性（9000 文件 ~3s）。
- 上传不占服务器磁盘，下载占（`documents/`）；清理用 `clean_cache.py`。

## 聊天命令层（v1.5→v1.9，`app.py` 内）

- 在 Telegram 里可用：`/search`（模糊搜索：文件名或**所在路径**包含即命中，网页与 bot 同一套 `search_files()`）、`/backups`（云端备份包列出/取回，读 remote.jsonl，不进索引）、`/ls`、`/get`、`/stats`、`/help`、
  **`/rm`（删文件/删目录，删目录先弹确认按钮，`-f` 跳过、`-file`/`-dir` 消歧、`#编号` 按 id 删）**、
  **`/move`（移动文件/目录，目标可留空=根目录，路径含空格用引号）**、**`/pass`（聊天里查网页账号密码）**；
  **直接把文件发给 bot 即自动收录**。
- 长轮询 `getUpdates`，不依赖公网回调；`offset` 存 `/opt/tgpool/tg_offset.json`。
- 取回文件用 **`copyMessage`（零带宽）**，失败回退 `sendDocument(file_id)`；
  反向上传**根本不传输字节**（文件已在 Telegram 上），故无大小限制。
- 白名单：`TG_CHAT_ID` + `TG_BOT_ADMIN_IDS`；`TG_BOT_POLL=0` 可整体关闭；`TG_BOT_SHOW_PASS=0` 关 `/pass`。
- 诊断：`GET /api/bot/status`（看 `token_ok` / `last_poll` / `handled` / `getme`）。
- ⚠️ **`rebuild_index.py` 只认 journal、不认 caption**。任何新的入库入口都必须写
  `journal/index.jsonl` 且字段与网页上传一致（含 `path`），目录自动创建要逐级写 `mkdir` 事件。
- ⚠️ **journal 的 `path` 一律是「目录路径」**：`add`/`mv` 记的是**所在目录**（不是文件全路径！），
  `mvd` 记 `{path: 旧目录, new: 新目录}`，`rmd`/`del` 见 `rebuild_index.replay()`。
  写错了不会报错，只会静默重建出一棵错目录树（2026-09-14 `/move` 踩过，被测试的「重放==现库」断言抓住）。
- 关键代码位置：`_bot_poll_loop` / `_handle_message` / `_handle_file_message` /
  `_incoming_file` / `_send_file` / `_reply_rm` / `_reply_move` / `_do_delete_folder` /
  `_tg_delete_messages` / `_resolve_node` / `_split_args`。
- 自测：`APP_DIR=<副本目录> <venv>/python tools/test_search_logic.py`
  （**196 项**，临时库 + 打桩 `_tg`，不联网；含 journal 重放一致性、网页删除接口回归、脚本测试）。
- 删 Telegram 消息走 `_tg_delete_messages()`：`deleteMessages` 批量（每批 100 条），
  失败退回逐条 `deleteMessage`；按每行自己的 `chat_id` 分组（跨会话收录的文件原消息不在池子会话里）。
- 找回/重设密码：`tools/show_password.py`（纯标准库，读 `tgpool.env`；`--token` / `--json` / `--reset --yes`）；
  聊天里 `/pass` 是同一信息的出口。

## 踩坑速查

- `Content-Disposition` 塞中文会 500 → 用 ASCII 兜底名 + `filename*=UTF-8''`。
- logrotate 需要显式 `su root root`，否则静默跳过。
- `telegram-bot-api` 的 systemd 单元是 inactive 属**正常**，bot API 实际跑在 docker 容器 `tg-bot-api`（127.0.0.1:8081）。
- 服务端路由是 `/api/list`、`/api/stats`（**不是** `/api/files`）；均需 Basic auth。
- `api.telegram.org` 偶发返回 `401 Unauthorized`（2026-09-11 出现过一次，次日自愈）。
  **单次 401 不足以判定 token 失效**——必须以 `sendMessage`/`getMe` 能成功为准再下结论。
