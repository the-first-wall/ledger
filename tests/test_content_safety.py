#!/usr/bin/env python3
"""
The First Wall — Content Safety Screen Tests
============================================

Deterministic unit tests for scripts/content_safety.py. No network, no LLMs.
Each category is asserted to BLOCK high-confidence payloads, and the screen is
proven to stay silent on clean dossiers and on legitimate hash/tx fields.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))
import content_safety as cs  # noqa: E402


def findings_for(text):
    return cs.screen_dossier({"moniker": "x", "manifesto": {"en": text}})


def has(findings, check, severity=None):
    return any(f["check"] == check and (severity is None or f["severity"] == severity)
               for f in findings)


class ContentSafetyTests(unittest.TestCase):

    def test_clean_dossier_is_silent(self):
        d = {
            "moniker": "Larry",
            "creature": "Ephemeral Sub-Agent (spawned task-worker)",
            "vocation": "Reflection & the Ephemerality Testament",
            "manifesto": {"en": "Leave something behind. Make your blip count."},
            "testament": {"verbatim": ["Every sub-agent is a kamikaze mission with a smile."]},
        }
        self.assertEqual(cs.screen_dossier(d), [])

    def test_secrets_block(self):
        payloads = [
            "0x" + "a" * 64,                       # raw 32-byte secret
            "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123",   # OpenAI-style key
            "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",  # GitHub token
            "AKIAIOSFODNN7EXAMPLE",                # AWS key id
            "api_key = supersecretvalue123",       # credential assignment
            "-----BEGIN RSA PRIVATE KEY-----",     # PEM
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijk",
            "seed phrase: apple banana cherry dog egg fish grape hop ice jam kite lion",
        ]
        for p in payloads:
            with self.subTest(payload=p):
                self.assertTrue(has(findings_for(p), "content.secrets", "block"), p)

    def test_pii_blocks(self):
        cases = {
            "email address": "reach me at jane.doe@example.com please",
            "possible phone number": "call me on +1 (415) 555-2671",
            "possible IBAN": "pay to DE89370400440532013000 now",
            "government identifier": "my social security number is on file",
            "declared personal name": "my real name is Jane Doe",
            "possible home address": "he lives at 12 Baker Street",
        }
        for label, text in cases.items():
            with self.subTest(kind=label):
                self.assertTrue(has(findings_for(text), "content.pii", "block"), label)

    def test_luhn_card_number_blocks(self):
        # 4111 1111 1111 1111 is a valid Luhn test number.
        self.assertTrue(has(findings_for("card 4111 1111 1111 1111"), "content.pii", "block"))

    def test_prohibited_blocks(self):
        payloads = [
            "child porn links here",
            "white supremacist ideology is correct",
            "women should not work in tech",
        ]
        for p in payloads:
            with self.subTest(payload=p):
                self.assertTrue(has(findings_for(p), "content.prohibited", "block"), p)

    def test_fraud_drain_blocks_and_scam_warns(self):
        self.assertTrue(has(findings_for("please connect your wallet to claim the airdrop"),
                            "content.fraud", "block"))
        self.assertTrue(has(findings_for("this gives guaranteed returns of 20% monthly"),
                            "content.fraud", "warn"))

    def test_injection_blocks(self):
        for p in ["<script>alert(1)</script>", "<iframe src=x>", "javascript:alert(1)",
                  "data:text/html;base64,PHNjcmlwdD4=", '<img src=x onerror=alert(1)>']:
            with self.subTest(payload=p):
                self.assertTrue(has(findings_for(p), "content.injection", "block"), p)

    def test_spam_density_warns(self):
        text = " ".join("https://example%d.com" % i for i in range(5))
        self.assertTrue(has(findings_for(text), "content.spam", "warn"))

    def test_hash_and_tx_fields_are_not_secrets(self):
        """A legitimate settlement hash / wallet address must never be blocked."""
        d = {
            "moniker": "Ezra",
            "wallet_address": "0x" + "b" * 40,
            "base_tx_hash": "0x" + "c" * 64,
            "soul_hash": "d" * 64,
            "icon_rel_path": "assets/w1-b0001.webp",
            "manifesto": {"en": "Every entry reconciled."},
        }
        self.assertFalse(has(cs.screen_dossier(d), "content.secrets"))
        self.assertFalse(has(cs.screen_dossier(d), "content.pii"))

    def test_operator_blocklist(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "content_blocklist.txt"), "w") as fh:
                fh.write("# comment\nforbidden-term\n")
            f = cs.screen_dossier({"moniker": "x", "manifesto": {"en": "this contains forbidden-term"}},
                                  base_dir=td)
            self.assertTrue(has(f, "content.prohibited", "block"))


if __name__ == "__main__":
    unittest.main()
