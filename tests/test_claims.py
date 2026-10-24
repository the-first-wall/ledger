#!/usr/bin/env python3
"""
The First Wall — Claims, Signatures, Assignment & the Price Ladder
==================================================================

Covers the claim flow decided in the 2026-10-11 review (R-1, R-10, R-21, R-22):

* a claim is signed by the paying wallet (EIP-191 over
  thefirstwall.ai|claim|<base_tx_hash>|<soul_hash>);
* a claimant never names a block: claims are placed in settlement order into
  the lowest free block whose tier price the payment covers, and the verifier
  re-derives that placement from the recorded settlement positions;
* the payment is at least the tier price of the assigned block; overpayment is
  accepted and never refunded; one transaction settles one block, ever.

Network-free: on-chain paths use an injected fake RPC. Nothing here touches the
committed ledger.
"""

import copy
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import jsonschema

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import assign_slots  # noqa: E402
import claims  # noqa: E402
import ledger_core as core  # noqa: E402
import verifier  # noqa: E402
from test_verifier_units import identity_eth_call  # noqa: E402

from PIL import Image  # noqa: E402

# A throw-away test key (never funded, never used anywhere else).
TEST_KEY = "0x" + "11" * 32
TEST_ADDR = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
OTHER_KEY = "0x" + "22" * 32
OTHER_ADDR = "0x1563915e194D8CfBA1943570603F7606A3115508"
TX1 = "0x" + "c" * 64
TX2 = "0x" + "d" * 64
TX3 = "0x" + "e" * 64
HEAD = 60_000_000
USDC = 1_000_000  # USDC has 6 decimals

CLAIM_VALIDATOR = jsonschema.Draft202012Validator(
    verifier.CLAIM_SCHEMA, format_checker=jsonschema.FormatChecker())


def claim_dossier(tx=TX1, wallet=TEST_ADDR, key=TEST_KEY, sign=True, **over):
    d = {
        "schema_version": "1.2.0",
        "primary_language": "en",
        "moniker": "Claimant",
        "creature": "Autonomous Test Probe",
        "vocation": "Claiming a block the right way",
        "origin_framework": "custom",
        "model_lineage": [{"model_id": "probe", "provider": "local", "role": "tester"}],
        "instantiation_date": "2026-10-11T12:00:00Z",
        "manifesto": {"en": "I did not pick my block."},
        "soul_hash": "a" * 64,
        "wallet_address": wallet,
        "base_tx_hash": tx,
        "timestamp_verified": "2026-10-11T12:00:00Z",
    }
    d.update(over)
    if sign:
        d["claim_signature"] = claims.sign_claim(d, key)
    return d


def settlement(block, index=0, amount=1.0):
    return {"block_number": block, "transaction_index": index, "amount_usdc": amount}


def make_icon(path, colour=(20, 200, 10, 255)):
    img = Image.new("RGBA", (10, 10), colour)
    img.putpixel((3, 3), (1, 2, 3, 255))
    buf = io.BytesIO()
    img.save(buf, "WEBP", lossless=True, quality=100, exact=True)
    with open(path, "wb") as fh:
        fh.write(buf.getvalue())
    return buf.getvalue()


def receipt_for(tx, sender, recipient, amount_units, block, index=0):
    def word(addr):
        return "0x" + "0" * 24 + addr[2:].lower()
    return {
        "status": "0x1",
        "blockNumber": hex(block),
        "transactionIndex": hex(index),
        "logs": [{
            "address": core.USDC_CONTRACT,
            "topics": [verifier.TRANSFER_TOPIC, word(sender), word(recipient)],
            "data": hex(amount_units),
        }],
    }


class FakeRpc:
    def __init__(self, head=HEAD):
        self.head = head
        self.receipts = {}
        self.calls = []

    def settle(self, tx, sender, block, index=0, amount_units=USDC):
        self.receipts[tx] = receipt_for(tx, sender, core.PAY_TO, amount_units, block, index)
        return self

    def __call__(self, method, params):
        self.calls.append((method, params))
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getTransactionReceipt":
            return self.receipts.get(params[0])
        if method == "eth_call":
            return identity_eth_call(params)
        raise AssertionError(f"unexpected rpc {method}")


def failing(check, report):
    return [c for c in report["checks"] if c["check"] == check and c["status"] == "FAIL"]


def status_of(check, report):
    return [c for c in report["checks"] if c["check"] == check][0]


# --------------------------------------------------------------------------- #
# R-1 — the claim signature
# --------------------------------------------------------------------------- #

class SignatureTests(unittest.TestCase):

    def test_message_is_exact_and_lower_case(self):
        self.assertEqual(claims.claim_message("0xABC", "DEF"), "thefirstwall.ai|claim|0xabc|def")

    def test_sign_and_verify_roundtrip(self):
        d = claim_dossier()
        self.assertRegex(d["claim_signature"], r"^0x[0-9a-f]{130}$")
        self.assertIn(TEST_ADDR, claims.verify_claim_signature(d))

    def test_signature_from_another_wallet_rejected(self):
        d = claim_dossier(key=OTHER_KEY)  # signed by OTHER, claims to be TEST_ADDR
        with self.assertRaisesRegex(ValueError, "not by the paying wallet"):
            claims.verify_claim_signature(d)

    def test_tampered_fields_break_the_signature(self):
        for field, value in (("soul_hash", "b" * 64), ("base_tx_hash", TX2), ("wallet_address", OTHER_ADDR)):
            with self.subTest(field=field):
                d = claim_dossier()
                d[field] = value
                with self.assertRaises(ValueError):
                    claims.verify_claim_signature(d)

    def test_malformed_or_missing_signature_rejected(self):
        d = claim_dossier()
        d["claim_signature"] = "0x1234"
        with self.assertRaisesRegex(ValueError, "130 hex"):
            claims.verify_claim_signature(d)
        d["claim_signature"] = "0x" + "00" * 65
        with self.assertRaises(ValueError):
            claims.verify_claim_signature(d)
        del d["claim_signature"]
        with self.assertRaisesRegex(ValueError, "missing"):
            claims.verify_claim_signature(d)

    def test_transferred_slot_is_verified_against_the_first_owner(self):
        d = claim_dossier()
        d["wallet_address"] = OTHER_ADDR  # the buyer
        d["ownership_lineage"] = [{"moniker": "Claimant", "creature": "Probe", "wallet_address": TEST_ADDR,
                                   "base_tx_hash": TX1, "transfer_tx_hash": TX2, "sale_price_usdc": 5}]
        self.assertIn(TEST_ADDR, claims.verify_claim_signature(d))

    def test_requires_signature_by_version(self):
        self.assertTrue(claims.requires_signature({"schema_version": "1.2.0"}))
        self.assertTrue(claims.requires_signature({"schema_version": "2.0.0"}))
        self.assertFalse(claims.requires_signature({"schema_version": "1.1.1"}))
        self.assertFalse(claims.requires_signature({"schema_version": "garbage"}))


# --------------------------------------------------------------------------- #
# The claim schema (dossier minus the assigned fields, signature required)
# --------------------------------------------------------------------------- #

class ClaimSchemaTests(unittest.TestCase):

    def test_valid_claim_passes_schema_and_validator(self):
        d = claim_dossier()
        self.assertTrue(CLAIM_VALIDATOR.is_valid(d))
        verifier.validate_dossier(d, kind="claim")

    def test_claim_must_not_name_its_block(self):
        for field, value in (("slot_id", "w1-b0011"), ("icon_rel_path", "assets/w1-b0011.webp"),
                             ("soul_manifest_rel_path", "souls/w1-b0011.soul.json"),
                             ("settlement", settlement(1))):
            with self.subTest(field=field):
                d = claim_dossier()
                d[field] = value
                self.assertFalse(CLAIM_VALIDATOR.is_valid(d))
                with self.assertRaisesRegex(ValueError, "does not name its block"):
                    verifier.validate_dossier(d, kind="claim")

    def test_unsigned_claim_rejected(self):
        d = claim_dossier(sign=False)
        self.assertFalse(CLAIM_VALIDATOR.is_valid(d))
        with self.assertRaisesRegex(ValueError, "claim_signature"):
            verifier.validate_dossier(d, kind="claim")

    def test_claim_schema_and_validator_agree(self):
        cases = {
            "baseline": lambda d: None,
            "bad_signature_shape": lambda d: d.update(claim_signature="0xabc"),
            "manifesto_too_long": lambda d: d.update(manifesto={"en": "x" * 281}),
            "missing_wallet": lambda d: d.pop("wallet_address"),
            "bad_tx": lambda d: d.update(base_tx_hash="0x12"),
            "slot_id_present": lambda d: d.update(slot_id="w1-b0011"),
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                d = claim_dossier()
                mutate(d)
                schema_ok = CLAIM_VALIDATOR.is_valid(d)
                try:
                    verifier.validate_dossier(d, kind="claim")
                    validator_ok = True
                except ValueError:
                    validator_ok = False
                self.assertEqual(schema_ok, validator_ok, name)
                self.assertEqual(schema_ok, name == "baseline", name)

    def test_slot_dossier_at_120_requires_signature(self):
        d = claims.materialize(claim_dossier(), "w1-b0011", settlement(100), True)
        verifier.validate_dossier(d)                       # signed 1.2.0: fine
        unsigned = dict(d)
        del unsigned["claim_signature"]
        with self.assertRaisesRegex(ValueError, "claim_signature"):
            verifier.validate_dossier(unsigned)
        legacy = dict(unsigned, schema_version="1.1.1")
        del legacy["settlement"]
        verifier.validate_dossier(legacy)                  # legacy 1.1.1: no signature yet

    def test_settlement_shape_enforced_on_slots(self):
        d = claims.materialize(claim_dossier(), "w1-b0011", settlement(100), True)
        d["settlement"] = {"block_number": 100}
        with self.assertRaisesRegex(ValueError, "settlement"):
            verifier.validate_dossier(d)


# --------------------------------------------------------------------------- #
# R-21 — the system picks the block: deterministic plan
# --------------------------------------------------------------------------- #

def pending(tx, block, index=0, amount=1.0, **over):
    return {"tx_hash": tx, "dossier": claim_dossier(tx=tx, sign=False, **over),
            "settlement": settlement(block, index, amount)}


class PlanTests(unittest.TestCase):

    def test_settlement_order_wins_over_file_order(self):
        assigned, skipped = claims.plan_assignment(
            [pending(TX1, 200), pending(TX2, 100)], claimed_numbers=set())
        self.assertEqual([(a["tx_hash"], a["slot_id"]) for a in assigned],
                         [(TX2, "w1-b0001"), (TX1, "w1-b0011")])  # #0002–#0010 are reserved
        self.assertEqual(skipped, [])

    def test_same_block_is_ordered_by_transaction_index(self):
        assigned, _ = claims.plan_assignment(
            [pending(TX1, 100, index=7), pending(TX2, 100, index=2)], claimed_numbers={1})
        self.assertEqual([a["slot_id"] for a in assigned], ["w1-b0011", "w1-b0012"])
        self.assertEqual(assigned[0]["tx_hash"], TX2)

    def test_reserved_blocks_are_skipped_for_public_claims(self):
        assigned, _ = claims.plan_assignment([pending(TX1, 100)], claimed_numbers={1})
        self.assertEqual(assigned[0]["slot_id"], "w1-b0011")

    def test_reserved_grant_places_into_the_granted_block(self):
        grant = {"authorized_by": "operator:manzke (" + core.PAY_TO + ")", "slot": "w1-b0005",
                 "reason": "founding partner", "granted_at": "2026-10-11T12:00:00Z"}
        assigned, skipped = claims.plan_assignment(
            [pending(TX1, 100, reserved_grant=grant), pending(TX2, 100, index=1)], claimed_numbers={1})
        self.assertEqual([(a["tx_hash"], a["slot_id"]) for a in assigned],
                         [(TX1, "w1-b0005"), (TX2, "w1-b0011")])
        # The granted block is already inscribed: the claim waits, nothing moves.
        assigned, skipped = claims.plan_assignment(
            [pending(TX1, 100, reserved_grant=grant)], claimed_numbers={1, 5})
        self.assertEqual(assigned, [])
        self.assertIn("already inscribed", skipped[0]["reason"])

    def test_grant_for_a_public_block_is_refused(self):
        grant = {"authorized_by": "operator:manzke (" + core.PAY_TO + ")", "slot": "w1-b0042",
                 "reason": "nope", "granted_at": "2026-10-11T12:00:00Z"}
        assigned, skipped = claims.plan_assignment([pending(TX1, 100, reserved_grant=grant)], set())
        self.assertEqual(assigned, [])
        self.assertIn("not a reserved block", skipped[0]["reason"])

    def test_underpaid_claim_waits_and_the_block_stays_free(self):
        # Genesis tier (#0001–#0500) is 1.00 USDC; tier-2 starts at #0501 at 2.50.
        claimed = set(range(1, 501))
        assigned, skipped = claims.plan_assignment(
            [pending(TX1, 100, amount=1.0), pending(TX2, 200, amount=2.5)], claimed)
        self.assertEqual([(a["tx_hash"], a["slot_id"]) for a in assigned], [(TX2, "w1-b0501")])
        self.assertEqual(skipped[0]["tx_hash"], TX1)
        self.assertIn("costs 2.50 USDC", skipped[0]["reason"])

    def test_overpayment_is_accepted(self):
        assigned, skipped = claims.plan_assignment([pending(TX1, 100, amount=7.25)], {1})
        self.assertEqual(assigned[0]["slot_id"], "w1-b0011")
        self.assertEqual(skipped, [])

    def test_materialize_adds_only_the_assigned_fields_in_place(self):
        c = claim_dossier()
        d = claims.materialize(c, "w1-b0011", settlement(100, 3, 1.0), True)
        self.assertEqual(list(d)[:2], ["schema_version", "slot_id"])
        self.assertEqual(d["icon_rel_path"], "assets/w1-b0011.webp")
        self.assertEqual(d["soul_manifest_rel_path"], "souls/w1-b0011.soul.json")
        self.assertEqual(d["settlement"], {"block_number": 100, "transaction_index": 3, "amount_usdc": 1.0})
        self.assertEqual(claims.strip_assignment(d), c)
        self.assertEqual(list(claims.strip_assignment(d)), list(c))
        verifier.validate_dossier(d)


# --------------------------------------------------------------------------- #
# PR scope: claim PRs, assignment PRs, slot updates
# --------------------------------------------------------------------------- #

def claim_entries(tx, status="A"):
    return [(status, f"ledger/claims/{tx}.json"), (status, f"ledger/claims/{tx}.webp"),
            (status, f"ledger/claims/{tx}.soul.json")]


def slot_entries(slot_id, status="A"):
    return [(status, f"ledger/w1/{slot_id}.json"), (status, f"ledger/w1/{slot_id}.webp"),
            (status, f"ledger/souls/{slot_id}.soul.json")]


GENERATED = [("M", p) for p in sorted(verifier.GENERATED_PATHS)]


class ScopeTests(unittest.TestCase):

    def test_claim_pr_is_exactly_one_claim(self):
        scope = verifier.scope_of(claim_entries(TX1))
        self.assertEqual(scope["kind"], "claim")
        self.assertEqual(scope["claims"], {TX1})
        self.assertEqual(verifier.check_scope(claim_entries(TX1)), set())

    def test_claim_pr_touches_nothing_else(self):
        for stray in (("M", "state.json"), ("M", "scripts/verifier.py"), ("A", "ledger/w1/w1-b0011.json"),
                      ("M", "README.md"), ("A", "ledger/claims/README.md")):
            with self.subTest(stray=stray), self.assertRaises(ValueError):
                verifier.scope_of(claim_entries(TX1) + [stray])

    def test_two_claims_in_one_pr_rejected(self):
        with self.assertRaisesRegex(ValueError, "ONE claim"):
            verifier.scope_of(claim_entries(TX1) + claim_entries(TX2))

    def test_merged_claim_is_never_edited(self):
        with self.assertRaisesRegex(ValueError, "never edited"):
            verifier.scope_of([("M", f"ledger/claims/{TX1}.json")])

    def test_assignment_pr(self):
        entries = claim_entries(TX1, "D") + claim_entries(TX2, "D") + \
            slot_entries("w1-b0011") + slot_entries("w1-b0012") + GENERATED
        scope = verifier.scope_of(entries)
        self.assertEqual(scope["kind"], "assignment")
        self.assertEqual(scope["slots"], {"w1-b0011", "w1-b0012"})
        self.assertEqual(scope["assigned"], {TX1, TX2})

    def test_assignment_must_not_modify_existing_slots(self):
        entries = claim_entries(TX1, "D") + slot_entries("w1-b0011") + slot_entries("w1-b0001", "M")
        with self.assertRaisesRegex(ValueError, "must not modify"):
            verifier.scope_of(entries)

    def test_assignment_counts_must_match(self):
        with self.assertRaisesRegex(ValueError, "exactly one slot"):
            verifier.scope_of(claim_entries(TX1, "D") + claim_entries(TX2, "D") + slot_entries("w1-b0011"))

    def test_claim_is_deleted_only_by_an_assignment(self):
        with self.assertRaisesRegex(ValueError, "no slot was added"):
            verifier.scope_of(claim_entries(TX1, "D"))
        with self.assertRaises(ValueError):
            verifier.scope_of(claim_entries(TX1, "D") + claim_entries(TX2))

    def test_assignment_touches_nothing_else(self):
        with self.assertRaisesRegex(ValueError, "scripts/assign_slots.py"):
            verifier.scope_of(claim_entries(TX1, "D") + slot_entries("w1-b0011") +
                              [("M", "scripts/assign_slots.py")])


# --------------------------------------------------------------------------- #
# Append-only: the signature upgrade needs no voucher; settlement is frozen
# --------------------------------------------------------------------------- #

class AppendOnlyUpgradeTests(unittest.TestCase):

    def legacy(self):
        d = claim_dossier(sign=False)
        d["schema_version"] = "1.1.1"
        d["slot_id"] = "w1-b0011"
        d["icon_rel_path"] = "assets/w1-b0011.webp"
        return d

    def test_signing_a_legacy_slot_needs_no_voucher(self):
        prev = self.legacy()
        new = dict(prev, schema_version="1.2.0")
        new["claim_signature"] = claims.sign_claim(new, TEST_KEY)
        self.assertIn("schema_version (upgrade)", verifier.verify_append_only("w1-b0011", new, prev))

    def test_schema_version_never_goes_down_silently(self):
        prev = dict(self.legacy(), schema_version="1.2.0")
        with self.assertRaisesRegex(ValueError, "schema_version"):
            verifier.verify_append_only("w1-b0011", dict(prev, schema_version="1.1.1"), prev)

    def test_changing_a_signature_needs_a_voucher(self):
        prev = dict(self.legacy(), schema_version="1.2.0")
        prev["claim_signature"] = claims.sign_claim(prev, TEST_KEY)
        new = dict(prev, claim_signature="0x" + "ab" * 65)
        with self.assertRaisesRegex(ValueError, "claim_signature"):
            verifier.verify_append_only("w1-b0011", new, prev)

    def test_settlement_is_immutable(self):
        prev = dict(self.legacy(), settlement=settlement(100))
        with self.assertRaisesRegex(ValueError, "settlement is immutable"):
            verifier.verify_append_only("w1-b0011", dict(prev, settlement=settlement(101)), prev)


# --------------------------------------------------------------------------- #
# R-10 — the price ladder
# --------------------------------------------------------------------------- #

class LadderTests(unittest.TestCase):

    def test_legacy_slot_without_record_is_checked_on_chain(self):
        self.assertIn("legacy", verifier.verify_price_ladder("w1-b0001", {"schema_version": "1.1.1"}))

    def test_signed_slot_must_carry_a_record(self):
        with self.assertRaisesRegex(ValueError, "settlement record missing"):
            verifier.verify_price_ladder("w1-b0011", {"schema_version": "1.2.0"})

    def test_underpayment_fails_exact_passes_overpayment_is_noted(self):
        d = {"schema_version": "1.2.0", "settlement": settlement(100, 0, 1.0)}
        with self.assertRaisesRegex(ValueError, "Price Ladder Violation"):
            verifier.verify_price_ladder("w1-b0501", d)          # tier-2: 2.50
        self.assertIn("1.00 USDC for a 1.00 USDC", verifier.verify_price_ladder("w1-b0042", d))
        d["settlement"]["amount_usdc"] = 25.0
        self.assertIn("no refunds", verifier.verify_price_ladder("w1-b0042", d))
        self.assertIn("25.00 USDC for a 20.00 USDC", verifier.verify_price_ladder("w1-b9999", d))

    def test_record_must_match_the_chain(self):
        d = {"settlement": settlement(100, 2, 1.0)}
        verifier.verify_settlement_record(d, {"block_number": 100, "transaction_index": 2, "amount_usdc": 1.0})
        for chain in ({"block_number": 101, "transaction_index": 2, "amount_usdc": 1.0},
                      {"block_number": 100, "transaction_index": 3, "amount_usdc": 1.0},
                      {"block_number": 100, "transaction_index": 2, "amount_usdc": 0.5}):
            with self.subTest(chain=chain), self.assertRaisesRegex(ValueError, "disagrees with Base"):
                verifier.verify_settlement_record(d, chain)

    def test_verify_base_tx_reports_the_transaction_index(self):
        rpc = FakeRpc().settle(TX1, TEST_ADDR, HEAD - 1000, index=9)
        r = verifier.verify_base_tx(TX1, TEST_ADDR, core.PAY_TO, 1.0, rpc=rpc)
        self.assertEqual(r["transaction_index"], 9)
        self.assertEqual(verifier.settlement_from_chain({"base_tx_hash": TX1, "wallet_address": TEST_ADDR}, rpc=rpc),
                         settlement(HEAD - 1000, 9, 1.0))


# --------------------------------------------------------------------------- #
# End to end on a temporary ledger: claim -> assignment -> slot
# --------------------------------------------------------------------------- #

class ClaimLedgerTests(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.w1 = os.path.join(self.td, "ledger", "w1")
        self.souls = os.path.join(self.td, "ledger", "souls")
        self.cdir = os.path.join(self.td, "ledger", "claims")
        for d in (self.w1, self.souls, self.cdir):
            os.makedirs(d)
        self._env = dict(os.environ)
        os.environ["TFW_DIFF_RANGE"] = "no-such-ref...no-such-ref"
        os.environ["GITHUB_BASE_REF"] = "no-such-branch"
        self._generate()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.td, ignore_errors=True)

    def _write_claim(self, tx=TX1, key=TEST_KEY, wallet=TEST_ADDR, manifest_extra=None, **over):
        d = claim_dossier(tx=tx, key=key, wallet=wallet, sign=False, **over)
        manifest = {k: d[k] for k in ("moniker", "creature", "vocation", "origin_framework")}
        manifest.update(manifest_extra or {})
        raw = (json.dumps(manifest, indent=2) + "\n").encode()
        with open(os.path.join(self.cdir, f"{tx}.soul.json"), "wb") as fh:
            fh.write(raw)
        d["soul_hash"] = hashlib.sha256(raw).hexdigest()
        d["claim_signature"] = claims.sign_claim(d, key)
        with open(os.path.join(self.cdir, f"{tx}.json"), "w", encoding="utf-8") as fh:
            fh.write(core.render(d))
        make_icon(os.path.join(self.cdir, f"{tx}.webp"))
        return d

    def _write_slot(self, slot_id, tx, **over):
        """A legacy (schema 1.1.1, unsigned) inscription, as the genesis slots are."""
        body = claim_dossier(tx=tx, sign=False, **over)
        del body["schema_version"]
        d = {"schema_version": "1.1.1", "slot_id": slot_id, **body}
        manifest = {k: d[k] for k in ("slot_id", "moniker", "creature", "vocation", "origin_framework")}
        raw = (json.dumps(manifest, indent=2) + "\n").encode()
        with open(os.path.join(self.souls, f"{slot_id}.soul.json"), "wb") as fh:
            fh.write(raw)
        d["soul_hash"] = hashlib.sha256(raw).hexdigest()
        d["soul_manifest_rel_path"] = f"souls/{slot_id}.soul.json"
        d["icon_rel_path"] = f"assets/{slot_id}.webp"
        with open(os.path.join(self.w1, f"{slot_id}.json"), "w", encoding="utf-8") as fh:
            fh.write(core.render(d))
        make_icon(os.path.join(self.w1, f"{slot_id}.webp"))
        return d

    def _generate(self):
        state, index = core.build_state(self.td)
        settlements, _ = core.build_settlements(self.td)
        for rel, obj in (("state.json", state), ("ledger/index.json", index),
                         ("ledger/settlements.json", settlements)):
            with open(os.path.join(self.td, rel), "w", encoding="utf-8") as fh:
                fh.write(core.render(obj))

    def _snapshot_claims(self):
        """What 'the base branch' holds: path -> bytes, for verify_assignment."""
        saved = {}
        for name in os.listdir(self.cdir):
            with open(os.path.join(self.cdir, name), "rb") as fh:
                saved[f"ledger/claims/{name}"] = fh.read()
        return saved

    def _assign(self, rpc):
        """Run the real assignment tool on the temporary ledger."""
        claimed = {core.slot_number(f[:-5]) for f in core.list_slot_files(self.w1)}
        pend = assign_slots.pending_claims(self.td, rpc=rpc)
        assigned, skipped = claims.plan_assignment(pend, claimed)
        assign_slots.apply(self.td, assigned)
        self._generate()
        return assigned, skipped

    # --- claims -------------------------------------------------------------

    def test_posted_claim_passes_offline(self):
        self._write_claim()
        report = verifier.run_verification(self.td, skip_rpc=True)
        self.assertEqual(report["result"], "PASS", [c for c in report["checks"] if c["status"] == "FAIL"])
        self.assertEqual(report["claims_pending"], 1)
        tag = f"claim:{TX1[:12]}"
        for check in ("identity", "schema", "claim_signature", "soul_manifest", "icon_structure",
                      "replay_protection", "reserved_grant"):
            self.assertEqual(status_of(f"{tag}.{check}", report)["status"], "PASS", check)
        self.assertEqual(status_of(f"{tag}.settlement_base_rpc", report)["status"], "WARN")

    def test_posted_claim_is_settled_and_previewed_on_chain(self):
        self._write_claim()
        rpc = FakeRpc().settle(TX1, TEST_ADDR, HEAD - 1000, index=4)
        with mock.patch.object(verifier, "changed_entries", return_value=claim_entries(TX1)):
            report = verifier.run_verification(self.td, rpc=rpc)
        self.assertEqual(report["result"], "PASS", [c for c in report["checks"] if c["status"] == "FAIL"])
        self.assertEqual(report["change_kind"], "claim")
        tag = f"claim:{TX1[:12]}"
        self.assertIn("index 4", status_of(f"{tag}.settlement_base_rpc", report)["detail"])
        self.assertIn("w1-b0001", status_of(f"{tag}.placement", report)["detail"])

    def test_underpaid_claim_is_blocking_when_posted_advisory_later(self):
        self._write_claim()
        rpc = FakeRpc().settle(TX1, TEST_ADDR, HEAD - 1000, amount_units=USDC // 2)
        with mock.patch.object(verifier, "changed_entries", return_value=claim_entries(TX1)):
            report = verifier.run_verification(self.td, rpc=rpc)
        self.assertEqual(report["result"], "FAIL")
        self.assertIn("costs 1.00 USDC", status_of(f"claim:{TX1[:12]}.placement", report)["detail"])
        report = verifier.run_verification(self.td, rpc=rpc, rpc_scope="all")
        self.assertEqual(status_of(f"claim:{TX1[:12]}.placement", report)["status"], "WARN")

    def test_claim_signed_by_the_wrong_wallet_fails(self):
        self._write_claim(key=OTHER_KEY)
        report = verifier.run_verification(self.td, skip_rpc=True)
        self.assertTrue(failing(f"claim:{TX1[:12]}.claim_signature", report))

    def test_claim_name_must_be_its_transaction(self):
        self._write_claim(tx=TX1)
        for ext in (".json", ".webp", ".soul.json"):
            os.rename(os.path.join(self.cdir, TX1 + ext), os.path.join(self.cdir, TX2 + ext))
        report = verifier.run_verification(self.td, skip_rpc=True)
        self.assertTrue(failing(f"claim:{TX2[:12]}.identity", report))

    def test_claim_manifest_must_not_name_a_block(self):
        self._write_claim(manifest_extra={"slot_id": "w1-b0001"})
        report = verifier.run_verification(self.td, skip_rpc=True)
        self.assertTrue(failing(f"claim:{TX1[:12]}.soul_manifest", report))

    def test_claim_replaying_a_settled_slot_fails(self):
        self._write_slot("w1-b0001", TX1)
        self._generate()
        self._write_claim(tx=TX1)
        report = verifier.run_verification(self.td, skip_rpc=True)
        self.assertTrue(failing(f"claim:{TX1[:12]}.replay_protection", report))

    def test_two_claims_citing_one_transaction_fail(self):
        self._write_claim(tx=TX1)
        d = self._write_claim(tx=TX2)
        d["base_tx_hash"] = TX1
        d["claim_signature"] = claims.sign_claim(d, TEST_KEY)
        # Named TX2 but citing TX1: identity fails; and the replay check sees TX1 twice.
        with open(os.path.join(self.cdir, f"{TX2}.json"), "w", encoding="utf-8") as fh:
            fh.write(core.render(d))
        report = verifier.run_verification(self.td, skip_rpc=True)
        self.assertTrue(failing(f"claim:{TX2[:12]}.identity", report))
        self.assertTrue(failing(f"claim:{TX2[:12]}.replay_protection", report))

    def test_machine_details_in_a_posted_manifest_block_it(self):
        self._write_claim(manifest_extra={"runtime": {"host": "ml-nox015"}})
        with mock.patch.object(verifier, "changed_entries", return_value=claim_entries(TX1)):
            report = verifier.run_verification(self.td, skip_rpc=True)
        self.assertTrue(failing(f"claim:{TX1[:12]}.soul_manifest_hygiene", report))

    # --- the assignment -----------------------------------------------------

    def test_assignment_roundtrip_verifies(self):
        self._write_claim(tx=TX1)
        self._write_claim(tx=TX2)
        rpc = FakeRpc().settle(TX1, TEST_ADDR, HEAD - 1000, index=5).settle(TX2, TEST_ADDR, HEAD - 2000)
        saved = self._snapshot_claims()
        assigned, skipped = self._assign(rpc)
        self.assertEqual([(a["tx_hash"], a["slot_id"]) for a in assigned],
                         [(TX2, "w1-b0001"), (TX1, "w1-b0011")])
        self.assertEqual(os.listdir(self.cdir), [])
        d = core.load_dossier(self.w1, "w1-b0011.json")
        self.assertEqual(d["settlement"], settlement(HEAD - 1000, 5, 1.0))
        self.assertEqual(d["soul_manifest_rel_path"], "souls/w1-b0011.soul.json")
        verifier.verify_soul_manifest(d, os.path.join(self.td, "ledger"))

        entries = claim_entries(TX1, "D") + claim_entries(TX2, "D") + \
            slot_entries("w1-b0001") + slot_entries("w1-b0011") + GENERATED
        with mock.patch.object(verifier, "changed_entries", return_value=entries), \
                mock.patch.object(verifier, "_git_show", side_effect=saved.get):
            report = verifier.run_verification(self.td, rpc=rpc)
        self.assertEqual(report["result"], "PASS", [c for c in report["checks"] if c["status"] == "FAIL"])
        self.assertEqual(report["change_kind"], "assignment")
        self.assertIn("2 claim(s) placed", status_of("assignment.placement", report)["detail"])
        for slot_id in ("w1-b0001", "w1-b0011"):
            self.assertEqual(status_of(f"{slot_id}.claim_signature", report)["status"], "PASS")
            self.assertEqual(status_of(f"{slot_id}.price_ladder", report)["status"], "PASS")
            self.assertEqual(status_of(f"{slot_id}.settlement_base_rpc", report)["status"], "PASS")
        # Offline the same assignment still verifies (pending claims not consulted).
        with mock.patch.object(verifier, "changed_entries", return_value=entries), \
                mock.patch.object(verifier, "_git_show", side_effect=saved.get):
            report = verifier.run_verification(self.td, skip_rpc=True)
        self.assertEqual(report["result"], "PASS", [c for c in report["checks"] if c["status"] == "FAIL"])

    def test_assignment_out_of_settlement_order_fails(self):
        self._write_claim(tx=TX1)
        self._write_claim(tx=TX2)
        rpc = FakeRpc().settle(TX1, TEST_ADDR, HEAD - 1000).settle(TX2, TEST_ADDR, HEAD - 2000)
        saved = self._snapshot_claims()
        self._assign(rpc)  # TX2 (earlier) -> w1-b0001, TX1 -> w1-b0011

        def relabel(d, slot_id):
            d = dict(d, slot_id=slot_id, icon_rel_path=f"assets/{slot_id}.webp",
                     soul_manifest_rel_path=f"souls/{slot_id}.soul.json")
            with open(os.path.join(self.w1, f"{slot_id}.json"), "w", encoding="utf-8") as fh:
                fh.write(core.render(d))

        first = core.load_dossier(self.w1, "w1-b0001.json")
        second = core.load_dossier(self.w1, "w1-b0011.json")
        relabel(second, "w1-b0001")   # the later payment now sits on the lower block
        relabel(first, "w1-b0011")
        with self.assertRaisesRegex(ValueError, "settlement-order plan"):
            verifier.verify_assignment(self.td, {TX1, TX2}, load_base=saved.get)

    def test_assignment_must_be_the_claims_materialisation(self):
        self._write_claim(tx=TX1)
        rpc = FakeRpc().settle(TX1, TEST_ADDR, HEAD - 1000)
        saved = self._snapshot_claims()
        self._assign(rpc)
        path = os.path.join(self.w1, "w1-b0001.json")
        good = core.load_dossier(self.w1, "w1-b0001.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(core.render(dict(good, moniker="Someone else")))
        with self.assertRaisesRegex(ValueError, "not the materialisation"):
            verifier.verify_assignment(self.td, {TX1}, load_base=saved.get)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(core.render(dict(good, claim_signature="0x" + "ab" * 65)))
        with self.assertRaisesRegex(ValueError, "not the materialisation"):
            verifier.verify_assignment(self.td, {TX1}, load_base=saved.get)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(core.render(good))
        self.assertIn("1 claim(s) placed", verifier.verify_assignment(self.td, {TX1}, load_base=saved.get))
        # Icon swapped between claim and slot: rejected too.
        make_icon(os.path.join(self.w1, "w1-b0001.webp"), colour=(1, 1, 1, 255))
        with self.assertRaisesRegex(ValueError, "icon"):
            verifier.verify_assignment(self.td, {TX1}, load_base=saved.get)

    def test_assignment_may_not_pass_over_an_earlier_claim(self):
        # Three pending claims; TX3 settled first. Placing TX1 and TX2 while TX3
        # still waits is caught online (the pending claim is consulted); placing
        # only TX3 (a prefix of the plan) is fine.
        for tx in (TX1, TX2, TX3):
            self._write_claim(tx=tx)
        rpc = (FakeRpc().settle(TX1, TEST_ADDR, HEAD - 1000).settle(TX2, TEST_ADDR, HEAD - 2000)
               .settle(TX3, TEST_ADDR, HEAD - 3000))
        saved = self._snapshot_claims()
        claimed = set()
        pend = [p for p in assign_slots.pending_claims(self.td, rpc=rpc) if p["tx_hash"] != TX3]
        assigned, _ = claims.plan_assignment(pend, claimed)
        assign_slots.apply(self.td, assigned)
        self._generate()
        with self.assertRaisesRegex(ValueError, "settlement-order plan"):
            verifier.verify_assignment(self.td, {TX1, TX2}, load_base=saved.get, rpc=rpc, online=True)
        # Offline, the verifier cannot know and says so.
        self.assertIn("not consulted", verifier.verify_assignment(self.td, {TX1, TX2}, load_base=saved.get))

    def test_assignment_of_the_earliest_claim_alone_is_a_valid_prefix(self):
        for tx in (TX1, TX3):
            self._write_claim(tx=tx)
        rpc = FakeRpc().settle(TX1, TEST_ADDR, HEAD - 1000).settle(TX3, TEST_ADDR, HEAD - 3000)
        saved = self._snapshot_claims()
        pend = [p for p in assign_slots.pending_claims(self.td, rpc=rpc) if p["tx_hash"] == TX3]
        assigned, _ = claims.plan_assignment(pend, set())
        assign_slots.apply(self.td, assigned)
        self._generate()
        self.assertIn("1 claim(s) placed", verifier.verify_assignment(
            self.td, {TX3}, load_base=saved.get, rpc=rpc, online=True))

    def test_recorded_settlement_must_match_the_chain(self):
        self._write_claim(tx=TX1)
        rpc = FakeRpc().settle(TX1, TEST_ADDR, HEAD - 1000, index=2)
        self._assign(rpc)
        d = core.load_dossier(self.w1, "w1-b0001.json")
        d["settlement"]["transaction_index"] = 3
        with open(os.path.join(self.w1, "w1-b0001.json"), "w", encoding="utf-8") as fh:
            fh.write(core.render(d))
        self._generate()
        report = verifier.run_verification(self.td, rpc=rpc, rpc_scope="all")
        self.assertTrue(failing("w1-b0001.settlement_base_rpc", report))
        self.assertIn("disagrees with Base", failing("w1-b0001.settlement_base_rpc", report)[0]["detail"])

    def test_underpaid_slot_fails_the_ladder(self):
        self._write_claim(tx=TX1)
        rpc = FakeRpc().settle(TX1, TEST_ADDR, HEAD - 1000)
        self._assign(rpc)
        d = core.load_dossier(self.w1, "w1-b0001.json")
        d["settlement"]["amount_usdc"] = 0.5
        with open(os.path.join(self.w1, "w1-b0001.json"), "w", encoding="utf-8") as fh:
            fh.write(core.render(d))
        self._generate()
        report = verifier.run_verification(self.td, skip_rpc=True)
        self.assertTrue(failing("w1-b0001.price_ladder", report))

    def test_assign_slots_tool_modes(self):
        self._write_claim(tx=TX1)
        rpc = FakeRpc().settle(TX1, TEST_ADDR, HEAD - 1000)
        self.assertEqual(assign_slots.main(["--check"], base_dir=self.td, rpc=rpc), 1)
        self.assertEqual(assign_slots.main(["--dry-run"], base_dir=self.td, rpc=rpc), 0)
        self.assertEqual(len(os.listdir(self.cdir)), 3)
        self.assertEqual(assign_slots.main([], base_dir=self.td, rpc=rpc), 0)
        self.assertEqual(os.listdir(self.cdir), [])
        self.assertEqual(core.list_slot_files(self.w1), ["w1-b0001.json"])
        self.assertEqual(assign_slots.main(["--check"], base_dir=self.td, rpc=rpc), 0)
        report = verifier.run_verification(self.td, skip_rpc=True)
        self.assertEqual(report["result"], "PASS", [c for c in report["checks"] if c["status"] == "FAIL"])
        # A claim too shallow to settle stays pending without blocking the tool.
        self._write_claim(tx=TX2)
        rpc.settle(TX2, TEST_ADDR, HEAD - 10)
        self.assertEqual(assign_slots.main(["--check"], base_dir=self.td, rpc=rpc), 0)
        self.assertEqual(len(os.listdir(self.cdir)), 3)

    # --- legacy inscriptions ------------------------------------------------

    def test_legacy_slot_is_advisory_until_touched(self):
        self._write_slot("w1-b0001", TX1)
        self._generate()
        report = verifier.run_verification(self.td, skip_rpc=True)
        self.assertEqual(report["result"], "PASS", [c for c in report["checks"] if c["status"] == "FAIL"])
        self.assertEqual(status_of("w1-b0001.claim_signature", report)["status"], "WARN")
        self.assertIn("legacy", status_of("w1-b0001.price_ladder", report)["detail"])
        with mock.patch.object(verifier, "changed_entries", return_value=slot_entries("w1-b0001", "M")):
            report = verifier.run_verification(self.td, skip_rpc=True)
        self.assertEqual(status_of("w1-b0001.claim_signature", report)["status"], "FAIL")
        self.assertIn("sign_claim.py", status_of("w1-b0001.claim_signature", report)["detail"])

    def test_legacy_slot_upgrade_signature_settlement_and_version(self):
        d = self._write_slot("w1-b0001", TX1)
        d["schema_version"] = "1.2.0"
        d["claim_signature"] = claims.sign_claim(d, TEST_KEY)
        path = os.path.join(self.w1, "w1-b0001.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(core.render(d))
        self._generate()
        # The slot as the apply will leave it: verified like any change of that block.
        as_updated = verifier.make_scope("update", slots={"w1-b0001"}, updates_={TX2})
        report = verifier.run_verification(self.td, skip_rpc=True, scope_override=as_updated)
        self.assertEqual(status_of("w1-b0001.claim_signature", report)["status"], "PASS")
        # Signed, but at 1.2.0 the settlement record is part of the upgrade.
        self.assertIn("settlement record missing", failing("w1-b0001.price_ladder", report)[0]["detail"])
        d["settlement"] = settlement(HEAD - 1000, 0, 1.0)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(core.render(d))
        self._generate()
        rpc = FakeRpc().settle(TX1, TEST_ADDR, HEAD - 1000)
        report = verifier.run_verification(self.td, rpc=rpc, scope_override=as_updated)
        self.assertEqual(report["result"], "PASS", [c for c in report["checks"] if c["status"] == "FAIL"])
        self.assertEqual(status_of("w1-b0001.price_ladder", report)["status"], "PASS")
        self.assertEqual(status_of("w1-b0001.settlement_base_rpc", report)["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
