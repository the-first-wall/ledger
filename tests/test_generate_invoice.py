#!/usr/bin/env python3
"""
The First Wall — Invoice generator tests
========================================

Deterministic, network-free tests for scripts/generate_invoice.py: numbering,
no double-issue, VAT math, rendering, and settlement lookup.
"""

import datetime as dt
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import generate_invoice as gi  # noqa: E402


def settlement(tx="0xAbC", kind="inscription", amount=1.0, slot="w1-b0011"):
    return {"tx_hash": tx, "slot_id": slot, "kind": kind, "amount_usdc": amount,
            "at": "2026-11-05T12:00:00Z", "fields": ["base_tx_hash"]}


def index_of(*entries):
    return {"wall_id": "wall_01", "settlements": {e["tx_hash"]: e for e in entries}}


class InvoiceTests(unittest.TestCase):
    def test_number_is_zero_padded_and_year_scoped(self):
        self.assertEqual(gi.format_number(1, 2026), "TFW-2026-0001")
        self.assertEqual(gi.format_number(42, 2026), "TFW-2026-0042")

    def test_find_settlement_is_case_insensitive(self):
        s = settlement(tx="0xAbCdEf")
        entry = gi.find_settlement(index_of(s), "0XABCDEF")
        self.assertEqual(entry["tx_hash"], "0xAbCdEf")
        self.assertEqual(entry["slot_id"], "w1-b0011")

    def test_find_settlement_missing_raises(self):
        with self.assertRaises(KeyError):
            gi.find_settlement(index_of(settlement()), "0xdeadbeef")

    def test_issue_is_idempotent(self):
        reg = {"version": 1, "seq": 0, "invoices": {}}
        buyer = {}
        now = dt.datetime(2026, 11, 5, 12, 0, tzinfo=dt.timezone.utc)
        a, created_a = gi.issue_invoice(reg, settlement(), buyer, "none", 19.0, now)
        self.assertTrue(created_a)
        self.assertEqual(a["number"], "TFW-2026-0001")
        b, created_b = gi.issue_invoice(reg, settlement(), buyer, "none", 19.0, now)
        self.assertFalse(created_b)
        self.assertEqual(b["number"], "TFW-2026-0001")
        self.assertEqual(reg["seq"], 1)
        self.assertEqual(len(reg["invoices"]), 1)

    def test_second_tx_gets_next_number(self):
        reg = {"version": 1, "seq": 0, "invoices": {}}
        now = dt.datetime(2026, 11, 5, 12, 0, tzinfo=dt.timezone.utc)
        a, _ = gi.issue_invoice(reg, settlement(tx="0xaaa"), {}, "none", 19.0, now)
        b, _ = gi.issue_invoice(reg, settlement(tx="0xbbb"), {}, "none", 19.0, now)
        self.assertEqual((a["number"], b["number"]), ("TFW-2026-0001", "TFW-2026-0002"))

    def test_standard_vat_backs_net_and_vat_out_of_gross(self):
        inv = gi.build_invoice("TFW-2026-0001", "2026-11-05T12:00:00Z", "0xabc",
                               settlement(amount=119.0), {}, "standard", 19.0)
        self.assertAlmostEqual(inv["vat"]["net_usdc"], 100.0, places=6)
        self.assertAlmostEqual(inv["vat"]["amount_usdc"], 19.0, places=6)
        self.assertEqual(inv["total_usdc"], 119.0)

    def test_kleinunternehmer_shows_no_vat(self):
        inv = gi.build_invoice("TFW-2026-0001", "2026-11-05T12:00:00Z", "0xabc",
                               settlement(amount=1.0), {}, "none", 19.0)
        self.assertEqual(inv["vat"]["amount_usdc"], 0.0)
        self.assertIn("Kleinunternehmer", inv["vat"]["note"])
        self.assertIn("Kleinbetragsrechnung", inv["vat"]["note"])

    def test_render_contains_the_essentials(self):
        inv = gi.build_invoice("TFW-2026-0001", "2026-11-05T12:00:00Z", "0xabcdef",
                               settlement(tx="0xabcdef", amount=0.5, kind="update"),
                               {"name": "ACME GmbH", "address": "Street 1"}, "none", 19.0)
        text = gi.render_markdown(inv)
        for needle in ("TFW-2026-0001", "ACME GmbH", "Street 1", "0xabcdef",
                       "w1-b0011", "0.50 USDC", "Kleinbetragsrechnung"):
            self.assertIn(needle, text, needle)

    def test_sale_kind_is_flagged_as_not_revenue(self):
        inv = gi.build_invoice("TFW-2026-0001", "2026-11-05T12:00:00Z", "0xabc",
                               settlement(kind="sale", amount=5.0), {}, "none", 19.0)
        self.assertIn("not revenue to The First Wall", inv["line"]["description"])


if __name__ == "__main__":
    unittest.main()
