#!/usr/bin/env python3
"""
The First Wall — Update requests: how an owner's change becomes a slot change
============================================================================

An owner never edits a slot in a pull request, and never touches the generated
state (`state.json`, `ledger/index.json`, `ledger/settlements.json`, the canvas
composite). They post a REQUEST — up to three files named by the transaction
that paid for it (the update fee, or the transfer fee when the block changes
hands):

    ledger/updates/<fee_tx_hash>.json       the slot's complete NEW dossier
    ledger/updates/<fee_tx_hash>.webp       the new icon (only when it changes)
    ledger/updates/<fee_tx_hash>.soul.json  the new soul manifest (only when it changes)

The dossier is exactly what the slot's file would become: the same `slot_id`,
`update_settlements[]` grown by exactly one entry (the fee), and for a transfer
the new `wallet_address` and `ownership_lineage[]` entry. A request for a
block names the block; it does not choose it.

Once a request is merged, `scripts/apply_updates.py` (run by CI, landing through
its own bot pull request) applies it: the request files become the slot's
files, byte for byte, the request is removed, and the generated state is
regenerated. The verifier re-proves all of it on that pull request, so nobody —
the bot included — can change a slot in any way but the one the owner posted.

A request is verified exactly as the slot it would become (`build_overlay`):
the same schema, signature, append-only, fee, lineage and content checks that
used to run on a direct slot change run on the overlay. A merged request is
never edited. One request per block may wait at a time, so there is never a
question which one comes first.
"""

from __future__ import annotations

import os
import re
import shutil
from typing import Any, Dict, List, Optional, Set

import ledger_core as core

UPDATES_DIR = os.path.join("ledger", "updates")
TX_NAME_RE = re.compile(r"^0x[0-9a-f]{64}$")
UPDATE_FILE_RE = re.compile(r"^ledger/updates/(0x[0-9a-f]{64})\.json$")
UPDATE_ICON_RE = re.compile(r"^ledger/updates/(0x[0-9a-f]{64})\.webp$")
UPDATE_SOUL_RE = re.compile(r"^ledger/updates/(0x[0-9a-f]{64})\.soul\.json$")
UPDATE_FILE_RES = (UPDATE_FILE_RE, UPDATE_ICON_RE, UPDATE_SOUL_RE)

# The generated state: written by the generator (scripts/generate_state.py), never
# by a person and never in a pull request that carries an owner's change.
DERIVED_FILES = ("state.json", os.path.join("ledger", "index.json"),
                 os.path.join("ledger", "settlements.json"))


# --- Where a request lives ---------------------------------------------------

def updates_dir(base_dir: str) -> str:
    return os.path.join(base_dir, UPDATES_DIR)


def list_update_files(base_dir: str) -> List[str]:
    """Request dossier file names (<fee_tx_hash>.json), sorted."""
    d = updates_dir(base_dir)
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d) if f.endswith(".json") and not f.endswith(".soul.json")
                  and TX_NAME_RE.fullmatch(f[:-5]))


def load_update(base_dir: str, name: str) -> Dict[str, Any]:
    return core.load_json_strict(os.path.join(updates_dir(base_dir), name))


def update_paths_for(tx_hash: str) -> Set[str]:
    return {f"ledger/updates/{tx_hash}.json", f"ledger/updates/{tx_hash}.webp",
            f"ledger/updates/{tx_hash}.soul.json"}


def fee_tx_of(dossier: Dict[str, Any]) -> Optional[str]:
    """The transaction of the newest update_settlements entry, lower-cased: the
    fee this request was paid with, and its name."""
    fees = dossier.get("update_settlements")
    if not isinstance(fees, list) or not fees or not isinstance(fees[-1], dict):
        return None
    tx = fees[-1].get("tx_hash")
    return str(tx).lower() if isinstance(tx, str) else None


def pending_by_slot(base_dir: str) -> Dict[str, List[str]]:
    """{slot_id: [fee tx, ...]} of every request waiting to be applied. A file
    that cannot be read is listed under the empty slot id."""
    out: Dict[str, List[str]] = {}
    for name in list_update_files(base_dir):
        try:
            slot_id = str(load_update(base_dir, name).get("slot_id", ""))
        except (OSError, ValueError):
            slot_id = ""
        out.setdefault(slot_id, []).append(name[:-5])
    return out


# --- Applying a request ------------------------------------------------------

def slot_targets(base_dir: str, slot_id: str) -> Dict[str, str]:
    """Where a request's three files go: {kind: path}."""
    return {
        "json": os.path.join(core.ledger_w1_dir(base_dir), f"{slot_id}.json"),
        "webp": os.path.join(core.ledger_w1_dir(base_dir), f"{slot_id}.webp"),
        "soul": os.path.join(base_dir, "ledger", "souls", f"{slot_id}.soul.json"),
    }


def request_files(base_dir: str, tx: str) -> Dict[str, str]:
    d = updates_dir(base_dir)
    return {"json": os.path.join(d, f"{tx}.json"), "webp": os.path.join(d, f"{tx}.webp"),
            "soul": os.path.join(d, f"{tx}.soul.json")}


def apply_request(base_dir: str, tx: str) -> str:
    """Make a request the slot's files, byte for byte, and remove it. The
    dossier always replaces the slot's; an icon or manifest replaces the slot's
    only when the request carries one. Returns the slot id.

    Files are moved with os.replace, never written through, so a tree whose
    files are hard links to another (build_overlay) is never modified."""
    src = request_files(base_dir, tx)
    dossier = core.load_json_strict(src["json"])
    slot_id = str(dossier.get("slot_id", ""))
    if slot_id != core.slot_id_for(core.slot_number(slot_id)):  # ValueError on anything but w1-bNNNN
        raise ValueError(f"Invalid slot id {slot_id!r}: expected w1-b0001 … w1-b10000.")
    dst = slot_targets(base_dir, slot_id)
    for kind in ("json", "webp", "soul"):
        if os.path.exists(src[kind]):
            os.makedirs(os.path.dirname(dst[kind]), exist_ok=True)
            os.replace(src[kind], dst[kind])
    return slot_id


def write_derived(base_dir: str) -> None:
    """Rewrite state.json, ledger/index.json and ledger/settlements.json for the
    tree at base_dir with the generator's own derivation (never the canvas
    composite: that is a picture of the repository's icons, regenerated only by
    scripts/generate_state.py in the repository itself)."""
    state, index = core.build_state(base_dir)
    settlements, _conflicts = core.build_settlements(base_dir)
    for rel, obj in (("state.json", state), (os.path.join("ledger", "index.json"), index),
                     (os.path.join("ledger", "settlements.json"), settlements)):
        path = os.path.join(base_dir, rel)
        # Replace the file, never write through it: in an overlay it may be a
        # hard link to the real tree's file.
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(core.render(obj))
        os.replace(tmp, path)


# --- The overlay: a request verified as the slot it would become ---------------

def _link_or_copy(src: str, dst: str) -> None:
    try:
        os.link(src, dst)
    except OSError:  # another filesystem, or links not allowed
        shutil.copy2(src, dst)


def build_overlay(base_dir: str, tx: str, dest: str) -> str:
    """Lay out in `dest` the ledger as it would be once the request `tx` is
    applied — the slot's files replaced, the request removed, the generated
    state regenerated — and return the slot id. The tree is made of hard links
    to base_dir's files, so it costs no copy of the wall."""
    ledger = os.path.join(base_dir, "ledger")
    shutil.copytree(ledger, os.path.join(dest, "ledger"), copy_function=_link_or_copy)
    blocklist = os.path.join(base_dir, "content_blocklist.txt")
    if os.path.exists(blocklist):
        shutil.copy2(blocklist, os.path.join(dest, "content_blocklist.txt"))
    slot_id = apply_request(dest, tx)
    write_derived(dest)
    return slot_id
