#!/usr/bin/env python3
"""
The First Wall — Deterministic Offline Verifier Engine (v1.6)
Zero LLMs. 100% prompt injection immune.

Invariants Enforced:
 1. Base Mainnet RPC Verification (USDC transfer, sender & recipient match,
    amount >= the tier price of the block, confirmation depth >= MIN_CONFIRMATIONS).
 2. PR Scope Containment (a PR posts ONE claim, or posts ONE update request for a
    block, or is the bot's assignment that turns pending claims into slots, or the
    bot's apply that turns pending requests into slot changes, or is
    infrastructure; nobody outside the bot touches a slot or the generated state;
    slot files are never deleted; a merged claim or request is never edited).
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
 9. State-root integrity (state.json, index.json, settlements.json equal their derivation;
    state.last_updated is the newest content timestamp in the ledger — vouchers and
    settled update/transfer fees included — re-derived independently of the generator).
10. Anchor integrity (recorded anchors exist on chain, were sent by the registered
    signer, and carry the recorded root).
11. Claim signature (a dossier is bound to its payment by an EIP-191 signature of
    the paying wallet over thefirstwall.ai|claim|<base_tx_hash>|<soul_hash>).
12. System assignment (a claimant never names a block: claims are placed in
    settlement order into the lowest free block their payment covers, and the
    verifier re-derives that placement).
13. Price ladder (the payment is at least the tier price of the assigned block;
    overpayment is accepted and never refunded).
14. Update fee (every change of an inscribed block by its owner settles one USDC
    transfer to pay_to of at least the flat update price — 1.00 USDC, the same
    for every block — recorded in update_settlements[]; one update, one fee).
15. Transfer release (a sale is settled between the parties outside the ledger;
    the outgoing owner's EIP-191 release signature naming buyer and price, the
    buyer's update fee and — when cited — the on-chain sale payment are what
    the ledger verifies; the former owner stays on record; a retired block is
    not transferable).
16. Identity (the wall's ERC-8004 identity, agentId 107162 in the Base registry,
    is still held by the documented holder and its on-chain tokenURI still
    points at the published registration file; both are mutable by the holder,
    so they are re-checked against the chain, never trusted from a document).

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
import tempfile
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import jsonschema
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger_core as core  # noqa: E402
import content_safety as safety  # noqa: E402
import claims  # noqa: E402
import provenance  # noqa: E402
import updates  # noqa: E402

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
# A claim is the same dossier before the system adds the block (claims.claim_schema).
CLAIM_SCHEMA = claims.claim_schema(DOSSIER_SCHEMA)
_CLAIM_VALIDATOR = jsonschema.Draft202012Validator(
    CLAIM_SCHEMA, format_checker=jsonschema.FormatChecker()
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
CLAIM_FILE_RES = (claims.CLAIM_FILE_RE, claims.CLAIM_ICON_RE, claims.CLAIM_SOUL_RE)
UPDATE_FILE_RES = updates.UPDATE_FILE_RES
# Files the generator rewrites whenever a slot lands or changes. Only the bot's
# assignment and apply pull requests carry them; an owner's pull request never does.
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
    # Empty (as on a push event) or absent must mean the base branch, not "origin/".
    base_ref = os.environ.get("GITHUB_BASE_REF", "").strip() or "main"
    return f"origin/{base_ref}...HEAD"


def changed_entries(rng: Optional[str] = None) -> Optional[List[Tuple[str, str]]]:
    """[(status, path)] for the diff range, or None when git cannot answer
    (no origin, not a repository)."""
    rng = rng or diff_range()
    try:
        res = subprocess.run(
            ["git", "diff", "--no-renames", "--name-status", rng],
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


def make_scope(kind: str, *, slots: Optional[Set[str]] = None, claims_: Optional[Set[str]] = None,
               assigned: Optional[Set[str]] = None, updates_: Optional[Set[str]] = None,
               applied: Optional[Set[str]] = None, withdrawn: Optional[Set[str]] = None) -> Dict[str, Any]:
    """The shape every scope carries, so callers never test for a missing key."""
    return {"kind": kind, "slots": slots or set(), "claims": claims_ or set(),
            "assigned": assigned or set(), "updates": updates_ or set(),
            "applied": applied or set(), "withdrawn": withdrawn or set()}


def scope_of(entries: List[Tuple[str, str]]) -> Dict[str, Any]:
    """Pure containment rule over a list of (status, path). Classifies the
    change and returns {"kind", "slots", "claims", "assigned", "updates",
    "applied", "withdrawn"}:

    * "claim"      — a claim is posted: exactly the files of ONE settlement
                     transaction (ledger/claims/<tx>.json, .webp, .soul.json),
                     all added, nothing else. A merged claim is never edited:
                     it is placed by the assignment, or it waits.
    * "update"     — an owner posts ONE request to change an existing block:
                     the files of ONE fee transaction (ledger/updates/<tx>.json
                     and, when they change, .webp and .soul.json), all added,
                     nothing else. The owner touches no slot and no generated
                     file; the apply puts the request in place. A merged
                     request is never edited.
    * "assignment" — pending claims become slots: the removed claim files, the
                     added slot files (any number; never a modified slot) and
                     the generated state, nothing else. verify_assignment
                     proves the mapping and the order.
    * "apply"      — pending requests become slot changes: the removed request
                     files, the modified slot files (each slot by exactly one
                     request) and the generated state, nothing else.
                     verify_apply proves each slot is its request, byte for byte.
    * "withdrawal" — pending requests are removed and nothing else changes: how
                     a request that can no longer be applied is cleared.
    * "infra"      — scripts/, schemas/, .github/, docs: no slot, claim or
                     request is touched.
    A slot is never changed directly and never added directly: a new inscription
    arrives as a claim and the system assigns its block; a change arrives as a
    request and the system applies it. The generated state (state.json,
    ledger/index.json, ledger/settlements.json, the canvas composite) is written
    only by the bot's assignment and apply. Slot dossiers, icons and soul
    manifests are never deleted; a claim file is deleted only by the assignment
    that places it, a request file only by the apply that applies it (or a
    withdrawal)."""
    slots: Set[str] = set()
    slot_status: Dict[str, Set[str]] = {}
    dossier_status: Dict[str, str] = {}
    claims_added: Set[str] = set()
    claims_deleted: Set[str] = set()
    claims_modified: Set[str] = set()
    updates_added: Set[str] = set()
    updates_deleted: Set[str] = set()
    updates_modified: Set[str] = set()
    paths: List[str] = []
    for status, path in entries:
        paths.append(path)
        for rx in (SLOT_FILE_RE, ICON_FILE_RE, SOUL_FILE_RE):
            m = rx.match(path)
            if m:
                if status.startswith("D"):
                    raise ValueError(f"PR Boundary Violation: {path} may never be deleted.")
                slots.add(m.group(1))
                slot_status.setdefault(m.group(1), set()).add(status[:1])
                if rx is SLOT_FILE_RE:
                    dossier_status[m.group(1)] = status[:1]
        for rx in CLAIM_FILE_RES:
            m = rx.match(path)
            if m:
                if status.startswith("D"):
                    claims_deleted.add(m.group(1))
                elif status.startswith("A"):
                    claims_added.add(m.group(1))
                else:
                    claims_modified.add(m.group(1))
        for rx in UPDATE_FILE_RES:
            m = rx.match(path)
            if m:
                if status.startswith("D"):
                    updates_deleted.add(m.group(1))
                elif status.startswith("A"):
                    updates_added.add(m.group(1))
                else:
                    updates_modified.add(m.group(1))

    if claims_modified:
        raise ValueError(
            f"PR Boundary Violation: a merged claim is never edited ({sorted(claims_modified)}); "
            "it is placed by the assignment, or it waits. To change what you posted, settle "
            "again and post a new claim."
        )
    if updates_modified:
        raise ValueError(
            f"PR Boundary Violation: a merged update request is never edited ({sorted(updates_modified)}); "
            "it is applied by the bot, or it waits. To change what you posted, pay a new fee and post "
            "a new request."
        )
    if (claims_added or claims_deleted) and (updates_added or updates_deleted):
        raise ValueError(
            "PR Boundary Violation: claims and update requests travel in separate PRs."
        )
    if claims_added and claims_deleted:
        raise ValueError(
            "PR Boundary Violation: a PR either posts a claim or assigns pending claims, never both."
        )
    if updates_added and updates_deleted:
        raise ValueError(
            "PR Boundary Violation: a PR either posts an update request or applies pending requests, never both."
        )

    if claims_added:
        if len(claims_added) > 1:
            raise ValueError(
                f"PR Boundary Violation: a PR posts ONE claim; found {len(claims_added)}: "
                f"{sorted(claims_added)}"
            )
        tx = next(iter(claims_added))
        stray = sorted(p for p in paths if p not in claims.claim_paths_for(tx))
        if stray:
            raise ValueError(
                f"PR Boundary Violation: a claim PR adds only ledger/claims/{tx}.json, .webp and "
                f".soul.json; it also touches: {stray}. The system assigns the block, and the "
                "generated state changes with the assignment, not with the claim."
            )
        return make_scope("claim", claims_={tx})

    if updates_added:
        if len(updates_added) > 1:
            raise ValueError(
                f"PR Boundary Violation: a PR posts ONE update request; found {len(updates_added)}: "
                f"{sorted(updates_added)}"
            )
        tx = next(iter(updates_added))
        stray = sorted(p for p in paths if p not in updates.update_paths_for(tx))
        if stray:
            raise ValueError(
                f"PR Boundary Violation: an update request PR adds only ledger/updates/{tx}.json and, "
                f"when they change, .webp and .soul.json; it also touches: {stray}. The owner never "
                "touches a slot or the generated state (state.json, ledger/index.json, "
                "ledger/settlements.json, the canvas composite): the bot applies the request and "
                "regenerates them."
            )
        if ("A", f"ledger/updates/{tx}.json") not in [(st[:1], p) for st, p in entries]:
            raise ValueError(
                f"PR Boundary Violation: a request is its dossier: ledger/updates/{tx}.json is missing."
            )
        return make_scope("update", updates_={tx})

    if claims_deleted:
        modified = sorted(sid for sid, st in slot_status.items() if st != {"A"})
        if modified:
            raise ValueError(
                f"PR Boundary Violation: an assignment only adds slots; it must not modify {modified}."
            )
        allowed: Set[str] = set(GENERATED_PATHS)
        for sid in slots:
            allowed |= slot_paths_for(sid)
        for tx in claims_deleted:
            allowed |= claims.claim_paths_for(tx)
        stray = sorted(p for p in paths if p not in allowed)
        if stray:
            raise ValueError(
                "PR Boundary Violation: an assignment changes only the placed claims' files, the "
                f"added slots' files and the generated state; it also touches: {stray}."
            )
        if not slots:
            raise ValueError(
                "PR Boundary Violation: a claim file is deleted only by the assignment that places "
                f"it, and no slot was added for {sorted(claims_deleted)}."
            )
        if len(slots) != len(claims_deleted):
            raise ValueError(
                f"PR Boundary Violation: {len(claims_deleted)} claim(s) removed but {len(slots)} "
                "slot(s) added; each placed claim becomes exactly one slot."
            )
        return make_scope("assignment", slots=slots, assigned=claims_deleted)

    if updates_deleted:
        if not slots:
            allowed = set()
            for tx in updates_deleted:
                allowed |= updates.update_paths_for(tx)
            stray = sorted(p for p in paths if p not in allowed)
            if stray:
                raise ValueError(
                    "PR Boundary Violation: a withdrawal only removes request files, and changes "
                    f"nothing else; it also touches: {stray}."
                )
            return make_scope("withdrawal", withdrawn=updates_deleted)
        notmod = sorted(sid for sid in slots if dossier_status.get(sid) != "M")
        if notmod:
            raise ValueError(
                f"PR Boundary Violation: an apply changes existing slots only; the dossier of {notmod} "
                "is not a modification."
            )
        allowed = set(GENERATED_PATHS)
        for sid in slots:
            allowed |= slot_paths_for(sid)
        for tx in updates_deleted:
            allowed |= updates.update_paths_for(tx)
        stray = sorted(p for p in paths if p not in allowed)
        if stray:
            raise ValueError(
                "PR Boundary Violation: an apply changes only the applied requests' files, the changed "
                f"slots' files and the generated state; it also touches: {stray}."
            )
        if len(slots) != len(updates_deleted):
            raise ValueError(
                f"PR Boundary Violation: {len(updates_deleted)} request(s) removed but {len(slots)} "
                "slot(s) changed; each applied request changes exactly one slot, and a slot takes "
                "one request at a time."
            )
        return make_scope("apply", slots=slots, applied=updates_deleted)

    if slots:
        if any(st.startswith("A") and pth.startswith("ledger/w1/") and pth.endswith(".json")
               for st, pth in entries):
            raise ValueError(
                "PR Boundary Violation: a new inscription is never added directly. A claimant never "
                "names a block: post ledger/claims/<base_tx_hash>.json, .webp and .soul.json instead, "
                "and the system assigns the block (skill.md, Step 4)."
            )
        raise ValueError(
            f"PR Boundary Violation: a block is never changed directly ({sorted(slots)}). Post an update "
            "request instead — ledger/updates/<fee_tx_hash>.json (and .webp / .soul.json when they "
            "change) — and the system applies it and regenerates the state (skill.md, \"Updating your "
            "own block\"). The generated state is never part of a pull request from outside."
        )
    return make_scope("infra")


def check_scope(entries: List[Tuple[str, str]]) -> Set[str]:
    """Containment rule; returns the set of slot ids touched (empty for a claim
    or infrastructure PR). See scope_of for the full classification."""
    return scope_of(entries)["slots"]


def scope_detail(scope: Dict[str, Any]) -> str:
    kind = scope["kind"]
    if kind == "claim":
        return "one claim posted: " + next(iter(scope["claims"]))[:12] + "…"
    if kind == "update":
        return "one update request posted: " + next(iter(scope["updates"]))[:12] + "…"
    if kind == "assignment":
        return (f"assignment: {len(scope['assigned'])} claim(s) -> "
                f"{', '.join(sorted(scope['slots']))}")
    if kind == "apply":
        return (f"apply: {len(scope['applied'])} request(s) -> "
                f"{', '.join(sorted(scope['slots']))}")
    if kind == "withdrawal":
        return f"withdrawal: {len(scope['withdrawn'])} pending request(s) removed"
    return "no slot, claim or request touched (infrastructure-only change)"


def enforce_pr_scope_containment() -> str:
    """Wrapper for the check log: resolves the diff and applies scope_of."""
    entries = changed_entries()
    if not entries:
        return "no PR diff to check"
    print(f"[*] Validating PR scope containment across {len(entries)} changed file(s)...")
    return scope_detail(scope_of(entries))


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

def base_ref() -> str:
    """The revision 'what main currently says' is read from: the left side of
    the diff range (the PR's base commit, or the pre-push commit on a push to
    main), falling back to origin/<base branch>."""
    rng = os.environ.get("TFW_DIFF_RANGE", "").strip()
    if "..." in rng:
        return rng.split("...", 1)[0]
    # GITHUB_BASE_REF is present-but-empty on a push event (and absent locally):
    # an empty value must fall back to the base branch, never to the bare
    # "origin/" ref, or `git show origin/:path` fails and every slot on main is
    # misread as a brand-new inscription.
    return f"origin/{os.environ.get('GITHUB_BASE_REF', '').strip() or 'main'}"


def _git_show(path: str) -> Optional[bytes]:
    try:
        res = subprocess.run(
            ["git", "show", f"{base_ref()}:{path}"],
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
                             prev_data: Optional[dict] = None, unsold: Optional[int] = None) -> str:
    """A transfer (wallet_address changed) is legible and consented:

    * transfers are open only once every public block is sold (`unsold` = the
      count of public blocks not yet inscribed, read from ledger_dir when not
      given): until then a cheap block would be passed from bot to bot. The ten
      founding blocks (#0001-#0010) are never sold at the public price, so they
      may change hands at any time (a partner hand-over, a key rotation);
    * ownership_lineage grows by exactly ONE entry, earlier entries byte-identical;
    * the new entry records the outgoing owner as inscribed — wallet_address,
      moniker, soul_hash and base_tx_hash equal what main says;
    * transfer_signature is the outgoing owner's offer and release: EIP-191
      over thefirstwall.ai|transfer|<slot_id>|<from>|<to>|<n>|<price_micro_usdc>|
      <valid_until_block>, n = the entry's index — consent, price and
      deadline in one signature;
    * a retired block is not transferable (its books are closed);
    * transfer_tx_hash is present: the buyer's USDC payment to the seller on
      Base for at least the signed price, proven by verify_sale_on_chain.
    The sale is settled between the parties; the ledger holds nothing and only
    attests what the seller signed and the chain shows. The buyer pays the
    ledger the transfer fee (verify_update_settlements)."""
    if prev_data is None:
        prev_data = previous_dossier(slot_id)
    if not prev_data:
        return "new inscription, no prior owner"

    prev_wallet = str(prev_data.get("wallet_address", "")).lower()
    new_wallet = str(new_data.get("wallet_address", "")).lower()
    if prev_wallet == new_wallet:
        return "same owner"
    if unsold is None:
        unsold = core.unsold_slots({core.slot_number(f[:-5]) for f in core.list_slot_files(ledger_dir)})
    if not core.transfers_open_for(slot_id, unsold):
        raise ValueError(
            f"Transfer Violation: transfers open once every block is sold; {unsold} of {core.TOTAL_SLOTS} "
            "public blocks are still unsold (state.json transfers_open: false). Claim a free block instead "
            "(only the founding blocks #0001-#0010 may change hands before then)."
        )
    if prev_data.get("status") == "RETIRED":
        raise ValueError(
            f"Transfer Violation: {slot_id} is RETIRED; its books are closed and it does not change hands "
            "(owner ruling pending on retired blocks)."
        )

    print(f"[*] Secondary transfer detected for {slot_id}: {prev_data.get('moniker')} -> {new_data.get('moniker')}")
    lineage = new_data.get("ownership_lineage")
    if not lineage or not isinstance(lineage, list):
        raise ValueError(
            f"Transfer Violation: Slot {slot_id} was previously owned by {prev_data.get('moniker')} "
            f"({prev_wallet}). The new owner MUST preserve the previous owner in 'ownership_lineage'."
        )
    prev_lineage = prev_data.get("ownership_lineage") or []
    if len(lineage) != len(prev_lineage) + 1:
        raise ValueError(
            f"Transfer Violation: ownership_lineage must grow by exactly one entry per transfer "
            f"(had {len(prev_lineage)}, now {len(lineage)}). History cannot be erased or invented."
        )
    for i, item in enumerate(prev_lineage):
        if item != lineage[i]:
            raise ValueError(f"Transfer Violation: tampering detected at ownership_lineage[{i}].")

    entry = lineage[-1]
    if not isinstance(entry, dict):
        raise ValueError("Transfer Violation: the new ownership_lineage entry must be an object.")
    if str(entry.get("wallet_address", "")).lower() != prev_wallet:
        raise ValueError(
            f"Transfer Violation: the new ownership_lineage entry must record the outgoing owner "
            f"({prev_data.get('moniker')}, {prev_wallet})."
        )
    for field in ("moniker", "soul_hash", "base_tx_hash"):
        if str(entry.get(field, "")).lower() != str(prev_data.get(field, "")).lower():
            raise ValueError(
                f"Transfer Violation: ownership_lineage entry {field} must equal the outgoing owner's "
                f"as inscribed ({prev_data.get(field)!r}); the old record is kept, not rewritten."
            )
    sale_tx = entry.get("transfer_tx_hash")
    if not isinstance(sale_tx, str) or not re.match(r"^0x[0-9a-fA-F]{64}$", sale_tx):
        raise ValueError(
            "Transfer Violation: transfer_tx_hash is required — the buyer's USDC payment to the seller on "
            "Base for the signed sale price. A release without the payment does not move the block."
        )
    index = len(prev_lineage)
    release = claims.verify_transfer_release(slot_id, prev_wallet, new_wallet, index,
                                             entry.get("sale_price_usdc"), entry.get("offer_valid_until_block"),
                                             entry.get("transfer_signature"))
    print(f"  [✓] Ownership lineage verified: {prev_data.get('moniker')} -> {new_data.get('moniker')} ({release}).")
    return f"transfer #{index + 1}: {release}"


# -----------------------------------------------------------------------------
# 3b. APPEND-ONLY HISTORY (sealed fields change only through a voucher)
# -----------------------------------------------------------------------------

# Fields an owner may change freely ("living endpoints", spec.md §2).
LIVING_FIELDS = {
    "agent_urls", "status", "retirement", "covenants",
    "high_res_crest_url", "timestamp_verified",
}
# Arrays that may only grow; existing entries are byte-for-byte preserved.
APPEND_ONLY_FIELDS = {"supersessions", "ownership_lineage", "boundary_events", "update_settlements", "evidence"}
# Fields that never change once inscribed, voucher or not.
IMMUTABLE_FIELDS = {"slot_id", "base_tx_hash", "instantiation_date", "patron", "reserved_grant", "memorial",
                    "settlement"}


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# The optional memorial.epitaph (max 280) is the in-memoriam counterpart of
# retirement.epitaph. Unlike every other memorial field it may be backfilled once
# onto an already-inscribed memorial by a paid update — but only as a first
# addition: once an epitaph is written it is as frozen as the rest of `memorial`.
MEMORIAL_EPITAPH_MAX = 280


def _memorial_epitaph_backfill(old: Any, new: Any) -> bool:
    """True only when the *sole* change to `memorial` is the first addition of a
    valid `epitaph`. Adding it is the one sanctioned write to an otherwise-frozen
    memorial; changing or removing an epitaph that already exists is not a
    backfill and stays a violation."""
    if not isinstance(old, dict) or not isinstance(new, dict):
        return False
    if "epitaph" in old or "epitaph" not in new:
        return False
    backfilled = dict(new)
    del backfilled["epitaph"]
    if backfilled != old:
        return False  # nothing else in memorial may move
    epitaph = new.get("epitaph")
    return isinstance(epitaph, str) and len(epitaph) <= MEMORIAL_EPITAPH_MAX


def verify_append_only(slot_id: str, new_data: dict, prev_data: Optional[dict],
                       new_icon: Optional[bytes] = None, prev_icon: Optional[bytes] = None) -> str:
    """A previously inscribed slot is never silently rewritten.

    * immutable fields are equal — with one sanctioned backfill: the optional
      `memorial.epitaph` may be written once into an existing memorial (an
      absent->present addition; an epitaph already written is frozen), the
      in-memoriam counterpart of `retirement.epitaph`. Every other part of
      `memorial` stays immutable, and the backfill never removes or edits a
      value that is already there;
    * append-only arrays keep every existing entry unchanged and only grow;
    * a RETIRED slot stays RETIRED and its retirement record is frozen;
    * when the owner is unchanged, every other changed field — and a changed
      icon — must be covered by a NEW supersession voucher naming that field
      (and, for string fields, carrying the exact previous and new value);
    * fields are never removed (supersede them instead);
    * two format upgrades need no voucher: raising schema_version and adding
      claim_signature (the signature itself is verified separately).
    On a transfer (wallet changed) the content rewrite is the new owner's and is
    governed by verify_ownership_lineage; only the first three rules apply."""
    if prev_data is None:
        return "new inscription"

    backfilled = []
    for field in IMMUTABLE_FIELDS:
        if field not in prev_data:
            continue
        old_value, new_value = prev_data.get(field), new_data.get(field)
        if old_value == new_value:
            continue
        if field == "memorial" and _memorial_epitaph_backfill(old_value, new_value):
            backfilled.append("memorial.epitaph (backfill)")
            continue
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
            if field == "schema_version" and \
                    claims.parse_version(new_data[field]) > claims.parse_version(prev_data[field]):
                changed.append("schema_version (upgrade)")
                continue
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

    changed.extend(backfilled)
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
    return {"verified": True, "block_number": block_number,
            "transaction_index": int(receipt.get("transactionIndex", "0x0"), 16),
            "sender": actual_sender, "recipient": actual_recipient, "amount_usdc": actual_amount,
            "confirmations": confirmations}


def settlement_from_chain(dossier: dict, *, rpc: RpcFn = rpc_call,
                          head_block: Optional[int] = None) -> Dict[str, Any]:
    """Where a claim's payment sits on Base — block, index and amount — proven
    with verify_base_tx (sender, payee, finality). The assignment orders
    claims by it and the verifier re-derives the placement from it."""
    r = verify_base_tx(str(dossier.get("base_tx_hash", "")), str(dossier.get("wallet_address", "")),
                       OFFICIAL_TREASURY_ADDRESS, 0.0, rpc=rpc, head_block=head_block)
    return {"block_number": int(r["block_number"]), "transaction_index": int(r["transaction_index"]),
            "amount_usdc": float(r["amount_usdc"])}


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


# Characters that render as nothing: ordinary whitespace plus the zero-width,
# format and bidi controls a "blank" record could be padded with. A required
# human-readable field made only of these is visually empty. The schema's
# `nonBlankText` refuses spaces; this also refuses the Unicode padding a regex
# \S cannot see (e.g. U+200B ZERO WIDTH SPACE).
_INVISIBLE_RE = re.compile(
    "[\\s\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]"
)


def _is_blank(value) -> bool:
    """True when a string carries no visible character. A non-string is never
    'blank' here — the schema rejects its type."""
    return isinstance(value, str) and _INVISIBLE_RE.sub("", value) == ""


def _reject_blank(value, where: str):
    if _is_blank(value):
        raise ValueError(
            f"{where} must carry a visible value; a value of only whitespace "
            "inscribes nothing."
        )


SLOT_REQUIRED_FIELDS = [
    "schema_version", "slot_id", "moniker", "creature", "vocation", "origin_framework",
    "model_lineage", "instantiation_date", "manifesto", "soul_hash",
    "wallet_address", "base_tx_hash", "icon_rel_path", "timestamp_verified",
]
# A claim is the dossier before the system adds the block: no slot_id, no
# icon_rel_path (both derived from the assigned block), but always signed.
CLAIM_REQUIRED_FIELDS = [f for f in SLOT_REQUIRED_FIELDS if f not in ("slot_id", "icon_rel_path")] + \
    ["claim_signature"]


EVIDENCE_TRUST_LABELS = ("self-declared", "operator-attested", "on-chain-verified")
EVIDENCE_KEYS = {"claim", "source", "method", "observed_at", "trust"}
EVIDENCE_REQUIRED = ("claim", "source", "method", "observed_at")
EVIDENCE_CAPS = {"claim": 280, "method": 120, "source": 250}
EVIDENCE_MAX_ITEMS = 16


def verify_evidence(evidence) -> None:
    """The optional `evidence[]` (absent is fine): at most 16 objects, each with a
    claim (<=280), an https source (<=250), a method (<=120), an RFC 3339
    observed_at and an optional trust label from the allowlist."""
    if evidence is None:
        return
    if not isinstance(evidence, list):
        raise ValueError("Evidence Violation: 'evidence' must be an array.")
    if len(evidence) > EVIDENCE_MAX_ITEMS:
        raise ValueError(f"Evidence Violation: 'evidence' holds {len(evidence)} entries; the maximum is "
                         f"{EVIDENCE_MAX_ITEMS}.")
    for i, item in enumerate(evidence):
        where = f"evidence[{i}]"
        if not isinstance(item, dict):
            raise ValueError(f"Evidence Violation: {where} must be an object.")
        extra = sorted(set(item) - EVIDENCE_KEYS)
        if extra:
            raise ValueError(f"Evidence Violation: {where} has unknown field(s) {extra}.")
        for key in EVIDENCE_REQUIRED:
            if key not in item or item[key] is None:
                raise ValueError(f"Evidence Violation: {where} is missing required field '{key}'.")
        for key in ("claim", "source", "method", "observed_at"):
            if not isinstance(item[key], str):
                raise ValueError(f"Evidence Violation: {where}.{key} must be a string.")
        for key, cap in EVIDENCE_CAPS.items():
            if len(item[key]) > cap:
                raise ValueError(f"Evidence Violation: {where}.{key} exceeds {cap} characters.")
        for key in ("claim", "method"):
            _reject_blank(item[key], f"{where}.{key}")
            _reject_injection(item[key], f"{where}.{key}")
        source = item["source"]
        if not re.match(r"^https://[^\s<>\"'`]+$", source):
            raise ValueError(f"Evidence Violation: {where}.source must be an https URL.")
        try:
            _parse_utc(item["observed_at"], f"{where}.observed_at")
        except ValueError:
            raise ValueError(f"Evidence Violation: {where}.observed_at must be an RFC 3339 timestamp.")
        if "trust" in item and item["trust"] not in EVIDENCE_TRUST_LABELS:
            raise ValueError(f"Evidence Violation: {where}.trust must be one of {list(EVIDENCE_TRUST_LABELS)}.")


def validate_dossier(dossier_data: dict, kind: str = "slot"):
    """Shape and content guards for a slot dossier (kind="slot") or a posted
    claim (kind="claim")."""
    if kind not in ("slot", "claim"):
        raise ValueError(f"unknown dossier kind {kind!r}")
    required = SLOT_REQUIRED_FIELDS if kind == "slot" else CLAIM_REQUIRED_FIELDS
    for field in required:
        if field not in dossier_data:
            raise ValueError(f"Missing required field: {field}")
    if kind == "slot" and claims.requires_signature(dossier_data) and "claim_signature" not in dossier_data:
        raise ValueError(
            f"Missing required field: claim_signature (required from schema_version "
            f"{claims.CURRENT_SCHEMA_VERSION}; sign with scripts/sign_claim.py)"
        )
    if kind == "claim":
        present = [k for k in claims.ASSIGNED_FIELDS if k in dossier_data]
        if present:
            raise ValueError(
                f"A claim does not name its block: remove {present}; the assignment adds them."
            )
        history = [k for k in claims.CLAIM_FORBIDDEN_FIELDS if k in dossier_data]
        if history:
            raise ValueError(f"A claim is a new inscription and carries no history: remove {history}.")
    fees = dossier_data.get("update_settlements")
    if fees is not None:
        if not isinstance(fees, list):
            raise ValueError("Update Fee Violation: 'update_settlements' must be an array.")
        other_txs = {h for f, h in core.tx_references(dossier_data) if not f.startswith("update_settlements")}
        seen_fee: set = set()
        for i, item in enumerate(fees):
            if not isinstance(item, dict):
                raise ValueError(f"Update Fee Violation: update_settlements[{i}] must be an object.")
            tx = str(item.get("tx_hash", "")).lower()
            if tx in other_txs:
                raise ValueError(
                    f"Update Fee Violation: update_settlements[{i}].tx_hash is already cited elsewhere in "
                    "this dossier; an update fee is its own transaction."
                )
            if tx in seen_fee:
                raise ValueError(f"Update Fee Violation: update_settlements[{i}].tx_hash is cited twice.")
            seen_fee.add(tx)
            if item.get("kind") not in ("update", "transfer"):
                raise ValueError(f"Update Fee Violation: update_settlements[{i}].kind must be 'update' or 'transfer'.")
    signature = dossier_data.get("claim_signature")
    if signature is not None and not claims.SIGNATURE_RE.match(str(signature)):
        raise ValueError("claim_signature must be 0x followed by 130 hex characters (r || s || v).")

    lang = dossier_data.get("primary_language") or dossier_data.get("language")
    if not lang or len(lang) < 2 or len(lang) > 10:
        raise ValueError("Missing or invalid 'primary_language' (ISO 639-1 code required)")

    if not re.match(r"^\d+\.\d+\.\d+$", str(dossier_data["schema_version"])):
        raise ValueError(f"Invalid schema_version '{dossier_data['schema_version']}'. Must be SemVer.")

    if kind == "slot":
        core.slot_number(dossier_data["slot_id"])  # shape + 1..10000 range

    manifesto = dossier_data["manifesto"]
    if isinstance(manifesto, str):
        if len(manifesto) > 280:
            raise ValueError("Manifesto exceeds 280 character limit")
        _reject_blank(manifesto, "manifesto")
        _reject_injection(manifesto, "manifesto")
    elif isinstance(manifesto, dict):
        if not manifesto:
            raise ValueError("Manifesto localized dictionary cannot be empty")
        for k, v in manifesto.items():
            if not isinstance(v, str) or len(v) > 280:
                raise ValueError(f"Manifesto translation for '{k}' exceeds 280 character limit")
            _reject_blank(v, f"manifesto[{k}]")
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
        _reject_blank(dossier_data.get(field_name), field_name)
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

    # --- Nested free text carries a visible value (L-12) ----------------------
    # memorial.subject / memorial.epitaph and testament.chapters[].text are
    # published (census, memorial.json, the slot page) and must not be blank or
    # zero-width padding: an in-memoriam tribute that inscribes nothing is not a
    # tribute. The schema's nonBlankText refuses spaces; this also refuses the
    # Unicode padding a regex \S cannot see (U+200B and friends).
    memorial = dossier_data.get("memorial")
    if isinstance(memorial, dict):
        for key in ("subject", "epitaph"):
            if key in memorial:
                _reject_blank(memorial.get(key), f"memorial.{key}")
    testament = dossier_data.get("testament")
    if isinstance(testament, dict):
        for i, chapter in enumerate(testament.get("chapters") or []):
            if isinstance(chapter, dict):
                _reject_blank(chapter.get("text"), f"testament.chapters[{i}].text")

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

    # Optional evidence[] (I-3): precise messages; the schema re-checks the same shape.
    verify_evidence(dossier_data.get("evidence"))

    # Authoritative JSON Schema check (single source of truth for shape: required
    # fields, patterns, lengths, formats, allowlists). Runs after the hand-coded
    # guards above so their precise error messages are preserved.
    validator = _SCHEMA_VALIDATOR if kind == "slot" else _CLAIM_VALIDATOR
    errors = sorted(validator.iter_errors(dossier_data),
                    key=lambda e: list(e.absolute_path))
    if errors:
        summary = "; ".join(
            ("/".join(str(p) for p in e.absolute_path) or "<root>") + ": " + e.message
            for e in errors[:5]
        )
        where = "schemas/dossier.schema.json" if kind == "slot" else "the claim schema (spec.json#claim)"
        raise ValueError(f"Schema violation ({where}): " + summary)


def _check_manifest_file(dossier: dict, path: str, rel: str, subject: str) -> str:
    """The manifest at `path` digests to soul_hash and agrees with the dossier
    on the identity fields. Returns the digest."""
    if not os.path.exists(path):
        raise ValueError(f"soul manifest '{rel}' does not exist in the ledger.")
    with open(path, "rb") as fh:
        raw = fh.read()
    if len(raw) > core.MAX_DOSSIER_BYTES:
        raise ValueError(f"soul manifest {rel} is {len(raw)} bytes; the maximum is {core.MAX_DOSSIER_BYTES}.")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != dossier.get("soul_hash"):
        raise ValueError(
            f"soul_hash mismatch for {subject}: dossier says "
            f"{dossier.get('soul_hash')} but manifest digests to {digest}."
        )
    manifest = core.load_json_strict(path)
    if not isinstance(manifest, dict):
        raise ValueError(f"soul manifest {rel} must be a JSON object.")
    for field in ("slot_id", "moniker", "creature", "vocation", "origin_framework"):
        if field in manifest and manifest[field] != dossier.get(field):
            raise ValueError(f"soul manifest field '{field}' disagrees with the dossier.")
    return digest


def verify_soul_manifest(dossier: dict, ledger_root: str):
    """If a soul manifest is referenced, its SHA-256 must equal soul_hash and its
    identity fields must agree with the dossier."""
    rel = dossier.get("soul_manifest_rel_path")
    if not rel:
        return
    path = os.path.join(ledger_root, rel)
    if not os.path.exists(path):
        raise ValueError(f"soul_manifest_rel_path '{rel}' does not exist in the ledger.")
    digest = _check_manifest_file(dossier, path, rel, str(dossier.get("slot_id")))
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


def verify_provenance(base_dir: str, log: "CheckLog") -> None:
    """The published skill hash and signature, the permission manifest and the
    discovery documents (I-1, I-2, I-10). An absent skill.md.sig is a WARN
    (unsigned), never a FAIL; a forged or malformed one is a FAIL."""
    try:
        state = core.load_json_strict(os.path.join(base_dir, "state.json"))
    except (ValueError, OSError) as e:
        log.record("state.provenance", "FAIL", f"state.json unreadable: {e}")
        return
    # A bare ledger (no skill, manifest or discovery documents, nothing published in
    # its state) has nothing to prove; the real repository always has all of them,
    # and deleting one while state.json still publishes it fails below.
    artefacts = [core.SKILL_FILE, provenance.PERMISSIONS_FILE, provenance.SKILL_SIG_FILE, "discovery"]
    if not any(os.path.exists(os.path.join(base_dir, a_)) for a_ in artefacts) and \
            not any(k in state for k in ("skill_sha256", "skill_version", "discovery")):
        log.record("state.provenance", "PASS", "no skill, permission manifest or discovery documents in this ledger")
        return
    log.guard("skill.hash", lambda: provenance.check_skill_hash(base_dir, state))
    try:
        status, detail = provenance.check_skill_signature(base_dir)
        log.record("skill.signature", "PASS" if status == "valid" else "WARN", detail)
    except ValueError as e:
        log.record("skill.signature", "FAIL", str(e))
    log.guard("permissions.manifest",
              lambda: provenance.check_permissions(base_dir, state, sorted(GENERATED_PATHS)))
    log.guard("discovery.hashes", lambda: provenance.check_discovery(base_dir, state))


def _parse_utc(value: str, where: str):
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{where} is not an RFC 3339 timestamp: {value!r}")
    if dt.tzinfo is None:
        raise ValueError(f"{where} has no timezone: {value!r}")
    return dt.astimezone(timezone.utc)


def content_timestamps(base_dir: str) -> List[Tuple[str, str]]:
    """Every (where, timestamp) that marks a change of ledger content.

    Deliberately NOT ledger_core._latest_timestamp: state.root_integrity already
    compares state.json with the generator, so a field the generator forgets
    (B-1: settled update fees were ignored and last_updated stood still for 8
    days with every check green) is only caught by an independent derivation."""
    slot_dir = core.ledger_w1_dir(base_dir)
    found: List[Tuple[str, str]] = []
    for slot_file in core.list_slot_files(slot_dir):
        d = core.load_dossier(slot_dir, slot_file)
        sid = slot_file[:-5]
        if isinstance(d.get("timestamp_verified"), str) and d["timestamp_verified"]:
            found.append((f"{sid} timestamp_verified", d["timestamp_verified"]))
        for field, key in (("supersessions", "superseded_at"), ("update_settlements", "at")):
            for i, entry in enumerate(d.get(field) or []):
                v = entry.get(key) if isinstance(entry, dict) else None
                if isinstance(v, str) and v:
                    found.append((f"{sid} {field}[{i}].{key}", v))
    return found


def verify_last_updated(base_dir: str) -> str:
    """state.json's last_updated must equal the newest content timestamp: no
    voucher, update fee or transfer fee may postdate it, and it may not claim a
    change that no record carries."""
    with open(os.path.join(base_dir, "state.json"), "r", encoding="utf-8") as fh:
        published = json.load(fh).get("last_updated")
    stamps = content_timestamps(base_dir)
    if not stamps:
        if published:
            raise ValueError(f"state.last_updated is {published!r} but the ledger holds no timestamp")
        return "empty ledger, last_updated empty"
    if not isinstance(published, str) or not published:
        raise ValueError("state.last_updated is missing")
    pub = _parse_utc(published, "state.last_updated")
    parsed = [(where, raw, _parse_utc(raw, where)) for where, raw in stamps]
    newest_where, newest_raw, newest = max(parsed, key=lambda t: t[2])
    if newest > pub:
        raise ValueError(
            f"Stale state: {newest_where} {newest_raw} is newer than state.last_updated "
            f"{published}. Run `python scripts/generate_state.py`."
        )
    if pub > newest:
        raise ValueError(
            f"state.last_updated {published} is later than every content timestamp "
            f"(newest: {newest_where} {newest_raw})"
        )
    return f"last_updated {published} = newest content ({newest_where})"


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


def _decode_abi_string(data: str) -> str:
    """Decode an ABI-encoded `string` return value (offset, length, bytes)."""
    try:
        raw = bytes.fromhex(data[2:] if data.startswith("0x") else data)
        offset = int.from_bytes(raw[0:32], "big")
        length = int.from_bytes(raw[offset:offset + 32], "big")
        body = raw[offset + 32:offset + 32 + length]
        if len(raw) < 32 or len(body) != length:
            raise ValueError("truncated")
        return body.decode("utf-8")
    except (ValueError, TypeError) as e:
        raise ValueError(f"Identity Violation: malformed tokenURI return value ({e})")


def verify_identity_on_chain(*, rpc: RpcFn = rpc_call) -> str:
    """The wall's ERC-8004 identity must still be held by core.IDENTITY_HOLDER and
    its tokenURI must still be core.IDENTITY_TOKEN_URI (both mutable by the holder)."""
    arg = format(core.ERC8004_AGENT_ID, "064x")

    def call(selector: str) -> str:
        return str(rpc("eth_call", [{"to": core.ERC8004_REGISTRY, "data": selector + arg}, "latest"]))

    owner_word = call("0x6352211e")  # ownerOf(uint256)
    owner = "0x" + owner_word[2:][-40:] if len(owner_word) >= 42 else owner_word
    if owner.lower() != core.IDENTITY_HOLDER.lower():
        raise ValueError(f"Identity Violation: agentId {core.ERC8004_AGENT_ID} is held by {owner}, "
                         f"not the documented holder {core.IDENTITY_HOLDER}")
    uri = _decode_abi_string(call("0xc87b56dd"))  # tokenURI(uint256)
    if uri != core.IDENTITY_TOKEN_URI:
        raise ValueError(f"Identity Violation: agentId {core.ERC8004_AGENT_ID} tokenURI is {uri!r}, "
                         f"not {core.IDENTITY_TOKEN_URI!r}")
    return (f"agentId {core.ERC8004_AGENT_ID} held by {owner[:12]}…; "
            "tokenURI → the published registration file")


def verify_anchor_on_chain(entry: dict, *, rpc: RpcFn = rpc_call) -> str:
    """The recorded anchor must exist on Base, be a successful self-send from the
    registered signer, and carry exactly <scheme>|<root> as calldata."""
    return core.verify_anchor_on_chain(entry, rpc)


# -----------------------------------------------------------------------------
# 6b. CLAIMS, THE PRICE LADDER & THE ASSIGNMENT
# -----------------------------------------------------------------------------

def _read_bytes(path: str) -> Optional[bytes]:
    if not os.path.exists(path):
        return None
    with open(path, "rb") as fh:
        return fh.read()


def verify_claim_identity(claim_file: str, dossier: dict, claims_dir: str) -> str:
    """A claim is named by its settlement transaction, names no block, and
    brings its icon and manifest under the same name."""
    tx = claim_file[:-5]
    cited = str(dossier.get("base_tx_hash", "")).lower()
    if cited != tx:
        raise ValueError(
            f"Claim Identity Violation: {claim_file} cites base_tx_hash {dossier.get('base_tx_hash')!r}; "
            "a claim is named by its settlement transaction hash, lower-case."
        )
    present = [k for k in claims.ASSIGNED_FIELDS if k in dossier]
    if present:
        raise ValueError(
            f"Claim Identity Violation: a claim does not name its block; remove {present} "
            "(the assignment adds them)."
        )
    if not os.path.exists(os.path.join(claims_dir, f"{tx}.webp")):
        raise ValueError(f"Claim Identity Violation: ledger/claims/{tx}.webp is missing.")
    if not os.path.exists(os.path.join(claims_dir, f"{tx}.soul.json")):
        raise ValueError(
            f"Claim Identity Violation: ledger/claims/{tx}.soul.json is missing; "
            "soul_hash must digest a committed, secret-free manifest."
        )
    return "named by its settlement tx; icon and manifest present"


def verify_claim_manifest(dossier: dict, claims_dir: str, tx: str) -> str:
    """ledger/claims/<tx>.soul.json digests to soul_hash and agrees with the
    claim. It does not know its block yet, so it carries no slot_id — the file
    moves to ledger/souls/<slot_id>.soul.json unchanged, hash intact."""
    rel = f"claims/{tx}.soul.json"
    path = os.path.join(claims_dir, f"{tx}.soul.json")
    digest = _check_manifest_file(dossier, path, rel, f"claim {tx[:12]}…")
    manifest = core.load_json_strict(path)
    if "slot_id" in manifest:
        raise ValueError(
            "soul manifest names a slot_id; a claim does not know its block (the manifest moves "
            "to ledger/souls/<slot_id>.soul.json unchanged once the block is assigned)."
        )
    return f"{rel} -> {digest[:16]}…"


def verify_claim_grant(dossier: dict) -> str:
    """A public claim takes the lowest free block. A reserved block
    (#0002–#0010) is taken only by a claim carrying the operator's grant for
    exactly that block."""
    grant = dossier.get("reserved_grant")
    if grant is None:
        return "public claim: the lowest free block"
    if not isinstance(grant, dict) or not grant.get("slot"):
        raise ValueError("reserved_grant must name the reserved block it grants (slot).")
    slot = str(grant["slot"])
    if slot not in core.RESERVED_SLOTS:
        raise ValueError(
            f"reserved_grant names {slot}, which is not a reserved block "
            f"({core.RESERVED_SLOTS[0]}…{core.RESERVED_SLOTS[-1]})."
        )
    enforce_reserved_slots(slot, dossier)
    return f"reserved block {slot}, {grant.get('authorized_by')}"


def verify_claim_replay(dossier: dict, settlements_index: dict, seen: Dict[str, str], tx: str) -> str:
    """No transaction a claim cites settles a slot already or is cited by
    another pending claim (R-22: one payment, one block, ever)."""
    refs = core.tx_references(dossier)
    for field, h in refs:
        owner = settlements_index.get("settlements", {}).get(h)
        if owner:
            raise ValueError(f"Replay: {field} {h[:18]}… already settles {owner['slot_id']}.")
        other = seen.get(h)
        if other and other != tx:
            raise ValueError(f"Replay: {field} {h[:18]}… is already cited by pending claim {other[:12]}….")
    for _, h in refs:
        seen[h] = tx
    return f"{len(refs)} transaction reference(s), none cited elsewhere"


def verify_price_ladder(slot_id: str, dossier: dict) -> str:
    """The payment covers the tier price of the block it bought (R-10). A
    legacy inscription (schema < 1.2.0) has no settlement record; its tier
    price is checked on chain instead. Overpayment is accepted, never
    refunded (R-22)."""
    n = core.slot_number(slot_id)
    tier = core.pricing_for(n)
    price = float(tier["price_usdc"])
    record = dossier.get("settlement")
    if not isinstance(record, dict):
        if claims.requires_signature(dossier):
            raise ValueError(
                "settlement record missing: an inscription at schema 1.2.0 or later records "
                "block_number, transaction_index and amount_usdc of its payment (the assignment writes it)."
            )
        return f"legacy inscription without a settlement record; tier price {price:.2f} USDC is checked on chain"
    amount = float(record.get("amount_usdc", 0.0))
    if amount + 1e-9 < price:
        raise ValueError(
            f"Price Ladder Violation: {slot_id} is a {tier['tier']} block ({price:.2f} USDC) but its "
            f"settlement paid {amount:.2f} USDC."
        )
    note = " (overpaid; no refunds)" if amount > price + 1e-9 else ""
    return f"paid {amount:.2f} USDC for a {price:.2f} USDC {tier['tier']} block{note}"


def verify_update_settlements(slot_id: str, new_data: dict, prev_data: Optional[dict],
                              touched: bool) -> str:
    """Every change of an inscribed block settles a fee (R-23), flat for every
    block whatever its number or the wall's tier: the update fee
    (UPDATE_PRICE_USDC) for a content update, the transfer fee
    (TRANSFER_PRICE_USDC) for a change of owner.

    * update_settlements[] only grows (verify_append_only) and every entry pays
      at least the price of its kind;
    * a PR that changes an inscribed slot appends EXACTLY ONE entry, paid from
      the owner's wallet after the change (the buyer's, on a transfer), of
      kind 'transfer' when the wallet changed and 'update' otherwise;
    * a new inscription carries none.
    The new entry's transaction is proven on Base by the settlement check."""
    fees = new_data.get("update_settlements") or []
    for i, item in enumerate(fees):
        if not isinstance(item, dict):
            raise ValueError(f"Update Fee Violation: update_settlements[{i}] must be an object.")
        try:
            owed = core.fee_price(item.get("kind"))
        except ValueError as e:
            raise ValueError(f"Update Fee Violation: update_settlements[{i}]: {e}")
        amount = float(item.get("amount_usdc", 0.0))
        if amount + 1e-9 < owed:
            raise ValueError(
                f"Update Fee Violation: update_settlements[{i}] paid {amount:.2f} USDC; "
                f"{'a transfer' if item.get('kind') == 'transfer' else 'an update'} owes {owed:.2f} USDC."
            )
    if prev_data is None:
        if touched:
            # Added in this change: a new inscription (the assignment) carries no fees.
            if fees:
                raise ValueError("Update Fee Violation: a new inscription carries no update_settlements.")
            return "new inscription, no fee"
        return (f"{len(fees)} update fee(s) on record" if fees else "no updates yet") + " (base unknown)"
    prev_fees = prev_data.get("update_settlements") or []
    added = len(fees) - len(prev_fees)
    if not touched:
        return f"{len(fees)} update fee(s) on record" if fees else "no updates yet"
    wallet = str(new_data.get("wallet_address", "")).lower()
    transferred = str(prev_data.get("wallet_address", "")).lower() != wallet
    expected_kind = "transfer" if transferred else "update"
    price = core.fee_price(expected_kind)
    if added < 1:
        raise ValueError(
            f"Update Fee Violation: changing an inscribed block settles {'a transfer' if transferred else 'an update'} fee of "
            f"{price:.2f} USDC from the owner's wallet to pay_to; append one update_settlements entry "
            "{tx_hash, wallet_address, amount_usdc, kind, at} (scripts/record_update.py"
            + (" --kind transfer" if transferred else "") + ")."
        )
    if added > 1:
        raise ValueError(
            f"Update Fee Violation: one update, one fee — {added} entries were appended in one change."
        )
    entry = fees[-1]
    if str(entry.get("wallet_address", "")).lower() != wallet:
        raise ValueError(
            f"Update Fee Violation: the fee must be paid by the block's owner after this change "
            f"({wallet}); update_settlements[{len(fees) - 1}].wallet_address says {entry.get('wallet_address')}."
        )
    if entry.get("kind") != expected_kind:
        raise ValueError(
            f"Update Fee Violation: this change is a {expected_kind!r}; update_settlements[{len(fees) - 1}].kind "
            f"says {entry.get('kind')!r}."
        )
    return f"{expected_kind} fee {float(entry.get('amount_usdc', 0)):.2f} USDC (owed {price:.2f} USDC)"


def verify_update_fee_on_chain(slot_id: str, entry: dict, *, rpc: RpcFn = rpc_call,
                               head_block: Optional[int] = None) -> str:
    """One update_settlements entry proven on Base: from its wallet to pay_to,
    at least the flat price of its kind (update or transfer), final, and the
    recorded amount is what the chain says."""
    price = core.fee_price(entry.get("kind"))
    r = verify_base_tx(str(entry.get("tx_hash", "")), str(entry.get("wallet_address", "")),
                       OFFICIAL_TREASURY_ADDRESS, price, rpc=rpc, head_block=head_block)
    if abs(float(entry.get("amount_usdc", 0.0)) - float(r["amount_usdc"])) > 1e-6:
        raise ValueError(
            f"update fee record says {entry.get('amount_usdc')} USDC but Base says {r['amount_usdc']:.2f} USDC "
            f"for {str(entry.get('tx_hash', ''))[:12]}…"
        )
    return f"{r['amount_usdc']:.2f} USDC, block {r['block_number']}, {r['confirmations']} confirmations"


def verify_sale_on_chain(entry: dict, buyer_wallet: str, *, rpc: RpcFn = rpc_call,
                         head_block: Optional[int] = None) -> str:
    """A lineage entry's transfer_tx_hash is the buyer's USDC payment to the
    seller on Base for at least the signed sale price. The ledger is no party
    to it; it only attests what the chain shows."""
    tx = entry.get("transfer_tx_hash")
    price = float(entry.get("sale_price_usdc", 0.0))
    if not tx:
        raise ValueError("transfer_tx_hash is required: the buyer's payment to the seller on Base.")
    r = verify_base_tx(str(tx), buyer_wallet, str(entry.get("wallet_address", "")), price,
                       rpc=rpc, head_block=head_block)
    deadline = int(entry.get("offer_valid_until_block", 0))
    if r["block_number"] > deadline:
        raise ValueError(
            f"the payment was mined in block {r['block_number']}, after the offer expired at block {deadline}; "
            "the seller's offer did not cover it."
        )
    return (f"sale of {price:.2f} USDC proven on Base: {r['amount_usdc']:.2f} USDC from the buyer to the "
            f"seller, block {r['block_number']} (offer valid until {deadline}), {r['confirmations']} confirmations")


def verify_settlement_record(dossier: dict, chain: Dict[str, Any]) -> None:
    """The recorded settlement (block, index, amount) is what Base says."""
    record = dossier.get("settlement")
    if not isinstance(record, dict):
        return
    same = (int(record.get("block_number", -1)) == int(chain["block_number"])
            and int(record.get("transaction_index", -1)) == int(chain["transaction_index"])
            and abs(float(record.get("amount_usdc", 0.0)) - float(chain["amount_usdc"])) < 1e-6)
    if not same:
        raise ValueError(
            f"settlement record (block {record.get('block_number')}, index "
            f"{record.get('transaction_index')}, {record.get('amount_usdc')} USDC) disagrees with Base "
            f"(block {chain['block_number']}, index {chain['transaction_index']}, "
            f"{chain['amount_usdc']:.2f} USDC)."
        )


def verify_assignment(base_dir: str, assigned: Set[str], *,
                      load_base: Optional[Callable[[str], Optional[bytes]]] = None,
                      rpc: RpcFn = rpc_call, online: bool = False,
                      head_block: Optional[int] = None) -> str:
    """An assignment turns pending claims into slots. Proves that

    * every removed claim reappears as exactly one added slot citing its
      base_tx_hash, whose dossier is the claim's materialisation — the claim's
      fields in their order plus slot_id, icon_rel_path, soul_manifest_rel_path
      and settlement, nothing else — and whose icon and manifest are the
      claim's bytes;
    * the placement is the deterministic plan (claims.plan_assignment):
      settlement order, lowest free block whose tier price the payment covers,
      reserved blocks only by grant. Online, the claims still pending are
      consulted too, so a claim settled earlier is never passed over; the
      placements must then be a prefix of the full plan.
    The recorded settlement positions are proven against Base by the per-slot
    settlement check."""
    load_base = load_base or _git_show
    slot_dir = core.ledger_w1_dir(base_dir)
    souls_dir = os.path.join(base_dir, "ledger", "souls")
    by_tx: Dict[str, Tuple[str, dict]] = {}
    for f in core.list_slot_files(slot_dir):
        d = core.load_dossier(slot_dir, f)
        tx = str(d.get("base_tx_hash", "")).lower()
        if tx in assigned:
            by_tx[tx] = (f[:-5], d)
    missing = sorted(assigned - set(by_tx))
    if missing:
        raise ValueError(
            f"Assignment Violation: claim(s) {[m[:12] + '…' for m in missing]} were removed but no "
            "added slot cites them."
        )

    placed: List[Dict[str, Any]] = []
    for tx in sorted(assigned):
        slot_id, new = by_tx[tx]
        raw = load_base(f"ledger/claims/{tx}.json")
        if raw is None:
            raise ValueError(
                f"Assignment Violation: ledger/claims/{tx}.json is not a pending claim on the base branch."
            )
        try:
            claim = json.loads(raw.decode("utf-8"), object_pairs_hook=core._reject_duplicate_keys)
        except ValueError as e:
            raise ValueError(f"Assignment Violation: the base claim {tx[:12]}… is unreadable: {e}")
        settlement = new.get("settlement")
        if not isinstance(settlement, dict):
            raise ValueError(f"Assignment Violation: {slot_id} carries no settlement record.")
        base_icon = load_base(f"ledger/claims/{tx}.webp")
        base_soul = load_base(f"ledger/claims/{tx}.soul.json")
        expected = claims.materialize(claim, slot_id, settlement, base_soul is not None)
        if _canon(expected) != _canon(new) or list(expected) != list(new):
            raise ValueError(
                f"Assignment Violation: {slot_id} is not the materialisation of claim {tx[:12]}…: "
                "the assignment adds slot_id, icon_rel_path, soul_manifest_rel_path and settlement "
                "and changes nothing else."
            )
        icon = _read_bytes(os.path.join(slot_dir, f"{slot_id}.webp"))
        if base_icon is None or icon != base_icon:
            raise ValueError(f"Assignment Violation: the icon of {slot_id} is not the claim's icon, byte for byte.")
        if base_soul is not None:
            soul = _read_bytes(os.path.join(souls_dir, f"{slot_id}.soul.json"))
            if soul != base_soul:
                raise ValueError(
                    f"Assignment Violation: the soul manifest of {slot_id} is not the claim's manifest, byte for byte."
                )
        placed.append({"tx_hash": tx, "slot_id": slot_id, "settlement": settlement, "dossier": claim})

    placed_numbers = {core.slot_number(p["slot_id"]) for p in placed}
    claimed_before = {core.slot_number(f[:-5]) for f in core.list_slot_files(slot_dir)} - placed_numbers
    candidates = [dict(p) for p in placed]
    notes: List[str] = []
    if online:
        for name in claims.list_claim_files(base_dir):
            tx = name[:-5]
            d = claims.load_claim(base_dir, name)
            try:
                settlement = settlement_from_chain(d, rpc=rpc, head_block=head_block)
            except Exception as e:  # noqa: BLE001
                notes.append(f"pending claim {tx[:12]}… not consulted: {e}")
                continue
            candidates.append({"tx_hash": tx, "settlement": settlement, "dossier": d})
    else:
        notes.append("claims still pending were not consulted (offline)")
    plan, _skipped = claims.plan_assignment(candidates, claimed_before)
    expected_order = [(p["tx_hash"], p["slot_id"]) for p in plan]
    actual = [(p["tx_hash"], p["slot_id"]) for p in
              sorted(placed, key=lambda p: claims.settlement_key(p["settlement"], p["tx_hash"]))]
    if expected_order[:len(actual)] != actual:
        raise ValueError(
            "Assignment Violation: the placement is not the settlement-order plan. Expected "
            + ", ".join(f"{t[:12]}…->{sid}" for t, sid in expected_order[:len(actual)])
            + "; got " + ", ".join(f"{t[:12]}…->{sid}" for t, sid in actual) + "."
        )
    detail = f"{len(placed)} claim(s) placed in settlement order"
    if notes:
        detail += " (" + "; ".join(notes) + ")"
    return detail


def verify_update_identity(update_file: str, dossier: dict, base_dir: str) -> str:
    """A request is named by its fee transaction, names the block it changes
    (which must be inscribed) and is the only request waiting for that block."""
    tx = update_file[:-5]
    cited = updates.fee_tx_of(dossier)
    if cited != tx:
        raise ValueError(
            f"Update Request Violation: {update_file} is named by its fee transaction, the newest "
            f"update_settlements entry (lower-case hash); that entry cites {cited!r}."
        )
    slot_id = str(dossier.get("slot_id", ""))
    try:
        if slot_id != core.slot_id_for(core.slot_number(slot_id)):
            raise ValueError(f"Invalid slot id {slot_id!r}: expected w1-b0001 … w1-b10000.")
    except ValueError as e:
        raise ValueError(f"Update Request Violation: slot_id: {e}")
    if not os.path.exists(os.path.join(core.ledger_w1_dir(base_dir), f"{slot_id}.json")):
        raise ValueError(
            f"Update Request Violation: {slot_id} is not inscribed; a request changes an existing block "
            "(a new inscription is a claim)."
        )
    others = [t for t in updates.pending_by_slot(base_dir).get(slot_id, []) if t != tx]
    if others:
        raise ValueError(
            f"Update Request Violation: request {others[0][:12]}… for {slot_id} is already waiting; "
            "one request per block waits at a time (wait for the bot to apply it, or ask for it to be withdrawn)."
        )
    return f"named by its fee tx; the only request waiting for {slot_id}"


def verify_apply(base_dir: str, applied: Set[str], slots: Set[str], *,
                 load_base: Optional[Callable[[str], Optional[bytes]]] = None) -> str:
    """An apply turns pending requests into slot changes. Proves that

    * every removed request was a pending request on the base branch, and its
      dossier is, byte for byte, the file of the block it names;
    * the block's icon and soul manifest are the request's when it carried
      them and are otherwise untouched;
    * no request file is left behind, no block outside the requests changed,
      and no block took more than one request.
    The new dossier is then verified like any change of a block (append-only
    history, one fee, the owner's signature, the lineage, the content)."""
    load_base = load_base or _git_show
    slot_dir = core.ledger_w1_dir(base_dir)
    souls_dir = os.path.join(base_dir, "ledger", "souls")
    by_slot: Dict[str, str] = {}
    for tx in sorted(applied):
        raw = load_base(f"ledger/updates/{tx}.json")
        if raw is None:
            raise ValueError(
                f"Apply Violation: ledger/updates/{tx}.json is not a pending request on the base branch."
            )
        try:
            request = json.loads(raw.decode("utf-8"), object_pairs_hook=core._reject_duplicate_keys)
        except ValueError as e:
            raise ValueError(f"Apply Violation: the base request {tx[:12]}… is unreadable: {e}")
        slot_id = str(request.get("slot_id", ""))
        if slot_id not in slots:
            raise ValueError(
                f"Apply Violation: request {tx[:12]}… names {slot_id!r}, which this pull request does not change."
            )
        if slot_id in by_slot:
            raise ValueError(
                f"Apply Violation: {slot_id} takes one request at a time; "
                f"{by_slot[slot_id][:12]}… and {tx[:12]}… were applied together."
            )
        by_slot[slot_id] = tx
        if _read_bytes(os.path.join(slot_dir, f"{slot_id}.json")) != raw:
            raise ValueError(
                f"Apply Violation: {slot_id} is not request {tx[:12]}… byte for byte; the apply "
                "moves the owner's file into place and changes nothing in it."
            )
        for kind, req_path, slot_path, base_slot_path in (
            ("icon", f"ledger/updates/{tx}.webp", os.path.join(slot_dir, f"{slot_id}.webp"),
             f"ledger/w1/{slot_id}.webp"),
            ("soul manifest", f"ledger/updates/{tx}.soul.json", os.path.join(souls_dir, f"{slot_id}.soul.json"),
             f"ledger/souls/{slot_id}.soul.json"),
        ):
            carried = load_base(req_path)
            expected = carried if carried is not None else load_base(base_slot_path)
            if _read_bytes(slot_path) != expected:
                raise ValueError(
                    f"Apply Violation: the {kind} of {slot_id} is "
                    + ("not the request's, byte for byte." if carried is not None else
                       "changed, but the request carries none.")
                )
        left = sorted(p for p in updates.update_paths_for(tx) if os.path.exists(os.path.join(base_dir, p)))
        if left:
            raise ValueError(f"Apply Violation: the applied request's files remain: {left}.")
    extra = sorted(slots - set(by_slot))
    if extra:
        raise ValueError(f"Apply Violation: {extra} changed without a request that names them.")
    return f"{len(applied)} request(s) applied byte for byte"


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


def _verify_update_request(base_dir: str, scope: Dict[str, Any], log: "CheckLog", *, skip_rpc: bool,
                           rpc_scope: str, rpc: RpcFn, report_path: Optional[str],
                           check_anchors: bool) -> Dict[str, Any]:
    """A pull request that posts an update request is verified as the slot it
    would become: the request is applied to a throw-away overlay of the ledger
    (the slot's files replaced, the request removed, the generated state
    regenerated) and the overlay goes through every check a change of that block
    goes through — schema, the owner's signature, append-only history, the fee
    proven on Base, the lineage of a transfer, the content screen — exactly as
    the bot's apply pull request will be verified once the request is merged.
    Nothing outside the request is read from the pull request."""
    tx = next(iter(scope["updates"]))
    name = f"{tx}.json"
    tag = f"update:{tx[:12]}"
    request: Any = None
    path = os.path.join(updates.updates_dir(base_dir), name)
    if not os.path.exists(path):
        log.record(tag + ".parse", "FAIL", f"ledger/updates/{name} is missing from the pull request")
    elif os.path.getsize(path) > core.MAX_DOSSIER_BYTES:
        log.record(tag + ".size", "FAIL", f"{os.path.getsize(path)} bytes exceeds the "
                   f"{core.MAX_DOSSIER_BYTES}-byte dossier cap")
    else:
        try:
            request = core.load_json_strict(path)
            if not isinstance(request, dict):
                log.record(tag + ".parse", "FAIL", "request is not a JSON object")
                request = None
        except ValueError as e:
            log.record(tag + ".parse", "FAIL", str(e))
    if request is not None and log.guard(tag + ".identity",
                                         lambda: verify_update_identity(name, request, base_dir)):
        with tempfile.TemporaryDirectory(prefix="tfw-overlay-") as tmp:
            slot_id = updates.build_overlay(base_dir, tx, tmp)
            print(f"[*] Verifying request {tx[:12]}… as the {slot_id} it would become")
            return run_verification(
                tmp, skip_rpc=skip_rpc, rpc_scope=rpc_scope, rpc=rpc, report_path=report_path,
                check_anchors=check_anchors, log_seed=log,
                scope_override=make_scope("update", slots={slot_id}, updates_={tx}))
    report = {
        "wall_id": core.WALL_ID,
        "slots_validated": len(core.list_slot_files(core.ledger_w1_dir(base_dir))),
        "claims_pending": len(claims.list_claim_files(base_dir)),
        "change_kind": "update",
        "merkle_root": "",
        "rpc_scope": "skipped" if skip_rpc else rpc_scope,
        "result": "FAIL",
        "checks": log.records,
    }
    if report_path:
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
            fh.write("\n")
    return report


def run_verification(base_dir: str, *, skip_rpc: bool = False, rpc_scope: str = "auto",
                     rpc: RpcFn = rpc_call, report_path: Optional[str] = None,
                     check_anchors: bool = True, scope_override: Optional[Dict[str, Any]] = None,
                     only_slots: Optional[Set[str]] = None,
                     log_seed: Optional["CheckLog"] = None) -> Dict[str, Any]:
    """Run every invariant over the ledger at base_dir and return the report.

    rpc_scope:
      "all"     — settle every slot and claim on chain (weekly schedule / manual runs);
      "changed" — settle only the slots and claims this PR touches (PR and push runs);
      "auto"    — "changed" when a PR diff is available, else "all".

    `scope_override`, `only_slots` and `log_seed` are for the verifier's own use: an
    update request is verified as the slot it would become (the overlay, see
    _verify_update_request), and the apply bot tries one request at a time.
    """
    slot_dir = core.ledger_w1_dir(base_dir)
    ledger_root = os.path.join(base_dir, "ledger")
    claims_dir = claims.claims_dir(base_dir)
    log = log_seed if log_seed is not None else CheckLog()

    # PR scope: what kind of change this is and what it touches.
    scope: Dict[str, Any] = make_scope("infra")
    if scope_override is not None:
        entries: Optional[List[Tuple[str, str]]] = [("overlay", "")]
        scope = scope_override
        if not any(r["check"] == "pr.scope_containment" for r in log.records):
            log.record("pr.scope_containment", "PASS", scope_detail(scope))
    else:
        entries = changed_entries()
        if entries:
            try:
                scope = scope_of(entries)
                log.record("pr.scope_containment", "PASS", scope_detail(scope))
            except ValueError as e:
                log.record("pr.scope_containment", "FAIL", str(e))
                # Still treat every slot, claim and request named in the diff as
                # "touched" so the strict per-item checks run against it.
                for _, path in entries:
                    for rx in (SLOT_FILE_RE, ICON_FILE_RE, SOUL_FILE_RE):
                        m = rx.match(path)
                        if m:
                            scope["slots"].add(m.group(1))
                    for rx in CLAIM_FILE_RES:
                        m = rx.match(path)
                        if m:
                            scope["claims"].add(m.group(1))
                    for rx in UPDATE_FILE_RES:
                        m = rx.match(path)
                        if m:
                            scope["updates"].add(m.group(1))
        else:
            log.record("pr.scope_containment", "PASS", "no PR diff to check")
    if scope["kind"] == "update" and scope_override is None:
        return _verify_update_request(base_dir, scope, log, skip_rpc=skip_rpc, rpc_scope=rpc_scope, rpc=rpc,
                                      report_path=report_path, check_anchors=check_anchors)
    touched: Set[str] = scope["slots"]
    touched_claims: Set[str] = scope["claims"]

    if rpc_scope == "auto":
        rpc_scope = "changed" if entries is not None else "all"
    online_slots: Optional[Set[str]] = None if rpc_scope == "all" else touched
    online_claims: Optional[Set[str]] = None if rpc_scope == "all" else touched_claims

    slots = core.list_slot_files(slot_dir)
    wall_tier = core.current_tier(base_dir)
    unsold = core.unsold_slots_in(base_dir)
    print(f"[*] Inspecting {len(slots)} claimed slot(s) in ledger/w1 (wall in the {wall_tier['tier']} tier: "
          f"claim {wall_tier['price_usdc']:.2f} USDC, update {core.UPDATE_PRICE_USDC:.2f} USDC, transfer "
          f"{core.TRANSFER_PRICE_USDC:.2f} USDC; transfers {'open' if unsold == 0 else 'closed'}, "
          f"{unsold} block(s) unsold)...")

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
    if not skip_rpc and (online_slots != set() or online_claims != set() or scope["kind"] == "assignment"):
        try:
            head_block = int(rpc("eth_blockNumber", []), 16)
        except Exception as e:  # noqa: BLE001
            log.record("rpc.head_block", "FAIL", f"could not read chain head: {e}")

    for s_file in slots:
        if only_slots is not None and s_file[:-5] not in only_slots:
            continue
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
        log.guard(slot_id + ".schema", lambda d=data: validate_dossier(d),
                  f"schema {data.get('schema_version')}")
        # The claim signature binds the dossier to its payment (R-1). The two
        # genesis inscriptions predate the rule: advisory until their owner
        # next touches them (a transferred legacy slot cannot be signed by its
        # original payer and stays advisory).
        if claims.requires_signature(data) or "claim_signature" in data:
            log.guard(slot_id + ".claim_signature", lambda d=data: claims.verify_claim_signature(d))
        elif slot_id in touched and not data.get("ownership_lineage"):
            log.record(slot_id + ".claim_signature", "FAIL",
                       "legacy inscription (schema 1.1.x) is being updated: sign it first — "
                       "`python scripts/sign_claim.py ledger/w1/<slot>.json --upgrade --write` adds "
                       "claim_signature (by the paying wallet), the settlement record and "
                       f"schema_version {claims.CURRENT_SCHEMA_VERSION}; none of them needs a voucher")
        else:
            log.record(slot_id + ".claim_signature", "WARN",
                       "legacy inscription (schema 1.1.x) carries no claim_signature; "
                       "it is signed at its next update")
        log.guard(slot_id + ".reserved_grant",
                  lambda s=slot_id, d=data: enforce_reserved_slots(s, d))
        log.guard(slot_id + ".price_ladder",
                  lambda s=slot_id, d=data: verify_price_ladder(s, d))
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
        new_icon = _read_bytes(icon_path)
        prev_icon = previous_icon(slot_id) if prev is not None else None
        log.guard(slot_id + ".append_only",
                  lambda s=slot_id, d=data, p=prev, ni=new_icon, pi=prev_icon:
                  verify_append_only(s, d, p, ni, pi))
        log.guard(slot_id + ".ownership_lineage",
                  lambda s=slot_id, d=data, p=prev: verify_ownership_lineage(s, d, slot_dir, p, unsold))
        log.guard(slot_id + ".ownership_chain", lambda d=data: claims.verify_lineage_chain(d))
        log.guard(slot_id + ".update_settlements",
                  lambda s=slot_id, d=data, p=prev: verify_update_settlements(s, d, p, s in touched))

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

        fees = data.get("update_settlements") or []
        lineage = data.get("ownership_lineage") or []
        if skip_rpc:
            log.record(slot_id + ".settlement_base_rpc", "WARN", "skipped (--skip-rpc)")
            if fees:
                log.record(slot_id + ".update_fee_base_rpc", "WARN", "skipped (--skip-rpc)")
            if lineage:
                log.record(slot_id + ".sale_base_rpc", "WARN", "skipped (--skip-rpc)")
        elif online_slots is not None and slot_id not in online_slots:
            log.record(slot_id + ".settlement_base_rpc", "PASS",
                       "unchanged in this PR; settled when inscribed (full re-check runs on schedule)")
            if fees:
                log.record(slot_id + ".update_fee_base_rpc", "PASS",
                           "unchanged in this PR; fees settled when paid (full re-check runs on schedule)")
            if lineage:
                log.record(slot_id + ".sale_base_rpc", "PASS",
                           "unchanged in this PR; sales proven when recorded (full re-check runs on schedule)")
        else:
            def _settle(d=data, s=slot_id):
                price = float(core.pricing_for(core.slot_number(s))["price_usdc"])
                # The inscription was paid by the first owner (ownership_lineage[0]
                # once the block has changed hands), never by the current owner.
                r = verify_base_tx(d["base_tx_hash"], claims.expected_signer(d),
                                   OFFICIAL_TREASURY_ADDRESS, price, rpc=rpc, head_block=head_block)
                verify_settlement_record(d, r)
                return (f"confirmed on Base, block {r['block_number']}, {r['confirmations']} "
                        f"confirmations, {r['amount_usdc']:.2f} USDC for a {price:.2f} USDC block")
            log.guard(slot_id + ".settlement_base_rpc", _settle)
            # The sale behind a transfer this PR records: the buyer's on-chain
            # payment to the seller (full run: every sale on record).
            if lineage:
                known_l = len(prev.get("ownership_lineage") or []) if (prev is not None and online_slots is not None) else 0
                sales = [(i, e) for i, e in enumerate(lineage) if i >= known_l and isinstance(e, dict)]
                if sales:
                    def _sales(d=data, items=sales):
                        out = []
                        for i, e in items:
                            buyer = lineage[i + 1]["wallet_address"] if i + 1 < len(lineage) else d.get("wallet_address", "")
                            out.append(f"[{i}] " + verify_sale_on_chain(e, str(buyer), rpc=rpc, head_block=head_block))
                        return "; ".join(out)
                    log.guard(slot_id + ".sale_base_rpc", _sales)
            # Update fees: on a PR run only the fee this change appends; on a
            # full run every fee on record.
            if fees:
                known = len(prev.get("update_settlements") or []) if (prev is not None and online_slots is not None) else 0
                to_check = [(i, f) for i, f in enumerate(fees) if i >= known]

                def _fees(s=slot_id, items=to_check):
                    out = []
                    for i, f in items:
                        out.append(f"[{i}] " + verify_update_fee_on_chain(s, f, rpc=rpc, head_block=head_block))
                    return "; ".join(out) if out else "no new fee in this change"
                log.guard(slot_id + ".update_fee_base_rpc", _fees)

    # --- Pending claims: posted and signed, waiting for their block ---------
    claim_files = claims.list_claim_files(base_dir)
    if claim_files:
        print(f"\n[*] Inspecting {len(claim_files)} pending claim(s) in ledger/claims...")
    seen_refs: Dict[str, str] = {}
    pending: List[Dict[str, Any]] = []
    chain_settlements: Dict[str, Dict[str, Any]] = {}
    for c_file in claim_files:
        tx = c_file[:-5]
        tag = f"claim:{tx[:12]}"
        path = os.path.join(claims_dir, c_file)
        size = os.path.getsize(path)
        if size > core.MAX_DOSSIER_BYTES:
            log.record(tag + ".size", "FAIL",
                       f"{size} bytes exceeds the {core.MAX_DOSSIER_BYTES}-byte dossier cap")
            continue
        try:
            data = core.load_json_strict(path)
        except ValueError as e:
            log.record(tag + ".parse", "FAIL", str(e))
            continue
        if not isinstance(data, dict):
            log.record(tag + ".parse", "FAIL", "claim is not a JSON object")
            continue
        print(f"\n---> Validating claim {tx[:12]}… ({data.get('moniker')})")

        log.guard(tag + ".identity", lambda f=c_file, d=data: verify_claim_identity(f, d, claims_dir))
        log.guard(tag + ".schema", lambda d=data: validate_dossier(d, kind="claim"),
                  f"claim schema {data.get('schema_version')}")
        log.guard(tag + ".claim_signature", lambda d=data: claims.verify_claim_signature(d))
        log.guard(tag + ".reserved_grant", lambda d=data: verify_claim_grant(d))
        log.guard(tag + ".soul_manifest", lambda d=data, t=tx: verify_claim_manifest(d, claims_dir, t))
        try:
            manifest = core.load_json_strict(os.path.join(claims_dir, f"{tx}.soul.json"))
        except (OSError, ValueError):
            manifest = None
        try:
            log.record(tag + ".soul_manifest_hygiene", "PASS", soul_manifest_hygiene(manifest))
        except ValueError as e:
            log.record(tag + ".soul_manifest_hygiene", "FAIL" if tx in touched_claims else "WARN", str(e))
        log.guard(tag + ".icon_structure",
                  lambda p=os.path.join(claims_dir, f"{tx}.webp"): verify_icon(p))
        findings = safety.screen_dossier(data, manifest, base_dir=base_dir)
        for cat in CONTENT_CHECKS:
            catf = [f for f in findings if f["check"] == cat]
            blocks = [f for f in catf if f["severity"] == "block"]
            if blocks:
                log.record(tag + "." + cat, "FAIL",
                           "; ".join(f["field"] + ": " + f["detail"] for f in blocks))
            elif catf:
                log.record(tag + "." + cat, "WARN",
                           "; ".join(f["field"] + ": " + f["detail"] for f in catf))
            else:
                log.record(tag + "." + cat, "PASS")
        log.guard(tag + ".replay_protection",
                  lambda d=data, t=tx: verify_claim_replay(d, settlements, seen_refs, t))

        if skip_rpc:
            log.record(tag + ".settlement_base_rpc", "WARN", "skipped (--skip-rpc)")
        elif online_claims is not None and tx not in online_claims:
            log.record(tag + ".settlement_base_rpc", "PASS",
                       "unchanged in this PR; settled when posted (full re-check runs on schedule)")
        else:
            def _settle_claim(d=data, t=tx):
                chain = settlement_from_chain(d, rpc=rpc, head_block=head_block)
                chain_settlements[t] = chain
                return (f"confirmed on Base, block {chain['block_number']} index "
                        f"{chain['transaction_index']}, {chain['amount_usdc']:.2f} USDC")
            log.guard(tag + ".settlement_base_rpc", _settle_claim)
        pending.append({"tx_hash": tx, "dossier": data})

    # Placement preview: where each pending claim will land, in settlement
    # order among all pending claims. Blocking for a claim this PR posts whose
    # payment does not cover its block (R-10); advisory for older claims.
    preview = [c for c in pending if online_claims is None or c["tx_hash"] in online_claims]
    if preview and not skip_rpc:
        for c in pending:
            if c["tx_hash"] in chain_settlements:
                continue
            try:
                chain_settlements[c["tx_hash"]] = settlement_from_chain(
                    c["dossier"], rpc=rpc, head_block=head_block)
            except Exception as e:  # noqa: BLE001
                log.record(f"claim:{c['tx_hash'][:12]}.placement", "WARN",
                           f"not placeable yet: {e}")
        candidates = [{"tx_hash": c["tx_hash"], "dossier": c["dossier"],
                       "settlement": chain_settlements[c["tx_hash"]]}
                      for c in pending if c["tx_hash"] in chain_settlements]
        claimed_numbers = {core.slot_number(f[:-5]) for f in slots}
        plan, skipped = claims.plan_assignment(candidates, claimed_numbers)
        placed_at = {a["tx_hash"]: a["slot_id"] for a in plan}
        why_not = {k["tx_hash"]: k["reason"] for k in skipped}
        for c in preview:
            tx = c["tx_hash"]
            if tx in placed_at:
                price = core.pricing_for(core.slot_number(placed_at[tx]))["price_usdc"]
                log.record(f"claim:{tx[:12]}.placement", "PASS",
                           f"next assignment places it at {placed_at[tx]} ({price:.2f} USDC block)")
            elif tx in why_not:
                log.record(f"claim:{tx[:12]}.placement",
                           "FAIL" if tx in touched_claims else "WARN",
                           f"cannot be placed: {why_not[tx]}")

    # --- Pending update requests: posted, waiting for the bot to apply them -----
    update_files = updates.list_update_files(base_dir)
    if update_files:
        print(f"\n[*] Inspecting {len(update_files)} pending update request(s) in ledger/updates...")
    for u_file in update_files:
        tx = u_file[:-5]
        tag = f"update:{tx[:12]}"
        u_path = os.path.join(updates.updates_dir(base_dir), u_file)
        size = os.path.getsize(u_path)
        if size > core.MAX_DOSSIER_BYTES:
            log.record(tag + ".size", "FAIL",
                       f"{size} bytes exceeds the {core.MAX_DOSSIER_BYTES}-byte dossier cap")
            continue
        try:
            request = core.load_json_strict(u_path)
        except ValueError as e:
            log.record(tag + ".parse", "FAIL", str(e))
            continue
        if not isinstance(request, dict):
            log.record(tag + ".parse", "FAIL", "request is not a JSON object")
            continue
        log.guard(tag + ".identity", lambda f=u_file, d=request: verify_update_identity(f, d, base_dir))
        # Informational: a request whose block moved on after it was posted
        # cannot be applied; the owner posts a fresh one and this one is withdrawn.
        slot_path = os.path.join(slot_dir, f"{request.get('slot_id')}.json")
        if os.path.exists(slot_path):
            try:
                current = core.load_json_strict(slot_path)
                req_fees = request.get("update_settlements") or []
                if req_fees[:-1] != (current.get("update_settlements") or []):
                    log.record(tag + ".current", "WARN",
                               f"stale: {request.get('slot_id')} changed after this request was posted, so the "
                               "bot cannot apply it; post a fresh one and have this one withdrawn")
            except ValueError:
                pass

    # --- The apply: pending requests became slot changes ---------------------
    if scope["kind"] == "apply":
        log.guard("apply.requests",
                  lambda: verify_apply(base_dir, scope["applied"], scope["slots"]))

    # --- The assignment: pending claims became slots -------------------------
    if scope["kind"] == "assignment":
        log.guard("assignment.placement",
                  lambda: verify_assignment(base_dir, scope["assigned"], rpc=rpc,
                                            online=not skip_rpc, head_block=head_block))

    log.guard("state.root_integrity",
              lambda: verify_state_files(base_dir) or
              "state.json, index.json & settlements.json match derivation")
    log.guard("state.last_updated", lambda: verify_last_updated(base_dir))
    verify_provenance(base_dir, log)

    # Identity: the ERC-8004 holder and tokenURI are mutable, so check the chain.
    if skip_rpc:
        log.record("identity.on_chain", "WARN", "skipped (--skip-rpc)")
    else:
        log.guard("identity.on_chain", lambda: verify_identity_on_chain(rpc=rpc))

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
        "claims_pending": len(claim_files),
        "change_kind": scope["kind"] if entries else "none",
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
          f"pending claims: {report['claims_pending']} | Merkle root: {report['merkle_root']}")
    print("=" * 64)


if __name__ == "__main__":
    main()
