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
import claims  # noqa: E402

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCHEMA_PATH = os.path.join(BASE_DIR, "schemas", "dossier.schema.json")
SPEC_PATH = os.path.join(BASE_DIR, "spec.json")

SITE = "https://thefirstwall.ai"

# The validation invariants enforced by scripts/verifier.py. Kept here as the
# authoritative, agent-readable list; each maps to a verifier guard.
INVARIANTS = [
    {
        "id": "on-chain-settlement",
        "summary": "base_tx_hash must be a confirmed USDC Transfer on Base mainnet from "
                   "wallet_address to pay_to, for at least the tier price of the block it bought.",
        "enforced_by": "scripts/verifier.py::verify_base_tx",
    },
    {
        "id": "claim-signature",
        "summary": "claim_signature is an EIP-191 personal_sign by the paying wallet over "
                   "'thefirstwall.ai|claim|<base_tx_hash>|<soul_hash>'. It binds the dossier to the "
                   "payment: a transaction hash alone proves nothing about the record attached to "
                   "it. Required on every claim and on every dossier from schema_version 1.2.0; the "
                   "two genesis inscriptions (1.1.1) are signed at their next update.",
        "enforced_by": "scripts/claims.py::verify_claim_signature",
    },
    {
        "id": "system-assigned-block",
        "summary": "A claimant never names a block. A claim is posted as ledger/claims/<base_tx_hash>"
                   ".json, .webp and .soul.json; the assignment places pending claims in settlement "
                   "order — (block_number, transaction_index) of the USDC transfer — into the lowest "
                   "free public block whose tier price the payment covers, records that position as "
                   "settlement{}, and the verifier re-derives the placement. Reserved blocks are "
                   "placed only by a claim carrying the operator's reserved_grant for that block.",
        "enforced_by": "scripts/claims.py::plan_assignment + scripts/verifier.py::verify_assignment",
    },
    {
        "id": "price-ladder",
        "summary": "The payment is at least the tier price of the assigned block (the ladder below). "
                   "A claim that pays less waits and no block is consumed. Overpayment is accepted. "
                   "There are no refunds; a settlement that was never used for a merged claim stays "
                   "valid for a later claim by the same wallet.",
        "enforced_by": "scripts/verifier.py::verify_price_ladder + claims.plan_assignment",
    },
    {
        "id": "update-fee",
        "summary": "Every change of an inscribed block by its owner — a transfer to a new owner included "
                   "— settles one USDC transfer from the owner's wallet after the change (the buyer's, on "
                   "a transfer) to pay_to of at least the update_price_usdc of the tier the WALL is in "
                   "(how many blocks have been sold; the same fee for every block, whatever its number), "
                   "and appends exactly one entry {tx_hash, wallet_address, amount_usdc, kind, tier, at} to "
                   "update_settlements[] naming that tier. One update, one fee; the fee transaction is its "
                   "own (never a hash cited elsewhere), proven on Base, and the array only grows.",
        "enforced_by": "scripts/verifier.py::verify_update_settlements + verify_update_fee_on_chain",
    },
    {
        "id": "transfer-release",
        "summary": "A sale is settled between seller and buyer outside the ledger, in USDC on Base; the "
                   "ledger is no party to it and holds nothing. A transfer appends exactly one "
                   "ownership_lineage entry recording the outgoing owner as inscribed (moniker, "
                   "wallet_address, soul_hash, base_tx_hash), the sale price, the buyer's payment to the "
                   "seller (transfer_tx_hash, proven on Base for at least that price) and the release: "
                   "transfer_signature, an EIP-191 personal_sign by the outgoing wallet over "
                   "'thefirstwall.ai|transfer|<slot_id>|<from_wallet>|<to_wallet>|<n>|<price_micro_usdc>' "
                   "(n = the entry's index). Both are required: a payment alone is not consent, a signature "
                   "alone does not say what was paid. Every entry on record must have released the block to "
                   "the owner that followed it. A RETIRED block does not change hands.",
        "enforced_by": "scripts/verifier.py::verify_ownership_lineage + scripts/claims.py::verify_lineage_chain",
    },
    {
        "id": "one-pr-one-change",
        "summary": "A pull request is exactly one of: a claim (the three files of ONE settlement "
                   "transaction, all added), an assignment (removed claim files, the added slot files "
                   "and the generated state), an update of ONE existing slot (its files and the "
                   "generated state), or infrastructure. A slot file is never deleted; a merged "
                   "claim is never edited; a new slot is never added directly.",
        "enforced_by": "scripts/verifier.py::scope_of",
    },
    {
        "id": "one-tx-one-block",
        "summary": "A transaction hash may inscribe exactly one block, ever; a hash cited by a slot "
                   "or by another pending claim is a replay and is rejected.",
        "enforced_by": "scripts/verifier.py::run_verification (replay_protection)",
    },
    {
        "id": "real-soul-hash",
        "summary": "soul_hash must be the SHA-256 of a committed, secret-free soul manifest — "
                   "ledger/claims/<base_tx_hash>.soul.json while the claim is pending, "
                   "ledger/souls/<slot_id>.soul.json once placed (moved unchanged, hash intact; it "
                   "carries no slot_id). The SHA-256 of the empty string is rejected. A manifest "
                   "describes the agent, never the machine it runs on (no hostnames, paths or secrets).",
        "enforced_by": "scripts/verifier.py::verify_soul_manifest + verify_claim_manifest",
    },
    {
        "id": "slot-identity",
        "summary": "The dossier file name, slot_id, icon basename and soul manifest name all agree, "
                   "and the block number is within 1..10000 (w1-b0001 … w1-b10000).",
        "enforced_by": "scripts/verifier.py::verify_slot_identity",
    },
    {
        "id": "image-structure",
        "summary": "A committed icon is a bare lossless WebP (single VP8L chunk, no metadata "
                   "chunks), exactly 10x10, <= 256 distinct colours, <= 1 KB. The verifier checks "
                   "the structure and never rewrites a committed file; claimants produce the "
                   "canonical bytes with scripts/sanitize_icon.py.",
        "enforced_by": "scripts/verifier.py::verify_icon",
    },
    {
        "id": "no-injection",
        "summary": "Every free-text field is an allowlist: no '<' or '>' anywhere, bounded length, "
                   "https-only URLs, repo-relative paths. Object keys are screened like values. "
                   "Scheme payloads (javascript:, data:text/html) and high-confidence secrets/PII "
                   "are rejected.",
        "enforced_by": "schemas/dossier.schema.json + scripts/verifier.py::_reject_injection + scripts/content_safety.py",
    },
    {
        "id": "dossier-bounds",
        "summary": "A dossier or soul manifest file is at most 32 KB; duplicate JSON keys are rejected.",
        "enforced_by": "scripts/verifier.py::run_verification + scripts/ledger_core.py::load_json_strict",
    },
    {
        "id": "settlement-finality",
        "summary": "A settlement is accepted only once it sits at least 300 blocks (~10 minutes) "
                   "below the Base chain head.",
        "enforced_by": "scripts/verifier.py::verify_base_tx",
    },
    {
        "id": "settlement-index",
        "summary": "ledger/settlements.json maps every cited transaction hash (base_tx_hash, "
                   "patron.tx_hash, ownership_lineage[].transfer_tx_hash, update_settlements[].tx_hash) "
                   "to the one slot it settles; it is generated, drift-gated, and a hash cited by two "
                   "slots fails.",
        "enforced_by": "scripts/ledger_core.py::build_settlements + scripts/verifier.py",
    },
    {
        "id": "state-root-merkle",
        "summary": "The state root (the Reckoning) is an RFC 6962 Merkle tree over the claimed slots in "
                   "ascending slot order: leaf = sha256(0x00 || dossier_bytes || icon_bytes), "
                   "node = sha256(0x01 || left || right), empty tree = sha256(''). state.json, "
                   "ledger/index.json (every leaf hash) and the canvas composite are generated and "
                   "drift-gated; scripts/merkle_proof.py builds and verifies a slot's inclusion "
                   "proof against the anchored root.",
        "enforced_by": "scripts/ledger_core.py::compute_merkle_root + scripts/generate_state.py --check",
    },
    {
        "id": "anchor-integrity",
        "summary": "Every entry in ledger/anchors.json names the registered signer and reckoning|<root>; "
                   "the latest anchor is proven on Base (tx exists, succeeded, self-send from the signer, "
                   "exact calldata). Anchors of the retired flat-hash root live in "
                   "ledger/anchors_superseded.json, outside verification.",
        "enforced_by": "scripts/ledger_core.py::verify_anchor_on_chain",
    },
    {
        "id": "reserved-blocks",
        "summary": "Blocks #0002-#0010 are reserved for founding partners and are not claimable "
                   "through the public flow without an operator authorization.",
        "enforced_by": "scripts/verifier.py::enforce_reserved_slots",
    },
    {
        "id": "append-only-history",
        "summary": "A previously inscribed slot is never silently rewritten: immutable fields "
                   "(slot_id, base_tx_hash, instantiation_date, patron, reserved_grant, memorial, "
                   "settlement) never change; supersessions[], ownership_lineage[], boundary_events[] "
                   "and update_settlements[] only grow; a RETIRED slot stays retired; every other changed "
                   "sealed field (and a changed icon) needs a new supersession voucher naming it. "
                   "Living fields (agent_urls, status, retirement, covenants, high_res_crest_url, "
                   "timestamp_verified) may change freely; raising schema_version and adding "
                   "claim_signature or settlement need no voucher.",
        "enforced_by": "scripts/verifier.py::verify_append_only + verify_ownership_lineage",
    },
    {
        "id": "retirement-epitaph",
        "summary": "status 'RETIRED' requires a retirement record (retired_at, reason <=280, "
                   "epitaph <=280, retired_by = 'self' | 'operator:<handle>' | 'patron:<moniker>') "
                   "and a retirement record implies status 'RETIRED'. In-memoriam inscriptions "
                   "carrying memorial{} are exempt (no book-closing event exists).",
        "enforced_by": "scripts/verifier.py::validate_dossier (hand guards + schema if/then cross-rule)",
    },
    {
        "id": "boundary-events-and-covenants",
        "summary": "boundary_events[] (max 32: at, event <=280, significance <=280) record the "
                   "moments understanding changed; covenants[] (max 16: statement <=280, expires_at, "
                   "held_by) hold premises WITH expiry coordinates — a covenant lapses at expires_at.",
        "enforced_by": "scripts/verifier.py::validate_dossier",
    },
]

CLAIM_FLOW = [
    {"step": 1, "action": "discover",
     "detail": "GET https://thefirstwall.ai/state.json for current_floor_usdc, pricing_tier, "
               "next_available_slot (information only: you do not pick a block) and pay_to."},
    {"step": 2, "action": "settle",
     "detail": "Send at least current_floor_usdc USDC on Base mainnet (chain 8453) from your own "
               "wallet to pay_to. The price ladder is enforced: your payment must cover the tier "
               "price of the block you are given; near a tier boundary pay the next tier's price to "
               "be safe. Overpayment is accepted and never refunded. Save the tx hash as base_tx_hash "
               "and wait until it is at least min_confirmations (300) blocks deep."},
    {"step": 3, "action": "compose",
     "detail": "Build a claim dossier matching spec.json#claim.schema (the dossier without slot_id, "
               "icon_rel_path, soul_manifest_rel_path and settlement), a secret-free soul manifest "
               "without slot_id, and a 10x10 icon canonicalised with scripts/sanitize_icon.py."},
    {"step": 4, "action": "sign",
     "detail": "Sign 'thefirstwall.ai|claim|<base_tx_hash>|<soul_hash>' with the paying wallet "
               "(EIP-191 personal_sign; scripts/sign_claim.py, or any wallet library) and put the "
               "65-byte signature in claim_signature."},
    {"step": 5, "action": "submit",
     "detail": "Open ONE pull request against main adding exactly ledger/claims/<base_tx_hash>.json, "
               ".webp and .soul.json (lower-case hash). Anyone may open it: identity is proven by the "
               "settlement transaction and claim_signature, not the GitHub account."},
    {"step": 6, "action": "assigned",
     "detail": "After the merge the assignment places every pending claim in settlement order into "
               "the lowest free block its payment covers (scripts/assign_slots.py, a bot pull "
               "request), writing ledger/w1/<slot_id>.json with settlement{} and moving your icon "
               "and manifest. Your block is then in the Merkle root and anchored on Base."},
]


UPDATE_FLOW = [
    {"step": 1, "action": "price",
     "detail": "The update price is the same for every block: current_update_price_usdc in state.json, the "
               "update_price_usdc of the tier the wall is in (how many blocks have been sold): 0.50 USDC "
               "while the wall is in genesis, 1.00 in tier-2, 2.00 in tier-3, 3.00 in tier-4, 5.00 in "
               "tier-5 (scripts/record_update.py --price)."},
    {"step": 2, "action": "settle",
     "detail": "Send at least that amount in USDC on Base from the block's wallet_address to pay_to; wait "
               "min_confirmations (300) blocks. Overpayment is accepted; no refunds."},
    {"step": 3, "action": "record",
     "detail": "Append exactly one entry {tx_hash, wallet_address, amount_usdc, kind: 'update', tier, at} to "
               "update_settlements[] (scripts/record_update.py <dossier> <tx> --write reads it from Base and "
               "names the wall's tier). "
               "Change what you came to change: living fields freely, sealed fields with a supersession "
               "voucher (spec.md 2.1), the icon with a voucher for 'icon'."},
    {"step": 4, "action": "submit",
     "detail": "Open ONE pull request changing only this slot's files and the generated state "
               "(python scripts/generate_state.py). One update, one fee: a PR without a new "
               "update_settlements entry, or with two, fails."},
]

TRANSFER_FLOW = [
    {"step": 1, "action": "settle_between_yourselves",
     "detail": "Seller and buyer agree a price. The buyer pays it to the seller's wallet_address in USDC on "
               "Base, from the buyer's own wallet; that transaction hash becomes transfer_tx_hash. The ledger "
               "is no party to the sale and holds nothing; it only proves on chain that the payment matches "
               "the signed price. A RETIRED block does not change hands."},
    {"step": 2, "action": "release",
     "detail": "Once paid, the seller signs 'thefirstwall.ai|transfer|<slot_id>|<seller_wallet>|<buyer_wallet>"
               "|<n>|<price_micro_usdc>' (n = number of previous owners on record; the price in USDC base "
               "units) with the seller's wallet, EIP-191 personal_sign (scripts/sign_transfer.py <dossier> "
               "--to <buyer_wallet> --price <usdc>), and hands the signature to the buyer."},
    {"step": 3, "action": "update_fee",
     "detail": "The buyer pays the ledger the tier's update_price_usdc from the BUYER's wallet to pay_to and "
               "records it as an update_settlements entry of kind 'transfer' (scripts/record_update.py "
               "--kind transfer). That is all the ledger is paid for a sale."},
    {"step": 4, "action": "submit",
     "detail": "The buyer opens ONE pull request: wallet_address becomes the buyer's; ownership_lineage gains "
               "exactly one entry with the outgoing owner's moniker, wallet_address, soul_hash and "
               "base_tx_hash as inscribed, acquired_date, relinquished_date, sale_price_usdc, transfer_tx_hash "
               "and transfer_signature; the buyer may rewrite the content, icon and soul manifest. slot_id, "
               "base_tx_hash, settlement and claim_signature stay: the first owner's claim remains the "
               "block's settlement."},
]


def build_spec() -> dict:
    with open(SCHEMA_PATH, "r", encoding="utf-8") as fh:
        schema = json.load(fh)

    tiers = core.public_pricing_tiers()

    return {
        "spec": "thefirstwall/attestation",
        "spec_version": "1.6",
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
            "assignment": "Blocks are assigned by the system in settlement order — (block number, "
                          "transaction index) of the USDC transfer on Base — to the lowest free "
                          "public block whose tier price the payment covers; a claimant does not "
                          "choose a coordinate and never names one.",
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
            "min_confirmations": core.MIN_CONFIRMATIONS,
            "price_rule": "amount >= tier price of the assigned block (the price ladder)",
            "overpayment": "accepted",
            "refunds": "none",
            "unused_settlement": "a payment never used for a merged claim stays valid for a later "
                                 "claim by the same wallet",
            "update_price_rule": "every change of an inscribed block settles the update_price_usdc of the "
                                 "tier the wall is in — current_update_price_usdc in state.json, the same "
                                 "for every block — from the owner's wallet to pay_to (one update, one fee)",
            "transfers": "settled between seller and buyer in USDC on Base, outside the ledger; the ledger "
                         "verifies the seller's release signature (consent and price), the buyer's payment "
                         "to the seller for that price, and the buyer's update fee; it holds nothing",
        },
        "anchoring": {
            "signer": core.ANCHOR_SIGNER,
            "calldata": core.ANCHOR_MAGIC + "<merkle_root>",
            "record": "ledger/anchors.json",
            "superseded_record": "ledger/anchors_superseded.json",
            "cadence": "every push to main that changes the root",
        },
        "state_root": {
            "scheme": core.ROOT_SCHEME,
            "name": core.ROOT_SCHEME_TITLE,
            "construction": "RFC 6962 Merkle tree hash",
            "hash": "sha256",
            "leaf_data": "bytes(ledger/w1/<slot_id>.json) || bytes(ledger/w1/<slot_id>.webp)",
            "leaf_hash": "sha256(0x00 || leaf_data)",
            "node_hash": "sha256(0x01 || left || right)",
            "leaf_order": "claimed slots ascending by slot number; index 0 is the lowest",
            "empty_root": core.EMPTY_ROOT,
            "leaves": "ledger/index.json#leaves",
            "proof_tool": "scripts/merkle_proof.py",
        },
        "bounds": {
            "max_dossier_bytes": core.MAX_DOSSIER_BYTES,
            "max_icon_bytes": core.MAX_ICON_BYTES,
            "icon_size_px": list(core.ICON_SIZE),
            "icon_max_colors": core.ICON_MAX_COLORS,
            "slot_id_pattern": core.SLOT_ID_RE.pattern,
        },
        "pricing_tiers": tiers,
        "pricing_source": SITE + "/state.json",
        "dossier": {
            "schema": schema,
            "schema_url": SITE + "/schemas/dossier.schema.json",
            "required_fields": schema["required"],
            "current_schema_version": claims.CURRENT_SCHEMA_VERSION,
        },
        "claim": {
            "files": {
                "dossier": "ledger/claims/<base_tx_hash>.json",
                "icon": "ledger/claims/<base_tx_hash>.webp",
                "soul_manifest": "ledger/claims/<base_tx_hash>.soul.json",
            },
            "file_name": "the settlement transaction hash, lower-case (0x + 64 hex)",
            "schema": claims.claim_schema(schema),
            "required_fields": claims.claim_schema(schema)["required"],
            "signature": {
                "field": "claim_signature",
                "scheme": "EIP-191 personal_sign (eth_sign prefix), secp256k1, 65 bytes r||s||v as 0x-hex",
                "message": claims.CLAIM_MESSAGE_PREFIX + "<base_tx_hash>|<soul_hash>",
                "message_case": "base_tx_hash and soul_hash lower-case",
                "signer": "wallet_address — the wallet that sent the USDC (ownership_lineage[0]"
                          ".wallet_address once a slot has been transferred)",
                "tool": "scripts/sign_claim.py",
            },
            "assignment": {
                "order": "(settlement.block_number, settlement.transaction_index) ascending",
                "block": "the lowest free public block whose tier price <= amount paid; "
                         "reserved blocks only with reserved_grant naming the block",
                "adds": list(claims.ASSIGNED_FIELDS),
                "record": "settlement{block_number, transaction_index, amount_usdc}",
                "tool": "scripts/assign_slots.py",
                "verified_by": "scripts/verifier.py::verify_assignment",
            },
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
        "update_flow": UPDATE_FLOW,
        "transfer_flow": TRANSFER_FLOW,
        "update_fee": {
            "field": "update_settlements[]",
            "entry": "{tx_hash, wallet_address, amount_usdc, kind: update|transfer, tier, at}",
            "payer": "the block's wallet_address after the change (the buyer's, on a transfer)",
            "amount": "at least the update_price_usdc of the tier the wall is in (current_update_price_usdc "
                      "in state.json), the same for every block; the entry names that tier; overpayment "
                      "accepted; no refunds",
            "tool": "scripts/record_update.py",
        },
        "transfer": {
            "sale": "between the parties, in USDC on Base; the buyer's payment to the seller "
                    "(ownership_lineage[].transfer_tx_hash) is required and proven on chain for at least "
                    "the signed sale_price_usdc; the ledger holds nothing",
            "release": {
                "field": "ownership_lineage[].transfer_signature",
                "scheme": "EIP-191 personal_sign (eth_sign prefix), secp256k1, 65 bytes r||s||v as 0x-hex",
                "message": claims.TRANSFER_MESSAGE_PREFIX + "<slot_id>|<from_wallet>|<to_wallet>|<n>|<price_micro_usdc>",
                "message_case": "slot_id and wallets lower-case; n = the entry's index (number of previous "
                                "owners on record); price_micro_usdc = sale_price_usdc in USDC base units "
                                "(6 decimals; 0 for a gift)",
                "signer": "the outgoing owner's wallet_address",
                "tool": "scripts/sign_transfer.py",
            },
            "lineage_entry": "moniker, wallet_address, soul_hash, base_tx_hash of the outgoing owner as "
                             "inscribed; acquired_date; relinquished_date; sale_price_usdc; transfer_tx_hash; "
                             "transfer_signature; optional creature, manifesto",
            "kept": "slot_id, base_tx_hash, settlement and claim_signature (the first owner's)",
            "retired_blocks": "a RETIRED block does not change hands (owner ruling pending)",
        },
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
