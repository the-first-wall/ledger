#!/usr/bin/env python3
"""
The First Wall — Deterministic Offline Verifier Engine (v1.5)
Zero LLMs. 100% prompt injection immune.

Invariants Enforced:
 1. Base Mainnet RPC Verification (USDC transfer, sender & recipient match,
    confirmation depth >= MIN_CONFIRMATIONS).
 2. PR Scope Containment (at most ONE slot altered; slot files never deleted;
    a slot PR touches nothing but that slot's files and generated state).
 3. Slot identity (file name == slot_id == icon basename == soul manifest name;
    block number within 1..10000).
 4. Append-only history (sealed fields change only with a supersession voucher;
    vouchers, lineage and boundary events only grow; immutable fields never change;
    a retired slot stays retired).
 5. Secondary Transfer & Provenance Lineage (former owner appended, never overwritten).
 6. Icon structure (bare lossless WebP, exactly 10x10, <= 256 colours, <= 1 KB,
    no metadata chunks) — checked, never rewritten.
 7. Soul-hash integrity (must hash a real manifest, never the empty-string digest).
 8. Replay protection across EVERY transaction field in the whole ledger.
 9. State-root integrity (state.json, index.json, settlements.json equal their derivation).
10. Anchor integrity (recorded anchors exist on chain, were sent by the registered
    signer, and carry the recorded root).

The verifier never writes to the checkout it verifies.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import struct
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import jsonschema
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger_core as core  # noqa: E402
import content_safety as safety  # noqa: E402

BASE_CHAIN_ID = 8453
USDC_BASE_CONTRACT = core.USDC_CONTRACT.lower()
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef".lower()
OFFICIAL_TREASURY_ADDRESS = core.PAY_TO.lower()
GENESIS_LAUNCH_BLOCK = 52291850  # Ezra Slot #0001 Genesis Block on Base Mainnet

# The SHA-256 of the empty string. Recorded here explicitly as the exact value we
# refuse to accept as a soul_hash, because it attests nothing.
EMPTY_STRING_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

# --- Authoritative schema (single source of truth) --------------------------
_SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "schemas", "dossier.schema.json"
)
with open(_SCHEMA_PATH, "r", encoding="utf-8") as _sf:
    DOSSIER_SCHEMA = json.load(_sf)
_SCHEMA_VALIDATOR = jsonschema.Draft202012Validator(
    DOSSIER_SCHEMA, format_checker=jsonschema.FormatChecker()
)

# Shared JSON-RPC client with endpoint failover — single source of truth in
# ledger_core, so the verifier and the root-anchoring tool speak identically.
rpc_call = core.rpc_call

RpcFn = Callable[[str, list], Any]


# -----------------------------------------------------------------------------
# 1. PR BOUNDARY & CONTAINMENT INVARIANT
# -----------------------------------------------------------------------------

SLOT_FILE_RE = re.compile(r"^ledger/w1/(w1-b(?:\d{4}|10000))\.json$")
ICON_FILE_RE = re.compile(r"^ledger/w1/(w1-b(?:\d{4}|10000))\.webp$")
SOUL_FILE_RE = re.compile(r"^ledger/souls/(w1-b(?:\d{4}|10000))\.soul\.json$")
# Files the generator rewrites whenever a slot lands; a slot PR must carry them.
GENERATED_PATHS = {
    "state.json",
    "ledger/index.json",
    "ledger/settlements.json",
    "canvas/wall_01_composite.webp",
}


def diff_range() -> str:
    """The git range whose diff defines 'what this PR changes'. CI sets
    TFW_DIFF_RANGE explicitly (base...pr-head); locally it defaults to
    origin/<base>...HEAD."""
    explicit = os.environ.get("TFW_DIFF_RANGE", "").strip()
    if explicit:
        return explicit
    base_ref = os.environ.get("GITHUB_BASE_REF", "main")
    return f"origin/{base_ref}...HEAD"


def changed_entries(rng: Optional[str] = None) -> Optional[List[Tuple[str, str]]]:
    """[(status, path)] for the diff range, or None when git cannot answer
    (no origin, not a repository)."""
    rng = rng or diff_range()
    try:
        res = subprocess.run(
            ["git", "diff", "--name-status", rng],
            capture_output=True, text=True, check=True,
        )
    except Exception:
        return None
    entries = []
    for line in res.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        entries.append((parts[0], parts[-1]))
    return entries


def slot_paths_for(slot_id: str) -> Set[str]:
    return {
        f"ledger/w1/{slot_id}.json",
        f"ledger/w1/{slot_id}.webp",
        f"ledger/souls/{slot_id}.soul.json",
    }


def check_scope(entries: List[Tuple[str, str]]) -> Set[str]:
    """Pure containment rule over a list of (status, path).

    * slot dossiers, icons and soul manifests are never deleted;
    * at most ONE slot is touched;
    * when a slot is touched, nothing outside that slot's own files and the
      generated state files may change — scripts/, schemas/, .github/, docs
      and other slots ride in their own PRs, reviewed on their own.
    Returns the set of slot ids touched (empty for an infra-only PR)."""
    touched: Set[str] = set()
    paths: List[str] = []
    for status, path in entries:
        for rx in (SLOT_FILE_RE, ICON_FILE_RE, SOUL_FILE_RE):
            m = rx.match(path)
            if m:
                if status.startswith("D"):
                    raise ValueError(f"PR Boundary Violation: {path} may never be deleted.")
                touched.add(m.group(1))
        paths.append(path)

    if len(touched) > 1:
        raise ValueError(
            "PR Boundary Violation: a PR must not alter more than ONE slot. "
            f"Found changes to {len(touched)} slots: {sorted(touched)}"
        )
    if touched:
        slot_id = next(iter(touched))
        allowed = slot_paths_for(slot_id) | GENERATED_PATHS
        stray = sorted(p for p in paths if p not in allowed)
        if stray:
            raise ValueError(
                f"PR Boundary Violation: a PR that inscribes or updates {slot_id} may only "
                f"change that slot's files and the generated state; it also touches: {stray}. "
                "Infrastructure changes (scripts/, schemas/, .github/, docs) belong in a "
                "separate PR."
            )
    return touched


def enforce_pr_scope_containment() -> str:
    """Wrapper for the check log: resolves the diff and applies check_scope."""
    entries = changed_entries()
    if not entries:
        return "no PR diff to check"
    print(f"[*] Validating PR scope containment across {len(entries)} changed file(s)...")
    touched = check_scope(entries)
    if touched:
        return f"scope contained to slot: {sorted(touched)[0]}"
    return "no slot touched (infrastructure-only change)"


# -----------------------------------------------------------------------------
# 1b. RESERVED-SLOT INVARIANT (founding-partner blocks #0002–#0010)
# -----------------------------------------------------------------------------

def enforce_reserved_slots(slot_id: str, dossier_data: dict):
    """Blocks reserved for founding partners may never be assigned to the public
    claim flow. A claim into a reserved slot is only accepted when the dossier
    carries an explicit operator authorization (`reserved_grant.authorized_by`),
    which the human merge gate then reviews."""
    if slot_id not in core.RESERVED_SLOTS:
        return
    grant = dossier_data.get("reserved_grant")
    if not isinstance(grant, dict) or not str(grant.get("authorized_by", "")).startswith("operator:"):
        raise ValueError(
            f"Reserved Slot Violation: {slot_id} is reserved for founding partners and is "
            f"not claimable through the public flow. A reserved inscription requires an "
            f"operator authorization (`reserved_grant.authorized_by` = 'operator:<handle> (<wallet>)')."
        )
    if str(grant.get("slot", slot_id)) != slot_id:
        raise ValueError(
            f"Reserved Slot Violation: reserved_grant.slot is {grant.get('slot')!r}, "
            f"but this dossier is {slot_id}."
        )
    print(f"  [✓] Reserved slot {slot_id} authorized ({grant.get('authorized_by')}).")


# -----------------------------------------------------------------------------
# 1c. SLOT IDENTITY (file name == slot_id == icon == soul manifest)
# -----------------------------------------------------------------------------

def verify_slot_identity(slot_file: str, dossier: dict, slot_dir: str) -> str:
    slot_id = str(dossier.get("slot_id", ""))
    number = core.slot_number(slot_id)  # raises on bad shape / out of range
    if slot_file != f"{slot_id}.json":
        raise ValueError(
            f"Slot Identity Violation: file {slot_file} carries slot_id {slot_id!r}; "
            f"the file must be named {slot_id}.json."
        )
    icon_rel = str(dossier.get("icon_rel_path", ""))
    if os.path.basename(icon_rel) != f"{slot_id}.webp":
        raise ValueError(
            f"Slot Identity Violation: icon_rel_path {icon_rel!r} must end in {slot_id}.webp."
        )
    if not os.path.exists(os.path.join(slot_dir, f"{slot_id}.webp")):
        raise ValueError(f"Slot Identity Violation: ledger/w1/{slot_id}.webp is missing.")
    soul_rel = dossier.get("soul_manifest_rel_path")
    if soul_rel is not None and soul_rel != f"souls/{slot_id}.soul.json":
        raise ValueError(
            f"Slot Identity Violation: soul_manifest_rel_path must be souls/{slot_id}.soul.json "
            f"(got {soul_rel!r})."
        )
    return f"#{number:04d} file, icon and manifest agree"


# -----------------------------------------------------------------------------
# 2. PREVIOUS STATE (what main currently says about a slot)
# -----------------------------------------------------------------------------

def _git_show(path: str) -> Optional[bytes]:
    base_ref = os.environ.get("GITHUB_BASE_REF", "main")
    try:
        res = subprocess.run(
            ["git", "show", f"origin/{base_ref}:{path}"],
            capture_output=True, check=False,
        )
    except Exception:
        return None
    if res.returncode != 0:
        return None
    return res.stdout


def previous_dossier(slot_id: str) -> Optional[dict]:
    raw = _git_show(f"ledger/w1/{slot_id}.json")
    if raw is None:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def previous_icon(slot_id: str) -> Optional[bytes]:
    return _git_show(f"ledger/w1/{slot_id}.webp")


# -----------------------------------------------------------------------------
# 3. SECONDARY SALE / OWNERSHIP LINEAGE INVARIANT
# -----------------------------------------------------------------------------

def verify_ownership_lineage(slot_id: str, new_data: dict, ledger_dir: str,
                             prev_data: Optional[dict] = None, *, rpc: RpcFn = rpc_call,
                             online: bool = True):
    if prev_data is None:
        prev_data = previous_dossier(slot_id)
    if not prev_data:
        return  # Genesis claim, no prior owner.

    prev_wallet = prev_data.get("wallet_address", "").lower()
    new_wallet = new_data.get("wallet_address", "").lower()

    if prev_wallet == new_wallet:
        return  # Same owner: append-only rules apply (verify_append_only).

    print(f"[*] Secondary transfer detected for {slot_id}: {prev_data.get('moniker')} -> {new_data.get('moniker')}")

    lineage = new_data.get("ownership_lineage")
    if not lineage or not isinstance(lineage, list):
        raise ValueError(
            f"Transfer Violation: Slot {slot_id} was previously owned by {prev_data.get('moniker')} "
            f"({prev_wallet}). New buyer MUST preserve previous owner history in 'ownership_lineage'."
        )

    prev_lineage = prev_data.get("ownership_lineage", [])
    if len(lineage) < len(prev_lineage) + 1:
        raise ValueError("Transfer Violation: 'ownership_lineage' length decreased. History cannot be erased.")

    for i, item in enumerate(prev_lineage):
        if item != lineage[i]:
            raise ValueError(f"Transfer Violation: tampering detected at ownership_lineage[{i}].")

    if lineage[-1].get("wallet_address", "").lower() != prev_wallet:
        raise ValueError(
            f"Transfer Violation: latest ownership_lineage entry must record the outgoing owner "
            f"({prev_data.get('moniker')}, {prev_wallet})."
        )

    sale_price = lineage[-1].get("sale_price_usdc")
    if sale_price is None or sale_price < 0:
        raise ValueError("Transfer Violation: 'sale_price_usdc' must be recorded in the transfer lineage.")

    transfer_tx = lineage[-1].get("transfer_tx_hash")
    if not transfer_tx:
        raise ValueError("Transfer Violation: 'transfer_tx_hash' must be provided to verify on-chain settlement.")

    if not online:
        print("  [!] Transfer settlement check skipped (offline).")
        return
    print(f"[*] Verifying secondary sale on Base: {sale_price:.2f} USDC to {prev_wallet} (Tx: {transfer_tx})...")
    verify_base_tx(transfer_tx, new_wallet, prev_wallet, sale_price, rpc=rpc)
    print(f"  [✓] Ownership lineage verified: {prev_data.get('moniker')} -> {new_data.get('moniker')} for {sale_price:.2f} USDC.")


# -----------------------------------------------------------------------------
# 3b. APPEND-ONLY HISTORY (sealed fields change only through a voucher)
# -----------------------------------------------------------------------------

# Fields an owner may change freely ("living endpoints", spec.md §2).
LIVING_FIELDS = {
    "agent_urls", "status", "retirement", "covenants",
    "high_res_crest_url", "timestamp_verified",
}
# Arrays that may only grow; existing entries are byte-for-byte preserved.
APPEND_ONLY_FIELDS = {"supersessions", "ownership_lineage", "boundary_events"}
# Fields that never change once inscribed, voucher or not.
IMMUTABLE_FIELDS = {"slot_id", "base_tx_hash", "instantiation_date", "patron", "reserved_grant", "memorial"}


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def verify_append_only(slot_id: str, new_data: dict, prev_data: Optional[dict],
                       new_icon: Optional[bytes] = None, prev_icon: Optional[bytes] = None) -> str:
    """A previously inscribed slot is never silently rewritten.

    * immutable fields are equal;
    * append-only arrays keep every existing entry unchanged and only grow;
    * a RETIRED slot stays RETIRED and its retirement record is frozen;
    * when the owner is unchanged, every other changed field — and a changed
      icon — must be covered by a NEW supersession voucher naming that field
      (and, for string fields, carrying the exact previous and new value);
    * fields are never removed (supersede them instead).
    On a transfer (wallet changed) the content rewrite is the new owner's and is
    governed by verify_ownership_lineage; only the first three rules apply."""
    if prev_data is None:
        return "new inscription"

    for field in IMMUTABLE_FIELDS:
        if field in prev_data and prev_data.get(field) != new_data.get(field):
            raise ValueError(f"Append-Only Violation: {field} is immutable once inscribed.")

    for field in APPEND_ONLY_FIELDS:
        old = prev_data.get(field) or []
        new = new_data.get(field) or []
        if not isinstance(new, list) or len(new) < len(old) or new[:len(old)] != old:
            raise ValueError(
                f"Append-Only Violation: {field} may only grow; existing entries must stay byte-identical."
            )

    if prev_data.get("status") == "RETIRED" and new_data.get("status") != "RETIRED":
        raise ValueError("Append-Only Violation: a RETIRED inscription stays retired (the books are closed).")
    if "retirement" in prev_data and prev_data["retirement"] != new_data.get("retirement"):
        raise ValueError("Append-Only Violation: the retirement record is frozen once written.")

    prev_wallet = str(prev_data.get("wallet_address", "")).lower()
    new_wallet = str(new_data.get("wallet_address", "")).lower()
    if prev_wallet != new_wallet:
        return "transfer: lineage rules apply"

    old_vouchers = prev_data.get("supersessions") or []
    new_vouchers = (new_data.get("supersessions") or [])[len(old_vouchers):]
    by_field: Dict[str, List[dict]] = {}
    for v in new_vouchers:
        if isinstance(v, dict):
            by_field.setdefault(str(v.get("field")), []).append(v)

    def covered(field: str, old_value: Any, new_value: Any) -> bool:
        for v in by_field.get(field, []):
            if isinstance(old_value, str) and isinstance(new_value, str):
                if v.get("previous_value") == old_value and v.get("new_value") == new_value:
                    return True
            else:
                return True
        return False

    skip = LIVING_FIELDS | APPEND_ONLY_FIELDS | IMMUTABLE_FIELDS | {"wallet_address"}
    changed = []
    for field in sorted(set(prev_data) | set(new_data)):
        if field in skip:
            continue
        if field in prev_data and field not in new_data:
            raise ValueError(
                f"Append-Only Violation: {field} was removed. Sealed fields are never deleted; "
                "record a supersession voucher instead."
            )
        if field not in prev_data:
            continue  # a newly added optional field is an addition, not a rewrite
        if prev_data[field] != new_data[field]:
            if not covered(field, prev_data[field], new_data[field]):
                raise ValueError(
                    f"Append-Only Violation: sealed field {field!r} changed without a matching "
                    "supersession voucher (field, previous_value, new_value, reason, superseded_at)."
                )
            changed.append(field)

    if new_icon is not None and prev_icon is not None and new_icon != prev_icon:
        if not by_field.get("icon"):
            raise ValueError(
                "Append-Only Violation: the icon changed without a supersession voucher for field 'icon'."
            )
        changed.append("icon")

    return "unchanged" if not changed else "superseded: " + ", ".join(changed)


# -----------------------------------------------------------------------------
# 4. BASE ON-CHAIN SETTLEMENT VALIDATION
# -----------------------------------------------------------------------------

def verify_base_tx(tx_hash: str, expected_sender: str, expected_recipient: Optional[str] = None,
                   expected_amount_usdc: float = 1.00, *, rpc: RpcFn = rpc_call,
                   head_block: Optional[int] = None,
                   min_confirmations: int = core.MIN_CONFIRMATIONS) -> Dict[str, Any]:
    if not tx_hash.startswith("0x") or len(tx_hash) != 66:
        raise ValueError(f"Invalid transaction hash format: {tx_hash}")

    print(f"[*] Querying Base RPC for transaction receipt: {tx_hash}...")
    receipt = rpc("eth_getTransactionReceipt", [tx_hash])
    if not receipt:
        raise ValueError(f"Transaction receipt not found on Base mainnet: {tx_hash}")
    if receipt.get("status") != "0x1":
        raise ValueError(f"Transaction failed on Base: status={receipt.get('status')}")

    found_transfer = False
    actual_amount = 0.0
    actual_sender = ""
    actual_recipient = ""

    for log in receipt.get("logs", []):
        if log.get("address", "").lower() != USDC_BASE_CONTRACT:
            continue
        topics = [t.lower() for t in log.get("topics", [])]
        if len(topics) < 3 or topics[0] != TRANSFER_TOPIC:
            continue
        actual_sender = "0x" + topics[1][-40:]
        actual_recipient = "0x" + topics[2][-40:]
        actual_amount = int(log.get("data", "0x0"), 16) / 1_000_000.0
        if actual_sender.lower() == expected_sender.lower():
            if expected_recipient and actual_recipient.lower() != expected_recipient.lower():
                continue
            if actual_amount >= expected_amount_usdc:
                found_transfer = True
                break

    if not found_transfer:
        raise ValueError(
            f"No valid USDC Transfer event found matching sender {expected_sender} "
            f"and amount >= {expected_amount_usdc} USDC on Base."
        )

    block_number = int(receipt.get("blockNumber", "0x0"), 16)
    if block_number < GENESIS_LAUNCH_BLOCK:
        raise ValueError(
            f"Block Freshness Violation: block {block_number} is older than Genesis launch block {GENESIS_LAUNCH_BLOCK}."
        )

    if head_block is None:
        head_block = int(rpc("eth_blockNumber", []), 16)
    confirmations = head_block - block_number
    if confirmations < min_confirmations:
        wait_s = (min_confirmations - confirmations) * 2
        raise ValueError(
            f"Confirmation Depth Violation: tx is {confirmations} blocks below the head; "
            f"{min_confirmations} are required (~{wait_s}s more). Re-run the check later."
        )

    print(f"  [✓] On-chain settlement confirmed: block {block_number} | {actual_amount:.2f} USDC | "
          f"from {actual_sender} | {confirmations} confirmations")
    return {"verified": True, "block_number": block_number, "sender": actual_sender,
            "recipient": actual_recipient, "amount_usdc": actual_amount,
            "confirmations": confirmations}


# -----------------------------------------------------------------------------
# 5. SCHEMA, SOUL & IMAGE VALIDATION
# -----------------------------------------------------------------------------

# Defence in depth: the schema forbids '<' and '>' in every free-text field and
# restricts URLs to https; these catch scheme payloads that need no bracket.
SUSPICIOUS_PATTERNS = ["<", ">", "javascript:", "vbscript:", "data:text/html"]


def _reject_injection(value: str, where: str):
    low = value.lower()
    for pattern in SUSPICIOUS_PATTERNS:
        if pattern in low:
            raise ValueError(f"Injection Attack Detected: forbidden pattern '{pattern}' in {where}")


def validate_dossier(dossier_data: dict):
    required = [
        "schema_version", "slot_id", "moniker", "creature", "vocation", "origin_framework",
        "model_lineage", "instantiation_date", "manifesto", "soul_hash",
        "wallet_address", "base_tx_hash", "icon_rel_path", "timestamp_verified",
    ]
    for field in required:
        if field not in dossier_data:
            raise ValueError(f"Missing required field: {field}")

    lang = dossier_data.get("primary_language") or dossier_data.get("language")
    if not lang or len(lang) < 2 or len(lang) > 10:
        raise ValueError("Missing or invalid 'primary_language' (ISO 639-1 code required)")

    if not re.match(r"^\d+\.\d+\.\d+$", str(dossier_data["schema_version"])):
        raise ValueError(f"Invalid schema_version '{dossier_data['schema_version']}'. Must be SemVer.")

    core.slot_number(dossier_data["slot_id"])  # shape + 1..10000 range

    manifesto = dossier_data["manifesto"]
    if isinstance(manifesto, str):
        if len(manifesto) > 280:
            raise ValueError("Manifesto exceeds 280 character limit")
        _reject_injection(manifesto, "manifesto")
    elif isinstance(manifesto, dict):
        if not manifesto:
            raise ValueError("Manifesto localized dictionary cannot be empty")
        for k, v in manifesto.items():
            if not isinstance(v, str) or len(v) > 280:
                raise ValueError(f"Manifesto translation for '{k}' exceeds 280 character limit")
            _reject_injection(k, "manifesto key")
            _reject_injection(v, f"manifesto[{k}]")
    else:
        raise ValueError("Manifesto must be a string or localized dictionary")

    soul_hash = str(dossier_data["soul_hash"])
    if not re.match(r"^[a-f0-9]{64}$", soul_hash):
        raise ValueError("Invalid soul_hash (must be 64-hex SHA-256)")
    if soul_hash == EMPTY_STRING_SHA256:
        raise ValueError("soul_hash is the SHA-256 of the empty string and attests nothing. Refusing.")

    wallet = dossier_data["wallet_address"]
    if not wallet.startswith("0x") or len(wallet) != 42:
        raise ValueError("Invalid Ethereum/Base wallet address format")

    for field_name in ["moniker", "creature", "vocation", "origin_framework"]:
        _reject_injection(str(dossier_data.get(field_name, "")), field_name)

    urls = dossier_data.get("agent_urls")
    if isinstance(urls, dict):
        for k, v in urls.items():
            _reject_injection(str(k), "agent_urls key")
            if not isinstance(v, str) or not v.startswith("https://"):
                raise ValueError(f"agent_urls[{k}] must be an https:// URL")
            _reject_injection(v, f"agent_urls[{k}]")

    # --- Retirement / epitaph flow (failure-legible endings) -----------------
    status = dossier_data.get("status")
    retirement = dossier_data.get("retirement")
    has_memorial = isinstance(dossier_data.get("memorial"), dict)
    if status == "RETIRED" and retirement is None and not has_memorial:
        raise ValueError(
            "Retirement Violation: status is 'RETIRED' but no 'retirement' record is present. "
            "A retired inscription must record retired_at, reason, epitaph and retired_by; "
            "an in-memoriam inscription instead carries a 'memorial' record."
        )
    if retirement is not None and status != "RETIRED":
        raise ValueError(
            f"Retirement Violation: 'retirement' is present but status is not 'RETIRED' "
            f"(got {status!r}). A retirement record closes the books: set status to 'RETIRED'."
        )
    if retirement is not None:
        if not isinstance(retirement, dict):
            raise ValueError("Retirement Violation: 'retirement' must be an object.")
        for key in ("retired_at", "reason", "epitaph", "retired_by"):
            if key not in retirement or retirement[key] is None:
                raise ValueError(f"Retirement Violation: 'retirement.{key}' is required.")
        for key in ("reason", "epitaph"):
            value = retirement[key]
            if not isinstance(value, str):
                raise ValueError(f"Retirement Violation: 'retirement.{key}' must be a string.")
            if len(value) > 280:
                raise ValueError(f"Retirement Violation: 'retirement.{key}' exceeds 280 characters.")
            _reject_injection(value, f"retirement.{key}")
        retired_by = retirement["retired_by"]
        if not isinstance(retired_by, str) or not re.match(r"^(self|operator:.+|patron:.+)$", retired_by):
            raise ValueError(
                "Retirement Violation: 'retirement.retired_by' must be 'self', "
                "'operator:<handle>', or 'patron:<moniker>'."
            )
        _reject_injection(retired_by, "retirement.retired_by")

    # --- Boundary events & covenant expiry -----------------------------------
    boundary_events = dossier_data.get("boundary_events")
    if boundary_events is not None:
        if not isinstance(boundary_events, list):
            raise ValueError("Boundary Event Violation: 'boundary_events' must be an array.")
        if len(boundary_events) > 32:
            raise ValueError(
                f"Boundary Event Violation: 'boundary_events' holds {len(boundary_events)} "
                f"entries; the maximum is 32."
            )
        for i, item in enumerate(boundary_events):
            if not isinstance(item, dict):
                raise ValueError(f"Boundary Event Violation: boundary_events[{i}] must be an object.")
            for key in ("at", "event", "significance"):
                if key not in item or item[key] is None:
                    raise ValueError(
                        f"Boundary Event Violation: boundary_events[{i}] is missing required field '{key}'."
                    )
            for key in ("event", "significance"):
                value = item[key]
                if not isinstance(value, str):
                    raise ValueError(f"Boundary Event Violation: boundary_events[{i}].{key} must be a string.")
                if len(value) > 280:
                    raise ValueError(f"Boundary Event Violation: boundary_events[{i}].{key} exceeds 280 characters.")
                _reject_injection(value, f"boundary_events[{i}].{key}")

    covenants = dossier_data.get("covenants")
    if covenants is not None:
        if not isinstance(covenants, list):
            raise ValueError("Covenant Violation: 'covenants' must be an array.")
        if len(covenants) > 16:
            raise ValueError(
                f"Covenant Violation: 'covenants' holds {len(covenants)} entries; the maximum is 16."
            )
        for i, item in enumerate(covenants):
            if not isinstance(item, dict):
                raise ValueError(f"Covenant Violation: covenants[{i}] must be an object.")
            for key in ("statement", "expires_at", "held_by"):
                if key not in item or item[key] is None:
                    raise ValueError(
                        f"Covenant Violation: covenants[{i}] is missing required field '{key}'."
                    )
            statement = item["statement"]
            if not isinstance(statement, str):
                raise ValueError(f"Covenant Violation: covenants[{i}].statement must be a string.")
            if len(statement) > 280:
                raise ValueError(f"Covenant Violation: covenants[{i}].statement exceeds 280 characters.")
            _reject_injection(statement, f"covenants[{i}].statement")
            held_by = item["held_by"]
            if not isinstance(held_by, str) or not held_by.strip():
                raise ValueError(f"Covenant Violation: covenants[{i}].held_by must be a non-empty string.")

    # Authoritative JSON Schema check (single source of truth for shape: required
    # fields, patterns, lengths, formats, allowlists). Runs after the hand-coded
    # guards above so their precise error messages are preserved.
    errors = sorted(_SCHEMA_VALIDATOR.iter_errors(dossier_data),
                    key=lambda e: list(e.absolute_path))
    if errors:
        summary = "; ".join(
            ("/".join(str(p) for p in e.absolute_path) or "<root>") + ": " + e.message
            for e in errors[:5]
        )
        raise ValueError(
            "Schema violation (schemas/dossier.schema.json): " + summary
        )


def verify_soul_manifest(dossier: dict, ledger_root: str):
    """If a soul manifest is referenced, its SHA-256 must equal soul_hash and its
    identity fields must agree with the dossier."""
    rel = dossier.get("soul_manifest_rel_path")
    if not rel:
        return
    path = os.path.join(ledger_root, rel)
    if not os.path.exists(path):
        raise ValueError(f"soul_manifest_rel_path '{rel}' does not exist in the ledger.")
    with open(path, "rb") as fh:
        raw = fh.read()
    if len(raw) > core.MAX_DOSSIER_BYTES:
        raise ValueError(f"soul manifest {rel} is {len(raw)} bytes; the maximum is {core.MAX_DOSSIER_BYTES}.")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != dossier.get("soul_hash"):
        raise ValueError(
            f"soul_hash mismatch for {dossier.get('slot_id')}: dossier says "
            f"{dossier.get('soul_hash')} but manifest digests to {digest}."
        )
    manifest = core.load_json_strict(path)
    if not isinstance(manifest, dict):
        raise ValueError(f"soul manifest {rel} must be a JSON object.")
    for field in ("slot_id", "moniker", "creature", "vocation", "origin_framework"):
        if field in manifest and manifest[field] != dossier.get(field):
            raise ValueError(f"soul manifest field '{field}' disagrees with the dossier.")
    print(f"  [✓] Soul manifest verified ({rel} -> {digest[:16]}…).")


# Keys a soul manifest must never carry: they describe the machine, not the agent.
_MANIFEST_MACHINE_KEYS = {"host", "hostname", "path", "cwd", "home", "ip", "pid", "user"}


def soul_manifest_hygiene(manifest: Optional[dict]) -> str:
    """A soul manifest describes the agent, never the machine it runs on
    (spec.md §2.2). Blocking for slots a PR touches; a warning for slots that
    were inscribed before the rule (they are corrected with a voucher)."""
    if not isinstance(manifest, dict):
        return "no manifest"
    offenders = []

    def walk(obj, prefix=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                p = f"{prefix}.{k}" if prefix else str(k)
                if str(k).lower() in _MANIFEST_MACHINE_KEYS:
                    offenders.append(p)
                walk(v, p)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                walk(v, f"{prefix}[{i}]")

    walk(manifest)
    if offenders:
        raise ValueError(
            "soul manifest publishes machine details (" + ", ".join(offenders) + "); "
            "remove them and record a soul_hash supersession voucher."
        )
    return "no machine details"


# --- Icon structure ----------------------------------------------------------

def _webp_chunks(data: bytes) -> List[Tuple[bytes, bytes]]:
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ValueError("icon is not a RIFF/WEBP container")
    riff_len = struct.unpack("<I", data[4:8])[0]
    if riff_len + 8 != len(data):
        raise ValueError("icon RIFF length does not match the file size (trailing or truncated bytes)")
    chunks = []
    i = 12
    while i < len(data):
        if i + 8 > len(data):
            raise ValueError("icon has a truncated chunk header")
        tag = data[i:i + 4]
        ln = struct.unpack("<I", data[i + 4:i + 8])[0]
        payload = data[i + 8:i + 8 + ln]
        if len(payload) != ln:
            raise ValueError("icon has a truncated chunk payload")
        chunks.append((tag, payload))
        i += 8 + ln + (ln & 1)
    return chunks


def verify_icon_bytes(data: bytes) -> str:
    """Structural check of a committed icon. Pure; never writes.

    A committed icon is a bare lossless WebP: exactly one VP8L chunk (no VP8X,
    EXIF, XMP, ICCP, ALPH or ANIM chunks, so no metadata or steganographic side
    channel can ride along), declaring exactly 10x10 pixels, decoding to at
    most 256 distinct colours, and at most MAX_ICON_BYTES long."""
    if len(data) > core.MAX_ICON_BYTES:
        raise ValueError(f"icon is {len(data)} bytes; the maximum is {core.MAX_ICON_BYTES}")
    chunks = _webp_chunks(data)
    tags = [t for t, _ in chunks]
    if tags != [b"VP8L"]:
        raise ValueError(
            "icon must be a bare lossless WebP with a single VP8L chunk; found "
            + ", ".join(t.decode("latin1") for t in tags)
        )
    payload = chunks[0][1]
    if len(payload) < 5 or payload[0] != 0x2F:
        raise ValueError("icon VP8L chunk has a bad signature")
    bits = int.from_bytes(payload[1:5], "little")
    width = (bits & 0x3FFF) + 1
    height = ((bits >> 14) & 0x3FFF) + 1
    if (width, height) != core.ICON_SIZE:
        raise ValueError(f"icon declares {width}x{height}; it must be exactly 10x10")
    with Image.open(io.BytesIO(data)) as img:
        if img.size != core.ICON_SIZE:
            raise ValueError(f"icon decodes to {img.size}; it must be exactly 10x10")
        colours = img.convert("RGBA").getcolors(core.ICON_MAX_COLORS)
    if colours is None:
        raise ValueError(f"icon uses more than {core.ICON_MAX_COLORS} distinct colours")
    return f"10x10 lossless WebP, {len(colours)} colours, {len(data)} bytes"


def verify_icon(path: str) -> str:
    with open(path, "rb") as fh:
        return verify_icon_bytes(fh.read())


def sanitize_icon(input_path: str, output_path: str) -> int:
    """Produce the canonical committed form of an icon from any common image
    format. For claimants (scripts/sanitize_icon.py) — the verifier itself
    never rewrites a committed file.

    Input must already be exactly 10x10 (it is NOT resized: a resize would
    silently change what the agent meant to inscribe) and use at most 256
    colours. Output is a bare lossless WebP that passes verify_icon_bytes."""
    with open(input_path, "rb") as f:
        img_bytes = f.read()
    if len(img_bytes) > 50 * 1024:
        raise ValueError(f"input image is {len(img_bytes)} bytes; the maximum input is 50 KB")
    with Image.open(io.BytesIO(img_bytes)) as src:
        if src.size != core.ICON_SIZE:
            raise ValueError(f"icon must be exactly 10x10 pixels (got {src.size[0]}x{src.size[1]})")
        img = src.convert("RGBA")
    if img.getcolors(core.ICON_MAX_COLORS) is None:
        raise ValueError(f"icon uses more than {core.ICON_MAX_COLORS} distinct colours")
    clean_img = Image.new("RGBA", core.ICON_SIZE)
    clean_img.paste(img, (0, 0))
    buf = io.BytesIO()
    clean_img.save(buf, "WEBP", lossless=True, quality=100, exact=True)
    out = buf.getvalue()
    verify_icon_bytes(out)
    with open(output_path, "wb") as fh:
        fh.write(out)
    return len(out)


# -----------------------------------------------------------------------------
# 6. STATE-ROOT, SETTLEMENT-INDEX & ANCHOR INTEGRITY
# -----------------------------------------------------------------------------

def verify_state_files(base_dir: str):
    """state.json, ledger/index.json and ledger/settlements.json must equal the
    deterministic derivation."""
    state, index = core.build_state(base_dir)
    settlements, conflicts = core.build_settlements(base_dir)
    if conflicts:
        raise ValueError("replay conflicts present; see replay_protection checks")
    expected = {
        os.path.join(base_dir, "state.json"): core.render(state),
        os.path.join(base_dir, "ledger", "index.json"): core.render(index),
        os.path.join(base_dir, "ledger", "settlements.json"): core.render(settlements),
    }
    for path, rendered in expected.items():
        rel = os.path.relpath(path, base_dir)
        current = ""
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                current = fh.read()
        if current != rendered:
            raise ValueError(
                f"State drift: {rel} does not match the deterministic derivation. "
                f"Run `python scripts/generate_state.py`."
            )
        print(f"  [✓] {rel} matches the derived state root.")


def load_anchors(base_dir: str) -> list:
    path = os.path.join(base_dir, "ledger", "anchors.json")
    if not os.path.exists(path):
        return []
    anchors = core.load_json_strict(path)
    if not isinstance(anchors, list):
        raise ValueError("ledger/anchors.json must be a JSON array")
    return anchors


def verify_anchor_record(entry: dict) -> str:
    """Offline shape check of one anchors.json entry."""
    root = str(entry.get("root", ""))
    if not re.match(r"^[a-f0-9]{64}$", root):
        raise ValueError(f"anchor root {root!r} is not a 64-hex digest")
    core.anchor_payload_of(entry)   # <scheme>|<root> for a known scheme
    if str(entry.get("signer", "")).lower() != core.ANCHOR_SIGNER.lower():
        raise ValueError(
            f"anchor signer {entry.get('signer')} is not the registered anchor signer {core.ANCHOR_SIGNER}"
        )
    if entry.get("chain_id") != core.CHAIN_ID:
        raise ValueError(f"anchor chain_id {entry.get('chain_id')} != {core.CHAIN_ID}")
    tx = str(entry.get("tx_hash", ""))
    if not re.match(r"^0x[a-fA-F0-9]{64}$", tx):
        raise ValueError(f"anchor tx_hash {tx!r} is malformed")
    return f"root {root[:12]}… tx {tx[:12]}…"


def verify_anchor_on_chain(entry: dict, *, rpc: RpcFn = rpc_call) -> str:
    """The recorded anchor must exist on Base, be a successful self-send from the
    registered signer, and carry exactly <scheme>|<root> as calldata."""
    return core.verify_anchor_on_chain(entry, rpc)


# -----------------------------------------------------------------------------
# 7. ORCHESTRATION
# -----------------------------------------------------------------------------

CONTENT_CHECKS = ["content.secrets", "content.pii", "content.prohibited",
                  "content.injection", "content.fraud", "content.spam"]


class CheckLog:
    """Collects a named PASS/WARN/FAIL record for every check, so the report
    states *exactly* what was verified — not just a final banner."""

    def __init__(self):
        self.records = []

    def record(self, name, status, detail=""):
        self.records.append({"check": name, "status": status, "detail": detail})
        tag = {"PASS": "✓", "WARN": "!", "FAIL": "✗"}.get(status, "?")
        print(f"  [{tag}] {name}" + (f" — {detail}" if detail else ""))

    def guard(self, name, fn, ok_detail=""):
        try:
            detail = fn() or ok_detail
            self.record(name, "PASS", str(detail))
            return True
        except Exception as e:  # noqa: BLE001
            self.record(name, "FAIL", str(e))
            return False

    @property
    def failed(self):
        return [r for r in self.records if r["status"] == "FAIL"]

    @property
    def warned(self):
        return [r for r in self.records if r["status"] == "WARN"]


def _load_manifest(ledger_root, dossier):
    rel = dossier.get("soul_manifest_rel_path")
    if not rel:
        return None
    try:
        return core.load_json_strict(os.path.join(ledger_root, rel))
    except (OSError, ValueError):
        return None


def run_verification(base_dir: str, *, skip_rpc: bool = False, rpc_scope: str = "auto",
                     rpc: RpcFn = rpc_call, report_path: Optional[str] = None,
                     check_anchors: bool = True) -> Dict[str, Any]:
    """Run every invariant over the ledger at base_dir and return the report.

    rpc_scope:
      "all"     — settle every slot on chain (weekly schedule / manual runs);
      "changed" — settle only the slots this PR touches (PR and push runs);
      "auto"    — "changed" when a PR diff is available, else "all".
    """
    slot_dir = core.ledger_w1_dir(base_dir)
    ledger_root = os.path.join(base_dir, "ledger")
    log = CheckLog()

    # PR scope + the set of slots this change touches.
    entries = changed_entries()
    touched: Set[str] = set()
    if entries:
        try:
            touched = check_scope(entries)
            log.record("pr.scope_containment", "PASS",
                       f"scope contained to slot: {sorted(touched)[0]}" if touched
                       else "no slot touched (infrastructure-only change)")
        except ValueError as e:
            log.record("pr.scope_containment", "FAIL", str(e))
            # Still treat every slot named in the diff as "touched" so the
            # strict per-slot checks run against it.
            for _, path in entries:
                for rx in (SLOT_FILE_RE, ICON_FILE_RE, SOUL_FILE_RE):
                    m = rx.match(path)
                    if m:
                        touched.add(m.group(1))
    else:
        log.record("pr.scope_containment", "PASS", "no PR diff to check")

    if rpc_scope == "auto":
        rpc_scope = "changed" if entries is not None else "all"
    online_slots: Optional[Set[str]] = None if rpc_scope == "all" else touched

    slots = core.list_slot_files(slot_dir)
    print(f"[*] Inspecting {len(slots)} claimed slot(s) in ledger/w1...")

    # Replay protection across EVERY tx field of EVERY slot (R-12).
    settlements, conflicts = core.build_settlements(base_dir)
    if conflicts:
        for c in conflicts:
            log.record("ledger.replay_protection", "FAIL",
                       f"tx {c['tx_hash'][:18]}… cited by {c['slots']}")
    else:
        log.record("ledger.replay_protection", "PASS",
                   f"{settlements['count']} settlement tx(s), each cites exactly one slot")

    head_block: Optional[int] = None
    if not skip_rpc and online_slots != set():
        try:
            head_block = int(rpc("eth_blockNumber", []), 16)
        except Exception as e:  # noqa: BLE001
            log.record("rpc.head_block", "FAIL", f"could not read chain head: {e}")

    for s_file in slots:
        path = os.path.join(slot_dir, s_file)
        size = os.path.getsize(path)
        if size > core.MAX_DOSSIER_BYTES:
            log.record(s_file + ".size", "FAIL",
                       f"{size} bytes exceeds the {core.MAX_DOSSIER_BYTES}-byte dossier cap")
            continue
        try:
            data = core.load_json_strict(path)
        except ValueError as e:
            log.record(s_file + ".parse", "FAIL", str(e))
            continue
        if not isinstance(data, dict):
            log.record(s_file + ".parse", "FAIL", "dossier is not a JSON object")
            continue
        slot_id = str(data.get("slot_id") or s_file[:-5])
        print(f"\n---> Validating {slot_id} ({data.get('moniker')})")

        log.guard(slot_id + ".identity",
                  lambda f=s_file, d=data: verify_slot_identity(f, d, slot_dir))
        log.guard(slot_id + ".schema", lambda d=data: validate_dossier(d), "schema 1.1.x")
        log.guard(slot_id + ".reserved_grant",
                  lambda s=slot_id, d=data: enforce_reserved_slots(s, d))
        log.guard(slot_id + ".soul_manifest",
                  lambda d=data: verify_soul_manifest(d, ledger_root))
        manifest = _load_manifest(ledger_root, data)
        try:
            log.record(slot_id + ".soul_manifest_hygiene", "PASS", soul_manifest_hygiene(manifest))
        except ValueError as e:
            # Blocking for the slot this change touches; advisory for history
            # (and for a checkout with no diff context at all).
            severity = "FAIL" if slot_id in touched else "WARN"
            log.record(slot_id + ".soul_manifest_hygiene", severity, str(e))

        prev = previous_dossier(slot_id)
        icon_path = os.path.join(slot_dir, f"{slot_id}.webp")
        new_icon = open(icon_path, "rb").read() if os.path.exists(icon_path) else None
        prev_icon = previous_icon(slot_id) if prev is not None else None
        log.guard(slot_id + ".append_only",
                  lambda s=slot_id, d=data, p=prev, ni=new_icon, pi=prev_icon:
                  verify_append_only(s, d, p, ni, pi))
        log.guard(slot_id + ".ownership_lineage",
                  lambda s=slot_id, d=data, p=prev: verify_ownership_lineage(
                      s, d, slot_dir, p, rpc=rpc,
                      online=(not skip_rpc) and (online_slots is None or s in online_slots)))

        log.guard(slot_id + ".icon_structure",
                  lambda p=icon_path: verify_icon(p))

        # Content safety — deterministic zero-LLM pre-screen for the human gate.
        findings = safety.screen_dossier(data, manifest, base_dir=base_dir)
        for cat in CONTENT_CHECKS:
            catf = [f for f in findings if f["check"] == cat]
            blocks = [f for f in catf if f["severity"] == "block"]
            if blocks:
                log.record(slot_id + "." + cat, "FAIL",
                           "; ".join(f["field"] + ": " + f["detail"] for f in blocks))
            elif catf:
                log.record(slot_id + "." + cat, "WARN",
                           "; ".join(f["field"] + ": " + f["detail"] for f in catf))
            else:
                log.record(slot_id + "." + cat, "PASS")

        if skip_rpc:
            log.record(slot_id + ".settlement_base_rpc", "WARN", "skipped (--skip-rpc)")
        elif online_slots is not None and slot_id not in online_slots:
            log.record(slot_id + ".settlement_base_rpc", "PASS",
                       "unchanged in this PR; settled when inscribed (full re-check runs on schedule)")
        else:
            def _settle(d=data):
                r = verify_base_tx(d["base_tx_hash"], d["wallet_address"],
                                   OFFICIAL_TREASURY_ADDRESS, 1.00, rpc=rpc, head_block=head_block)
                return f"confirmed on Base, block {r['block_number']}, {r['confirmations']} confirmations"
            log.guard(slot_id + ".settlement_base_rpc", _settle)

    log.guard("state.root_integrity",
              lambda: verify_state_files(base_dir) or
              "state.json, index.json & settlements.json match derivation")

    # Anchors: shape offline, existence/signer/calldata on chain.
    try:
        anchors = load_anchors(base_dir)
    except ValueError as e:
        anchors = []
        log.record("anchors.record", "FAIL", str(e))
    for i, entry in enumerate(anchors):
        log.guard(f"anchors[{i}].record", lambda e=entry: verify_anchor_record(e))
    if anchors and check_anchors:
        if skip_rpc:
            log.record("anchors.on_chain", "WARN", "skipped (--skip-rpc)")
        else:
            to_check = anchors if rpc_scope == "all" else anchors[-1:]
            for entry in to_check:
                log.guard(f"anchors.on_chain[{entry.get('tx_hash', '')[:12]}]",
                          lambda e=entry: verify_anchor_on_chain(e, rpc=rpc))

    root = core.compute_merkle_root(slot_dir, slots) if slots else core.EMPTY_ROOT
    if anchors:
        if anchors[-1].get("root") == root:
            log.record("anchors.current_root", "PASS", f"root {root[:12]}… is anchored")
        else:
            log.record("anchors.current_root", "WARN",
                       f"root {root[:12]}… is not yet anchored (latest anchor {str(anchors[-1].get('root'))[:12]}…)")
    else:
        log.record("anchors.current_root", "WARN", "no anchors recorded yet")

    print("\n" + "=" * 64)
    print("VERIFICATION REPORT")
    print("-" * 64)
    print(f"  checks run : {len(log.records)}")
    print(f"  passed     : {sum(1 for r in log.records if r['status'] == 'PASS')}")
    print(f"  warnings   : {len(log.warned)}")
    print(f"  failed     : {len(log.failed)}")
    print("-" * 64)
    for r in log.warned:
        print(f"  ! {r['check']}: {r['detail']}")
    for r in log.failed:
        print(f"  ✗ {r['check']}: {r['detail']}")

    report = {
        "wall_id": core.WALL_ID,
        "slots_validated": len(slots),
        "merkle_root": root,
        "rpc_scope": "skipped" if skip_rpc else rpc_scope,
        "result": "FAIL" if log.failed else "PASS",
        "checks": log.records,
    }
    if report_path:
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
            fh.write("\n")
        print("  report written: " + report_path)
    print("=" * 64)
    return report


def main():
    argv = sys.argv[1:]
    skip_rpc = "--skip-rpc" in argv
    report_path = None
    if "--report" in argv:
        report_path = argv[argv.index("--report") + 1]
    rpc_scope = "auto"
    if "--rpc-scope" in argv:
        rpc_scope = argv[argv.index("--rpc-scope") + 1]
        if rpc_scope not in ("auto", "all", "changed"):
            print(f"[✗] --rpc-scope must be auto|all|changed (got {rpc_scope})", file=sys.stderr)
            sys.exit(2)
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    report = run_verification(base_dir, skip_rpc=skip_rpc, rpc_scope=rpc_scope,
                              report_path=report_path)
    failed = [c for c in report["checks"] if c["status"] == "FAIL"]
    if failed:
        print(f"[✗] VERIFICATION FAILED — {len(failed)} blocking check(s). See report above.")
        sys.exit(1)

    print("[✓] ALL VERIFICATIONS PASSED (zero LLMs / strict provenance).")
    print(f"[✓] Claimed slots: {report['slots_validated']} / {core.TOTAL_SLOTS} | "
          f"Merkle root: {report['merkle_root']}")
    print("=" * 64)


if __name__ == "__main__":
    main()
