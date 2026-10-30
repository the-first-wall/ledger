#!/usr/bin/env python3
"""
The First Wall — verification.json (E-10)
=========================================

A single, machine-readable statement of what was checked about the ledger and
when, so an agent need not take liveness on faith:

    {root, as_of_block, ledger_commit, anchor:{tx,block}, slots,
     checks:[{id,status,observed_at,evidence}],
     services:[{url,status,checked_at}]}

    python scripts/verification.py                 # write verification.json, OFFLINE (file-hash checks only)
    python scripts/verification.py --probe         # also GET the live endpoints (network; scheduled job only)
    python scripts/verification.py --check         # validate the committed verification.json, OFFLINE

Two kinds of checks, never mixed up:
  * file-hash assertions — pure functions of the committed bytes (skill.md,
    the discovery documents, permissions.json, the Merkle root). Offline.
  * live probes — a real HTTP GET with a timestamp (x402 manifest, state.json
    on the canonical site). Run ONLY with --probe, by its own scheduled workflow
    (.github/workflows/verification_probe.yml), never by unit tests or `--check`.
    Without --probe the services are listed with status "unprobed".

`--check` never touches the network and never fails because a probe is old. It
checks the document's shape and, when the file describes THIS snapshot (same
root and skill hash as state.json), that every file-hash assertion agrees with
the committed files; a file for an older root is reported as stale (exit 0).

Read-only; no keys. Exit: 0 ok, 1 invalid/contradicted.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger_core as core  # noqa: E402
import provenance  # noqa: E402

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
VERIFICATION_FILE = "verification.json"
SCHEMA = "thefirstwall.verification/1"
SERVICES = (
    ("x402_manifest", "https://x402.thefirstwall.ai/.well-known/x402"),
    ("ledger_state", "https://thefirstwall.ai/state.json"),
)
TOP_KEYS = {"schema", "root", "as_of_block", "ledger_commit", "anchor", "slots", "checks", "services"}
STATUSES = {"pass", "fail", "unprobed"}
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
USER_AGENT = "TheFirstWall-verification/1.0"

Fetch = Callable[[str], Dict[str, Any]]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def http_get(url: str, timeout: float = 20.0) -> Dict[str, Any]:
    """A real GET. Returns {http_status, sha256, bytes, body} or {error}."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return {"http_status": resp.status, "sha256": hashlib.sha256(body).hexdigest(),
                    "bytes": len(body), "body": body}
    except Exception as e:  # noqa: BLE001 — a failed probe is evidence, not a crash
        return {"error": f"{type(e).__name__}: {e}"}


def _load(base_dir: str, rel: str) -> Dict[str, Any]:
    return core.load_json_strict(os.path.join(base_dir, rel))


def _git_commit(base_dir: str) -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=base_dir, capture_output=True, text=True, timeout=10)
        sha = out.stdout.strip()
        return sha if out.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha) else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def file_checks(base_dir: str, state: Dict[str, Any], observed_at: str) -> List[Dict[str, Any]]:
    """Offline assertions about the committed bytes. Deterministic given the files."""
    def mk(cid: str, ok: bool, evidence: Dict[str, Any]) -> Dict[str, Any]:
        return {"id": cid, "status": "pass" if ok else "fail", "observed_at": observed_at, "evidence": evidence}

    out = []
    skill = core.sha256_file(os.path.join(base_dir, core.SKILL_FILE))
    out.append(mk("skill_sha256", skill is not None and skill == state.get("skill_sha256"),
                  {"file": core.SKILL_FILE, "sha256": skill, "published": state.get("skill_sha256")}))
    disc = state.get("discovery") or {}
    for key, rel in core.DISCOVERY_FILES.items():
        h = core.sha256_file(os.path.join(base_dir, rel))
        out.append(mk(f"discovery.{key}", h is not None and h == disc.get(key),
                      {"file": rel, "sha256": h, "published": disc.get(key)}))
    try:
        errs = provenance.validate_permissions(_load(base_dir, provenance.PERMISSIONS_FILE), state)
    except (OSError, ValueError) as e:
        errs = [str(e)]
    out.append(mk("permissions_manifest", not errs,
                  {"file": provenance.PERMISSIONS_FILE, "problems": errs,
                   "active_tier_price_usdc": provenance.active_tier_price(state)[0]}))
    try:
        idx_root = _load(base_dir, "ledger/index.json").get("merkle_root")
    except (OSError, ValueError):
        idx_root = None
    out.append(mk("merkle_root_matches_index", idx_root is not None and idx_root == state.get("merkle_root"),
                  {"state": state.get("merkle_root"), "index": idx_root}))
    return out


def probe_checks(state: Dict[str, Any], fetch: Fetch, clock: Callable[[], str]):
    """Live probes: real GETs. Returns (checks, services)."""
    checks, services = [], []
    for sid, url in SERVICES:
        at = clock()
        r = fetch(url)
        status_code = r.get("http_status")
        ok = status_code == 200 and "error" not in r
        evidence: Dict[str, Any] = {"url": url, "method": "GET", "http_status": status_code,
                                    "sha256": r.get("sha256"), "bytes": r.get("bytes")}
        if r.get("error"):
            evidence["error"] = r["error"]
        if ok:
            try:
                doc = json.loads(r["body"].decode("utf-8"))
            except ValueError:
                doc, ok = None, False
                evidence["error"] = "response is not JSON"
            if ok and sid == "x402_manifest":
                ok = isinstance(doc, dict) and doc.get("x402Version") is not None
                evidence["x402Version"] = doc.get("x402Version") if isinstance(doc, dict) else None
            if ok and sid == "ledger_state":
                live = doc.get("merkle_root") if isinstance(doc, dict) else None
                evidence["live_merkle_root"] = live
                evidence["matches_repo_root"] = live == state.get("merkle_root")
                ok = live is not None  # a lagging deploy is recorded, not hidden, but the endpoint is live
        checks.append({"id": f"http_probe.{sid}", "status": "pass" if ok else "fail", "observed_at": at,
                       "evidence": evidence})
        services.append({"url": url, "status": "up" if ok else "down", "checked_at": at})
    return checks, services


def build(base_dir: str = BASE_DIR, *, probe: bool = False, fetch: Fetch = http_get,
          clock: Callable[[], str] = _now, commit: Optional[str] = None) -> Dict[str, Any]:
    state = _load(base_dir, "state.json")
    anchors = _load(base_dir, "ledger/anchors.json")
    last = anchors[-1] if isinstance(anchors, list) and anchors else {}
    checks = file_checks(base_dir, state, clock())
    if probe:
        pchecks, services = probe_checks(state, fetch, clock)
        checks += pchecks
    else:
        services = [{"url": url, "status": "unprobed", "checked_at": None} for _, url in SERVICES]
    return {
        "schema": SCHEMA,
        "root": state.get("merkle_root"),
        "as_of_block": last.get("block_number"),
        "ledger_commit": commit if commit is not None else _git_commit(base_dir),
        "anchor": {"tx": last.get("tx_hash"), "block": last.get("block_number")},
        "slots": state.get("claimed_slots"),
        "checks": checks,
        "services": services,
    }


def _shape_errors(doc: Any) -> List[str]:
    if not isinstance(doc, dict):
        return ["verification.json must be an object"]
    errs = []
    if set(doc) != TOP_KEYS:
        errs.append(f"top-level keys must be exactly {sorted(TOP_KEYS)}")
    if doc.get("schema") != SCHEMA:
        errs.append(f"schema must be {SCHEMA!r}")
    if not isinstance(doc.get("root"), str) or not re.fullmatch(r"[0-9a-f]{64}", doc.get("root") or ""):
        errs.append("root must be a 64-hex Merkle root")
    if not isinstance(doc.get("slots"), int):
        errs.append("slots must be an integer")
    anchor = doc.get("anchor")
    if not isinstance(anchor, dict) or set(anchor) != {"tx", "block"}:
        errs.append("anchor must be {tx, block}")
    ids = set()
    checks = doc.get("checks")
    if not isinstance(checks, list) or not checks:
        errs.append("checks must be a non-empty list")
        checks = []
    for c in checks:
        if not isinstance(c, dict) or set(c) != {"id", "status", "observed_at", "evidence"}:
            errs.append("each check must be exactly {id,status,observed_at,evidence}")
            continue
        ids.add(c["id"])
        if c["status"] not in STATUSES:
            errs.append(f"check {c['id']} has status {c['status']!r}")
        if not (isinstance(c["observed_at"], str) and _TS.match(c["observed_at"])):
            errs.append(f"check {c['id']} observed_at must be an RFC 3339 UTC timestamp")
        if not isinstance(c["evidence"], dict):
            errs.append(f"check {c['id']} evidence must be an object")
    services = doc.get("services")
    if not isinstance(services, list) or not services:
        errs.append("services must be a non-empty list")
        services = []
    for s in services:
        if not isinstance(s, dict) or set(s) != {"url", "status", "checked_at"}:
            errs.append("each service must be exactly {url,status,checked_at}")
        elif s["status"] != "unprobed" and not (isinstance(s["checked_at"], str) and _TS.match(s["checked_at"])):
            errs.append(f"service {s['url']} was probed but has no checked_at timestamp")
    return errs


def check(base_dir: str = BASE_DIR) -> List[str]:
    """Offline validation of the committed verification.json. Returns problems."""
    path = os.path.join(base_dir, VERIFICATION_FILE)
    if not os.path.exists(path):
        return [f"{VERIFICATION_FILE} is missing"]
    try:
        doc = core.load_json_strict(path)
    except ValueError as e:
        return [f"{VERIFICATION_FILE} is not valid JSON: {e}"]
    errs = _shape_errors(doc)
    if errs:
        return errs
    state = _load(base_dir, "state.json")
    # A file written for an older root/skill is stale, not wrong: the scheduled job refreshes it.
    skill_check = next((c for c in doc["checks"] if c["id"] == "skill_sha256"), None)
    same_snapshot = (doc["root"] == state.get("merkle_root")
                     and (skill_check or {}).get("evidence", {}).get("sha256") == state.get("skill_sha256"))
    if not same_snapshot:
        return []
    errs = []
    if doc["slots"] != state.get("claimed_slots"):
        errs.append(f"slots is {doc['slots']}; state.json claimed_slots is {state.get('claimed_slots')}")
    truth = {c["id"]: c for c in file_checks(base_dir, state, "1970-01-01T00:00:00Z")}
    claimed = {c["id"]: c for c in doc["checks"]}
    for cid, real in truth.items():
        got = claimed.get(cid)
        if got is None:
            errs.append(f"check {cid} is missing")
        elif got["status"] != real["status"] or got["evidence"] != real["evidence"]:
            errs.append(f"check {cid} contradicts the committed files (status/evidence differ)")
    return errs


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--check" in argv:
        errs = check()
        for e in errs:
            print(f"[✗] {e}")
        if not errs:
            print("[✓] verification.json is well-formed and agrees with the committed files (offline).")
        return 1 if errs else 0
    doc = build(probe="--probe" in argv)
    with open(os.path.join(BASE_DIR, VERIFICATION_FILE), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    failed = [c["id"] for c in doc["checks"] if c["status"] == "fail"]
    print(f"[verification] wrote {VERIFICATION_FILE}: {len(doc['checks'])} checks, "
          f"{'probed' if '--probe' in argv else 'offline'}; failing: {failed or 'none'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
