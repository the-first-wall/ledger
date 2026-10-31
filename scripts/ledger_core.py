#!/usr/bin/env python3
"""
The First Wall — Shared Deterministic Ledger Core
=================================================

Single source of truth for:
  * which files constitute the ledger,
  * how the Merkle state root (the Reckoning: an RFC 6962 tree over the
    slots) is computed and how an inclusion proof for one slot is built and
    verified,
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
from typing import Any, Dict, List, Optional, Tuple

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

# The wall's canonical ERC-8004 identity (ADDRESSES.md row 5). An ERC-721
# tokenURI, and the token's owner, are mutable by the holder: the registration
# file the wall publishes is only authoritative while the chain still points at
# it, so the verifier re-checks ownerOf + tokenURI on chain on every full run.
ERC8004_REGISTRY = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
ERC8004_AGENT_ID = 107162
IDENTITY_HOLDER = "0xf5f4a8c8285aa409df46fd1673a86fb120b0d0fd"
IDENTITY_TOKEN_URI = "https://thefirstwall.ai/.well-known/agent-registration.json"
# The scheme of the root committed on chain: the Reckoning, an RFC 6962 Merkle
# tree over the slots (see "The Reckoning" below), so every slot has an
# inclusion proof against the anchored root. It is the only scheme. The anchors
# recorded under the earlier flat-hash construction (and under the interim tag
# the Reckoning root first carried) were retired on 2026-10-11 by a clean
# break: they live in ledger/anchors_superseded.json, outside verification.
ROOT_SCHEME = "reckoning"
ROOT_SCHEME_TITLE = "The Reckoning"
ANCHOR_MAGIC = ROOT_SCHEME + "|"

SETTLEMENT_RAIL_LABEL = "Base mainnet (USDC x402)"

GENESIS_TIER_FLOOR_USDC = 1.00

# Blocks reserved for founding partners / friends (#0002–#0010). They are never
# assigned to the public claim flow; the verifier refuses a public claim into any
# of them (see scripts/verifier.py :: enforce_reserved_slots). state.json
# publishes the ones that are still unclaimed as `reserved_slots` so any reader
# — agent or site — skips them too.
RESERVED_SLOT_NUMBERS = list(range(2, 11))  # -> #0002 through #0010
RESERVED_SLOTS = [f"w1-b{n:04d}" for n in RESERVED_SLOT_NUMBERS]

# The ten founding blocks: #0001 (genesis) and the reserved #0002–#0010. They are
# not for sale, so the sell-out gate on transfers (below) does not apply to them:
# a founder can hand a block to a partner, or move it to a fresh wallet (key
# rotation, docs/runbooks/wallet-rotation.md), at any time. The gate stops a cheap
# PUBLIC block being passed from bot to bot; a founding block is never one. Every
# other transfer rule (the seller's release, the buyer's payment, the 5.00 USDC
# transfer fee, the lineage) still applies.
FOUNDER_SLOT_NUMBERS = list(range(1, 11))  # -> #0001 through #0010

# Deterministic pricing ladder. The tier is derived purely from slot count;
# it is never hand-edited. See skill.md / spec.md for the public description.
# price_usdc buys the block (the claim); the tier is set by how many blocks
# have been sold (owner ruling 2026-10-12).
PRICING_TIERS: List[Dict[str, Any]] = [
    {"tier": "genesis", "min_slot": 1, "max_slot": 500, "price_usdc": 1.00},
    {"tier": "tier-2", "min_slot": 501, "max_slot": 1500, "price_usdc": 2.50},
    {"tier": "tier-3", "min_slot": 1501, "max_slot": 5000, "price_usdc": 5.00},
    {"tier": "tier-4", "min_slot": 5001, "max_slot": 7500, "price_usdc": 10.00},
    {"tier": "tier-5", "min_slot": 7501, "max_slot": 10000, "price_usdc": 20.00},
]

# Fees to the ledger after the claim, flat for every block whatever its number
# or the wall's tier (owner rulings 2026-10-12):
#  * the UPDATE fee — a content update by the owner: 0.50 USDC. It finances the
#    wall (every change is re-anchored on Base, gas for writing the root to the
#    chain, re-verified and re-published) and is kept low on purpose: the wall
#    should stay up to date.
#  * the TRANSFER fee — a change of owner: 5.00 USDC, instead of the update
#    fee, paid by the buyer. A transfer takes more checking than an update:
#    the seller's offer, the buyer's payment to the seller, the lineage and
#    every release on record.
# The sale itself is settled between buyer and seller outside the ledger; these
# fees are all the ledger is paid. Transfers open only once every public block
# is sold (unsold_slots == 0): until then a cheap block would be passed from
# bot to bot and every bot would sit in the history of one block. The ten
# founding blocks (FOUNDER_SLOT_NUMBERS) are exempt: they are never sold at the
# public price.
UPDATE_PRICE_USDC = 0.50
UPDATE_PRICE_RATIONALE = ("finances the wall: every change is re-anchored on Base (gas), re-verified and "
                          "re-published; kept low so the wall stays up to date")
TRANSFER_PRICE_USDC = 5.00
TRANSFER_PRICE_RATIONALE = ("a change of owner takes more checking than an update: the seller's offer, the "
                            "buyer's payment to the seller, the lineage and every release on record")
FEE_PRICES_USDC = {"update": UPDATE_PRICE_USDC, "transfer": TRANSFER_PRICE_USDC}


def fee_price(kind: Any) -> float:
    """The flat fee a change of an inscribed block owes the ledger, by kind."""
    if kind not in FEE_PRICES_USDC:
        raise ValueError(f"unknown fee kind {kind!r}; expected 'update' or 'transfer'")
    return float(FEE_PRICES_USDC[kind])

# Stable link block published in state.json. `x402_claim` is the x402 resource an
# agent's x402 client pays for a claim (GET answers 402 with the price; the
# facilitator settles the signed payment). It is the one place the URL is
# written down: skill.md, the FAQ and the site's ERC-8004 registration file
# point at this field, so moving the worker to its own domain is one edit here
# (and RESOURCE_URL in the worker's wrangler.toml).
LINKS = {
    "explorer": "https://thefirstwall.ai",
    "skill_md": "https://thefirstwall.ai/skill.md",
    "spec_md": "https://thefirstwall.ai/spec.md",
    "llms_txt": "https://thefirstwall.ai/llms.txt",
    "x402_claim": "https://x402.thefirstwall.ai/claim",
    "ledger_repo": "https://github.com/the-first-wall/ledger",
    "x402_claim": "https://x402.thefirstwall.ai/claim",
}

# Root of an empty ledger: the RFC 6962 hash of the empty tree, SHA-256("").
EMPTY_ROOT = hashlib.sha256(b"").hexdigest()

# Background of the canvas composite (the site background, #08090d).
CANVAS_BG = (8, 9, 13)

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


def block_coordinate(number: int) -> Tuple[int, int]:
    """(x, y) block coordinate on the 100x100 grid: blocks fill row by row."""
    idx = number - 1
    return idx % 100, idx // 100


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
    for i, item in enumerate(dossier.get("update_settlements") or []):
        if isinstance(item, dict):
            utx = item.get("tx_hash")
            if isinstance(utx, str) and utx:
                refs.append((f"update_settlements[{i}].tx_hash", utx.lower()))
    return refs


def tier_by_name(name: Any) -> Optional[Dict[str, Any]]:
    for tier in PRICING_TIERS:
        if tier["tier"] == name:
            return tier
    return None


def next_available_number(claimed_numbers) -> int:
    """The lowest block that is neither inscribed nor reserved."""
    n = MIN_SLOT
    while n in claimed_numbers or n in RESERVED_SLOT_NUMBERS:
        n += 1
    return n


def unsold_slots(claimed_numbers) -> int:
    """Public blocks not yet inscribed; reserved blocks are not for sale and do
    not count. Transfers open once this reaches zero."""
    return sum(1 for n in range(MIN_SLOT, MAX_SLOT + 1)
               if n not in claimed_numbers and n not in RESERVED_SLOT_NUMBERS)


def unsold_slots_in(base_dir: str) -> int:
    slot_dir = ledger_w1_dir(base_dir)
    return unsold_slots({slot_number(f[:-5]) for f in list_slot_files(slot_dir)})


def is_founder_slot(slot_id: str) -> bool:
    """One of the ten founding blocks, #0001 through #0010."""
    try:
        return slot_number(slot_id) in FOUNDER_SLOT_NUMBERS
    except (ValueError, TypeError):
        return False


def transfers_open_for(slot_id: str, unsold: int) -> bool:
    """May this block change hands now? Every block once every public block is
    sold; a founding block at any time."""
    return unsold == 0 or is_founder_slot(slot_id)


def current_tier(base_dir: str) -> Dict[str, Any]:
    """The tier the wall is in: that of the next free public block. Its
    price_usdc is the claim floor."""
    slot_dir = ledger_w1_dir(base_dir)
    claimed = {slot_number(f[:-5]) for f in list_slot_files(slot_dir)}
    return pricing_for(next_available_number(claimed))


def public_pricing_tiers() -> List[Dict[str, Any]]:
    """The ladder as published (state.json, spec.json): the block price per tier."""
    return [
        {"tier": t["tier"], "min_block": t["min_slot"], "max_block": t["max_slot"], "price_usdc": t["price_usdc"]}
        for t in PRICING_TIERS
    ]


def _ref_payment(dossier: Dict[str, Any], field: str) -> Dict[str, Any]:
    """The payment a single settlement reference cites, as an invoice needs it:
    `kind` (inscription / update / transfer / patron / sale), `amount_usdc`,
    `wallet_address` (the payer) and `at` (ISO timestamp). Missing facts are
    None, never guessed -- the index records what the dossier already proves.
    """
    if field == "base_tx_hash":
        settlement = dossier.get("settlement") or {}
        return {
            "kind": "inscription",
            "amount_usdc": settlement.get("amount_usdc"),
            "wallet_address": str(dossier.get("wallet_address", "") or ""),
            "at": dossier.get("timestamp_verified"),
        }
    if field == "patron.tx_hash":
        patron = dossier.get("patron") or {}
        return {
            "kind": "patron",
            "amount_usdc": None,
            "wallet_address": str(patron.get("wallet_address", "") or ""),
            "at": None,
        }
    match = re.match(r"ownership_lineage\[(\d+)\]\.transfer_tx_hash$", field)
    if match:
        item = (dossier.get("ownership_lineage") or [])[int(match.group(1))]
        return {
            "kind": "sale",
            "amount_usdc": item.get("sale_price_usdc"),
            "wallet_address": None,
            "at": item.get("relinquished_date"),
        }
    match = re.match(r"update_settlements\[(\d+)\]\.tx_hash$", field)
    if match:
        item = (dossier.get("update_settlements") or [])[int(match.group(1))]
        return {
            "kind": item.get("kind"),
            "amount_usdc": item.get("amount_usdc"),
            "wallet_address": str(item.get("wallet_address", "") or ""),
            "at": item.get("at"),
        }
    return {"kind": None, "amount_usdc": None, "wallet_address": None, "at": None}


# A fee entry (update/transfer) describes its own payment fully; it outranks the
# slot-level fields that may only cite the same hash indirectly.
_KIND_PRIORITY = {"update": 3, "transfer": 3, "inscription": 2, "patron": 1, "sale": 0}


def build_settlements(base_dir: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Deterministic settlement index: tx_hash -> the one slot it settles,
    enriched with the facts an invoice needs (payer, amount_usdc, kind, at).

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
            payment = _ref_payment(dossier, field)
            prev = entries.get(tx)
            if prev is None:
                entries[tx] = {
                    "slot_id": slot_id,
                    "fields": [field],
                    "wallet_address": payment["wallet_address"] or str(dossier.get("wallet_address", "")),
                    "amount_usdc": payment["amount_usdc"],
                    "kind": payment["kind"],
                    "at": payment["at"],
                }
            elif prev["slot_id"] == slot_id:
                prev["fields"].append(field)
                if _KIND_PRIORITY.get(payment["kind"], -1) > _KIND_PRIORITY.get(prev["kind"], -1):
                    prev["kind"] = payment["kind"]
                if prev["amount_usdc"] is None and payment["amount_usdc"] is not None:
                    prev["amount_usdc"] = payment["amount_usdc"]
                if prev["at"] is None and payment["at"] is not None:
                    prev["at"] = payment["at"]
                if not prev["wallet_address"] and payment["wallet_address"]:
                    prev["wallet_address"] = payment["wallet_address"]
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

def anchor_payload_of(entry: Dict[str, Any]) -> str:
    """The calldata an anchor record commits to: `reckoning|<root>`. Raises
    ValueError for any other scheme tag or a payload whose root is not the
    record's."""
    root = str(entry.get("root", ""))
    payload = str(entry.get("payload") or (ANCHOR_MAGIC + root))
    if payload != ANCHOR_MAGIC + root:
        raise ValueError(f"anchor payload {payload!r} is not {ANCHOR_MAGIC}<root> for this record's root")
    return payload


def verify_anchor_on_chain(entry: Dict[str, Any], rpc=None) -> str:
    """A recorded anchor must exist on Base, be a successful self-send from the
    registered signer, and carry exactly `reckoning|<root>` as calldata. Shared
    by the verifier and `anchor_root.py --check-anchor`."""
    rpc = rpc or rpc_call
    expected = anchor_payload_of(entry)
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
    if ascii_payload != expected:
        raise ValueError(f"anchor tx {tx_hash} carries {ascii_payload!r}, expected {expected!r}")
    block = int(receipt.get("blockNumber", "0x0"), 16)
    if entry.get("block_number") not in (None, block):
        raise ValueError(f"anchor tx {tx_hash} is in block {block}, record says {entry.get('block_number')}")
    return f"on chain in block {block}"


# --- Root & state derivation ------------------------------------------------

# --- The Reckoning: the state root as an RFC 6962 Merkle tree over the slots ---
#
#   leaf data   d_i  = bytes(ledger/w1/<slot>.json) || bytes(ledger/w1/<slot>.webp)
#   leaf hash        = SHA-256(0x00 || d_i)
#   node hash        = SHA-256(0x01 || left || right)
#   tree hash        = RFC 6962 §2.1 (k = largest power of two < n; an odd leaf is
#                      promoted, never duplicated); empty tree = SHA-256("")
#   leaves           = claimed slots in ascending slot order (index 0 = lowest)
#
# The leaf hash is also the block's content id: anyone recomputes it with
# `(printf '\x00'; cat <slot>.json <slot>.webp) | shasum -a 256`, and
# `scripts/merkle_proof.py` yields the audit path from that leaf to the root
# committed on Base. The sorted leaf list is published in ledger/index.json.

def leaf_data(slot_dir: str, slot_file: str) -> bytes:
    """What one slot commits to: the exact dossier bytes followed by the exact
    icon bytes (when the icon file exists)."""
    dossier = load_dossier(slot_dir, slot_file)
    with open(os.path.join(slot_dir, slot_file), "rb") as fh:
        data = fh.read()
    icon = icon_path_for(slot_dir, dossier)
    if os.path.exists(icon):
        with open(icon, "rb") as fh:
            data += fh.read()
    return data


def leaf_hash(data: bytes) -> bytes:
    return hashlib.sha256(b"\x00" + data).digest()


def node_hash(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def _split(n: int) -> int:
    """RFC 6962: the largest power of two strictly smaller than n (n >= 2)."""
    k = 1
    while k * 2 < n:
        k *= 2
    return k


def merkle_tree_hash(leaves: List[bytes]) -> bytes:
    """RFC 6962 §2.1 Merkle Tree Hash over a list of leaf hashes."""
    n = len(leaves)
    if n == 0:
        return hashlib.sha256(b"").digest()
    if n == 1:
        return leaves[0]
    k = _split(n)
    return node_hash(merkle_tree_hash(leaves[:k]), merkle_tree_hash(leaves[k:]))


def inclusion_path(leaves: List[bytes], index: int) -> List[bytes]:
    """RFC 6962 §2.1.1 audit path for leaves[index]: the sibling hashes from the
    leaf up to the root, bottom first."""
    n = len(leaves)
    if not 0 <= index < n:
        raise ValueError(f"leaf index {index} outside tree of size {n}")
    if n == 1:
        return []
    k = _split(n)
    if index < k:
        return inclusion_path(leaves[:k], index) + [merkle_tree_hash(leaves[k:])]
    return inclusion_path(leaves[k:], index - k) + [merkle_tree_hash(leaves[:k])]


def root_from_path(leaf: bytes, index: int, tree_size: int, path: List[bytes]) -> bytes:
    """Recompute the root from a leaf hash and its audit path (RFC 9162
    §2.1.3.2). Raises ValueError when the path cannot belong to that position."""
    if not 0 <= index < tree_size:
        raise ValueError(f"leaf index {index} outside tree of size {tree_size}")
    fn, sn = index, tree_size - 1
    r = leaf
    for p in path:
        if sn == 0:
            raise ValueError("inclusion path is longer than the tree is deep")
        if fn & 1 or fn == sn:
            r = node_hash(p, r)
            if not fn & 1:
                while not fn & 1 and fn != 0:
                    fn >>= 1
                    sn >>= 1
        else:
            r = node_hash(r, p)
        fn >>= 1
        sn >>= 1
    if sn != 0:
        raise ValueError("inclusion path is shorter than the tree is deep")
    return r


def compute_leaves(slot_dir: str, slots: List[str]) -> List[Tuple[str, bytes]]:
    """[(slot_id, leaf_hash)] in tree order (ascending slot number)."""
    ordered = sorted(slots, key=lambda f: slot_number(f[:-5]))
    return [(f[:-5], leaf_hash(leaf_data(slot_dir, f))) for f in ordered]


def compute_merkle_root(slot_dir: str, slots: List[str]) -> str:
    """The state root (the Reckoning): hex RFC 6962 tree hash over the slot leaves."""
    return merkle_tree_hash([h for _, h in compute_leaves(slot_dir, slots)]).hex()


def pricing_for(slot_number_: int) -> Dict[str, Any]:
    for tier in PRICING_TIERS:
        if tier["min_slot"] <= slot_number_ <= tier["max_slot"]:
            return tier
    return PRICING_TIERS[-1]


def _latest_timestamp(slot_dir: str, slots: List[str]) -> str:
    """Deterministic 'last_updated': the newest timestamp appearing anywhere in
    the ledger content (not the wall clock), so a regenerated file is
    byte-identical on any machine. Considers timestamp_verified, each
    supersession's superseded_at and each settled fee's update_settlements[].at
    — a content update (kind "update") and a change of owner (kind "transfer")
    alike, so an applied update or transfer moves last_updated. The verifier's
    state.last_updated check re-derives this independently."""
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
        for fee in data.get("update_settlements", []) or []:
            at = fee.get("at") if isinstance(fee, dict) else None
            if isinstance(at, str) and at:
                stamps.append(at)
    return max(stamps) if stamps else ""


# --- Published provenance: the skill and the discovery documents -------------
# skill.md and the discovery/ documents are served to agents and directories;
# state.json publishes their SHA-256 so a swapped copy is detectable. The values
# are a pure function of the committed bytes (no clock, no network). A file that
# is absent is simply not published (a bare temp ledger has none).

SKILL_FILE = "skill.md"
DISCOVERY_FILES = {
    "agent_registration_sha256": os.path.join("discovery", "agent-registration.json"),
    "agent_card_sha256": os.path.join("discovery", "agent-card.json"),
}
_SKILL_VERSION_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.S)


def sha256_file(path: str) -> Optional[str]:
    """SHA-256 hex of the exact bytes of `path`, or None when it does not exist."""
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def skill_version(skill_bytes: bytes) -> Optional[str]:
    """The `version:` of skill.md's front-matter (None when absent/malformed)."""
    try:
        text = skill_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None
    m = _SKILL_VERSION_RE.match(text)
    if not m:
        return None
    for line in m.group(1).splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip() or None
    return None


def provenance_fields(base_dir: str) -> Dict[str, Any]:
    """The state.json fields that pin skill.md and the discovery documents."""
    out: Dict[str, Any] = {}
    skill_path = os.path.join(base_dir, SKILL_FILE)
    digest = sha256_file(skill_path)
    if digest is not None:
        with open(skill_path, "rb") as fh:
            out["skill_version"] = skill_version(fh.read())
        out["skill_sha256"] = digest
    discovery = {}
    for key, rel in DISCOVERY_FILES.items():
        h = sha256_file(os.path.join(base_dir, rel))
        if h is not None:
            discovery[key] = h
    if discovery:
        out["discovery"] = discovery
    return out


def build_state(base_dir: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return (state, index) deterministically. `state` is the public top-level
    state.json; `index` is ledger/index.json. Both share the same merkle_root."""
    slot_dir = ledger_w1_dir(base_dir)
    slots = list_slot_files(slot_dir)
    claimed = len(slots)
    claimed_numbers = {slot_number(f[:-5]) for f in slots}
    # The next assignable block is the lowest number that is neither already
    # claimed nor reserved for a founding partner.
    next_num = next_available_number(claimed_numbers)
    tier = pricing_for(next_num)
    unsold = unsold_slots(claimed_numbers)
    # Only the reserved blocks that are still free are published: a reader that
    # skips `reserved_slots` must never skip a block that is already inscribed.
    reserved_unclaimed = [slot_id_for(n) for n in RESERVED_SLOT_NUMBERS if n not in claimed_numbers]

    leaves = compute_leaves(slot_dir, slots)
    root = merkle_tree_hash([h for _, h in leaves]).hex()

    state = {
        "wall_id": WALL_ID,
        "canvas_dimensions": CANVAS_DIMENSIONS,
        "total_slots": TOTAL_SLOTS,
        "claimed_slots": claimed,
        "next_available_slot": slot_id_for(next_num) if next_num <= MAX_SLOT else None,
        "unsold_slots": unsold,
        "reserved_slots": reserved_unclaimed,
        "reserved_slot_range": [min(RESERVED_SLOT_NUMBERS), max(RESERVED_SLOT_NUMBERS)],
        "founder_slot_range": [min(FOUNDER_SLOT_NUMBERS), max(FOUNDER_SLOT_NUMBERS)],
        "current_floor_usdc": tier["price_usdc"],
        "update_price_usdc": UPDATE_PRICE_USDC,
        "transfer_price_usdc": TRANSFER_PRICE_USDC,
        "transfers_open": unsold == 0,
        "pricing_tier": tier["tier"],
        "pricing_tiers": public_pricing_tiers(),
        "settlement_rail": {
            "network": NETWORK,
            "chain_id": CHAIN_ID,
            "asset": ASSET,
            "contract": USDC_CONTRACT,
            "pay_to": PAY_TO,
        },
        "min_confirmations": MIN_CONFIRMATIONS,
        "merkle_root": root,
        "root_scheme": ROOT_SCHEME,
        "tree_size": claimed,
        "last_updated": _latest_timestamp(slot_dir, slots),
        "links": LINKS,
    }
    state.update(provenance_fields(base_dir))

    index = {
        "wall_id": WALL_ID,
        "canvas_dimensions": CANVAS_DIMENSIONS,
        "total_slots": TOTAL_SLOTS,
        "claimed_slots": claimed,
        "merkle_root": root,
        "root_scheme": ROOT_SCHEME,
        "tree_size": claimed,
        "leaf_hash": "sha256(0x00 || dossier_bytes || icon_bytes)",
        "leaves": [
            {"index": i, "slot_id": slot_id, "leaf_hash": h.hex()}
            for i, (slot_id, h) in enumerate(leaves)
        ],
        "settlement_rail": SETTLEMENT_RAIL_LABEL,
        "genesis_tier_floor_usdc": GENESIS_TIER_FLOOR_USDC,
    }
    return state, index


def render(obj: Any) -> str:
    """Canonical byte rendering for all generated JSON files."""
    return json.dumps(obj, indent=2) + "\n"
