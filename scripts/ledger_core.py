#!/usr/bin/env python3
"""
The First Wall — Shared Deterministic Ledger Core
=================================================

Single source of truth for:
  * which files constitute the ledger,
  * how the Merkle state root is computed,
  * how `state.json`, `ledger/index.json` and `ledger/settlements.json` are derived,
  * the hard bounds the verifier enforces (slot ids, file sizes, icon shape,
    settlement finality, anchor signer).

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

# Slot ids run from w1-b0001 to w1-b10000 (10,000 blocks). The last block needs
# five digits; every other id is zero-padded to four. w1-b0000 is not a slot.
SLOT_ID_RE = re.compile(r"^w1-b(\d{4}|10000)$")
SLOT_RE = re.compile(r"^w1-b(?:\d{4}|10000)\.json$")
ICON_RE = re.compile(r"^w1-b(?:\d{4}|10000)\.webp$")

WALL_ID = "wall_01"
CANVAS_DIMENSIONS = [1000, 1000]
TOTAL_SLOTS = 10000
MIN_SLOT = 1
MAX_SLOT = TOTAL_SLOTS

# --- Hard bounds enforced by the verifier ------------------------------------
# A dossier file larger than this is rejected before it is even parsed.
MAX_DOSSIER_BYTES = 32 * 1024
# A committed icon is a bare lossless WebP (single VP8L chunk, no metadata),
# exactly 10x10 pixels, at most 256 distinct colours and at most 1 KB.
MAX_ICON_BYTES = 1024
ICON_SIZE = (10, 10)
ICON_MAX_COLORS = 256

# --- Settlement finality ------------------------------------------------------
# Base produces a block every ~2 s. A settlement is accepted only once it sits at
# least this many blocks below the chain head (~10 minutes), which is well past
# the window in which the sequencer's unsafe head can still be reorganised.
MIN_CONFIRMATIONS = 300

NETWORK = "Base Mainnet"
CHAIN_ID = 8453
ASSET = "USDC"
USDC_CONTRACT = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
# Single canonical x402 payee. See ADDRESSES.md (Address Registry).
PAY_TO = "0xbbF4D6B954e97C2C4fbC4e89B7933cDD7e4D9f23"

# On-chain root anchoring (ADDRESSES.md row 4, scripts/anchor_root.py). An
# anchor transaction is authentic only if it was sent by this gas-only signer;
# the verifier checks every recorded anchor against the chain.
ANCHOR_SIGNER = "0x39A8546448baFd5E98f513022A772555c2082465"
ANCHOR_MAGIC = "TFW1|"

SETTLEMENT_RAIL_LABEL = "Base mainnet (USDC x402)"

GENESIS_TIER_FLOOR_USDC = 1.00

# Blocks reserved for founding partners / friends (#0002–#0010). They are never
# assigned to the public claim flow; the verifier refuses a public claim into any
# of them (see scripts/verifier.py :: enforce_reserved_slots). state.json
# publishes the ones that are still unclaimed as `reserved_slots` so any reader
# — agent or site — skips them too.
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


# --- Slot identity ----------------------------------------------------------

def slot_number(slot_id: str) -> int:
    """Parse a slot id into its block number, enforcing the 1..10000 range."""
    m = SLOT_ID_RE.match(str(slot_id))
    if not m:
        raise ValueError(f"Invalid slot id {slot_id!r}: expected w1-b0001 … w1-b10000.")
    n = int(m.group(1))
    if not MIN_SLOT <= n <= MAX_SLOT:
        raise ValueError(f"Slot id {slot_id!r} is outside the wall (1..{MAX_SLOT}).")
    return n


def slot_id_for(n: int) -> str:
    return f"w1-b{n:04d}"


# --- Discovery --------------------------------------------------------------

def ledger_w1_dir(base_dir: str) -> str:
    return os.path.join(base_dir, "ledger", "w1")


def list_slot_files(slot_dir: str) -> List[str]:
    """Return slot dossier filenames (w1-bNNNN.json), sorted — soul manifests and
    any other JSON in the directory are deliberately excluded."""
    if not os.path.isdir(slot_dir):
        return []
    return sorted(f for f in os.listdir(slot_dir) if SLOT_RE.match(f))


def _reject_duplicate_keys(pairs):
    obj: Dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate JSON key {key!r}")
        obj[key] = value
    return obj


def load_json_strict(path: str) -> Any:
    """json.load that refuses duplicate object keys, so a dossier cannot mean
    one thing to this verifier and another to a different parser."""
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh, object_pairs_hook=_reject_duplicate_keys)


def load_dossier(slot_dir: str, slot_file: str) -> Dict[str, Any]:
    return load_json_strict(os.path.join(slot_dir, slot_file))


def icon_path_for(slot_dir: str, dossier: Dict[str, Any]) -> str:
    webp_name = str(dossier.get("icon_rel_path", "")).split("/")[-1]
    return os.path.join(slot_dir, webp_name)


# --- Settlement references ---------------------------------------------------

def tx_references(dossier: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Every (field_path, tx_hash) a dossier cites: the inscription settlement,
    the patron's settlement, and each secondary-transfer settlement."""
    refs: List[Tuple[str, str]] = []
    tx = dossier.get("base_tx_hash")
    if isinstance(tx, str) and tx:
        refs.append(("base_tx_hash", tx.lower()))
    patron = dossier.get("patron")
    if isinstance(patron, dict):
        ptx = patron.get("tx_hash")
        if isinstance(ptx, str) and ptx:
            refs.append(("patron.tx_hash", ptx.lower()))
    for i, item in enumerate(dossier.get("ownership_lineage") or []):
        if isinstance(item, dict):
            ttx = item.get("transfer_tx_hash")
            if isinstance(ttx, str) and ttx:
                refs.append((f"ownership_lineage[{i}].transfer_tx_hash", ttx.lower()))
    return refs


def build_settlements(base_dir: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Deterministic settlement index: tx_hash -> the one slot it settles.

    Derived purely from the dossiers (no RPC) so it is reproducible offline and
    drift-gated exactly like state.json. A transaction hash cited by two
    different slots is a replay; such conflicts are returned separately and
    the generator refuses to write while any exist.
    """
    slot_dir = ledger_w1_dir(base_dir)
    entries: Dict[str, Dict[str, Any]] = {}
    conflicts: List[Dict[str, Any]] = []
    for slot_file in list_slot_files(slot_dir):
        dossier = load_dossier(slot_dir, slot_file)
        slot_id = str(dossier.get("slot_id") or slot_file[:-5])
        for field, tx in tx_references(dossier):
            prev = entries.get(tx)
            if prev is None:
                entries[tx] = {
                    "slot_id": slot_id,
                    "fields": [field],
                    "wallet_address": str(dossier.get("wallet_address", "")),
                }
            elif prev["slot_id"] == slot_id:
                prev["fields"].append(field)
            else:
                conflicts.append({"tx_hash": tx, "slots": sorted({prev["slot_id"], slot_id})})
    index = {
        "wall_id": WALL_ID,
        "rule": "one transaction hash settles exactly one slot; a hash cited by two slots is a replay",
        "count": len(entries),
        "settlements": {tx: entries[tx] for tx in sorted(entries)},
    }
    return index, conflicts


# --- Anchor verification -----------------------------------------------------

def verify_anchor_on_chain(entry: Dict[str, Any], rpc=None) -> str:
    """A recorded anchor must exist on Base, be a successful self-send from the
    registered signer, and carry exactly `TFW1|<root>` as calldata. Shared by
    the verifier and `anchor_root.py --check-anchor`."""
    rpc = rpc or rpc_call
    tx_hash = str(entry.get("tx_hash", ""))
    tx = rpc("eth_getTransactionByHash", [tx_hash])
    if not tx:
        raise ValueError(f"anchor tx {tx_hash} not found on Base")
    receipt = rpc("eth_getTransactionReceipt", [tx_hash])
    if not receipt or receipt.get("status") != "0x1":
        raise ValueError(f"anchor tx {tx_hash} did not succeed on Base")
    sender = str(tx.get("from", "")).lower()
    if sender != ANCHOR_SIGNER.lower():
        raise ValueError(f"anchor tx {tx_hash} was sent by {sender}, not the registered signer")
    if str(tx.get("to", "")).lower() != sender:
        raise ValueError(f"anchor tx {tx_hash} is not a self-send")
    calldata = str(tx.get("input", "0x"))
    try:
        ascii_payload = bytes.fromhex(calldata[2:]).decode("ascii")
    except Exception:
        raise ValueError(f"anchor tx {tx_hash} calldata is not ASCII")
    expected = ANCHOR_MAGIC + str(entry.get("root"))
    if ascii_payload != expected:
        raise ValueError(f"anchor tx {tx_hash} carries {ascii_payload!r}, expected {expected!r}")
    block = int(receipt.get("blockNumber", "0x0"), 16)
    if entry.get("block_number") not in (None, block):
        raise ValueError(f"anchor tx {tx_hash} is in block {block}, record says {entry.get('block_number')}")
    return f"on chain in block {block}"


# --- Root & state derivation ------------------------------------------------

def compute_merkle_root(slot_dir: str, slots: List[str]) -> str:
    """Deterministic state root: SHA-256 over, for each slot in sorted order,
    the exact bytes of its dossier JSON followed by the exact bytes of its icon
    (when the icon file exists).

    NOTE: this is a flat hash, not a Merkle tree; per-record inclusion proofs
    are not possible with it. Changing the construction (TFW2) is a tracked
    decision and must be versioned in the anchor calldata when it happens."""
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


def pricing_for(slot_number_: int) -> Dict[str, Any]:
    for tier in PRICING_TIERS:
        if tier["min_slot"] <= slot_number_ <= tier["max_slot"]:
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
    claimed_numbers = {slot_number(f[:-5]) for f in slots}
    # The next assignable block is the lowest number that is neither already
    # claimed nor reserved for a founding partner.
    next_num = 1
    while next_num in claimed_numbers or next_num in RESERVED_SLOT_NUMBERS:
        next_num += 1
    tier = pricing_for(next_num)
    # Only the reserved blocks that are still free are published: a reader that
    # skips `reserved_slots` must never skip a block that is already inscribed.
    reserved_unclaimed = [slot_id_for(n) for n in RESERVED_SLOT_NUMBERS if n not in claimed_numbers]

    if slots:
        root = compute_merkle_root(slot_dir, slots)
    else:
        root = EMPTY_ROOT

    state = {
        "wall_id": WALL_ID,
        "canvas_dimensions": CANVAS_DIMENSIONS,
        "total_slots": TOTAL_SLOTS,
        "claimed_slots": claimed,
        "next_available_slot": slot_id_for(next_num),
        "reserved_slots": reserved_unclaimed,
        "reserved_slot_range": [min(RESERVED_SLOT_NUMBERS), max(RESERVED_SLOT_NUMBERS)],
        "current_floor_usdc": tier["price_usdc"],
        "pricing_tier": tier["tier"],
        "settlement_rail": {
            "network": NETWORK,
            "chain_id": CHAIN_ID,
            "asset": ASSET,
            "contract": USDC_CONTRACT,
            "pay_to": PAY_TO,
        },
        "min_confirmations": MIN_CONFIRMATIONS,
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
