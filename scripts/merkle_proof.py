#!/usr/bin/env python3
"""
The First Wall — Inclusion proofs for the TFW2 state root
=========================================================

Every claimed slot is a leaf of an RFC 6962 Merkle tree (see ledger_core).
This tool produces the audit path from one slot to the root committed on Base,
and verifies such a proof against nothing but the slot's two files.

    python scripts/merkle_proof.py w1-b0001                  # proof as JSON on stdout
    python scripts/merkle_proof.py w1-b0001 --out proof.json
    python scripts/merkle_proof.py --verify proof.json ledger/w1/w1-b0001.json ledger/w1/w1-b0001.webp
    python scripts/merkle_proof.py --verify proof.json <dossier> <icon> --root <hex>   # pin the root

A proof is only as good as the root it ends in: compare `root` with the one
anchored on Base (ledger/anchors.json, payload `TFW2|<root>`).
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger_core as core  # noqa: E402

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def build_proof(slot_id: str) -> dict:
    slot_dir = core.ledger_w1_dir(BASE_DIR)
    slots = core.list_slot_files(slot_dir)
    leaves = core.compute_leaves(slot_dir, slots)
    ids = [sid for sid, _ in leaves]
    if slot_id not in ids:
        raise SystemExit(f"[✗] {slot_id} is not a claimed slot")
    index = ids.index(slot_id)
    hashes = [h for _, h in leaves]
    path = core.inclusion_path(hashes, index)
    return {
        "scheme": core.ROOT_SCHEME,
        "slot_id": slot_id,
        "leaf_index": index,
        "tree_size": len(hashes),
        "leaf_hash": hashes[index].hex(),
        "path": [p.hex() for p in path],
        "root": core.merkle_tree_hash(hashes).hex(),
    }


def verify_proof(proof: dict, dossier_path: str, icon_path: str, root: str | None) -> str:
    with open(dossier_path, "rb") as fh:
        data = fh.read()
    with open(icon_path, "rb") as fh:
        data += fh.read()
    leaf = core.leaf_hash(data)
    if leaf.hex() != proof.get("leaf_hash"):
        raise ValueError("the files do not hash to the proof's leaf_hash")
    computed = core.root_from_path(leaf, int(proof["leaf_index"]), int(proof["tree_size"]),
                                   [bytes.fromhex(p) for p in proof["path"]])
    expected = (root or proof.get("root") or "").lower()
    if computed.hex() != expected:
        raise ValueError(f"path leads to root {computed.hex()}, not {expected}")
    return computed.hex()


def main(argv: list) -> int:
    if "--verify" in argv:
        i = argv.index("--verify")
        try:
            proof_path, dossier_path, icon_path = argv[i + 1:i + 4]
        except ValueError:
            print("usage: merkle_proof.py --verify <proof.json> <dossier.json> <icon.webp> [--root <hex>]",
                  file=sys.stderr)
            return 2
        root = argv[argv.index("--root") + 1] if "--root" in argv else None
        with open(proof_path, "r", encoding="utf-8") as fh:
            proof = json.load(fh)
        try:
            r = verify_proof(proof, dossier_path, icon_path, root)
        except (ValueError, KeyError) as e:
            print(f"[✗] Proof INVALID: {e}")
            return 1
        print(f"[✓] {proof.get('slot_id')} is leaf {proof.get('leaf_index')} of {proof.get('tree_size')} "
              f"under root {r}" + ("" if root else "  (now compare this root with the anchor on Base)"))
        return 0

    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    proof = build_proof(args[0])
    rendered = json.dumps(proof, indent=2) + "\n"
    if "--out" in argv:
        out = argv[argv.index("--out") + 1]
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(rendered)
        print(f"[✓] wrote {out}")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
