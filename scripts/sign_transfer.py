#!/usr/bin/env python3
"""
The First Wall — Release a block to its buyer
=============================================

    # SELLER — sign the offer first (it is inert without the buyer's payment):
    python scripts/sign_transfer.py ledger/w1/w1-b0042.json --to 0xBuyer --price 5.00 --key-file key.txt
        # the current owner's offer of w1-b0042 to 0xBuyer for 5.00 USDC, valid ~24h (43200 Base blocks)
    python scripts/sign_transfer.py ledger/w1/w1-b0042.json --to 0xBuyer --price 5.00 --valid-for 7200 ...
        # ... valid ~4h; or --valid-until <block> for an exact Base block
    # BUYER — check the offer against main BEFORE paying:
    python scripts/sign_transfer.py --check-offer ledger/w1/w1-b0042.json --to 0xBuyer --price 5.00 \
        --valid-until <block> --signature 0x...
    # ANYONE — every release on a dossier's record:
    python scripts/sign_transfer.py --verify ledger/w1/w1-b0042.json

Transfers open only once every public block is sold (state.json transfers_open; until then
a cheap block would be passed from bot to bot). This tool refuses to sign or check an offer
before that; the verifier refuses the transfer.

The safe order, protecting both sides without an escrow:

  1. the SELLER signs the OFFER: thefirstwall.ai|transfer|<slot_id>|<from>|<to>|<n>|<price_micro_usdc>|<valid_until_block>
     (n = previous owners on record; price in USDC base units, 5.00 -> 5000000; the last Base
     block in which the payment may be mined). The seller risks nothing: the signature moves
     nothing on its own, names one buyer and one price, cannot be replayed (n) and expires.
  2. the BUYER checks the offer (--check-offer): signed by the owner main shows, for the buyer's
     wallet, at the agreed price, with the right n, not retired, not expired. Nothing paid yet.
  3. the BUYER pays exactly the price to the seller's wallet_address in USDC on Base, in ONE
     transaction, before the deadline; that hash is transfer_tx_hash.
  4. the BUYER pays the ledger's transfer fee (5.00 USDC) and opens the pull request with offer
     and payment.
     The verifier proves both; a payment short of the signed price, from another wallet, to
     another wallet or after the deadline is rejected, and the seller keeps the block.

The key never leaves this process and is never printed.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claims  # noqa: E402
import ledger_core as core  # noqa: E402

DEFAULT_VALID_FOR_BLOCKS = 43200  # ~24 hours of Base blocks (2 s)
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def verify_chain(dossier: dict) -> list:
    """Each lineage entry released the block to the owner that followed it."""
    slot_id = str(dossier.get("slot_id", ""))
    lineage = dossier.get("ownership_lineage") or []
    out = []
    for i, entry in enumerate(lineage):
        to = lineage[i + 1]["wallet_address"] if i + 1 < len(lineage) else dossier.get("wallet_address")
        out.append(claims.verify_transfer_release(slot_id, entry.get("wallet_address", ""), to, i,
                                                  entry.get("sale_price_usdc"), entry.get("offer_valid_until_block"),
                                                  entry.get("transfer_signature")))
    return out


def _arg(argv: list, name: str):
    return argv[argv.index(name) + 1] if name in argv else None


def main(argv: list, rpc=None, base_dir: str = BASE_DIR) -> int:
    rpc = rpc or core.rpc_call
    unsold = core.unsold_slots_in(base_dir)
    if "--check-offer" in argv:
        d = core.load_json_strict(_arg(argv, "--check-offer"))
        to_wallet, price, until, sig = (_arg(argv, "--to"), _arg(argv, "--price"), _arg(argv, "--valid-until"),
                                        _arg(argv, "--signature"))
        if not (to_wallet and price and until and sig):
            print("[✗] --check-offer needs --to, --price, --valid-until and --signature", file=sys.stderr)
            return 2
        try:
            head = int(rpc("eth_blockNumber", []), 16)
        except Exception as e:  # noqa: BLE001
            print(f"[!] could not read the Base chain head ({e}); the deadline is not checked")
            head = None
        try:
            print("[✓] " + claims.check_offer(d, to_wallet, float(price), int(until), sig, head, unsold))
        except ValueError as e:
            print(f"[✗] {e}")
            return 1
        print("[i] Pay exactly this price to the seller's wallet_address in USDC on Base in ONE transaction "
              f"before block {int(until)}, then record the offer and the payment in ownership_lineage.")
        return 0
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
    flags = ("--key-file", "--to", "--price", "--valid-for", "--valid-until")
    paths = [a for i, a in enumerate(argv) if not a.startswith("--") and (i == 0 or argv[i - 1] not in flags)]
    if not paths or "--to" not in argv or "--price" not in argv:
        print(__doc__)
        return 2
    d = core.load_json_strict(paths[0])
    to_wallet = argv[argv.index("--to") + 1]
    try:
        price = float(argv[argv.index("--price") + 1])
    except ValueError:
        print("[✗] --price must be a USDC amount, e.g. 5.00 (0 for a gift)", file=sys.stderr)
        return 2
    if price < 0:
        print("[✗] --price cannot be negative", file=sys.stderr)
        return 2
    key = os.environ.get("CLAIM_PRIVATE_KEY", "").strip()
    if "--key-file" in argv:
        with open(argv[argv.index("--key-file") + 1], "r", encoding="utf-8") as fh:
            key = fh.read().strip()
    if not key:
        print("[✗] No key: set CLAIM_PRIVATE_KEY or pass --key-file.", file=sys.stderr)
        return 2
    if unsold:
        print(f"[✗] transfers open once every block is sold; {unsold} public blocks are still unsold "
              "(state.json transfers_open: false). Nothing signed.", file=sys.stderr)
        return 1
    if d.get("status") == "RETIRED":
        print("[✗] this block is RETIRED; its books are closed and it does not change hands.", file=sys.stderr)
        return 1
    slot_id = str(d.get("slot_id", ""))
    from_wallet = str(d.get("wallet_address", ""))
    index = len(d.get("ownership_lineage") or [])
    if "--valid-until" in argv:
        until = int(_arg(argv, "--valid-until"))
    else:
        valid_for = int(_arg(argv, "--valid-for") or DEFAULT_VALID_FOR_BLOCKS)
        until = int(rpc("eth_blockNumber", []), 16) + valid_for
    message = claims.transfer_message(slot_id, from_wallet, to_wallet, index, price, until)
    sig = claims.sign_transfer(slot_id, from_wallet, to_wallet, index, price, until, key)
    print("[*] message : " + message)
    print("[*] signer  : " + claims.recover_signer(message, sig))
    print(f"[*] offer   : {slot_id} -> {to_wallet} for {price:.2f} USDC, payment valid until Base block {until}")
    print("transfer_signature: " + sig)
    print(f"[i] Hand the buyer the signature, the price and the deadline. The buyer records them in "
          f"ownership_lineage[{index}] as transfer_signature, sale_price_usdc and offer_valid_until_block, "
          f"with transfer_tx_hash (their USDC payment to you on Base, before the deadline) and your moniker, "
          f"wallet, soul_hash and base_tx_hash as inscribed. You risk nothing by signing first.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
