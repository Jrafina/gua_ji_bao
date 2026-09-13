# pkmkv 电影更新监控 + 邮件提醒

定时抓取 `https://www.pkmkv.com/` 首页「电影推荐」列表（前 6 项），与上一次结果对比，
一旦出现**新增 / 下架 / 画质变化 / 片名变化**，就通过 **Resend API** 发一封 HTML 邮件提醒。

- 抓取目标：`//*[@id="change-ul-1"]/li[i]/div[1]/a/img`
- 记录字段：**电影名称** + **资质**（HD / TC / 超清 …）+ 详情链接 + 海报
- 触发时间：每天 **08:00 / 12:00 / 18:00 / 24:00**（默认 Asia/Shanghai 时区，即每 6 小时一次）
- 发件人：`notify@1795857.xyz`（Resend 第三方代发，无需 SMTP 授权码，直接用 API Key）
- 收件人：`1795857787@qq.com`

---

## 1. 目录结构

```
movie-notify/
├── movie_notify.py         # 主程序（纯标准库 + requests，无需额外安装依赖）
├── config.json             # 配置（时区/时间点/收发件人/开关）
├── movie-notify.service    # systemd 常驻方案
├── crontab.example         # crontab 常驻方案（二选一）
├── state.json              # 最近一次快照（自动生成，用于下次对比）
├── history/history.jsonl   # 历史快照流水（自动生成，每行一条）
└── logs/movie_notify.log   # 运行日志（自动生成，自动轮转 2MB×3）
```

## 2. 快速开始

```bash
cd /root/movie-notify

# 1) 填入 Resend Key 并发测试邮件
RESEND_API_KEY="re_xxx" python3 movie_notify.py --test-email

# 2)在config里面填写resend api

# 3) 常驻（systemd，Key 写进 unit 的 Environment= 行）
sudo cp movie-notify.service /etc/systemd/system/
sudo systemctl enable --now movie-notify
```

## 3. 命令行参数

```bash
python3 movie_notify.py --once          # 立即抓取一次（cron 方案用这个）
python3 movie_notify.py                 # 守护进程，按 8/12/18/24 点定时执行
python3 movie_notify.py --baseline      # 强制刷新基线，不发邮件
python3 movie_notify.py --test-email    # 发测试邮件验证 Resend 配置
python3 movie_notify.py --status        # 打印当前快照内容
python3 movie_notify.py -v              # DEBUG 日志
```

## 4. 配置项（`config.json`）

| 字段 | 默认值 | 说明 |
|---|---|---|
| `url` | `https://www.pkmkv.com/` | 目标页面 |
| `count` | `6` | 只取前几项 |
| `tz` | `Asia/Shanghai` | 调度时区 |
| `schedule_hours` | `[8, 12, 18, 0]` | 每天几点触发（24 点写成 `0`） |
| `from` | `notify@1795857.xyz` | Resend 发件人 |
| `to` | `["1795857787@qq.com"]` | 收件人，可多个 |
| `subject_prefix` | `[pkmkv 更新]` | 邮件标题前缀 |
| `resend_api_key` | `""` | Resend API Key（建议改用环境变量） |
| `dry_run` | `false` | `true` 时只打印不发送；**未配置 Key 时会自动退化成 dry-run** |
| `notify_on_order_change` | `false` | 内容相同、仅顺序变化时是否也发邮件 |
| `cover_image_in_email` | `true` | 邮件里是否内嵌海报图 |
| `http_timeout` / `max_retries` / `retry_backoff` | `25` / `3` / `5` | 请求超时、重试次数、重试间隔基数 |
| `state_file` | `state.json` | 快照文件 |
| `history_file` | `history/history.jsonl` | 历史流水 |
| `log_file` | `logs/movie_notify.log` | 日志文件 |

### 环境变量（优先级最高，可覆盖 config.json）

| 变量 | 对应配置 |
|---|---|
| `RESEND_API_KEY` | `resend_api_key` |
| `NOTIFY_FROM` | `from` |
| `NOTIFY_TO` | `to`（逗号分隔） |
| `NOTIFY_URL` | `url` |
| `NOTIFY_COUNT` | `count` |
| `NOTIFY_TZ` | `tz` |
| `NOTIFY_HOURS` | `schedule_hours`（逗号分隔，如 `8,12,18,0`） |
| `NOTIFY_DRY_RUN` | `dry_run` |
| `NOTIFY_STATE` | `state_file` |

## 5. 通知逻辑

1. **首次运行**：写入基线快照，不发邮件。
2. **后续运行**：以「详情页 URL」为唯一标识，与上次快照对比：
   - `新增`：这次有、上次没有
   - `下架`：上次有、这次没有
   - `画质变化`：同一部片子，`HD` → `TC` 之类
   - `片名变化`：同一链接换了名字
3. 邮件标题示例：`[pkmkv 更新]有更新：新增 1、画质变化 2 - 2026-09-09 12:00 CST`
4. **去重保护**：同一份内容指纹只发一次，避免列表来回抖动时刷邮件；
   内容相同但顺序变化时默认不发（可用 `notify_on_order_change` 打开）。
5. **抓取失败不会污染快照**：请求或解析出错直接跳过本轮，保留上次结果。
6. 抓取前 6 项时若某项结构异常，只跳过该项并在日志留痕。

> 已验证该列表在多次请求下**内容稳定**（不是每次随机刷新），所以对比结果是有意义的。

## 6. 常见问题

- **邮件没收到**
  1. 日志里看是否有 `邮件发送失败：Resend 返回 ...`，把返回原文贴到 Resend 工单里最快。
  2. `notify@1795857.xyz` 的域名必须在 Resend 后台验证过，否则报 `Sender address is not verified`。
  3. QQ 邮箱把新域名发件人拦进垃圾箱是常态，记得看「垃圾箱」并把 `notify@1795857.xyz` 加进通讯录/白名单。
  4. 先跑 `--test-email` 排除脚本问题，再排查投递问题。
- **提示 `未配置 resend_api_key`**：环境变量没传进进程。systemd 方案要写进 unit 的
  `Environment=RESEND_API_KEY=*** 直接写。
- **提示找不到 `id="change-ul-1"`**：页面改版了。改 `extract_movies()` 里的选择逻辑即可，
  脚本会整轮跳过而不写坏快照。
- **想改成每小时/每天一次**：`schedule_hours` 改成 `[]` 不行，改成比如 `[0]`（每天 0 点）；
  需要「每 N 小时」可把 hours 写全，如 `[0,6,12,18]`。
- **历史流水想清掉**：`rm -f history/history.jsonl`（不影响下次对比）。
- **想重新基线**：`python3 movie_notify.py --baseline`。

## 7. 手动验证脚本自身

```bash
cd /root/movie-notify
python3 -m py_compile movie_notify.py        # 语法检查
python3 movie_notify.py --status             # 看当前快照
tail -n 50 logs/movie_notify.log             # 看日志
```
