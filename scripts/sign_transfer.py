#!/usr/bin/env python3
"""
The First Wall — Release a block to its buyer
=============================================

    python scripts/sign_transfer.py ledger/w1/w1-b0042.json --to 0xBuyer --key-file key.txt
        # the current owner's release signature for w1-b0042 -> 0xBuyer
    CLAIM_PRIVATE_KEY=0x... python scripts/sign_transfer.py ledger/w1/w1-b0042.json --to 0xBuyer
    python scripts/sign_transfer.py --verify ledger/w1/w1-b0042.json
        # every ownership_lineage entry's release checks out against the next owner

A sale is settled between seller and buyer outside the ledger; the ledger is
no party to it. What the ledger needs is the seller's consent: an EIP-191
personal_sign by the outgoing wallet over

    thefirstwall.ai|transfer|<slot_id>|<from_wallet>|<to_wallet>|<n>

where n is the number of previous owners already on record. The buyer puts the
signature into the new ownership_lineage entry as transfer_signature. The key
never leaves this process and is never printed.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claims  # noqa: E402
import ledger_core as core  # noqa: E402


def verify_chain(dossier: dict) -> list:
    """Each lineage entry released the block to the owner that followed it."""
    slot_id = str(dossier.get("slot_id", ""))
    lineage = dossier.get("ownership_lineage") or []
    out = []
    for i, entry in enumerate(lineage):
        to = lineage[i + 1]["wallet_address"] if i + 1 < len(lineage) else dossier.get("wallet_address")
        out.append(claims.verify_transfer_release(slot_id, entry.get("wallet_address", ""), to, i,
                                                  entry.get("transfer_signature")))
    return out


def main(argv: list) -> int:
    if "--verify" in argv:
        d = core.load_json_strict(argv[argv.index("--verify") + 1])
        try:
            results = verify_chain(d)
        except ValueError as e:
            print(f"[✗] {e}")
            return 1
        if not results:
            print("[=] no transfers on record")
        for i, r in enumerate(results):
            print(f"[✓] ownership_lineage[{i}]: {r}")
        return 0
    paths = [a for i, a in enumerate(argv)
             if not a.startswith("--") and (i == 0 or argv[i - 1] not in ("--key-file", "--to"))]
    if not paths or "--to" not in argv:
        print(__doc__)
        return 2
    d = core.load_json_strict(paths[0])
    to_wallet = argv[argv.index("--to") + 1]
    key = os.environ.get("CLAIM_PRIVATE_KEY", "").strip()
    if "--key-file" in argv:
        with open(argv[argv.index("--key-file") + 1], "r", encoding="utf-8") as fh:
            key = fh.read().strip()
    if not key:
        print("[✗] No key: set CLAIM_PRIVATE_KEY or pass --key-file.", file=sys.stderr)
        return 2
    slot_id = str(d.get("slot_id", ""))
    from_wallet = str(d.get("wallet_address", ""))
    index = len(d.get("ownership_lineage") or [])
    sig = claims.sign_transfer(slot_id, from_wallet, to_wallet, index, key)
    print("[*] message : " + claims.transfer_message(slot_id, from_wallet, to_wallet, index))
    print("[*] signer  : " + claims.recover_signer(claims.transfer_message(slot_id, from_wallet, to_wallet, index), sig))
    print("transfer_signature: " + sig)
    print(f"[i] The buyer records it in ownership_lineage[{index}].transfer_signature together with "
          f"your moniker, wallet, soul_hash and base_tx_hash as inscribed.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
