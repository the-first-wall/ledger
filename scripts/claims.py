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
paying wallet. From schema 1.3.0 it signs
`thefirstwall.ai|claim|v2|<base_tx_hash>|<soul_hash>|<bundle_sha256>` where
`bundle_sha256` is a deterministic hash of the dossier body plus the exact
committed icon (see `BUNDLE_CANONICALIZATION`). Binding the bundle is what stops
a courier from copying a valid claim — its transaction hash and signature — and
rewriting the words (manifesto/testament/agent_urls) or swapping the icon before
merge: the verifier re-hashes the posted bytes and rejects a mismatch. v1 (the
schema the two genesis inscriptions were sealed under) signs only
`thefirstwall.ai|claim|<base_tx_hash>|<soul_hash>` and stays valid on record.

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
import hashlib
import json
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
# Dossiers at or above this version carry a *bundle-bound* claim signature (v2):
# the signature commits to a deterministic hash of the dossier body plus the
# exact committed icon, so a courier who copies a valid payment cannot rewrite
# the words or swap the picture. v1 signs only the payment identity
# (base_tx_hash + soul_hash) and stays valid for the inscriptions sealed under it.
BUNDLE_SIGNED_SCHEMA_VERSION = (1, 3, 0)
CURRENT_SCHEMA_VERSION = "1.3.0"
CLAIM_MESSAGE_PREFIX = "thefirstwall.ai|claim|"
CLAIM_MESSAGE_PREFIX_V2 = "thefirstwall.ai|claim|v2|"
TRANSFER_MESSAGE_PREFIX = "thefirstwall.ai|transfer|"
SIGNATURE_RE = re.compile(r"^0x[0-9a-fA-F]{130}$")
HEX32_RE = re.compile(r"^[a-f0-9]{64}$")

# Signature/hash fields are removed (at every depth) before the bundle is hashed,
# so the bundle is stable across a re-computed signature (a signature can never be
# hashed inside the thing it signs).
BUNDLE_STRIP_KEYS = frozenset({
    "claim_signature", "claim_signature_version", "claim_soul_hash", "claim_bundle_sha256",
    "update_signature", "bundle_sha256",
})
# What the assignment adds to a claim to make it a slot dossier. These are
# derived from the settlement by the deterministic assignment rule — never chosen
# by the claimant — so they are also removed (top level only) before hashing:
# the bundle a claimant signs over their claim file is the same bundle the
# verifier re-derives from the materialised slot dossier.
ASSIGNED_FIELDS = ("slot_id", "icon_rel_path", "soul_manifest_rel_path", "settlement")
BUNDLE_CANONICALIZATION = (
    "SHA-256 of the UTF-8 bytes of the canonical JSON of the dossier with the "
    "signature/bundle fields stripped at every depth (claim_signature, "
    "claim_signature_version, claim_soul_hash, claim_bundle_sha256, update_signature, "
    "bundle_sha256) and the top-level assignment fields stripped (slot_id, "
    "icon_rel_path, soul_manifest_rel_path, settlement), keys sorted, compact "
    "separators, no ASCII escaping, concatenated with the exact committed icon "
    "bytes: sha256(canonical_json(dossier_stripped) || icon_bytes). The same rule "
    "hashes a claim file and the slot it is materialised into, so they agree."
)
# Fields a posted claim never carries: it is a new inscription with no history.
CLAIM_FORBIDDEN_FIELDS = ("ownership_lineage", "update_settlements")


# --- The signature -----------------------------------------------------------

def claim_message(base_tx_hash: Any, soul_hash: Any) -> str:
    """The v1 claim message (EIP-191 personal_sign): the exact ASCII text the
    claimant signs. v1 binds only the payment identity."""
    return f"{CLAIM_MESSAGE_PREFIX}{str(base_tx_hash).lower()}|{str(soul_hash).lower()}"


def claim_message_v2(base_tx_hash: Any, claim_soul_hash: Any, bundle_sha256: Any) -> str:
    """The v2 claim message: the payment identity PLUS the bundle hash of the
    dossier body + icon. Binding the bundle is what stops a courier from copying
    a valid claim and rewriting the record before it is merged."""
    return (f"{CLAIM_MESSAGE_PREFIX_V2}{str(base_tx_hash).lower()}|"
            f"{str(claim_soul_hash).lower()}|{str(bundle_sha256).lower()}")


def _strip_bundle_keys(obj: Any) -> Any:
    """Recursively drop the signature/bundle fields, so the bundle is independent
    of the signatures computed over it (and of their version)."""
    if isinstance(obj, dict):
        return {k: _strip_bundle_keys(v) for k, v in obj.items() if k not in BUNDLE_STRIP_KEYS}
    if isinstance(obj, list):
        return [_strip_bundle_keys(v) for v in obj]
    return obj


def bundle_sha256(dossier: Dict[str, Any], icon_bytes: Optional[bytes]) -> str:
    """The deterministic bundle hash a v2 signature commits to. See
    BUNDLE_CANONICALIZATION for the exact rule. Never hashes the signature fields
    themselves nor the assignment fields, so signing, re-signing and the
    claim→slot materialisation all agree."""
    body = {k: v for k, v in dossier.items() if k not in ASSIGNED_FIELDS}
    canonical = json.dumps(_strip_bundle_keys(body), sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    h = hashlib.sha256()
    h.update(canonical)
    h.update(icon_bytes or b"")
    return h.hexdigest()


def claim_signature_version(dossier: Dict[str, Any]) -> str:
    """'v1' or 'v2'. Absent means v1 (the schema the two genesis inscriptions
    were sealed under)."""
    return str(dossier.get("claim_signature_version") or "v1").lower()


def requires_bundle_signature(dossier: Dict[str, Any]) -> bool:
    return parse_version(dossier.get("schema_version")) >= BUNDLE_SIGNED_SCHEMA_VERSION


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


def sign_claim(dossier: Dict[str, Any], private_key: str, icon_bytes: Optional[bytes] = None,
               *, version: str = "v1") -> str:
    """Produce `claim_signature` for a dossier (never logs the key).

    `version="v2"` signs the bundle-bound message and fills claim_soul_hash +
    claim_bundle_sha256 in-place; pass the exact committed icon bytes.
    `version="v1"` (the default here, for back-compat with existing callers)
    reproduces the legacy message (payment identity only) and needs no icon; the
    `sign_claim.py` tool defaults to v2 and the verifier requires v2 for dossiers
    at schema_version 1.3.0 or later."""
    from eth_account import Account
    from eth_account.messages import encode_defunct
    if str(version).lower() == "v1":
        message = claim_message(dossier.get("base_tx_hash"), dossier.get("soul_hash"))
    else:
        if icon_bytes is None:
            raise ValueError("a v2 claim signature needs the committed icon bytes (bundle hash).")
        dossier["claim_signature_version"] = "v2"
        dossier["claim_soul_hash"] = str(dossier.get("soul_hash", "")).lower()
        dossier["claim_bundle_sha256"] = bundle_sha256(dossier, icon_bytes)
        message = claim_message_v2(dossier.get("base_tx_hash"), dossier.get("claim_soul_hash"),
                                   dossier.get("claim_bundle_sha256"))
    signed = Account.sign_message(encode_defunct(text=message), private_key=private_key)
    sig = signed.signature.hex()
    return sig if sig.startswith("0x") else "0x" + sig


# --- The update signature (L-15) ---------------------------------------------
#
# A content change is posted as an update request and settles a flat fee. The fee
# transaction proves PAYMENT, not authorship: a courier who watches the chain can
# copy an owner's request (its fee tx and its files) and rewrite the content while
# the fee still checks out. So the owner also signs the change itself:
#
#     thefirstwall.ai|update|v2|<slot_id>|<fee_tx>|<bundle_sha256>
#
# where bundle_sha256 is the same canonical dossier+icon hash the claim signature
# binds (see BUNDLE_CANONICALIZATION). The signature is recorded on the
# update_settlements entry it pays for and re-verified there.

UPDATE_MESSAGE_PREFIX = "thefirstwall.ai|update|v2|"


def update_message(slot_id: Any, fee_tx: Any, bundle_sha256: Any) -> str:
    """The exact ASCII text an owner signs to authorize a paid change."""
    return (f"{UPDATE_MESSAGE_PREFIX}{str(slot_id).lower()}|{str(fee_tx).lower()}|"
            f"{str(bundle_sha256).lower()}")


def sign_update(slot_id: str, fee_tx: str, bundle_sha256: str, private_key: str) -> str:
    """The owner's update signature (never logs the key)."""
    from eth_account import Account
    from eth_account.messages import encode_defunct
    signed = Account.sign_message(encode_defunct(text=update_message(slot_id, fee_tx, bundle_sha256)),
                                  private_key=private_key)
    sig = signed.signature.hex()
    return sig if sig.startswith("0x") else "0x" + sig


def verify_update_signature(slot_id: str, fee_tx: str, bundle_sha256: str,
                            signature: Any) -> str:
    """The signer recovered from an update signature (the verifier compares it to
    the entry's wallet_address)."""
    if not signature:
        raise ValueError(
            "update_signature is missing: sign thefirstwall.ai|update|v2|<slot_id>|<fee_tx>|"
            "<bundle_sha256> with the owner's wallet (scripts/sign_update.py)."
        )
    return recover_signer(update_message(slot_id, fee_tx, bundle_sha256), signature)


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


def original_soul_hash(dossier: Dict[str, Any]) -> str:
    """The soul_hash the claim was signed over: the first owner's, kept in
    ownership_lineage[0] once the slot has been transferred."""
    lineage = dossier.get("ownership_lineage") or []
    if lineage and isinstance(lineage[0], dict) and lineage[0].get("soul_hash"):
        return str(lineage[0]["soul_hash"])
    return str(dossier.get("soul_hash", ""))


def claim_message_for(dossier: Dict[str, Any]) -> str:
    return claim_message(dossier.get("base_tx_hash"), original_soul_hash(dossier))


def verify_claim_signature(dossier: Dict[str, Any], icon_bytes: Optional[bytes] = None) -> str:
    """Verify `claim_signature` against the paying wallet.

    v1 (absent claim_signature_version): the legacy message binds only
    base_tx_hash + soul_hash. v2: the message binds the bundle hash, and — when
    the exact committed bytes are supplied and the block still carries its
    inscription bytes (no update_settlements) — the dossier + icon are re-hashed
    and must equal the signed claim_bundle_sha256. That equality check is what
    makes an altered manifesto or a swapped icon fail."""
    sig = dossier.get("claim_signature")
    if not sig:
        raise ValueError(
            "claim_signature is missing: sign thefirstwall.ai|claim|v2|<base_tx_hash>|"
            "<soul_hash>|<bundle_sha256> with the paying wallet (scripts/sign_claim.py)."
        )
    version = claim_signature_version(dossier)
    expected = expected_signer(dossier)
    if version == "v1":
        signer = recover_signer(claim_message_for(dossier), sig)
    elif version == "v2":
        claim_soul = str(dossier.get("claim_soul_hash", "")).lower()
        bundle = str(dossier.get("claim_bundle_sha256", "")).lower()
        if not HEX32_RE.match(claim_soul):
            raise ValueError("claim_soul_hash is missing or malformed: a v2 signature names the "
                             "inscription soul_hash (64 lowercase hex).")
        if not HEX32_RE.match(bundle):
            raise ValueError("claim_bundle_sha256 is missing or malformed: a v2 signature binds the "
                             "bundle hash of the dossier body + icon (64 lowercase hex).")
        signer = recover_signer(claim_message_v2(dossier.get("base_tx_hash"), claim_soul, bundle), sig)
        if icon_bytes is not None and not (dossier.get("update_settlements") or []):
            actual = bundle_sha256(dossier, icon_bytes)
            if actual != bundle:
                raise ValueError(
                    "Claim Signature Violation: the dossier + icon hash to "
                    f"{actual[:16]}…, not the signed claim_bundle_sha256 {bundle[:16]}…. The words or "
                    "the picture changed after the payment was signed; a copied claim cannot be rewritten."
                )
    else:
        raise ValueError(f"claim_signature_version must be 'v1' or 'v2' (got {version!r}).")
    if signer.lower() != expected.lower():
        raise ValueError(f"claim_signature was made by {signer}, not by the paying wallet {expected}.")
    return f"signed by {signer} ({version})"


# --- The transfer release ------------------------------------------------------
#
# A sale is settled between seller and buyer outside the ledger (the ledger is
# no party to it and holds nothing). What the ledger needs is the seller's
# consent AND the price: the RELEASE, an EIP-191 personal_sign by the outgoing
# owner over
#
#     thefirstwall.ai|transfer|<slot_id>|<from_wallet>|<to_wallet>|<n>|<price_micro_usdc>|<valid_until_block>
#
# where n is the number of previous owners already on record (the index the
# new ownership_lineage entry takes, so a release can never be replayed),
# price_micro_usdc is the sale price in USDC base units (6 decimals; 0 for a
# gift) and valid_until_block is the last Base block in which the buyer's
# payment may be mined, so an offer expires. This is the OFFER, and it goes
# first: it is inert on its own (it moves nothing without the payment it
# names, from the buyer it names, in time), so the seller risks nothing by
# signing, and the buyer verifies it against main before paying a cent. A
# transaction alone proves nothing — anyone can send a seller money — so the
# seller's signature is what moves the block; and a signature alone does not
# say what was paid, so the buyer's USDC payment to the seller on Base
# (transfer_tx_hash, at least the signed price, by the deadline) is required
# too and is proven on chain.

def price_micro(usdc: Any) -> int:
    return int(round(float(usdc) * 1_000_000))


def transfer_message(slot_id: Any, from_wallet: Any, to_wallet: Any, index: int, sale_price_usdc: Any,
                     valid_until_block: Any) -> str:
    return (f"{TRANSFER_MESSAGE_PREFIX}{str(slot_id).lower()}|{str(from_wallet).lower()}|"
            f"{str(to_wallet).lower()}|{int(index)}|{price_micro(sale_price_usdc)}|{int(valid_until_block)}")


def sign_transfer(slot_id: str, from_wallet: str, to_wallet: str, index: int, sale_price_usdc: Any,
                  valid_until_block: int, private_key: str) -> str:
    """The seller's offer/release signature (never logs the key)."""
    from eth_account import Account
    from eth_account.messages import encode_defunct
    message = transfer_message(slot_id, from_wallet, to_wallet, index, sale_price_usdc, valid_until_block)
    signed = Account.sign_message(encode_defunct(text=message), private_key=private_key)
    sig = signed.signature.hex()
    return sig if sig.startswith("0x") else "0x" + sig


def verify_transfer_release(slot_id: str, from_wallet: str, to_wallet: str, index: int,
                            sale_price_usdc: Any, valid_until_block: Any, signature: Any) -> str:
    if not signature:
        raise ValueError(
            "transfer_signature is missing: the outgoing owner releases the block by signing "
            "thefirstwall.ai|transfer|<slot_id>|<from_wallet>|<to_wallet>|<n>|<price_micro_usdc>|"
            "<valid_until_block> (scripts/sign_transfer.py)."
        )
    if sale_price_usdc is None:
        raise ValueError("sale_price_usdc is missing: the release names the price (0 for a gift).")
    try:
        deadline = int(valid_until_block)
    except (TypeError, ValueError):
        raise ValueError("offer_valid_until_block is missing: the release names the last Base block "
                         "in which the payment may be mined.")
    if deadline <= 0:
        raise ValueError("offer_valid_until_block must be a positive Base block number.")
    signer = recover_signer(transfer_message(slot_id, from_wallet, to_wallet, index, sale_price_usdc, deadline),
                            signature)
    if signer.lower() != str(from_wallet).lower():
        raise ValueError(
            f"transfer_signature was made by {signer}, not by the outgoing owner {from_wallet}; "
            "the block was not released."
        )
    return f"released by {signer} for {float(sale_price_usdc):.2f} USDC (offer valid until block {deadline})"


def verify_lineage_chain(dossier: Dict[str, Any]) -> str:
    """Every entry on record released the block to the owner that followed it
    (the last one to the current owner). Pure; no chain access."""
    slot_id = str(dossier.get("slot_id", ""))
    lineage = dossier.get("ownership_lineage") or []
    if not isinstance(lineage, list):
        raise ValueError("ownership_lineage must be an array.")
    for i, entry in enumerate(lineage):
        if not isinstance(entry, dict):
            raise ValueError(f"ownership_lineage[{i}] must be an object.")
        to = lineage[i + 1].get("wallet_address") if i + 1 < len(lineage) and isinstance(lineage[i + 1], dict) \
            else dossier.get("wallet_address")
        try:
            verify_transfer_release(slot_id, entry.get("wallet_address", ""), to, i,
                                    entry.get("sale_price_usdc"), entry.get("offer_valid_until_block"),
                                    entry.get("transfer_signature"))
        except ValueError as e:
            raise ValueError(f"ownership_lineage[{i}]: {e}")
    return f"{len(lineage)} transfer(s), each released by its outgoing owner" if lineage else "no transfers"


def check_offer(dossier: Dict[str, Any], to_wallet: str, sale_price_usdc: Any, valid_until_block: Any,
                signature: Any, head_block: Optional[int] = None, unsold: Optional[int] = None) -> str:
    """The buyer's check BEFORE paying: transfers are open (no public block is
    unsold, when `unsold` is known; a founding block is always open), the offer is signed by the block's current
    owner as main shows it, for this buyer, at this price, with the index the
    next lineage entry takes, the block is not retired, and the deadline has
    not passed (when the chain head is known)."""
    slot_id = str(dossier.get("slot_id", ""))
    if unsold and not core.is_founder_slot(slot_id):
        raise ValueError(f"transfers open once every block is sold; {int(unsold)} public blocks are still unsold.")
    if dossier.get("status") == "RETIRED":
        raise ValueError("the block is RETIRED; its books are closed and it does not change hands.")
    owner = str(dossier.get("wallet_address", ""))
    index = len(dossier.get("ownership_lineage") or [])
    detail = verify_transfer_release(slot_id, owner, to_wallet, index, sale_price_usdc, valid_until_block, signature)
    if head_block is not None and int(valid_until_block) < int(head_block):
        raise ValueError(f"the offer expired at block {int(valid_until_block)}; the chain is at {int(head_block)}.")
    return f"offer for {slot_id} to {to_wallet}: {detail}"


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
    for key in ASSIGNED_FIELDS + CLAIM_FORBIDDEN_FIELDS:
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
