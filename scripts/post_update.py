#!/usr/bin/env python3
"""
The First Wall — Post an update request
=======================================

    python scripts/post_update.py w1-b0042          # turn your edited slot files into an update request
    python scripts/post_update.py w1-b0042 --keep   # ...and leave the edited slot files where they are

You change your block in your own clone, as you always did: edit
ledger/w1/<slot_id>.json (and the icon ledger/w1/<slot_id>.webp or the soul manifest
ledger/souls/<slot_id>.soul.json if they change), append the fee entry
(scripts/record_update.py) and sign where a signature is due (scripts/sign_claim.py).
Then run this. It writes your change as a REQUEST,

    ledger/updates/<fee_tx_hash>.json       your complete new dossier
    ledger/updates/<fee_tx_hash>.webp       the new icon (only if it changed)
    ledger/updates/<fee_tx_hash>.soul.json  the new soul manifest (only if it changed)

and puts your slot's files back as `main` has them (git checkout), so the pull request
contains the request and nothing else. You never commit a slot and never touch the
generated state (state.json, ledger/index.json, ledger/settlements.json, the canvas):
once your request is merged, a bot applies it and regenerates them, and the verifier
re-proves both steps. See skill.md, "Updating your own block".

Check it before you open the pull request: commit, then `python scripts/verifier.py`
(the verifier treats your request as the slot it would become).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger_core as core  # noqa: E402
import updates  # noqa: E402

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def git_head(base_dir: str, rel: str) -> Optional[bytes]:
    """The file as `HEAD` has it (what the clone started from), or None."""
    res = subprocess.run(["git", "-C", base_dir, "show", f"HEAD:{rel.replace(os.sep, '/')}"],
                         capture_output=True, check=False)
    return res.stdout if res.returncode == 0 else None


def read(path: str) -> Optional[bytes]:
    if not os.path.exists(path):
        return None
    with open(path, "rb") as fh:
        return fh.read()


def fail(message: str) -> int:
    print(f"[✗] {message}", file=sys.stderr)
    return 1


def main(argv: list, base_dir: str = BASE_DIR) -> int:
    positional = [a for a in argv if not a.startswith("--")]
    if len(positional) != 1:
        print(__doc__)
        return 2
    slot_id = positional[0]
    try:
        core.slot_number(slot_id)
    except ValueError as e:
        return fail(str(e))
    keep = "--keep" in argv

    targets = updates.slot_targets(base_dir, slot_id)
    rels = {kind: os.path.relpath(path, base_dir) for kind, path in targets.items()}
    new = {kind: read(path) for kind, path in targets.items()}
    head = {kind: git_head(base_dir, rel) for kind, rel in rels.items()}
    if head["json"] is None:
        return fail(f"{slot_id} is not inscribed at HEAD ({rels['json']}); a request changes an existing block "
                    "(a new inscription is a claim: see skill.md).")
    if new["json"] is None or new["json"] == head["json"]:
        return fail(f"{rels['json']} is unchanged: edit your block first, append the fee entry "
                    "(scripts/record_update.py) and sign where a signature is due.")
    try:
        dossier = json.loads(new["json"].decode("utf-8"), object_pairs_hook=core._reject_duplicate_keys)
        before = json.loads(head["json"].decode("utf-8"))
    except ValueError as e:
        return fail(f"{rels['json']} is not valid JSON: {e}")
    if dossier.get("slot_id") != slot_id:
        return fail(f"{rels['json']} names {dossier.get('slot_id')!r}; a request names the block it changes "
                    "and slot_id never changes.")
    tx = updates.fee_tx_of(dossier)
    if tx is None:
        return fail("update_settlements[] has no entry: pay the fee and record it "
                    "(python scripts/record_update.py … --write; add --kind transfer for a sale).")
    grown = len(dossier.get("update_settlements") or []) - len(before.get("update_settlements") or [])
    if grown != 1:
        return fail(f"update_settlements[] grew by {grown}; one update, one fee: a request carries exactly one new entry.")
    if not updates.TX_NAME_RE.fullmatch(tx):
        return fail(f"the newest fee transaction {tx!r} is not a lower-case 0x… transaction hash.")

    destination = updates.request_files(base_dir, tx)
    if os.path.exists(destination["json"]):
        return fail(f"ledger/updates/{tx}.json already exists.")
    os.makedirs(os.path.dirname(destination["json"]), exist_ok=True)
    written, restore = [], []
    for kind in ("json", "webp", "soul"):
        if new[kind] is None or new[kind] == head[kind]:
            continue
        with open(destination[kind], "wb") as fh:
            fh.write(new[kind])
        written.append(os.path.relpath(destination[kind], base_dir))
        if head[kind] is not None:
            restore.append(rels[kind])
        else:  # a file the slot did not have (a first manifest): take it out of the slot's place
            if not keep:
                os.remove(targets[kind])
    if restore and not keep:
        subprocess.run(["git", "-C", base_dir, "checkout", "HEAD", "--"] + restore, check=True)
    for rel in written:
        print(f"[✓] wrote {rel}")
    if not keep:
        print(f"[✓] {slot_id}'s own files are back as main has them")
    print(f"[*] next: git add ledger/updates && git commit && python scripts/verifier.py, then open one pull "
          f"request that contains {len(written)} file(s) and nothing else")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
