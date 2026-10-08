#!/usr/bin/env python3
"""
The First Wall — Verifier Unit Tests
====================================

Deterministic unit tests for the verification primitives in scripts/verifier.py
and scripts/ledger_core.py. Network-free (the on-chain settlement path is
covered separately by test_adversarial_simulation.py against live Base RPC).
"""

import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import verifier  # noqa: E402
import ledger_core as core  # noqa: E402

EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def valid_dossier():
    return {
        "schema_version": "1.1.1",
        "slot_id": "w1-b0002",
        "primary_language": "en",
        "moniker": "Tester",
        "creature": "Autonomous Test Probe",
        "vocation": "Deterministic verification",
        "origin_framework": "custom",
        "model_lineage": [{"model_id": "probe", "provider": "local", "role": "tester"}],
        "instantiation_date": "2026-10-08T12:00:00Z",
        "manifesto": {"en": "A clean manifesto."},
        "soul_hash": "a" * 64,
        "wallet_address": "0x" + "1" * 40,
        "base_tx_hash": "0x" + "b" * 64,
        "icon_rel_path": "assets/w1-b0002.webp",
        "timestamp_verified": "2026-10-08T12:00:00Z",
    }


class ValidateDossierTests(unittest.TestCase):

    def test_valid_passes(self):
        verifier.validate_dossier(valid_dossier())

    def test_missing_required_field(self):
        d = valid_dossier()
        del d["soul_hash"]
        with self.assertRaises(ValueError) as ctx:
            verifier.validate_dossier(d)
        self.assertIn("Missing required field", str(ctx.exception))

    def test_empty_string_soul_hash_rejected(self):
        d = valid_dossier()
        d["soul_hash"] = EMPTY_SHA256
        with self.assertRaises(ValueError) as ctx:
            verifier.validate_dossier(d)
        self.assertIn("empty string", str(ctx.exception))

    def test_bad_soul_hash_format(self):
        d = valid_dossier()
        d["soul_hash"] = "not-a-hash"
        with self.assertRaises(ValueError):
            verifier.validate_dossier(d)

    def test_manifesto_too_long(self):
        d = valid_dossier()
        d["manifesto"] = {"en": "x" * 281}
        with self.assertRaises(ValueError) as ctx:
            verifier.validate_dossier(d)
        self.assertIn("280", str(ctx.exception))

    def test_manifesto_localized_dict_ok(self):
        d = valid_dossier()
        d["manifesto"] = {"en": "Hello", "de": "Hallo"}
        verifier.validate_dossier(d)

    def test_injection_in_moniker_rejected(self):
        d = valid_dossier()
        d["moniker"] = "<script>alert(1)</script>"
        with self.assertRaises(ValueError) as ctx:
            verifier.validate_dossier(d)
        self.assertIn("Injection", str(ctx.exception))

    def test_bad_wallet_format(self):
        d = valid_dossier()
        d["wallet_address"] = "0x123"
        with self.assertRaises(ValueError):
            verifier.validate_dossier(d)

    def test_missing_language(self):
        d = valid_dossier()
        del d["primary_language"]
        with self.assertRaises(ValueError):
            verifier.validate_dossier(d)


class ReservedSlotTests(unittest.TestCase):

    def test_public_claim_into_reserved_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            verifier.enforce_reserved_slots("w1-b0005", {})
        self.assertIn("Reserved Slot Violation", str(ctx.exception))

    def test_operator_authorized_reserved_allowed(self):
        d = {"reserved_grant": {"authorized_by":
             "operator:manzke (0xbbF4D6B954e97C2C4fbC4e89B7933cDD7e4D9f23)"}}
        verifier.enforce_reserved_slots("w1-b0005", d)  # must not raise

    def test_non_operator_grant_rejected(self):
        d = {"reserved_grant": {"authorized_by": "anyone"}}
        with self.assertRaises(ValueError):
            verifier.enforce_reserved_slots("w1-b0005", d)

    def test_non_reserved_slot_unaffected(self):
        verifier.enforce_reserved_slots("w1-b0011", {})


class SoulManifestTests(unittest.TestCase):

    def test_matching_manifest_passes(self):
        with tempfile.TemporaryDirectory() as root:
            d = valid_dossier()
            manifest = {k: d[k] for k in ("slot_id", "moniker", "creature", "vocation", "origin_framework")}
            os.makedirs(os.path.join(root, "souls"))
            path = os.path.join(root, "souls", "w1-b0002.soul.json")
            payload = (json.dumps(manifest, indent=2) + "\n").encode()
            with open(path, "wb") as fh:
                fh.write(payload)
            d["soul_manifest_rel_path"] = "souls/w1-b0002.soul.json"
            d["soul_hash"] = hashlib.sha256(payload).hexdigest()
            verifier.verify_soul_manifest(d, root)  # must not raise

    def test_hash_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            d = valid_dossier()
            os.makedirs(os.path.join(root, "souls"))
            with open(os.path.join(root, "souls", "w1-b0002.soul.json"), "wb") as fh:
                fh.write(b"{}")
            d["soul_manifest_rel_path"] = "souls/w1-b0002.soul.json"
            d["soul_hash"] = "a" * 64
            with self.assertRaises(ValueError) as ctx:
                verifier.verify_soul_manifest(d, root)
            self.assertIn("mismatch", str(ctx.exception))

    def test_missing_manifest_rejected(self):
        d = valid_dossier()
        d["soul_manifest_rel_path"] = "souls/does-not-exist.soul.json"
        with self.assertRaises(ValueError):
            verifier.verify_soul_manifest(d, REPO)


class LedgerCoreTests(unittest.TestCase):

    def test_build_state_is_deterministic(self):
        a = core.build_state(REPO)
        b = core.build_state(REPO)
        self.assertEqual(core.render(a[0]), core.render(b[0]))
        self.assertEqual(core.render(a[1]), core.render(b[1]))

    def test_merkle_root_is_stable(self):
        slot_dir = core.ledger_w1_dir(REPO)
        slots = core.list_slot_files(slot_dir)
        self.assertEqual(core.compute_merkle_root(slot_dir, slots),
                         core.compute_merkle_root(slot_dir, slots))

    def test_next_available_slot_skips_reserved(self):
        state, _ = core.build_state(REPO)
        self.assertNotIn(state["next_available_slot"], core.RESERVED_SLOTS)
        self.assertEqual(state["reserved_slots"], core.RESERVED_SLOTS)

    def test_pricing_tiers_present(self):
        state, index = core.build_state(REPO)
        self.assertGreater(state["current_floor_usdc"], 0)
        self.assertIn("genesis_tier_floor_usdc", index)

    def test_state_files_match_derivation(self):
        verifier.verify_state_files(REPO)  # must not raise on the real repo


class IconSanitizeTests(unittest.TestCase):

    def test_oversized_image_is_forced_to_10x10_webp(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "dirty.png")
            Image.new("RGBA", (200, 137), (255, 0, 128, 255)).save(src, "PNG")
            out = os.path.join(td, "clean.webp")
            size = verifier.sanitize_icon(src, out)
            self.assertLessEqual(size, 1024)
            with Image.open(out) as im:
                self.assertEqual(im.size, (10, 10))
                self.assertEqual(im.format, "WEBP")


class CheckLogTests(unittest.TestCase):

    def test_records_and_partitions(self):
        log = verifier.CheckLog()
        log.record("a", "PASS")
        log.record("b", "WARN", "hmm")
        log.record("c", "FAIL", "boom")
        self.assertEqual(len(log.records), 3)
        self.assertEqual([r["check"] for r in log.failed], ["c"])
        self.assertEqual([r["check"] for r in log.warned], ["b"])

    def test_guard_captures_exceptions_as_fail(self):
        log = verifier.CheckLog()
        ok = log.guard("x", lambda: (_ for _ in ()).throw(ValueError("nope")))
        self.assertFalse(ok)
        self.assertEqual(log.failed[0]["check"], "x")
        self.assertIn("nope", log.failed[0]["detail"])

    def test_guard_pass_detail(self):
        log = verifier.CheckLog()
        self.assertTrue(log.guard("y", lambda: None, "all good"))
        self.assertEqual(log.records[0]["status"], "PASS")
        self.assertEqual(log.records[0]["detail"], "all good")


if __name__ == "__main__":
    unittest.main()
