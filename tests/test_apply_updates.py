#!/usr/bin/env python3
"""
The First Wall — Update requests and the apply
==============================================

Owner ruling of 2026-10-12: nobody outside touches the generated state
(state.json, ledger/index.json, ledger/settlements.json, the canvas composite),
and nobody outside edits a slot. An owner posts a REQUEST
(ledger/updates/<fee_tx>.json, plus .webp / .soul.json when they change); a bot
applies it and regenerates the state; the verifier re-proves both steps.

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

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import apply_updates  # noqa: E402
import claims  # noqa: E402
import ledger_core as core  # noqa: E402
import updates  # noqa: E402
import verifier  # noqa: E402

from PIL import Image  # noqa: E402

# Shared fixtures (functions only, so no test case is collected twice).
from test_updates import (BUYER, BUYER_KEY, HEAD, SELLER, SELLER_KEY, TX_CLAIM, TX_FEE1, TX_FEE2,  # noqa: E402
                          TX_SALE1, USDC, FakeRpc, fee, failing, inscribed, status_of, transferred)

SLOT = "w1-b0042"
OTHER = "w1-b0043"
TX_CLAIM_OTHER = "0x" + "d" * 64


def entries_for(tx, status="A", icon=False, soul=False):
    out = [(status, f"ledger/updates/{tx}.json")]
    if icon:
        out.append((status, f"ledger/updates/{tx}.webp"))
    if soul:
        out.append((status, f"ledger/updates/{tx}.soul.json"))
    return out


GENERATED = [("M", p) for p in sorted(verifier.GENERATED_PATHS)]


def slot_files(slot_id, status="M"):
    return [(status, f"ledger/w1/{slot_id}.json"), (status, f"ledger/w1/{slot_id}.webp"),
            (status, f"ledger/souls/{slot_id}.soul.json")]


# --------------------------------------------------------------------------- #
# The containment rule
# --------------------------------------------------------------------------- #

class ScopeTests(unittest.TestCase):

    def test_an_update_pr_is_exactly_one_request(self):
        for e in (entries_for(TX_FEE1), entries_for(TX_FEE1, icon=True), entries_for(TX_FEE1, icon=True, soul=True)):
            with self.subTest(entries=e):
                scope = verifier.scope_of(e)
                self.assertEqual(scope["kind"], "update")
                self.assertEqual(scope["updates"], {TX_FEE1})
                self.assertEqual(scope["slots"], set())
                self.assertEqual(verifier.check_scope(e), set())

    def test_the_owner_touches_no_slot_and_no_generated_file(self):
        # The whole point: not the slot, not the state, not the index, not the settlements,
        # not the canvas, not anything else.
        for stray in [("M", p) for p in sorted(verifier.GENERATED_PATHS)] + [
                ("M", f"ledger/w1/{SLOT}.json"), ("M", f"ledger/w1/{SLOT}.webp"),
                ("M", f"ledger/souls/{SLOT}.soul.json"), ("M", "scripts/verifier.py"), ("M", "README.md"),
                ("A", "ledger/updates/README.md"), ("A", f"ledger/claims/{TX_CLAIM}.json")]:
            with self.subTest(stray=stray), self.assertRaises(ValueError):
                verifier.scope_of(entries_for(TX_FEE1) + [stray])

    def test_the_message_names_what_the_owner_did_wrong(self):
        with self.assertRaisesRegex(ValueError, r"never touches a slot or the generated state"):
            verifier.scope_of(entries_for(TX_FEE1) + [("M", "state.json")])

    def test_two_requests_in_one_pr_are_refused(self):
        with self.assertRaisesRegex(ValueError, "ONE update request"):
            verifier.scope_of(entries_for(TX_FEE1) + entries_for(TX_FEE2))

    def test_a_request_needs_its_dossier(self):
        with self.assertRaisesRegex(ValueError, "is its dossier"):
            verifier.scope_of([("A", f"ledger/updates/{TX_FEE1}.webp")])

    def test_a_merged_request_is_never_edited(self):
        with self.assertRaisesRegex(ValueError, "never edited"):
            verifier.scope_of([("M", f"ledger/updates/{TX_FEE1}.json")])

    def test_claims_and_requests_travel_apart(self):
        with self.assertRaisesRegex(ValueError, "separate PRs"):
            verifier.scope_of(entries_for(TX_FEE1) + [("A", f"ledger/claims/{TX_CLAIM}.json")])

    def test_apply_pr(self):
        e = entries_for(TX_FEE1, "D", icon=True) + entries_for(TX_FEE2, "D") + \
            slot_files(SLOT) + [("M", f"ledger/w1/{OTHER}.json")] + GENERATED
        scope = verifier.scope_of(e)
        self.assertEqual(scope["kind"], "apply")
        self.assertEqual(scope["applied"], {TX_FEE1, TX_FEE2})
        self.assertEqual(scope["slots"], {SLOT, OTHER})

    def test_apply_may_add_a_manifest_the_slot_lacked(self):
        e = entries_for(TX_FEE1, "D", soul=True) + [("M", f"ledger/w1/{SLOT}.json"),
                                                   ("A", f"ledger/souls/{SLOT}.soul.json")] + GENERATED
        self.assertEqual(verifier.scope_of(e)["kind"], "apply")

    def test_apply_changes_existing_slots_only(self):
        with self.assertRaisesRegex(ValueError, "existing slots only"):
            verifier.scope_of(entries_for(TX_FEE1, "D") + slot_files(SLOT, "A") + GENERATED)

    def test_apply_counts_must_match(self):
        with self.assertRaisesRegex(ValueError, "exactly one slot"):
            verifier.scope_of(entries_for(TX_FEE1, "D") + entries_for(TX_FEE2, "D") + slot_files(SLOT) + GENERATED)
        with self.assertRaisesRegex(ValueError, "exactly one slot"):
            verifier.scope_of(entries_for(TX_FEE1, "D") + slot_files(SLOT) + slot_files(OTHER) + GENERATED)

    def test_apply_touches_nothing_else(self):
        with self.assertRaisesRegex(ValueError, "scripts/apply_updates.py"):
            verifier.scope_of(entries_for(TX_FEE1, "D") + slot_files(SLOT) + GENERATED +
                              [("M", "scripts/apply_updates.py")])

    def test_apply_is_never_mixed_with_a_post(self):
        with self.assertRaisesRegex(ValueError, "never both"):
            verifier.scope_of(entries_for(TX_FEE1, "D") + entries_for(TX_FEE2, "A") + slot_files(SLOT))

    def test_a_withdrawal_removes_requests_and_nothing_else(self):
        scope = verifier.scope_of(entries_for(TX_FEE1, "D", icon=True))
        self.assertEqual(scope["kind"], "withdrawal")
        self.assertEqual(scope["withdrawn"], {TX_FEE1})
        with self.assertRaisesRegex(ValueError, "only removes request files"):
            verifier.scope_of(entries_for(TX_FEE1, "D") + [("M", "state.json")])

    def test_a_slot_is_never_changed_directly(self):
        # What an owner's pull request used to be; it is a request now.
        with self.assertRaisesRegex(ValueError, "never changed directly"):
            verifier.scope_of(slot_files(SLOT) + GENERATED)


# --------------------------------------------------------------------------- #
# A temporary ledger with one inscribed block and requests for it
# --------------------------------------------------------------------------- #

class RequestLedger(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.w1 = os.path.join(self.td, "ledger", "w1")
        self.souls = os.path.join(self.td, "ledger", "souls")
        self.upd = os.path.join(self.td, "ledger", "updates")
        os.makedirs(self.w1)
        os.makedirs(self.souls)
        self._env = dict(os.environ)
        os.environ["TFW_DIFF_RANGE"] = "no-such-ref...no-such-ref"
        os.environ["GITHUB_BASE_REF"] = "no-such-branch"
        self.prev = self._inscribe()
        self.base = self._snapshot_base()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.td, ignore_errors=True)

    # --- building blocks ---------------------------------------------------

    @staticmethod
    def manifest_bytes(slot_id, moniker, note):
        return (json.dumps({"slot_id": slot_id, "moniker": moniker, "note": note}, indent=2) + "\n").encode()

    @staticmethod
    def icon_bytes(color=(20, 200, 10, 255)):
        buf = io.BytesIO()
        Image.new("RGBA", (10, 10), color).save(buf, "WEBP", lossless=True, quality=100, exact=True)
        return buf.getvalue()

    def _write_slot(self, d, note="first", slot_id=SLOT):
        raw = self.manifest_bytes(slot_id, d["moniker"], note)
        with open(os.path.join(self.souls, f"{slot_id}.soul.json"), "wb") as fh:
            fh.write(raw)
        d["soul_hash"] = hashlib.sha256(raw).hexdigest()
        d["soul_manifest_rel_path"] = f"souls/{slot_id}.soul.json"
        with open(os.path.join(self.w1, f"{slot_id}.webp"), "wb") as fh:
            fh.write(self.icon_bytes())
        d["claim_signature"] = claims.sign_claim(d, SELLER_KEY)
        with open(os.path.join(self.w1, f"{slot_id}.json"), "w", encoding="utf-8") as fh:
            fh.write(core.render(d))
        return d

    def _generate(self):
        updates.write_derived(self.td)

    def _inscribe(self):
        d = self._write_slot(inscribed(slot_id=SLOT))
        self._generate()
        return d

    def _snapshot_base(self):
        """What `main` says: every file the verifier reads from git."""
        out = {}
        for root, _dirs, files in os.walk(os.path.join(self.td, "ledger")):
            for f in files:
                path = os.path.join(root, f)
                with open(path, "rb") as fh:
                    out[os.path.relpath(path, self.td)] = fh.read()
        return out

    def post(self, new, tx=TX_FEE1, soul_note=None, icon=None, key=SELLER_KEY):
        """Write a request: the dossier, and the manifest / icon when they change.
        The newest fee entry is signed by the owner over the content (the fee proves
        payment, not authorship), hashed with the icon that will be in effect."""
        os.makedirs(self.upd, exist_ok=True)
        new = dict(new)
        if soul_note is not None:
            raw = self.manifest_bytes(new["slot_id"], new["moniker"], soul_note)
            with open(os.path.join(self.upd, f"{tx}.soul.json"), "wb") as fh:
                fh.write(raw)
            new["soul_hash"] = hashlib.sha256(raw).hexdigest()
        if icon is not None:
            with open(os.path.join(self.upd, f"{tx}.webp"), "wb") as fh:
                fh.write(icon)
        fees = new.get("update_settlements")
        if fees:
            # The icon the slot will carry once applied: the request's, else the slot's.
            effective = icon if icon is not None else self.icon_bytes()
            entry = fees[-1]
            entry["bundle_sha256"] = claims.bundle_sha256(new, effective)
            entry["update_signature"] = claims.sign_update(new["slot_id"], entry["tx_hash"],
                                                           entry["bundle_sha256"], key)
        with open(os.path.join(self.upd, f"{tx}.json"), "w", encoding="utf-8") as fh:
            fh.write(core.render(new))
        return new

    def request_entries(self, tx=TX_FEE1):
        return [("A", p) for p in sorted(updates.update_paths_for(tx))
                if os.path.exists(os.path.join(self.td, p))]

    def pr(self, tx=TX_FEE1, rpc=None, sold_out=True, **kw):
        """Verify the request pull request as CI does."""
        with mock.patch.object(verifier, "changed_entries", return_value=self.request_entries(tx)), \
                mock.patch.object(verifier, "_git_show", side_effect=self.base.get), \
                mock.patch.object(core, "unsold_slots_in", return_value=0 if sold_out else 9990):
            return verifier.run_verification(self.td, rpc=rpc, skip_rpc=rpc is None, **kw)

    def good_rpc(self, **kw):
        return FakeRpc().settle(TX_CLAIM, SELLER, USDC, block=HEAD - 5000).settle(TX_FEE1, SELLER, USDC // 2)

    def tree_bytes(self):
        out = {}
        for root, _dirs, files in os.walk(self.td):
            for f in files:
                path = os.path.join(root, f)
                with open(path, "rb") as fh:
                    out[os.path.relpath(path, self.td)] = fh.read()
        return out


# --------------------------------------------------------------------------- #
# Verifying a request as the slot it would become
# --------------------------------------------------------------------------- #

class RequestVerificationTests(RequestLedger):

    def test_a_valid_request_passes_and_the_tree_is_left_untouched(self):
        self.post(dict(self.prev, agent_urls={"home": "https://example.com"}, update_settlements=[fee()]))
        before = self.tree_bytes()
        report = self.pr(rpc=self.good_rpc())
        self.assertEqual(report["result"], "PASS", failing(report))
        self.assertIn("update request", status_of("pr.scope_containment", report)["detail"])
        self.assertEqual(status_of(f"update:{TX_FEE1[:12]}.identity", report)["status"], "PASS")
        self.assertIn("update fee 0.50", status_of(f"{SLOT}.update_settlements", report)["detail"])
        self.assertIn("[0] 0.50 USDC", status_of(f"{SLOT}.update_fee_base_rpc", report)["detail"])
        self.assertEqual(status_of("state.root_integrity", report)["status"], "PASS")
        self.assertEqual(report["change_kind"], "update")
        # The overlay is made of links to the real files; nothing may write through them.
        self.assertEqual(self.tree_bytes(), before)

    def test_the_request_is_checked_against_the_block_main_has(self):
        # A sealed field changed without a voucher is refused, as it was when an owner edited the slot.
        self.post(dict(self.prev, moniker="Someone Else", update_settlements=[fee()]))
        report = self.pr()
        self.assertEqual(report["result"], "FAIL")
        self.assertEqual(status_of(f"{SLOT}.append_only", report)["status"], "FAIL")

    def test_a_request_without_its_fee_is_refused(self):
        # Named by a fee transaction, but the dossier carries no fee entry at all.
        os.makedirs(self.upd)
        with open(os.path.join(self.upd, f"{TX_FEE1}.json"), "w", encoding="utf-8") as fh:
            fh.write(core.render(dict(self.prev, agent_urls={"home": "https://example.com"})))
        report = self.pr()
        self.assertEqual(report["result"], "FAIL")
        self.assertIn("named by its fee transaction", status_of(f"update:{TX_FEE1[:12]}.identity", report)["detail"])

    def test_the_fee_must_be_final_on_base(self):
        self.post(dict(self.prev, agent_urls={"home": "https://example.com"}, update_settlements=[fee()]))
        rpc = FakeRpc().settle(TX_CLAIM, SELLER, USDC, block=HEAD - 5000).settle(TX_FEE1, SELLER, USDC // 2, block=HEAD - 5)
        report = self.pr(rpc=rpc)
        self.assertEqual(report["result"], "FAIL")
        self.assertIn("Confirmation Depth", status_of(f"{SLOT}.update_fee_base_rpc", report)["detail"])

    def test_the_request_is_named_by_its_fee_transaction(self):
        self.post(dict(self.prev, agent_urls={"home": "https://example.com"}, update_settlements=[fee(tx=TX_FEE2)]),
                  tx=TX_FEE1)
        report = self.pr()
        self.assertEqual(report["result"], "FAIL")
        self.assertIn("named by its fee transaction", status_of(f"update:{TX_FEE1[:12]}.identity", report)["detail"])

    def test_a_request_changes_an_inscribed_block(self):
        new = self.prev.copy()
        new["slot_id"] = "w1-b0077"
        self.post(dict(new, update_settlements=[fee()]))
        report = self.pr()
        self.assertEqual(report["result"], "FAIL")
        self.assertIn("is not inscribed", status_of(f"update:{TX_FEE1[:12]}.identity", report)["detail"])

    def test_one_request_per_block_waits_at_a_time(self):
        first = dict(self.prev, agent_urls={"home": "https://example.com"}, update_settlements=[fee()])
        self.post(first)
        self.post(dict(self.prev, agent_urls={"home": "https://example.org"}, update_settlements=[fee(tx=TX_FEE2)]),
                  tx=TX_FEE2)
        report = self.pr(tx=TX_FEE2)
        self.assertEqual(report["result"], "FAIL")
        self.assertIn("already waiting", status_of(f"update:{TX_FEE2[:12]}.identity", report)["detail"])

    def test_a_new_icon_travels_with_the_request(self):
        self.post(dict(self.prev, update_settlements=[fee()],
                       supersessions=[{"field": "icon", "previous_value": "sha256:old", "new_value": "sha256:new",
                                       "reason": "new colours", "superseded_at": "2026-10-12T09:00:00Z"}]),
                  icon=self.icon_bytes((200, 20, 10, 255)))
        report = self.pr(rpc=self.good_rpc())
        self.assertEqual(report["result"], "PASS", failing(report))
        self.assertIn("superseded: icon", status_of(f"{SLOT}.append_only", report)["detail"])
        # Without the voucher the same icon is refused, as it was when an owner edited the slot.
        self.post(dict(self.prev, update_settlements=[fee()]), icon=self.icon_bytes((200, 20, 10, 255)))
        report = self.pr(rpc=self.good_rpc())
        self.assertEqual(report["result"], "FAIL")
        self.assertIn("icon changed without a supersession voucher", status_of(f"{SLOT}.append_only", report)["detail"])

    def test_a_transfer_is_a_request_like_any_other(self):
        sold = transferred(self.prev)
        self.post(sold, soul_note="the buyer's manifest", key=BUYER_KEY)
        rpc = (FakeRpc().settle(TX_CLAIM, SELLER, USDC, block=HEAD - 5000).settle(TX_FEE1, BUYER, 5 * USDC)
               .settle(TX_SALE1, BUYER, 5 * USDC, to=SELLER))
        report = self.pr(rpc=rpc)
        self.assertEqual(report["result"], "PASS", failing(report))
        self.assertIn("transfer fee 5.00", status_of(f"{SLOT}.update_settlements", report)["detail"])
        self.assertIn("transfer #1", status_of(f"{SLOT}.ownership_lineage", report)["detail"])
        # Closed while blocks are unsold (the block is not a founding one).
        report = self.pr(rpc=rpc, sold_out=False)
        self.assertEqual(report["result"], "FAIL")
        self.assertIn("transfers open once every block is sold", status_of(f"{SLOT}.ownership_lineage", report)["detail"])

    def test_a_pending_request_whose_block_moved_on_is_flagged_not_failed(self):
        self.post(dict(self.prev, agent_urls={"home": "https://example.com"}, update_settlements=[fee()]))
        # The block changes under it (the apply of some other request).
        moved = dict(self.prev, update_settlements=[fee(tx=TX_FEE2)])
        self._write_slot(moved)
        self._generate()
        with mock.patch.object(verifier, "changed_entries", return_value=None):
            report = verifier.run_verification(self.td, skip_rpc=True)
        self.assertEqual(status_of(f"update:{TX_FEE1[:12]}.current", report)["status"], "WARN")
        self.assertIn("stale", status_of(f"update:{TX_FEE1[:12]}.current", report)["detail"])
        self.assertEqual(status_of(f"update:{TX_FEE1[:12]}.identity", report)["status"], "PASS")


# --------------------------------------------------------------------------- #
# The apply
# --------------------------------------------------------------------------- #

class ApplyTests(RequestLedger):

    def run_apply(self, argv=(), rpc=None):
        rpc = rpc or self.good_rpc()
        with mock.patch.object(verifier, "_git_show", side_effect=self.base.get), \
                mock.patch.object(core, "unsold_slots_in", return_value=0):
            return apply_updates.main(list(argv), base_dir=self.td, rpc=rpc)

    def read(self, rel):
        with open(os.path.join(self.td, rel), "rb") as fh:
            return fh.read()

    def test_the_request_becomes_the_slot_byte_for_byte_and_the_state_follows(self):
        new = self.post(dict(self.prev, agent_urls={"home": "https://example.com"}, update_settlements=[fee()]))
        posted = self.read(f"ledger/updates/{TX_FEE1}.json")
        self.assertEqual(self.run_apply(), 0)
        self.assertEqual(self.read(f"ledger/w1/{SLOT}.json"), posted)
        self.assertFalse(os.path.exists(os.path.join(self.upd, f"{TX_FEE1}.json")))
        # The icon and manifest the request did not carry stay as they were.
        self.assertEqual(self.read(f"ledger/w1/{SLOT}.webp"), self.base[f"ledger/w1/{SLOT}.webp"])
        self.assertEqual(self.read(f"ledger/souls/{SLOT}.soul.json"), self.base[f"ledger/souls/{SLOT}.soul.json"])
        # The generated state is the derivation of the new tree, and lists the fee.
        state, index = core.build_state(self.td)
        self.assertEqual(self.read("state.json"), core.render(state).encode())
        self.assertEqual(self.read("ledger/index.json"), core.render(index).encode())
        settlements = json.loads(self.read("ledger/settlements.json"))
        self.assertEqual(settlements["settlements"][TX_FEE1]["slot_id"], SLOT)
        self.assertNotEqual(self.read("ledger/index.json"), self.base["ledger/index.json"])
        self.assertEqual(new["update_settlements"][0]["tx_hash"], TX_FEE1)

    def test_a_carried_icon_and_manifest_replace_the_slots(self):
        icon = self.icon_bytes((200, 20, 10, 255))
        posted = self.post(transferred(self.prev), soul_note="the buyer's manifest", icon=icon, key=BUYER_KEY)
        rpc = (FakeRpc().settle(TX_CLAIM, SELLER, USDC, block=HEAD - 5000).settle(TX_FEE1, BUYER, 5 * USDC)
               .settle(TX_SALE1, BUYER, 5 * USDC, to=SELLER))
        self.assertEqual(self.run_apply(rpc=rpc), 0)
        self.assertEqual(self.read(f"ledger/w1/{SLOT}.webp"), icon)
        self.assertEqual(hashlib.sha256(self.read(f"ledger/souls/{SLOT}.soul.json")).hexdigest(), posted["soul_hash"])
        self.assertEqual(json.loads(self.read(f"ledger/w1/{SLOT}.json"))["wallet_address"], BUYER)
        self.assertFalse(os.path.exists(os.path.join(self.upd, f"{TX_FEE1}.webp")))
        self.assertFalse(os.path.exists(os.path.join(self.upd, f"{TX_FEE1}.soul.json")))

    def test_dry_run_changes_nothing(self):
        self.post(dict(self.prev, agent_urls={"home": "https://example.com"}, update_settlements=[fee()]))
        before = self.tree_bytes()
        self.assertEqual(self.run_apply(["--dry-run"]), 0)
        self.assertEqual(self.tree_bytes(), before)
        self.assertEqual(self.run_apply(["--check"]), 1)
        self.assertEqual(self.tree_bytes(), before)

    def test_nothing_pending_nothing_done(self):
        before = self.tree_bytes()
        self.assertEqual(self.run_apply(), 0)
        self.assertEqual(self.run_apply(["--check"]), 0)
        self.assertEqual(self.tree_bytes(), before)

    def test_a_request_that_does_not_verify_stays_pending_and_blocks_nobody(self):
        # An unpaid request for one block, a good one for another.
        other = self._write_slot(inscribed(slot_id=OTHER, moniker="Other", base_tx_hash=TX_CLAIM_OTHER),
                                 slot_id=OTHER)
        self._generate()
        self.base = self._snapshot_base()
        self.post(dict(self.prev, agent_urls={"home": "https://example.com"}, update_settlements=[fee(tx=TX_FEE2)]),
                  tx=TX_FEE2)
        good = self.post(dict(other, agent_urls={"home": "https://example.org"},
                              update_settlements=[fee(tx=TX_FEE1)]), tx=TX_FEE1)
        rpc = (FakeRpc().settle(TX_CLAIM, SELLER, USDC, block=HEAD - 5000)
               .settle(TX_CLAIM_OTHER, SELLER, USDC, block=HEAD - 5000).settle(TX_FEE1, SELLER, USDC // 2))
        out = []
        with mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            self.assertEqual(self.run_apply(rpc=rpc), 0)
        self.assertTrue(os.path.exists(os.path.join(self.upd, f"{TX_FEE2}.json")), "the unpaid request waits")
        self.assertFalse(os.path.exists(os.path.join(self.upd, f"{TX_FEE1}.json")))
        self.assertEqual(json.loads(self.read(f"ledger/w1/{OTHER}.json")), good)
        self.assertTrue(any(TX_FEE2[:12] in line and "stays pending" in line for line in out), out)

    def test_two_requests_for_one_block_apply_neither(self):
        self.post(dict(self.prev, agent_urls={"home": "https://example.com"}, update_settlements=[fee()]))
        self.post(dict(self.prev, agent_urls={"home": "https://example.org"}, update_settlements=[fee(tx=TX_FEE2)]),
                  tx=TX_FEE2)
        rpc = self.good_rpc().settle(TX_FEE2, SELLER, USDC // 2)
        before = self.read(f"ledger/w1/{SLOT}.json")
        self.assertEqual(self.run_apply(["--check"], rpc=rpc), 0)
        self.assertEqual(self.run_apply(rpc=rpc), 0)
        self.assertEqual(self.read(f"ledger/w1/{SLOT}.json"), before)
        self.assertEqual(len(updates.list_update_files(self.td)), 2)

    def test_the_apply_pull_request_verifies(self):
        self.post(dict(self.prev, agent_urls={"home": "https://example.com"}, update_settlements=[fee()]))
        requests = {f"ledger/updates/{TX_FEE1}.json": self.read(f"ledger/updates/{TX_FEE1}.json")}
        self.assertEqual(self.run_apply(), 0)
        base = dict(self.base, **requests)
        changed = [("D", f"ledger/updates/{TX_FEE1}.json"), ("M", f"ledger/w1/{SLOT}.json")] + GENERATED
        with mock.patch.object(verifier, "changed_entries", return_value=changed), \
                mock.patch.object(verifier, "_git_show", side_effect=base.get), \
                mock.patch.object(core, "unsold_slots_in", return_value=0):
            report = verifier.run_verification(self.td, rpc=self.good_rpc())
        self.assertEqual(report["result"], "PASS", failing(report))
        self.assertIn("applied byte for byte", status_of("apply.requests", report)["detail"])
        self.assertIn("apply:", status_of("pr.scope_containment", report)["detail"])
        self.assertEqual(status_of("state.root_integrity", report)["status"], "PASS")


class VerifyApplyTests(RequestLedger):
    """What the verifier refuses on an apply pull request."""

    def applied(self):
        self.post(dict(self.prev, agent_urls={"home": "https://example.com"}, update_settlements=[fee()]))
        with open(os.path.join(self.upd, f"{TX_FEE1}.json"), "rb") as fh:
            raw = fh.read()
        self.base = dict(self.base, **{f"ledger/updates/{TX_FEE1}.json": raw})
        updates.apply_request(self.td, TX_FEE1)
        self._generate()
        return raw

    def check(self, applied=None, slots=None):
        return verifier.verify_apply(self.td, applied or {TX_FEE1}, slots or {SLOT}, load_base=self.base.get)

    def test_the_honest_apply_passes(self):
        self.applied()
        self.assertIn("1 request(s) applied byte for byte", self.check())

    def test_the_slot_must_be_the_request_byte_for_byte(self):
        self.applied()
        path = os.path.join(self.w1, f"{SLOT}.json")
        with open(path, "rb") as fh:
            raw = fh.read()
        with open(path, "wb") as fh:
            fh.write(raw.replace(b"example.com", b"example.net"))
        with self.assertRaisesRegex(ValueError, "byte for byte"):
            self.check()

    def test_the_icon_may_not_change_unless_the_request_carries_one(self):
        self.applied()
        with open(os.path.join(self.w1, f"{SLOT}.webp"), "wb") as fh:
            fh.write(self.icon_bytes((1, 2, 3, 255)))
        with self.assertRaisesRegex(ValueError, "request carries none"):
            self.check()

    def test_the_manifest_may_not_change_unless_the_request_carries_one(self):
        self.applied()
        with open(os.path.join(self.souls, f"{SLOT}.soul.json"), "wb") as fh:
            fh.write(b"{}\n")
        with self.assertRaisesRegex(ValueError, "request carries none"):
            self.check()

    def test_only_a_pending_request_can_be_applied(self):
        self.applied()
        with self.assertRaisesRegex(ValueError, "not a pending request on the base branch"):
            verifier.verify_apply(self.td, {TX_FEE2}, {SLOT}, load_base=self.base.get)

    def test_a_block_that_no_request_names_may_not_change(self):
        self.applied()
        with self.assertRaisesRegex(ValueError, "without a request that names them"):
            self.check(slots={SLOT, OTHER})

    def test_a_request_may_not_be_left_behind(self):
        self.applied()
        os.makedirs(self.upd, exist_ok=True)
        with open(os.path.join(self.upd, f"{TX_FEE1}.json"), "w") as fh:
            fh.write("{}")
        with self.assertRaisesRegex(ValueError, "remain"):
            self.check()

    def test_a_request_may_only_change_the_block_it_names(self):
        self.applied()
        with self.assertRaisesRegex(ValueError, "does not change"):
            self.check(slots={OTHER})


# --------------------------------------------------------------------------- #
# The overlay is a copy that cannot write through
# --------------------------------------------------------------------------- #

class OverlayTests(RequestLedger):

    def test_the_overlay_is_the_ledger_after_the_apply_and_the_real_one_is_untouched(self):
        self.post(dict(self.prev, agent_urls={"home": "https://example.com"}, update_settlements=[fee()]))
        before = self.tree_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            slot_id = updates.build_overlay(self.td, TX_FEE1, tmp)
            self.assertEqual(slot_id, SLOT)
            self.assertFalse(os.path.exists(os.path.join(tmp, "ledger", "updates", f"{TX_FEE1}.json")))
            with open(os.path.join(tmp, "ledger", "w1", f"{SLOT}.json"), "rb") as fh:
                overlay_slot = fh.read()
            self.assertEqual(overlay_slot, before[os.path.join("ledger", "updates", f"{TX_FEE1}.json")])
            state, _index = core.build_state(tmp)
            with open(os.path.join(tmp, "state.json"), "rb") as fh:
                self.assertEqual(fh.read(), core.render(state).encode())
        self.assertEqual(self.tree_bytes(), before)

    def test_a_request_names_a_real_block_id(self):
        d = dict(self.prev, update_settlements=[fee()])
        d["slot_id"] = "not-a-slot"
        self.post(d)
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            updates.build_overlay(self.td, TX_FEE1, tmp)


if __name__ == "__main__":
    unittest.main()
