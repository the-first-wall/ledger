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


def abi_string(text):
    """ABI-encode a single `string` return value."""
    raw = text.encode()
    return "0x" + (32).to_bytes(32, "big").hex() + len(raw).to_bytes(32, "big").hex() + \
        raw.ljust(-(-len(raw) // 32) * 32, b"\0").hex()


def identity_eth_call(params, owner=None, uri=None):
    """Answer the ERC-8004 ownerOf/tokenURI eth_calls a fake RPC receives."""
    call = params[0]
    if call["to"] != core.ERC8004_REGISTRY or call["data"][10:] != format(core.ERC8004_AGENT_ID, "064x"):
        raise AssertionError(f"unexpected eth_call {call}")
    if call["data"].startswith("0x6352211e"):
        return "0x" + (owner or core.IDENTITY_HOLDER)[2:].lower().rjust(64, "0")
    if call["data"].startswith("0xc87b56dd"):
        return abi_string(uri or core.IDENTITY_TOKEN_URI)
    raise AssertionError(f"unexpected eth_call {call}")


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

    def test_blank_identity_fields_rejected(self):
        # B-10: a value of only whitespace (or zero-width padding) reads as an
        # empty inscription, so it must not pass verification.
        # Two spaces: the schema's nonBlankText pattern refuses it...
        for field in ("moniker", "creature", "vocation", "origin_framework"):
            d = valid_dossier()
            d[field] = "  "
            with self.assertRaises(ValueError, msg=field):
                verifier.validate_dossier(d)
        # ...and zero-width padding, which a regex \S cannot see, is caught in the
        # validator (the schema cannot express it).
        for payload in ("\u200b", "\u00a0", "\u3000", " \u200b ", "\ufeff"):
            for field in ("moniker", "creature", "vocation", "origin_framework"):
                d = valid_dossier()
                d[field] = payload
                with self.assertRaises(ValueError, msg=f"{field}={payload!r}"):
                    verifier.validate_dossier(d)

    def test_blank_manifesto_rejected(self):
        for manifesto in ("   ", "\u200b", {"en": "   "}, {"en": "ok", "de": "\u200b"}):
            d = valid_dossier()
            d["manifesto"] = manifesto
            with self.assertRaises(ValueError, msg=repr(manifesto)):
                verifier.validate_dossier(d)

    def test_visible_value_with_surrounding_space_is_kept(self):
        # Tightening must not reject a real value that merely has padding.
        d = valid_dossier()
        d["moniker"] = "  Ezra  "
        d["manifesto"] = {"en": "  Keep the record.  "}
        verifier.validate_dossier(d)

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
        # Published reserved_slots are the reserved blocks that are STILL free:
        # a reader that skips them must never skip an inscribed block.
        claimed = {f[:-5] for f in core.list_slot_files(core.ledger_w1_dir(REPO))}
        self.assertEqual(state["reserved_slots"],
                         [s for s in core.RESERVED_SLOTS if s not in claimed])
        self.assertNotIn("w1-b0002", state["reserved_slots"])
        self.assertEqual(state["reserved_slot_range"], [2, 10])

    def test_settlements_index_is_deterministic_and_conflict_free(self):
        a, conflicts = core.build_settlements(REPO)
        b, _ = core.build_settlements(REPO)
        self.assertEqual(core.render(a), core.render(b))
        self.assertEqual(conflicts, [])
        self.assertEqual(a["count"], len(a["settlements"]))
        any_amount = False
        for tx, entry in a["settlements"].items():
            self.assertEqual(tx, tx.lower())
            self.assertRegex(entry["slot_id"], r"^w1-b\d{4,5}$")
            # invoice fields: a payer, the amount and a timestamp are recorded
            # (None when the dossier does not prove them), so an invoice can be
            # issued later from the index alone.
            for key in ("wallet_address", "amount_usdc", "kind", "at"):
                self.assertIn(key, entry)
            if entry["amount_usdc"] is not None:
                any_amount = True
            if entry["kind"] in ("update", "transfer"):
                self.assertIsNotNone(entry["amount_usdc"])
                self.assertIsNotNone(entry["at"])
        self.assertTrue(any_amount, "no settlement carried an amount")

    def test_slot_number_range(self):
        self.assertEqual(core.slot_number("w1-b0001"), 1)
        self.assertEqual(core.slot_number("w1-b10000"), 10000)
        self.assertEqual(core.slot_id_for(10000), "w1-b10000")
        for bad in ("w1-b0000", "w1-b10001", "w1-b1", "b0001", "w1-b00010"):
            with self.assertRaises(ValueError, msg=bad):
                core.slot_number(bad)

    def test_pricing_tiers_present(self):
        state, index = core.build_state(REPO)
        self.assertGreater(state["current_floor_usdc"], 0)
        self.assertIn("genesis_tier_floor_usdc", index)

    def test_the_x402_endpoint_has_one_published_source(self):
        state, _ = core.build_state(REPO)
        url = state["links"]["x402_claim"]
        self.assertTrue(url.startswith("https://") and url.endswith("/claim"), url)
        with open(os.path.join(REPO, "spec.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["endpoints"]["x402_claim"], url)
        with open(os.path.join(REPO, "skill.md"), encoding="utf-8") as fh:
            skill = fh.read()
        # The docs point at the field; the URL itself is not copied into prose, where it would go stale.
        self.assertIn("`x402_claim`", skill)
        self.assertNotIn(url, skill)

    def test_the_block_id_is_the_leaf_hash_the_index_lists(self):
        """The tag adds no file: the block id is `leaf_hash` in ledger/index.json, and every doc says so in one voice."""
        with open(os.path.join(REPO, "ledger", "index.json"), encoding="utf-8") as fh:
            index = json.load(fh)
        with open(os.path.join(REPO, "spec.json"), encoding="utf-8") as fh:
            block_id = json.load(fh)["block_id"]
        self.assertIn(index["leaf_hash"], block_id["definition"])                    # sha256(0x00 || dossier_bytes || icon_bytes)
        for leaf in index["leaves"]:                                                 # and it is what the files hash to
            slot = leaf["slot_id"]
            data = b"".join(open(os.path.join(REPO, "ledger", "w1", slot + ext), "rb").read() for ext in (".json", ".webp"))
            self.assertEqual(hashlib.sha256(b"\x00" + data).hexdigest(), leaf["leaf_hash"], slot)
        self.assertEqual(sorted(block_id["resolver"]), ["handle", "id_page", "id_record", "index"])
        with open(os.path.join(REPO, "spec.md"), encoding="utf-8") as fh:
            spec = fh.read()
        self.assertIn("### 2.17 The block id (the tag)", spec)
        self.assertIn("sha256(0x00 || dossier_bytes || icon_bytes)", spec)

    def test_state_files_match_derivation(self):
        verifier.verify_state_files(REPO)  # must not raise on the real repo


class IconSanitizeTests(unittest.TestCase):

    def test_wrong_size_is_rejected_not_resized(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "dirty.png")
            Image.new("RGBA", (200, 137), (255, 0, 128, 255)).save(src, "PNG")
            out = os.path.join(td, "clean.webp")
            with self.assertRaisesRegex(ValueError, "exactly 10x10"):
                verifier.sanitize_icon(src, out)
            self.assertFalse(os.path.exists(out))

    def test_png_with_metadata_becomes_bare_vp8l(self):
        from PIL import Image, PngImagePlugin
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "icon.png")
            img = Image.new("RGBA", (10, 10), (255, 0, 128, 255))
            img.putpixel((2, 2), (0, 0, 0, 255))
            meta = PngImagePlugin.PngInfo()
            meta.add_text("Comment", "hidden payload " * 50)
            img.save(src, "PNG", pnginfo=meta)
            out = os.path.join(td, "clean.webp")
            size = verifier.sanitize_icon(src, out)
            self.assertLessEqual(size, 1024)
            detail = verifier.verify_icon(out)
            self.assertIn("10x10 lossless WebP", detail)
            with open(out, "rb") as fh:
                data = fh.read()
            self.assertNotIn(b"hidden payload", data)
            self.assertEqual([t for t, _ in verifier._webp_chunks(data)], [b"VP8L"])

    def test_committed_icons_pass_structural_check(self):
        slot_dir = core.ledger_w1_dir(REPO)
        for f in core.list_slot_files(slot_dir):
            with self.subTest(slot=f):
                verifier.verify_icon(os.path.join(slot_dir, f[:-5] + ".webp"))


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


class LastUpdatedTests(unittest.TestCase):
    """B-1: state.last_updated must move with settled update and transfer fees,
    and the verifier must catch it when it does not."""

    SLOTS = {
        "w1-b0001": {
            "timestamp_verified": "2026-10-07T11:40:00Z",
            "supersessions": [{"superseded_at": "2026-10-12T22:38:05Z"}],
            "update_settlements": [{"kind": "update", "at": "2026-10-12T22:48:28Z"}],
        },
        "w1-b0002": {
            "timestamp_verified": "2026-10-08T17:00:00Z",
            "update_settlements": [
                {"kind": "update", "at": "2026-10-12T22:59:48Z"},
                {"kind": "transfer", "at": "2026-10-20T19:17:10Z"},
            ],
        },
    }

    def _ledger(self, root, slots, last_updated=None):
        slot_dir = core.ledger_w1_dir(root)
        os.makedirs(slot_dir)
        for sid, body in slots.items():
            with open(os.path.join(slot_dir, sid + ".json"), "w", encoding="utf-8") as fh:
                json.dump(body, fh)
        if last_updated is not None:
            with open(os.path.join(root, "state.json"), "w", encoding="utf-8") as fh:
                json.dump({"last_updated": last_updated}, fh)
        return slot_dir

    def test_latest_timestamp_counts_update_and_transfer_fees(self):
        for kind in ("update", "transfer"):
            slots = copy.deepcopy(self.SLOTS)
            slots["w1-b0002"]["update_settlements"][1]["kind"] = kind
            with tempfile.TemporaryDirectory() as root:
                slot_dir = self._ledger(root, slots)
                got = core._latest_timestamp(slot_dir, core.list_slot_files(slot_dir))
                self.assertEqual(got, "2026-10-20T19:17:10Z", kind)

    def test_latest_timestamp_without_fees_unchanged(self):
        slots = {"w1-b0001": {"timestamp_verified": "2026-10-07T11:40:00Z",
                              "supersessions": [{"superseded_at": "2026-10-08T13:20:00Z"}]}}
        with tempfile.TemporaryDirectory() as root:
            slot_dir = self._ledger(root, slots)
            self.assertEqual(core._latest_timestamp(slot_dir, core.list_slot_files(slot_dir)),
                             "2026-10-08T13:20:00Z")

    def test_verifier_passes_on_current_value(self):
        with tempfile.TemporaryDirectory() as root:
            self._ledger(root, self.SLOTS, "2026-10-20T19:17:10Z")
            detail = verifier.verify_last_updated(root)
            self.assertIn("w1-b0002 update_settlements[1].at", detail)

    def test_verifier_fails_on_stale_value(self):
        # The exact B-1 state: last_updated stuck at the newest supersession.
        with tempfile.TemporaryDirectory() as root:
            self._ledger(root, self.SLOTS, "2026-10-12T22:38:05Z")
            with self.assertRaisesRegex(ValueError, r"w1-b0002 update_settlements\[1\]\.at"):
                verifier.verify_last_updated(root)

    def test_verifier_fails_on_future_value(self):
        with tempfile.TemporaryDirectory() as root:
            self._ledger(root, self.SLOTS, "2026-10-21T00:00:00Z")
            with self.assertRaisesRegex(ValueError, "later than every content timestamp"):
                verifier.verify_last_updated(root)

    def test_verifier_is_independent_of_generator(self):
        # A generator regression must not be able to bless itself.
        with tempfile.TemporaryDirectory() as root:
            self._ledger(root, self.SLOTS, "2026-10-12T22:38:05Z")
            orig = core._latest_timestamp
            core._latest_timestamp = lambda *_a, **_k: "2026-10-12T22:38:05Z"
            try:
                with self.assertRaises(ValueError):
                    verifier.verify_last_updated(root)
            finally:
                core._latest_timestamp = orig

    def test_committed_state_is_current(self):
        verifier.verify_last_updated(REPO)


class IdentityOnChainTests(unittest.TestCase):
    """verify_identity_on_chain re-checks the mutable ERC-8004 owner and tokenURI."""

    @staticmethod
    def rpc(**kw):
        def fake(method, params):
            if method != "eth_call":
                raise AssertionError(method)
            return identity_eth_call(params, **kw)
        return fake

    def test_correct_owner_and_uri_pass(self):
        detail = verifier.verify_identity_on_chain(rpc=self.rpc())
        self.assertIn("107162", detail)
        self.assertIn("0xf5f4a8c8", detail)

    def test_owner_comparison_is_case_insensitive(self):
        verifier.verify_identity_on_chain(rpc=self.rpc(owner=core.IDENTITY_HOLDER.upper().replace("0X", "0x")))

    def test_wrong_owner_fails(self):
        with self.assertRaisesRegex(ValueError, "Identity Violation:"):
            verifier.verify_identity_on_chain(rpc=self.rpc(owner="0x" + "11" * 20))

    def test_wrong_token_uri_fails(self):
        with self.assertRaisesRegex(ValueError, "Identity Violation:"):
            verifier.verify_identity_on_chain(rpc=self.rpc(uri="https://evil.example/registration.json"))


if __name__ == "__main__":
    unittest.main()
