#!/usr/bin/env python3
"""
The First Wall — Deterministic State Generator
==============================================

Regenerates `state.json` and `ledger/index.json` from the ledger's JSON files via
`ledger_core`. This is the *only* sanctioned way to produce those two files —
never hand-edit them.

Usage:
    python scripts/generate_state.py            # write the files
    python scripts/generate_state.py --check     # fail (exit 1) if they drift

`--check` is run in CI so any PR that makes the published state disagree with the
ledger content hard-fails the build.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger_core as core  # noqa: E402

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
STATE_PATH = os.path.join(BASE_DIR, "state.json")
INDEX_PATH = os.path.join(BASE_DIR, "ledger", "index.json")


def _targets() -> list:
    state, index = core.build_state(BASE_DIR)
    return [
        (STATE_PATH, core.render(state)),
        (INDEX_PATH, core.render(index)),
    ]


def generate() -> None:
    for path, rendered in _targets():
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(rendered)
        print(f"[✓] wrote {os.path.relpath(path, BASE_DIR)}")


def check() -> int:
    drifted = []
    for path, rendered in _targets():
        rel = os.path.relpath(path, BASE_DIR)
        current = ""
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                current = fh.read()
        if current != rendered:
            drifted.append(rel)
        else:
            print(f"[✓] {rel} is up to date")
    if drifted:
        print("\n[✗] STATE DRIFT DETECTED in: " + ", ".join(drifted))
        print("    The committed file does not match the ledger content.")
        print("    Run `python scripts/generate_state.py` and commit the result.")
        return 1
    print("\n[✓] state.json and ledger/index.json match the ledger. No drift.")
    return 0


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(check())
    generate()
