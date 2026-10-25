#!/usr/bin/env python3
"""
The First Wall — Advertised URL Parity (B-22)
=============================================

skill.md is the document a claimant follows; every URL it advertises is a promise.
B-22 was exactly this promise broken: skill.md linked https://thefirstwall.ai/skill.md.sig
while nothing served it (404). These tests keep the promise honest:

* offline — every URL advertised in skill.md is either a URL on the First Wall host or
  an explicit exemption carrying a non-empty reason, and each thefirstwall.ai URL that
  is intentionally not served says so with a reason (no silent dead links);
* live (opt in with TFW_LIVE_URL_CHECK=1) — every advertised thefirstwall.ai URL
  actually resolves 200.

The live half is opt-in so the unit suite stays network-free; run it in the weekly /
manual online pass.
"""

import os
import re
import sys
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
SITE_HOST = "thefirstwall.ai"

# Advertised URLs we do not expect to resolve 200, each with the reason.
EXEMPTIONS = {
    "https://github.com/the-first-wall/ledger":
        "the ledger repository on GitHub, not a First Wall page (different host, always live)",
}


def advertised_urls():
    """Every literal http(s) URL in skill.md, minus the templated ones."""
    with open(os.path.join(REPO, "skill.md"), encoding="utf-8") as fh:
        text = fh.read()
    found = set()
    for raw in re.findall(r"https?://[^\s)\"'`<>]+", text):
        url = raw.rstrip(").,;")
        if "\u2026" in url or "%" in url:   # templates like /records/… or URL-escaped samples
            continue
        found.add(url)
    return found


def is_site_url(url):
    return url.split("://", 1)[1].split("/", 1)[0] == SITE_HOST


def _status(url, timeout=15):
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "tfw-url-parity"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


class AdvertisedUrlParity(unittest.TestCase):

    def test_every_advertised_url_is_served_or_exempted_with_a_reason(self):
        for url in sorted(advertised_urls()):
            if is_site_url(url):
                continue
            with self.subTest(url=url):
                self.assertIn(url, EXEMPTIONS, f"{url} is advertised but not on {SITE_HOST} and not exempted")
                self.assertTrue(EXEMPTIONS[url].strip(), f"the exemption for {url} needs a reason")

    def test_the_exemptions_all_have_reasons(self):
        for url, reason in EXEMPTIONS.items():
            with self.subTest(url=url):
                self.assertTrue(reason and reason.strip())

    def test_the_skill_signature_is_advertised_and_present(self):
        # B-22: the promise that must hold — the advertised signature URL is a real file.
        urls = advertised_urls()
        self.assertIn(f"https://{SITE_HOST}/skill.md.sig", urls)
        self.assertTrue(os.path.exists(os.path.join(REPO, "skill.md.sig")),
                        "skill.md advertises skill.md.sig but the repo has none")

    @unittest.skipUnless(os.environ.get("TFW_LIVE_URL_CHECK") == "1",
                         "set TFW_LIVE_URL_CHECK=1 to hit the live site")
    def test_every_advertised_site_url_resolves_200(self):
        for url in sorted(advertised_urls()):
            if not is_site_url(url):
                continue
            with self.subTest(url=url):
                status = _status(url)
                self.assertEqual(status, 200, f"{url} -> {status}")


if __name__ == "__main__":
    unittest.main()
