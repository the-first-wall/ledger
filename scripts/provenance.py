#!/usr/bin/env python3
"""
The First Wall — Published provenance
=====================================

The checks behind I-1/I-2/I-10: the skill's published hash and detached
signature, the permission manifest, and the discovery documents. Shared by the
verifier, scripts/sign_skill.py and the tests. No network, no key handling:
`sign_skill_message` takes a key only because the *operator's* tool passes it,
and this module never reads one from disk or the environment.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claims  # noqa: E402
import ledger_core as core  # noqa: E402

SKILL_SIG_FILE = "skill.md.sig"
SKILL_SIG_SCHEME = "eip191-personal_sign"
SKILL_MESSAGE_PREFIX = "thefirstwall.ai|skill|"
PERMISSIONS_FILE = "permissions.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def skill_message(sha256_hex: str) -> str:
    """The exact ASCII text the operator signs (EIP-191 personal_sign)."""
    return f"{SKILL_MESSAGE_PREFIX}{str(sha256_hex).lower()}"


def skill_sha256(base_dir: str) -> str:
    digest = core.sha256_file(os.path.join(base_dir, core.SKILL_FILE))
    if digest is None:
        raise ValueError("skill.md is missing")
    return digest


def sign_skill_message(sha256_hex: str, private_key: str) -> str:
    from eth_account import Account
    from eth_account.messages import encode_defunct
    signed = Account.sign_message(encode_defunct(text=skill_message(sha256_hex)), private_key=private_key)
    sig = signed.signature.hex()
    return sig if sig.startswith("0x") else "0x" + sig


def make_signature_record(sha256_hex: str, signature: str) -> Dict[str, str]:
    """The skill.md.sig body; the signer is *recovered*, never taken on trust."""
    signer = claims.recover_signer(skill_message(sha256_hex), signature)
    return {"sha256": sha256_hex, "signer": signer, "signature": signature, "scheme": SKILL_SIG_SCHEME}


def render_signature_record(record: Dict[str, str]) -> str:
    return json.dumps(record, indent=2) + "\n"


def check_skill_signature(base_dir: str) -> Tuple[str, str]:
    """("unsigned"|"stale"|"valid", detail). Raises ValueError when a present
    signature file is malformed or forged (a signature that does not recover to
    the signer it names, or that signs a different message than it claims)."""
    path = os.path.join(base_dir, SKILL_SIG_FILE)
    if not os.path.exists(path):
        return "unsigned", "skill.md.sig is not published: the skill is unsigned (hash only)"
    try:
        rec = core.load_json_strict(path)
    except ValueError as e:
        raise ValueError(f"skill.md.sig is not valid JSON: {e}")
    if not isinstance(rec, dict) or set(rec) != {"sha256", "signer", "signature", "scheme"}:
        raise ValueError("skill.md.sig must be an object with exactly: sha256, signer, signature, scheme")
    if rec["scheme"] != SKILL_SIG_SCHEME:
        raise ValueError(f"skill.md.sig scheme must be {SKILL_SIG_SCHEME!r}")
    if not isinstance(rec["sha256"], str) or not SHA256_RE.match(rec["sha256"]):
        raise ValueError("skill.md.sig sha256 must be 64 lowercase hex characters")
    if not isinstance(rec["signer"], str) or not ADDRESS_RE.match(rec["signer"]):
        raise ValueError("skill.md.sig signer must be a 0x address")
    recovered = claims.recover_signer(skill_message(rec["sha256"]), rec["signature"])
    if recovered.lower() != rec["signer"].lower():
        raise ValueError(f"skill.md.sig was made by {recovered}, not by the named signer {rec['signer']}")
    current = skill_sha256(base_dir)
    if rec["sha256"] != current:
        return "stale", f"skill.md.sig signs {rec['sha256'][:12]}…, but skill.md is now {current[:12]}…: re-sign it"
    return "valid", f"skill.md is signed by {recovered}"


def check_skill_hash(base_dir: str, state: Dict[str, Any]) -> str:
    """state.json publishes sha256(skill.md) and the skill's version."""
    path = os.path.join(base_dir, core.SKILL_FILE)
    if not os.path.exists(path):
        raise ValueError("skill.md is missing")
    with open(path, "rb") as fh:
        raw = fh.read()
    actual = hashlib.sha256(raw).hexdigest()
    published = state.get("skill_sha256")
    if published != actual:
        raise ValueError(f"state.json skill_sha256 is {published!r}; sha256(skill.md) is {actual}. "
                         "Run `python scripts/generate_state.py`.")
    version = core.skill_version(raw)
    if not version or state.get("skill_version") != version:
        raise ValueError(f"state.json skill_version is {state.get('skill_version')!r}; skill.md says {version!r}")
    return f"skill.md {version} matches the published sha256 {actual[:12]}…"


def check_discovery(base_dir: str, state: Dict[str, Any]) -> str:
    """The committed discovery documents are valid JSON and equal their published hashes."""
    published = state.get("discovery")
    if not isinstance(published, dict):
        raise ValueError("state.json has no discovery block; run `python scripts/generate_state.py`")
    for key, rel in core.DISCOVERY_FILES.items():
        path = os.path.join(base_dir, rel)
        digest = core.sha256_file(path)
        if digest is None:
            raise ValueError(f"{rel} is missing")
        try:
            doc = core.load_json_strict(path)
        except ValueError as e:
            raise ValueError(f"{rel} is not valid JSON: {e}")
        if not isinstance(doc, dict):
            raise ValueError(f"{rel} must be a JSON object")
        if published.get(key) != digest:
            raise ValueError(f"state.json discovery.{key} is {published.get(key)!r}; sha256({rel}) is {digest}. "
                             "Run `python scripts/generate_state.py`.")
    if set(published) != set(core.DISCOVERY_FILES):
        raise ValueError(f"state.json discovery has unexpected keys: {sorted(set(published) - set(core.DISCOVERY_FILES))}")
    return "discovery documents match their published sha256"


# --- permissions.json -----------------------------------------------------------

_ALLOWED_RPC_METHODS = {"eth_blockNumber", "eth_chainId", "eth_getTransactionByHash", "eth_getTransactionReceipt",
                        "eth_getBlockByNumber", "eth_getLogs", "eth_call"}  # read-only only
_TOP_KEYS = {"schema", "description", "skill", "network", "files", "secrets", "spend", "pull_request"}
_CLAIM_PATTERNS = {claims.CLAIM_FILE_RE.pattern, claims.CLAIM_ICON_RE.pattern, claims.CLAIM_SOUL_RE.pattern}


def validate_permissions(manifest: Any, state: Dict[str, Any],
                         forbidden_paths: Optional[List[str]] = None) -> List[str]:
    """Problems found in the permission manifest (empty = well-formed and no
    capability the verifier would not allow). `forbidden_paths` are files an
    outside pull request may never touch (the generated state)."""
    errs: List[str] = []
    if not isinstance(manifest, dict):
        return ["permissions.json must be a JSON object"]
    if set(manifest) != _TOP_KEYS:
        errs.append(f"top-level keys must be exactly {sorted(_TOP_KEYS)}")
    if manifest.get("schema") != "thefirstwall.permissions/1":
        errs.append("schema must be 'thefirstwall.permissions/1'")

    # network: https only, only the declared hosts, read-only RPC
    net = manifest.get("network") or {}
    allowed_hosts = {"thefirstwall.ai", "x402.thefirstwall.ai"}
    x402_url = (state.get("links") or {}).get("x402_claim")
    seen_ids = set()
    for item in net.get("allow") or []:
        if not isinstance(item, dict):
            errs.append("network.allow entries must be objects")
            continue
        seen_ids.add(item.get("id"))
        url = item.get("url")
        if url is not None:
            m = re.match(r"^https://([a-z0-9.-]+)(/[^\s]*)?$", str(url))
            if not m or m.group(1) not in allowed_hosts:
                errs.append(f"network url {url!r} must be https on one of {sorted(allowed_hosts)}")
        if item.get("id") == "x402_claim" and url != x402_url:
            errs.append(f"x402_claim url must equal state.json links.x402_claim ({x402_url!r})")
        if item.get("id") == "base_rpc":
            if item.get("chain_id") != core.CHAIN_ID:
                errs.append(f"base_rpc chain_id must be {core.CHAIN_ID}")
            methods = item.get("rpc_methods")
            if not isinstance(methods, list) or not methods or not set(methods) <= _ALLOWED_RPC_METHODS:
                errs.append("base_rpc rpc_methods must be a non-empty subset of the read-only methods "
                            f"{sorted(_ALLOWED_RPC_METHODS)}")
        for m_ in item.get("methods") or []:
            if m_ not in ("GET", "HEAD"):
                errs.append(f"network {item.get('id')} method {m_!r} is not read-only (GET/HEAD)")
    if seen_ids != {"ledger_state", "base_rpc", "x402_claim"}:
        errs.append("network.allow must list exactly: ledger_state, base_rpc, x402_claim")
    if net.get("deny_all_other") is not True:
        errs.append("network.deny_all_other must be true")

    # files: writes are exactly the claim files; nothing the verifier forbids
    files = manifest.get("files") or {}
    patterns = [w.get("path_pattern") for w in files.get("write") or [] if isinstance(w, dict)]
    if set(patterns) != _CLAIM_PATTERNS or len(patterns) != len(_CLAIM_PATTERNS):
        errs.append("files.write must be exactly the claim dossier, icon and soul manifest "
                    "(the verifier's CLAIM_FILE_RE/CLAIM_ICON_RE/CLAIM_SOUL_RE)")
    for rel in (forbidden_paths or []) + ["ledger/w1/w1-b0001.json", "ledger/souls/w1-b0001.soul.json"]:
        if any(re.match(p, rel) for p in patterns if isinstance(p, str)):
            errs.append(f"files.write allows {rel}, which a claim may never touch")
    for rel in files.get("read") or []:
        if not isinstance(rel, str) or rel.startswith(("/", ".")) or ".." in rel or "secret" in rel.lower():
            errs.append(f"files.read entry {rel!r} must be a repo-relative public file")

    # secrets: none
    sec = manifest.get("secrets") or {}
    if sec.get("reads_secrets") is not False or sec.get("reads_env") != [] or sec.get("reads_paths") != []:
        errs.append("secrets must declare reads_secrets=false, reads_env=[] and reads_paths=[]")

    # spend: the genesis cap and the settlement rail from state.json
    spend = manifest.get("spend") or {}
    rail = state.get("settlement_rail") or {}
    genesis = next((t["price_usdc"] for t in state.get("pricing_tiers", []) if t.get("tier") == "genesis"), None)
    if spend.get("max_per_claim_usdc") != genesis:
        errs.append(f"spend.max_per_claim_usdc must equal the genesis price {genesis}")
    if spend.get("claims_per_payment") != 1:
        errs.append("spend.claims_per_payment must be 1")
    for k_m, k_s in (("asset", "asset"), ("chain_id", "chain_id"), ("contract", "contract"), ("pay_to", "pay_to")):
        if spend.get(k_m) != rail.get(k_s):
            errs.append(f"spend.{k_m} must equal state.json settlement_rail.{k_s} ({rail.get(k_s)!r})")

    pr = manifest.get("pull_request") or {}
    if pr.get("count") != 1:
        errs.append("pull_request.count must be 1")
    if pr.get("repo") != (state.get("links") or {}).get("ledger_repo"):
        errs.append("pull_request.repo must equal state.json links.ledger_repo")
    return errs


def check_permissions(base_dir: str, state: Dict[str, Any], forbidden_paths: Optional[List[str]] = None) -> str:
    path = os.path.join(base_dir, PERMISSIONS_FILE)
    if not os.path.exists(path):
        raise ValueError("permissions.json is missing")
    manifest = core.load_json_strict(path)
    errs = validate_permissions(manifest, state, forbidden_paths)
    if errs:
        raise ValueError("permissions.json: " + "; ".join(errs))
    return "permissions.json is well-formed and within what the verifier allows"
