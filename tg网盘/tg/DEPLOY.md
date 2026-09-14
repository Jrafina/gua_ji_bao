# DEPLOY.md — 一键部署 / 灾难重建

一个压缩包、一条命令，把 TG 存储池装到一台**全新的 Ubuntu** 上；
在**已经装好的机器**上重跑，它会自动跳过已完成的步骤，不会破坏数据。

> 换服务器（旧机报废 → 新机重建）的完整背景与原理，见 [REBUILD.md](REBUILD.md)。
> 本文只讲**怎么用这个脚本**。

---

## 一、你需要上传什么

**只有一个文件：**

```
C:\Users\Jrafina\Desktop\tg\tgpool-deploy.tar.gz     （约 37 KB）
```

用你的 SSH 工具（FinalShell / Xshell / WinSCP / 宝塔 等）把它拖到服务器的 `/root/` 目录即可。

包里已经装好了所有东西，**不需要你另外准备任何文件**：

```
tgpool-deploy/
├── deploy.sh                  一键脚本（入口）
├── tgpool.service             systemd 服务单元
├── app/
│   ├── app.py                 后端（FastAPI）
│   ├── requirements.txt       Python 依赖
│   └── static/
│       ├── index.html         网页前端
│       └── background.jpg     网页背景图
└── tools/
    ├── rebuild_index.py       索引重建工具
    ├── backup_index.py        索引备份工具
    ├── clean_cache.py         bot API 本地缓存清理工具
    └── show_password.py       找回 / 重设网页登录密码（忘记密码时用）
```

---

## 二、脚本会问你的东西

运行后会依次问 5 项，**直接回车 = 用默认值**：

| # | 项目 | 必需？ | 说明 |
|---|---|---|---|
| 1 | **Bot Token** | **必需** | Telegram 里找 `@BotFather` → 发 `/newbot` → 拿到形如 `123456789:AAH...` 的串 |
| 2 | chat_id | 可留空 | 留空则脚本稍后**自动探测**（前提：你先给 bot 发一条消息，脚本会提示你做） |
| 3 | api_id | 有默认 | 默认 `2040`（Telegram Desktop 官方公开值），回车即可 |
| 4 | api_hash | 有默认 | 回车即可 |
| 5 | 网页账号 / 密码 | 有默认 | 账号默认 `admin`；密码留空则**自动生成 16 位随机密码**，结尾会打给你 |
| 6 | 索引备份包路径 | 可选 | 重建时填（从 Telegram 下载的 `tgpool-backup-*.tar.gz`）；全新安装留空。**留空时脚本会在 `/root`、当前目录、包所在目录等位置自动找**，找到且本机还没装过就会问一句「用它恢复索引？」，回车即自动恢复 |

> **token 输入时不回显**（防肩窥），输完会显示掩码让你核对，格式不对会要求重输。
> 还需要一样东西但**不用输**：`@storage_pool_bot` 的 private key 在 Telegram 里跟 `@BotFather` 发 `/mybots` 随时可查。

---

## 三、执行

```bash
cd /root
tar xzf tgpool-deploy.tar.gz
cd tgpool-deploy
bash deploy.sh
```

脚本先打印一份**现状体检**（系统 / 内存 / swap / Docker / nginx / 是否已装过），
然后让你确认一次 `[y/N]`，之后无人值守跑完 10 步。

> **重建（换机）最省事的做法**：把 `tgpool-deploy.tar.gz` 和从 Telegram 下载的
> `tgpool-backup-*.tar.gz` **一起丢到 `/root/`**，然后照上面三条命令跑。
> 脚本会自动发现那个备份包并问一句「用它恢复索引？」，回车即恢复 —— 不用手打路径。
> （本机已经装过、`/opt/tgpool/index.db` 还在时不触发这个自动发现，避免误覆盖线上数据。）

结尾会打印：

```
✓ 完成

  网页地址   https://<你的IP>:8443/
  用户名     admin
  密码       xxxxxxxxxxxxxxxx
```

---

## 四、这 10 步分别做什么

| 步骤 | 内容 | 已配置时 |
|---|---|---|
| 1 | swap（内存 <4G 时加 4G，防 OOM） | 已有 swap → 跳过 |
| 2 | apt 软件源（默认源不通时自动挑最快镜像） | 默认源可达 → 不换 |
| 3 | Docker（安装 + 镜像加速 + 启动） | 已装 → 跳过；已配加速 → 不动 daemon.json |
| 4 | 应用文件 + `tgpool.env` 配置 | 文件一致 → 不重写；配置只更新有变化的项 |
| 5 | Python 虚拟环境 + 依赖 + 注册 systemd 服务 | 已就绪 → 跳过 |
| 6 | Telegram bot API 容器（`--local` 模式，解锁 2GB） | 容器健康且 token 匹配 → 跳过 |
| 7 | **可选**：从备份包恢复索引 | 未提供备份包 → 跳过 |
| 8 | 绑定 `chat_id`（自动探测 + 上传冒烟测试） | 已配置 → 跳过 |
| 9 | nginx 反向代理（8443 HTTPS，自签证书兜底） | 已装且已配 → 跳过 |
| 10 | 索引灾备 + 缓存瘦身（journal 起点；每日 03:30 备份；每周日 05:00 缓存瘦身） | 已有 journal / cron → 跳过（cron 缺缓存条目会自动补齐） |

> 装好后网页工具栏上有 **「备份索引」** 和 **「清理缓存」** 两个按钮，随时手动执行，
> 不用登服务器。右上角还有个 **「缓存」** 数值显示当前占了多少磁盘，点它也能打开清理弹窗。
> 缓存上限默认 8GB，可用 `TG_CACHE_MAX_GB` 调整。

---

## 五、先看看会做什么：`--check`

**建议第一次跑之前先用它体检**，确认无误再真跑：

```bash
bash deploy.sh --check
```

只读取状态、打印将要执行的操作，**不改动任何东西**、不启动任何服务、不交互。
输出里每一行的含义：

- `✔` 本次会执行的动作
- `─` 检测到已完成，**将跳过**
- `[check]` 将要执行、但此刻没执行

---

## 六、在已装好的机器上重跑

直接 `bash deploy.sh`。它会：

- 已完成的步骤**跳过**（不会重复下载、不会重建容器）
- 应用文件**只在内容有变化时**才覆盖（用 `cmp` 逐字节比对）
- 配置项**只在值有变化时**才写（比如你换了 token）
- nginx 配置 / cron 文件**只在不存时才写**（避免覆盖你的自定义）

所以升级版本很简单：**把新包解压到某个目录，跑一次即可**。

---

## 七、演练模式（不动系统）

指定 `--app-dir` 到别的地方，脚本只操作那个目录，**不碰 systemd / nginx / cron**：

```bash
bash deploy.sh --app-dir /tmp/tgsim
```

用来验证包能正常解压、装依赖、写配置，而不影响正在跑的服务。

---

## 八、无人值守 / 批量

配合环境变量 + `--yes`，全程零交互：

```bash
cd /root/tgpool-deploy
TG_BOT_TOKEN='123456789:AAH...' \
TG_CHAT_ID='8575978784' \
TG_AUTH_PASS='yourpassword' \
bash deploy.sh --yes
```

支持的环境变量：
`TG_BOT_TOKEN` `TG_CHAT_ID` `TG_API_ID` `TG_API_HASH` `TG_AUTH_USER` `TG_AUTH_PASS`
`TG_NGINX_PORT` `TG_APP_DIR` `TG_BUNDLE` `TG_CACHE_MAX_GB`（缓存瘦身阈值，默认 8）

---

## 九、参数速查

| 参数 | 作用 |
|---|---|
| `--check` | 只体检，不改动 |
| `--yes` | 非交互，值取环境变量或已有配置 |
| `--bundle FILE` | 顺便从备份包恢复索引（不指定时自动到 `/root` 等位置找，见上表第 6 项） |
| `--port N` | 对外 HTTPS 端口（默认 8443） |
| `--app-dir DIR` | 安装目录（默认 `/opt/tgpool`；换成别处 = 演练模式） |
| `-h, --help` | 帮助 |

---

## 十、常见报错

| 现象 | 原因 / 处理 |
|---|---|
| `bash: deploy.sh: No such file or directory` | 忘了 `cd tgpool-deploy`，或者没解压 |
| `部署包不完整：找不到 .../app/app.py` | 没在解压出来的目录里运行，或者包没传完整 |
| `请以 root 运行` | 用 `sudo bash deploy.sh`，或者直接以 root 登录 |
| 卡在 `还没收到任何消息` 等待 | 按提示打开 Telegram 搜你的 bot 发一条 `hi`，再回车 |
| `非交互模式下无法自动探测 chat_id` | 用 `TG_CHAT_ID=xxx` 显式指定后重跑 |
| `默认源不可达（超时）`，后面各镜像跟 `FAIL:原因` | **按原因分别处理**：`DNS 解析失败` → 查 `/etc/resolv.conf`；`超时(12s)` / `TCP 80 不通` → 出网被防火墙或云安全组拦；`连接被拒` → 存在代理 |
| `所有 apt 镜像都不可达`（连国内镜像也不通） | 这台机器基本没有出网。在服务器上逐条执行：`command -v curl wget; cat /etc/resolv.conf; getent hosts mirrors.aliyun.com; timeout 6 bash -c 'exec 3<>/dev/tcp/223.5.5.5/80 && echo TCP-OK'` |
| `apt-get update 失败`（脚本会打印日志末尾） | 自动换源后仍失败，多为 DNS 或走了不通的代理；可 `TG_FORCE_MIRROR=1 bash deploy.sh` 强制再换一次源 |
| `镜像拉取失败` | Docker 加速没生效，检查 `/etc/docker/daemon.json` |
| 外网打不开网页 | **云服务商安全组**放行 8443（最常见原因）；服务器内 `ufw` 也要放行 |
| 浏览器提示"不安全" | 自签名证书的正常提示，点「高级 → 继续访问」 |
| 点下载后弹窗一直转、很久才开始 | **正常现象**。bot API 必须先把整份文件拉到服务器本地才能开始传输，服务器带宽约 580 KB/s（2 GB 约 1 小时）。弹窗显示的是真实进度，可点「关闭（继续后台拉取）」等它拉完，之后秒开 |
| 上传 >100MB 失败（413） | 走了 Cloudflare 代理。**注意 CF 连 8443 也代理**，换 `https://域名:8443/` 无效。解法：① 用 `https://IP:8443/` 直连；② 或到 Cloudflare 把该 DNS 记录改成**灰云（DNS only）**，之后 `https://域名:8443/` 即可直连、无 100MB 限制 |
| `nginx 配置检查失败` | 8443 被别的服务占用，换端口：`--port 9443` |
| `备份包 sha256 校验未通过` | 包下载不完整或损坏，重新从 Telegram 下载一份 |
| **新服务器网页列表为空，但 Telegram 里文件都还在** | **正常现象，不是 bug**：索引（`index.db`）只存在服务器本地，新机器是空账本。用备份包恢复即可：`bash deploy.sh --bundle /root/tgpool-backup-*.tar.gz`。包可从 Telegram 会话下载，或旧服务器还活着时从 `/opt/tgpool/backup/` 取 |
| 恢复后能列出但下载 404 | 新机器的 bot token 与旧机器不是同一个 → `file_id` 失效，需用原 bot 的 token |
| 第 8 步探测不到 chat_id（发了消息也"还没收到"） | **同一个 token 只允许一个 `getUpdates` 消费者**：本机旧 tgpool 或另一台旧机器在抢消息。旧机器执行 `systemctl stop tgpool && docker stop tg-bot-api` 后再发一条**新**消息重试；本机重部署时 v1.8 起脚本会自动暂停 tgpool 再探测 |
| 手动填备份路径提示"文件不存在" | 路径带了引号（v1.8 起自动剥掉）；或文件是浏览器改名副本 `xxx.tar (2).gz`（v1.8 起嗅探与校验都兼容，直接回车让脚本自动找即可） |
| 给 bot 发 `/search` 没反应 | 先看 `curl -s -u admin:密码 http://127.0.0.1:8080/api/bot/status`：`token_ok=false` + `last_error` 含 `401` → token 被 Telegram 拒绝，去 BotFather 核对后更新 `tgpool.env`；`last_poll=0` → 轮询未启动，检查 `TG_BOT_POLL` 是否为 `0`、服务是否单 worker；`handled` 一直为 0 → 消息到达了但会话不在白名单，把用户 ID 加进 `TG_BOT_ADMIN_IDS` |
| `/search` 回复重复好几遍 | `tgpool.service` 被改成了多 worker。长轮询必须单进程，恢复成 `--workers 1` |
| 发文件给 bot 没有收录回复 | 同上先看 `/api/bot/status`。若 `handled` 不涨说明消息没进白名单（把用户 ID 加进 `TG_BOT_ADMIN_IDS`）；若是 `sticker` 或非文件类型，属于设计上不收录；若回复"已经在池子里了"则是按 `file_unique_id` 命中了去重 |
| 发文件后消息多了一行 `TGPOOL /xxx` | **正常设计**：脚本给原本没有说明的消息补一行 `TGPOOL <路径>`，让 Telegram 侧自带目录信息（便于人工核对）。你原本的说明不会被覆盖 |
| 忘记网页密码 / 账号密码找不着 | 三条路任选：① Telegram 里发 `/pass`；② 服务器上 `python3 /opt/tgpool/tools/show_password.py`（`--token` 连 bot token 一起看）；③ `python3 /opt/tgpool/tools/show_password.py --reset --yes` 直接换个新的，然后 `systemctl restart tgpool`。脚本是纯标准库，服务挂了也能跑，要用 root |
| `/rm` 删完能恢复吗 | **不能**。`/rm` 会连 Telegram 上的原消息一起删掉，是真删除。所以删文件夹时 bot 会先列出内容让你点「确认删除」（想跳过加 `-f`）。索引侧照旧写 journal，但消息没了就真没了 |
| `/move` 的目标目录不存在 | **会自动逐级创建**（和发文件时在说明里写路径一个规矩），回复里会列出新建了哪几级；不想让它建就先手工把目录建出来 |
| `/move` 报"已经有同名文件夹了" | 目标目录下已有同名目录，改名或换个目标；不会静默覆盖 |
| `/move` 报"不能挪进它自己的子目录" | 循环保护。先把子目录挪走，或换个目标目录 |

---

## 十一、重建时特别注意两件事

**① 顺序不能反。** 用 `--bundle` 恢复备份必须发生在「索引灾备」之前——
脚本已经按正确顺序内置了（第 7 步恢复，第 10 步启用灾备）。
如果你**手工分开执行**，务必先恢复、后启用，否则 journal 会用空索引做起点。

**② 三样东西必须留在服务器之外**（少一样，恢复等级就降一级）：

| 东西 | 在哪 | 丢了会怎样 |
|---|---|---|
| `tgpool-deploy.tar.gz`（代码） | 你的电脑 | 服务重建不出来 |
| Bot Token | `@BotFather` 随时可查 | 找不回（但可 revoke 换新） |
| 备份包 | Telegram 会话里的 `tgpool-backup-*.tar.gz` | 索引从零开始（文件本体还在 Telegram） |

---

## 相关文档

- [README.md](README.md) — 项目总览、日常运维、目录结构、配置项说明
- [REBUILD.md](REBUILD.md) — 换服务器重建的分步 runbook（含期望输出与故障排查）
