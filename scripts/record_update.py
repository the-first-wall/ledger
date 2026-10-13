#!/usr/bin/env python3
"""
The First Wall — Record an update fee
=====================================

    python scripts/record_update.py ledger/w1/w1-b0042.json 0x<tx_hash> --write
    python scripts/record_update.py ledger/w1/w1-b0042.json 0x<tx_hash> --kind transfer --write
    python scripts/record_update.py --price          # what an update and a transfer cost (flat)

Every change of an inscribed block by its owner settles one USDC transfer from
the owner's wallet to pay_to and appends exactly one entry to
update_settlements[]: a content update owes the update fee (0.50 USDC, flat,
kept low so the wall stays up to date; it finances the re-anchoring of every
change on Base), a change of owner owes the transfer fee instead (5.00 USDC,
flat, paid by the buyer; a transfer takes more checking). The entry:

    {"tx_hash", "wallet_address", "amount_usdc", "kind": "update"|"transfer", "at"}

This tool reads the transfer from Base with the verifier's own routine
(sender, payee, amount, finality) and appends the entry. No key is involved.

Then post the change as an update request: `python scripts/post_update.py <slot_id>`
(the slot itself and the generated state are never part of your pull request).
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger_core as core  # noqa: E402
import verifier  # noqa: E402

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def main(argv: list, rpc=None, base_dir: str = BASE_DIR) -> int:
    if "--price" in argv:
        print(f"update fee   {core.UPDATE_PRICE_USDC:.2f} USDC flat for every block ({core.UPDATE_PRICE_RATIONALE})")
        print(f"transfer fee {core.TRANSFER_PRICE_USDC:.2f} USDC flat for every block, paid by the buyer "
              f"instead of the update fee ({core.TRANSFER_PRICE_RATIONALE})")
        return 0
    positional = [a for i, a in enumerate(argv)
                  if not a.startswith("--") and (i == 0 or argv[i - 1] not in ("--kind", "--at"))]
    if len(positional) < 2:
        print(__doc__)
        return 2
    path, tx = positional[0], positional[1].lower()
    kind = argv[argv.index("--kind") + 1] if "--kind" in argv else "update"
    if kind not in ("update", "transfer"):
        print("[✗] --kind must be update or transfer", file=sys.stderr)
        return 2
    d = core.load_json_strict(path)
    slot_id = str(d.get("slot_id", ""))
    wallet = str(d.get("wallet_address", ""))
    price = core.fee_price(kind)
    for i, item in enumerate(d.get("update_settlements") or []):
        if str(item.get("tx_hash", "")).lower() == tx:
            print(f"[=] {tx[:12]}… is already update_settlements[{i}]")
            return 0
    try:
        r = verifier.verify_base_tx(tx, wallet, verifier.OFFICIAL_TREASURY_ADDRESS, price,
                                    rpc=rpc or core.rpc_call)
    except ValueError as e:
        print(f"[✗] {e}")
        return 1
    at = argv[argv.index("--at") + 1] if "--at" in argv else \
        _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    entry = {"tx_hash": tx, "wallet_address": wallet, "amount_usdc": float(r["amount_usdc"]),
             "kind": kind, "at": at}
    print("[*] entry   : " + json.dumps(entry))
    print(f"[*] covers  : {price:.2f} USDC {kind} fee for {slot_id}")
    if "--write" in argv:
        d.setdefault("update_settlements", []).append(entry)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=2)
            fh.write("\n")
        print(f"[✓] appended to update_settlements in {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
