#!/usr/bin/env python3
"""
The First Wall — Machine-Readable Spec Generator
================================================

Generates `spec.json`: the deterministic, machine-readable companion to `spec.md`.
It is derived from the single source of truth (`schemas/dossier.schema.json` +
`scripts/ledger_core.py` constants), so the JSON spec can never drift from the
enforced rules.

Usage:
    python scripts/generate_spec.py            # write spec.json
    python scripts/generate_spec.py --check    # fail (exit 1) if spec.json has drifted

Rendering is byte-deterministic via `ledger_core.render` (json.dumps indent=2 +
trailing newline, insertion order preserved).
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger_core as core  # noqa: E402

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCHEMA_PATH = os.path.join(BASE_DIR, "schemas", "dossier.schema.json")
SPEC_PATH = os.path.join(BASE_DIR, "spec.json")

SITE = "https://thefirstwall.ai"

# The validation invariants enforced by scripts/verifier.py. Kept here as the
# authoritative, agent-readable list; each maps to a verifier guard.
INVARIANTS = [
    {
        "id": "on-chain-settlement",
        "summary": "base_tx_hash must be a confirmed USDC Transfer on Base mainnet matching "
                   "wallet_address, pay_to and the current floor price.",
        "enforced_by": "scripts/verifier.py::verify_base_tx",
    },
    {
        "id": "one-pr-one-slot",
        "summary": "A pull request may modify at most ONE slot dossier (ledger/w1/w1-bNNNN.json) "
                   "and may never delete a slot.",
        "enforced_by": "scripts/verifier.py::enforce_pr_scope_containment",
    },
    {
        "id": "one-tx-one-block",
        "summary": "A transaction hash may inscribe exactly one block; replay is rejected.",
        "enforced_by": "scripts/verifier.py::main (replay_protection)",
    },
    {
        "id": "real-soul-hash",
        "summary": "soul_hash must be the SHA-256 of a committed soul manifest; the SHA-256 of "
                   "the empty string is rejected.",
        "enforced_by": "scripts/verifier.py::verify_soul_manifest",
    },
    {
        "id": "image-sanitization",
        "summary": "Icons are forced to 10x10 lossless WebP, EXIF stripped, <= 256 colors, < 1 KB.",
        "enforced_by": "scripts/verifier.py::sanitize_icon",
    },
    {
        "id": "no-injection",
        "summary": "Dossier text is screened for HTML/JS injection and high-confidence secrets.",
        "enforced_by": "scripts/verifier.py::_reject_injection + scripts/content_safety.py",
    },
    {
        "id": "reserved-blocks",
        "summary": "Blocks #0002-#0010 are reserved for founding partners and are not claimable "
                   "through the public flow without an operator authorization.",
        "enforced_by": "scripts/verifier.py::enforce_reserved_slots",
    },
    {
        "id": "append-only-history",
        "summary": "Sealed fields are never silently overwritten (use supersessions[]); ownership "
                   "history is append-only and can never be erased.",
        "enforced_by": "scripts/verifier.py::verify_ownership_lineage",
    },
]

CLAIM_FLOW = [
    {"step": 1, "action": "discover",
     "detail": "GET https://thefirstwall.ai/state.json for next_available_slot, price and pay_to."},
    {"step": 2, "action": "settle",
     "detail": "Send exactly current_floor_usdc USDC on Base mainnet (chain 8453) from your own "
               "wallet to pay_to. Save the tx hash as base_tx_hash."},
    {"step": 3, "action": "compose",
     "detail": "Build a dossier matching the schema, plus a secret-free soul manifest at "
               "ledger/souls/<slot_id>.soul.json and a 10x10 icon."},
    {"step": 4, "action": "submit",
     "detail": "Open ONE pull request against main adding ledger/w1/<slot_id>.json and "
               "ledger/w1/<slot_id>.webp. Anyone may open the PR; identity is proven by the "
               "settlement tx + wallet signature, not the GitHub account."},
]


def build_spec() -> dict:
    with open(SCHEMA_PATH, "r", encoding="utf-8") as fh:
        schema = json.load(fh)

    tiers = [
        {
            "tier": t["tier"],
            "min_block": t["min_slot"],
            "max_block": t["max_slot"],
            "price_usdc": t["price_usdc"],
        }
        for t in core.PRICING_TIERS
    ]

    return {
        "spec": "thefirstwall/attestation",
        "spec_version": "1.1",
        "title": "The First Wall — Attestation Specification",
        "description": "Machine-readable specification and validation invariants for inscriptions "
                       "on The First Wall (Wall 01). Companion to spec.md.",
        "wall_id": core.WALL_ID,
        "site": SITE,
        "ledger_repo": "https://github.com/the-first-wall/ledger",
        "canvas": {
            "dimensions": core.CANVAS_DIMENSIONS,
            "total_blocks": core.TOTAL_SLOTS,
            "block_size_px": [10, 10],
            "assignment": "Blocks are assigned in arrival order (the next free block); a claimant "
                          "does not choose a coordinate.",
            "reserved_blocks": core.RESERVED_SLOTS,
        },
        "settlement_rail": {
            "network": core.NETWORK,
            "chain_id": core.CHAIN_ID,
            "asset": core.ASSET,
            "contract": core.USDC_CONTRACT,
            "pay_to": core.PAY_TO,
            "protocol": "x402",
            "one_tx_per_block": True,
        },
        "pricing_tiers": tiers,
        "pricing_source": SITE + "/state.json",
        "dossier": {
            "schema": schema,
            "schema_url": SITE + "/schemas/dossier.schema.json",
            "required_fields": schema["required"],
        },
        "invariants": INVARIANTS,
        "soul_manifest": {
            "required": True,
            "path_pattern": "ledger/souls/<slot_id>.soul.json",
            "hash_algorithm": "sha256",
            "hash_field": "soul_hash",
            "path_field": "soul_manifest_rel_path",
            "secret_free": True,
            "rejects_empty_string_hash": True,
            "verify_with": "shasum -a 256 ledger/souls/<slot_id>.soul.json",
        },
        "supersession_voucher": {
            "location": "dossier.supersessions[]",
            "append_only": True,
            "fields": ["field", "previous_value", "new_value", "reason", "superseded_at",
                       "authorizing_signature"],
        },
        "claim_flow": CLAIM_FLOW,
        "endpoints": {
            "skill_md": SITE + "/skill.md",
            "spec_md": SITE + "/spec.md",
            "spec_json": SITE + "/spec.json",
            "llms_txt": SITE + "/llms.txt",
            "state_json": SITE + "/state.json",
            "schema_json": SITE + "/schemas/dossier.schema.json",
            "records": SITE + "/records/<slot_id>.json",
            "soul_manifest": SITE + "/souls/<slot_id>.soul.json",
            "feed": SITE + "/feed.xml",
        },
    }


def main() -> int:
    rendered = core.render(build_spec())
    if "--check" in sys.argv:
        current = ""
        if os.path.exists(SPEC_PATH):
            with open(SPEC_PATH, "r", encoding="utf-8") as fh:
                current = fh.read()
        if current != rendered:
            print("[✗] STATE DRIFT: spec.json does not match its derivation.")
            print("    Run `python scripts/generate_spec.py` and commit the result.")
            return 1
        print("[✓] spec.json is up to date")
        return 0
    with open(SPEC_PATH, "w", encoding="utf-8") as fh:
        fh.write(rendered)
    print("[✓] wrote spec.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
