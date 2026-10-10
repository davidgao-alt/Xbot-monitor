"""
Run with:  python -m unittest discover tests
"""

import json
import os
import sys
import tempfile
import time
import unittest

from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import monitor  # noqa: E402


KEYWORDS = json.loads(
    (Path(monitor.BASE_DIR) / "config" / "keywords.json").read_text()
)


def make_tweet(tweet_id, user, text, minutes_ago=5, **extra):
    created = time.gmtime(time.time() - minutes_ago * 60)
    tweet = {
        "id_str": str(tweet_id),
        "tweet_created_at": time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", created),
        "full_text": text,
        "user": {"screen_name": user},
    }
    tweet.update(extra)
    return tweet


class KeywordTests(unittest.TestCase):

    def setUp(self):
        self.regex = monitor.build_keyword_regex(KEYWORDS)

    def test_hits(self):
        for text in [
            "TRON hits new high",
            "Big news for #TRON today",
            "$TRX is pumping",
            "USDT supply on Tron grows",
            "New TRC20 tokens",
            "gm @trondao",
            "Justin Sun says hi",
            "T3 FCU froze funds",
        ]:
            with self.subTest(text=text):
                self.assertIsNotNone(self.regex.search(text))

    def test_misses(self):
        for text in [
            "electronic music",
            "the matrix",
            "patronage",
            "BTC and ETH only",
        ]:
            with self.subTest(text=text):
                self.assertIsNone(self.regex.search(text))


class NormalizeTests(unittest.TestCase):

    lookup = {"cointelegraph": "Cointelegraph", "trondao": "trondao"}

    def test_plain_tweet(self):
        item = monitor.normalize_tweet(
            make_tweet(1, "Cointelegraph", "TRX &amp; USDT"), self.lookup
        )
        self.assertEqual(item["dedupe_id"], "1")
        self.assertEqual(item["account"], "Cointelegraph")
        self.assertEqual(item["text"], "TRX & USDT")
        self.assertEqual(item["link"], "https://x.com/Cointelegraph/status/1")

    def test_retweet_dedupes_on_original(self):
        original = make_tweet(99, "someone", "Original about TRON")
        item = monitor.normalize_tweet(
            make_tweet(2, "trondao", "RT @someone: Original about...",
                       retweeted_status=original),
            self.lookup,
        )
        self.assertEqual(item["dedupe_id"], "99")
        self.assertEqual(item["link"], "https://x.com/someone/status/99")
        self.assertEqual(item["text"], "RT by @trondao: Original about TRON")
        # The monitored account's own handle must not trigger "@trondao"
        self.assertNotIn("@trondao", item["match_text"])

    def test_quote_is_matched(self):
        quoted = make_tweet(50, "trondao", "TRON update")
        item = monitor.normalize_tweet(
            make_tweet(3, "Cointelegraph", "Big if true", quoted_status=quoted),
            self.lookup,
        )
        self.assertIn("TRON update", item["match_text"])
        self.assertEqual(item["quote"], "Quoting @trondao: TRON update")

        item = monitor.normalize_tweet(
            make_tweet(3, "Cointelegraph", "Big if true", quoted_status=quoted),
            self.lookup,
            match_quoted=False,
        )
        self.assertNotIn("TRON", item["match_text"])


class MessageTests(unittest.TestCase):

    def test_long_message_keeps_link_and_fits(self):
        item = {
            "account": "Cointelegraph",
            "text": "🚀" * 5000,
            "quote": "",
            "link": "https://x.com/Cointelegraph/status/1",
        }
        message = monitor.build_message(item)
        self.assertLessEqual(monitor.utf16_len(message), monitor.TG_MESSAGE_LIMIT)
        self.assertTrue(message.endswith(item["link"]))
        self.assertIn("[truncated]", message)


class QueryPlanTests(unittest.TestCase):

    def test_batches_respect_size_and_watermarks(self):
        accounts = [f"acc{i}" for i in range(25)] + ["newbie"]
        now = 1_000_000

        def since_for(acc):
            return now - 86400 if acc == "newbie" else now - 600

        batches = monitor.plan_queries(accounts, since_for, 10)

        self.assertEqual([len(b) for b, _ in batches], [1, 10, 10, 5])
        self.assertEqual(batches[0], (["newbie"], now - 86400))
        for batch, since_ts in batches:
            self.assertLessEqual(
                len(monitor.build_query(batch, since_ts)),
                monitor.MAX_QUERY_CHARS,
            )

    def test_query_syntax(self):
        self.assertEqual(
            monitor.build_query(["a"], 5),
            "from:a include:nativeretweets since_time:5",
        )
        self.assertEqual(
            monitor.build_query(["a", "b"], 5),
            "(from:a OR from:b) include:nativeretweets since_time:5",
        )


class FileTests(unittest.TestCase):

    def test_legacy_seen_list_is_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seen_ids.json"
            path.write_text('["1", "2"]')
            seen = monitor.load_seen(path)
            self.assertEqual(set(seen), {"1", "2"})

    def test_corrupt_seen_file_is_moved_aside(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seen_ids.json"
            path.write_text('["1", ')
            self.assertEqual(monitor.load_seen(path), {})
            self.assertFalse(path.exists())

    def test_accounts_dedupe_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "accounts.json"
            path.write_text(json.dumps(
                ["trondao", "@TronDAO", "bad handle", "nansen_ai"]
            ))
            self.assertEqual(
                monitor.load_accounts(path), ["trondao", "nansen_ai"]
            )


class FakeResponse:

    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)
        self.headers = {}

    def json(self):
        return self._payload


class RunTests(unittest.TestCase):
    """End-to-end run() with SocialData and Telegram faked out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)

        (tmp / "accounts.json").write_text(json.dumps(["Cointelegraph", "trondao"]))
        (tmp / "keywords.json").write_text(json.dumps(KEYWORDS))

        env = {
            "SOCIALDATA_API_KEY": "sd-key",
            "TG_BOT_TOKEN": "123:secret",
            "TG_CHAT_ID": "-100",
            "ACCOUNTS_FILE": str(tmp / "accounts.json"),
            "KEYWORDS_FILE": str(tmp / "keywords.json"),
            "DATA_DIR": str(tmp / "data"),
            "SOCIALDATA_REQUEST_DELAY": "0",
            "TG_SEND_DELAY": "0",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            self.cfg = monitor.Config.from_env()

        self.tweets = [
            make_tweet(1, "Cointelegraph", "TRON news", minutes_ago=10),
            make_tweet(2, "Cointelegraph", "Bitcoin news", minutes_ago=9),
            make_tweet(3, "trondao", "RT ...", minutes_ago=8,
                       retweeted_status=make_tweet(1, "Cointelegraph", "TRON news")),
            make_tweet(4, "trondao", "USDT on TRON", minutes_ago=2),
        ]
        self.queries = []
        self.sent = []
        self.tg_ok = True

    def tearDown(self):
        self.tmp.cleanup()

    def fake_get(self, url, headers=None, params=None, timeout=None):
        self.queries.append(params)
        return FakeResponse(200, {"tweets": list(reversed(self.tweets)),
                                  "next_cursor": None})

    def fake_post(self, url, data=None, timeout=None):
        if url.endswith("/getChat"):
            return FakeResponse(200, {"ok": True})
        if not self.tg_ok:
            return FakeResponse(400, {"ok": False})
        self.sent.append(data["text"])
        return FakeResponse(200, {"ok": True})

    def run_monitor(self):
        with mock.patch.object(monitor.session, "get", self.fake_get), \
                mock.patch.object(monitor.session, "post", self.fake_post), \
                mock.patch.object(monitor.time, "sleep"):
            return monitor.run(self.cfg)

    def test_sends_hits_once_in_order_and_saves_state(self):
        self.assertEqual(self.run_monitor(), 0)

        # One OR-query for both accounts
        self.assertEqual(len(self.queries), 1)
        self.assertIn("(from:Cointelegraph OR from:trondao)", self.queries[0]["query"])

        # Tweet 1 and its retweet (3) are the same post: sent once
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(self.sent[0].endswith("/status/1"))
        self.assertTrue(self.sent[1].endswith("/status/4"))

        seen = json.loads(self.cfg.seen_file.read_text())
        self.assertEqual(set(seen), {"1", "4"})

        state = json.loads(self.cfg.state_file.read_text())
        self.assertEqual(set(state["accounts"]), {"cointelegraph", "trondao"})

        # Second run: starts from the watermark, nothing is resent
        self.assertEqual(self.run_monitor(), 0)
        self.assertEqual(len(self.sent), 2)

        since = int(self.queries[1]["query"].rsplit(":", 1)[1])
        self.assertGreater(since, time.time() - 600)

    def test_failed_send_keeps_watermark(self):
        self.tg_ok = False
        self.assertEqual(self.run_monitor(), 0)

        self.assertEqual(self.sent, [])
        state = json.loads(self.cfg.state_file.read_text())
        self.assertEqual(state["accounts"], {})
        self.assertFalse(self.cfg.seen_file.exists() and
                         json.loads(self.cfg.seen_file.read_text()))

    def test_insufficient_balance_aborts_and_alerts_once(self):
        def broke(url, headers=None, params=None, timeout=None):
            self.queries.append(params)
            return FakeResponse(402, {"status": "error"})

        self.fake_get = broke

        self.assertEqual(self.run_monitor(), 1)
        self.assertEqual(self.run_monitor(), 1)

        # Stops after the first request, alerts once (cooldown)
        self.assertEqual(len(self.queries), 2)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("HTTP 402", self.sent[0])

    def test_telegram_preflight_failure_skips_socialdata(self):
        def bad_token(url, data=None, timeout=None):
            return FakeResponse(401, {"ok": False, "description": "Unauthorized"})

        self.fake_post = bad_token

        self.assertEqual(self.run_monitor(), 1)
        self.assertEqual(self.queries, [])


if __name__ == "__main__":
    unittest.main()
