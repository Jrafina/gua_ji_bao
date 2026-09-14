# TG 存储池（Telegram Storage Pool）

用 Telegram 当**备份存储池**：文件本体存在 Telegram 云端，服务器只做中转和索引。
配有一个网页前端，可直接上传/下载/整理文件夹，**单文件最大 2GB**，下载支持断点续传。

> ⚠️ 这不是"虚拟硬盘"。Telegram Bot API 无法做随机读取，所以做不了挂载盘符。
> 本项目的定位是**存储池 + 网页存取**，不是把 Telegram 挂成 Z 盘。

---

## 快速访问

| 项 | 值 |
|---|---|
| 网址 | **https://IP:8443/** |
| 用户名 | `admin` |
| 密码 | `PGEHzEbbqLpwKlj3` |

**三个必须注意的点：**

1. **不要走 `example.com` 的小黄云（Cloudflare 代理）**。该域名目前解析到 Cloudflare 的 IP，
   而 Cloudflare **连 8443 端口一起代理**（实测 `https://example.com:8443/` 响应头是 `server: cloudflare`），
   免费版会拦截超过 **100MB 的上传**，而我们的需求是 2GB。
   → **解决办法**：把该 DNS 记录改成**灰云（DNS only）**，域名就直连服务器，不再有 100MB 限制，
   访问方式与 IP 完全一致。详见「踩坑记录 #3」。
2. 在切成灰云之前，访问请用 **IP + 8443**：`https://IP:8443/`
3. 证书是**自签名**的，浏览器会提示"不安全" → 点「高级」→「继续访问」。
   流量是加密的，只是证书没有被公共 CA 签发。

> 🔒 **密码请勿公开分享**。所有敏感凭据都存在服务器 `/opt/tgpool/tgpool.env`（权限 600）。

---

## 文件夹功能

网页支持多级文件夹，操作方式：

| 操作 | 方式 |
|---|---|
| 进入文件夹 | 点文件夹名，或用顶部**面包屑**回退 |
| 新建文件夹 | 点「新建文件夹」按钮，创建在**当前目录**下 |
| 上传到指定目录 | 先进目录，再点「上传文件」或直接拖拽文件到页面 |
| 移动文件 | 把文件行**拖到目标文件夹行**；或点「移动」选目录 |
| 移动文件夹 | 点文件夹的「移动」（不能移到自己或自己的子目录） |
| 重命名 | 点文件夹的「重命名」 |
| 删除文件夹 | 非空文件夹会**连同内容一起从 Telegram 删除**，有二次确认 |
| 搜索 | 顶部搜索框跨所有目录做**包含式模糊搜索**：文件名或所在路径含关键词即命中，关键词在文件名/路径中高亮，所在路径可点击直达 |

**实现方式（重要）**：目录结构是**虚拟的**，只存在服务器 SQLite（`folders` 表 + `files.folder_id`）。
Telegram 侧依然是**扁平存储**，所有文件都在同一个会话里，文件夹不产生任何额外的 Telegram 消息或上传。

> 目录结构既然只在本地，就有丢失风险 —— 见下面的 **索引灾备** 章节，已有三层保护。
> 另外：上传时会在 Telegram 侧给消息附一条 `TGPOOL /路径` 的 caption，让云端消息自带目录信息。

**两个约束**

- **同一目录下不允许同名文件夹**（保证路径唯一，否则灾备重建时同名目录会被合并）。
  同名**文件**不受限制。
- 灾备重建后，**文件 id 保持不变**（下载链接照旧有效），但**文件夹 id 会重新分配**
  （目录 id 仅内部使用，不影响任何持久化链接）。

主要接口：

```
GET    /api/list?folder_id=&q=     列目录 / 跨目录搜索
GET    /api/folders/tree           全部文件夹扁平树（用于移动选择器）
POST   /api/folders                新建 {name, parent_id}
PATCH  /api/folders/{id}           重命名 / 移动 {name?, parent_id?}
DELETE /api/folders/{id}?force=1   删除（非空必须 force）
POST   /api/upload                 表单 file + folder_id
PATCH  /api/files/{id}             移动文件 {folder_id}
DELETE /api/files/{id}             删除文件
POST   /api/backup                 立即备份索引（本地 + Telegram）
POST   /api/download/{id}/prepare  把文件拉到服务器本地（下载前的预热，幂等）
GET    /api/download/{id}/progress 查询预热进度（字节/百分比/速度/剩余时间）
GET    /api/download/{id}          下载（支持 Range 断点续传）
GET    /api/cache                  服务器本地缓存统计
POST   /api/cache/clean            清理缓存 ?mode=days|max_gb|all
GET    /api/bot/status             机器人命令诊断（token 是否有效、轮询心跳、offset）
```

---

## 聊天命令（在 Telegram 里 /search 查文件）

不用打开网页，直接给 @storage_pool_bot 发命令就能查池子、整理目录。

```
/search 关键词   模糊搜索：文件名或所在路径包含关键词即命中
/ls [路径]       列出目录内容，省略路径则列根目录
/get 编号        按编号取回文件
/move 源 目标    移动文件或文件夹，目标留空 = 挪到根目录
/rm 路径         删除文件；删文件夹先确认再动手
/backups         列出云端索引备份包，点编号即可取回
/stats           池子统计
/pass            忘记网页密码时把账号密码捞回来
/help            命令帮助
```

搜索结果列出发送者可读的编号、大小、完整路径与索引 id，下方是对应的数字按钮：

```
搜索「报表」命中 14 个文件（第 1/2 页）

1. 财务报表.pdf
    2.1 MB · /工作/2026 · #18
2. 报表模板.docx
    12.0 KB · / · #24
```

每个结果带一个数字按钮，点一下文件就回到对话里；超过 8 条时分页，
第 2 页起用「上一页 / 下一页」**原地改写**同一张消息（`editMessageText`），不刷屏。

**搜索规则（网页与 bot 完全同一套）**

- **包含即命中**：文件名或所在路径包含关键词就显示，大小写不敏感；
- **多关键词**：空格分隔，要求全部命中，且可以分别落在文件名和路径上
  （`/search 2026 财务` 能找到 `/工作/2026/财务报表.pdf`）；
- **目录名命中连带子文件**：目录在路径里，搜目录名（或片段）即列出其下全部文件；
- **`%` 和 `_` 按字面量匹配**，不是 SQL 通配符（搜 `2026_09` 不会误中 `2026-09`）；
- **相关性排序**：文件名全等 > 名称前缀 > 名称包含 > 仅路径包含，同级新的靠前；
- 命中超过 500 个（网页）/ 200 个（bot）时只显示最新的，并提示换更具体的关键词。

**`/backups`：云端备份包的查看与取回**

每日 03:30 的自动备份（和网页「备份索引」按钮）会把 `tgpool-backup-*.tar.gz` 上传到
Telegram 会话作为**异地灾备副本**。这些包是基础设施，**刻意不进池子索引**——否则在网页或
bot 里删它等于删掉异地副本。`/backups` 读备份注册表（`backup/remote.jsonl`）列出最近 14 份，
点编号即用 `copyMessage` 零带宽把备份包复制回对话；拿到包后按灾备文档恢复即可。

### `/rm`：在聊天里删文件 / 删目录

```
/rm /工作/2026/报表.pdf    删一个文件
/rm /工作/2026             删整个文件夹（含里面所有文件）
/rm #1234                  按编号删（编号来自 /ls、/search 的 #数字）
/rm -f /工作/2026          跳过确认直接删
/rm -file /同名            只删文件（同名文件和文件夹并存时才需要）
```

- **删文件夹一定先确认**：bot 会把目录路径、子目录数、文件数、总容量列出来，点「确认删除」才动手
  （按钮 10 分钟内有效、点一次就失效）。手滑发错路径不会直接毁数据。
- 删的是**真删除**：Telegram 上的原消息和索引一起清掉，无法找回。所以宁可多点一下。
- 根目录不可删；同名文件和文件夹同时存在时默认按**文件夹**处理，回复里会写明，要删文件加 `-file`。
- 删除同样写 journal（`del` / `rmd` 事件），与网页端删除完全一致——**索引依然可以从日志完整重建**。
- 路径里含空格就用引号包起来：`/rm "/我的 报告/最终版.docx"`。

### `/move`：在聊天里挪文件 / 挪目录

```
/move /工作/2026 /归档      把 /工作/2026 挪到 /归档 下
/move /报表模板.docx /模板   移动单个文件
/move "/我的 报告"           只写源路径 = 挪到根目录
```

- 源可以是文件或文件夹，**目标目录不存在会自动逐级创建**（和发文件时写路径的规矩一致）；
- 拒绝把文件夹挪进它自己或它的子目录（循环）；
- 同层已有同名文件夹时会拒绝并提示，不会静默覆盖；
- 移动写 journal（`mvd` / `mv` 事件），重建索引后目录结构照样一致。

### `/pass`：忘记网页密码

```
/pass   →  外网地址 / 本机地址 / 账号 / 密码
```

密码存在服务器 `/opt/tgpool/tgpool.env`（权限 600），只有 root 能读，也不在备份包里。
忘了密码有三条路，任选：

1. Telegram 里发 `/pass`（消息里带一个「删掉这条消息」按钮，看完可以直接清掉）；
2. 服务器上执行 `python3 /opt/tgpool/tools/show_password.py`
   （后端了也能用；`--token` 连 bot token 一起看，`--json` 给脚本用）；
3. 干脆换一个：`python3 /opt/tgpool/tools/show_password.py --reset --yes`
   （原文件自动备份成 `tgpool.env.bak-<时间>`，改完 `systemctl restart tgpool` 生效）。

介意密码出现在聊天记录里的话，在 `tgpool.env` 里设 `TG_BOT_SHOW_PASS=0` 关掉 `/pass`。

### 直接发文件给 bot 即可收录（反向上传）

把文件发给 @storage_pool_bot 就收录进池子了——手机、桌面端都行，一次多选也可以。

**服务器不传任何字节**：文件发到你与 bot 的私聊时，它已经在 Telegram 上了。
所谓"上传"在这里只是登记一条索引（文件名 / 大小 / `file_id` / `message_id` / 路径）再写一条 journal。
于是：**没有大小限制**（既不撞 2GB 上传上限，也不受 20MB 下载上限影响）、**不用等**、**不耗带宽**。

| 你发的 | 结果 |
|---|---|
| 文件，无说明 | 收进 `/收件箱`，并给该消息补一行 `TGPOOL /收件箱` 说明 |
| 文件 + 说明写 `/工作/2026` | 收进该目录；目录不存在会**自动逐级创建** |
| 同一个文件再发一次 | 提示"已经在池子里了"，不重复收录（按 `file_unique_id` 去重） |
| 文件 + 已有说明 | **不覆盖你的说明**；`TGPOOL` 标记只在消息本来没说明时才补 |

支持 `document` / `video` / `audio` / `animation` / `voice` / `video_note` / `photo`
（`photo` 取最大尺寸那份；`sticker` 不算文件，跳过）。收录后立刻能被 `/search` 搜到、能点按钮取回，
网页上同样可见。

### 为什么取回文件几乎不花时间、也不耗带宽

走 `copyMessage`：Telegram 直接把原消息复制一份到对话，**字节完全不动**。

| 操作 | 走谁的网络 | 服务器带宽 |
|------|-----------|-----------|
| 网页上传 / 下载 | 服务器中转 | 消耗 |
| 聊天里发文件收录 | 根本不传输（文件已在 Telegram 上） | **零** |
| 聊天里搜索并取回 | Telegram 内部复制 | **零** |

万一 `copyMessage` 失败（原消息已被删除等），自动回退成 `sendDocument` + 已有 `file_id`，
Telegram 同样复用那一份文件，仍然不重新上传。

### 实现要点

- **长轮询 `getUpdates`** —— 不需要公网回调地址、不新开端口、不用证书；
- 启动时先 `deleteWebhook`（同一个 bot，Telegram 侧只允许一个消费者）；
- 首次启动先探一次最后一条 `update_id` 并跳过，避免把历史积压的消息一次性重放；
- `offset` 持久化在 `/opt/tgpool/tg_offset.json`，重启不丢、不重复处理；
- **只响应白名单会话**（`TG_CHAT_ID` + `TG_BOT_ADMIN_IDS`）。否则别人搜到这个 bot
  就能把你的文件清单翻出来；非白名单会话发来的文件同样不收录、也不回复；
- **必须单进程**（`tgpool.service` 已是 `--workers 1`）。多个 worker 会各自长轮询，
  同一条消息被回复多次；
- **反向上传必须写 journal**。`rebuild_index.py` 只认 `journal/index.jsonl`，不认 caption，
  所以「发文件即入库」写的是与网页上传完全相同的一组字段。默认落点用 `TG_BOT_INBOX` 配置。

### 排查

```bash
# 轮询是否活着、token 是否有效：token_ok=true 且 last_error=null 即正常
curl -s -u admin:密码 http://127.0.0.1:8080/api/bot/status

# 不联网验证 /search 的搜索与排版逻辑（用临时库，不碰生产索引）
cd /opt/tgpool && APP_DIR=/opt/tgpool/app venv/bin/python tools/test_search_logic.py
# 期望输出：结果：PASS 88 / FAIL 0
```

`token_ok=false` 且 `last_error` 里出现 `401`，说明 Telegram 拒绝了 bot token：
去 BotFather 核对 token，更新 `/opt/tgpool/tgpool.env` 的 `TG_BOT_TOKEN` 后重启服务。

---

## 架构

```
浏览器                          Telegram 私聊（/search · /ls · /get）
  │ HTTPS (自签名证书 + Basic)      │ 长轮询 getUpdates（无需公网入站）
  ▼                                ▼
nginx :8443  ──────────►  FastAPI 应用 (systemd: tgpool)
                          上传/浏览/文件夹/下载/删除 + SQLite 索引 + 命令处理
                            │ HTTP  127.0.0.1:8080 / 8081
                            ▼
                          tg-bot-api 容器 (--local 模式)   aiogram/telegram-bot-api
                            │ MTProto
                            ▼
                          Telegram  (bot @storage_pool_bot 与你的私聊 = 真正的存储池)
```

**设计要点**

- 文件本体存在 Telegram，**服务器磁盘只是可清理的缓存**。
- 每个文件上传后获得 `file_id`，**永久有效**；本地缓存删掉也能重新从 Telegram 拉取。
- 频道/私聊与 `file_id` 解耦：即使以后更换存储位置，老文件依然可下载。

---

## 端到端验证结果

已从外部网络实测通过：

| 测试项 | 结果 |
|---|---|
| 页面上传 3MB | ✅ 3145728 字节 |
| 页面下载 | ✅ HTTP 200，**sha256 与服务端一致** |
| 断点续传 | ✅ HTTP 206，`content-range: bytes 0-99/3145728` |
| 删除 | ✅ Telegram 消息与本地索引同步删除 |
| 上传到指定文件夹 | ✅ `folder_id` 正确落库 |
| 移动文件 / 重命名文件夹 | ✅ |
| 非法移动（移入自己的子目录） | ✅ 被拒 400 |
| 模糊搜索 | ✅ 文件名+路径包含匹配，关键词高亮，多关键词 AND |
| 非空文件夹不加 `force` 删除 | ✅ 被拒 409 |
| 递归删除文件夹 | ✅ 返回 `deleted_files` / `deleted_folders` 计数 |
| 子目录内文件下载 | ✅ **sha256 一致** |

---

## 索引灾备（index.db 丢了怎么办）

### 先说结论：不能靠 Telegram 反向重建

**Telegram Bot API 无法读取历史消息**——没有"列举历史"或"按 message_id 取消息"的接口，
而且 **bot 收不到自己发出的消息**，所以 `getUpdates` 里只有"别人发给 bot"的内容，
查不到 bot 上传过的文件。实测确认过：

```
getUpdates 返回的只有用户发来的 /start 之类，没有任何 sendDocument 记录
本地 bot API 落盘目录是 file_0.bin / file_1 这种不透明名字，原始文件名也拿不到
```

所以正确做法不是"事后从 Telegram 捞"，而是**让索引始终可重建**。

### 三层保护

| 层 | 机制 | 防什么 | 恢复窗口 |
|---|---|---|---|
| 1 | **journal**（`/opt/tgpool/journal/index.jsonl`） | 库损坏 / 误删 / 迁移失败 | **零丢失**，随时重放 |
| 2 | **本地每日备份**（`/opt/tgpool/backup/`，保留 14 份） | journal 本身被删 | 最多 1 天 |
| 3 | **远程备份**（同一份上传到 Telegram 会话） | 服务器**整盘损坏** | 最多 1 天 |

任何写操作（上传/删除/移动/新建目录/改名）都会往 journal 追加一行，用**路径**而非 id 记录，
所以重放不依赖任何自增 id。日志永不截断，一份文件约 300 字节，量级完全可忽略。

### 网页上一键备份（不用登服务器）

网页工具栏上的 **「备份索引」** 按钮 = 手动跑一次 `backup_index.py`：
点一下就把索引用 SQLite 在线快照打包 + 存到服务器 `/opt/tgpool/backup/`，
同时上传一份到 Telegram 会话。弹窗会显示备份包名、大小、目录/文件数，
以及**服务器本地**与 **Telegram 云端**各自是否成功；失败时展开可看脚本完整输出。

适合这些时候用：刚批量整理完文件、准备重装系统、或只是想"现在立刻存一份"。
平时不用管 —— 每天 03:30 会自动备份一次。

> 对应接口：`POST /api/backup`（加 `?local_only=1` 则只存本地、不上传 Telegram）。
> 与定时任务**同一个脚本、同一套逻辑**，不是两套代码。

### 命令速查（在服务器上执行）

```bash
cd /opt/tgpool

# 看日志与索引是否一致（不写文件，可随时跑）
venv/bin/python tools/rebuild_index.py --verify

# 只看统计
venv/bin/python tools/rebuild_index.py --stats

# 从 journal 完整重建（原库自动另存为 index.db.bak-<时间>）
venv/bin/python tools/rebuild_index.py

# 只丢了 journal、索引还完好 → 用当前索引重建日志起点（索引零回退）
venv/bin/python tools/rebuild_index.py --seed --force

# 索引和 journal 都没了 → 先解包备份，再指向里面的 index.db
venv/bin/python tools/rebuild_index.py --restore /tmp/index.db

# 手动备份 / 列出备份 / 从 Telegram 取回 / 清理过期远程备份
venv/bin/python tools/backup_index.py
venv/bin/python tools/backup_index.py --list
venv/bin/python tools/backup_index.py --fetch
venv/bin/python tools/backup_index.py --prune-remote
```

定时任务在 `/etc/cron.d/tgpool-backup`，**每天 03:30** 自动备份 + 清理，日志在
`/var/log/tgpool-backup.log`。

### 三种故障的处置

**A. 只有 `index.db` 坏了（最常见）—— 秒级恢复，零丢失**

```bash
venv/bin/python tools/rebuild_index.py && systemctl restart tgpool
```
实测：删掉 `index.db` 后重建，3 目录 / 5 文件**完全一致**，文件 id 不变，下载 sha256 依然匹配。

**A2. 只丢了 `journal`，`index.db` 还完好 —— 重建灾备能力，索引零回退**

网页照常能用，但"随时可重建"的能力没了。**不要**从备份 `--restore`（那会把索引一起回滚到备份时刻、
丢掉最近的改动）。正确做法是用当前索引重新导出一份日志起点：

```bash
venv/bin/python tools/rebuild_index.py --seed --force
venv/bin/python tools/rebuild_index.py --verify     # 应输出「校验通过」
```

**B. `index.db` 和 `journal` 都没了 —— 用备份**
```bash
cp backup/tgpool-backup-<最新>.tar.gz /tmp/b.tgz && tar xzf /tmp/b.tgz -C /tmp
venv/bin/python tools/rebuild_index.py --restore /tmp/index.db
```
备份包里同时含 `index.db`、`journal/index.jsonl` 和 `MANIFEST.json`（含条数与 sha256）。

**C. 服务器整盘报废 —— 从 Telegram 取回**

备份包每天都会作为一条消息发到你的存储池会话里，名叫 `tgpool-backup-<时间>.tar.gz`。
**可以直接在 Telegram 客户端里手动把它下载下来**（不需要任何 token），
或者在新服务器上跑 `venv/bin/python tools/backup_index.py --fetch` 自动取回并校验 sha256。

> 取回后需要重建环境（见「部署 / 重建流程」）。⚠️ `tgpool.env` 里的 bot token 不包含在备份包里，
> 请**单独妥善保存**（丢了就得重新找 BotFather 要，且旧会话不再可用）。

### 灾备演练

`scripts/test_dr.py` 会自动跑一遍完整演练：造数据 → 备份 → 删库 → 重建 → 逐项比对 → 清理。
改动灾备相关代码后建议跑一次：

```bash
/opt/tgpool/venv/bin/python /opt/tgpool-setup/test_dr.py
```

### 踩坑：`--local` 模式下 `getFile` 返回的是本机绝对路径

`backup_index.py --fetch` 一开始报 404，因为拿 `file_path` 去拼
`http://127.0.0.1:8081/file/bot<token>/<file_path>` —— 而 `--local` 模式下它返回的是
`/var/lib/telegram-bot-api/.../documents/file_11.gz` 这种**本机绝对路径**。
这种情况下要**直接读磁盘**，不能走 HTTP。主程序 `app.py` 里也是同一套处理。

---

## 下载为什么要等，以及进度条

**现象**：点一个大文件下载，浏览器会转很久才开始。

**原因（实测）**：bot API 的 `getFile` **必须先把整份文件从 Telegram 拉到服务器本地磁盘，才会返回文件路径**。
在它返回之前，服务器无法向浏览器发出第一个字节。

实测一个 22.34 MB 的 PDF：

| 场景 | 首字节到达时间 | 总耗时 |
|---|---|---|
| 首次下载（未缓存） | **40.46 秒** | 40.52 秒 |
| 第二次下载（已缓存） | 0.013 秒 | 0.076 秒 |

即 99.8% 的时间花在「还没开始」。传输本身只占 0.07 秒 —— 因为文件已经在本地磁盘上，读盘速度 300 MB/s 级。

**换算**：服务器到 Telegram 约 578 KB/s，所以 **2 GB 文件首次点击要等约 1 小时**才开始。

> 这是架构固有的，**无法改成「边下边传」**：Bot API 的 `getFile` 没有流式 / Range 接口，
> 只返回一个本地文件路径，而这条路径只有在整份文件落盘后才存在。`--local` 自建 server 已是此架构下最优。

**本项目的处理**：点「下载」后先走预热，弹窗显示**真实进度**：

1. 前端 `POST /api/download/{id}/prepare` —— 服务器在后台开始 `getFile`
2. 前端每 0.7 秒轮询 `GET /api/download/{id}/progress`
3. 进度不是估算的：bot API 下载中的文件先落在 `<root>/<token>/temp/`，
   完成后才原子改名进 `documents/`。后端直接读这个目录的文件大小，
   换算出**精确的字节数 / 百分比 / 速度 / 剩余时间**
4. 拉取完成后自动开始下载（此时是本地磁盘读取，瞬间完成）

弹窗里的「关闭（继续后台拉取）」只是关掉界面，**服务器仍在后台拉** —— 稍后再点「下载」就秒开。
同一个文件第二次点下载，`prepare` 会直接返回 `ready`（实测 0.004 秒）。

**实用提示**：想避免长时间等待，就在**空闲时间**（比如睡前）点一次「下载」然后立刻关掉弹窗，
让服务器慢慢拉；第二天再下就是秒开。这相当于手动预热。

---

## 服务器磁盘与缓存

**结论：上传不占盘，下载才占。**

| 操作 | 是否占用服务器磁盘 | 说明 |
|---|---|---|
| **上传** | ❌ 不占 | bot API 把文件直通转发给 Telegram，转发完不留副本；本地临时文件在 `finally` 里删掉 |
| **下载** | ✅ 占 | 文件会落在 `/var/lib/telegram-bot-api/<token>/documents/`，**永久累积** |
| **从网页删文件** | ⚠️ 不释放 | bot API 不知道你删了索引记录，缓存要单独清 |

实测（上传 / 下载同一个 20 MB 文件）：

| 时点 | `documents/` 占用 |
|---|---|
| 上传前 | 48 MB |
| **上传 20 MB 后** | 48 MB ← 没变 |
| **下载该文件后** | 68 MB ← +20 MB |
| 从池子里删掉该文件 | 68 MB ← 仍未释放 |

好处是**重复下载会秒开**——命中本地缓存时读磁盘（3.1 GB/s），比从 Telegram 回源（584 KB/s）
快约 1000 倍。所以缓存是**该留的**，只是要有上限。

### 怎么清理

网页上有两个入口：顶栏的 **「清理缓存」按钮**，以及右上角的 **「缓存」数值**（点它也打开）。

弹窗里会显示缓存总量、文件数和**年龄分布**，并给三个动作：

- **清理 30 天前的** —— 最常用，只删长期没碰过的
- **全部清空** —— 需二次确认；之后所有下载都要回源
- **关闭**

命令行同样可用（工具在 `/opt/tgpool/tools/clean_cache.py`）：

```bash
cd /opt/tgpool
./venv/bin/python tools/clean_cache.py                  # 只看，不删
./venv/bin/python tools/clean_cache.py --days 30        # 删 30 天未活动的
./venv/bin/python tools/clean_cache.py --days 30 --dry-run   # 先预览
./venv/bin/python tools/clean_cache.py --max-gb 8       # 超 8GB 就从最旧的删起
./venv/bin/python tools/clean_cache.py --all            # 全清（会要求输入 yes）
```

**自动瘦身**：部署脚本会装一条每周日 05:00 的定时任务，只在缓存**超过 8GB** 时才从最旧的删起
（阈值可用 `TG_CACHE_MAX_GB` 调整）。平时不动，不影响"秒开"体验。

**安全边界**：工具只碰 `<root>/<token>/documents` 和 `temp`，**绝不碰 `td.binlog`**
（那是 TDLib 的数据库日志）。还会跳过最近 10 分钟内活动过的文件，避免删掉正在下载的那个。

**删缓存是安全的**：`file_id` 存在索引里，下次下载时 bot API 会自动从 Telegram 重新拉取，
只是那一遍会退回慢速。整个过程不影响存储池里的任何文件。

---

## 服务器信息

| 项 | 值 |
|---|---|
| IP | `82.158.224.64` |
| 系统 | Ubuntu 22.04 LTS，x86_64，**1 核** |
| 内存 | 971 MB（已加 4GB swap，`/swapfile`） |
| 磁盘 | 29 GB（可用约 21 GB） |
| 已装 | Docker 29.1.3、nginx、Python 3.10 |

---

## 服务端目录结构

```
/opt/tgpool/
├── app/
│   ├── app.py                 FastAPI 主程序
│   ├── static/index.html      网页前端
│   └── requirements.txt
├── tools/
│   ├── rebuild_index.py       ★ 从 journal 重建 / 校验 / 恢复索引
│   ├── backup_index.py        ★ 备份（本地 + 上传 Telegram）/ 取回
│   ├── clean_cache.py         bot API 缓存清理（定时任务与网页按钮共用）
│   ├── show_password.py       找回 / 重设网页登录密码（读 tgpool.env）
│   └── test_search_logic.py   搜索与聊天命令的离线自测（196 项，不联网）
├── journal/index.jsonl        ★ 追加式变更日志（灾备核心，勿删）
├── backup/                    本地备份包 + remote.jsonl（远程备份记录）
├── venv/                      Python 虚拟环境
├── tgpool.env                 配置与敏感凭据（权限 600）
├── tg_offset.json             getUpdates 游标（聊天命令用，删掉会重放积压消息）
├── tmp/                       上传中转目录
└── index.db                   SQLite 索引

/var/lib/telegram-bot-api/     bot API 数据 + 文件缓存（可清理）
/opt/tgpool-setup/             部署脚本
/var/log/tgpool-backup.log     每日备份日志

/etc/systemd/system/tgpool.service         Web 应用服务
/etc/nginx/sites-available/tgpool          8443 反向代理
/etc/cron.d/tgpool-backup                  每日 03:30 备份
/etc/logrotate.d/tgpool-backup             备份日志轮转
```

---

## 配置项（`/opt/tgpool/tgpool.env`）

| 变量 | 说明 |
|---|---|
| `TG_BOT_TOKEN` | BotFather 给的 token |
| `TG_CHAT_ID` | 存储池目标会话 ID（当前 `8575978784`） |
| `TG_API_ID` / `TG_API_HASH` | bot API server 的开发者凭据 |
| `TG_API_BASE` | 本地 bot API 地址 `http://127.0.0.1:8081` |
| `TG_AUTH_USER` / `TG_AUTH_PASS` | 网页 Basic 认证账号密码 |
| `TG_TMP_DIR` | 上传中转目录 |
| `TG_DB_PATH` | SQLite 索引路径 |
| `TG_LOCAL_ROOT` | bot API 缓存根目录 |
| `TG_JOURNAL` | 变更日志路径（默认 `/opt/tgpool/journal/index.jsonl`） |
| `TG_CAPTION_PATH` | 上传时是否附 `TGPOOL /路径` caption（默认 `1`，设 `0` 关闭） |
| `TG_BOT_POLL` | 是否开启聊天命令长轮询（默认 `1`，设 `0` 关闭；关闭后 `/search` 等命令无响应） |
| `TG_BOT_ADMIN_IDS` | 额外允许使用命令的用户 ID，逗号分隔（`TG_CHAT_ID` 始终允许） |
| `TG_BOT_OFFSET` | `getUpdates` 游标文件路径（默认 `/opt/tgpool/tg_offset.json`） |
| `TG_BOT_INBOX` | 直接发文件给 bot 时的默认目录名（默认 `收件箱`） |
| `TG_BOT_SHOW_PASS` | 是否允许 `/pass` 在聊天里回网页密码（默认 `1`，设 `0` 关闭） |
| `TG_NGINX_PORT` | 对外 HTTPS 端口（部署脚本自动写入，`/pass` 用它拼网页地址） |

修改后执行 `systemctl restart tgpool` 生效。

---

## 日常运维

```bash
# 查看 Web 应用状态与日志
systemctl status tgpool
journalctl -u tgpool -n 50

# 查看 bot API 容器状态与实际生效参数
docker ps --filter name=tg-bot-api
docker logs --tail 30 tg-bot-api

# 重启服务
systemctl restart tgpool
docker restart tg-bot-api

# 查看磁盘占用（缓存）
du -sh /var/lib/telegram-bot-api

# 索引一致性自检（不会改任何东西）
/opt/tgpool/venv/bin/python /opt/tgpool/tools/rebuild_index.py --verify

# 立即备份一次 / 查看备份 / 清理过期远程备份
/opt/tgpool/venv/bin/python /opt/tgpool/tools/backup_index.py
/opt/tgpool/venv/bin/python /opt/tgpool/tools/backup_index.py --list
/opt/tgpool/venv/bin/python /opt/tgpool/tools/backup_index.py --prune-remote

# 每日备份日志
tail -20 /var/log/tgpool-backup.log
```

### 忘记网页密码怎么办

三条路，任选一条（都不需要联网）：

```bash
# 1) 在服务器上直接看（推荐；连 bot token 也能一起看）
python3 /opt/tgpool/tools/show_password.py
python3 /opt/tgpool/tools/show_password.py --token      # 附带 bot token（灾备重建要用）
python3 /opt/tgpool/tools/show_password.py --json       # 脚本可读

# 2) 换一个新密码（原文件自动备份成 tgpool.env.bak-<时间戳>）
python3 /opt/tgpool/tools/show_password.py --reset --yes
systemctl restart tgpool

# 3) Telegram 里给 bot 发 /pass
```

`show_password.py` 是纯标准库脚本，系统自带的 `python3` 就能跑，服务挂了也不影响它；
密码存在 `/opt/tgpool/tgpool.env`（权限 600），所以要用 root 执行。

---

## 本地工具（Windows 侧）

工作目录 `C:\Users\use\Desktop\tg\`：

| 文件 | 用途 |
|---|---|
| `ssh_run.py` | 在服务器上执行单条命令：`python ssh_run.py "ls /opt"` |
| `ssh_upload.py` | 上传文件：`python ssh_upload.py 本地 远程` |
| `ssh_push.py` | 上传脚本并立即执行 |
| `scripts/` | 全部部署脚本，可重复执行 |
| `server/` | Web 应用的源码副本 |

依赖已装在隔离虚拟环境 `~/.workbuddy/binaries/python/envs/tg-server`（paramiko）。

---

## 服务器部署

> 🚨 **服务器报废、要在全新 Ubuntu 上重建 → 直接看 [`REBUILD.md`](REBUILD.md)**
> 那里有逐条命令、期望输出、故障排查表，以及"必须留在服务器之外的三样东西"清单。

### ******************推荐：一键脚本

上传**文件** `tgpool-deploy.tar.gz` 和`tgpool-backup-***.tar.gz`(索引文件，新构建的不要上传，如果bot已有文件则在bot里面发送`/bk`下载最新的索引文件)到服务器的 `/root/`，然后：

```bash
tar xzf tgpool-deploy.tar.gz && cd tgpool-deploy
bash deploy.sh              # 交互式，依次问你要 bot token 等
```

## telegram bot部署

1. 在@BotFather里面发送`/newbot`
2. 按步骤先后输入：名称、username(以`bot`结尾，如`storage_bot`)
3. 复制好HTTP API：8919###:###LPU
4. 输入到服务器（1/5）Bot Token:

## 踩坑记录（重要，重装必读）

### 1. Docker 镜像会静默忽略命令行参数

`aiogram/telegram-bot-api` 的 entrypoint **只读取环境变量，完全不转发 `"$@"`**。

- ❌ 错误：`docker run ... image --local --http-port=8081` ← `--local` 被丢弃
- ✅ 正确：加环境变量 `-e TELEGRAM_LOCAL=1`

**症状**：`getFile` 返回相对路径 `documents/file_0.bin`，下载报 404。
**排查**：`docker logs tg-bot-api` 看实际生效参数；`docker inspect <image> --format '{{.Config.Cmd}}'`。

### 2. 服务器默认 apt 源不通

`archive.ubuntu.com` / `security.ubuntu.com` 超时，但 `mirrors.aliyun.com` 只要 27ms。
必须换源，否则 `apt-get update` 会无限卡住。

### 3. Cloudflare 免费版限制上传 100MB（8443 端口也一样）

域名 `example.com` 走 Cloudflare 代理，**请求体超过 100MB 会被直接拦截**（`413`）。

**一个容易踩的坑**：Cloudflare 免费版支持的代理端口列表里**包含 8443**，
所以换成 `https://example.com:8443/` **并不能**绕过限制 —— 实测响应头仍是 `server: cloudflare`。
真正绕过的只有「IP 直连」或「关掉代理」。

| 做法 | 结果 |
|---|---|
| **灰云（DNS only）** | Cloudflare 只做解析、直接返回源站真实 IP，流量完全绕过 CF → **无 100MB 限制，2GB 正常** |
| 保持小黄云（proxied） | 只能用 ``（IP 直连）绕开域名；且流量双倍计费、多绕一跳 |

**切成灰云的方法**：Cloudflare 控制台 → DNS → 找到该 A/AAAA 记录 → 点橙色云朵变灰（DNS only）。

灰云下有三个必须记住的点：

1. **端口不能省**：不代理就没有 443/80 的自动转发，访问地址仍是 。
2. **A 记录必须指向真实 IP**。如果原来有 AAAA 记录指向 Cloudflare 的 IPv6，要删掉或改成真实 IPv6。
3. **证书仍然是自签的**，浏览器照样警告 —— 灰云解决的只是 100MB 限制，不解决证书信任。

代价：源站 IP 暴露、失去 CF 的 CDN 与 DDoS 防护。对个人备份池可以接受。

> 想让证书也变可信又不想撞 100MB 墙：用 **Let's Encrypt 的 DNS-01 验证**（DNS 已在 Cloudflare，
> 用 API Token 签，不需要开放任何端口），全程不经过 CF 代理 → 既无警告也无 100MB 限制。

### 4. 服务器 Python 3.10 的 f-string 限制

f-string 表达式内**不能包含反斜杠**（3.12+ 才允许）。需先算出中间变量再插值。

### 5. `pkill -f <pattern>` 会杀掉自己的 shell

`ssh ... "pkill -f get.docker.com"` 时，远程命令行本身包含该字符串，会把自己也匹配杀掉。
改用 `pkill -x`（精确进程名）或直接按 PID 杀。

### 6. 中文文件名会让下载 500

`Content-Disposition` 等 HTTP 头只能用 latin-1 编码，中文名直接塞进去会抛
`UnicodeEncodeError: 'latin-1' codec can't encode characters` → 下载接口 500。
**症状**：英文名文件下载正常，一旦有中文名就失败（容易误判成"文件坏了"）。
**修法**：按 RFC 6266/5987 输出 ASCII 兜底名 + `filename*=UTF-8''<百分号编码>`，
见 `app.py` 的 `_content_disposition()`。新增任何"把用户文件名写进响应头"的代码都要走它。

---

## 已知限制

| 限制 | 说明 |
|---|---|
| **服务器带宽有限** | 实测下行 **~585 KB/s**、上行 **~1.14 MB/s**。所有网页传输都要过服务器中转，2GB 文件约需 **1 小时**，属物理限制 |
| **流量翻倍** | 一次 N GB 的网页传输会在服务器上产生 **2N GB** 流量（进出各一份），有月流量额度的 VPS 需留意 |
| **下载会占服务器磁盘** | 下载过的文件会在 `/var/lib/telegram-bot-api` 留一份缓存（**上传不占**）。已提供网页/命令行清理工具 + 每周自动瘦身，见「服务器磁盘与缓存」 |
| **api_id 为公共凭证** | `api_id=2040` 是 Telegram 官方桌面端公开值，长期高频可能被限流 |
| **单文件上限 2GB** | 由 `--local` 模式决定，不可突破 |
| **不适合频繁随机读写** | Bot API 无 Range 写能力，本方案面向"存取"而非"实时读写" |
| **文件夹结构只在本地** | 目录关系存于 `index.db`，不在 Telegram 侧；已有 journal + 双层备份保护，见「索引灾备」 |
| **重建后目录 id 会变** | 文件 id 稳定（下载链接不变），但文件夹 id 会重新分配；仅内部使用，无持久化影响 |
| **同级目录名需唯一** | 同名文件夹会被拒绝（409），否则路径不唯一、重建时会合并 |
| **`tgpool.env` 不在备份包里** | bot token 等凭据请单独保存，否则整盘损坏后无法取回备份 |

---

## 后续换用自己的 api_id

1. 在 `my.telegram.org` 申请到 `api_id` / `api_hash`
2. 编辑 `/opt/tgpool/tgpool.env` 的 `TG_API_ID` / `TG_API_HASH`
3. `docker rm -f tg-bot-api` 后重新执行 `scripts/setup_botapi.sh`

**已存在的文件不受影响**，`file_id` 与新 api_id 兼容。



## ******************自述

1. 上传下载和服务器带宽有关

2. 每天3：30自动备份好`tgpool-backup-XXXXXXXX-XXXXXX.tar.gz`

3. 限制上传文件大小：小于2GB

4. ### bot 命令

   未知命令 /test

   TG 存储池 · 命令一览

   `/search` 关键词   模糊搜索：文件名或所在路径包含即命中,例：/search 报表　/search 2026 财务

   `/ls [路径]  `     列出目录内容，省略路径则列根目录例：/ls  /ls /工作/2026

   `/get 编号   `     按编号取回文件（编号来自搜索结果）
`/move 源 目标    移动文件或文件夹`，目标留空 = 挪到根目录，例：/move /工作/2026 /归档　/move "/我的 报告"
   `/rm 路径`  删除文件；删文件夹会先问一句再动手，例：/rm /工作/2026/报表.pdf　/rm /工作/2026　/rm #1234
      /rm -f 路径    跳过确认直接删

   `/backups(/bk)`       列出云端索引备份包，点编号即可取回
`/stats`          池子统计
   `/pass `           忘记网页密码时把账号密码捞回来
`/help`            显示本帮助
   
