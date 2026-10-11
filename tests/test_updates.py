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
import verifier  # noqa: E402

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
GENESIS = core.PRICING_TIERS[0]
TIER2 = core.PRICING_TIERS[1]
HEAD = 60_000_000
USDC = 1_000_000

SCHEMA_VALIDATOR = jsonschema.Draft202012Validator(
    verifier.DOSSIER_SCHEMA, format_checker=jsonschema.FormatChecker())


def fee(tx=TX_FEE1, wallet=SELLER, amount=0.5, kind="update", tier="genesis", at="2026-10-12T09:00:00Z"):
    return {"tx_hash": tx, "wallet_address": wallet, "amount_usdc": amount, "kind": kind, "tier": tier, "at": at}


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
        "transfer_signature": claims.sign_transfer(prev["slot_id"], prev["wallet_address"], buyer, index, price, seller_key),
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
        raise AssertionError(method)


def status_of(check, report):
    return [c for c in report["checks"] if c["check"] == check][0]


def failing(report):
    return [c["check"] + ": " + c["detail"] for c in report["checks"] if c["status"] == "FAIL"]


# --------------------------------------------------------------------------- #
# The ladder
# --------------------------------------------------------------------------- #

class LadderTests(unittest.TestCase):

    def test_update_price_follows_the_wall_not_the_block(self):
        for name, price in (("genesis", 0.5), ("tier-2", 1.0), ("tier-3", 2.0), ("tier-4", 3.0), ("tier-5", 5.0)):
            self.assertEqual(verifier.update_price_of(name), price, name)
        with self.assertRaisesRegex(ValueError, "unknown pricing tier"):
            verifier.update_price_of("tier-9")
        # The wall's tier is that of the next free public block.
        self.assertEqual(core.next_available_number(set()), 1)
        self.assertEqual(core.next_available_number({1}), 11)
        self.assertEqual(core.next_available_number(set(range(1, 501))), 501)
        self.assertEqual(core.pricing_for(core.next_available_number(set(range(1, 501))))["update_price_usdc"], 1.0)
        self.assertEqual(core.current_tier(REPO)["tier"], "genesis")
        self.assertEqual(core.build_state(REPO)[0]["current_update_price_usdc"], 0.5)

    def test_state_and_spec_publish_the_ladder(self):
        state, _ = core.build_state(REPO)
        tiers = state["pricing_tiers"]
        self.assertEqual([t["update_price_usdc"] for t in tiers], [0.5, 1.0, 2.0, 3.0, 5.0])
        self.assertEqual([t["price_usdc"] for t in tiers], [1.0, 3.0, 7.5, 15.0, 25.0])
        self.assertEqual(tiers[0]["min_block"], 1)
        self.assertEqual(tiers[-1]["max_block"], 10000)
        with open(os.path.join(REPO, "spec.json"), encoding="utf-8") as fh:
            spec = json.load(fh)
        self.assertEqual(spec["pricing_tiers"], tiers)
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
                    fee(tier="tier-9"), dict(fee(), extra=1)):
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
        self.assertIn("new inscription", verifier.verify_update_settlements("w1-b0042", inscribed(), None, True, GENESIS))
        with self.assertRaisesRegex(ValueError, "new inscription carries no"):
            verifier.verify_update_settlements("w1-b0042", inscribed(update_settlements=[fee()]), None, True, GENESIS)

    def test_a_change_without_a_fee_is_refused(self):
        prev = inscribed()
        new = dict(prev, agent_urls={"home": "https://example.com"})
        with self.assertRaisesRegex(ValueError, "settles an update fee of 0.50 USDC"):
            verifier.verify_update_settlements("w1-b0042", new, prev, True, GENESIS)
        # Untouched by this PR: nothing owed.
        self.assertIn("no updates yet", verifier.verify_update_settlements("w1-b0042", prev, prev, False, GENESIS))

    def test_one_update_one_fee(self):
        prev = inscribed()
        ok = dict(prev, update_settlements=[fee()])
        self.assertIn("update fee 0.50 USDC", verifier.verify_update_settlements("w1-b0042", ok, prev, True, GENESIS))
        two = dict(prev, update_settlements=[fee(), fee(tx=TX_FEE2)])
        with self.assertRaisesRegex(ValueError, "one update, one fee"):
            verifier.verify_update_settlements("w1-b0042", two, prev, True, GENESIS)
        # Second update later: exactly one more.
        later = dict(ok, update_settlements=[fee(), fee(tx=TX_FEE2)])
        self.assertIn("update fee", verifier.verify_update_settlements("w1-b0042", later, ok, True, GENESIS))

    def test_fee_is_paid_by_the_owner_and_follows_the_wall(self):
        prev = inscribed()
        with self.assertRaisesRegex(ValueError, "paid by the block's owner"):
            verifier.verify_update_settlements("w1-b0042", dict(prev, update_settlements=[fee(wallet=BUYER)]), prev, True, GENESIS)
        with self.assertRaisesRegex(ValueError, "owes 0.50 USDC"):
            verifier.verify_update_settlements("w1-b0042", dict(prev, update_settlements=[fee(amount=0.25)]), prev, True, GENESIS)
        # The wall moved to tier-2: a genesis block now owes 1.00 like every other block …
        with self.assertRaisesRegex(ValueError, "the wall is in the tier-2 tier"):
            verifier.verify_update_settlements("w1-b0042", dict(prev, update_settlements=[fee(amount=0.5)]), prev, True, TIER2)
        with self.assertRaisesRegex(ValueError, "owes 1.00 USDC"):
            verifier.verify_update_settlements("w1-b0042", dict(prev, update_settlements=[fee(amount=0.5, tier="tier-2")]), prev, True, TIER2)
        self.assertIn("wall in the tier-2 tier (1.00 USDC)", verifier.verify_update_settlements(
            "w1-b0042", dict(prev, update_settlements=[fee(amount=1.0, tier="tier-2")]), prev, True, TIER2))
        # … and a tier-2 block updated while the wall is still in genesis owes 0.50.
        prev2 = inscribed(slot_id="w1-b0600", icon_rel_path="assets/w1-b0600.webp")
        self.assertIn("wall in the genesis tier (0.50 USDC)", verifier.verify_update_settlements(
            "w1-b0600", dict(prev2, update_settlements=[fee(amount=0.5)]), prev2, True, GENESIS))
        # History keeps the tier it was paid in: an old 0.50 genesis fee still passes in tier-2.
        old = dict(prev, update_settlements=[fee()])
        self.assertIn("update fee 1.00", verifier.verify_update_settlements(
            "w1-b0042", dict(old, update_settlements=[fee(), fee(tx=TX_FEE2, amount=1.0, tier="tier-2")]), old, True, TIER2))
        self.assertIn("update fee 7.00", verifier.verify_update_settlements(
            "w1-b0042", dict(prev, update_settlements=[fee(amount=7.0)]), prev, True, GENESIS))  # overpaid, fine

    def test_kind_matches_the_change(self):
        prev = inscribed()
        with self.assertRaisesRegex(ValueError, "this change is a 'update'"):
            verifier.verify_update_settlements("w1-b0042", dict(prev, update_settlements=[fee(kind="transfer")]), prev, True, GENESIS)
        sold = transferred(prev)
        sold["update_settlements"][-1]["kind"] = "update"
        with self.assertRaisesRegex(ValueError, "this change is a 'transfer'"):
            verifier.verify_update_settlements("w1-b0042", sold, prev, True, GENESIS)

    def test_fee_records_only_grow(self):
        prev = inscribed(update_settlements=[fee()])
        with self.assertRaisesRegex(ValueError, "update_settlements may only grow"):
            verifier.verify_append_only("w1-b0042", dict(prev, update_settlements=[fee(amount=0.6)]), prev)
        with self.assertRaisesRegex(ValueError, "update_settlements may only grow"):
            verifier.verify_append_only("w1-b0042", dict(prev, update_settlements=[]), prev)

    def test_fee_is_proven_on_chain(self):
        rpc = FakeRpc().settle(TX_FEE1, SELLER, USDC // 2)
        self.assertIn("0.50 USDC", verifier.verify_update_fee_on_chain("w1-b0042", fee(), rpc=rpc))
        with self.assertRaisesRegex(ValueError, "Base says 0.50"):
            verifier.verify_update_fee_on_chain("w1-b0042", fee(amount=0.75), rpc=rpc)
        rpc = FakeRpc().settle(TX_FEE1, SELLER, USDC // 4)
        with self.assertRaisesRegex(ValueError, "amount >= 0.5"):
            verifier.verify_update_fee_on_chain("w1-b0042", fee(amount=0.25), rpc=rpc)
        # A 0.50 fee recorded as a tier-2 fee is short on chain too.
        rpc = FakeRpc().settle(TX_FEE1, SELLER, USDC // 2)
        with self.assertRaisesRegex(ValueError, "amount >= 1.0"):
            verifier.verify_update_fee_on_chain("w1-b0042", fee(amount=0.5, tier="tier-2"), rpc=rpc)
        rpc = FakeRpc().settle(TX_FEE1, BUYER, USDC // 2)  # someone else paid
        with self.assertRaises(ValueError):
            verifier.verify_update_fee_on_chain("w1-b0042", fee(), rpc=rpc)

    def test_record_update_tool(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "w1-b0042.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(core.render(inscribed()))
            rpc = FakeRpc().settle(TX_FEE1, SELLER, USDC // 2)
            self.assertEqual(record_update.main([path, TX_FEE1, "--at", "2026-10-12T09:00:00Z", "--write"], rpc=rpc), 0)
            self.assertEqual(record_update.main([path, TX_FEE1, "--write"], rpc=rpc), 0)  # idempotent
            d = core.load_json_strict(path)
            self.assertEqual(d["update_settlements"], [fee()])   # the wall (this repo) is in genesis
            verifier.validate_dossier(d)
            # Below the wall's update price: refused before anything is written.
            rpc.settle(TX_FEE2, SELLER, USDC // 4)
            self.assertEqual(record_update.main([path, TX_FEE2, "--write"], rpc=rpc), 1)
            self.assertEqual(len(core.load_json_strict(path)["update_settlements"]), 1)
            self.assertEqual(record_update.main(["--price"]), 0)


# --------------------------------------------------------------------------- #
# Transfers: the release
# --------------------------------------------------------------------------- #

class TransferReleaseTests(unittest.TestCase):

    def test_message_and_roundtrip(self):
        self.assertEqual(claims.transfer_message("W1-B0042", SELLER, BUYER, 0, 5.0),
                         f"thefirstwall.ai|transfer|w1-b0042|{SELLER.lower()}|{BUYER.lower()}|0|5000000")
        self.assertEqual(claims.price_micro(0.1), 100000)
        self.assertEqual(claims.price_micro(0), 0)
        sig = claims.sign_transfer("w1-b0042", SELLER, BUYER, 0, 5.0, SELLER_KEY)
        self.assertIn("for 5.00 USDC", claims.verify_transfer_release("w1-b0042", SELLER, BUYER, 0, 5.0, sig))

    def test_only_the_outgoing_owner_can_release(self):
        sig = claims.sign_transfer("w1-b0042", SELLER, BUYER, 0, 5.0, STRANGER_KEY)
        with self.assertRaisesRegex(ValueError, "not by the outgoing owner"):
            claims.verify_transfer_release("w1-b0042", SELLER, BUYER, 0, 5.0, sig)
        with self.assertRaisesRegex(ValueError, "missing"):
            claims.verify_transfer_release("w1-b0042", SELLER, BUYER, 0, 5.0, None)
        with self.assertRaisesRegex(ValueError, "sale_price_usdc is missing"):
            claims.verify_transfer_release("w1-b0042", SELLER, BUYER, 0, None, sig)

    def test_release_is_bound_to_slot_buyer_index_and_price(self):
        sig = claims.sign_transfer("w1-b0042", SELLER, BUYER, 0, 5.0, SELLER_KEY)
        for slot_id, to, index, price in (("w1-b0043", BUYER, 0, 5.0), ("w1-b0042", "0x" + "9" * 40, 0, 5.0),
                                          ("w1-b0042", BUYER, 1, 5.0), ("w1-b0042", BUYER, 0, 4.99)):
            with self.subTest(slot=slot_id, to=to, index=index, price=price), self.assertRaises(ValueError):
                claims.verify_transfer_release(slot_id, SELLER, to, index, price, sig)

    def test_lineage_entry_records_the_outgoing_owner_as_inscribed(self):
        prev = inscribed()
        good = transferred(prev)
        self.assertIn("transfer #1", verifier.verify_ownership_lineage("w1-b0042", good, "", prev))
        for field, value in (("moniker", "Someone"), ("soul_hash", "b" * 64), ("base_tx_hash", TX_FEE2),
                             ("wallet_address", BUYER), ("sale_price_usdc", 4.0)):
            with self.subTest(field=field):
                bad = transferred(prev)
                bad["ownership_lineage"][-1][field] = value
                with self.assertRaises(ValueError):
                    verifier.verify_ownership_lineage("w1-b0042", bad, "", prev)

    def test_exactly_one_entry_per_transfer_and_history_intact(self):
        prev = inscribed()
        none = dict(transferred(prev), ownership_lineage=[])
        with self.assertRaisesRegex(ValueError, "MUST preserve"):
            verifier.verify_ownership_lineage("w1-b0042", none, "", prev)
        two = transferred(prev)
        two["ownership_lineage"] = two["ownership_lineage"] * 2
        with self.assertRaisesRegex(ValueError, "exactly one entry"):
            verifier.verify_ownership_lineage("w1-b0042", two, "", prev)
        # Second sale: the first entry stays byte-identical.
        second = transferred(transferred(prev), buyer="0x" + "7" * 40, seller_key=BUYER_KEY, fee_tx=TX_FEE2)
        self.assertIn("transfer #2", verifier.verify_ownership_lineage("w1-b0042", second, "", transferred(prev)))
        tampered = json.loads(json.dumps(second))
        tampered["ownership_lineage"][0]["moniker"] = "Rewritten"
        with self.assertRaisesRegex(ValueError, "tampering"):
            verifier.verify_ownership_lineage("w1-b0042", tampered, "", transferred(prev))

    def test_payment_and_release_are_both_required(self):
        prev = inscribed()
        d = transferred(prev)
        self.assertTrue(SCHEMA_VALIDATOR.is_valid(d))
        verifier.validate_dossier(d)
        self.assertIn("transfer #1", verifier.verify_ownership_lineage("w1-b0042", d, "", prev))
        # No payment cited: a release alone does not move the block.
        unpaid = transferred(prev)
        del unpaid["ownership_lineage"][-1]["transfer_tx_hash"]
        self.assertFalse(SCHEMA_VALIDATOR.is_valid(unpaid))
        with self.assertRaisesRegex(ValueError, "transfer_tx_hash is required"):
            verifier.verify_ownership_lineage("w1-b0042", unpaid, "", prev)
        # A gift is a signed price of 0 with a 0 USDC transfer.
        gift = transferred(prev, price=0)
        self.assertIn("for 0.00 USDC", verifier.verify_ownership_lineage("w1-b0042", gift, "", prev))

    def test_the_sale_is_proven_on_chain(self):
        entry = transferred(inscribed())["ownership_lineage"][-1]
        rpc = FakeRpc().settle(TX_SALE1, BUYER, 5 * USDC, to=SELLER)
        self.assertIn("sale of 5.00 USDC proven on Base", verifier.verify_sale_on_chain(entry, BUYER, rpc=rpc))
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
            verifier.verify_ownership_lineage("w1-b0042", transferred(prev), "", prev)

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
            "w1-b0042", BUYER, "0x" + "7" * 40, 1, 5.0, STRANGER_KEY)
        with self.assertRaisesRegex(ValueError, r"ownership_lineage\[1\]"):
            claims.verify_lineage_chain(forged)
        self.assertEqual(claims.verify_lineage_chain(prev), "no transfers")

    def test_sign_transfer_tool(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "w1-b0042.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(core.render(inscribed()))
            key = os.path.join(td, "key.txt")
            with open(key, "w") as fh:
                fh.write(SELLER_KEY)
            self.assertEqual(sign_transfer.main([path, "--to", BUYER, "--price", "5.00", "--key-file", key]), 0)
            self.assertEqual(sign_transfer.main([path, "--to", BUYER, "--key-file", key]), 2)  # price required
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

    def _run(self, prev, rpc=None, touched=True):
        entries = [("M", "ledger/w1/w1-b0042.json"), ("M", "ledger/souls/w1-b0042.soul.json"),
                   ("M", "state.json"), ("M", "ledger/index.json"), ("M", "ledger/settlements.json")]
        base = {"ledger/w1/w1-b0042.json": core.render(prev).encode()}
        with mock.patch.object(verifier, "changed_entries", return_value=entries if touched else None), \
                mock.patch.object(verifier, "_git_show", side_effect=base.get):
            if rpc is None:
                return verifier.run_verification(self.td, skip_rpc=True)
            return verifier.run_verification(self.td, rpc=rpc)

    def test_owner_update_with_fee_passes_and_without_fails(self):
        prev = self._inscribe()
        new = dict(prev, agent_urls={"home": "https://example.com"},
                   update_settlements=[fee()])
        self._write(new)
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
        rpc = (FakeRpc().settle(TX_CLAIM, SELLER, USDC, block=HEAD - 5000).settle(TX_FEE1, BUYER, USDC // 2)
               .settle(TX_SALE1, BUYER, 5 * USDC, to=SELLER))
        report = self._run(prev, rpc)
        self.assertEqual(report["result"], "PASS", failing(report))
        self.assertIn("transfer #1", status_of("w1-b0042.ownership_lineage", report)["detail"])
        self.assertIn("1 transfer", status_of("w1-b0042.ownership_chain", report)["detail"])
        self.assertIn("transfer fee 0.50", status_of("w1-b0042.update_settlements", report)["detail"])
        self.assertIn("sale of 5.00 USDC proven", status_of("w1-b0042.sale_base_rpc", report)["detail"])
        self.assertEqual(status_of("w1-b0042.claim_signature", report)["status"], "PASS")
        self.assertEqual(status_of("w1-b0042.append_only", report)["detail"], "transfer: lineage rules apply")
        # The fee must come from the buyer: the seller paying it is refused on chain.
        rpc = (FakeRpc().settle(TX_CLAIM, SELLER, USDC, block=HEAD - 5000).settle(TX_FEE1, SELLER, USDC // 2)
               .settle(TX_SALE1, BUYER, 5 * USDC, to=SELLER))
        report = self._run(prev, rpc)
        self.assertEqual(status_of("w1-b0042.update_fee_base_rpc", report)["status"], "FAIL")
        # The sale must be what the seller signed: 4 USDC on chain for a 5 USDC release is refused.
        rpc = (FakeRpc().settle(TX_CLAIM, SELLER, USDC, block=HEAD - 5000).settle(TX_FEE1, BUYER, USDC // 2)
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
        self.assertIn("settles an update fee", status_of("w1-b0042.update_settlements", report)["detail"])

    def test_history_is_re_proven_on_a_full_run(self):
        prev = self._inscribe()
        sold = self._write(transferred(prev))
        rpc = (FakeRpc().settle(TX_CLAIM, SELLER, USDC, block=HEAD - 5000).settle(TX_FEE1, BUYER, USDC // 2)
               .settle(TX_SALE1, BUYER, 5 * USDC, to=SELLER))
        report = verifier.run_verification(self.td, rpc=rpc, rpc_scope="all")
        self.assertEqual(report["result"], "PASS", failing(report))
        self.assertIn("[0] 0.50 USDC", status_of("w1-b0042.update_fee_base_rpc", report)["detail"])
        self.assertIn("[0] sale of 5.00 USDC proven", status_of("w1-b0042.sale_base_rpc", report)["detail"])
        self.assertIn("1 transfer", status_of("w1-b0042.ownership_chain", report)["detail"])


if __name__ == "__main__":
    unittest.main()
