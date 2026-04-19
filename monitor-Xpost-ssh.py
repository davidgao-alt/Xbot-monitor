import feedparser
import json
import re
import requests
from datetime import datetime, timedelta, timezone
import email.utils
import os
import time

KEYWORDS = ["tron", "trx", "trc20", "justinsuntron", "justin","usdt","@trondao","@justinsuntron"]

TG_BOT_TOKEN = ""
TG_CHAT_ID = ""

SEEN_FILE = "/root/tronbot/seen_ids.json"


def send_to_tg(message):

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": TG_CHAT_ID,
        "text": message,
        "disable_web_page_preview": True
    }

    requests.post(url, data=payload)


def keyword_hit(text: str):
    text_lower = text.lower()

    for k in KEYWORDS:
        if k.startswith("@"):
            if k in text_lower:
                return True
        else:
            pattern = r"\b" + re.escape(k) + r"\b"
            if re.search(pattern, text_lower):
                return True
    return False


def to_x_link(nitter_link: str):

    link = nitter_link.split("#")[0]

    link = link.replace("https://nitter.net/", "https://x.com/") \
               .replace("http://nitter.net/", "https://x.com/")

    return link


def load_seen():

    if not os.path.exists(SEEN_FILE):
        return set()

    with open(SEEN_FILE, "r") as f:
        return set(json.load(f))


def save_seen(seen):

    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)


seen_ids = load_seen()

with open("/root/tronbot/accounts.json", "r") as f:
    accounts = json.load(f)


now = datetime.now(timezone.utc)
cutoff = now - timedelta(hours=24)


for acc in accounts:

    url = f"https://nitter.net/{acc}/rss"

    feed = feedparser.parse(url)

    for entry in feed.entries:

        published = getattr(entry, "published", None) or getattr(entry, "updated", None)
        if not published:
            continue

        dt = email.utils.parsedate_to_datetime(published)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        if dt < cutoff:
            continue

        text = entry.title

        if not keyword_hit(text):
            continue

        link = to_x_link(entry.link)

        m = re.search(r"/status/(\d+)", link)
        tweet_id = m.group(1) if m else link

        if tweet_id in seen_ids:
            continue

        seen_ids.add(tweet_id)

        message = f"{acc}\n{text}\n{link}"

        print()
        print(message)

        send_to_tg(message)

        time.sleep(1.5)   


save_seen(seen_ids)