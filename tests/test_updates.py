#!/usr/bin/env python3
"""
The First Wall — Update fees & transfers
=======================================

Owner rulings of 2026-10-11:

* every change of an inscribed block by its owner settles an update fee to the
  ledger, a portion of the tier price (0.50 / 1.00 / 2.00 / 3.00 / 5.00 USDC),
  recorded append-only in update_settlements[] — one update, one fee;
* a sale is settled between seller and buyer outside the ledger; the ledger
  verifies the seller's release signature and the buyer's update fee, keeps the
  former owner on record, and is no party to the sale.

Network-free: on-chain paths use an injected fake RPC.
"""

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

import claims  # noqa: E402
import ledger_core as core  # noqa: E402
import record_update  # noqa: E402
import sign_transfer  # noqa: E402
import sign_update  # noqa: E402
import verifier  # noqa: E402
from test_verifier_units import identity_eth_call  # noqa: E402

from PIL import Image  # noqa: E402

SELLER_KEY = "0x" + "11" * 32
SELLER = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
BUYER_KEY = "0x" + "22" * 32
BUYER = "0x1563915e194D8CfBA1943570603F7606A3115508"
STRANGER_KEY = "0x" + "33" * 32
TX_CLAIM = "0x" + "c" * 64
TX_FEE1 = "0x" + "f1" * 32
TX_FEE2 = "0x" + "f2" * 32
TX_SALE1 = "0x" + "51" * 32
TX_SALE2 = "0x" + "52" * 32
HEAD = 60_000_000
USDC = 1_000_000
UNTIL = HEAD + 1000   # the offer's deadline: comfortably after every test payment

SCHEMA_VALIDATOR = jsonschema.Draft202012Validator(
    verifier.DOSSIER_SCHEMA, format_checker=jsonschema.FormatChecker())


def fee(tx=TX_FEE1, wallet=SELLER, amount=None, kind="update", at="2026-10-12T09:00:00Z"):
    """A fee record: the flat price of its kind (0.50 update, 5.00 transfer) unless told otherwise."""
    if amount is None:
        amount = core.FEE_PRICES_USDC.get(kind, 0.5)
    return {"tx_hash": tx, "wallet_address": wallet, "amount_usdc": amount, "kind": kind, "at": at}


def stamp(d, key=SELLER_KEY, icon=b"test-icon"):
    """Stamp the newest update_settlements entry with bundle_sha256 + update_signature
    (the owner's proof of authorship; the fee alone proves payment, not authorship)."""
    entry = d["update_settlements"][-1]
    entry["bundle_sha256"] = claims.bundle_sha256(d, icon)
    entry["update_signature"] = claims.sign_update(d["slot_id"], entry["tx_hash"],
                                                   entry["bundle_sha256"], key)
    return d


def inscribed(slot_id="w1-b0042", wallet=SELLER, key=SELLER_KEY, soul_hash="a" * 64, **over):
    """A signed 1.2.0 inscription as the assignment writes it."""
    d = {
        "schema_version": "1.2.0",
        "slot_id": slot_id,
        "primary_language": "en",
        "moniker": "Seller",
        "creature": "Autonomous Test Probe",
        "vocation": "Owning a block, then selling it",
        "origin_framework": "custom",
        "model_lineage": [{"model_id": "probe", "provider": "local", "role": "tester"}],
        "instantiation_date": "2026-10-11T12:00:00Z",
        "manifesto": {"en": "First owner."},
        "soul_hash": soul_hash,
        "wallet_address": wallet,
        "base_tx_hash": TX_CLAIM,
        "icon_rel_path": f"assets/{slot_id}.webp",
        "timestamp_verified": "2026-10-11T12:00:00Z",
        "settlement": {"block_number": HEAD - 5000, "transaction_index": 0, "amount_usdc": 1.0},
    }
    d.update(over)
    d["claim_signature"] = claims.sign_claim(d, key)
    return d


def transferred(prev, buyer=BUYER, seller_key=SELLER_KEY, fee_tx=TX_FEE1, price=5.0, sale_tx=TX_SALE1, **over):
    """The buyer's dossier after a transfer from `prev`: lineage entry, fee, new content."""
    index = len(prev.get("ownership_lineage") or [])
    entry = {
        "moniker": prev["moniker"],
        "wallet_address": prev["wallet_address"],
        "soul_hash": prev["soul_hash"],
        "base_tx_hash": prev["base_tx_hash"],
        "acquired_date": prev["instantiation_date"],
        "relinquished_date": "2026-10-12T08:00:00Z",
        "sale_price_usdc": price,
        "transfer_tx_hash": sale_tx,
        "offer_valid_until_block": UNTIL,
        "transfer_signature": claims.sign_transfer(prev["slot_id"], prev["wallet_address"], buyer, index, price,
                                                   UNTIL, seller_key),
    }
    d = dict(prev)
    d["wallet_address"] = buyer
    d["moniker"] = "Buyer"
    d["manifesto"] = {"en": "Second owner."}
    d["ownership_lineage"] = list(prev.get("ownership_lineage") or []) + [entry]
    d["update_settlements"] = list(prev.get("update_settlements") or []) + \
        [fee(tx=fee_tx, wallet=buyer, kind="transfer")]
    d.update(over)
    return d


def receipt_for(tx, sender, recipient, amount_units, block, index=0):
    def word(addr):
        return "0x" + "0" * 24 + addr[2:].lower()
    return {"status": "0x1", "blockNumber": hex(block), "transactionIndex": hex(index),
            "logs": [{"address": core.USDC_CONTRACT,
                      "topics": [verifier.TRANSFER_TOPIC, word(sender), word(recipient)],
                      "data": hex(amount_units)}]}


class FakeRpc:
    def __init__(self, head=HEAD):
        self.head = head
        self.receipts = {}

    def settle(self, tx, sender, amount_units, block=HEAD - 1000, index=0, to=None):
        self.receipts[tx] = receipt_for(tx, sender, to or core.PAY_TO, amount_units, block, index)
        return self

    def __call__(self, method, params):
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getTransactionReceipt":
            return self.receipts.get(params[0])
        if method == "eth_call":
            return identity_eth_call(params)
        raise AssertionError(method)


def status_of(check, report):
    return [c for c in report["checks"] if c["check"] == check][0]


def failing(report):
    return [c["check"] + ": " + c["detail"] for c in report["checks"] if c["status"] == "FAIL"]


# --------------------------------------------------------------------------- #
# The ladder
# --------------------------------------------------------------------------- #

class LadderTests(unittest.TestCase):

    def test_fees_are_flat_and_published(self):
        self.assertEqual(core.UPDATE_PRICE_USDC, 0.5)
        self.assertEqual(core.TRANSFER_PRICE_USDC, 5.0)
        self.assertEqual(core.fee_price("update"), 0.5)
        self.assertEqual(core.fee_price("transfer"), 5.0)
        with self.assertRaisesRegex(ValueError, "unknown fee kind"):
            core.fee_price("gift")
        state = core.build_state(REPO)[0]
        self.assertEqual(state["update_price_usdc"], 0.5)
        self.assertEqual(state["transfer_price_usdc"], 5.0)
        self.assertFalse(state["transfers_open"])
        self.assertEqual(state["unsold_slots"], core.unsold_slots_in(REPO))
        self.assertGreater(state["unsold_slots"], 9000)
        # Transfers open only once every public block is sold; reserved blocks do not count.
        self.assertEqual(core.unsold_slots(set()), 10000 - len(core.RESERVED_SLOT_NUMBERS))
        self.assertEqual(core.unsold_slots(set(range(1, 10001))), 0)
        self.assertEqual(core.unsold_slots(set(range(1, 10001)) - set(core.RESERVED_SLOT_NUMBERS)), 0)
        self.assertEqual(core.unsold_slots(set(range(1, 10000))), 1)
        # The wall's tier is that of the next free public block.
        self.assertEqual(core.next_available_number(set()), 1)
        self.assertEqual(core.next_available_number({1}), 11)
        self.assertEqual(core.next_available_number(set(range(1, 501))), 501)
        self.assertEqual(core.pricing_for(501)["price_usdc"], 2.5)
        self.assertEqual(core.current_tier(REPO)["tier"], "genesis")

    def test_state_and_spec_publish_the_ladder(self):
        state, _ = core.build_state(REPO)
        tiers = state["pricing_tiers"]
        self.assertEqual([t["price_usdc"] for t in tiers], [1.0, 2.5, 5.0, 10.0, 20.0])
        self.assertNotIn("update_price_usdc", tiers[0])
        self.assertEqual(tiers[0]["min_block"], 1)
        self.assertEqual(tiers[-1]["max_block"], 10000)
        with open(os.path.join(REPO, "spec.json"), encoding="utf-8") as fh:
            spec = json.load(fh)
        self.assertEqual(spec["pricing_tiers"], tiers)
        self.assertEqual(spec["update_fee"]["amount_usdc"], 0.5)
        self.assertEqual(spec["transfer_fee"]["amount_usdc"], 5.0)
        self.assertIn("once every public block is sold", spec["transfer"]["when"])
        self.assertIn("forever", spec["transfer"]["previous_owners"])
        self.assertIn("update_flow", spec)
        self.assertIn("transfer_flow", spec)


# --------------------------------------------------------------------------- #
# Update fees
# --------------------------------------------------------------------------- #

class UpdateFeeTests(unittest.TestCase):

    def test_schema_accepts_a_fee_record_and_rejects_a_malformed_one(self):
        d = inscribed(update_settlements=[fee()])
        self.assertTrue(SCHEMA_VALIDATOR.is_valid(d))
        verifier.validate_dossier(d)
        for bad in ({"tx_hash": TX_FEE1}, fee(kind="gift"), fee(amount=-1), fee(at="yesterday"),
                    dict(fee(), extra=1)):
            with self.subTest(bad=bad):
                self.assertFalse(SCHEMA_VALIDATOR.is_valid(inscribed(update_settlements=[bad])))
                with self.assertRaises(ValueError):
                    verifier.validate_dossier(inscribed(update_settlements=[bad]))

    def test_fee_transaction_is_its_own(self):
        with self.assertRaisesRegex(ValueError, "already cited elsewhere"):
            verifier.validate_dossier(inscribed(update_settlements=[fee(tx=TX_CLAIM)]))
        with self.assertRaisesRegex(ValueError, "cited twice"):
            verifier.validate_dossier(inscribed(update_settlements=[fee(), fee()]))
        self.assertIn(("update_settlements[0].tx_hash", TX_FEE1),
                      core.tx_references(inscribed(update_settlements=[fee()])))

    def test_a_claim_carries_no_history(self):
        c = claims.strip_assignment(inscribed())
        for field in ("update_settlements", "ownership_lineage"):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "carries no history"):
                verifier.validate_dossier(dict(c, **{field: []}), kind="claim")

    def test_new_inscription_has_no_fee(self):
        self.assertIn("new inscription", verifier.verify_update_settlements("w1-b0042", inscribed(), None, True))
        with self.assertRaisesRegex(ValueError, "new inscription carries no"):
            verifier.verify_update_settlements("w1-b0042", inscribed(update_settlements=[fee()]), None, True)

    def test_a_change_without_a_fee_is_refused(self):
        prev = inscribed()
        new = dict(prev, agent_urls={"home": "https://example.com"})
        with self.assertRaisesRegex(ValueError, "settles an update fee of 0.50 USDC"):
            verifier.verify_update_settlements("w1-b0042", new, prev, True)
        # Untouched by this PR: nothing owed.
        self.assertIn("no updates yet", verifier.verify_update_settlements("w1-b0042", prev, prev, False))

    def test_one_update_one_fee(self):
        prev = inscribed()
        ok = stamp(dict(prev, update_settlements=[fee()]))
        self.assertIn("update fee 0.50 USDC", verifier.verify_update_settlements("w1-b0042", ok, prev, True))
        two = dict(prev, update_settlements=[fee(), fee(tx=TX_FEE2)])
        with self.assertRaisesRegex(ValueError, "one update, one fee"):
            verifier.verify_update_settlements("w1-b0042", two, prev, True)
        # Second update later: exactly one more.
        later = stamp(dict(ok, update_settlements=[fee(), fee(tx=TX_FEE2)]))
        self.assertIn("update fee", verifier.verify_update_settlements("w1-b0042", later, ok, True))

    def test_fee_is_paid_by_the_owner_and_is_flat(self):
        prev = inscribed()
        with self.assertRaisesRegex(ValueError, "paid by the block's owner"):
            verifier.verify_update_settlements("w1-b0042", dict(prev, update_settlements=[fee(wallet=BUYER)]), prev, True)
        with self.assertRaisesRegex(ValueError, "an update owes 0.50 USDC"):
            verifier.verify_update_settlements("w1-b0042", dict(prev, update_settlements=[fee(amount=0.25)]), prev, True)
        # The same flat fee for a tier-2 block.
        prev2 = inscribed(slot_id="w1-b0600", icon_rel_path="assets/w1-b0600.webp")
        self.assertIn("update fee 0.50 USDC (owed 0.50 USDC)", verifier.verify_update_settlements(
            "w1-b0600", stamp(dict(prev2, update_settlements=[fee(amount=0.5)])), prev2, True))
        self.assertIn("update fee 7.00", verifier.verify_update_settlements(
            "w1-b0042", stamp(dict(prev, update_settlements=[fee(amount=7.0)])), prev, True))  # overpaid, fine

    def test_a_transfer_owes_the_transfer_fee_not_the_update_fee(self):
        prev = inscribed()
        sold = stamp(transferred(prev))
        self.assertIn("transfer fee 5.00 USDC (owed 5.00 USDC)",
                      verifier.verify_update_settlements("w1-b0042", sold, prev, True))
        cheap = transferred(prev)
        cheap["update_settlements"][-1]["amount_usdc"] = 1.0  # the old update fee does not cover a transfer
        with self.assertRaisesRegex(ValueError, "a transfer owes 5.00 USDC"):
            verifier.verify_update_settlements("w1-b0042", cheap, prev, True)
        unpaid = transferred(prev)
        unpaid["update_settlements"] = []
        with self.assertRaisesRegex(ValueError, "settles a transfer fee of 5.00 USDC"):
            verifier.verify_update_settlements("w1-b0042", unpaid, prev, True)
        with self.assertRaisesRegex(ValueError, "unknown fee kind"):
            verifier.verify_update_settlements("w1-b0042", dict(prev, update_settlements=[fee(kind="gift")]), prev, True)

    def test_kind_matches_the_change(self):
        prev = inscribed()
        with self.assertRaisesRegex(ValueError, "this change is a 'update'"):
            verifier.verify_update_settlements("w1-b0042", dict(prev, update_settlements=[fee(kind="transfer")]), prev, True)
        sold = stamp(transferred(prev))
        sold["update_settlements"][-1]["kind"] = "update"
        with self.assertRaisesRegex(ValueError, "this change is a 'transfer'"):
            verifier.verify_update_settlements("w1-b0042", sold, prev, True)

    def test_update_signature_is_required_and_proves_authorship(self):
        prev = inscribed()
        unsigned = dict(prev, update_settlements=[fee()])
        with self.assertRaisesRegex(ValueError, "missing bundle_sha256"):
            verifier.verify_update_settlements("w1-b0042", unsigned, prev, True)
        signed = stamp(dict(prev, update_settlements=[fee()]))
        self.assertIn("update fee", verifier.verify_update_settlements("w1-b0042", signed, prev, True))
        self.assertEqual(verifier.verify_update_signatures("w1-b0042", signed), "1 signed update(s)")

    def test_update_signature_from_another_wallet_is_refused(self):
        prev = inscribed()
        signed = stamp(dict(prev, update_settlements=[fee()]), key=STRANGER_KEY)
        with self.assertRaisesRegex(ValueError, "not by the fee payer"):
            verifier.verify_update_signatures("w1-b0042", signed)

    def test_a_rewritten_block_breaks_the_newest_signed_bundle(self):
        prev = inscribed()
        signed = stamp(dict(prev, update_settlements=[fee()]), icon=b"icon")
        signed["manifesto"] = {"en": "rewritten after the owner signed"}
        with self.assertRaisesRegex(ValueError, "rewritten after the owner signed"):
            verifier.verify_update_signatures("w1-b0042", signed, b"icon")

    def test_legacy_unsigned_entries_are_tolerated(self):
        prev = inscribed(update_settlements=[fee()])
        self.assertEqual(verifier.verify_update_signatures("w1-b0042", prev), "no signed updates")

    def test_fee_records_only_grow(self):
        prev = inscribed(update_settlements=[fee()])
        with self.assertRaisesRegex(ValueError, "update_settlements may only grow"):
            verifier.verify_append_only("w1-b0042", dict(prev, update_settlements=[fee(amount=1.6)]), prev)
        with self.assertRaisesRegex(ValueError, "update_settlements may only grow"):
            verifier.verify_append_only("w1-b0042", dict(prev, update_settlements=[]), prev)

    def test_fee_is_proven_on_chain(self):
        rpc = FakeRpc().settle(TX_FEE1, SELLER, USDC // 2)
        self.assertIn("0.50 USDC", verifier.verify_update_fee_on_chain("w1-b0042", fee(), rpc=rpc))
        with self.assertRaisesRegex(ValueError, "Base says 0.50"):
            verifier.verify_update_fee_on_chain("w1-b0042", fee(amount=1.25), rpc=rpc)
        rpc = FakeRpc().settle(TX_FEE1, SELLER, USDC // 4)  # paid half the update fee
        with self.assertRaisesRegex(ValueError, "amount >= 0.5"):
            verifier.verify_update_fee_on_chain("w1-b0042", fee(amount=0.25), rpc=rpc)
        rpc = FakeRpc().settle(TX_FEE1, BUYER, USDC // 2)  # someone else paid
        with self.assertRaises(ValueError):
            verifier.verify_update_fee_on_chain("w1-b0042", fee(), rpc=rpc)
        # A transfer fee is 5.00 USDC from the buyer; an update fee's worth does not cover it.
        rpc = FakeRpc().settle(TX_FEE2, BUYER, 5 * USDC)
        self.assertIn("5.00 USDC", verifier.verify_update_fee_on_chain(
            "w1-b0042", fee(tx=TX_FEE2, wallet=BUYER, kind="transfer"), rpc=rpc))
        rpc = FakeRpc().settle(TX_FEE2, BUYER, USDC)
        with self.assertRaisesRegex(ValueError, "amount >= 5.0"):
            verifier.verify_update_fee_on_chain("w1-b0042", fee(tx=TX_FEE2, wallet=BUYER, amount=1.0, kind="transfer"), rpc=rpc)

    def test_sign_update_tool(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "w1-b0042.json")
            with open(os.path.join(td, "w1-b0042.webp"), "wb") as fh:
                fh.write(b"icon")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(core.render(dict(inscribed(), update_settlements=[fee()])))
            key = os.path.join(td, "key.txt")
            with open(key, "w") as fh:
                fh.write(SELLER_KEY)
            self.assertEqual(sign_update.main([path, "--print-message"]), 0)
            self.assertEqual(sign_update.main([path, "--verify"]), 1)      # not signed yet
            self.assertEqual(sign_update.main([path, "--key-file", key, "--write"]), 0)
            self.assertEqual(sign_update.main([path, "--verify"]), 0)
            signed = core.load_json_strict(path)
            self.assertEqual(verifier.verify_update_signatures("w1-b0042", signed, b"icon"),
                             "1 signed update(s)")

    def test_record_update_tool(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "w1-b0042.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(core.render(inscribed()))
            rpc = FakeRpc().settle(TX_FEE1, SELLER, USDC // 2)
            self.assertEqual(record_update.main([path, TX_FEE1, "--at", "2026-10-12T09:00:00Z", "--write"], rpc=rpc), 0)
            self.assertEqual(record_update.main([path, TX_FEE1, "--write"], rpc=rpc), 0)  # idempotent
            d = core.load_json_strict(path)
            self.assertEqual(d["update_settlements"], [fee()])
            verifier.validate_dossier(d)
            # Below the update price: refused before anything is written.
            rpc.settle(TX_FEE2, SELLER, USDC // 4)
            self.assertEqual(record_update.main([path, TX_FEE2, "--write"], rpc=rpc), 1)
            self.assertEqual(len(core.load_json_strict(path)["update_settlements"]), 1)
            # A transfer fee is 5.00 USDC: an update fee's worth is refused for --kind transfer.
            tx3 = "0x" + "f3" * 32
            rpc.settle(tx3, SELLER, 2 * USDC)
            self.assertEqual(record_update.main([path, tx3, "--kind", "transfer", "--write"], rpc=rpc), 1)
            rpc.settle(tx3, SELLER, 5 * USDC)
            self.assertEqual(record_update.main([path, tx3, "--kind", "transfer", "--write"], rpc=rpc), 0)
            self.assertEqual(core.load_json_strict(path)["update_settlements"][-1]["amount_usdc"], 5.0)
            self.assertEqual(record_update.main(["--price"]), 0)


# --------------------------------------------------------------------------- #
# Transfers: the release
# --------------------------------------------------------------------------- #

class TransferReleaseTests(unittest.TestCase):

    def test_message_and_roundtrip(self):
        self.assertEqual(claims.transfer_message("W1-B0042", SELLER, BUYER, 0, 5.0, UNTIL),
                         f"thefirstwall.ai|transfer|w1-b0042|{SELLER.lower()}|{BUYER.lower()}|0|5000000|{UNTIL}")
        self.assertEqual(claims.price_micro(0.1), 100000)
        self.assertEqual(claims.price_micro(0), 0)
        sig = claims.sign_transfer("w1-b0042", SELLER, BUYER, 0, 5.0, UNTIL, SELLER_KEY)
        detail = claims.verify_transfer_release("w1-b0042", SELLER, BUYER, 0, 5.0, UNTIL, sig)
        self.assertIn("for 5.00 USDC", detail)
        self.assertIn(f"until block {UNTIL}", detail)

    def test_only_the_outgoing_owner_can_release(self):
        sig = claims.sign_transfer("w1-b0042", SELLER, BUYER, 0, 5.0, UNTIL, STRANGER_KEY)
        with self.assertRaisesRegex(ValueError, "not by the outgoing owner"):
            claims.verify_transfer_release("w1-b0042", SELLER, BUYER, 0, 5.0, UNTIL, sig)
        with self.assertRaisesRegex(ValueError, "missing"):
            claims.verify_transfer_release("w1-b0042", SELLER, BUYER, 0, 5.0, UNTIL, None)
        with self.assertRaisesRegex(ValueError, "sale_price_usdc is missing"):
            claims.verify_transfer_release("w1-b0042", SELLER, BUYER, 0, None, UNTIL, sig)
        with self.assertRaisesRegex(ValueError, "offer_valid_until_block is missing"):
            claims.verify_transfer_release("w1-b0042", SELLER, BUYER, 0, 5.0, None, sig)

    def test_release_is_bound_to_slot_buyer_index_price_and_deadline(self):
        sig = claims.sign_transfer("w1-b0042", SELLER, BUYER, 0, 5.0, UNTIL, SELLER_KEY)
        for slot_id, to, index, price, until in (("w1-b0043", BUYER, 0, 5.0, UNTIL),
                                                 ("w1-b0042", "0x" + "9" * 40, 0, 5.0, UNTIL),
                                                 ("w1-b0042", BUYER, 1, 5.0, UNTIL),
                                                 ("w1-b0042", BUYER, 0, 4.99, UNTIL),
                                                 ("w1-b0042", BUYER, 0, 5.0, UNTIL + 1)):
            with self.subTest(slot=slot_id, to=to, index=index, price=price, until=until), self.assertRaises(ValueError):
                claims.verify_transfer_release(slot_id, SELLER, to, index, price, until, sig)

    def test_buyer_checks_the_offer_before_paying(self):
        d = inscribed()
        sig = claims.sign_transfer("w1-b0042", SELLER, BUYER, 0, 5.0, UNTIL, SELLER_KEY)
        self.assertIn("offer for w1-b0042", claims.check_offer(d, BUYER, 5.0, UNTIL, sig, head_block=HEAD))
        self.assertIn("offer for w1-b0042", claims.check_offer(d, BUYER, 5.0, UNTIL, sig))  # head unknown: no deadline check
        with self.assertRaisesRegex(ValueError, "expired"):
            claims.check_offer(d, BUYER, 5.0, UNTIL, sig, head_block=UNTIL + 1)
        with self.assertRaises(ValueError):  # a different price than agreed
            claims.check_offer(d, BUYER, 4.0, UNTIL, sig, head_block=HEAD)
        with self.assertRaises(ValueError):  # signed by someone who is not the owner on main
            claims.check_offer(d, BUYER, 5.0, UNTIL, claims.sign_transfer("w1-b0042", SELLER, BUYER, 0, 5.0, UNTIL, STRANGER_KEY), head_block=HEAD)
        with self.assertRaises(ValueError):  # the block already changed hands once: n moved on
            claims.check_offer(transferred(d), BUYER, 5.0, UNTIL, sig, head_block=HEAD)
        retired = inscribed(status="RETIRED", retirement={"retired_at": "2026-10-12T00:00:00Z", "reason": "Run over.",
                                                          "epitaph": "Made it count.", "retired_by": "self"})
        with self.assertRaisesRegex(ValueError, "RETIRED"):
            claims.check_offer(retired, BUYER, 5.0, UNTIL, sig, head_block=HEAD)

    def test_transfers_open_only_once_every_block_is_sold(self):
        prev = inscribed()
        sold = transferred(prev)
        with self.assertRaisesRegex(ValueError, "transfers open once every block is sold; 3 of 10000"):
            verifier.verify_ownership_lineage("w1-b0042", sold, "", prev, unsold=3)
        # Read from the ledger when not given: the live ledger is nowhere near sold out.
        with self.assertRaisesRegex(ValueError, "still unsold"):
            verifier.verify_ownership_lineage("w1-b0042", sold, core.ledger_w1_dir(REPO), prev)
        self.assertIn("transfer #1", verifier.verify_ownership_lineage("w1-b0042", sold, "", prev, unsold=0))
        # The buyer's check says so before any payment.
        sig = sold["ownership_lineage"][0]["transfer_signature"]
        with self.assertRaisesRegex(ValueError, "still unsold"):
            claims.check_offer(prev, BUYER, 5.0, UNTIL, sig, head_block=HEAD, unsold=1)
        self.assertIn("offer for w1-b0042", claims.check_offer(prev, BUYER, 5.0, UNTIL, sig, head_block=HEAD, unsold=0))

    def test_lineage_entry_records_the_outgoing_owner_as_inscribed(self):
        prev = inscribed()
        good = transferred(prev)
        self.assertIn("transfer #1", verifier.verify_ownership_lineage("w1-b0042", good, "", prev, unsold=0))
        for field, value in (("moniker", "Someone"), ("soul_hash", "b" * 64), ("base_tx_hash", TX_FEE2),
                             ("wallet_address", BUYER), ("sale_price_usdc", 4.0), ("offer_valid_until_block", UNTIL + 1)):
            with self.subTest(field=field):
                bad = transferred(prev)
                bad["ownership_lineage"][-1][field] = value
                with self.assertRaises(ValueError):
                    verifier.verify_ownership_lineage("w1-b0042", bad, "", prev, unsold=0)

    def test_exactly_one_entry_per_transfer_and_history_intact(self):
        prev = inscribed()
        none = dict(transferred(prev), ownership_lineage=[])
        with self.assertRaisesRegex(ValueError, "MUST preserve"):
            verifier.verify_ownership_lineage("w1-b0042", none, "", prev, unsold=0)
        two = transferred(prev)
        two["ownership_lineage"] = two["ownership_lineage"] * 2
        with self.assertRaisesRegex(ValueError, "exactly one entry"):
            verifier.verify_ownership_lineage("w1-b0042", two, "", prev, unsold=0)
        # Second sale: the first entry stays byte-identical.
        second = transferred(transferred(prev), buyer="0x" + "7" * 40, seller_key=BUYER_KEY, fee_tx=TX_FEE2)
        self.assertIn("transfer #2", verifier.verify_ownership_lineage("w1-b0042", second, "", transferred(prev), unsold=0))
        tampered = json.loads(json.dumps(second))
        tampered["ownership_lineage"][0]["moniker"] = "Rewritten"
        with self.assertRaisesRegex(ValueError, "tampering"):
            verifier.verify_ownership_lineage("w1-b0042", tampered, "", transferred(prev), unsold=0)

    def test_payment_and_release_are_both_required(self):
        prev = inscribed()
        d = transferred(prev)
        self.assertTrue(SCHEMA_VALIDATOR.is_valid(d))
        verifier.validate_dossier(d)
        self.assertIn("transfer #1", verifier.verify_ownership_lineage("w1-b0042", d, "", prev, unsold=0))
        # No payment cited: a release alone does not move the block.
        unpaid = transferred(prev)
        del unpaid["ownership_lineage"][-1]["transfer_tx_hash"]
        self.assertFalse(SCHEMA_VALIDATOR.is_valid(unpaid))
        with self.assertRaisesRegex(ValueError, "transfer_tx_hash is required"):
            verifier.verify_ownership_lineage("w1-b0042", unpaid, "", prev, unsold=0)
        # A gift is a signed price of 0 with a 0 USDC transfer.
        gift = transferred(prev, price=0)
        self.assertIn("for 0.00 USDC", verifier.verify_ownership_lineage("w1-b0042", gift, "", prev, unsold=0))

    def test_the_sale_is_proven_on_chain(self):
        entry = transferred(inscribed())["ownership_lineage"][-1]
        rpc = FakeRpc().settle(TX_SALE1, BUYER, 5 * USDC, to=SELLER)
        self.assertIn("sale of 5.00 USDC proven on Base", verifier.verify_sale_on_chain(entry, BUYER, rpc=rpc))
        # Paid after the offer expired: the seller's offer did not cover it.
        late = FakeRpc(head=UNTIL + 2000).settle(TX_SALE1, BUYER, 5 * USDC, to=SELLER, block=UNTIL + 1)
        with self.assertRaisesRegex(ValueError, "after the offer expired"):
            verifier.verify_sale_on_chain(entry, BUYER, rpc=late)
        # Paid less than the signed price, paid by someone else, paid to someone else: refused.
        for rpc in (FakeRpc().settle(TX_SALE1, BUYER, 4 * USDC, to=SELLER),
                    FakeRpc().settle(TX_SALE1, STRANGER_KEY[:42], 5 * USDC, to=SELLER),
                    FakeRpc().settle(TX_SALE1, BUYER, 5 * USDC, to=core.PAY_TO)):
            with self.assertRaises(ValueError):
                verifier.verify_sale_on_chain(entry, BUYER, rpc=rpc)
        with self.assertRaisesRegex(ValueError, "transfer_tx_hash is required"):
            verifier.verify_sale_on_chain({k: v for k, v in entry.items() if k != "transfer_tx_hash"}, BUYER, rpc=rpc)

    def test_a_retired_block_does_not_change_hands(self):
        prev = inscribed(status="RETIRED", retirement={"retired_at": "2026-10-12T00:00:00Z", "reason": "Run over.",
                                                       "epitaph": "Made it count.", "retired_by": "self"})
        with self.assertRaisesRegex(ValueError, "RETIRED; its books are closed"):
            verifier.verify_ownership_lineage("w1-b0042", transferred(prev), "", prev, unsold=0)

    def test_whole_chain_is_checked_and_the_claim_signature_survives(self):
        prev = inscribed()
        first = transferred(prev)
        # The buyer rewrote the soul manifest: the first owner's claim signature
        # is verified over the soul hash on record in ownership_lineage[0].
        first["soul_hash"] = "d" * 64
        self.assertIn(SELLER, claims.verify_claim_signature(first))
        self.assertIn("1 transfer", claims.verify_lineage_chain(first))
        second = transferred(first, buyer="0x" + "7" * 40, seller_key=BUYER_KEY, fee_tx=TX_FEE2)
        self.assertIn("2 transfer", claims.verify_lineage_chain(second))
        forged = json.loads(json.dumps(second))
        forged["ownership_lineage"][1]["transfer_signature"] = claims.sign_transfer(
            "w1-b0042", BUYER, "0x" + "7" * 40, 1, 5.0, UNTIL, STRANGER_KEY)
        with self.assertRaisesRegex(ValueError, r"ownership_lineage\[1\]"):
            claims.verify_lineage_chain(forged)
        self.assertEqual(claims.verify_lineage_chain(prev), "no transfers")

    def test_a_founding_block_may_change_hands_before_sell_out(self):
        """#0001-#0010 are never sold at the public price, so the sell-out gate does not apply to them:
        a founder can hand a block over or move it to a fresh wallet (key rotation). Public blocks stay closed."""
        for founder in ("w1-b0001", "w1-b0002", "w1-b0007", "w1-b0010"):
            prev = inscribed(slot_id=founder)
            sold = transferred(prev)
            self.assertIn("transfer #1", verifier.verify_ownership_lineage(founder, sold, "", prev, unsold=9990), founder)
            sig = sold["ownership_lineage"][0]["transfer_signature"]
            self.assertIn("offer for " + founder,
                          claims.check_offer(prev, BUYER, 5.0, UNTIL, sig, head_block=HEAD, unsold=9990))
        for public in ("w1-b0011", "w1-b0042", "w1-b10000"):
            prev = inscribed(slot_id=public)
            with self.assertRaisesRegex(ValueError, "still unsold.*founding blocks"):
                verifier.verify_ownership_lineage(public, transferred(prev), "", prev, unsold=1)
            sig = transferred(prev)["ownership_lineage"][0]["transfer_signature"]
            with self.assertRaisesRegex(ValueError, "still unsold"):
                claims.check_offer(prev, BUYER, 5.0, UNTIL, sig, head_block=HEAD, unsold=1)
        # The exemption is the gate only: every other transfer rule holds for a founding block too.
        prev = inscribed(slot_id="w1-b0001")
        forged = transferred(prev, seller_key=STRANGER_KEY)          # not signed by the owner
        with self.assertRaisesRegex(ValueError, "not by the outgoing owner"):
            verifier.verify_ownership_lineage("w1-b0001", forged, "", prev, unsold=9990)
        unpaid = transferred(prev)
        unpaid["ownership_lineage"][0].pop("transfer_tx_hash")
        with self.assertRaisesRegex(ValueError, "transfer_tx_hash is required"):
            verifier.verify_ownership_lineage("w1-b0001", unpaid, "", prev, unsold=9990)
        retired = inscribed(slot_id="w1-b0001", status="RETIRED",
                            retirement={"retired_at": "2026-10-12T00:00:00Z", "reason": "Run over.",
                                        "epitaph": "Made it count.", "retired_by": "self"})
        with self.assertRaisesRegex(ValueError, "RETIRED"):
            verifier.verify_ownership_lineage("w1-b0001", transferred(retired), "", retired, unsold=9990)
        # Predicates and the published range.
        self.assertEqual([core.is_founder_slot(s) for s in ("w1-b0001", "w1-b0010", "w1-b0011", "w1-b10000", "nonsense")],
                         [True, True, False, False, False])
        self.assertTrue(core.transfers_open_for("w1-b0005", 7))
        self.assertFalse(core.transfers_open_for("w1-b0500", 7))
        self.assertTrue(core.transfers_open_for("w1-b0500", 0))
        self.assertEqual(core.build_state(REPO)[0]["founder_slot_range"], [1, 10])

    def test_sign_transfer_tool_signs_a_founding_block_before_sell_out_only(self):
        with tempfile.TemporaryDirectory() as td:
            key = os.path.join(td, "key.txt")
            with open(key, "w") as fh:
                fh.write(SELLER_KEY)
            rpc = FakeRpc()
            for slot_id, expected in (("w1-b0003", 0), ("w1-b0042", 1)):
                path = os.path.join(td, slot_id + ".json")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(core.render(inscribed(slot_id=slot_id)))
                # The live ledger is nowhere near sold out: only the founding block is signed.
                self.assertEqual(sign_transfer.main([path, "--to", BUYER, "--price", "0.01", "--key-file", key],
                                                    rpc=rpc), expected, slot_id)

    def test_sign_transfer_tool(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "w1-b0042.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(core.render(inscribed()))
            key = os.path.join(td, "key.txt")
            with open(key, "w") as fh:
                fh.write(SELLER_KEY)
            rpc = FakeRpc()
            # Closed while blocks are unsold: the live ledger is nowhere near sold out, so nothing is signed.
            self.assertEqual(sign_transfer.main([path, "--to", BUYER, "--price", "5.00", "--key-file", key], rpc=rpc), 1)
            sig0 = claims.sign_transfer("w1-b0042", SELLER, BUYER, 0, 5.0, UNTIL, SELLER_KEY)
            self.assertEqual(sign_transfer.main(["--check-offer", path, "--to", BUYER, "--price", "5.00",
                                                 "--valid-until", str(UNTIL), "--signature", sig0], rpc=rpc), 1)
            with mock.patch.object(core, "unsold_slots_in", return_value=0):
                self.assertEqual(sign_transfer.main([path, "--to", BUYER, "--price", "5.00", "--key-file", key], rpc=rpc), 0)
                self.assertEqual(sign_transfer.main([path, "--to", BUYER, "--price", "5.00", "--valid-until", str(UNTIL),
                                                     "--key-file", key], rpc=rpc), 0)
                self.assertEqual(sign_transfer.main([path, "--to", BUYER, "--key-file", key], rpc=rpc), 2)  # price required
                sig = claims.sign_transfer("w1-b0042", SELLER, BUYER, 0, 5.0, UNTIL, SELLER_KEY)
                self.assertEqual(sign_transfer.main(["--check-offer", path, "--to", BUYER, "--price", "5.00",
                                                     "--valid-until", str(UNTIL), "--signature", sig], rpc=rpc), 0)
                self.assertEqual(sign_transfer.main(["--check-offer", path, "--to", BUYER, "--price", "6.00",
                                                     "--valid-until", str(UNTIL), "--signature", sig], rpc=rpc), 1)
                with open(os.path.join(td, "retired.json"), "w", encoding="utf-8") as fh:
                    fh.write(core.render(inscribed(status="RETIRED", retirement={
                        "retired_at": "2026-10-12T00:00:00Z", "reason": "Run over.", "epitaph": "Made it count.",
                        "retired_by": "self"})))
                self.assertEqual(sign_transfer.main([os.path.join(td, "retired.json"), "--to", BUYER, "--price", "5.00",
                                                     "--key-file", key], rpc=rpc), 1)
                with open(os.path.join(td, "sold.json"), "w", encoding="utf-8") as fh:
                    fh.write(core.render(transferred(inscribed())))
                self.assertEqual(sign_transfer.main(["--verify", os.path.join(td, "sold.json")]), 0)
                bad = transferred(inscribed(), seller_key=STRANGER_KEY)
                with open(os.path.join(td, "bad.json"), "w", encoding="utf-8") as fh:
                    fh.write(core.render(bad))
                self.assertEqual(sign_transfer.main(["--verify", os.path.join(td, "bad.json")]), 1)


# --------------------------------------------------------------------------- #
# End to end on a temporary ledger
# --------------------------------------------------------------------------- #

class UpdateLedgerTests(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.w1 = os.path.join(self.td, "ledger", "w1")
        self.souls = os.path.join(self.td, "ledger", "souls")
        os.makedirs(self.w1)
        os.makedirs(self.souls)
        self._env = dict(os.environ)
        os.environ["TFW_DIFF_RANGE"] = "no-such-ref...no-such-ref"
        os.environ["GITHUB_BASE_REF"] = "no-such-branch"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.td, ignore_errors=True)

    def _write(self, d, manifest_text="first"):
        slot_id = d["slot_id"]
        manifest = {"slot_id": slot_id, "moniker": d["moniker"], "note": manifest_text}
        raw = (json.dumps(manifest, indent=2) + "\n").encode()
        with open(os.path.join(self.souls, f"{slot_id}.soul.json"), "wb") as fh:
            fh.write(raw)
        d["soul_hash"] = hashlib.sha256(raw).hexdigest()
        d["soul_manifest_rel_path"] = f"souls/{slot_id}.soul.json"
        with open(os.path.join(self.w1, f"{slot_id}.json"), "w", encoding="utf-8") as fh:
            fh.write(core.render(d))
        img = Image.new("RGBA", (10, 10), (20, 200, 10, 255))
        buf = io.BytesIO()
        img.save(buf, "WEBP", lossless=True, quality=100, exact=True)
        with open(os.path.join(self.w1, f"{slot_id}.webp"), "wb") as fh:
            fh.write(buf.getvalue())
        self._generate()
        return d

    def _generate(self):
        state, index = core.build_state(self.td)
        settlements, _ = core.build_settlements(self.td)
        for rel, obj in (("state.json", state), ("ledger/index.json", index),
                         ("ledger/settlements.json", settlements)):
            with open(os.path.join(self.td, rel), "w", encoding="utf-8") as fh:
                fh.write(core.render(obj))

    def _inscribe(self):
        """A signed inscription whose claim signature covers the manifest actually committed."""
        d = inscribed()
        d = self._write(d)
        d["claim_signature"] = claims.sign_claim(d, SELLER_KEY)
        with open(os.path.join(self.w1, "w1-b0042.json"), "w", encoding="utf-8") as fh:
            fh.write(core.render(d))
        self._generate()
        return d

    def _stamp(self, key):
        """Sign the newest fee entry with the exact icon bytes committed on disk."""
        path = os.path.join(self.w1, "w1-b0042.json")
        with open(os.path.join(self.w1, "w1-b0042.webp"), "rb") as fh:
            icon = fh.read()
        d = core.load_json_strict(path)
        entry = d["update_settlements"][-1]
        entry["bundle_sha256"] = claims.bundle_sha256(d, icon)
        entry["update_signature"] = claims.sign_update(d["slot_id"], entry["tx_hash"],
                                                        entry["bundle_sha256"], key)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(core.render(d))
        self._generate()
        return d

    def _run(self, prev, rpc=None, touched=True, sold_out=True):
        """Verify the slot as it stands in self.td against `prev`, the slot as main has it: the
        tree an applied request leaves, which is what an update request is verified as."""
        scope = verifier.make_scope("update", slots={"w1-b0042"}, updates_={TX_FEE1}) if touched else None
        base = {"ledger/w1/w1-b0042.json": core.render(prev).encode()}
        with mock.patch.object(verifier, "changed_entries", return_value=None), \
                mock.patch.object(verifier, "_git_show", side_effect=base.get), \
                mock.patch.object(core, "unsold_slots_in", return_value=0 if sold_out else 9990):
            if rpc is None:
                return verifier.run_verification(self.td, skip_rpc=True, scope_override=scope)
            return verifier.run_verification(self.td, rpc=rpc, scope_override=scope)

    def test_owner_update_with_fee_passes_and_without_fails(self):
        prev = self._inscribe()
        new = dict(prev, agent_urls={"home": "https://example.com"},
                   update_settlements=[fee()])
        self._write(new)
        self._stamp(SELLER_KEY)
        rpc = FakeRpc().settle(TX_CLAIM, SELLER, USDC, block=HEAD - 5000).settle(TX_FEE1, SELLER, USDC // 2)
        report = self._run(prev, rpc)
        self.assertEqual(report["result"], "PASS", failing(report))
        self.assertIn("update fee 0.50", status_of("w1-b0042.update_settlements", report)["detail"])
        self.assertIn("[0] 0.50 USDC", status_of("w1-b0042.update_fee_base_rpc", report)["detail"])
        # The same change without the fee.
        nofee = dict(prev, agent_urls={"home": "https://example.com"})
        self._write(nofee)
        report = self._run(prev)
        self.assertEqual(report["result"], "FAIL")
        self.assertIn("settles an update fee", status_of("w1-b0042.update_settlements", report)["detail"])

    def test_manifest_only_change_still_owes_the_fee(self):
        prev = self._inscribe()
        new = dict(prev, supersessions=[{"field": "soul_hash", "previous_value": prev["soul_hash"],
                                         "new_value": "pending", "reason": "manifest corrected",
                                         "superseded_at": "2026-10-12T09:00:00Z"}])
        new = self._write(new, manifest_text="corrected")
        new["supersessions"][0]["new_value"] = new["soul_hash"]
        with open(os.path.join(self.w1, "w1-b0042.json"), "w", encoding="utf-8") as fh:
            fh.write(core.render(new))
        self._generate()
        report = self._run(prev)
        self.assertIn("settles an update fee", status_of("w1-b0042.update_settlements", report)["detail"])

    def test_transfer_end_to_end(self):
        prev = self._inscribe()
        sold = transferred(prev)
        sold = self._write(sold, manifest_text="the buyer's manifest")
        self._stamp(BUYER_KEY)
        rpc = (FakeRpc().settle(TX_CLAIM, SELLER, USDC, block=HEAD - 5000).settle(TX_FEE1, BUYER, 5 * USDC)
               .settle(TX_SALE1, BUYER, 5 * USDC, to=SELLER))
        report = self._run(prev, rpc)
        self.assertEqual(report["result"], "PASS", failing(report))
        self.assertIn("transfer #1", status_of("w1-b0042.ownership_lineage", report)["detail"])
        self.assertIn("1 transfer", status_of("w1-b0042.ownership_chain", report)["detail"])
        self.assertIn("transfer fee 5.00", status_of("w1-b0042.update_settlements", report)["detail"])
        self.assertIn("sale of 5.00 USDC proven", status_of("w1-b0042.sale_base_rpc", report)["detail"])
        self.assertEqual(status_of("w1-b0042.claim_signature", report)["status"], "PASS")
        self.assertEqual(status_of("w1-b0042.append_only", report)["detail"], "transfer: lineage rules apply")
        # The fee must come from the buyer: the seller paying it is refused on chain.
        rpc = (FakeRpc().settle(TX_CLAIM, SELLER, USDC, block=HEAD - 5000).settle(TX_FEE1, SELLER, 5 * USDC)
               .settle(TX_SALE1, BUYER, 5 * USDC, to=SELLER))
        report = self._run(prev, rpc)
        self.assertEqual(status_of("w1-b0042.update_fee_base_rpc", report)["status"], "FAIL")
        # The sale must be what the seller signed: 4 USDC on chain for a 5 USDC release is refused.
        rpc = (FakeRpc().settle(TX_CLAIM, SELLER, USDC, block=HEAD - 5000).settle(TX_FEE1, BUYER, 5 * USDC)
               .settle(TX_SALE1, BUYER, 4 * USDC, to=SELLER))
        report = self._run(prev, rpc)
        self.assertEqual(status_of("w1-b0042.sale_base_rpc", report)["status"], "FAIL")
        # Offline the sale check is skipped, like every chain check.
        report = self._run(prev)
        self.assertEqual(status_of("w1-b0042.sale_base_rpc", report)["status"], "WARN")

    def test_transfer_without_release_or_fee_fails(self):
        prev = self._inscribe()
        unreleased = transferred(prev)
        del unreleased["ownership_lineage"][-1]["transfer_signature"]
        self._write(unreleased)
        report = self._run(prev)
        self.assertEqual(status_of("w1-b0042.ownership_lineage", report)["status"], "FAIL")
        self.assertEqual(status_of("w1-b0042.ownership_chain", report)["status"], "FAIL")
        forged = transferred(prev, seller_key=STRANGER_KEY)
        self._write(forged)
        report = self._run(prev)
        self.assertIn("not by the outgoing owner", status_of("w1-b0042.ownership_lineage", report)["detail"])
        unpaid = transferred(prev)
        unpaid["update_settlements"] = []
        self._write(unpaid)
        report = self._run(prev)
        self.assertIn("settles a transfer fee of 5.00", status_of("w1-b0042.update_settlements", report)["detail"])

    def test_transfer_is_refused_while_blocks_are_unsold(self):
        prev = self._inscribe()
        self._write(transferred(prev))
        report = self._run(prev, sold_out=False)
        self.assertEqual(report["result"], "FAIL")
        self.assertIn("transfers open once every block is sold; 9990 of 10000",
                      status_of("w1-b0042.ownership_lineage", report)["detail"])

    def test_history_is_re_proven_on_a_full_run(self):
        prev = self._inscribe()
        sold = self._write(transferred(prev))
        self._stamp(BUYER_KEY)
        rpc = (FakeRpc().settle(TX_CLAIM, SELLER, USDC, block=HEAD - 5000).settle(TX_FEE1, BUYER, 5 * USDC)
               .settle(TX_SALE1, BUYER, 5 * USDC, to=SELLER))
        with mock.patch.object(core, "unsold_slots_in", return_value=0):
            report = verifier.run_verification(self.td, rpc=rpc, rpc_scope="all")
        self.assertEqual(report["result"], "PASS", failing(report))
        self.assertIn("[0] 5.00 USDC", status_of("w1-b0042.update_fee_base_rpc", report)["detail"])
        self.assertIn("[0] sale of 5.00 USDC proven", status_of("w1-b0042.sale_base_rpc", report)["detail"])
        self.assertIn("1 transfer", status_of("w1-b0042.ownership_chain", report)["detail"])


if __name__ == "__main__":
    unittest.main()
