#!/usr/bin/env python3
"""
The First Wall — Settlement Watcher (B-34)
==========================================

Scans Base mainnet for USDC transfers paid to the canonical x402 payee, i.e.
incoming slot settlements. Read-only: no keys, no secrets, no writes to the
ledger (it writes only its own receipt and cursor under ops/).

Every run produces a SCAN RECEIPT, so a "zero" is falsifiable: it names the
block window that was actually read, how many payee transfers the chain
returned for it, and any errors. The receipt is written to
ops/scan-receipt.json (the committed heartbeat) and, with --json, printed:

    {status, from_block, to_block, target_block, blocks_scanned, transfers_seen,
     candidates, new_settlements, errors, started_at, finished_at, state_root}

  * from_block..to_block is the window actually scanned. to_block never claims
    a block that was not read: after a failed chunk it is the last block that
    WAS read, and the cursor stops there (no gap is ever skipped).
  * candidates = every USDC Transfer to the payee in the window;
    new_settlements = candidates whose tx hash is not yet in
    ledger/settlements.json.
  * status: "no_transfers_in_window" (window read, nothing paid — also when
    the cursor is already at the chain head, with blocks_scanned 0),
    "transfers_found", or "error".

Exit codes: 0 = the scan completed (zero or more settlements); 1 = a transport
or RPC failure. A failure is recorded in `errors`, the receipt is STILL
written, and the process exits non-zero so a scheduler cannot mistake it for
"no news".

Usage:
    python ops/settlement_watch.py                 # scan new blocks, print a summary
    python ops/settlement_watch.py --json          # print the full receipt
    python ops/settlement_watch.py --lookback 5000 # first run / no cursor: how far back
    python ops/settlement_watch.py --state-file X --receipt Y
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

OPS_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.abspath(os.path.join(OPS_DIR, ".."))
sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))

import ledger_core as core  # noqa: E402

USDC = core.USDC_CONTRACT
PAY_TO = core.PAY_TO
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
DEFAULT_STATE = os.path.join(OPS_DIR, "settlement-cursor.json")
DEFAULT_RECEIPT = os.path.join(OPS_DIR, "scan-receipt.json")
DEFAULT_LOOKBACK = 5000  # ~2.7h of Base blocks (2s) when there is no cursor
# Public Base RPCs cap eth_getLogs ranges (tenderly: 1000 blocks, 1rpc: 50), so a
# stale cursor is scanned in chunks rather than one wide query.
CHUNK_BLOCKS = 900

Rpc = Callable[[str, list], Any]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hex_to_int(v: Any) -> int:
    if v is None:
        return 0
    return int(v, 16) if isinstance(v, str) else int(v)


def _load_cursor(path: str) -> Optional[int]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            v = json.load(fh).get("last_scanned_block")
        return int(v) if v is not None else None
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def _write_json(path: str, doc: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=False)
        fh.write("\n")
    os.replace(tmp, path)


def _state_root(base_dir: str) -> Optional[str]:
    try:
        with open(os.path.join(base_dir, "state.json"), "r", encoding="utf-8") as fh:
            return json.load(fh).get("merkle_root")
    except (OSError, ValueError):
        return None


def _known_tx_hashes(base_dir: str) -> set:
    try:
        with open(os.path.join(base_dir, "ledger", "settlements.json"), "r", encoding="utf-8") as fh:
            return {str(k).lower() for k in (json.load(fh).get("settlements") or {})}
    except (OSError, ValueError, AttributeError):
        return set()


def scan(rpc: Rpc, from_block: int, to_block: int, sleep: float = 0.15) -> Tuple[List[Dict[str, Any]], int, Optional[str]]:
    """USDC Transfers to the payee in [from_block, to_block], in chunks.

    Returns (transfers, scanned_through, error). scanned_through is the last block
    actually read (from_block - 1 if none); on a failed chunk it stops BEFORE that
    chunk and `error` says why, so the window is never overstated."""
    topic_to = "0x" + PAY_TO[2:].lower().rjust(64, "0")
    logs: List[Dict[str, Any]] = []
    scanned_through = from_block - 1
    error = None
    for start in range(from_block, to_block + 1, CHUNK_BLOCKS):
        end = min(start + CHUNK_BLOCKS - 1, to_block)
        try:
            chunk = rpc("eth_getLogs", [{
                "fromBlock": hex(start), "toBlock": hex(end), "address": USDC,
                "topics": [TRANSFER_TOPIC, None, topic_to],
            }]) or []
        except Exception as e:  # noqa: BLE001 — recorded, never swallowed
            error = f"eth_getLogs {start}..{end}: {e}"
            break
        logs.extend(chunk)
        scanned_through = end
        if sleep:
            time.sleep(sleep)
    out = []
    for log in logs:
        topics = [t.lower() for t in log.get("topics", [])]
        if len(topics) < 3 or topics[0] != TRANSFER_TOPIC:
            continue
        out.append({
            "tx_hash": str(log.get("transactionHash", "")).lower(),
            "from": "0x" + topics[1][-40:],
            "to": PAY_TO,
            "amount_usdc": _hex_to_int(log.get("data", "0x0")) / 1_000_000.0,
            "block": _hex_to_int(log.get("blockNumber")),
        })
    return out, scanned_through, error


def run(rpc: Rpc = core.rpc_call, *, base_dir: str = BASE_DIR, state_file: str = DEFAULT_STATE,
        receipt_file: str = DEFAULT_RECEIPT, lookback: int = DEFAULT_LOOKBACK,
        clock: Callable[[], str] = _now, sleep: float = 0.15) -> Tuple[Dict[str, Any], int]:
    """One scan. Always writes the receipt; returns (receipt, exit_code)."""
    started = clock()
    receipt: Dict[str, Any] = {
        "status": "error", "from_block": None, "to_block": None, "target_block": None, "blocks_scanned": 0,
        "transfers_seen": 0, "candidates": [], "new_settlements": [], "errors": [],
        "started_at": started, "finished_at": None, "state_root": _state_root(base_dir),
    }
    code = 0
    try:
        latest = _hex_to_int(rpc("eth_blockNumber", []))
        receipt["target_block"] = latest
        last = _load_cursor(state_file)
        if last is None:
            last = max(latest - lookback, 0)
        from_block = last + 1
        receipt["from_block"] = from_block
        if from_block > latest:
            # Already at the head: nothing new to read. Said plainly (blocks_scanned 0).
            receipt["to_block"] = latest
            receipt["status"] = "no_transfers_in_window"
        else:
            transfers, scanned_through, err = scan(rpc, from_block, latest, sleep)
            receipt["to_block"] = scanned_through
            receipt["blocks_scanned"] = max(scanned_through - from_block + 1, 0)
            receipt["transfers_seen"] = len(transfers)
            receipt["candidates"] = transfers
            known = _known_tx_hashes(base_dir)
            receipt["new_settlements"] = [t for t in transfers if t["tx_hash"] not in known]
            if err:
                receipt["errors"].append(err)
            receipt["status"] = ("error" if err else "transfers_found" if transfers else "no_transfers_in_window")
            if scanned_through >= from_block:
                _write_json(state_file, {"last_scanned_block": scanned_through})
        if receipt["errors"]:
            code = 1
    except Exception as e:  # noqa: BLE001 — a transport failure is recorded, then non-zero
        receipt["errors"].append(f"{type(e).__name__}: {e}")
        receipt["status"] = "error"
        code = 1
    receipt["finished_at"] = clock()
    _write_json(receipt_file, receipt)
    return receipt, code


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state-file", default=DEFAULT_STATE)
    ap.add_argument("--receipt", default=DEFAULT_RECEIPT)
    ap.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    receipt, code = run(state_file=args.state_file, receipt_file=args.receipt, lookback=args.lookback)
    if args.json:
        print(json.dumps(receipt, indent=2))
    else:
        print(f"[settlement-watch] {receipt['status']}: blocks {receipt['from_block']}..{receipt['to_block']} "
              f"({receipt['blocks_scanned']} scanned), transfers seen {receipt['transfers_seen']}, "
              f"new settlements {len(receipt['new_settlements'])}")
        for m in receipt["new_settlements"]:
            print(f"  • {m['amount_usdc']:.2f} USDC from {m['from']} (block {m['block']})\n"
                  f"    https://basescan.org/tx/{m['tx_hash']}")
    for e in receipt["errors"]:
        print(f"[settlement-watch] ERROR: {e}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
