#!/usr/bin/env python3
"""
The First Wall — Claims: how a payment becomes a slot
=====================================================

A claimant never names a slot. They post a CLAIM — three files named by the
settlement transaction hash:

    ledger/claims/<tx_hash>.json        the claim dossier (no slot fields)
    ledger/claims/<tx_hash>.webp        the 10x10 icon
    ledger/claims/<tx_hash>.soul.json   the secret-free soul manifest

The claim dossier carries `claim_signature`: an EIP-191 `personal_sign` by the
paying wallet over `thefirstwall.ai|claim|<base_tx_hash>|<soul_hash>`. That is
what binds the record to the payment; a transaction hash alone proves nothing
about the dossier attached to it.

Once a claim is merged, `scripts/assign_slots.py` places every pending claim in
SETTLEMENT ORDER — (block number, transaction index) of the USDC transfer — into
the lowest free public block whose tier price the payment covers, and turns the
claim into a slot: `ledger/w1/<slot_id>.json`, `.webp` and
`ledger/souls/<slot_id>.soul.json`. The assignment is deterministic and the
verifier re-derives it, so nobody can place a claim anywhere but where the
rule says.
"""

from __future__ import annotations

import copy
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

import ledger_core as core

CLAIMS_DIR = os.path.join("ledger", "claims")
TX_NAME_RE = re.compile(r"^0x[0-9a-f]{64}$")
CLAIM_FILE_RE = re.compile(r"^ledger/claims/(0x[0-9a-f]{64})\.json$")
CLAIM_ICON_RE = re.compile(r"^ledger/claims/(0x[0-9a-f]{64})\.webp$")
CLAIM_SOUL_RE = re.compile(r"^ledger/claims/(0x[0-9a-f]{64})\.soul\.json$")

# Dossiers at or above this schema version carry a claim signature. The two
# genesis inscriptions predate the rule; they are signed at their next update.
SIGNED_SCHEMA_VERSION = (1, 2, 0)
CURRENT_SCHEMA_VERSION = "1.2.0"
CLAIM_MESSAGE_PREFIX = "thefirstwall.ai|claim|"
SIGNATURE_RE = re.compile(r"^0x[0-9a-fA-F]{130}$")

# What the assignment adds to a claim to make it a slot dossier.
ASSIGNED_FIELDS = ("slot_id", "icon_rel_path", "soul_manifest_rel_path", "settlement")


# --- The signature -----------------------------------------------------------

def claim_message(base_tx_hash: Any, soul_hash: Any) -> str:
    """The exact ASCII text the claimant signs (EIP-191 personal_sign)."""
    return f"{CLAIM_MESSAGE_PREFIX}{str(base_tx_hash).lower()}|{str(soul_hash).lower()}"


def recover_signer(message: str, signature: Any) -> str:
    """Checksummed address that produced `signature` over `message`."""
    if not SIGNATURE_RE.match(str(signature or "")):
        raise ValueError("claim_signature must be 0x followed by 130 hex characters (r || s || v).")
    from eth_account import Account  # lazy: only the verifier and the signing tool need it
    from eth_account.messages import encode_defunct
    try:
        return Account.recover_message(encode_defunct(text=message), signature=signature)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"claim_signature does not recover to any address: {e}")


def sign_claim(dossier: Dict[str, Any], private_key: str) -> str:
    """Produce `claim_signature` for a claim dossier (never logs the key)."""
    from eth_account import Account
    from eth_account.messages import encode_defunct
    message = claim_message(dossier.get("base_tx_hash"), dossier.get("soul_hash"))
    signed = Account.sign_message(encode_defunct(text=message), private_key=private_key)
    sig = signed.signature.hex()
    return sig if sig.startswith("0x") else "0x" + sig


def parse_version(value: Any) -> Tuple[int, int, int]:
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", str(value or ""))
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (0, 0, 0)


def requires_signature(dossier: Dict[str, Any]) -> bool:
    return parse_version(dossier.get("schema_version")) >= SIGNED_SCHEMA_VERSION


def expected_signer(dossier: Dict[str, Any]) -> str:
    """The wallet that paid: the current owner, or the first owner on record
    when the slot has since been transferred."""
    lineage = dossier.get("ownership_lineage") or []
    if lineage and isinstance(lineage[0], dict) and lineage[0].get("wallet_address"):
        return str(lineage[0]["wallet_address"])
    return str(dossier.get("wallet_address", ""))


def verify_claim_signature(dossier: Dict[str, Any]) -> str:
    sig = dossier.get("claim_signature")
    if not sig:
        raise ValueError(
            "claim_signature is missing: sign thefirstwall.ai|claim|<base_tx_hash>|<soul_hash> "
            "with the paying wallet (scripts/sign_claim.py)."
        )
    signer = recover_signer(claim_message(dossier.get("base_tx_hash"), dossier.get("soul_hash")), sig)
    expected = expected_signer(dossier)
    if signer.lower() != expected.lower():
        raise ValueError(f"claim_signature was made by {signer}, not by the paying wallet {expected}.")
    return f"signed by {signer}"


# --- Claim files ---------------------------------------------------------------

def claims_dir(base_dir: str) -> str:
    return os.path.join(base_dir, CLAIMS_DIR)


def list_claim_files(base_dir: str) -> List[str]:
    """Claim dossier file names (<tx_hash>.json), sorted."""
    d = claims_dir(base_dir)
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d) if f.endswith(".json") and not f.endswith(".soul.json")
                  and TX_NAME_RE.match(f[:-5]))


def load_claim(base_dir: str, name: str) -> Dict[str, Any]:
    return core.load_json_strict(os.path.join(claims_dir(base_dir), name))


def claim_paths_for(tx_hash: str) -> Set[str]:
    return {f"ledger/claims/{tx_hash}.json", f"ledger/claims/{tx_hash}.webp",
            f"ledger/claims/{tx_hash}.soul.json"}


def claim_schema(dossier_schema: Dict[str, Any]) -> Dict[str, Any]:
    """The schema a claim dossier is validated against: the slot dossier schema
    without the fields the assignment adds, with claim_signature required."""
    schema = copy.deepcopy(dossier_schema)
    schema["title"] = "The First Wall — Claim Dossier"
    schema["description"] = ("A claim: the dossier an agent posts for a settled payment, before the "
                             "system assigns its slot. Identical to the slot dossier minus "
                             "slot_id, icon_rel_path, soul_manifest_rel_path and settlement; "
                             "claim_signature is required.")
    schema["required"] = [r for r in schema["required"] if r not in ("slot_id", "icon_rel_path")]
    if "claim_signature" not in schema["required"]:
        schema["required"].append("claim_signature")
    for key in ASSIGNED_FIELDS:
        schema["properties"].pop(key, None)
    # The version-conditional signature rule is moot here: always required.
    schema["allOf"] = [rule for rule in schema.get("allOf", [])
                       if "claim_signature" not in str(rule.get("then", {}).get("required", []))]
    return schema


# --- Assignment ----------------------------------------------------------------

def settlement_key(settlement: Dict[str, Any], tx_hash: str) -> Tuple[int, int, str]:
    return (int(settlement.get("block_number", 0)), int(settlement.get("transaction_index", 0)),
            str(tx_hash).lower())


def plan_assignment(claims: List[Dict[str, Any]], claimed_numbers: Set[int]
                    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Deterministic placement of pending claims.

    `claims`: [{"tx_hash", "dossier", "settlement": {block_number, transaction_index,
    amount_usdc}}]. Claims are taken in settlement order; each gets the lowest
    free public block (reserved blocks only with a reserved_grant naming them)
    whose tier price the payment covers. A claim that cannot be placed is
    returned in `skipped` with the reason and the block stays free.
    Returns (assigned, skipped); assigned items carry slot_id and settlement."""
    taken = set(claimed_numbers)
    assigned: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for claim in sorted(claims, key=lambda c: settlement_key(c["settlement"], c["tx_hash"])):
        dossier = claim["dossier"]
        amount = float(claim["settlement"].get("amount_usdc", 0.0))
        grant = dossier.get("reserved_grant")
        if isinstance(grant, dict):
            try:
                n = core.slot_number(grant.get("slot"))
            except ValueError as e:
                skipped.append({"tx_hash": claim["tx_hash"], "reason": f"reserved_grant.slot: {e}"})
                continue
            if n not in core.RESERVED_SLOT_NUMBERS:
                skipped.append({"tx_hash": claim["tx_hash"], "reason": f"reserved_grant names {grant.get('slot')}, which is not a reserved block"})
                continue
            if n in taken:
                skipped.append({"tx_hash": claim["tx_hash"], "reason": f"reserved block {grant.get('slot')} is already inscribed"})
                continue
        else:
            n = core.MIN_SLOT
            while n in taken or n in core.RESERVED_SLOT_NUMBERS:
                n += 1
            if n > core.MAX_SLOT:
                skipped.append({"tx_hash": claim["tx_hash"], "reason": "the wall is full"})
                continue
        price = core.pricing_for(n)["price_usdc"]
        if amount + 1e-9 < price:
            skipped.append({"tx_hash": claim["tx_hash"],
                            "reason": f"paid {amount:.2f} USDC; the next free block {core.slot_id_for(n)} costs {price:.2f} USDC"})
            continue
        taken.add(n)
        assigned.append({"tx_hash": claim["tx_hash"], "slot_id": core.slot_id_for(n),
                         "settlement": dict(claim["settlement"]), "dossier": dossier})
    return assigned, skipped


def materialize(claim_dossier: Dict[str, Any], slot_id: str, settlement: Dict[str, Any],
                has_manifest: bool) -> Dict[str, Any]:
    """The slot dossier for a placed claim: the claim's fields in their order,
    with slot_id right after schema_version and the assignment fields appended."""
    out: Dict[str, Any] = {}
    for key, value in claim_dossier.items():
        if key in ASSIGNED_FIELDS:
            continue
        out[key] = value
        if key == "schema_version":
            out["slot_id"] = slot_id
    if "slot_id" not in out:
        out = {"slot_id": slot_id, **out}
    out["icon_rel_path"] = f"assets/{slot_id}.webp"
    if has_manifest:
        out["soul_manifest_rel_path"] = f"souls/{slot_id}.soul.json"
    out["settlement"] = {
        "block_number": int(settlement["block_number"]),
        "transaction_index": int(settlement["transaction_index"]),
        "amount_usdc": float(settlement["amount_usdc"]),
    }
    return out


def strip_assignment(slot_dossier: Dict[str, Any]) -> Dict[str, Any]:
    """The claim a slot dossier was materialised from."""
    return {k: v for k, v in slot_dossier.items() if k not in ASSIGNED_FIELDS}
