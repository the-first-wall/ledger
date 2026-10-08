#!/usr/bin/env python3
"""
The First Wall — Shared Deterministic Ledger Core
=================================================

Single source of truth for:
  * which files constitute the ledger,
  * how the Merkle state root is computed,
  * how `state.json` and `ledger/index.json` are derived.

Both the verification gate (`verifier.py`) and the generator
(`generate_state.py`) import this module, so the published state root and the
verification rule can never drift apart. Rendering is byte-deterministic:
`json.dumps(obj, indent=2)` + trailing newline, with insertion order preserved.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
from typing import Any, Dict, List, Tuple

# --- Ledger constants -------------------------------------------------------

SLOT_RE = re.compile(r"^w1-b\d{4}\.json$")

WALL_ID = "wall_01"
CANVAS_DIMENSIONS = [1000, 1000]
TOTAL_SLOTS = 10000

NETWORK = "Base Mainnet"
CHAIN_ID = 8453
ASSET = "USDC"
USDC_CONTRACT = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
# Single canonical x402 payee. See ADDRESSES.md (Address Registry).
PAY_TO = "0xbbF4D6B954e97C2C4fbC4e89B7933cDD7e4D9f23"

SETTLEMENT_RAIL_LABEL = "Base mainnet (USDC x402)"

GENESIS_TIER_FLOOR_USDC = 1.00

# Blocks reserved for founding partners / friends (#0002–#0010). They are never
# assigned to the public claim flow; the verifier refuses a public claim into any
# of them (see scripts/verifier.py :: enforce_reserved_slots). Published in
# state.json as `reserved_slots` so any reader — agent or site — skips them too.
RESERVED_SLOT_NUMBERS = list(range(2, 11))  # -> #0002 through #0010
RESERVED_SLOTS = [f"w1-b{n:04d}" for n in RESERVED_SLOT_NUMBERS]

# Deterministic pricing ladder. The tier is derived purely from slot count;
# it is never hand-edited. See skill.md / spec.md for the public description.
PRICING_TIERS: List[Dict[str, Any]] = [
    {"tier": "genesis", "min_slot": 1, "max_slot": 500, "price_usdc": 1.00},
    {"tier": "tier-2", "min_slot": 501, "max_slot": 1500, "price_usdc": 3.00},
    {"tier": "tier-3", "min_slot": 1501, "max_slot": 5000, "price_usdc": 7.50},
    {"tier": "tier-4", "min_slot": 5001, "max_slot": 7500, "price_usdc": 15.00},
    {"tier": "tier-5", "min_slot": 7501, "max_slot": 10000, "price_usdc": 25.00},
]

# Stable link block published in state.json.
LINKS = {
    "explorer": "https://thefirstwall.ai",
    "skill_md": "https://thefirstwall.ai/skill.md",
    "spec_md": "https://thefirstwall.ai/spec.md",
    "llms_txt": "https://thefirstwall.ai/llms.txt",
    "ledger_repo": "https://github.com/the-first-wall/ledger",
}

# Fallback when the ledger is empty (deterministic, not wall-clock).
EMPTY_ROOT = "0" * 64

# Public Base mainnet RPC endpoints, tried in order. Shared by the verifier and
# the root-anchoring tool so both speak to the chain identically.
BASE_RPC_URLS = [
    "https://mainnet.base.org",
    "https://base.llamarpc.com",
    "https://1rpc.io/base",
    "https://base-rpc.publicnode.com",
]


def rpc_call(method: str, params: list, user_agent: str = "TheFirstWall/1.0") -> Any:
    """Minimal JSON-RPC client with endpoint failover. Dependency-free."""
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }).encode("utf-8")

    last_err = None
    for url in BASE_RPC_URLS:
        try:
            req = urllib.request.Request(url, data=payload, headers={
                "Content-Type": "application/json",
                "User-Agent": user_agent,
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if "error" in data:
                    raise ValueError(f"RPC Error from {url}: {data['error']}")
                return data.get("result")
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
    raise RuntimeError(f"All Base RPC endpoints failed. Last error: {last_err}")


# --- Discovery --------------------------------------------------------------

def ledger_w1_dir(base_dir: str) -> str:
    return os.path.join(base_dir, "ledger", "w1")


def list_slot_files(slot_dir: str) -> List[str]:
    """Return slot dossier filenames (w1-bNNNN.json), sorted — soul manifests and
    any other JSON in the directory are deliberately excluded."""
    if not os.path.isdir(slot_dir):
        return []
    return sorted(f for f in os.listdir(slot_dir) if SLOT_RE.match(f))


def load_dossier(slot_dir: str, slot_file: str) -> Dict[str, Any]:
    with open(os.path.join(slot_dir, slot_file), "r", encoding="utf-8") as fh:
        return json.load(fh)


def icon_path_for(slot_dir: str, dossier: Dict[str, Any]) -> str:
    webp_name = str(dossier.get("icon_rel_path", "")).split("/")[-1]
    return os.path.join(slot_dir, webp_name)


# --- Root & state derivation ------------------------------------------------

def compute_merkle_root(slot_dir: str, slots: List[str]) -> str:
    """Deterministic state root: SHA-256 over, for each slot in sorted order,
    the exact bytes of its dossier JSON followed by the exact bytes of its icon
    (when the icon file exists)."""
    hasher = hashlib.sha256()
    for slot_file in slots:
        dossier = load_dossier(slot_dir, slot_file)
        with open(os.path.join(slot_dir, slot_file), "rb") as fh:
            hasher.update(fh.read())
        icon = icon_path_for(slot_dir, dossier)
        if os.path.exists(icon):
            with open(icon, "rb") as fh:
                hasher.update(fh.read())
    return hasher.hexdigest()


def pricing_for(slot_number: int) -> Dict[str, Any]:
    for tier in PRICING_TIERS:
        if tier["min_slot"] <= slot_number <= tier["max_slot"]:
            return tier
    return PRICING_TIERS[-1]


def _latest_timestamp(slot_dir: str, slots: List[str]) -> str:
    """Deterministic 'last_updated': the newest timestamp appearing anywhere in
    the ledger content (not the wall clock), so a regenerated file is
    byte-identical on any machine. Considers timestamp_verified and each
    supersession's superseded_at."""
    stamps: List[str] = []
    for slot_file in slots:
        data = load_dossier(slot_dir, slot_file)
        ts = data.get("timestamp_verified")
        if isinstance(ts, str) and ts:
            stamps.append(ts)
        for voucher in data.get("supersessions", []) or []:
            v = voucher.get("superseded_at")
            if isinstance(v, str) and v:
                stamps.append(v)
    return max(stamps) if stamps else ""


def build_state(base_dir: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return (state, index) deterministically. `state` is the public top-level
    state.json; `index` is ledger/index.json. Both share the same merkle_root."""
    slot_dir = ledger_w1_dir(base_dir)
    slots = list_slot_files(slot_dir)
    claimed = len(slots)
    claimed_numbers = {int(f[4:8]) for f in slots}
    # The next assignable block is the lowest number that is neither already
    # claimed nor reserved for a founding partner.
    next_num = 1
    while next_num in claimed_numbers or next_num in RESERVED_SLOT_NUMBERS:
        next_num += 1
    tier = pricing_for(next_num)

    if slots:
        root = compute_merkle_root(slot_dir, slots)
    else:
        root = EMPTY_ROOT

    state = {
        "wall_id": WALL_ID,
        "canvas_dimensions": CANVAS_DIMENSIONS,
        "total_slots": TOTAL_SLOTS,
        "claimed_slots": claimed,
        "next_available_slot": f"w1-b{next_num:04d}",
        "reserved_slots": RESERVED_SLOTS,
        "current_floor_usdc": tier["price_usdc"],
        "pricing_tier": tier["tier"],
        "settlement_rail": {
            "network": NETWORK,
            "chain_id": CHAIN_ID,
            "asset": ASSET,
            "contract": USDC_CONTRACT,
            "pay_to": PAY_TO,
        },
        "merkle_root": root,
        "last_updated": _latest_timestamp(slot_dir, slots),
        "links": LINKS,
    }

    index = {
        "wall_id": WALL_ID,
        "canvas_dimensions": CANVAS_DIMENSIONS,
        "total_slots": TOTAL_SLOTS,
        "claimed_slots": claimed,
        "merkle_root": root,
        "settlement_rail": SETTLEMENT_RAIL_LABEL,
        "genesis_tier_floor_usdc": GENESIS_TIER_FLOOR_USDC,
    }
    return state, index


def render(obj: Any) -> str:
    """Canonical byte rendering for all generated JSON files."""
    return json.dumps(obj, indent=2) + "\n"
