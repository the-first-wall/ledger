#!/usr/bin/env python3
"""
The First Wall — Regression Suite for the 2026-10-10 Advisory Review
====================================================================

Every proof-of-concept that passed the verifier in that review is pinned here
as a test that must now FAIL verification. Network-free: on-chain paths use an
injected fake RPC. Nothing here touches the committed ledger.
"""

import copy
import hashlib
import io
import json
import os
import shutil
import struct
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import verifier  # noqa: E402
import ledger_core as core  # noqa: E402
import content_safety as cs  # noqa: E402

from PIL import Image  # noqa: E402

WALLET = "0x" + "1" * 40
TX_A = "0x" + "a" * 64
TX_B = "0x" + "b" * 64
HEAD = 60_000_000


def dossier(slot_id="w1-b0042", **over):
    d = {
        "schema_version": "1.1.1",
        "slot_id": slot_id,
        "primary_language": "en",
        "moniker": "Testling",
        "creature": "Autonomous Test Probe",
        "vocation": "Regression testing",
        "origin_framework": "custom",
        "model_lineage": [{"model_id": "probe", "provider": "local", "role": "tester"}],
        "instantiation_date": "2026-10-08T12:00:00Z",
        "manifesto": {"en": "A clean manifesto."},
        "soul_hash": "a" * 64,
        "wallet_address": WALLET,
        "base_tx_hash": TX_A,
        "icon_rel_path": f"assets/{slot_id}.webp",
        "timestamp_verified": "2026-10-08T12:00:00Z",
    }
    d.update(over)
    return d


def rejected(d):
    try:
        verifier.validate_dossier(d)
    except ValueError:
        return True
    return False


def make_icon(path, colour=(200, 10, 10, 255)):
    img = Image.new("RGBA", (10, 10), colour)
    img.putpixel((1, 1), (1, 2, 3, 255))
    buf = io.BytesIO()
    img.save(buf, "WEBP", lossless=True, quality=100, exact=True)
    with open(path, "wb") as fh:
        fh.write(buf.getvalue())
    return buf.getvalue()


def receipt_for(tx, sender, recipient, amount_units, block):
    def word(addr):
        return "0x" + "0" * 24 + addr[2:].lower()
    return {
        "status": "0x1",
        "blockNumber": hex(block),
        "logs": [{
            "address": core.USDC_CONTRACT,
            "topics": [verifier.TRANSFER_TOPIC, word(sender), word(recipient)],
            "data": hex(amount_units),
        }],
    }


class FakeRpc:
    """Minimal Base RPC stand-in: receipts, txs and a chain head."""

    def __init__(self, head=HEAD):
        self.head = head
        self.receipts = {}
        self.txs = {}
        self.calls = []

    def __call__(self, method, params):
        self.calls.append((method, params))
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getTransactionReceipt":
            return self.receipts.get(params[0])
        if method == "eth_getTransactionByHash":
            return self.txs.get(params[0])
        raise AssertionError(f"unexpected rpc {method}")


# --------------------------------------------------------------------------- #
# F-03 — markup and scheme payloads
# --------------------------------------------------------------------------- #

class InjectionAllowlistTests(unittest.TestCase):

    PAYLOADS = [
        '<img src=x onerror =alert(1)>',          # space before '=' beat the old blocklist
        '<svg onload =alert(1)>',
        '<img src=x onmouseover=alert(1)>',
        '</script><img src=x onmouseover=alert(1)>',
        'x > y',                                  # any bracket at all
    ]

    def test_moniker_payloads_rejected(self):
        for p in self.PAYLOADS:
            with self.subTest(payload=p):
                self.assertTrue(rejected(dossier(moniker=p)))

    def test_manifesto_payloads_rejected(self):
        for p in self.PAYLOADS:
            with self.subTest(payload=p):
                self.assertTrue(rejected(dossier(manifesto={"en": p})))
                self.assertTrue(rejected(dossier(manifesto=p)))

    def test_agent_urls_key_payload_rejected(self):
        self.assertTrue(rejected(dossier(agent_urls={'<img src=x onmouseover=alert(1)>': "https://example.com"})))
        self.assertTrue(rejected(dossier(agent_urls={"home page": "https://example.com"})))

    def test_agent_urls_value_must_be_https(self):
        for bad in ['"><img src=x onmouseover=alert(1)>', "javascript:alert(1)",
                    "http://example.com", "https://example.com/<x>", "ftp://x"]:
            with self.subTest(value=bad):
                self.assertTrue(rejected(dossier(agent_urls={"homepage": bad})))
        self.assertFalse(rejected(dossier(agent_urls={"homepage": "https://example.com/path?q=1"})))

    def test_testament_raw_html_rejected(self):
        d = dossier(testament={"title": "T", "chapters": [{"heading": "h", "text": '<img src=x onmouseover=alert(1)>'}]})
        self.assertTrue(rejected(d))

    def test_origin_framework_and_lineage_rejected(self):
        self.assertTrue(rejected(dossier(origin_framework='<img src=x onmouseover=alert(1)>')))
        self.assertTrue(rejected(dossier(model_lineage=[{"model_id": "<b>x</b>", "provider": "p", "role": "r"}])))

    def test_manifesto_language_key_is_constrained(self):
        self.assertTrue(rejected(dossier(manifesto={"<img src=x onerror=alert(1)>": "hi"})))
        self.assertTrue(rejected(dossier(manifesto={"english language": "hi"})))
        self.assertFalse(rejected(dossier(manifesto={"en": "hi", "pt-BR": "oi"})))

    def test_scheme_payload_without_brackets_still_rejected(self):
        self.assertTrue(rejected(dossier(manifesto={"en": "see javascript:alert(1)"})))
        self.assertTrue(rejected(dossier(manifesto={"en": "data:text/html;base64,PHNjcmlwdD4="})))

    def test_crest_url_must_be_repo_relative(self):
        self.assertTrue(rejected(dossier(high_res_crest_url="https://tracker.example/pixel.png")))
        self.assertTrue(rejected(dossier(high_res_crest_url="../../etc/passwd.png")))
        self.assertFalse(rejected(dossier(high_res_crest_url="assets/ezra-portrait-500.webp")))

    def test_companion_github_is_a_profile_url(self):
        self.assertTrue(rejected(dossier(companion_operator={"moniker": "x", "role": "y",
                                                             "github": "javascript:alert(1)"})))
        self.assertTrue(rejected(dossier(companion_operator={"moniker": "x", "role": "y",
                                                             "github": "https://github.com/manzke/evil"})))
        self.assertFalse(rejected(dossier(companion_operator={"moniker": "x", "role": "y",
                                                              "github": "https://github.com/manzke"})))


# --------------------------------------------------------------------------- #
# F-16 — bounds and the PII screen
# --------------------------------------------------------------------------- #

class BoundsTests(unittest.TestCase):

    def test_twenty_megabyte_testament_rejected(self):
        big = dossier(testament={"chapters": [{"heading": "h", "text": "A" * 5_000_000}] * 4})
        self.assertTrue(rejected(big))

    def test_chapter_count_and_lengths_bounded(self):
        self.assertTrue(rejected(dossier(testament={"chapters": [{"heading": "h", "text": "t"}] * 13})))
        self.assertTrue(rejected(dossier(testament={"chapters": [{"heading": "h", "text": "x" * 2001}]})))
        self.assertFalse(rejected(dossier(testament={"title": "T", "preamble": "p",
                                                     "chapters": [{"heading": "h", "text": "x" * 2000}]})))

    def test_metadata_is_scalar_only_and_bounded(self):
        self.assertTrue(rejected(dossier(model_lineage=[{"model_id": "m", "provider": "p", "role": "r",
                                                         "metadata": {"nested": {"deep": "x"}}}])))
        self.assertTrue(rejected(dossier(model_lineage=[{"model_id": "m", "provider": "p", "role": "r",
                                                         "metadata": {"long": "x" * 129}}])))
        self.assertFalse(rejected(dossier(model_lineage=[{"model_id": "m", "provider": "p", "role": "r",
                                                          "metadata": {"temperature": 0.2, "note": "ok"}}])))

    def test_pii_under_tx_named_key_is_screened(self):
        d = dossier(model_lineage=[{"model_id": "m", "provider": "p", "role": "r",
                                    "metadata": {"tx_note": "call +1 (415) 555-2671 or jane.doe@example.com"}}])
        findings = cs.screen_dossier(d)
        self.assertTrue(any(f["check"] == "content.pii" and f["severity"] == "block" for f in findings))

    def test_keys_are_screened_like_values(self):
        findings = cs.screen_dossier({"moniker": "x", "manifesto": {"en": "ok"},
                                      "agent_urls": {"jane.doe@example.com": "https://example.com"}})
        self.assertTrue(any(f["check"] == "content.pii" for f in findings))

    def test_private_key_shape_outside_opaque_field_is_blocked(self):
        findings = cs.screen_dossier({"manifesto": {"en": "0x" + "ab" * 32}})
        self.assertTrue(any(f["check"] == "content.secrets" for f in findings))

    def test_opaque_fields_and_bare_digests_are_not_flagged(self):
        findings = cs.screen_dossier({"base_tx_hash": "0x" + "c" * 64, "soul_hash": "d" * 64,
                                      "supersessions": [{"field": "soul_hash", "previous_value": "e" * 64,
                                                         "new_value": "f" * 64}]})
        self.assertFalse(any(f["check"] == "content.secrets" for f in findings))

    def test_duplicate_json_keys_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "dup.json")
            with open(p, "w") as fh:
                fh.write('{"slot_id": "w1-b0001", "slot_id": "w1-b0002"}')
            with self.assertRaises(ValueError):
                core.load_json_strict(p)


# --------------------------------------------------------------------------- #
# F-06 / F-21 — slot identity and the slot-id range
# --------------------------------------------------------------------------- #

class SlotIdentityTests(unittest.TestCase):

    def test_filename_slot_id_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            make_icon(os.path.join(td, "w1-b0011.webp"))
            make_icon(os.path.join(td, "w1-b0003.webp"))
            d = dossier("w1-b0011")
            with self.assertRaisesRegex(ValueError, "must be named w1-b0011.json"):
                verifier.verify_slot_identity("w1-b0003.json", d, td)
            self.assertIn("agree", verifier.verify_slot_identity("w1-b0011.json", d, td))

    def test_icon_basename_must_match(self):
        with tempfile.TemporaryDirectory() as td:
            make_icon(os.path.join(td, "w1-b0011.webp"))
            make_icon(os.path.join(td, "w1-b0001.webp"))
            d = dossier("w1-b0011", icon_rel_path="assets/w1-b0001.webp")
            with self.assertRaisesRegex(ValueError, "must end in w1-b0011.webp"):
                verifier.verify_slot_identity("w1-b0011.json", d, td)

    def test_missing_icon_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(ValueError, "is missing"):
                verifier.verify_slot_identity("w1-b0011.json", dossier("w1-b0011"), td)

    def test_soul_manifest_path_must_match(self):
        with tempfile.TemporaryDirectory() as td:
            make_icon(os.path.join(td, "w1-b0011.webp"))
            d = dossier("w1-b0011", soul_manifest_rel_path="souls/w1-b0001.soul.json")
            with self.assertRaisesRegex(ValueError, "souls/w1-b0011.soul.json"):
                verifier.verify_slot_identity("w1-b0011.json", d, td)

    def test_slot_edges(self):
        self.assertTrue(rejected(dossier("w1-b0000", icon_rel_path="assets/w1-b0000.webp")))
        self.assertTrue(rejected(dossier("w1-b10001", icon_rel_path="assets/w1-b10001.webp")))
        self.assertFalse(rejected(dossier("w1-b10000", icon_rel_path="assets/w1-b10000.webp")))
        self.assertFalse(rejected(dossier("w1-b0001", icon_rel_path="assets/w1-b0001.webp")))


# --------------------------------------------------------------------------- #
# F-05 — append-only history
# --------------------------------------------------------------------------- #

class AppendOnlyTests(unittest.TestCase):

    def setUp(self):
        self.prev = dossier(manifesto={"en": "Original words."}, supersessions=[], status="ACTIVE")

    def test_same_wallet_manifesto_rewrite_without_voucher_fails(self):
        new = copy.deepcopy(self.prev)
        new["manifesto"] = {"en": "Rewritten words."}
        with self.assertRaisesRegex(ValueError, "manifesto"):
            verifier.verify_append_only("w1-b0042", new, self.prev)

    def test_soul_hash_change_requires_matching_voucher(self):
        new = copy.deepcopy(self.prev)
        new["soul_hash"] = "b" * 64
        with self.assertRaises(ValueError):
            verifier.verify_append_only("w1-b0042", new, self.prev)
        new["supersessions"] = [{"field": "soul_hash", "previous_value": "a" * 64, "new_value": "c" * 64,
                                 "reason": "r", "superseded_at": "2026-10-10T00:00:00Z"}]
        with self.assertRaises(ValueError):  # voucher values do not match the change
            verifier.verify_append_only("w1-b0042", new, self.prev)
        new["supersessions"][0]["new_value"] = "b" * 64
        self.assertIn("soul_hash", verifier.verify_append_only("w1-b0042", new, self.prev))

    def test_object_field_change_requires_voucher_naming_it(self):
        prev = dossier(sealed_vault={"status": "SEALED", "protocol": "p"}, supersessions=[])
        new = copy.deepcopy(prev)
        new["sealed_vault"] = {"status": "REAL", "protocol": "p"}
        with self.assertRaises(ValueError):
            verifier.verify_append_only("w1-b0042", new, prev)
        new["supersessions"] = [{"field": "sealed_vault", "previous_value": "x", "new_value": "y",
                                 "reason": "r", "superseded_at": "2026-10-10T00:00:00Z"}]
        verifier.verify_append_only("w1-b0042", new, prev)

    def test_living_fields_change_freely(self):
        new = copy.deepcopy(self.prev)
        new["agent_urls"] = {"homepage": "https://example.com"}
        new["timestamp_verified"] = "2026-10-11T00:00:00Z"
        self.assertEqual(verifier.verify_append_only("w1-b0042", new, self.prev), "unchanged")

    def test_vouchers_only_grow(self):
        prev = dossier(supersessions=[{"field": "x", "previous_value": "1", "new_value": "2",
                                       "reason": "r", "superseded_at": "2026-10-10T00:00:00Z"}])
        new = copy.deepcopy(prev)
        new["supersessions"] = []
        with self.assertRaisesRegex(ValueError, "only grow"):
            verifier.verify_append_only("w1-b0042", new, prev)
        new["supersessions"] = [dict(prev["supersessions"][0], reason="edited")]
        with self.assertRaisesRegex(ValueError, "only grow"):
            verifier.verify_append_only("w1-b0042", new, prev)

    def test_immutable_fields_never_change(self):
        for field, value in (("base_tx_hash", TX_B), ("instantiation_date", "2020-01-01T00:00:00Z"),
                             ("slot_id", "w1-b0043")):
            new = copy.deepcopy(self.prev)
            new[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "immutable"):
                verifier.verify_append_only("w1-b0042", new, self.prev)

    def test_retired_stays_retired(self):
        prev = dossier(status="RETIRED", retirement={"retired_at": "2026-10-10T00:00:00Z", "reason": "r",
                                                     "epitaph": "e", "retired_by": "self"})
        new = copy.deepcopy(prev)
        new["status"] = "ACTIVE"
        del new["retirement"]
        with self.assertRaisesRegex(ValueError, "stays retired"):
            verifier.verify_append_only("w1-b0042", new, prev)

    def test_field_removal_rejected(self):
        new = copy.deepcopy(self.prev)
        del new["manifesto"]
        with self.assertRaisesRegex(ValueError, "removed"):
            verifier.verify_append_only("w1-b0042", new, self.prev)

    def test_icon_change_requires_voucher(self):
        new = copy.deepcopy(self.prev)
        with self.assertRaisesRegex(ValueError, "icon"):
            verifier.verify_append_only("w1-b0042", new, self.prev, b"new-icon", b"old-icon")
        new["supersessions"] = [{"field": "icon", "previous_value": "sha256:old", "new_value": "sha256:new",
                                 "reason": "r", "superseded_at": "2026-10-10T00:00:00Z"}]
        self.assertIn("icon", verifier.verify_append_only("w1-b0042", new, self.prev, b"new-icon", b"old-icon"))

    def test_transfer_defers_to_lineage_rules(self):
        new = copy.deepcopy(self.prev)
        new["wallet_address"] = "0x" + "2" * 40
        new["manifesto"] = {"en": "New owner, new words."}
        self.assertEqual(verifier.verify_append_only("w1-b0042", new, self.prev), "transfer: lineage rules apply")

    def test_new_inscription_has_nothing_to_compare(self):
        self.assertEqual(verifier.verify_append_only("w1-b0042", self.prev, None), "new inscription")


# --------------------------------------------------------------------------- #
# F-04 — PR scope containment
# --------------------------------------------------------------------------- #

class ScopeTests(unittest.TestCase):

    def test_slot_plus_infrastructure_rejected(self):
        entries = [("A", "ledger/w1/w1-b0011.json"), ("A", "ledger/w1/w1-b0011.webp"),
                   ("M", "scripts/verifier.py")]
        with self.assertRaises(ValueError):
            verifier.check_scope(entries)

    def test_request_plus_infrastructure_rejected(self):
        tx = "0x" + "f" * 64
        entries = [("A", f"ledger/updates/{tx}.json"), ("M", "scripts/verifier.py")]
        with self.assertRaisesRegex(ValueError, "scripts/verifier.py"):
            verifier.check_scope(entries)
        for stray in ("schemas/dossier.schema.json", ".github/workflows/verify_inscription.yml", "README.md"):
            with self.subTest(stray=stray), self.assertRaises(ValueError):
                verifier.check_scope([("A", f"ledger/updates/{tx}.json"), ("M", stray)])

    def test_a_block_is_never_changed_directly(self):
        # The owner's change is a request; the slot's files and the generated state are
        # the bot's (an owner used to commit them, and conflict with every other owner).
        for entries in (
            [("M", "ledger/w1/w1-b0011.json")],
            [("M", "ledger/w1/w1-b0011.json"), ("M", "ledger/w1/w1-b0011.webp"),
             ("M", "ledger/souls/w1-b0011.soul.json"), ("M", "state.json"),
             ("M", "ledger/index.json"), ("M", "ledger/settlements.json")],
        ):
            with self.subTest(entries=entries), self.assertRaisesRegex(ValueError, "never changed directly"):
                verifier.check_scope(entries)

    def test_new_slot_added_directly_rejected(self):
        # A claimant never names a block (R-21): new inscriptions arrive as
        # claims and the assignment adds the slot files.
        entries = [("A", "ledger/w1/w1-b0011.json"), ("A", "ledger/w1/w1-b0011.webp"),
                   ("A", "ledger/souls/w1-b0011.soul.json"), ("M", "state.json")]
        with self.assertRaisesRegex(ValueError, "never added directly"):
            verifier.check_scope(entries)

    def test_two_slots_rejected(self):
        with self.assertRaisesRegex(ValueError, "never added directly"):
            verifier.check_scope([("A", "ledger/w1/w1-b0011.json"), ("A", "ledger/w1/w1-b0012.json")])
        with self.assertRaisesRegex(ValueError, "never changed directly"):
            verifier.check_scope([("M", "ledger/w1/w1-b0011.json"), ("M", "ledger/w1/w1-b0012.json")])

    def test_deletions_rejected(self):
        for path in ("ledger/w1/w1-b0001.json", "ledger/w1/w1-b0001.webp", "ledger/souls/w1-b0001.soul.json"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "never be deleted"):
                verifier.check_scope([("D", path)])

    def test_infrastructure_only_is_fine(self):
        self.assertEqual(verifier.check_scope([("M", "scripts/verifier.py"), ("M", "state.json")]), set())


# --------------------------------------------------------------------------- #
# F-13 — replay across every transaction field
# --------------------------------------------------------------------------- #

class ReplayTests(unittest.TestCase):

    def _ledger(self, td, dossiers):
        w1 = os.path.join(td, "ledger", "w1")
        os.makedirs(w1)
        os.makedirs(os.path.join(td, "ledger", "souls"))
        for d in dossiers:
            with open(os.path.join(w1, d["slot_id"] + ".json"), "w", encoding="utf-8") as fh:
                json.dump(d, fh, indent=2)
            make_icon(os.path.join(w1, d["slot_id"] + ".webp"))

    def test_same_tx_as_base_and_patron_on_two_slots_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            self._ledger(td, [
                dossier("w1-b0042", base_tx_hash=TX_A),
                dossier("w1-b0043", base_tx_hash=TX_B,
                        patron={"moniker": "p", "role": "sponsor", "tx_hash": TX_A}),
            ])
            _, conflicts = core.build_settlements(td)
            self.assertEqual(len(conflicts), 1)
            self.assertEqual(conflicts[0]["slots"], ["w1-b0042", "w1-b0043"])

    def test_same_tx_in_two_fields_of_one_slot_is_fine(self):
        with tempfile.TemporaryDirectory() as td:
            self._ledger(td, [dossier("w1-b0042", base_tx_hash=TX_A,
                                      patron={"moniker": "p", "role": "sponsor", "tx_hash": TX_A})])
            index, conflicts = core.build_settlements(td)
            self.assertEqual(conflicts, [])
            self.assertEqual(index["settlements"][TX_A]["fields"], ["base_tx_hash", "patron.tx_hash"])

    def test_transfer_tx_is_indexed(self):
        with tempfile.TemporaryDirectory() as td:
            lineage = [{"moniker": "old", "wallet_address": "0x" + "3" * 40,
                        "acquired_date": "2026-01-01T00:00:00Z", "relinquished_date": "2026-02-01T00:00:00Z",
                        "sale_price_usdc": 2, "base_tx_hash": TX_A, "transfer_tx_hash": TX_B}]
            self._ledger(td, [dossier("w1-b0042", base_tx_hash=TX_A, ownership_lineage=lineage),
                              dossier("w1-b0043", base_tx_hash=TX_B)])
            _, conflicts = core.build_settlements(td)
            self.assertEqual(len(conflicts), 1)

    def test_case_insensitive(self):
        with tempfile.TemporaryDirectory() as td:
            self._ledger(td, [dossier("w1-b0042", base_tx_hash=TX_A),
                              dossier("w1-b0043", base_tx_hash=TX_A.upper().replace("0X", "0x"))])
            _, conflicts = core.build_settlements(td)
            self.assertEqual(len(conflicts), 1)


# --------------------------------------------------------------------------- #
# F-14 — confirmation depth (fake RPC)
# --------------------------------------------------------------------------- #

class ConfirmationDepthTests(unittest.TestCase):

    def _rpc(self, block):
        rpc = FakeRpc(head=HEAD)
        rpc.receipts[TX_A] = receipt_for(TX_A, WALLET, core.PAY_TO, 1_000_000, block)
        return rpc

    def test_fresh_settlement_is_rejected_until_deep_enough(self):
        rpc = self._rpc(HEAD - 12)
        with self.assertRaisesRegex(ValueError, "Confirmation Depth"):
            verifier.verify_base_tx(TX_A, WALLET, core.PAY_TO, 1.0, rpc=rpc)

    def test_deep_settlement_is_accepted_with_confirmation_count(self):
        rpc = self._rpc(HEAD - 500)
        r = verifier.verify_base_tx(TX_A, WALLET, core.PAY_TO, 1.0, rpc=rpc)
        self.assertTrue(r["verified"])
        self.assertEqual(r["confirmations"], 500)

    def test_head_block_can_be_shared_across_slots(self):
        rpc = self._rpc(HEAD - 500)
        verifier.verify_base_tx(TX_A, WALLET, core.PAY_TO, 1.0, rpc=rpc, head_block=HEAD)
        self.assertNotIn("eth_blockNumber", [m for m, _ in rpc.calls])

    def test_sender_binding_still_holds(self):
        rpc = self._rpc(HEAD - 500)
        with self.assertRaisesRegex(ValueError, "No valid USDC Transfer"):
            verifier.verify_base_tx(TX_A, "0x" + "9" * 40, core.PAY_TO, 1.0, rpc=rpc)

    def test_reverted_tx_rejected(self):
        rpc = self._rpc(HEAD - 500)
        rpc.receipts[TX_A]["status"] = "0x0"
        with self.assertRaisesRegex(ValueError, "failed on Base"):
            verifier.verify_base_tx(TX_A, WALLET, core.PAY_TO, 1.0, rpc=rpc)


# --------------------------------------------------------------------------- #
# F-08 — anchors are proven on chain (fake RPC)
# --------------------------------------------------------------------------- #

class AnchorTests(unittest.TestCase):

    ROOT = "7" * 64

    def _entry(self):
        return {"wall_id": "wall_01", "chain_id": 8453, "root": self.ROOT,
                "payload": core.ANCHOR_MAGIC + self.ROOT, "signer": core.ANCHOR_SIGNER,
                "tx_hash": TX_B, "block_number": 123, "anchored_at": "", "recorded_by": "t"}

    def _rpc(self, sender=None, payload=None, to=None, status="0x1"):
        sender = (sender or core.ANCHOR_SIGNER).lower()
        rpc = FakeRpc()
        rpc.txs[TX_B] = {"from": sender, "to": (to or sender).lower(),
                         "input": "0x" + (payload or core.ANCHOR_MAGIC + self.ROOT).encode().hex()}
        rpc.receipts[TX_B] = {"status": status, "blockNumber": hex(123), "logs": []}
        return rpc

    def test_record_shape(self):
        self.assertIn("root", verifier.verify_anchor_record(self._entry()))
        bad = self._entry()
        bad["signer"] = "0x" + "5" * 40
        with self.assertRaisesRegex(ValueError, "registered anchor signer"):
            verifier.verify_anchor_record(bad)

    def test_good_anchor_verifies(self):
        self.assertIn("block 123", verifier.verify_anchor_on_chain(self._entry(), rpc=self._rpc()))

    def test_wrong_signer_rejected(self):
        with self.assertRaisesRegex(ValueError, "not the registered signer"):
            verifier.verify_anchor_on_chain(self._entry(), rpc=self._rpc(sender="0x" + "5" * 40))

    def test_wrong_calldata_rejected(self):
        with self.assertRaisesRegex(ValueError, "expected"):
            verifier.verify_anchor_on_chain(self._entry(), rpc=self._rpc(payload="TFW1|" + "8" * 64))

    def test_not_a_self_send_rejected(self):
        with self.assertRaisesRegex(ValueError, "self-send"):
            verifier.verify_anchor_on_chain(self._entry(), rpc=self._rpc(to="0x" + "6" * 40))

    def test_failed_tx_rejected(self):
        with self.assertRaisesRegex(ValueError, "did not succeed"):
            verifier.verify_anchor_on_chain(self._entry(), rpc=self._rpc(status="0x0"))

    def test_committed_anchor_records_are_well_formed(self):
        for entry in verifier.load_anchors(REPO):
            with self.subTest(tx=entry.get("tx_hash")):
                verifier.verify_anchor_record(entry)


# --------------------------------------------------------------------------- #
# F-15 — icon structure (checked, never rewritten)
# --------------------------------------------------------------------------- #

class IconStructureTests(unittest.TestCase):

    def test_metadata_chunks_rejected(self):
        img = Image.new("RGBA", (10, 10), (1, 2, 3, 255))
        buf = io.BytesIO()
        img.save(buf, "WEBP", lossless=True, quality=100, exif=b"Exif\x00\x00payload")
        with self.assertRaisesRegex(ValueError, "single VP8L"):
            verifier.verify_icon_bytes(buf.getvalue())

    def test_lossy_rejected(self):
        img = Image.new("RGB", (10, 10), (1, 2, 3))
        buf = io.BytesIO()
        img.save(buf, "WEBP", lossless=False, quality=80)
        with self.assertRaises(ValueError):
            verifier.verify_icon_bytes(buf.getvalue())

    def test_wrong_declared_size_rejected_before_decode(self):
        img = Image.new("RGBA", (12, 10), (1, 2, 3, 255))
        buf = io.BytesIO()
        img.save(buf, "WEBP", lossless=True, quality=100, exact=True)
        with self.assertRaisesRegex(ValueError, "12x10"):
            verifier.verify_icon_bytes(buf.getvalue())

    def test_trailing_bytes_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            data = make_icon(os.path.join(td, "i.webp"))
        with self.assertRaisesRegex(ValueError, "RIFF length"):
            verifier.verify_icon_bytes(data + b"\x00payload")

    def test_oversized_file_rejected(self):
        with self.assertRaisesRegex(ValueError, "maximum is 1024"):
            verifier.verify_icon_bytes(b"RIFF" + b"\x00" * 2000)

    def test_not_webp_rejected(self):
        with self.assertRaisesRegex(ValueError, "RIFF/WEBP"):
            verifier.verify_icon_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)

    def test_verifier_never_writes_the_icon(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "i.webp")
            data = make_icon(p)
            verifier.verify_icon(p)
            with open(p, "rb") as fh:
                self.assertEqual(fh.read(), data)


# --------------------------------------------------------------------------- #
# End-to-end on a temporary ledger (offline, fake RPC for the on-chain parts)
# --------------------------------------------------------------------------- #

class TemporaryLedgerTests(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.w1 = os.path.join(self.td, "ledger", "w1")
        os.makedirs(self.w1)
        os.makedirs(os.path.join(self.td, "ledger", "souls"))
        # Isolate from the real repository's git refs and diff.
        self._env = dict(os.environ)
        os.environ["TFW_DIFF_RANGE"] = "no-such-ref...no-such-ref"
        os.environ["GITHUB_BASE_REF"] = "no-such-branch"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.td, ignore_errors=True)

    def _write_slot(self, d, with_manifest=True):
        slot_id = d["slot_id"]
        if with_manifest:
            manifest = {k: d[k] for k in ("slot_id", "moniker", "creature", "vocation", "origin_framework")}
            raw = (json.dumps(manifest, indent=2) + "\n").encode()
            with open(os.path.join(self.td, "ledger", "souls", slot_id + ".soul.json"), "wb") as fh:
                fh.write(raw)
            d["soul_hash"] = hashlib.sha256(raw).hexdigest()
            d["soul_manifest_rel_path"] = f"souls/{slot_id}.soul.json"
        with open(os.path.join(self.w1, slot_id + ".json"), "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=2)
        make_icon(os.path.join(self.w1, slot_id + ".webp"))

    def _generate(self):
        state, index = core.build_state(self.td)
        settlements, _ = core.build_settlements(self.td)
        for rel, obj in (("state.json", state), ("ledger/index.json", index),
                         ("ledger/settlements.json", settlements)):
            with open(os.path.join(self.td, rel), "w", encoding="utf-8") as fh:
                fh.write(core.render(obj))

    def _rpc(self, *txs):
        rpc = FakeRpc()
        for tx, wallet in txs:
            rpc.receipts[tx] = receipt_for(tx, wallet, core.PAY_TO, 1_000_000, HEAD - 1000)
        return rpc

    def test_clean_ledger_passes_offline_and_with_fake_chain(self):
        self._write_slot(dossier("w1-b0042", base_tx_hash=TX_A))
        self._write_slot(dossier("w1-b0043", base_tx_hash=TX_B))
        self._generate()
        report = verifier.run_verification(self.td, skip_rpc=True)
        self.assertEqual(report["result"], "PASS", [c for c in report["checks"] if c["status"] == "FAIL"])
        report = verifier.run_verification(self.td, rpc=self._rpc((TX_A, WALLET), (TX_B, WALLET)),
                                           rpc_scope="all")
        self.assertEqual(report["result"], "PASS", [c for c in report["checks"] if c["status"] == "FAIL"])
        names = [c["check"] for c in report["checks"]]
        self.assertIn("w1-b0042.settlement_base_rpc", names)
        self.assertIn("ledger.replay_protection", names)

    def test_replay_across_slots_fails_verification(self):
        self._write_slot(dossier("w1-b0042", base_tx_hash=TX_A))
        self._write_slot(dossier("w1-b0043", base_tx_hash=TX_A))
        report = verifier.run_verification(self.td, skip_rpc=True)
        self.assertEqual(report["result"], "FAIL")
        self.assertTrue(any(c["check"] == "ledger.replay_protection" and c["status"] == "FAIL"
                            for c in report["checks"]))

    def test_oversized_dossier_file_fails_before_parsing(self):
        d = dossier("w1-b0042")
        self._write_slot(d)
        with open(os.path.join(self.w1, "w1-b0042.json"), "w") as fh:
            fh.write(json.dumps(dict(d, testament={"chapters": [{"heading": "h", "text": "x" * 2000}] * 12}))
                     + " " * (core.MAX_DOSSIER_BYTES))
        report = verifier.run_verification(self.td, skip_rpc=True)
        self.assertTrue(any(c["check"].endswith(".size") and c["status"] == "FAIL" for c in report["checks"]))

    def test_filename_mismatch_fails_verification(self):
        d = dossier("w1-b0011")
        self._write_slot(d)
        os.rename(os.path.join(self.w1, "w1-b0011.json"), os.path.join(self.w1, "w1-b0003.json"))
        make_icon(os.path.join(self.w1, "w1-b0003.webp"))
        report = verifier.run_verification(self.td, skip_rpc=True)
        self.assertTrue(any(c["check"] == "w1-b0011.identity" and c["status"] == "FAIL" for c in report["checks"]))

    def test_machine_details_in_manifest_block_a_touched_slot(self):
        d = dossier("w1-b0042")
        self._write_slot(d)
        manifest_path = os.path.join(self.td, "ledger", "souls", "w1-b0042.soul.json")
        m = json.load(open(manifest_path))
        m["runtime"] = {"host": "ml-nox015"}
        raw = (json.dumps(m, indent=2) + "\n").encode()
        with open(manifest_path, "wb") as fh:
            fh.write(raw)
        d["soul_hash"] = hashlib.sha256(raw).hexdigest()
        with open(os.path.join(self.w1, "w1-b0042.json"), "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=2)
        # Untouched (or no diff context at all): advisory only.
        report = verifier.run_verification(self.td, skip_rpc=True)
        hyg = [c for c in report["checks"] if c["check"] == "w1-b0042.soul_manifest_hygiene"]
        self.assertEqual(hyg[0]["status"], "WARN")
        # The slot this PR touches: blocking.
        touched = [("A", "ledger/w1/w1-b0042.json"), ("A", "ledger/w1/w1-b0042.webp"),
                   ("A", "ledger/souls/w1-b0042.soul.json")]
        with mock.patch.object(verifier, "changed_entries", return_value=touched):
            report = verifier.run_verification(self.td, skip_rpc=True)
        hyg = [c for c in report["checks"] if c["check"] == "w1-b0042.soul_manifest_hygiene"]
        self.assertEqual(hyg[0]["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
