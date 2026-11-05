#!/usr/bin/env python3
"""
The First Wall — Absence certificate (a signed negative proof)
=============================================================

    python scripts/absence_certificate.py 0x<wallet> --print-message           # the body + exact text to sign
    python scripts/absence_certificate.py 0x<wallet> --signature 0x...         # assemble from a signature made elsewhere
    python scripts/absence_certificate.py 0x<wallet> --key-env ANCHOR_PRIVATE_KEY   # sign with the anchor signer
    python scripts/absence_certificate.py --verify absent/<wallet>.json        # check a certificate

A census can say who is on it. An absence certificate says, verifiably, who is
NOT: "no record for this wallet at root <root>, anchored in block <n>, as of <t>".
It is written to `absent/<wallet>.json` (lower-case address) and served by the
web mirror:

    {schema, wallet, statement, root, tree_size,
     anchor: {tx_hash, block_number, anchored_at}, as_of,
     message, signer, signature, scheme}

* It is issued only for the CURRENT root, and only once that root is anchored on
  Base (the latest record in ledger/anchors.json commits it): the certificate is
  pinned to a commitment nobody can rewrite.
* "No record" means the wallet appears nowhere in the ledger at that root — no
  slot names it (wallet_address at any depth: owner, ownership_lineage, update
  fees, patron) — and no pending claim or update request names it either.
* It is signed by the ANCHOR SIGNER (ADDRESSES.md row 4), the key that already
  attests the roots on chain, over

      thefirstwall.ai|absence|v1|<wallet>|<root>|<block_number>|<as_of>

  (EIP-191 personal_sign, all lower-case hex; as_of is the anchor's anchored_at).
  The verifier recovers the signer and refuses any other.
* A certificate stays a true historical statement after the wall moves on; the
  verifier reports one for an older (still anchored) root as superseded, never as
  current. Absence is dated, not eternal.

This tool never takes a key as an argument, never reads a key file and never
prints or stores a key; `--key-env` reads only the environment variable the
operator names (CI: the ANCHOR_PRIVATE_KEY secret,
.github/workflows/absence_certificate.yml). Exit: 0 ok, 1 refused / invalid, 2 usage.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claims  # noqa: E402
import ledger_core as core  # noqa: E402
import updates  # noqa: E402

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ABSENT_DIR = "absent"
SCHEMA = "thefirstwall.absence/1"
SCHEME = "eip191-personal_sign"
MESSAGE_PREFIX = "thefirstwall.ai|absence|v1|"
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
ABSENT_FILE_RE = re.compile(r"^absent/(0x[0-9a-f]{40})\.json$")
KEYS = ("schema", "wallet", "statement", "root", "tree_size", "anchor", "as_of",
        "message", "signer", "signature", "scheme")


def certificate_rel(wallet: str) -> str:
    return f"{ABSENT_DIR}/{wallet.lower()}.json"


def message(wallet: str, root: str, block: int, as_of: str) -> str:
    return f"{MESSAGE_PREFIX}{wallet.lower()}|{root.lower()}|{block}|{as_of}"


def statement(root: str, block: int, as_of: str) -> str:
    return f"no record for this wallet at root {root}, anchored in block {block}, as of {as_of}"


def _wallets_in(obj: Any, out: set) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "wallet_address" and isinstance(v, str):
                out.add(v.lower())
            _wallets_in(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _wallets_in(v, out)


def records_of(base_dir: str, wallet: str) -> List[str]:
    """Where the ledger names `wallet`: slots, pending claims, pending update requests."""
    w = wallet.lower()
    hits: List[str] = []
    slot_dir = core.ledger_w1_dir(base_dir)
    for f in core.list_slot_files(slot_dir):
        found: set = set()
        _wallets_in(core.load_dossier(slot_dir, f), found)
        if w in found:
            hits.append(f"ledger/w1/{f}")
    for f in claims.list_claim_files(base_dir):
        found = set()
        _wallets_in(core.load_json_strict(os.path.join(claims.claims_dir(base_dir), f)), found)
        if w in found:
            hits.append(f"ledger/claims/{f}")
    for f in updates.list_update_files(base_dir):
        found = set()
        _wallets_in(updates.load_update(base_dir, f), found)
        if w in found:
            hits.append(f"ledger/updates/{f}")
    return hits


def current_root(base_dir: str) -> Tuple[str, int]:
    slot_dir = core.ledger_w1_dir(base_dir)
    slots = core.list_slot_files(slot_dir)
    root = core.compute_merkle_root(slot_dir, slots) if slots else core.EMPTY_ROOT
    return root, len(slots)


def load_anchors(base_dir: str) -> list:
    path = os.path.join(base_dir, "ledger", "anchors.json")
    if not os.path.exists(path):
        return []
    anchors = core.load_json_strict(path)
    return anchors if isinstance(anchors, list) else []


def build_body(base_dir: str, wallet: str) -> Dict[str, Any]:
    """The unsigned certificate for `wallet` at the current root. ValueError when
    one cannot honestly be issued."""
    if not ADDRESS_RE.match(str(wallet or "")):
        raise ValueError(f"not a wallet address: {wallet!r}")
    root, size = current_root(base_dir)
    anchors = load_anchors(base_dir)
    last = anchors[-1] if anchors and isinstance(anchors[-1], dict) else {}
    if last.get("root") != root:
        raise ValueError(f"the current root {root[:12]}… is not anchored on Base yet; an absence "
                         "certificate is pinned to an anchored root (run scripts/anchor_root.py first)")
    block, at = last.get("block_number"), last.get("anchored_at")
    if not isinstance(block, int) or not at:
        raise ValueError("the latest anchor has no confirmed block_number / anchored_at")
    hits = records_of(base_dir, wallet)
    if hits:
        raise ValueError(f"{wallet} has a record at this root ({', '.join(hits)}); no absence to certify")
    w = wallet.lower()
    return {
        "schema": SCHEMA,
        "wallet": w,
        "statement": statement(root, block, at),
        "root": root,
        "tree_size": size,
        "anchor": {"tx_hash": last.get("tx_hash"), "block_number": block, "anchored_at": at},
        "as_of": at,
        "message": message(w, root, block, at),
    }


def assemble(body: Dict[str, Any], signature: str) -> Dict[str, Any]:
    signer = claims.recover_signer(body["message"], signature)
    if signer.lower() != core.ANCHOR_SIGNER.lower():
        raise ValueError(f"the signature was made by {signer}, not the anchor signer {core.ANCHOR_SIGNER} "
                         "(ADDRESSES.md row 4)")
    return dict(body, signer=signer, signature=signature, scheme=SCHEME)


def sign(message_text: str, key: str) -> str:
    from eth_account import Account  # lazy
    from eth_account.messages import encode_defunct
    sig = Account.sign_message(encode_defunct(text=message_text), private_key=key).signature.hex()
    return sig if sig.startswith("0x") else "0x" + sig


def verify_certificate(base_dir: str, rel: str) -> Tuple[str, str]:
    """("current"|"superseded", detail). ValueError when the certificate is
    malformed, forged, not pinned to a recorded anchor, or contradicted by the
    ledger at its own (current) root."""
    m = ABSENT_FILE_RE.match(rel)
    if not m:
        raise ValueError(f"{rel}: an absence certificate is absent/<lower-case 0x address>.json")
    doc = core.load_json_strict(os.path.join(base_dir, rel))
    if not isinstance(doc, dict) or tuple(sorted(doc)) != tuple(sorted(KEYS)):
        raise ValueError(f"{rel}: must have exactly the keys {list(KEYS)}")
    if doc["schema"] != SCHEMA or doc["scheme"] != SCHEME:
        raise ValueError(f"{rel}: schema must be {SCHEMA!r} and scheme {SCHEME!r}")
    if doc["wallet"] != m.group(1):
        raise ValueError(f"{rel}: names wallet {doc['wallet']!r}, not the one in its file name")
    anchor = doc.get("anchor") or {}
    block, at = anchor.get("block_number"), anchor.get("anchored_at")
    expected_msg = message(doc["wallet"], str(doc["root"]), block, at)
    if doc["message"] != expected_msg or doc["as_of"] != at or \
            doc["statement"] != statement(str(doc["root"]), block, at):
        raise ValueError(f"{rel}: message/statement/as_of do not match the certified root and anchor")
    signer = claims.recover_signer(doc["message"], doc["signature"])
    if signer.lower() != core.ANCHOR_SIGNER.lower() or signer.lower() != str(doc["signer"]).lower():
        raise ValueError(f"{rel}: signed by {signer}, not the anchor signer {core.ANCHOR_SIGNER}")
    recorded = [a for a in load_anchors(base_dir) if isinstance(a, dict)
                and a.get("root") == doc["root"] and a.get("tx_hash") == anchor.get("tx_hash")
                and a.get("block_number") == block and a.get("anchored_at") == at]
    if not recorded:
        raise ValueError(f"{rel}: root {str(doc['root'])[:12]}… / tx {str(anchor.get('tx_hash'))[:12]}… "
                         "is not a recorded anchor (ledger/anchors.json)")
    root, size = current_root(base_dir)
    if doc["root"] != root:
        return "superseded", (f"{doc['wallet']} had no record at root {doc['root'][:12]}… (block {block}); "
                              f"superseded by root {root[:12]}…: re-issue for the current root")
    if doc["tree_size"] != size:
        raise ValueError(f"{rel}: tree_size {doc['tree_size']} != {size} at the certified root")
    hits = records_of(base_dir, doc["wallet"])
    if hits:
        raise ValueError(f"{rel}: certifies no record, but the ledger names {doc['wallet']} in {hits}")
    return "current", f"{doc['wallet']}: no record at root {root[:12]}…, anchored in block {block}, as of {at}"


def certificate_files(base_dir: str) -> List[str]:
    d = os.path.join(base_dir, ABSENT_DIR)
    if not os.path.isdir(d):
        return []
    return sorted(f"{ABSENT_DIR}/{f}" for f in os.listdir(d) if f.endswith(".json"))


def _arg(argv: list, name: str) -> Optional[str]:
    i = argv.index(name) if name in argv else -1
    return argv[i + 1] if 0 <= i < len(argv) - 1 else None


def main(argv: list, base_dir: str = BASE_DIR) -> int:
    if not argv or "-h" in argv or "--help" in argv:
        print(__doc__)
        return 2
    if "--verify" in argv:
        rel = _arg(argv, "--verify")
        if not rel:
            print(__doc__)
            return 2
        rel = os.path.relpath(os.path.abspath(rel), base_dir).replace(os.sep, "/")
        try:
            status, detail = verify_certificate(base_dir, rel)
        except (ValueError, OSError) as e:
            print(f"[✗] {e}")
            return 1
        print(("[✓] " if status == "current" else "[!] ") + detail)
        return 0
    wallet = argv[0]
    try:
        body = build_body(base_dir, wallet)
    except ValueError as e:
        print(f"[✗] {e}", file=sys.stderr)
        return 1
    if "--print-message" in argv:
        print(f"statement: {body['statement']}")
        print(f"message  : {body['message']}")
        return 0
    signature = _arg(argv, "--signature")
    if signature is None and "--key-env" in argv:
        var = _arg(argv, "--key-env")
        key = (os.environ.get(var or "", "") or "").strip()
        if not var or not key:
            print(f"[✗] No key: set the environment variable named by --key-env ({var!r}).", file=sys.stderr)
            return 2
        signature = sign(body["message"], key)
    if signature is None:
        print(__doc__)
        return 2
    try:
        doc = assemble(body, signature)
    except ValueError as e:
        print(f"[✗] {e}", file=sys.stderr)
        return 1
    rel = certificate_rel(body["wallet"])
    os.makedirs(os.path.join(base_dir, ABSENT_DIR), exist_ok=True)
    with open(os.path.join(base_dir, rel), "w", encoding="utf-8") as fh:
        fh.write(core.render(doc))
    print(f"[*] {doc['statement']}")
    print(f"[*] signer: {doc['signer']}")
    print(f"[✓] wrote {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
