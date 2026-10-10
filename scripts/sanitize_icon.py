#!/usr/bin/env python3
"""
The First Wall — Icon Canonicalizer (claimant tool)
===================================================

Turns a 10x10 image in any common format into the exact bytes the ledger
accepts: a bare lossless WebP (single VP8L chunk, no metadata), <= 256 colours,
<= 1 KB. The verifier never rewrites a committed icon, so run this BEFORE you
commit `ledger/w1/<slot_id>.webp`.

Usage:
    python scripts/sanitize_icon.py my-icon.png ledger/w1/w1-b0011.webp
    python scripts/sanitize_icon.py --check ledger/w1/w1-b0011.webp

Rules (the verifier enforces the same ones, see spec.md §2.8):
  * input must already be exactly 10x10 — it is NOT resized;
  * at most 256 distinct colours;
  * input file at most 50 KB.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verifier  # noqa: E402


def main(argv: list) -> int:
    if len(argv) == 2 and argv[0] == "--check":
        try:
            print("[✓] " + verifier.verify_icon(argv[1]))
            return 0
        except Exception as e:  # noqa: BLE001
            print(f"[✗] {argv[1]}: {e}", file=sys.stderr)
            return 1
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    src, dst = argv
    try:
        size = verifier.sanitize_icon(src, dst)
    except Exception as e:  # noqa: BLE001
        print(f"[✗] {src}: {e}", file=sys.stderr)
        return 1
    print(f"[✓] wrote {dst} ({size} bytes): " + verifier.verify_icon(dst))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
