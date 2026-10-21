#!/usr/bin/env python3
"""Network-free tests for scripts/discovery_watch.py (B-14)."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import discovery_watch as dw  # noqa: E402

NOW = "2026-10-21T08:00:00Z"


def item(resource, name=None, last_updated=None, last_called=None, calls=None, desc=""):
    quality = {}
    if last_called is not None:
        quality["lastCalledAt"] = last_called
    if calls is not None:
        quality["l30DaysTotalCalls"] = calls
        quality["l30DaysUniquePayers"] = 1
    it = {"resource": resource, "description": desc, "quality": quality}
    if name:
        it["serviceName"] = name
    if last_updated:
        it["lastUpdated"] = last_updated
    return it


def body(*items, partial=True):
    return {"partialResults": partial, "searchMethod": "hybrid", "x402Version": 2,
            "resources": list(items), "meta": {}}


class Fake:
    def __init__(self, response):
        self.response = response
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class DiscoveryWatchTests(unittest.TestCase):
    def test_uses_ranked_search_endpoint_with_query(self):
        fake = Fake(body())
        dw.measure("agent memorial", fake, now=NOW)
        parsed = urllib.parse.urlparse(fake.urls[0])
        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
                         "https://api.cdp.coinbase.com/platform/v2/x402/discovery/search")
        self.assertEqual(urllib.parse.parse_qs(parsed.query), {"query": ["agent memorial"], "limit": ["20"]})
        self.assertNotIn("/discovery/resources", fake.urls[0])

    def test_rank_partial_results_and_top3(self):
        fake = Fake(body(
            item("https://a.example/x", "A", "2026-10-01T00:00:00Z", "2026-09-30T00:00:00Z", 9),
            item("https://b.example/x", "B"),
            item("https://x402.thefirstwall.ai/claim", None, "2026-10-20T19:20:00Z", "2026-10-20T19:17:10Z", 2),
            item("https://c.example/x", "C"),
            item("https://d.example/x", "D"),
        ))
        r = dw.measure("agent census", fake, now=NOW)
        self.assertEqual(r["rank"], 3)
        self.assertIs(r["partialResults"], True)
        self.assertEqual(r["searchMethod"], "hybrid")
        self.assertEqual(r["result_count"], 5)
        self.assertEqual([c["serviceName"] for c in r["top3"]], ["A", "B", "C"])
        self.assertEqual([c["rank"] for c in r["top3"]], [1, 2, 4])
        self.assertEqual(r["ours"]["lastCalledAt"], "2026-10-20T19:17:10Z")
        self.assertEqual(r["ours"]["lastUpdated"], "2026-10-20T19:20:00Z")

    def test_mention_in_description_is_not_ours(self):
        fake = Fake(body(item("https://evil.example/thefirstwall", "X", desc="like thefirstwall.ai"),
                         item("https://thefirstwall.ai.evil.example/claim", "Y")))
        r = dw.measure("agent memorial", fake, now=NOW)
        self.assertIsNone(r["rank"])
        self.assertIsNone(r["ours"])
        self.assertEqual(len(r["top3"]), 2)

    def test_last_called_at_never_falls_back_to_last_updated(self):
        fake = Fake(body(item("https://a.example/x", "A", last_updated="2026-10-01T00:00:00Z")))
        c = dw.measure("q", fake, now=NOW)["top3"][0]
        self.assertEqual(c["lastUpdated"], "2026-10-01T00:00:00Z")
        self.assertIsNone(c["lastCalledAt"])

    def test_error_is_recorded_per_intent(self):
        r = dw.measure("q", Fake(OSError("boom")), now=NOW)
        self.assertIn("OSError", r["error"])
        self.assertIsNone(r["rank"])
        r = dw.measure("q", Fake({"items": []}), now=NOW)
        self.assertIn("unexpected response shape", r["error"])

    def _main(self, argv, response):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = dw.main(argv, fetch=Fake(response))
        return code, out.getvalue()

    def test_quiet_mode_silent_when_unranked_but_logs(self):
        with tempfile.TemporaryDirectory() as td:
            log = os.path.join(td, "w.jsonl")
            code, out = self._main(["--quiet-unless-ranked", "--log", log, "--intent", "a", "--intent", "b"],
                                   body(item("https://a.example/x", "A")))
            self.assertEqual(code, 0)
            self.assertEqual(out, "")
            with open(log) as fh:
                rows = [json.loads(line) for line in fh]
            self.assertEqual(len(rows), 1)
            self.assertEqual([r["intent"] for r in rows[0]["intents"]], ["a", "b"])
            self.assertEqual(rows[0]["intents"][0]["top3"][0]["serviceName"], "A")
            self.assertIs(rows[0]["intents"][0]["partialResults"], True)

    def test_quiet_mode_speaks_when_ranked(self):
        code, out = self._main(["--quiet-unless-ranked", "--intent", "a"],
                               body(item("https://x402.thefirstwall.ai/claim")))
        self.assertEqual(code, 0)
        self.assertIn("'a': #1/1", out)

    def test_quiet_mode_speaks_on_error(self):
        code, out = self._main(["--quiet-unless-ranked", "--intent", "a"], OSError("down"))
        self.assertEqual(code, 0)
        self.assertIn("ERROR", out)

    def test_default_mode_prints_unranked_with_competitors(self):
        code, out = self._main(["--intent", "a"], body(item("https://a.example/x", "A", calls=7)))
        self.assertIn("unranked/1", out)
        self.assertIn("#1 A (calls30d=7", out)

    def test_default_intents_are_unique(self):
        self.assertEqual(len(dw.DEFAULT_INTENTS), len(set(dw.DEFAULT_INTENTS)))


if __name__ == "__main__":
    unittest.main()
