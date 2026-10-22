#!/usr/bin/env python3
"""
The First Wall — Sign the skill
===============================

    python scripts/sign_skill.py --print-message                 # sha256 + the exact text to sign (offline / hardware wallet)
    python scripts/sign_skill.py --signature 0x...               # assemble skill.md.sig from a signature made elsewhere
    python scripts/sign_skill.py --key-env SKILL_SIGNING_KEY     # sign with a key the operator holds in that env var
    python scripts/sign_skill.py --verify                        # check the committed skill.md.sig

Signs `thefirstwall.ai|skill|<sha256(skill.md)>` (EIP-191 personal_sign) and
writes the detached signature to skill.md.sig:

    {"sha256": ..., "signer": ..., "signature": ..., "scheme": "eip191-personal_sign"}

Key-agnostic by design: any wallet that can personal_sign produces the same
signature (`--print-message` gives the text; `--signature` records the result).
This tool never takes a key as an argument, never reads a key file and never
prints or stores a key; `--key-env` reads only the environment variable the
operator names. The signer is always *recovered* from the signature, never typed.
Re-sign after every change to skill.md (state.json's skill_sha256 changes with it).

Exit codes for --verify: 0 valid, 1 forged / stale / malformed, 2 unsigned.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import provenance  # noqa: E402

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _arg(argv: list, name: str):
    i = argv.index(name) if name in argv else -1
    return argv[i + 1] if 0 <= i < len(argv) - 1 else None


def main(argv: list, base_dir: str = BASE_DIR) -> int:
    if not argv or "-h" in argv or "--help" in argv:
        print(__doc__)
        return 2
    try:
        sha = provenance.skill_sha256(base_dir)
    except ValueError as e:
        print(f"[✗] {e}", file=sys.stderr)
        return 1
    if "--verify" in argv:
        try:
            status, detail = provenance.check_skill_signature(base_dir)
        except ValueError as e:
            print(f"[✗] {e}")
            return 1
        print(("[✓] " if status == "valid" else "[!] ") + detail)
        return {"valid": 0, "stale": 1, "unsigned": 2}[status]
    if "--print-message" in argv:
        print(f"sha256 : {sha}")
        print(f"message: {provenance.skill_message(sha)}")
        return 0
    signature = _arg(argv, "--signature")
    if signature is None and "--key-env" in argv:
        var = _arg(argv, "--key-env")
        key = (os.environ.get(var or "", "") or "").strip()
        if not var or not key:
            print(f"[✗] No key: set the environment variable named by --key-env ({var!r}).", file=sys.stderr)
            return 2
        signature = provenance.sign_skill_message(sha, key)
    if signature is None:
        print(__doc__)
        return 2
    try:
        record = provenance.make_signature_record(sha, signature)
    except ValueError as e:
        print(f"[✗] {e}", file=sys.stderr)
        return 1
    with open(os.path.join(base_dir, provenance.SKILL_SIG_FILE), "w", encoding="utf-8") as fh:
        fh.write(provenance.render_signature_record(record))
    print(f"[*] message: {provenance.skill_message(sha)}")
    print(f"[*] signer : {record['signer']}")
    print(f"[✓] wrote {provenance.SKILL_SIG_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
