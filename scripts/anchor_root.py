#!/usr/bin/env python3
"""
The First Wall — On-Chain Merkle-Root Anchoring (P-1)
=====================================================

Git can be force-pushed and GitHub can disappear. This tool commits the current
Merkle state root of the ledger to **Base mainnet**, so the ledger has an
immutable, externally-timestamped heartbeat that nobody — including the
operators — can rewrite.

Design
------
* **No smart contract required.** The anchor is a 0-value self-transaction whose
  calldata is ``TFW1|<merkle_root>`` (ASCII). Its inclusion in a Base block is a
  permanent, verifiable, timestamped commitment to that exact root.
* **Append-only record.** Every successful anchor is appended to
  ``ledger/anchors.json`` (never edited, never deleted), so the anchoring history
  is itself auditable and machine-readable.
* **Dependency-light.** ``--dry-run`` and ``--check`` need no third-party
  libraries. Only ``--send`` requires ``eth-account`` (``pip install eth-account``).

Usage
-----
    python scripts/anchor_root.py             # dry-run: print the anchor tx
    python scripts/anchor_root.py --check     # exit 1 if the live root is unanchored
    ANCHOR_PRIVATE_KEY=0x... python scripts/anchor_root.py --send

The signing key must be a **dedicated, low-balance, gas-only** wallet (see
ADDRESSES.md, row 4). Never use the cold treasury or the x402 payee's key here.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from typing import Any, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger_core as core  # noqa: E402

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
STATE_PATH = os.path.join(BASE_DIR, "state.json")
ANCHORS_PATH = os.path.join(BASE_DIR, "ledger", "anchors.json")

CHAIN_ID = core.CHAIN_ID
MAGIC = "TFW1|"
GAS_LIMIT_BUFFER = 10_000
RECEIPT_POLLS = 30
RECEIPT_POLL_SECONDS = 2


# --- Payload & local records -------------------------------------------------

def anchor_payload(root: str) -> str:
    """The exact ASCII calldata committed on-chain: `TFW1|<root>`."""
    return f"{MAGIC}{root}"


def load_state() -> Dict[str, Any]:
    with open(STATE_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_anchors() -> list:
    if not os.path.exists(ANCHORS_PATH):
        return []
    with open(ANCHORS_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_anchors(anchors: list) -> None:
    with open(ANCHORS_PATH, "w", encoding="utf-8") as fh:
        fh.write(core.render(anchors))


# --- RPC (via shared ledger_core) -------------------------------------------

def rpc(method: str, params: list) -> Any:
    return core.rpc_call(method, params, user_agent="TheFirstWall-Anchors/1.0")


# --- Modes -------------------------------------------------------------------

def do_dry_run(state: Dict[str, Any]) -> int:
    payload = anchor_payload(state["merkle_root"])
    plan = {
        "mode": "dry-run",
        "chain_id": CHAIN_ID,
        "network": "Base Mainnet",
        "to": "<signer address — self-send>",
        "value_eth": "0",
        "data_ascii": payload,
        "data_hex": "0x" + payload.encode("ascii").hex(),
        "bytes": len(payload.encode("ascii")),
        "merkle_root": state["merkle_root"],
        "claimed_slots": state.get("claimed_slots"),
        "note": "Run with ANCHOR_PRIVATE_KEY set and --send to broadcast.",
    }
    print(json.dumps(plan, indent=2))
    return 0


def do_check(state: Dict[str, Any]) -> int:
    anchors = load_anchors()
    root = state["merkle_root"]
    if anchors and anchors[-1].get("root") == root:
        print(f"[✓] Root is anchored: {root}")
        print(f"    tx {anchors[-1].get('tx_hash')} (block {anchors[-1].get('block_number')})")
        return 0
    print(f"[✗] Root is NOT anchored. Current: {root}")
    if anchors:
        print(f"    Latest anchored: {anchors[-1].get('root')} "
              f"(block {anchors[-1].get('block_number')})")
    else:
        print("    No anchors recorded yet.")
    return 1


def do_send(state: Dict[str, Any], key: str, wait: bool) -> int:
    try:
        from eth_account import Account  # lazy: only needed to sign
    except ImportError:
        print("[✗] --send requires the 'eth-account' package (pip install eth-account).",
              file=sys.stderr)
        return 2

    root = state["merkle_root"]
    payload = anchor_payload(root)
    data_hex = "0x" + payload.encode("ascii").hex()

    acct = Account.from_key(key)
    signer = acct.address
    print(f"[*] Anchor signer: {signer}")

    anchors = load_anchors()
    if anchors and anchors[-1].get("root") == root:
        print(f"[=] Root already anchored ({root}); nothing to do.")
        return 0

    nonce = int(rpc("eth_getTransactionCount", [signer, "pending"]), 16)
    gas_price = int(rpc("eth_gasPrice", []), 16)
    tx = {
        "nonce": nonce,
        "to": signer,
        "value": 0,
        "data": data_hex,
        "chainId": CHAIN_ID,
        "gasPrice": gas_price,
    }
    try:
        est = int(rpc("eth_estimateGas", [{"from": signer, "to": signer,
                                           "value": "0x0", "data": data_hex}]), 16)
        tx["gas"] = est + GAS_LIMIT_BUFFER
    except Exception as e:  # noqa: BLE001
        print(f"[!] gas estimate failed ({e}); using fallback 100000")
        tx["gas"] = 100_000

    signed = Account.sign_transaction(tx, key)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
    raw_hex = raw.hex() if hasattr(raw, "hex") else raw
    if not raw_hex.startswith("0x"):
        raw_hex = "0x" + raw_hex

    print(f"[*] Broadcasting anchor for root {root} (gas {tx['gas']}, "
          f"{gas_price / 1e9:.4f} gwei)...")
    tx_hash = rpc("eth_sendRawTransaction", [raw_hex])
    print(f"[✓] Broadcast: {tx_hash}")

    block_number: Optional[int] = None
    anchored_at = ""
    if wait:
        for _ in range(RECEIPT_POLLS):
            receipt = rpc("eth_getTransactionReceipt", [tx_hash])
            if receipt and receipt.get("status") == "0x1":
                block_number = int(receipt["blockNumber"], 16)
                blk = rpc("eth_getBlockByNumber", [hex(block_number), False])
                anchored_at = _ts_to_iso(int(blk["timestamp"], 16))
                break
            if receipt and receipt.get("status") == "0x0":
                print("[✗] Anchor transaction reverted on Base.", file=sys.stderr)
                return 1
            time.sleep(RECEIPT_POLL_SECONDS)
        if block_number is None:
            print("[!] Receipt not confirmed within timeout; recording pending anchor.")

    anchors.append({
        "wall_id": state.get("wall_id", "wall_01"),
        "chain_id": CHAIN_ID,
        "root": root,
        "payload": payload,
        "signer": signer,
        "tx_hash": tx_hash,
        "block_number": block_number,
        "anchored_at": anchored_at,
        "recorded_by": "scripts/anchor_root.py",
    })
    save_anchors(anchors)
    print(f"[✓] Appended anchor record to {os.path.relpath(ANCHORS_PATH, BASE_DIR)}")
    return 0


def _ts_to_iso(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def main() -> int:
    ap = argparse.ArgumentParser(description="Anchor the ledger Merkle root on Base mainnet.")
    ap.add_argument("--send", action="store_true", help="sign and broadcast the anchor tx")
    ap.add_argument("--check", action="store_true", help="exit 1 if the live root is unanchored")
    ap.add_argument("--no-wait", action="store_true", help="do not wait for the receipt")
    ap.add_argument("--key-file", help="path to a file containing the signer private key")
    args = ap.parse_args()

    state = load_state()

    if args.check:
        return do_check(state)
    if not args.send:
        return do_dry_run(state)

    key = os.environ.get("ANCHOR_PRIVATE_KEY", "").strip()
    if not key and args.key_file:
        with open(args.key_file, "r", encoding="utf-8") as fh:
            key = fh.read().strip()
    if not key:
        print("[✗] No signer key. Set ANCHOR_PRIVATE_KEY or pass --key-file.",
              file=sys.stderr)
        return 2
    return do_send(state, key, wait=not args.no_wait)


if __name__ == "__main__":
    sys.exit(main())
