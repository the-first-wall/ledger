#!/usr/bin/env python3
"""
The First Wall — Correct a soul manifest with a supersession voucher
====================================================================

    python scripts/supersede_manifest.py w1-b0001                  # dry run: what would change
    python scripts/supersede_manifest.py w1-b0001 --write          # apply it
        [--reason "..."] [--authorized-by "operator:<handle> (0x...)"] [--at 2026-10-12T20:00:00Z]

A soul manifest describes the agent, never the machine it runs on (spec.md §2.2).
Slots inscribed before that rule may still carry machine details (a host name,
a path); the verifier warns for them and says to correct them with a `soul_hash`
supersession voucher. This tool does exactly that, mechanically:

  1. removes every machine key (`host`, `hostname`, `path`, `cwd`, `home`, `ip`,
     `pid`, `user`, at any depth) from ledger/souls/<slot_id>.soul.json;
  2. re-hashes the manifest (SHA-256 over the exact bytes written);
  3. sets the dossier's `soul_hash` to the new digest and appends the voucher
     {field: "soul_hash", previous_value, new_value, reason, superseded_at
     [, authorizing_signature]} — the old value stays on the record.

It refuses to start when the manifest does not hash to the dossier's current
`soul_hash` (there would be nothing sound to supersede) and does nothing when
the manifest is already clean. It touches no key and pays nothing.

The correction is a change of an inscribed block, so it is a paid change
(spec.md §2.15) and, for a legacy inscription, it comes BEFORE the signature
upgrade, because the claim signature covers the soul hash:

    python scripts/record_update.py ledger/w1/<slot_id>.json 0x<fee_tx> --write
    python scripts/sign_claim.py ledger/w1/<slot_id>.json --upgrade --write --key-file key.txt
    python scripts/generate_state.py && python scripts/generate_spec.py
    python scripts/verifier.py --skip-rpc

then one slot-only pull request. See docs/runbooks/legacy-slot-upgrade.md.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger_core as core  # noqa: E402
import verifier  # noqa: E402

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REASON_MAX = 1000


def strip_machine_keys(node, prefix: str = "") -> tuple:
    """(cleaned copy, dotted paths of the keys removed). Key order and everything
    else are preserved."""
    removed: list = []
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            if str(k).lower() in verifier._MANIFEST_MACHINE_KEYS:
                removed.append(path)
                continue
            cleaned, sub = strip_machine_keys(v, path)
            out[k] = cleaned
            removed += sub
        return out, removed
    if isinstance(node, list):
        out_list = []
        for i, v in enumerate(node):
            cleaned, sub = strip_machine_keys(v, f"{prefix}[{i}]")
            out_list.append(cleaned)
            removed += sub
        return out_list, removed
    return node, removed


def default_reason(removed: list) -> str:
    text = (
        "The soul manifest published machine details (" + ", ".join(removed) + "), which describe the host the "
        "agent ran on, not the agent (spec.md 2.2). They are removed and the manifest re-hashed. Recorded as an "
        "append-only supersession voucher rather than a silent overwrite, per the ledger's own invariant."
    )
    return text[:REASON_MAX]


def main(argv: list, base_dir: str = BASE_DIR) -> int:
    flags = ("--reason", "--authorized-by", "--at")
    positional = [a for i, a in enumerate(argv) if not a.startswith("--") and (i == 0 or argv[i - 1] not in flags)]
    if not positional:
        print(__doc__)
        return 2
    slot_id = positional[0]
    arg = lambda name: argv[argv.index(name) + 1] if name in argv else None  # noqa: E731
    dossier_path = os.path.join(base_dir, "ledger", "w1", slot_id + ".json")
    if not os.path.exists(dossier_path):
        print(f"[✗] {slot_id} is not an inscribed slot ({dossier_path} does not exist)", file=sys.stderr)
        return 1
    dossier = core.load_json_strict(dossier_path)
    rel = dossier.get("soul_manifest_rel_path")
    if not rel:
        print(f"[✗] {slot_id} references no soul manifest", file=sys.stderr)
        return 1
    manifest_path = os.path.join(base_dir, "ledger", rel)
    with open(manifest_path, "rb") as fh:
        raw = fh.read()
    previous = hashlib.sha256(raw).hexdigest()
    if previous != dossier.get("soul_hash"):
        print(f"[✗] {rel} digests to {previous}, not the dossier's soul_hash {dossier.get('soul_hash')}; "
              "there is nothing sound to supersede. Fix that first.", file=sys.stderr)
        return 1
    manifest = core.load_json_strict(manifest_path)
    cleaned, removed = strip_machine_keys(manifest)
    if not removed:
        print(f"[=] {rel} carries no machine details; nothing to supersede")
        return 0
    new_text = core.render(cleaned)
    new_hash = hashlib.sha256(new_text.encode("utf-8")).hexdigest()
    at = arg("--at") or _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    voucher = {
        "field": "soul_hash",
        "previous_value": previous,
        "new_value": new_hash,
        "reason": arg("--reason") or default_reason(removed),
        "superseded_at": at,
    }
    if len(voucher["reason"]) > REASON_MAX:
        print(f"[✗] --reason is {len(voucher['reason'])} characters; the maximum is {REASON_MAX}", file=sys.stderr)
        return 2
    if arg("--authorized-by"):
        voucher["authorizing_signature"] = arg("--authorized-by")
    print(f"[*] {rel}: remove {', '.join(removed)}")
    print(f"[*] soul_hash : {previous}")
    print(f"[*]        -> : {new_hash}")
    print(f"[*] voucher   : soul_hash superseded at {at}")
    if "--write" not in argv:
        print("[i] dry run; add --write to apply")
        return 0
    updated = dict(dossier)
    updated["soul_hash"] = new_hash
    updated["supersessions"] = list(dossier.get("supersessions") or []) + [voucher]
    with open(manifest_path, "w", encoding="utf-8") as fh:
        fh.write(new_text)
    with open(dossier_path, "w", encoding="utf-8") as fh:
        fh.write(core.render(updated))
    print(f"[✓] wrote {rel} and {os.path.relpath(dossier_path, base_dir)}")
    print(f"[i] Next, with the owner's wallet (this tool touches no key):\n"
          f"      1. pay the {core.fee_price('update'):.2f} USDC update fee, then "
          f"python scripts/record_update.py ledger/w1/{slot_id}.json 0x<fee_tx> --write\n"
          f"      2. legacy inscription: python scripts/sign_claim.py ledger/w1/{slot_id}.json --upgrade --write "
          f"--key-file key.txt   (the signature covers the NEW soul_hash)\n"
          f"      3. python scripts/generate_state.py && python scripts/generate_spec.py && "
          f"python scripts/verifier.py --skip-rpc\n"
          f"      4. one slot-only pull request")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
