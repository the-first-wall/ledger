#!/usr/bin/env python3
"""
The First Wall — Published records: per-slot inclusion proof + receipt (P-55)
=============================================================================

For every claimed slot the generator writes

    records/<slot_id>.proof.json

a generated artifact (never hand-edited; `scripts/generate_state.py` writes it
and `--check` fails on drift) that the web mirror serves next to the record. It
makes "Merkle commit proof" resolve to real bytes:

    {schema, wall_id, slot_id,
     scheme, leaf_index, tree_size, leaf_hash, path, root,     <- scripts/merkle_proof.py
     files: {dossier, icon}, verify,
     liveness: {...} | null,                                   <- P-64, derived
     receipt: {payer, amount_usdc, tx_hash, block_number, confirmations,
               last_confirmed_at, bundle_sha256, anchor_tx, proof_path}}

The proof verifies against nothing but the slot's two committed files:
`python scripts/merkle_proof.py --verify records/<slot>.proof.json
ledger/w1/<slot>.json ledger/w1/<slot>.webp --root <root anchored on Base>`.

The receipt is ONE object per record, every field derived from committed bytes:

* payer         — the wallet that settled the inscription (the first owner on
                  record; the address the verifier proves the USDC Transfer from);
* amount_usdc, block_number — the dossier's `settlement` (null on a legacy
                  record sealed without one);
* tx_hash       — base_tx_hash;
* confirmations — confirmations of the settlement at the anchoring block
                  (anchor block - settlement block + 1): a lower bound that only
                  grows; null while the current root is not yet anchored;
* last_confirmed_at — the newest instant the dossier itself attests (the
                  inscription's timestamp_verified, a settled update fee's `at`,
                  or a witnessed heartbeat); null when it attests none (P-54);
* bundle_sha256 — the bundle hash of the committed dossier + icon (spec.md §2.14);
* anchor_tx     — the Base transaction that anchored the current root
                  (ledger/anchors.json), null while unanchored;
* proof_path    — this file's path.

Pure function of the ledger: no clock, no network, no keys.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claims  # noqa: E402
import ledger_core as core  # noqa: E402
import merkle_proof  # noqa: E402

RECORDS_DIR = "records"
PROOF_SCHEMA = "thefirstwall.proof/1"
RECORD_FILE_RE = re.compile(r"^records/(w1-b(?:\d{4}|10000))\.proof\.json$")
RECEIPT_KEYS = ("payer", "amount_usdc", "tx_hash", "block_number", "confirmations",
                "last_confirmed_at", "bundle_sha256", "anchor_tx", "proof_path")


def _instant(iso: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:  # noqa: BLE001 — an unparseable stamp is no instant, never a crash
        return None


def last_confirmed_at(dossier: Dict[str, Any]) -> Optional[str]:
    """The newest instant the dossier itself attests (P-54): the inscription's
    timestamp_verified, every settled update fee's `at` (core.attested_timestamps), and the last
    witnessed heartbeat. A pure function of the committed bytes — never the clock — and null when
    the dossier attests none, so an ACTIVE record is never an undated placeholder."""
    stamps = list(core.attested_timestamps(dossier))
    seen = dossier.get("last_witnessed_at")
    if isinstance(seen, str) and seen:
        stamps.append(seen)
    found = [(dt, s) for dt, s in ((_instant(s), s) for s in stamps) if dt]
    return max(found, key=lambda pair: pair[0])[1] if found else None


def proof_rel(slot_id: str) -> str:
    return f"{RECORDS_DIR}/{slot_id}.proof.json"


def current_anchor(base_dir: str, root: str) -> Optional[Dict[str, Any]]:
    """The newest recorded anchor that commits exactly `root`, or None."""
    path = os.path.join(base_dir, "ledger", "anchors.json")
    if not os.path.exists(path):
        return None
    anchors = core.load_json_strict(path)
    if not isinstance(anchors, list):
        return None
    hits = [a for a in anchors if isinstance(a, dict) and a.get("root") == root]
    return hits[-1] if hits else None


def _read(path: str) -> Optional[bytes]:
    if not os.path.exists(path):
        return None
    with open(path, "rb") as fh:
        return fh.read()


def receipt(dossier: Dict[str, Any], icon: Optional[bytes], anchor: Optional[Dict[str, Any]],
            slot_id: str) -> Dict[str, Any]:
    settlement = dossier.get("settlement") if isinstance(dossier.get("settlement"), dict) else {}
    block = settlement.get("block_number")
    anchor_block = anchor.get("block_number") if anchor else None
    confirmations = (int(anchor_block) - int(block) + 1
                     if isinstance(block, int) and isinstance(anchor_block, int) and anchor_block >= block
                     else None)
    return {
        "payer": claims.expected_signer(dossier),
        "amount_usdc": settlement.get("amount_usdc"),
        "tx_hash": dossier.get("base_tx_hash"),
        "block_number": block,
        "confirmations": confirmations,
        "last_confirmed_at": last_confirmed_at(dossier),
        "bundle_sha256": claims.bundle_sha256(dossier, icon),
        "anchor_tx": anchor.get("tx_hash") if anchor else None,
        "proof_path": proof_rel(slot_id),
    }


def build_records(base_dir: str) -> Dict[str, Dict[str, Any]]:
    """{relative path: record object} for every claimed slot, in slot order."""
    slot_dir = core.ledger_w1_dir(base_dir)
    slots = core.list_slot_files(slot_dir)
    if not slots:
        return {}
    leaves = core.compute_leaves(slot_dir, slots)
    root = core.merkle_tree_hash([h for _, h in leaves]).hex()
    anchor = current_anchor(base_dir, root)
    as_of = core.ledger_clock(base_dir)
    out: Dict[str, Dict[str, Any]] = {}
    for slot_file in slots:
        slot_id = slot_file[:-5]
        dossier = core.load_dossier(slot_dir, slot_file)
        icon = _read(os.path.join(slot_dir, f"{slot_id}.webp"))
        proof = merkle_proof.build_proof(slot_id, base_dir, leaves=leaves)
        rel = proof_rel(slot_id)
        record = {"schema": PROOF_SCHEMA, "wall_id": core.WALL_ID}
        record.update(proof)
        record["files"] = {"dossier": f"ledger/w1/{slot_id}.json", "icon": f"ledger/w1/{slot_id}.webp"}
        record["verify"] = (f"python scripts/merkle_proof.py --verify {rel} ledger/w1/{slot_id}.json "
                            f"ledger/w1/{slot_id}.webp --root {root}  (then compare the root with the anchor on Base: "
                            f"ledger/anchors.json, calldata reckoning|<root>)")
        record["liveness"] = core.liveness(dossier, as_of)
        record["receipt"] = receipt(dossier, icon, anchor, slot_id)
        out[rel] = record
    return out


def committed_record_files(base_dir: str) -> list:
    d = os.path.join(base_dir, RECORDS_DIR)
    if not os.path.isdir(d):
        return []
    return sorted(f"{RECORDS_DIR}/{f}" for f in os.listdir(d) if f.endswith(".proof.json"))


def drift(base_dir: str) -> list:
    """Problems: a record that is missing, stray, or differs from the derivation."""
    problems = []
    expected = build_records(base_dir)
    for rel, obj in expected.items():
        current = _read(os.path.join(base_dir, rel))
        if current is None:
            problems.append(f"{rel} is missing")
        elif current.decode("utf-8") != core.render(obj):
            problems.append(f"{rel} does not match the derivation")
    for rel in committed_record_files(base_dir):
        if rel not in expected:
            problems.append(f"{rel} is stray (no such claimed slot)")
    return problems


def write_records(base_dir: str) -> list:
    """Write every record, remove stray ones; returns the paths written."""
    expected = build_records(base_dir)
    for rel in committed_record_files(base_dir):
        if rel not in expected:
            os.remove(os.path.join(base_dir, rel))
    for rel, obj in expected.items():
        path = os.path.join(base_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(core.render(obj))
        os.replace(tmp, path)
    return sorted(expected)


def verify_records(base_dir: str) -> str:
    """The verifier's view: every record is the derivation, and every proof
    actually verifies from the slot's committed bytes to the current root."""
    if not os.path.isdir(os.path.join(base_dir, RECORDS_DIR)):
        return "no records/ in this ledger"
    problems = drift(base_dir)
    expected = build_records(base_dir)
    for rel, obj in expected.items():
        try:
            merkle_proof.verify_proof(obj, os.path.join(base_dir, obj["files"]["dossier"]),
                                      os.path.join(base_dir, obj["files"]["icon"]), obj["root"])
        except (ValueError, KeyError, OSError) as e:
            problems.append(f"{rel}: proof does not verify: {e}")
    if problems:
        raise ValueError("Records Violation: " + "; ".join(problems[:5]) +
                         ". Run `python scripts/generate_state.py`.")
    anchored = sum(1 for o in expected.values() if o["receipt"]["anchor_tx"])
    return (f"{len(expected)} inclusion proof(s) verify to the current root; "
            f"{anchored} receipt(s) name the anchoring tx")
