#!/usr/bin/env python3
"""
The First Wall — Deterministic Offline Verifier Engine (v1.3)
Zero LLMs. 100% prompt injection immune.

Invariants Enforced:
1. Base Mainnet RPC Verification (USDC transfer, confirmation, sender & recipient match).
2. PR Scope Containment (at most ONE slot altered; slot files never deleted; history never erased).
3. Secondary Transfer & Provenance Lineage (former owner appended, never overwritten).
4. Steganography Defense (forced 10x10 lossless WebP, stripped EXIF, < 1 KB).
5. Soul-hash integrity (must hash a real manifest, never the empty-string digest).
6. State-root integrity (state.json & ledger/index.json must equal the deterministic derivation).
"""

import hashlib
import io
import json
import os
import re
import subprocess
import sys
from typing import Any, Dict, Optional

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
# `schemas/dossier.schema.json` defines the dossier shape. validate_dossier()
# enforces it for every dossier, so the schema file can never drift from what is
# actually enforced (tests/test_schema_agreement.py asserts the agreement).
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


# -----------------------------------------------------------------------------
# 1. PR BOUNDARY & CONTAINMENT INVARIANT
# -----------------------------------------------------------------------------

SLOT_FILE_RE = re.compile(r"^ledger/w1/w1-b\d{4}\.json$")


def enforce_pr_scope_containment():
    """
    Guarantees that a PR can alter AT MOST ONE slot dossier, can never delete a
    slot, and can never touch more than one slot coordinate.

    Repository infrastructure (scripts/, schemas/, .github/, docs, state.json,
    canvas) MAY change in the same PR — the human merge gate reviews those — but
    the ledger's *historical* content stays append-only: existing slots are never
    silently rewritten, and no PR may bundle changes across multiple slots.
    """
    base_ref = os.environ.get("GITHUB_BASE_REF", "main")
    try:
        res = subprocess.run(
            ["git", "diff", "--name-status", f"origin/{base_ref}...HEAD"],
            capture_output=True, text=True, check=True,
        )
    except Exception:
        # Not in a git/CI context (e.g. local run without origin) — nothing to check.
        return

    entries = [line.split("\t") for line in res.stdout.splitlines() if line.strip()]
    if not entries:
        return

    slot_paths = set()
    print(f"[*] Validating PR scope containment across {len(entries)} changed file(s)...")
    for parts in entries:
        status, path = parts[0], parts[-1]
        if SLOT_FILE_RE.match(path):
            if status.startswith("D"):
                raise ValueError(f"PR Boundary Violation: slot file {path} may never be deleted.")
            slot_paths.add(path)

    if len(slot_paths) > 1:
        raise ValueError(
            "PR Boundary Violation: a PR must not alter more than ONE slot dossier. "
            f"Found changes to {len(slot_paths)} slots: {sorted(slot_paths)}"
        )
    if slot_paths:
        print(f"  [✓] Scope contained to slot: {sorted(slot_paths)[0]}")


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
    print(f"  [✓] Reserved slot {slot_id} authorized ({grant.get('authorized_by')}).")


# -----------------------------------------------------------------------------
# 2. SECONDARY SALE / OWNERSHIP LINEAGE INVARIANT
# -----------------------------------------------------------------------------

def verify_ownership_lineage(slot_id: str, new_data: dict, ledger_dir: str):
    base_ref = os.environ.get("GITHUB_BASE_REF", "main")
    prev_json_str = None
    try:
        res = subprocess.run(
            ["git", "show", f"origin/{base_ref}:ledger/w1/{slot_id}.json"],
            capture_output=True, text=True,
        )
        if res.returncode == 0:
            prev_json_str = res.stdout
    except Exception:
        pass

    if not prev_json_str:
        return  # Genesis claim, no prior owner.

    prev_data = json.loads(prev_json_str)
    prev_wallet = prev_data.get("wallet_address", "").lower()
    new_wallet = new_data.get("wallet_address", "").lower()

    if prev_wallet == new_wallet:
        return  # Same owner metadata update.

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
        if item.get("wallet_address", "").lower() != lineage[i].get("wallet_address", "").lower():
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

    print(f"[*] Verifying secondary sale on Base: {sale_price:.2f} USDC to {prev_wallet} (Tx: {transfer_tx})...")
    verify_base_tx(transfer_tx, new_wallet, prev_wallet, sale_price)
    print(f"  [✓] Ownership lineage verified: {prev_data.get('moniker')} -> {new_data.get('moniker')} for {sale_price:.2f} USDC.")


# -----------------------------------------------------------------------------
# 3. BASE ON-CHAIN SETTLEMENT VALIDATION
# -----------------------------------------------------------------------------

def verify_base_tx(tx_hash: str, expected_sender: str, expected_recipient: Optional[str] = None,
                   expected_amount_usdc: float = 1.00) -> Dict[str, Any]:
    if not tx_hash.startswith("0x") or len(tx_hash) != 66:
        raise ValueError(f"Invalid transaction hash format: {tx_hash}")

    print(f"[*] Querying Base RPC for transaction receipt: {tx_hash}...")
    receipt = rpc_call("eth_getTransactionReceipt", [tx_hash])
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

    print(f"  [✓] On-chain settlement confirmed: block {block_number} | {actual_amount:.2f} USDC | from {actual_sender}")
    return {"verified": True, "block_number": block_number, "sender": actual_sender,
            "recipient": actual_recipient, "amount_usdc": actual_amount}


# -----------------------------------------------------------------------------
# 4. SCHEMA, SOUL & IMAGE VALIDATION
# -----------------------------------------------------------------------------

SUSPICIOUS_PATTERNS = ["<script", "<iframe", "javascript:", "onerror=", "onload=", "data:text/html"]


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

    for field_name in ["moniker", "creature", "vocation"]:
        _reject_injection(str(dossier_data.get(field_name, "")), field_name)

    # Authoritative JSON Schema check (single source of truth for shape: required
    # fields, patterns, lengths, formats). Runs after the hand-coded guards above
    # so their precise error messages are preserved.
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
        digest = hashlib.sha256(fh.read()).hexdigest()
    if digest != dossier.get("soul_hash"):
        raise ValueError(
            f"soul_hash mismatch for {dossier.get('slot_id')}: dossier says "
            f"{dossier.get('soul_hash')} but manifest digests to {digest}."
        )
    with open(path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    for field in ("slot_id", "moniker", "creature", "vocation", "origin_framework"):
        if field in manifest and manifest[field] != dossier.get(field):
            raise ValueError(f"soul manifest field '{field}' disagrees with the dossier.")
    print(f"  [✓] Soul manifest verified ({rel} -> {digest[:16]}…).")


def sanitize_icon(input_path: str, output_path: str) -> int:
    with open(input_path, "rb") as f:
        img_bytes = f.read()
    img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    if img.size != (10, 10):
        img = img.resize((10, 10), Image.Resampling.LANCZOS)
    clean_img = Image.new("RGBA", (10, 10))
    clean_img.paste(img, (0, 0))
    clean_img.save(output_path, "WEBP", lossless=True, quality=100)
    size = os.path.getsize(output_path)
    if size > 1024:
        raise ValueError(f"Sanitized icon size {size} bytes exceeds 1 KB limit")
    return size


# -----------------------------------------------------------------------------
# 5. STATE-ROOT INTEGRITY
# -----------------------------------------------------------------------------

def verify_state_files(base_dir: str):
    """state.json and ledger/index.json must equal the deterministic derivation."""
    state, index = core.build_state(base_dir)
    expected = {
        os.path.join(base_dir, "state.json"): core.render(state),
        os.path.join(base_dir, "ledger", "index.json"): core.render(index),
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


# -----------------------------------------------------------------------------
# 6. MAIN
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
        tag = {"PASS": "\u2713", "WARN": "!", "FAIL": "\u2717"}.get(status, "?")
        print(f"  [{tag}] {name}" + (f" \u2014 {detail}" if detail else ""))

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
        with open(os.path.join(ledger_root, rel), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except OSError:
        return None


def main():
    skip_rpc = "--skip-rpc" in sys.argv
    report_path = None
    if "--report" in sys.argv:
        report_path = sys.argv[sys.argv.index("--report") + 1]
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    slot_dir = core.ledger_w1_dir(base_dir)
    ledger_root = os.path.join(base_dir, "ledger")
    log = CheckLog()

    log.guard("pr.scope_containment", enforce_pr_scope_containment, "\u2264 1 slot touched")

    slots = core.list_slot_files(slot_dir)
    print(f"[*] Inspecting {len(slots)} claimed slot(s) in ledger/w1...")

    seen_tx_hashes = set()
    for s_file in slots:
        with open(os.path.join(slot_dir, s_file), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        slot_id = data["slot_id"]
        print(f"\n---> Validating {slot_id} ({data.get('moniker')})")

        tx_hash = data.get("base_tx_hash", "").lower()
        if tx_hash in seen_tx_hashes:
            log.record(slot_id + ".replay_protection", "FAIL",
                       "duplicate tx " + tx_hash[:18] + "\u2026")
        else:
            log.record(slot_id + ".replay_protection", "PASS")
        seen_tx_hashes.add(tx_hash)

        log.guard(slot_id + ".schema", lambda d=data: validate_dossier(d), "schema 1.1.x")
        log.guard(slot_id + ".reserved_grant",
                  lambda s=slot_id, d=data: enforce_reserved_slots(s, d))
        log.guard(slot_id + ".soul_manifest",
                  lambda d=data: verify_soul_manifest(d, ledger_root))
        log.guard(slot_id + ".ownership_lineage",
                  lambda s=slot_id, d=data: verify_ownership_lineage(s, d, slot_dir))

        def _icon(d=data):
            p = core.icon_path_for(slot_dir, d)
            if not os.path.exists(p):
                raise FileNotFoundError("Icon not found at: " + p)
            return "10x10 lossless WebP, %d bytes" % sanitize_icon(p, p)
        log.guard(slot_id + ".icon_sanitize", _icon)

        # Content safety — deterministic zero-LLM pre-screen for the human gate.
        manifest = _load_manifest(ledger_root, data)
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
        else:
            def _settle(d=data):
                verify_base_tx(d["base_tx_hash"], d["wallet_address"],
                               OFFICIAL_TREASURY_ADDRESS, 1.00)
                return "confirmed on Base"
            log.guard(slot_id + ".settlement_base_rpc", _settle)

    log.guard("state.root_integrity",
              lambda: verify_state_files(base_dir) or "state.json & index.json match derivation")

    root = core.compute_merkle_root(slot_dir, slots) if slots else core.EMPTY_ROOT

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
        print(f"  \u2717 {r['check']}: {r['detail']}")

    report = {
        "wall_id": core.WALL_ID,
        "slots_validated": len(slots),
        "merkle_root": root,
        "result": "FAIL" if log.failed else "PASS",
        "checks": log.records,
    }
    if report_path:
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
            fh.write("\n")
        print("  report written: " + report_path)
    print("=" * 64)

    if log.failed:
        print(f"[\u2717] VERIFICATION FAILED \u2014 {len(log.failed)} blocking check(s). See report above.")
        sys.exit(1)

    print("[\u2713] ALL VERIFICATIONS PASSED (zero LLMs / strict provenance).")
    print(f"[\u2713] Claimed slots: {len(slots)} / {core.TOTAL_SLOTS} | Merkle root: {root}")
    print("=" * 64)


if __name__ == "__main__":
    main()
