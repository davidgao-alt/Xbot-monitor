# Xbot-monitor

监控一组 X (Twitter) 账号的最新帖子（含回复和转推），命中关键词（TRON / TRX / USDT …）就推送到 Telegram 群。

数据来源是 [SocialData API](https://socialdata.tools)（旧版走 nitter RSS，nitter 已失效）。

## 工作原理

每次运行（由 cron / systemd 定时触发）：

1. 预检 Telegram：bot token 和群 ID 可用才继续，避免花 SocialData 额度抓了却发不出去。
2. 把账号按 `ACCOUNTS_PER_QUERY`（默认 10 个）合并成一个搜索：
   `(from:a OR from:b ...) include:nativeretweets since_time:<上次运行时间>`，自动翻页。
3. **增量抓取**：每个账号记录上次成功抓取的时间（`data/state.json`），下次只抓这之后的新帖（再往前多查 `OVERLAP_SECONDS` 秒，防止搜索索引延迟漏帖）。
   第一次运行、新加的账号、或停机很久之后，最多回看 `LOOKBACK_HOURS` 小时。
4. 关键词匹配原文。转推匹配被转的原文，引用推匹配自己的文字和被引用的帖子。
5. 按时间从旧到新发到 Telegram；发送成功才记入 `data/seen_ids.json`，去重。
   转推按**原帖 ID** 去重：同一条帖子被多个监控账号转推，也只推一次。
6. SocialData 余额不足 / key 无效时立即停止本次运行，并发一条告警到 Telegram（`ALERT_COOLDOWN_HOURS` 内只发一次）。

## 目录结构

```
.
├── monitor.py            # 主程序
├── config/
│   ├── accounts.json     # 监控的账号列表
│   └── keywords.json     # 关键词列表
├── tests/                # 单元测试
├── .env.example          # 配置模板 → 复制成 .env
├── requirements.txt
└── data/                 # 运行时状态，自动创建，不进 git
    ├── seen_ids.json     #   已推送的帖子 ID
    ├── state.json        #   每个账号的抓取进度、告警冷却
    └── monitor.lock      #   防止两次运行重叠
```

## 快速开始

需要 Python 3.8+。

```bash
git clone https://github.com/davidgao-alt/Xbot-monitor.git
cd Xbot-monitor

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
nano .env        # 填 SOCIALDATA_API_KEY / TG_BOT_TOKEN / TG_CHAT_ID

# 试运行：只打印命中的帖子，不发 Telegram、不写状态
.venv/bin/python monitor.py --dry-run

# 正式运行一次
.venv/bin/python monitor.py
```

> `TG_CHAT_ID` 获取方法：把 bot 拉进群，在群里发一条消息，然后打开
> `https://api.telegram.org/bot<TOKEN>/getUpdates`，找 `"chat":{"id":-100...}`。

## 配置 (.env)

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SOCIALDATA_API_KEY` | **必填** | SocialData API key |
| `TG_BOT_TOKEN` | **必填** | Telegram bot token（@BotFather） |
| `TG_CHAT_ID` | **必填** | 推送的目标群 / 频道 |
| `TG_ALERT_CHAT_ID` | = `TG_CHAT_ID` | 错误告警发到哪里，建议设成自己的私聊 |
| `ACCOUNTS_FILE` | `config/accounts.json` | 账号列表 |
| `KEYWORDS_FILE` | `config/keywords.json` | 关键词列表 |
| `DATA_DIR` | `data` | 运行时状态目录 |
| `SEEN_FILE` | `data/seen_ids.json` | 已推送 ID（兼容旧版的纯列表格式） |
| `STATE_FILE` | `data/state.json` | 抓取进度 |
| `LOOKBACK_HOURS` | `24` | 首次运行 / 新账号 / 停机后最多回看多久 |
| `OVERLAP_SECONDS` | `120` | 每次多往前查多少秒，防止索引延迟漏帖 |
| `ACCOUNTS_PER_QUERY` | `10` | 几个账号合并成一个搜索；设 `1` 就是逐个账号查 |
| `MAX_PAGES_PER_QUERY` | `15` | 每个搜索最多翻几页（约 20 条/页），防止意外烧钱 |
| `SOCIALDATA_REQUEST_DELAY` | `0.7` | 请求间隔（秒），SocialData 限制 120 次/分钟 |
| `MATCH_QUOTED_TWEETS` | `true` | 是否也匹配被引用帖子的内容 |
| `TG_SEND_DELAY` | `1.5` | Telegram 发送间隔（秒） |
| `SEEN_RETENTION_DAYS` | `30` | 已推送 ID 保留多少天 |
| `ALERT_COOLDOWN_HOURS` | `6` | 同一个错误告警的最短间隔 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

相对路径都是相对仓库目录。也可以用 `--env-file /path/to/.env` 指定别的配置文件。

## 账号和关键词

`config/accounts.json`：账号 handle 列表，不带 `@`（带了也会自动去掉），大小写不敏感，重复的会自动跳过。

`config/keywords.json`：关键词列表，不区分大小写。

- 以 `@` 开头的（如 `@trondao`）：按子串匹配。
- 其他（单词、短语、`$TRX`、`#TRON`）：前后不能紧挨字母/数字/下划线。
  所以 `tron` 能匹配 `TRON`、`#tron`、`$TRON`，但不会匹配 `electronic`、`tronscan`。
  也因此 `tron` 已经覆盖了 `#tron` 和 `$tron`，后两个写不写都一样。

改完两个文件不需要重启，下次运行自动生效。

## 部署（服务器）

### cron（推荐）

```cron
*/5 * * * * cd /root/Xbot-monitor && .venv/bin/python monitor.py >> monitor.log 2>&1
```

程序自带文件锁：上一次还没跑完，下一次会直接退出，不会重复推送。

日志会一直增长，可以加一个 logrotate 配置 `/etc/logrotate.d/xbot-monitor`：

```
/root/Xbot-monitor/monitor.log {
    weekly
    rotate 4
    compress
    missingok
    copytruncate
}
```

### systemd timer（可选）

`/etc/systemd/system/xbot-monitor.service`

```ini
[Unit]
Description=XBot monitor (X -> Telegram)
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/root/Xbot-monitor
ExecStart=/root/Xbot-monitor/.venv/bin/python monitor.py
```

`/etc/systemd/system/xbot-monitor.timer`

```ini
[Unit]
Description=Run XBot monitor every 5 minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
```

```bash
systemctl daemon-reload
systemctl enable --now xbot-monitor.timer
journalctl -u xbot-monitor -f     # 看日志
```

## 从旧版迁移

旧版文件在 `/root/tronbot/`。想沿用旧的去重记录、避免重复推送，在 `.env` 里指过去即可（旧的列表格式会自动转换）：

```env
SEEN_FILE=/root/tronbot/seen_ids.json
ACCOUNTS_FILE=/root/tronbot/accounts.json   # 如果还想用服务器上那份账号列表
```

然后把 crontab 里的 `monitor-Xpost-ssh.py` 换成 `monitor.py`。注意 seen 文件会被改写成新格式（`{id: 时间}`），之后旧脚本就不能再用它了。

第一次运行会回看 24 小时，把旧版漏掉的命中帖补发出来（旧版每个账号只看最新 10 条）。不想补发的话，第一次运行前临时设 `LOOKBACK_HOURS=1`。

## 费用

SocialData 按返回的推文条数收费，约 **$0.0002 / 条**。每次运行结束的 `RUN SUMMARY` 会打印本次返回了多少条和估算费用。

- 旧版：每次运行每个账号都重新拉最近 24 小时的第一页（最多 20 条）。57 个账号每次最多约 1100 条，约 $0.23。每 10 分钟跑一次的话，一天最多约 $33。
- 新版：只拉上次运行之后的新帖，每天费用 ≈ 这些账号每天实际发帖量 ×（1 + `OVERLAP_SECONDS` / 运行间隔秒数）× $0.0002。比如每 5 分钟跑一次、重叠 120 秒，大约是实际发帖量的 1.4 倍。

## 测试

```bash
python -m unittest discover tests
```

测试不访问网络：SocialData 和 Telegram 都是模拟的。

## 安全

- 所有 key 只放 `.env`，`.env` 已在 `.gitignore` 里，不要提交。
- 以前提交到仓库历史里的 Telegram bot token 应视为已泄露：在 @BotFather 里 `/revoke` 重新生成，再更新 `.env`。
