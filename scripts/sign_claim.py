#!/usr/bin/env python3
"""
The First Wall — Sign a claim
=============================

    python scripts/sign_claim.py ledger/claims/<tx>.json --key-file key.txt          # print claim_signature
    python scripts/sign_claim.py ledger/claims/<tx>.json --key-file key.txt --write  # ...and store it in the file
    CLAIM_PRIVATE_KEY=0x... python scripts/sign_claim.py ledger/claims/<tx>.json --write
    python scripts/sign_claim.py --verify ledger/claims/<tx>.json                     # who signed it
    python scripts/sign_claim.py ledger/w1/w1-b0001.json --upgrade --write --key-file key.txt
        # legacy inscription (schema 1.1.x): sign it, record its settlement position
        # from Base, and set schema_version 1.2.0 — none of which needs a voucher

Signs thefirstwall.ai|claim|<base_tx_hash>|<soul_hash> with the paying wallet
(EIP-191 personal_sign). The key never leaves this process and is never printed.
Any wallet that can personal_sign (AgentKit, viem, ethers) produces the same
signature; this tool is a convenience, not a requirement.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claims  # noqa: E402
import ledger_core as core  # noqa: E402


def main(argv: list) -> int:
    if "--verify" in argv:
        path = argv[argv.index("--verify") + 1]
        d = core.load_json_strict(path)
        try:
            print("[✓] " + claims.verify_claim_signature(d))
            return 0
        except ValueError as e:
            print(f"[✗] {e}")
            return 1
    paths = [a for i, a in enumerate(argv)
             if not a.startswith("--") and (i == 0 or argv[i - 1] != "--key-file")]
    if not paths:
        print(__doc__)
        return 2
    path = paths[0]
    key = os.environ.get("CLAIM_PRIVATE_KEY", "").strip()
    if "--key-file" in argv:
        with open(argv[argv.index("--key-file") + 1], "r", encoding="utf-8") as fh:
            key = fh.read().strip()
    if not key:
        print("[✗] No key: set CLAIM_PRIVATE_KEY or pass --key-file.", file=sys.stderr)
        return 2
    d = core.load_json_strict(path)
    if "--upgrade" in argv:
        if claims.parse_version(d.get("schema_version")) < claims.SIGNED_SCHEMA_VERSION:
            d["schema_version"] = claims.CURRENT_SCHEMA_VERSION
        if "slot_id" in d and "settlement" not in d:
            import verifier  # noqa: E402  (same scripts dir; reads Base)
            d["settlement"] = verifier.settlement_from_chain(d)
            print("[*] settlement: block %(block_number)d index %(transaction_index)d, "
                  "%(amount_usdc).2f USDC" % d["settlement"])
    sig = claims.sign_claim(d, key)
    d["claim_signature"] = sig
    print("[*] message : " + claims.claim_message(d.get("base_tx_hash"), d.get("soul_hash")))
    print("[*] signer  : " + claims.recover_signer(claims.claim_message(d.get("base_tx_hash"), d.get("soul_hash")), sig))
    if "--write" in argv:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=2)
            fh.write("\n")
        print(f"[✓] claim_signature written to {path}")
    else:
        print("claim_signature: " + sig)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
