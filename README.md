# Xbot-monitor

Watches the latest posts (including replies and retweets) from a list of X (Twitter) accounts and forwards the ones that match a keyword list to a Telegram group.

Data comes from the [SocialData API](https://socialdata.tools). The old version used nitter RSS, which no longer works.

## How it works

Each run (triggered by cron or a systemd timer):

1. Telegram preflight: it only continues if the bot token and chat ID work, so no SocialData credits are spent on posts that can't be delivered.
2. Accounts are combined into searches of `ACCOUNTS_PER_QUERY` accounts each (default 10):
   `(from:a OR from:b ...) include:nativeretweets since_time:<last run>`, with automatic pagination.
3. **Incremental fetching**: the time of each account's last successful fetch is stored in `data/state.json`, and the next run only fetches posts after that (plus `OVERLAP_SECONDS` extra, so posts aren't missed when the search index lags).
   On the first run, for newly added accounts, or after long downtime, it looks back at most `LOOKBACK_HOURS` hours.
4. Keywords are matched against the post text. For retweets, the original post is matched; for quote posts, both the post's own text and the quoted post are matched.
5. Matches are sent to Telegram oldest first. A post is recorded in `data/seen_ids.json` only after it was sent successfully, which is used for de-duplication.
   Retweets are de-duplicated by the **original post ID**: a post retweeted by several monitored accounts is only sent once.
6. If the SocialData balance runs out or the API key is invalid, the run stops immediately and an alert is sent to Telegram (at most once per `ALERT_COOLDOWN_HOURS`).

## Layout

```
.
├── monitor.py            # main script
├── config/
│   ├── accounts.json     # accounts to monitor
│   └── keywords.json     # keywords to match
├── tests/                # unit tests
├── .env.example          # config template → copy to .env
├── requirements.txt
└── data/                 # runtime state, created automatically, not in git
    ├── seen_ids.json     #   IDs of posts already sent
    ├── state.json        #   per-account fetch progress, alert cooldowns
    └── monitor.lock      #   prevents overlapping runs
```

## Quick start

Requires Python 3.8+.

```bash
git clone https://github.com/davidgao-alt/Xbot-monitor.git
cd Xbot-monitor

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
nano .env        # fill in SOCIALDATA_API_KEY / TG_BOT_TOKEN / TG_CHAT_ID

# Dry run: only prints matching posts, sends nothing to Telegram, writes no state
.venv/bin/python monitor.py --dry-run

# A real run
.venv/bin/python monitor.py
```

> To find `TG_CHAT_ID`: add the bot to the group, send a message in the group, then open
> `https://api.telegram.org/bot<TOKEN>/getUpdates` and look for `"chat":{"id":-100...}`.

## Configuration (.env)

| Variable | Default | Description |
| --- | --- | --- |
| `SOCIALDATA_API_KEY` | **required** | SocialData API key |
| `TG_BOT_TOKEN` | **required** | Telegram bot token (from @BotFather) |
| `TG_CHAT_ID` | **required** | Group / channel that receives the posts |
| `TG_ALERT_CHAT_ID` | = `TG_CHAT_ID` | Where error alerts go; your own private chat is a good choice |
| `ACCOUNTS_FILE` | `config/accounts.json` | Account list |
| `KEYWORDS_FILE` | `config/keywords.json` | Keyword list |
| `DATA_DIR` | `data` | Runtime state directory |
| `SEEN_FILE` | `data/seen_ids.json` | IDs of sent posts (the old plain-list format is still read) |
| `STATE_FILE` | `data/state.json` | Fetch progress |
| `LOOKBACK_HOURS` | `24` | Maximum look-back on the first run, for new accounts, or after downtime |
| `OVERLAP_SECONDS` | `120` | Extra seconds re-checked each run, so posts aren't missed when the search index lags |
| `ACCOUNTS_PER_QUERY` | `10` | Accounts combined into one search; `1` queries each account separately |
| `MAX_PAGES_PER_QUERY` | `15` | Maximum result pages per search (~20 posts per page), a guard against runaway costs |
| `SOCIALDATA_REQUEST_DELAY` | `0.7` | Seconds between requests; SocialData allows 120 requests/minute |
| `MATCH_QUOTED_TWEETS` | `true` | Also match the content of quoted posts |
| `TG_SEND_DELAY` | `1.5` | Seconds between Telegram messages |
| `SEEN_RETENTION_DAYS` | `30` | How many days sent post IDs are kept |
| `ALERT_COOLDOWN_HOURS` | `6` | Minimum hours between repeats of the same error alert |
| `LOG_LEVEL` | `INFO` | Log level |

Relative paths are resolved against the repo directory. Use `--env-file /path/to/.env` to load a different config file.

## Accounts and keywords

`config/accounts.json`: a list of account handles without `@` (a leading `@` is stripped automatically). Handles are case-insensitive, and duplicates are skipped.

`config/keywords.json`: a list of keywords, matched case-insensitively.

- Keywords starting with `@` (e.g. `@trondao`) match as a plain substring.
- Everything else (words, phrases, `$TRX`, `#TRON`) must not be directly preceded or followed by a letter, digit or underscore.
  So `tron` matches `TRON`, `#tron` and `$TRON`, but not `electronic` or `tronscan`.
  This also means `tron` already covers `#tron` and `$tron`; listing those too makes no difference.

Changes to either file take effect on the next run; no restart is needed.

## Deployment (server)

### cron (recommended)

```cron
*/5 * * * * cd /root/Xbot-monitor && .venv/bin/python monitor.py >> monitor.log 2>&1
```

The script holds a file lock: if the previous run is still going, the next one exits right away, so nothing is sent twice.

The log keeps growing. A logrotate config at `/etc/logrotate.d/xbot-monitor` keeps it in check:

```
/root/Xbot-monitor/monitor.log {
    weekly
    rotate 4
    compress
    missingok
    copytruncate
}
```

### systemd timer (optional)

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
journalctl -u xbot-monitor -f     # follow the logs
```

## Migrating from the old version

The old version's files are in `/root/tronbot/`. To keep its de-duplication history and avoid re-sending posts, point `.env` at them (the old list format is converted automatically):

```env
SEEN_FILE=/root/tronbot/seen_ids.json
ACCOUNTS_FILE=/root/tronbot/accounts.json   # if you want to keep using the account list on the server
```

Then replace `monitor-Xpost-ssh.py` with `monitor.py` in your crontab. Note that the seen file is rewritten in the new format (`{id: timestamp}`), so the old script can't use it afterwards.

The first run looks back 24 hours and sends matching posts the old version missed (it only checked the latest 10 posts per account). To skip that, set `LOOKBACK_HOURS=1` for the first run.

## Cost

SocialData charges per post returned, about **$0.0002 per post**. The `RUN SUMMARY` at the end of each run shows how many posts were returned and the estimated cost.

- Old version: every run re-fetched the first page (up to 20 posts) of the last 24 hours for every account. With 57 accounts that is up to ~1,100 posts per run, about $0.23. Running every 10 minutes, that comes to up to ~$33 per day.
- New version: only posts since the previous run are fetched. Daily cost ≈ posts the accounts actually publish per day × (1 + `OVERLAP_SECONDS` / run interval in seconds) × $0.0002. For example, running every 5 minutes with a 120-second overlap costs about 1.4× the actual number of posts.

## Tests

```bash
python -m unittest discover tests
```

The tests make no network calls; SocialData and Telegram are mocked.

## Security

- Keep all keys in `.env` only. `.env` is in `.gitignore`; don't commit it.
- Treat any Telegram bot token that was committed to the repo history as leaked: revoke it with `/revoke` in @BotFather, generate a new one and update `.env`.
