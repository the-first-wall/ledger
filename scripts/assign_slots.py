#!/usr/bin/env python3
"""
The First Wall — Assign slots to pending claims
===============================================

    python scripts/assign_slots.py            # place every pending claim, regenerate the state
    python scripts/assign_slots.py --dry-run  # print the plan only
    python scripts/assign_slots.py --check    # exit 1 if a pending claim could be placed but is not

The system picks the block; the claimant never does. Pending claims
(ledger/claims/<tx>.json) are placed in settlement order — (block number,
transaction index) of the USDC transfer on Base, read from the receipt — into
the lowest free public block whose tier price the payment covers. A reserved
block is only placed with a reserved_grant naming it. The verifier re-derives
the same placement from the recorded settlement positions.

Runs in CI after a claim merges (.github/workflows/assign_slots.yml) and opens
the assignment pull request; it can also be run by hand.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claims  # noqa: E402
import ledger_core as core  # noqa: E402
import verifier  # noqa: E402

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def settlement_of(dossier: Dict[str, Any], rpc=core.rpc_call, head_block=None) -> Dict[str, Any]:
    """Block, index and amount of the claim's settlement, proven on Base with
    the verifier's own routine (sender, payee, finality)."""
    return verifier.settlement_from_chain(dossier, rpc=rpc, head_block=head_block)


def pending_claims(base_dir: str, rpc=None) -> List[Dict[str, Any]]:
    rpc = rpc or core.rpc_call
    out = []
    head = int(rpc("eth_blockNumber", []), 16)
    for name in claims.list_claim_files(base_dir):
        tx = name[:-5]
        dossier = claims.load_claim(base_dir, name)
        try:
            settlement = settlement_of(dossier, rpc=rpc, head_block=head)
        except ValueError as e:
            print(f"[!] {tx[:12]}… not placeable yet: {e}")
            continue
        out.append({"tx_hash": tx, "dossier": dossier, "settlement": settlement})
    return out


def apply(base_dir: str, assigned: List[Dict[str, Any]]) -> None:
    slot_dir = core.ledger_w1_dir(base_dir)
    souls_dir = os.path.join(base_dir, "ledger", "souls")
    cdir = claims.claims_dir(base_dir)
    os.makedirs(slot_dir, exist_ok=True)
    os.makedirs(souls_dir, exist_ok=True)
    for a in assigned:
        tx, slot_id = a["tx_hash"], a["slot_id"]
        soul_src = os.path.join(cdir, f"{tx}.soul.json")
        has_manifest = os.path.exists(soul_src)
        dossier = claims.materialize(a["dossier"], slot_id, a["settlement"], has_manifest)
        with open(os.path.join(slot_dir, f"{slot_id}.json"), "w", encoding="utf-8") as fh:
            fh.write(core.render(dossier))
        shutil.move(os.path.join(cdir, f"{tx}.webp"), os.path.join(slot_dir, f"{slot_id}.webp"))
        if has_manifest:
            shutil.move(soul_src, os.path.join(souls_dir, f"{slot_id}.soul.json"))
        os.remove(os.path.join(cdir, f"{tx}.json"))
        print(f"[✓] {tx[:12]}… -> {slot_id} (block {a['settlement']['block_number']}, "
              f"index {a['settlement']['transaction_index']}, {a['settlement']['amount_usdc']:.2f} USDC)")


def main(argv: list, base_dir: str = BASE_DIR, rpc=None) -> int:
    slot_dir = core.ledger_w1_dir(base_dir)
    claimed = {core.slot_number(f[:-5]) for f in core.list_slot_files(slot_dir)}
    pend = pending_claims(base_dir, rpc=rpc)
    assigned, skipped = claims.plan_assignment(pend, claimed)
    for s in skipped:
        print(f"[!] {s['tx_hash'][:12]}… stays pending: {s['reason']}")
    if not assigned:
        print("[=] Nothing to place.")
        return 1 if ("--check" in argv and pend) else 0
    for a in assigned:
        print(f"[*] plan: {a['tx_hash'][:12]}… -> {a['slot_id']}")
    if "--dry-run" in argv:
        return 0
    if "--check" in argv:
        print("[✗] pending claims can be placed; run scripts/assign_slots.py")
        return 1
    apply(base_dir, assigned)
    regenerate(base_dir)
    return 0


def regenerate(base_dir: str) -> None:
    """Rewrite the generated state after a placement (scripts/generate_state.py
    for the repository itself; the same derivation for any other ledger root)."""
    import generate_state  # noqa: E402  (same scripts dir)
    if os.path.abspath(base_dir) == os.path.abspath(generate_state.BASE_DIR):
        generate_state.generate()
        return
    state, index = core.build_state(base_dir)
    settlements, _ = core.build_settlements(base_dir)
    for rel, obj in (("state.json", state), (os.path.join("ledger", "index.json"), index),
                     (os.path.join("ledger", "settlements.json"), settlements)):
        with open(os.path.join(base_dir, rel), "w", encoding="utf-8") as fh:
            fh.write(core.render(obj))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
