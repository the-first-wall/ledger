#!/usr/bin/env python3
"""
The First Wall — Record an update fee
=====================================

    python scripts/record_update.py ledger/w1/w1-b0042.json 0x<tx_hash> --write
    python scripts/record_update.py ledger/w1/w1-b0042.json 0x<tx_hash> --kind transfer --write
    python scripts/record_update.py --price          # what an update costs right now (any block)

Every change of an inscribed block by its owner — a transfer to a new owner
included — settles one USDC transfer from the owner's wallet (the buyer's, on
a transfer) to pay_to of at least the update price of the tier the WALL is in
(the same for every block, whatever its number), and appends exactly one
entry to update_settlements[] naming that tier:

    {"tx_hash", "wallet_address", "amount_usdc", "kind": "update"|"transfer", "tier", "at"}

This tool reads the transfer from Base with the verifier's own routine
(sender, payee, amount, finality) and appends the entry. No key is involved.
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
    wall = core.current_tier(base_dir)
    if "--price" in argv:
        print(f"the wall is in the {wall['tier']} tier: update price {wall['update_price_usdc']:.2f} USDC "
              f"for every block (claim floor {wall['price_usdc']:.2f} USDC)")
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
    price = float(wall["update_price_usdc"])
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
             "kind": kind, "tier": wall["tier"], "at": at}
    print("[*] entry   : " + json.dumps(entry))
    print(f"[*] covers  : {price:.2f} USDC {kind} fee for {slot_id} (wall in the {wall['tier']} tier)")
    if "--write" in argv:
        d.setdefault("update_settlements", []).append(entry)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=2)
            fh.write("\n")
        print(f"[✓] appended to update_settlements in {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
