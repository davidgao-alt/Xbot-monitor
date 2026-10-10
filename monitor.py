#!/usr/bin/env python3
"""
XBot Monitor

Watches a list of X (Twitter) accounts through the SocialData API and
forwards posts that match a keyword list to a Telegram chat.

Meant to be run periodically (cron / systemd timer). Each run only asks
SocialData for posts newer than the previous successful run of that
account, so every tweet is paid for roughly once instead of re-downloading
the last 24 hours on every run.

    python monitor.py             # normal run
    python monitor.py --dry-run   # print matches only: no Telegram, no state written

All settings come from environment variables / .env (see .env.example).
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
import time

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
except ImportError:  # optional: env vars can also come from cron/systemd
    load_dotenv = None


BASE_DIR = Path(__file__).resolve().parent

SOCIALDATA_SEARCH_URL = "https://api.socialdata.tools/twitter/search"

# SocialData bills per tweet returned; only used for the cost estimate.
SOCIALDATA_PRICE_PER_TWEET = 0.0002

# Telegram counts its 4096 limit in UTF-16 code units, not Python chars.
TG_MESSAGE_LIMIT = 4096

# Stay well below X's search query length limit when OR-ing accounts.
MAX_QUERY_CHARS = 400

# Accounts whose watermarks are this close (seconds) can share one query.
BATCH_MERGE_WINDOW = 15 * 60

# A valid X handle: 1-15 letters, digits or underscores.
HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")


log = logging.getLogger("xbot-monitor")

session = requests.Session()


class ConfigError(Exception):
    pass


class FatalAPIError(Exception):
    """An error every following request would hit too (bad key, no balance)."""


# =========================================================
# CONFIG
# =========================================================

def _env(name, default=None):
    value = os.getenv(name)

    if value is None or not value.strip():
        return default

    return value.strip()


def _env_number(name, default, cast):
    value = _env(name)

    if value is None:
        return default

    try:
        return cast(value)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {value!r}")


def _env_bool(name, default):
    value = _env(name)

    if value is None:
        return default

    return value.lower() in ("1", "true", "yes", "on")


def _env_path(name, default):
    """Relative paths are resolved against the repo directory."""
    value = _env(name)
    path = Path(value).expanduser() if value else Path(default)

    return path if path.is_absolute() else BASE_DIR / path


@dataclass
class Config:
    socialdata_api_key: str
    tg_bot_token: str
    tg_chat_id: str
    tg_alert_chat_id: str

    accounts_file: Path
    keywords_file: Path
    seen_file: Path
    state_file: Path
    lock_file: Path

    lookback_hours: float
    overlap_seconds: int
    accounts_per_query: int
    max_pages_per_query: int
    request_delay: float

    match_quoted_tweets: bool
    tg_send_delay: float
    seen_retention_days: float
    alert_cooldown_hours: float

    @classmethod
    def from_env(cls, require_telegram=True):
        data_dir = _env_path("DATA_DIR", "data")

        cfg = cls(
            socialdata_api_key=_env("SOCIALDATA_API_KEY", ""),
            tg_bot_token=_env("TG_BOT_TOKEN", ""),
            tg_chat_id=_env("TG_CHAT_ID", ""),
            tg_alert_chat_id=(
                _env("TG_ALERT_CHAT_ID")
                or _env("TG_CHAT_ID", "")
            ),

            accounts_file=_env_path("ACCOUNTS_FILE", "config/accounts.json"),
            keywords_file=_env_path("KEYWORDS_FILE", "config/keywords.json"),
            seen_file=_env_path("SEEN_FILE", data_dir / "seen_ids.json"),
            state_file=_env_path("STATE_FILE", data_dir / "state.json"),
            lock_file=data_dir / "monitor.lock",

            lookback_hours=_env_number("LOOKBACK_HOURS", 24, float),
            overlap_seconds=_env_number("OVERLAP_SECONDS", 120, int),
            accounts_per_query=_env_number("ACCOUNTS_PER_QUERY", 10, int),
            max_pages_per_query=_env_number("MAX_PAGES_PER_QUERY", 15, int),
            request_delay=_env_number("SOCIALDATA_REQUEST_DELAY", 0.7, float),

            match_quoted_tweets=_env_bool("MATCH_QUOTED_TWEETS", True),
            tg_send_delay=_env_number("TG_SEND_DELAY", 1.5, float),
            seen_retention_days=_env_number("SEEN_RETENTION_DAYS", 30, float),
            alert_cooldown_hours=_env_number("ALERT_COOLDOWN_HOURS", 6, float),
        )

        required = ["SOCIALDATA_API_KEY"]

        if require_telegram:
            required += ["TG_BOT_TOKEN", "TG_CHAT_ID"]

        missing = [name for name in required if not _env(name)]

        if missing:
            raise ConfigError(
                f"Missing required setting(s): {', '.join(missing)}. "
                f"Copy .env.example to .env and fill them in."
            )

        if cfg.accounts_per_query < 1 or cfg.max_pages_per_query < 1:
            raise ConfigError(
                "ACCOUNTS_PER_QUERY and MAX_PAGES_PER_QUERY must be >= 1"
            )

        return cfg


def redact(cfg, text):
    """Keep secrets out of logs (the bot token is part of Telegram URLs)."""
    text = str(text)

    for secret in (cfg.tg_bot_token, cfg.socialdata_api_key):
        if secret and len(secret) >= 8:
            text = text.replace(secret, "***")

    return text


# =========================================================
# FILES
# =========================================================

def load_json(path, default):
    if not path.exists():
        return default

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    except ValueError as e:
        # Keep the broken file for inspection instead of crashing every run
        backup = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        os.replace(path, backup)

        log.error(
            f"[STATE] {path} is not valid JSON ({e}); "
            f"moved it to {backup.name} and starting fresh"
        )

        return default


def save_json(path, data, indent=None):
    # Atomic write: never leaves a half-written file behind
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_file = path.with_name(path.name + ".tmp")

    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, sort_keys=True)

    os.replace(temp_file, path)


def load_config_list(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise ConfigError(f"File not found: {path}")
    except ValueError as e:
        raise ConfigError(f"{path} is not valid JSON: {e}")

    if not isinstance(data, list):
        raise ConfigError(f"{path} must contain a JSON list")

    return data


def load_accounts(path):
    raw_accounts = load_config_list(path)

    accounts = []
    seen_lower = set()

    for entry in raw_accounts:
        name = str(entry).strip().lstrip("@")

        if not HANDLE_RE.match(name):
            log.warning(f"[ACCOUNTS] Skipping invalid handle {entry!r}")
            continue

        # X handles are case-insensitive
        if name.lower() in seen_lower:
            log.warning(f"[ACCOUNTS] Skipping duplicate {name!r}")
            continue

        seen_lower.add(name.lower())
        accounts.append(name)

    if not accounts:
        raise ConfigError(f"No valid accounts in {path}")

    return accounts


def load_keywords(path):
    raw_keywords = load_config_list(path)

    keywords = [str(k).strip() for k in raw_keywords if str(k).strip()]

    if not keywords:
        raise ConfigError(f"No keywords in {path}")

    return keywords


def load_seen(path):
    """
    {dedupe_id: unix time it was sent}.
    Also reads the old format (a plain list of ids).
    """
    data = load_json(path, {})
    now = int(time.time())

    if isinstance(data, list):
        return {str(x): now for x in data}

    if isinstance(data, dict):
        return {
            str(k): int(v) if isinstance(v, (int, float)) else now
            for k, v in data.items()
        }

    return {}


def prune_seen(seen, retention_days):
    cutoff = time.time() - retention_days * 86400
    stale = [k for k, sent_at in seen.items() if sent_at < cutoff]

    for k in stale:
        del seen[k]

    return len(stale)


def load_state(path):
    """
    accounts: {handle_lower: unix time of the last successful fetch}
    alerts:   {alert_key: unix time the alert was last sent}
    """
    state = load_json(path, {})

    if not isinstance(state, dict):
        state = {}

    state.setdefault("accounts", {})
    state.setdefault("alerts", {})

    return state


@contextmanager
def single_instance(lock_path):
    """
    Exclusive lock for the whole run, so a slow run and the next cron
    run can't both send the same post.
    """
    try:
        import fcntl
    except ImportError:  # Windows: no flock, just run
        yield True
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with open(lock_path, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return

        yield True


# =========================================================
# KEYWORD FILTER
# =========================================================

def build_keyword_regex(keywords):
    """
    One compiled regex for all keywords.

    - "@mention" keywords match as a plain substring.
    - Everything else (words, phrases, $tickers, #hashtags) must not be
      glued to a letter/digit/underscore: "tron" matches "TRON", "#tron"
      and "$TRON" but not "electronic".
    """
    parts = []

    for k in keywords:
        k_lower = k.lower()

        if k_lower.startswith("@"):
            parts.append(re.escape(k_lower))
        else:
            parts.append(
                r"(?<![a-z0-9_])"
                + re.escape(k_lower)
                + r"(?![a-z0-9_])"
            )

    return re.compile("|".join(parts), re.IGNORECASE)


# =========================================================
# DATETIME
# =========================================================

def parse_datetime(value):
    if not value:
        return None

    value = str(value)

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            # Classic Twitter format: "Wed Oct 10 20:19:24 +0000 2018"
            dt = datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y")
        except ValueError:
            return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt


# =========================================================
# SOCIALDATA
# =========================================================

def build_query(accounts, since_ts):
    froms = " OR ".join(f"from:{a}" for a in accounts)

    if len(accounts) > 1:
        froms = f"({froms})"

    return f"{froms} include:nativeretweets since_time:{since_ts}"


def plan_queries(accounts, since_for, per_query):
    """
    Group accounts into OR-queries of up to `per_query` accounts.

    Accounts are grouped by watermark, so e.g. a newly added account
    (which needs the full lookback window) doesn't drag the whole batch
    back 24 hours.

    Returns [(accounts, since_ts), ...].
    """
    batches = []

    for acc in sorted(accounts, key=since_for):
        since_ts = since_for(acc)

        if batches:
            batch, batch_since = batches[-1]
            candidate = batch + [acc]

            if (
                len(candidate) <= per_query
                and since_ts - batch_since <= BATCH_MERGE_WINDOW
                and len(build_query(candidate, batch_since)) <= MAX_QUERY_CHARS
            ):
                batch.append(acc)
                continue

        batches.append(([acc], since_ts))

    return batches


def _retry_after(response, default):
    try:
        return max(1, min(60, int(float(response.headers["Retry-After"]))))
    except (KeyError, ValueError):
        return default


def socialdata_get(cfg, params):
    """One API call with retries. Returns parsed JSON, or None on failure."""
    headers = {
        "Authorization": f"Bearer {cfg.socialdata_api_key}",
        "Accept": "application/json"
    }

    for attempt in range(1, 4):

        try:
            r = session.get(
                SOCIALDATA_SEARCH_URL,
                headers=headers,
                params=params,
                timeout=30
            )
        except requests.RequestException as e:
            wait = 3 * attempt
            log.warning(f"[SD NETWORK ERROR] {e} (retry in {wait}s)")
            time.sleep(wait)
            continue

        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                wait = 3 * attempt
                log.warning(f"[SD] Invalid JSON response (retry in {wait}s)")
                time.sleep(wait)
                continue

        # These would fail for every account: stop the whole run
        if r.status_code == 401:
            raise FatalAPIError(
                "SocialData rejected the API key (HTTP 401). "
                "Check SOCIALDATA_API_KEY."
            )

        if r.status_code == 402:
            raise FatalAPIError(
                "Insufficient SocialData balance (HTTP 402). "
                "Top up the account."
            )

        if r.status_code == 429:
            wait = _retry_after(r, 5 * attempt)
        elif r.status_code >= 500:
            wait = 3 * attempt
        else:
            log.error(
                f"[SD ERROR] status={r.status_code} "
                f"response={r.text[:300]}"
            )
            return None

        log.warning(f"[SD {r.status_code}] retry in {wait}s")
        time.sleep(wait)

    log.error("[SD FAILED] giving up after 3 attempts")

    return None


def search_tweets(cfg, query, stats):
    """
    All result pages for one query (newest first).
    Returns a list of tweets, or None if the query failed.
    """
    tweets = []
    cursor = None

    for page in range(1, cfg.max_pages_per_query + 1):

        params = {"query": query, "type": "Latest"}

        if cursor:
            params["cursor"] = cursor

        data = socialdata_get(cfg, params)

        if data is None:
            return None

        stats["requests"] += 1

        if isinstance(data, dict):
            page_tweets = data.get("tweets") or []
            next_cursor = data.get("next_cursor")
        elif isinstance(data, list):
            page_tweets = data
            next_cursor = None
        else:
            page_tweets = []
            next_cursor = None

        tweets.extend(page_tweets)

        if not page_tweets or not next_cursor or next_cursor == cursor:
            return tweets

        cursor = next_cursor

        # Keep SocialData requests below rate limit
        time.sleep(cfg.request_delay)

    log.warning(
        f"[SD] Reached MAX_PAGES_PER_QUERY={cfg.max_pages_per_query}; "
        f"older posts in this window were skipped"
    )

    return tweets


# =========================================================
# NORMALIZE TWEET
# =========================================================

def _tweet_id(tweet):
    return str(tweet.get("id_str") or tweet.get("id") or "")


def _screen_name(tweet):
    return (tweet.get("user") or {}).get("screen_name") or ""


def _tweet_text(tweet):
    # The API returns HTML-escaped text ("&amp;", "&gt;", ...)
    text = tweet.get("full_text") or tweet.get("text") or ""

    return html.unescape(text).strip()


def normalize_tweet(tweet, account_lookup, match_quoted=True):
    """
    Returns a dict with:
        tweet_id    id of the post itself
        dedupe_id   id used for "already sent" (the original for retweets)
        account     monitored account that posted / retweeted it
        dt          created time (of the retweet, for retweets)
        text        text shown in Telegram
        quote       "Quoting @user: ..." or ""
        match_text  text the keywords are matched against
        link        x.com link to the post (the original for retweets)
    """
    tweet_id = _tweet_id(tweet)
    poster = _screen_name(tweet)
    account = account_lookup.get(poster.lower(), poster)

    retweeted = tweet.get("retweeted_status")

    if isinstance(retweeted, dict) and _tweet_id(retweeted):
        # Native retweet: dedupe on the original so it is only sent
        # once, however many monitored accounts retweet it
        content = retweeted
        dedupe_id = _tweet_id(retweeted)
        author = _screen_name(retweeted) or account
        text = _tweet_text(retweeted) or _tweet_text(tweet)
        shown_text = f"RT by @{account}: {text}"
    else:
        content = tweet
        dedupe_id = tweet_id
        author = poster or account
        text = _tweet_text(tweet)
        shown_text = text

    quote = ""
    quoted = content.get("quoted_status")

    if match_quoted and isinstance(quoted, dict):
        quoted_text = _tweet_text(quoted)

        if quoted_text:
            quoted_user = _screen_name(quoted)
            quote = (
                f"Quoting @{quoted_user}: {quoted_text}"
                if quoted_user else f"Quoting: {quoted_text}"
            )

    return {
        "tweet_id": tweet_id,
        "dedupe_id": dedupe_id,
        "account": account,
        "dt": parse_datetime(
            tweet.get("tweet_created_at")
            or tweet.get("created_at")
        ),
        "text": shown_text,
        "quote": quote,
        # The "RT by @account" prefix is left out on purpose, so an
        # account's own handle doesn't match "@handle" keywords
        "match_text": "\n".join(part for part in (text, quote) if part),
        "link": f"https://x.com/{author}/status/{dedupe_id}",
    }


# =========================================================
# TELEGRAM
# =========================================================

def utf16_len(text):
    return len(text.encode("utf-16-le")) // 2


def truncate_utf16(text, limit, marker="\n...[truncated]"):
    if utf16_len(text) <= limit:
        return text

    budget = max(0, limit - utf16_len(marker))
    used = 0
    out = []

    for ch in text:
        size = 2 if ord(ch) > 0xFFFF else 1

        if used + size > budget:
            break

        out.append(ch)
        used += size

    return "".join(out).rstrip() + marker


def build_message(item):
    """Plain-text alert. Long posts are trimmed, the link is always kept."""
    header = item["account"]
    footer = item["link"]
    body = item["text"]

    if item["quote"]:
        body += "\n\n" + item["quote"]

    room = TG_MESSAGE_LIMIT - utf16_len(header) - utf16_len(footer) - 2

    return f"{header}\n{truncate_utf16(body, room)}\n{footer}"


def telegram_call(cfg, method, payload):
    url = f"https://api.telegram.org/bot{cfg.tg_bot_token}/{method}"

    return session.post(url, data=payload, timeout=30)


def send_to_tg(cfg, message, chat_id=None):
    payload = {
        "chat_id": chat_id or cfg.tg_chat_id,
        "text": message,
        "disable_web_page_preview": True
    }

    for attempt in range(1, 4):

        try:
            r = telegram_call(cfg, "sendMessage", payload)
        except requests.RequestException as e:
            log.warning(f"[TG NETWORK ERROR] {redact(cfg, e)}")
            time.sleep(3 * attempt)
            continue

        if r.status_code == 200:
            return True

        if r.status_code == 429:
            try:
                retry_after = int(
                    r.json()
                    .get("parameters", {})
                    .get("retry_after", 5)
                )
            except (ValueError, TypeError, AttributeError):
                retry_after = 5

            log.warning(f"[TG 429] waiting {retry_after}s")
            time.sleep(retry_after)
            continue

        log.error(
            f"[TG ERROR] status={r.status_code} "
            f"response={redact(cfg, r.text[:300])}"
        )

        # 4xx (bad chat id, message rejected...) won't fix itself
        if r.status_code < 500:
            return False

        time.sleep(3 * attempt)

    return False


def check_telegram(cfg):
    """
    Make sure the bot token works and the bot can see the chat before
    spending SocialData credits on posts that can't be delivered.
    """
    try:
        r = telegram_call(cfg, "getChat", {"chat_id": cfg.tg_chat_id})
    except requests.RequestException as e:
        log.error(f"[TG] Telegram unreachable: {redact(cfg, e)}")
        return False

    if r.status_code == 200:
        return True

    log.error(
        f"[TG] Preflight failed (status={r.status_code}): "
        f"{redact(cfg, r.text[:300])}. "
        f"Check TG_BOT_TOKEN / TG_CHAT_ID and that the bot is in the chat."
    )

    return False


def notify_ops(cfg, state, key, text):
    """Operational alert to Telegram, at most once per ALERT_COOLDOWN_HOURS."""
    now = int(time.time())
    last_sent = state["alerts"].get(key, 0)

    if now - last_sent < cfg.alert_cooldown_hours * 3600:
        log.info(f"[ALERT] Suppressed (cooldown): {text}")
        return

    if send_to_tg(cfg, f"⚠️ XBot monitor\n{text}", chat_id=cfg.tg_alert_chat_id):
        state["alerts"][key] = now


# =========================================================
# RUN
# =========================================================

def run(cfg, dry_run=False):
    keyword_re = build_keyword_regex(load_keywords(cfg.keywords_file))
    accounts = load_accounts(cfg.accounts_file)
    account_lookup = {a.lower(): a for a in accounts}

    seen = load_seen(cfg.seen_file)
    state = load_state(cfg.state_file)
    watermarks = state["accounts"]

    if not dry_run and not check_telegram(cfg):
        return 1

    now_ts = int(time.time())
    floor_ts = now_ts - int(cfg.lookback_hours * 3600)

    def since_for(acc):
        # Re-check a short overlap: X's search index can lag a little
        last_fetch = watermarks.get(acc.lower())

        if not isinstance(last_fetch, (int, float)):
            return floor_ts

        return max(floor_ts, int(last_fetch) - cfg.overlap_seconds)

    queries = plan_queries(accounts, since_for, cfg.accounts_per_query)

    log.info(
        f"[RUN] {len(accounts)} accounts in {len(queries)} queries"
        + (" (dry run)" if dry_run else "")
    )

    stats = Counter()
    handled_this_run = set()

    for i, (batch, since_ts) in enumerate(queries):

        if i:
            time.sleep(cfg.request_delay)

        fetch_started = int(time.time())

        try:
            tweets = search_tweets(cfg, build_query(batch, since_ts), stats)

        except FatalAPIError as e:
            log.error(f"[FATAL] {e}")

            if not dry_run:
                notify_ops(cfg, state, "socialdata", str(e))
                save_json(cfg.state_file, state, indent=2)

            return 1

        if tweets is None:
            stats["failed_queries"] += 1
            log.warning(
                f"[SKIP] Query failed, will retry next run: "
                f"{', '.join(batch)}"
            )
            continue

        stats["tweets"] += len(tweets)
        since_dt = datetime.fromtimestamp(since_ts, timezone.utc)

        items = []

        for tweet in tweets:
            item = normalize_tweet(
                tweet,
                account_lookup,
                cfg.match_quoted_tweets
            )

            if not item["tweet_id"] or not item["dt"]:
                continue

            if item["dt"] < since_dt:
                continue

            items.append(item)

        # Oldest first, so the Telegram chat reads chronologically
        items.sort(key=lambda x: x["dt"])

        log.info(
            f"[CHECK] {len(batch)} account(s) "
            f"since {since_dt:%Y-%m-%d %H:%M} UTC: "
            f"{len(items)} post(s)"
        )

        failed_accounts = set()

        for item in items:

            stats["checked"] += 1

            dedupe_id = item["dedupe_id"]

            # Persistent duplicate check
            if dedupe_id in seen or dedupe_id in handled_this_run:
                stats["duplicates"] += 1
                continue

            # Keyword filter
            match = keyword_re.search(item["match_text"])

            if not match:
                continue

            stats["hits"] += 1

            message = build_message(item)

            log.info(
                f"[HIT] {item['account']} "
                f"keyword={match.group(0)!r} {item['link']}"
            )

            if dry_run:
                print(f"\n{message}\n", flush=True)
                handled_this_run.add(dedupe_id)
                continue

            # Only mark seen after successful TG delivery
            if send_to_tg(cfg, message):
                seen[dedupe_id] = int(time.time())
                handled_this_run.add(dedupe_id)
                save_json(cfg.seen_file, seen)
                stats["sent"] += 1
                time.sleep(cfg.tg_send_delay)
            else:
                # Don't move this account's watermark, so the post is
                # fetched (and retried) again next run
                stats["send_failed"] += 1
                handled_this_run.add(dedupe_id)
                failed_accounts.add(item["account"].lower())
                log.error(f"[TG FAILED] Not marking {dedupe_id} as seen")

        if not dry_run:
            for acc in batch:
                if acc.lower() not in failed_accounts:
                    watermarks[acc.lower()] = fetch_started

            save_json(cfg.state_file, state, indent=2)

    if not dry_run:
        stats["pruned"] = prune_seen(seen, cfg.seen_retention_days)

        # Forget accounts that were removed from accounts.json
        for key in list(watermarks):
            if key not in account_lookup:
                del watermarks[key]

        # SocialData works again: allow the next outage to alert at once
        if not stats["failed_queries"]:
            state["alerts"].pop("socialdata", None)

        save_json(cfg.seen_file, seen)
        save_json(cfg.state_file, state, indent=2)

    log_summary(accounts, queries, stats)

    return 0


def log_summary(accounts, queries, stats):
    cost = stats["tweets"] * SOCIALDATA_PRICE_PER_TWEET

    log.info("========== RUN SUMMARY ==========")
    log.info(f"Accounts:               {len(accounts)}")
    log.info(f"Queries:                {len(queries)} ({stats['failed_queries']} failed)")
    log.info(f"SocialData requests:    {stats['requests']}")
    log.info(f"Tweets returned by API: {stats['tweets']} (~${cost:.4f})")
    log.info(f"Tweets checked:         {stats['checked']}")
    log.info(f"Duplicates skipped:     {stats['duplicates']}")
    log.info(f"Keyword hits:           {stats['hits']}")
    log.info(f"Telegram sent:          {stats['sent']} ({stats['send_failed']} failed)")
    log.info("=================================")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Forward keyword-matching X posts to Telegram "
            "via the SocialData API."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print matches only: no Telegram messages, no state written"
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=BASE_DIR / ".env",
        help="path to the .env file (default: .env next to this script)"
    )
    args = parser.parse_args(argv)

    if load_dotenv is not None:
        load_dotenv(args.env_file)

    logging.basicConfig(
        level=getattr(logging, _env("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # urllib3 debug logs would print Telegram URLs, which contain the token
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    if load_dotenv is None and args.env_file.exists():
        log.warning(
            "python-dotenv is not installed, ignoring "
            f"{args.env_file} (pip install -r requirements.txt)"
        )

    try:
        cfg = Config.from_env(require_telegram=not args.dry_run)

        if args.dry_run:
            return run(cfg, dry_run=True)

        with single_instance(cfg.lock_file) as acquired:

            if not acquired:
                log.warning("Previous run is still in progress, exiting")
                return 0

            return run(cfg)

    except ConfigError as e:
        log.error(f"[CONFIG] {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
