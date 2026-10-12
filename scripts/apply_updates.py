#!/usr/bin/env python3
"""
The First Wall — Apply pending update requests
==============================================

    python scripts/apply_updates.py            # apply every pending request that verifies, regenerate the state
    python scripts/apply_updates.py --dry-run  # print the plan only
    python scripts/apply_updates.py --check    # exit 1 if a pending request could be applied but is not

An owner changes a block by posting a REQUEST (ledger/updates/<fee_tx>.json, see
scripts/updates.py), never by editing the slot and never by touching the
generated state. Once a request is merged this script makes it the slot's files,
byte for byte, removes it and regenerates state.json, ledger/index.json,
ledger/settlements.json and the canvas composite.

Each pending request is first tried on a throw-away overlay of the ledger with
the verifier's own checks (the same ones that ran when it was posted, plus the
ones that depend on the ledger as it is now: the fee's finality on Base, the
block's history). A request that does not verify stays pending and is listed
with the reason; it blocks nobody else. The requests that pass are tried in
turn, each on top of the ones accepted before it, so two requests that cannot
live together (a transaction cited twice) never land together.

Runs in CI after a request merges (.github/workflows/apply_updates.yml) and
opens the apply pull request; it can also be run by hand.
"""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger_core as core  # noqa: E402
import updates  # noqa: E402
import verifier  # noqa: E402

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def try_request(work_dir: str, base_dir: str, name: str, rpc, stack: contextlib.ExitStack
                ) -> Tuple[str, str, str]:
    """Try one request on an overlay of `work_dir`. Returns (slot_id, overlay_dir, "")
    when it verifies, ("", "", reason) when it does not. `base_dir` is the real
    tree the one-request-per-block rule is read from."""
    tx = name[:-5]
    request = updates.load_update(base_dir, name)
    verifier.verify_update_identity(name, request, base_dir)
    tmp = stack.enter_context(tempfile.TemporaryDirectory(prefix="tfw-apply-"))
    slot_id = updates.build_overlay(work_dir, tx, tmp)
    report = verifier.run_verification(
        tmp, rpc=rpc, rpc_scope="changed", check_anchors=False, only_slots={slot_id},
        scope_override=verifier.make_scope("update", slots={slot_id}, updates_={tx}))
    failed = [c for c in report["checks"] if c["status"] == "FAIL"]
    if failed:
        return "", "", "; ".join(f"{c['check']}: {c['detail']}"[:300] for c in failed[:3])
    return slot_id, tmp, ""


def plan(base_dir: str, rpc=None) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], contextlib.ExitStack]:
    """([accepted], [skipped], stack): the requests that verify, in file order, each on
    top of the ones before it, and the ones that do not, with the reason. The
    caller closes the stack (it owns the overlays)."""
    rpc = rpc or core.rpc_call
    stack = contextlib.ExitStack()
    accepted: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    work = base_dir
    for name in updates.list_update_files(base_dir):
        tx = name[:-5]
        try:
            slot_id, overlay, reason = try_request(work, base_dir, name, rpc, stack)
        except Exception as e:  # noqa: BLE001
            slot_id, overlay, reason = "", "", str(e)
        if reason:
            skipped.append({"tx_hash": tx, "reason": reason})
            continue
        accepted.append({"tx_hash": tx, "slot_id": slot_id})
        work = overlay
    return accepted, skipped, stack


def apply(base_dir: str, accepted: List[Dict[str, Any]]) -> None:
    for a in accepted:
        slot_id = updates.apply_request(base_dir, a["tx_hash"])
        print(f"[✓] {a['tx_hash'][:12]}… -> {slot_id}")


def regenerate(base_dir: str) -> None:
    """Rewrite the generated state after an apply (scripts/generate_state.py for the
    repository itself, with the canvas composite; the same derivation for any other
    ledger root)."""
    import generate_state  # noqa: E402  (same scripts dir)
    if os.path.abspath(base_dir) == os.path.abspath(generate_state.BASE_DIR):
        generate_state.generate()
        return
    updates.write_derived(base_dir)


def main(argv: list, base_dir: str = BASE_DIR, rpc=None) -> int:
    pending = updates.list_update_files(base_dir)
    if not pending:
        print("[=] No pending update requests.")
        return 0
    accepted, skipped, stack = plan(base_dir, rpc=rpc)
    with stack:
        for s in skipped:
            print(f"[!] {s['tx_hash'][:12]}… stays pending: {s['reason']}")
        if not accepted:
            print("[=] Nothing to apply.")
            return 0
        for a in accepted:
            print(f"[*] plan: {a['tx_hash'][:12]}… -> {a['slot_id']}")
    if "--dry-run" in argv:
        return 0
    if "--check" in argv:
        print("[✗] pending requests can be applied; run scripts/apply_updates.py")
        return 1
    apply(base_dir, accepted)
    regenerate(base_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
