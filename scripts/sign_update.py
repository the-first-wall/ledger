#!/usr/bin/env python3
"""
The First Wall — Sign an update request
=======================================

    python scripts/sign_update.py ledger/w1/<slot_id>.json --key-file key.txt --write
    python scripts/sign_update.py --print-message ledger/w1/<slot_id>.json   # the exact text to sign
    python scripts/sign_update.py --verify ledger/w1/<slot_id>.json          # check the newest entry
    UPDATE_PRIVATE_KEY=0x... python scripts/sign_update.py ledger/w1/<slot_id>.json --write

The update fee (0.50 USDC, or the 5.00 USDC transfer fee) proves *payment*, not
*authorship*: a courier who watches the chain can copy an owner's request — its
fee transaction and its files — and rewrite the content while the fee still
checks out. So the owner also signs the change itself:

    thefirstwall.ai|update|v2|<slot_id>|<fee_tx>|<bundle_sha256>

where fee_tx is the newest update_settlements[] entry's transaction hash and
bundle_sha256 is the bundle hash of the block's content (canonical dossier +
exact committed icon; see spec.md). This tool computes the bundle from the
sibling .webp, records bundle_sha256 + update_signature on that newest entry, and
stores the signature. The verifier re-proves it: the newest signed entry must
match the committed bytes, so a rewritten block is rejected.

The key never leaves this process and is never printed. Any wallet that can
personal_sign produces the same signature; this tool is a convenience.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claims  # noqa: E402
import ledger_core as core  # noqa: E402

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _icon_bytes(path: str):
    icon = os.path.splitext(path)[0] + ".webp"
    if os.path.exists(icon):
        with open(icon, "rb") as fh:
            return fh.read()
    return None


def _newest_entry(d, path):
    fees = d.get("update_settlements") or []
    if not fees or not isinstance(fees[-1], dict) or not fees[-1].get("tx_hash"):
        raise ValueError(
            f"{path}: update_settlements[] has no newest entry with a tx_hash; record the fee first "
            "(python scripts/record_update.py … --write)."
        )
    return fees[-1]


def main(argv: list, base_dir: str = BASE_DIR) -> int:
    positional = [a for i, a in enumerate(argv)
                  if not a.startswith("--") and (i == 0 or argv[i - 1] != "--key-file")]
    if not positional:
        print(__doc__)
        return 2
    path = positional[0]
    d = core.load_json_strict(path)
    slot_id = str(d.get("slot_id", ""))
    if not slot_id:
        return _fail("the dossier has no slot_id: an update request changes an existing block")
    icon = _icon_bytes(path)
    if icon is None:
        return _fail(f"no icon next to {path} (expected {os.path.splitext(path)[0]}.webp); "
                     "the bundle hash needs the committed icon")
    try:
        entry = _newest_entry(d, path)
    except ValueError as e:
        return _fail(str(e))
    bundle = claims.bundle_sha256(d, icon)
    message = claims.update_message(slot_id, entry.get("tx_hash"), bundle)

    if "--verify" in argv or "--print-message" in argv:
        if "--verify" in argv:
            try:
                signer = claims.verify_update_signature(slot_id, entry.get("tx_hash"),
                                                         entry.get("bundle_sha256"), entry.get("update_signature"))
            except ValueError as e:
                print(f"[✗] {e}")
                return 1
            if str(entry.get("bundle_sha256", "")).lower() != bundle:
                print(f"[✗] the newest signed bundle_sha256 does not match the committed dossier + icon "
                      f"({bundle[:16]}…)")
                return 1
            print(f"[✓] signed by {signer}")
            return 0
        print("[*] fee_tx : " + str(entry.get("tx_hash")))
        print("[*] bundle : " + bundle)
        print("[*] message: " + message)
        return 0

    key = os.environ.get("UPDATE_PRIVATE_KEY", "").strip()
    if "--key-file" in argv:
        with open(argv[argv.index("--key-file") + 1], "r", encoding="utf-8") as fh:
            key = fh.read().strip()
    if not key:
        return _fail("No key: set UPDATE_PRIVATE_KEY or pass --key-file.")
    sig = claims.sign_update(slot_id, entry.get("tx_hash"), bundle, key)
    entry["bundle_sha256"] = bundle
    entry["update_signature"] = sig
    print("[*] message: " + message)
    print("[*] signer : " + claims.verify_update_signature(slot_id, entry.get("tx_hash"), bundle, sig))
    if "--write" in argv:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=2)
            fh.write("\n")
        print(f"[✓] bundle_sha256 + update_signature written onto the newest update_settlements entry in {path}")
    else:
        print("update_signature: " + sig)
    return 0


def _fail(message: str) -> int:
    print(f"[✗] {message}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
