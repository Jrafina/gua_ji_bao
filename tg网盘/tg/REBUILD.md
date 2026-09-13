# 服务器报废后，在全新 Ubuntu 上重建

> 前提：你现在能拿到新服务器的 **root SSH**。
> 下面所有命令都是**在新服务器的 root shell 里**执行的。按顺序做，每一步都有「期望输出」，不通过就别往下走。

> ⚡ **更省事的做法**：上传 `tgpool-deploy.tar.gz`，跑 `bash deploy.sh`。
> 它会交互式收集 token / chat_id / 备份包，自动完成本文第 2–11 节，
> 并且已经内置了「**先恢复备份、再启用灾备**」这个关键顺序约束。
> 用法见 [`DEPLOY.md`](DEPLOY.md)。
> 本文档保留为**手工逐步版**：出问题时可以照着它定位是哪一步挂了。

---

## 0. 先说清楚：什么能恢复，什么恢复不了

| 东西 | 能不能恢复 | 靠什么 |
|---|---|---|
| **文件本体** | ✅ 一定能 | 在 Telegram 云端，`file_id` 长期有效 |
| **目录结构 + 文件索引** | ✅ 一定能 | 从 Telegram 里的每日备份包 `tgpool-backup-*.tar.gz` |
| **服务本身** | ✅ 一定能 | 本地 `tg/` 目录里的脚本 + 源码 |
| 备份包生成**之后**的新改动 | ⚠️ 最多丢 1 天 | 备份是每天 03:30 的 |
| 服务器本地磁盘缓存 | ❌ 不需要 | 本来就是可清理的缓存 |

**关键前提**：这套恢复依赖三样东西**不在这台报废的服务器上**——脚本源码、bot token、备份包。这三样缺一，恢复等级就下降，见第 14 节。

> 🔥 **两台机器同时报废**（老机新机都连不上）的专门说明见 [DISASTER.md](DISASTER.md)。
> 一句话结论：文件本体和索引备份都在 Telegram 云端，能完整恢复；备份包从 Telegram 会话里手动下载即可。

---

## 1. 你手上必须先有这 4 样

| # | 东西 | 在哪找 | 没有的后果 |
|---|---|---|---|
| 1 | **本地项目目录** `tg/` | 你的电脑 `C:\Users\Jrafina\Desktop\tg\` | 没有源码和脚本，得重写 |
| 2 | **bot token** | `tg/scripts/set_token.sh`；或 Telegram 找 `@BotFather` → `/mybots` → 选 bot → API Token | 服务起不来 |
| 3 | **备份包** `tgpool-backup-*.tar.gz` | Telegram 里打开和 `@storage_pool_bot` 的会话，往下翻，**手动下载最新那个** | 目录结构和索引全丢（文件本体还在，但要手工重新登记） |
| 4 | 新服务器 root 密码/IP | 服务商面板 | — |

> ⚠️ **顺带提醒**：第 1 项是**源码的唯一副本**。建议把 `C:\Users\Jrafina\Desktop\tg\` 传到一个私有 Git 仓库（注意 `set_token.sh` 和 README 里含明文凭据，先处理再传）。否则电脑出问题就是双重灾难。

---

## 2. 把项目文件传到新服务器

先用你的 SSH 工具登录新服务器，建目录：

```bash
mkdir -p /opt/tgpool-setup /opt/tgpool/app/static /opt/tgpool/tools
```

然后用你的 SFTP/SCP 工具（或 `scp -r`），按**下面的对应关系**上传。目录结构必须和这里一致，否则后面的脚本会找不到文件：

```
本地 tg/ 里                              →  服务器上
────────────────────────────────────────────────────────────
scripts/*.sh                             →  /opt/tgpool-setup/
server/tgpool.service                    →  /opt/tgpool-setup/tgpool.service
server/app.py                            →  /opt/tgpool/app/app.py
server/static/index.html                 →  /opt/tgpool/app/static/index.html
server/requirements.txt                  →  /opt/tgpool/app/requirements.txt
server/tools/rebuild_index.py            →  /opt/tgpool/tools/rebuild_index.py
server/tools/backup_index.py             →  /opt/tgpool/tools/backup_index.py
```

用 `scp` 的话，在**你的电脑上**执行（把 `新IP` 换掉）：

```bash
cd "C:/Users/Jrafina/Desktop/tg"
scp -r scripts/*.sh root@新IP:/opt/tgpool-setup/
scp -r server/tgpool.service root@新IP:/opt/tgpool-setup/
scp -r server/app.py server/requirements.txt root@新IP:/opt/tgpool/app/
scp -r server/static/index.html root@新IP:/opt/tgpool/app/static/
scp -r server/tools/*.py root@新IP:/opt/tgpool/tools/
```

**校验上传完整**（在服务器上执行）：

```bash
ls -la /opt/tgpool-setup/ /opt/tgpool/app/ /opt/tgpool/app/static/ /opt/tgpool/tools/
```

期望看到：`/opt/tgpool-setup/` 下有 8 个 `.sh` + `tgpool.service`；`app/` 下有 `app.py`、`requirements.txt`；`tools/` 下有 2 个 `.py`。

---

## 3. 基础环境

### 3.1 看机器规格

```bash
whoami; nproc; free -h; df -h /; cat /etc/os-release | head -2
```

参考原机器：1 核 / 971MB 内存 / 29G 磁盘 / Ubuntu 22.04。**内存小于 2G 就做下一步。**

### 3.2 加 swap（内存 < 2G 时必做）

```bash
if [ ! -f /swapfile ]; then
  fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "swap 已创建"
fi
free -h
```

期望：`Swap:` 一行显示 4.0Gi。

### 3.3 换 apt 源（国内服务器必做）

```bash
bash /opt/tgpool-setup/fix_apt.sh
```

脚本会自动探测发行版代号、**先测默认源**（通就不动，避免无谓改动）、不通才挑最快的国内镜像。

期望结尾：`apt-get update` 输出一堆 `Get:...` 而不是超时。如果显示「默认源可用，无需换源」，直接过。

### 3.4 装 Docker

```bash
bash /opt/tgpool-setup/install_docker.sh
```

`apt-get install docker.io` 要几分钟，**耐心等**。期望结尾：

```
docker ready
```

验证一下：

```bash
docker --version
docker run --rm hello-world | head -3
```

期望：打印 Docker 版本，`hello-world` 容器能跑起来。失败就先解决 Docker Hub 连通问题（脚本里已配了 4 个国内镜像加速）。

---

## 4. 初始化 Web 应用（建配置、虚拟环境、systemd）

```bash
bash /opt/tgpool-setup/init_webapp.sh
```

这一步会：

- 生成 `/opt/tgpool/tgpool.env`（**token 留空**，`TG_AUTH_PASS` 是**新随机生成**的 16 位密码）
- 装 `python3-venv` / `python3-pip`
- 建 `/opt/tgpool/venv` 并装 FastAPI 等依赖
- 注册 systemd 服务 `tgpool.service` 并设为开机自启
- **发现 token 为空 → 故意不启动**（正常）

**记下新的访问密码**：

```bash
grep TG_AUTH_PASS /opt/tgpool/tgpool.env
```

> 想沿用旧密码（`PGEHzEbbqLpwKlj3`），就直接改这一行；不想留旧密码就用新生成的。**这个密码之后要输进浏览器，别弄丢。**

---

## 5. 写入 bot token

两种办法，选一个：

**A. 用现成脚本**（`set_token.sh` 里已经写死了 token，先 `cat` 确认无误）

```bash
cat /opt/tgpool-setup/set_token.sh | sed -n '4p'    # 看一眼要写入的 token
bash /opt/tgpool-setup/set_token.sh
```

**B. 手动写**

```bash
sed -i 's|^TG_BOT_TOKEN=.*|TG_BOT_TOKEN=你的token|' /opt/tgpool/tgpool.env
chmod 600 /opt/tgpool/tgpool.env
```

**校验**（必须看到长度是 46）：

```bash
awk -F= '/^TG_BOT_TOKEN=/{print length($2)" 字符"}' /opt/tgpool/tgpool.env
```

token 丢了？Telegram 找 `@BotFather` → `/mybots` → 选你的 bot → **API Token**。能拿到原值。

---

## 6. 起 bot API server

```bash
bash /opt/tgpool-setup/setup_botapi.sh
```

期望结尾：`getMe` 返回类似

```json
{"ok":true,"result":{"id":8888005262,"is_bot":true,"username":"storage_pool_bot",...}}
```

看到 `"ok":true` 就说明 **token 有效、容器正常、到 Telegram 的网络通**。

> **这个环节最大的坑**：这个镜像的 entrypoint **只读环境变量，命令行参数会被静默忽略**。开本地模式必须靠 `-e TELEGRAM_LOCAL=1`（脚本里已经写好了）。如果漏掉，症状是**上传正常但下载 404**。
> 自查：`docker logs --tail 20 tg-bot-api`，启动参数里应该能看到 `--local`。

容器起来后确认一下：

```bash
docker ps --filter name=tg-bot-api
```

---

## 7. 起 Web 应用

```bash
systemctl restart tgpool
sleep 3
systemctl is-active tgpool
```

期望输出 `active`。然后本地探一下：

```bash
PASS=$(grep '^TG_AUTH_PASS=' /opt/tgpool/tgpool.env | cut -d= -f2)
curl -s -o /dev/null -w 'root=%{http_code}\n' -u "admin:$PASS" http://127.0.0.1:8080/
curl -s -u "admin:$PASS" http://127.0.0.1:8080/api/stats; echo
```

期望：`root=200`，`/api/stats` 返回 `{"count":0,"bytes":0,"folders":0,"chat_id":""}`。

> 此时 `count:0` 是**正常的**——索引还是空的，下一步才恢复。`chat_id` 为空也正常。

---

## 8. 恢复索引与目录结构 ★核心步骤

**8.1 先把备份包弄到服务器上**

在 Telegram 客户端打开和 `@storage_pool_bot` 的会话 → 往下翻到最新的一条 `tgpool-backup-YYYYmmdd-HHMMSS.tar.gz` → 下载到本地 → 再用 SFTP/`scp` 传到服务器 `/root/`。

> 整个过程**不需要任何 token**，就是普通聊天文件下载。

**8.2 先试跑，不碰线上**

```bash
mkdir -p /tmp/rbtest/tools && cp /opt/tgpool/tools/rebuild_index.py /tmp/rbtest/tools/
bash /opt/tgpool-setup/restore_bundle.sh /root/tgpool-backup-XXXX.tar.gz --app-dir /tmp/rbtest
```

期望关键几行：

```
  index.db : sha256 一致
  journal  : sha256 一致
[√] 校验通过，索引与 journal 完全一致
```

> `--app-dir` 模式下不会动 systemd 服务、不碰线上 index.db，纯粹验证备份包本身是好的。

**8.3 正式恢复**

```bash
bash /opt/tgpool-setup/restore_bundle.sh /root/tgpool-backup-XXXX.tar.gz
```

脚本会：停服务 → 原有 index.db 另存为 `index.db.pre-restore-<时间>` → 恢复 `index.db` 和 `journal` → 起服务 → 跑 `--verify`。

期望最后：

```
  tgpool: active
[√] 校验通过，索引与 journal 完全一致
完成。若上面显示「校验通过」，说明索引已完整恢复。
```

**8.4 确认文件真的回来了**

```bash
PASS=$(grep '^TG_AUTH_PASS=' /opt/tgpool/tgpool.env | cut -d= -f2)
curl -s -u "admin:$PASS" http://127.0.0.1:8080/api/stats; echo
```

期望：`count`（文件数）和 `folders`（目录数）**与你备份包 MANIFEST 里的统计一致**（原机器上是 3 个文件 / 1 个目录）。

---

## 9. 重新绑定 chat_id

新服务器的 `/opt/tgpool/tgpool.env` 里 `TG_CHAT_ID` 是空的，上传还没法工作——因为 bot 不知道往哪个会话写文件。

**9.1 先在 Telegram 里给 bot 发一条消息**（打开 `@storage_pool_bot`，发个 `hi`）

**9.2 然后跑**

```bash
bash /opt/tgpool-setup/detect_chat_id.sh
```

脚本会从 `getUpdates` 自动识别会话、写入配置、重启服务，并做一次 1KB 上传冒烟测试。

期望结尾返回类似 `{"id":1,"name":"_cid_probe.bin",...}`。

> 如果提示「没有任何更新」，说明第 9.1 步没做或没生效——发完消息再跑一次。

**9.3 删掉那个探测文件**

它现在会出现在网页文件列表里（1KB，叫 `_cid_probe.bin`），在网页上直接删掉即可——会同步从 Telegram 删除。

---

## 10. 配 HTTPS 入口

```bash
bash /opt/tgpool-setup/setup_nginx.sh
```

这一步**已做过适配，可以直接在全新机器上跑**：nginx 没装就自动装；找不到证书就自动生成 10 年有效的自签名证书（SAN 带上本机 IP）。

期望结尾：

```
完成。访问地址: https://<新IP>:8443/
```

检查监听：

```bash
ss -tlnp | grep -E ':8443'
```

**关于证书**：原机器用的是你的通配符证书 `o.jrafina.top`，新服务器上没有，所以脚本会生成自签名的。浏览器会警告「不安全」→ 点「高级 → 继续访问」。想换成你自己的域名证书，把 crt/key 放到 `/etc/nginx/ssl/o.jrafina.top.{crt,key}` 再重跑一次 `setup_nginx.sh` 即可自动沿用。

**⚠️ 端口要放行**：`8443` 必须在**云服务商的安全组/防火墙**里放行。新服务器往往是默认全关的，这一步很容易忘。

---

## 11. 启用灾备定时任务

```bash
bash /opt/tgpool-setup/install_backup.sh
```

它会：

- 建 `tools/` `journal/` `backup/` 目录
- 检查 journal —— **因为第 8 步已恢复 journal，这里会自动跳过 `--seed`**（正确行为）
- 写入 `/etc/cron.d/tgpool-backup`：**每天 03:30** 本地备份 + 上传 Telegram + 清理过期（保留 14 份）
- 配 logrotate 防止日志涨爆
- 立即跑一次备份并校验

期望结尾：

```
[√] 校验通过，索引与 journal 完全一致
完成。定时任务：
30 3 * * * root /opt/tgpool/venv/bin/python /opt/tgpool/tools/backup_index.py ...
```

> **不要跳过第 8 步直接做这一步**。如果 journal 是空的，脚本会自动执行 `--seed`——那会拿**空索引**导出日志起点，结果是"灾备能力有了，但历史文件索引永久丢失"。

---

## 12. 端到端验收

全部做完，跑这套检查。**每一条都要通过**：

```bash
PASS=$(grep '^TG_AUTH_PASS=' /opt/tgpool/tgpool.env | cut -d= -f2)
IP=$(hostname -I | awk '{print $1}')

# 1. 两个服务都活着
systemctl is-active tgpool && docker ps --filter name=tg-bot-api --format '{{.Names}} {{.Status}}'

# 2. 本地接口通
curl -s -u "admin:$PASS" http://127.0.0.1:8080/api/stats; echo

# 3. 外网入口通
curl -sk -o /dev/null -w 'page=%{http_code}\n' -u "admin:$PASS" https://$IP:8443/

# 4. 索引与日志一致
/opt/tgpool/venv/bin/python /opt/tgpool/tools/rebuild_index.py --verify | tail -3

# 5. 定时任务在
cat /etc/cron.d/tgpool-backup | tail -1

# 6. 真实上传 + 下载 + 完整性校验
head -c 3145728 /dev/urandom > /tmp/_e2e.bin
curl -sk -u "admin:$PASS" -F "file=@/tmp/_e2e.bin;filename=_e2e.bin" https://$IP:8443/api/upload; echo
ID=$(curl -sk -u "admin:$PASS" https://$IP:8443/api/list | python3 -c \
     'import json,sys; d=json.load(sys.stdin); print([f["id"] for f in d["files"] if f["name"]=="_e2e.bin"][0])')
curl -sk -u "admin:$PASS" -o /tmp/_e2e_dl.bin https://$IP:8443/api/download/$ID
sha256sum /tmp/_e2e.bin /tmp/_e2e_dl.bin     # 两行必须完全相同
curl -sk -u "admin:$PASS" -X DELETE https://$IP:8443/api/files/$ID; echo
rm -f /tmp/_e2e.bin /tmp/_e2e_dl.bin
```

**最后：打开浏览器** `https://<新IP>:8443/`，用 `admin` + 第 4 步记下的密码登录，确认文件列表和目录结构跟原机器一致。

---

## 13. 故障排查

| 症状 | 原因 | 处理 |
|---|---|---|
| `init_webapp.sh` 卡在 apt | 默认源不通 | 先跑 `fix_apt.sh`，再重跑 |
| `apt-get` 报 `Could not get lock` | 有残留 apt 进程 | `pkill -9 -x apt-get; pkill -9 -x dpkg` |
| `setup_botapi.sh` 拉镜像失败 | Docker Hub 不通 | 检查 `/etc/docker/daemon.json` 的镜像加速，`systemctl restart docker` |
| `getMe` 返回 `"ok":false` | token 错 或 容器没起 | `docker logs --tail 30 tg-bot-api` |
| **上传成功但下载 404** | `--local` 没生效 | `docker inspect tg-bot-api`，确认环境变量有 `TELEGRAM_LOCAL=1`；`docker logs` 里应能看到 `--local` |
| 页面能开但文件列表空 | 第 8 步没做或失败 | 重跑 `restore_bundle.sh`，看 `--verify` |
| 上传报错 / 卡住 | `TG_CHAT_ID` 为空 | 跑第 9 步 `detect_chat_id.sh` |
| 浏览器打不开 `https://IP:8443` | 安全组没放行 8443 | 去云服务商控制台加规则 |
| 浏览器证书警告 | 自签名证书 | 正常，点「高级 → 继续访问」 |
| `--verify` 报不一致 | journal 与索引不同步 | 跑 `venv/bin/python tools/rebuild_index.py` 重建 |
| 下载只有 ~145 KB/s | 服务器上行带宽限制 | 物理限制，代码层面无解 |

---

## 14. 降级情况：如果备份包也丢了

只能重建**服务**，索引从零开始：

1. 照常做到第 7 步（服务能跑）
2. 跳过第 8 步
3. 跑第 11 步 —— 它会自动 `--seed`，从空索引导出 journal 起点
4. **文件本体还在 Telegram 里**（打开和 bot 的会话可见全部文件），但网页上看不到、也无法自动登记
5. 想把老文件重新纳入管理，只能**逐个重新上传一遍**（下载→再上传）

**怎么避免落到这一步**：备份包每天自动发到你的 Telegram 会话。**别删那些消息**。它们是整盘损坏时唯一的索引来源。

## 15. 一句话记住的运维要点

- 平时**什么都不用做**（journal 自动写、每天 03:30 自动备份）
- 索引坏了 → `venv/bin/python tools/rebuild_index.py && systemctl restart tgpool`
- 服务器没了 → 照本文档从第 2 步做起
- **三样东西必须留在服务器之外**：本地 `tg/` 源码、bot token、Telegram 里的备份包
