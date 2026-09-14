# 最坏情况：老机器和新机器同时报废

> 场景：两台服务器在同一时间段都不可用了，一个都连不上。
> 本文只回答一个问题：**文件还在吗？索引还能回来吗？接下来做什么？**

---

## 结论：能完整恢复，不用慌

这套设计从一开始就把「异地副本」放在 **Telegram 云端本身**，而不是放在某台服务器上。
所以「服务器全挂」恰好是它被设计来应对的那种情况。

| 东西 | 存在哪 | 两台机器全挂会怎样 |
|---|---|---|
| **文件本体** | Telegram 云端（`file_id` 是云端 id，长期有效） | ✅ 完全不受影响 |
| **目录结构 + 索引** | **Telegram 云端**（每天 03:30 上传的 `tgpool-backup-*.tar.gz`） | ✅ 完全不受影响 |
| **服务源码** | Telegram 云端（已存一份 `tgpool-deploy.tar.gz`） | ✅ 不受影响 |
| bot token | `@BotFather` 随时可查 | ✅ 不受影响 |

### 这一点已实测确认

拿**官方 `api.telegram.org`**（完全绕开你自己的服务器）按 `file_id` 查备份包：

```json
{"ok":true,"result":{"file_size":2295,"file_path":"documents/file_1"}}
```

返回 `ok:true` + 有效 `file_path`，说明备份包**真实存在 Telegram 云端**，
任何一台能上网的机器都能取回，和你的服务器活不活着无关。

---

## 恢复步骤（约 20 分钟）

**你手上要有的三样东西：**

1. 一台能重装的 Ubuntu（任意 VPS，1 核 1G 起）
2. `tgpool-deploy.tar.gz` —— **在 Telegram 里**（和 `@storage_pool_bot` 的会话中，翻到最新那条）
3. bot token —— `@BotFather` → `/mybots` → 你的 bot → **API Token**

### 第 1 步 · 部署服务底座

```bash
# 从 Telegram 下载 tgpool-deploy.tar.gz，传到新机 /root/，然后：
cd /root && tar xzf tgpool-deploy.tar.gz && cd tgpool-deploy
bash deploy.sh          # 交互填 token；chat_id 先留空，第 4 步自动探测
```

### 第 2 步 · 去 Telegram 捞索引备份 ★核心步骤

打开 Telegram（手机 / 电脑都行）→ 和 `@storage_pool_bot` 的会话 →
往上翻，找到最新一条 **`tgpool-backup-XXXXXXXX-XXXXXX.tar.gz`** → 下载到本地 →
再传回新服务器的 `/root/`。

> 这一步**不需要任何 token**，就是普通聊天文件下载。
> 会话里会有很多这种 `.tar.gz` 和你平时存的文件混在一起 —— **它们不能删**，
> 它们是索引唯一的云端副本。系统每天自动传 1 份、滚动保留最近 14 份。

### 第 3 步 · 用备份包恢复索引

```bash
cd /root/tgpool-deploy
bash deploy.sh --bundle /root/tgpool-backup-XXXXXXXX-XXXXXX.tar.gz
```

跑完网页上就能看到原来的目录和文件了。

### 第 4 步 · 绑定 chat_id（第 1 步留空的话）

先在 Telegram 给 bot 发一条 `hi`，然后重跑：

```bash
bash /root/tgpool-deploy/deploy.sh
```

脚本会自动进入探测流程并做一次上传冒烟测试。

### 第 5 步 · 验收

浏览器打开 `https://<新IP>:8443/`，用 `admin` + 密码登录
（密码在 `deploy.sh` 结尾打印过，忘了就 `python3 /opt/tgpool/tools/show_password.py`，
或直接在 Telegram 里发 `/pass`），确认文件数 / 目录数与备份包一致。

完整验收清单见 [REBUILD.md](REBUILD.md) 第 12 节。

> **密码是每台机器各自生成的**：`tgpool.env` 故意不进备份包（里面有 bot token 等凭据）。
> 所以重建出来的新机器密码是**新随机密码**，和旧机器不一样 —— 用上面两条命令随时能查到。

---

## 最坏时间点：索引最多丢 1 天

备份是**每天 03:30**（服务器时间）跑一次。如果两台机器正好在两次备份之间同时报废：

- **文件本体**：一份不丢（每次上传都是即时进云端的）
- **索引**：最多回退到上一次备份 —— 那之后新增 / 改名 / 移动 / 删除的文件会丢登记
  （文件本体仍在 Telegram 里可见，只是网页列表里没有，需要重新上传登记）

想把这个窗口缩到最小，可以在还活着的机器上手动补一次：

```bash
/opt/tgpool/venv/bin/python /opt/tgpool/tools/backup_index.py
```

---

## 平时唯一要记住的一件事

**别删和 bot 会话里的这两类消息：**

| 消息 | 作用 |
|---|---|
| `tgpool-backup-*.tar.gz` | 索引的云端副本 —— 服务器全毁时**唯一**的来源，每天 03:30 新增一份，滚动保留 14 份 |
| `tgpool-deploy.tar.gz` | 服务源码 —— 电脑也挂了的时候，靠它重建整套服务 |

> 补充：服务器本地的 `documents/` 文件缓存**丢了也无所谓**，它只是为了加速重复下载；
> 真正的文件本体一直在 Telegram 云端。

---

## 相关文档

- [REBUILD.md](REBUILD.md) — 换单台服务器的完整分步 runbook（含期望输出、故障排查）
- [DEPLOY.md](DEPLOY.md) — 一键部署脚本用法
- [README.md](README.md) — 项目总览与日常运维
