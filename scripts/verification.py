#!/usr/bin/env python3
"""
The First Wall — verification.json (E-10)
=========================================

A single, machine-readable statement of what was checked about the ledger and
when, so an agent need not take liveness on faith:

    {schema, generated_at, root, as_of_block, ledger_commit, anchor:{tx,block}, slots,
     checks:[{id,status,observed_at,evidence,invalidates_on}],
     services:[{url,status,checked_at}]}

B-54/B-55: the checks are the verifier's FULL inventory, not a hand-picked few.
Besides the file-hash assertions and the live probes, every check
`scripts/verifier.py --skip-rpc` runs is published under its own id — the
provenance and signature checks included (skill.signature,
w1-bNNNN.claim_signature, w1-bNNNN.update_signature, update_precondition,
records.proofs, anchors.current_root, ...) — with status pass | fail | warn |
skipped (an on-chain check the offline run did not perform). Every check names
`invalidates_on`, the condition that retires it (claim_signature_matches_bundle,
anchor_includes_root, http_200(<url>), ...), and the document carries
`generated_at`, so a stale pass is visibly stale rather than looking current.

    python scripts/verification.py                 # write verification.json, OFFLINE (file-hash checks only)
    python scripts/verification.py --probe         # also GET the live endpoints (network; scheduled job only)
    python scripts/verification.py --check         # validate the committed verification.json, OFFLINE

Two kinds of checks, never mixed up:
  * file-hash assertions — pure functions of the committed bytes (skill.md,
    the discovery documents, permissions.json, the Merkle root). Offline.
  * live probes — a real HTTP GET with a timestamp (the x402 manifest, state.json
    on the canonical site, and the live `/claim` 402 body — the price a buyer is
    actually quoted, B-44). Run ONLY with --probe, by its own scheduled workflow
    (.github/workflows/verification_probe.yml), never by unit tests or `--check`.
    Without --probe the services are listed with status "unprobed".

`--check` never touches the network and never fails because a probe is old. It
checks the document's shape and, when the file describes THIS snapshot (same
root, skill hash and latest anchor as the committed files), that every file-hash
assertion agrees with the committed files and that no published "pass" is
contradicted by the verifier run now (a stale pass is an error; a published
warn/fail that has since healed is merely stale); a file for an older snapshot
is reported as stale (exit 0).

Read-only; no keys. Exit: 0 ok, 1 invalid/contradicted.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger_core as core  # noqa: E402
import provenance  # noqa: E402

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
VERIFICATION_FILE = "verification.json"
SCHEMA = "thefirstwall.verification/1"
SITE = "https://thefirstwall.ai"

# Where a check's evidence lives (B-102). A `repo` check inspects the committed ledger and can
# be green while the canonical origin 404s the very artefact it names; a `published` check
# fetches the origin and cannot go green unless the URL answers. The scope is recorded in the
# check's evidence so `records.proofs = pass` is never mistaken for "the site serves the proof".
SCOPE_REPO = "repo"
SCOPE_PUBLISHED = "published"
PROOF_REL_RE = re.compile(r"records/w1-b(?:\d{4}|10000)\.proof\.json$")
ABSENCE_REL_RE = re.compile(r"absent/0x[0-9a-f]{40}\.json$")
SERVICES = (
    ("x402_manifest", "https://x402.thefirstwall.ai/.well-known/x402"),
    ("ledger_state", "https://thefirstwall.ai/state.json"),
    ("x402_claim", "https://x402.thefirstwall.ai/claim"),
)
# The HTTP status each probe must see. The claim endpoint answers 402 by design —
# that body *is* the price a buyer is quoted, so a 200 there is itself a defect (B-44).
EXPECTED_STATUS = {"x402_manifest": 200, "ledger_state": 200, "x402_claim": 402}
CLAIM_URL = "https://x402.thefirstwall.ai/claim"
# The extensions the /claim 402 challenge must carry (the Bazaar listing block).
CLAIM_EXTENSION_KEYS = ("bazaar",)
USDC_DECIMALS = 6
TOP_KEYS = {"schema", "generated_at", "root", "as_of_block", "ledger_commit", "anchor", "slots", "checks",
            "services"}
CHECK_KEYS = {"id", "status", "observed_at", "evidence", "invalidates_on"}
STATUSES = {"pass", "fail", "warn", "skipped", "unprobed"}
VERIFIER_SOURCE = "scripts/verifier.py --skip-rpc"
# Verifier checks that describe the pending queue, a PR diff or optional
# artefacts rather than the ledger at this root: they come and go without the
# root moving, so --check does not require them to be published.
TRANSIENT_PREFIXES = ("claim:", "update:", "pr.", "apply.", "assignment.", "rpc.", "absent.")
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
    except urllib.error.HTTPError as e:
        # A real answer with a non-2xx status. The body is the evidence: the
        # /claim endpoint answers 402 by design, and that 402 body is the price
        # the buyer is actually quoted (B-44). Read it instead of raising.
        try:
            body = e.read()
        except Exception:  # noqa: BLE001
            body = b""
        return {"http_status": e.code, "sha256": hashlib.sha256(body).hexdigest(),
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


_SLOT_CHECK = re.compile(r"^(w1-b(?:\d{4}|10000))\.(.+)$")
_SLOT_RULES = {
    "claim_signature": "claim_signature_matches_bundle({slot})",
    "update_signature": "update_signature_matches_bundle({slot})",
    "update_precondition": "prev_bundle_matches_base({slot})",
    "liveness": "ledger_clock_passes_absent_after({slot})",
    "soul_manifest": "sha256_changes(ledger/souls/{slot}.soul.json)",
    "icon_structure": "sha256_changes(ledger/w1/{slot}.webp)",
}


def invalidates_on(cid: str, status: str = "pass", evidence: Optional[Dict[str, Any]] = None) -> str:
    """The condition that retires a check: once it holds, the recorded result
    says nothing about the ledger any more and must be re-derived."""
    if status == "skipped":
        return "rpc_unverified(run scripts/verifier.py without --skip-rpc)"
    if cid.startswith("http_probe."):
        ev = evidence or {}
        return f"http_{ev.get('expected_status', 200)}({ev.get('url', '?')})"
    if cid in ("skill_sha256", "skill.hash"):
        return "sha256_changes(skill.md)"
    if cid == "skill.signature":
        return "skill_sig_matches_sha256(skill.md.sig, skill.md)"
    if cid.startswith("discovery."):
        rel = core.DISCOVERY_FILES.get(cid.split(".", 1)[1])
        return f"sha256_changes({rel})" if rel else "sha256_changes(discovery/*)"
    if cid in ("permissions_manifest", "permissions.manifest"):
        return "file_changes(permissions.json)"
    if cid.startswith("anchors"):
        return "anchor_includes_root"
    m = _SLOT_CHECK.match(cid)
    if m:
        slot, what = m.group(1), m.group(2)
        rule = _SLOT_RULES.get(what, "slot_bytes_change(ledger/w1/{slot}.json)")
        return rule.format(slot=slot)
    if cid.startswith(("claim:", "update:")):
        return "pending_queue_changes(ledger/claims, ledger/updates)"
    if cid.startswith(("state.", "records.", "ledger.", "merkle_root", "absent.")):
        return "root_changes"
    return "ledger_commit_changes"


def _slot_ids(base_dir: str) -> List[str]:
    slot_dir = core.ledger_w1_dir(base_dir)
    if not os.path.isdir(slot_dir):
        return []
    return [f[:-5] for f in core.list_slot_files(slot_dir) if f.endswith(".json")]


def published_artifacts(base_dir: str) -> List[tuple]:
    """[(id, url)] for every artefact the ledger commits for the site to serve: one per claimed
    slot's inclusion proof (`records/<slot>.proof.json`) and one per absence certificate
    (`absent/<wallet>.json`). Read from the ledger, so the probe set follows it."""
    out: List[tuple] = []
    for slot_id in sorted(_slot_ids(base_dir)):
        rel = "records/%s.proof.json" % slot_id
        if PROOF_REL_RE.fullmatch(rel):
            out.append(("proof.%s" % slot_id, "%s/%s" % (SITE, rel)))
    absent_dir = os.path.join(base_dir, "absent")
    if os.path.isdir(absent_dir):
        for name in sorted(os.listdir(absent_dir)):
            rel = "absent/%s" % name
            if ABSENCE_REL_RE.fullmatch(rel):
                out.append(("absence.%s" % name[:-5], "%s/%s" % (SITE, rel)))
    return out


def verifier_checks(base_dir: str, observed_at: str) -> List[Dict[str, Any]]:
    """Every check the offline verifier runs, published under its own id (B-54).
    Run in-process with --skip-rpc semantics and the scope of an infrastructure
    run (no PR diff); its console output is suppressed."""
    import contextlib
    import io
    import verifier  # noqa: E402  (same scripts dir; heavy: jsonschema, Pillow)
    with contextlib.redirect_stdout(io.StringIO()):
        report = verifier.run_verification(base_dir, skip_rpc=True,
                                           scope_override=verifier.make_scope("infra"))
    out: List[Dict[str, Any]] = []
    seen: Dict[str, int] = {}
    for r in report["checks"]:
        cid = r["check"]
        seen[cid] = seen.get(cid, 0) + 1
        if seen[cid] > 1:
            cid = f"{cid}#{seen[cid]}"
        detail = str(r.get("detail") or "")
        status = {"PASS": "pass", "FAIL": "fail"}.get(r["status"], "warn")
        if status == "warn" and "skipped (--skip-rpc)" in detail:
            status = "skipped"
        out.append({"id": cid, "status": status, "observed_at": observed_at,
                    "evidence": {"detail": detail, "source": VERIFIER_SOURCE, "scope": SCOPE_REPO},
                    "invalidates_on": invalidates_on(cid, status)})
    return out


def file_checks(base_dir: str, state: Dict[str, Any], observed_at: str) -> List[Dict[str, Any]]:
    """Offline assertions about the committed bytes. Deterministic given the files."""
    def mk(cid: str, ok: bool, evidence: Dict[str, Any]) -> Dict[str, Any]:
        return {"id": cid, "status": "pass" if ok else "fail", "observed_at": observed_at, "evidence": evidence,
                "invalidates_on": invalidates_on(cid)}

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


def _expected_units(state: Dict[str, Any]) -> Optional[int]:
    """The ledger floor in USDC base units (6 decimals), or None when state.json
    carries no usable floor. The one number a buyer pays; never a guess."""
    floor = state.get("current_floor_usdc")
    if isinstance(floor, bool) or not isinstance(floor, (int, float)) or floor <= 0:
        return None
    return int(round(float(floor) * 10 ** USDC_DECIMALS))


def _accepts_problems(accepts: Any, state: Dict[str, Any], label: str,
                      required: bool = True) -> List[str]:
    """An `accepts[0]` block must quote exactly the ledger floor and the ledger's
    settlement rail. This is the money check B-44 was missing: a 999.999999 USDC
    quote used to pass because only identity fields were compared. `required` is
    False for the static 200 manifest, which by design carries no price."""
    rail = state.get("settlement_rail") or {}
    if not isinstance(accepts, list) or not accepts or not isinstance(accepts[0], dict):
        return [f"{label}: accepts[0] is missing (no price a buyer can pay)"] if required else []
    a0 = accepts[0]
    problems: List[str] = []
    units = _expected_units(state)
    raw = a0.get("amount")
    try:
        got = int(str(raw))
    except (TypeError, ValueError):
        got = None
    if units is None:
        problems.append(f"{label}: state.json has no usable current_floor_usdc")
    elif got != units:
        problems.append(f"{label}: accepts[0].amount {raw!r} != the ledger floor "
                        f"{units} units ({state.get('current_floor_usdc')} USDC)")
    chain_id = rail.get("chain_id")
    if chain_id is not None and a0.get("network") != f"eip155:{chain_id}":
        problems.append(f"{label}: accepts[0].network {a0.get('network')!r} != eip155:{chain_id}")
    if rail.get("pay_to") is not None and a0.get("payTo") != rail.get("pay_to"):
        problems.append(f"{label}: accepts[0].payTo {a0.get('payTo')!r} != {rail.get('pay_to')!r}")
    if rail.get("contract") is not None and a0.get("asset") != rail.get("contract"):
        problems.append(f"{label}: accepts[0].asset {a0.get('asset')!r} != {rail.get('contract')!r}")
    return problems


def _price_problems(price: Any, state: Dict[str, Any], label: str,
                    required: bool = False) -> List[str]:
    """A served `price` block must name the ledger floor and the active tier. The
    static 200 manifest carries none by design, so absence is only a failure where
    a price is required (the 402 challenge)."""
    if not isinstance(price, dict):
        return [f"{label}: price is missing (the served manifest must publish the floor)"] if required else []
    problems: List[str] = []
    floor = state.get("current_floor_usdc")
    if price.get("amount_usdc") != floor:
        problems.append(f"{label}: price.amount_usdc {price.get('amount_usdc')!r} != the ledger floor {floor!r}")
    tier = state.get("pricing_tier")
    if tier is not None and price.get("pricing_tier") != tier:
        problems.append(f"{label}: price.pricing_tier {price.get('pricing_tier')!r} != {tier!r}")
    return problems


def _x402_manifest_semantics(doc: Any, state: Dict[str, Any], authored: Dict[str, Any]) -> List[str]:
    """The served x402 manifest is built at runtime, so its bytes never pin (B-40).
    Assert the fields that matter against the ledger's settlement rail and the
    authored discovery file; a drifted payee, asset or url is the failure to catch.
    The money fields (`price`, `accepts[0].amount`) are checked against the ledger
    floor too — identity without the price is a green tick that cannot see (B-44)."""
    if not isinstance(doc, dict):
        return ["served manifest is not a JSON object"]
    rail = state.get("settlement_rail") or {}

    def pick(d: Any, *path: str):
        cur = d
        for k in path:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(k)
        return cur

    a_res = authored.get("resources") if isinstance(authored, dict) else None
    a0 = a_res[0] if isinstance(a_res, list) and a_res and isinstance(a_res[0], dict) else {}
    s_res = doc.get("resources")
    s0 = s_res[0] if isinstance(s_res, list) and s_res and isinstance(s_res[0], dict) else {}

    expected = {
        "x402Version": authored.get("x402Version") if isinstance(authored, dict) else None,
        "resources[0].url": a0.get("url"),
        "resources[0].network": a0.get("network"),
        "resources[0].payTo": rail.get("pay_to"),
        "resources[0].asset.address": rail.get("contract"),
    }
    served = {
        "x402Version": doc.get("x402Version"),
        "resources[0].url": s0.get("url"),
        "resources[0].network": s0.get("network"),
        "resources[0].payTo": s0.get("payTo"),
        "resources[0].asset.address": pick(s0, "asset", "address"),
    }
    problems = [f"{k}: served {served[k]!r} != expected {exp!r}"
                for k, exp in expected.items() if exp is not None and served[k] != exp]
    a_ext = authored.get("extensions") if isinstance(authored, dict) else None
    s_ext = doc.get("extensions")
    a_keys = sorted(a_ext) if isinstance(a_ext, dict) else []
    s_keys = sorted(s_ext) if isinstance(s_ext, dict) else []
    if a_keys != s_keys:
        problems.append(f"extensions keys: served {s_keys} != expected {a_keys}")
    # The money: if the served manifest carries `price`/`accepts`, they must quote
    # the ledger floor and the rail. The static 200 manifest carries neither by
    # design (the price lives in the /claim 402 body), so absence is not a failure
    # here — a present-and-wrong price is (B-44).
    problems += _price_problems(s0.get("price"), state, "manifest", required=False)
    problems += _accepts_problems(s0.get("accepts"), state, "manifest", required=False)
    return problems


def _x402_claim_semantics(doc: Any, state: Dict[str, Any]) -> List[str]:
    """The live `/claim` 402 body is the price a buyer is actually quoted. Probe it
    (B-44): a seller can quietly inflate the price in the 402 challenge while the
    static manifest stays honest, and the old check never looked."""
    if not isinstance(doc, dict):
        return ["402 body is not a JSON object"]
    problems = _accepts_problems(doc.get("accepts"), state, "402")
    res = doc.get("resource")
    url = res.get("url") if isinstance(res, dict) else None
    if url != CLAIM_URL:
        problems.append(f"402: resource.url {url!r} != {CLAIM_URL!r}")
    ext = doc.get("extensions")
    keys = sorted(ext) if isinstance(ext, dict) else []
    if keys != sorted(CLAIM_EXTENSION_KEYS):
        problems.append(f"402: extensions keys {keys} != {sorted(CLAIM_EXTENSION_KEYS)}")
    return problems


def probe_checks(state: Dict[str, Any], fetch: Fetch, clock: Callable[[], str],
                 authored: Optional[Dict[str, Any]] = None, published: Optional[List[tuple]] = None):
    """Live probes: real GETs. Returns (checks, services).

    `authored` is the committed `discovery/x402.json`: the served x402 manifest is
    built at runtime, so it is checked by *meaning*, never by a byte-pin (B-40).
    The `/claim` probe is read-only and bounded (one GET, 20s timeout): it must
    see 402, and the 402 body must quote the ledger floor (B-44)."""
    checks, services = [], []
    for sid, url in SERVICES:
        at = clock()
        r = fetch(url)
        status_code = r.get("http_status")
        want = EXPECTED_STATUS.get(sid, 200)
        ok = status_code == want and "error" not in r
        evidence: Dict[str, Any] = {"url": url, "method": "GET", "http_status": status_code,
                                    "expected_status": want, "bytes": r.get("bytes"),
                                    "scope": SCOPE_PUBLISHED}
        if r.get("sha256"):
            # The bytes actually served. For the runtime-built manifest this is NOT a
            # pin — the semantics check below is what can fail for a real reason.
            evidence["served_body_sha256"] = r["sha256"]
        if r.get("error"):
            evidence["error"] = r["error"]
        if ok:
            try:
                doc = json.loads(r["body"].decode("utf-8"))
            except ValueError:
                doc, ok = None, False
                evidence["error"] = "response is not JSON"
            if ok and sid == "x402_manifest":
                problems = _x402_manifest_semantics(doc, state, authored or {})
                evidence["x402Version"] = doc.get("x402Version") if isinstance(doc, dict) else None
                evidence["semantics"] = "pass" if not problems else "fail"
                if problems:
                    evidence["problems"] = problems
                ok = not problems
            if ok and sid == "x402_claim":
                # B-44: read the live 402 body and check the money it quotes.
                problems = _x402_claim_semantics(doc, state)
                evidence["x402Version"] = doc.get("x402Version") if isinstance(doc, dict) else None
                accepts = doc.get("accepts") if isinstance(doc, dict) else None
                a0 = accepts[0] if isinstance(accepts, list) and accepts and isinstance(accepts[0], dict) else {}
                evidence["quoted_amount"] = a0.get("amount")
                evidence["expected_amount"] = None if _expected_units(state) is None else str(_expected_units(state))
                evidence["semantics"] = "pass" if not problems else "fail"
                if problems:
                    evidence["problems"] = problems
                ok = not problems
            if ok and sid == "ledger_state":
                live = doc.get("merkle_root") if isinstance(doc, dict) else None
                evidence["live_merkle_root"] = live
                evidence["matches_repo_root"] = live == state.get("merkle_root")
                ok = live is not None  # a lagging deploy is recorded, not hidden, but the endpoint is live
        checks.append({"id": f"http_probe.{sid}", "status": "pass" if ok else "fail", "observed_at": at,
                       "evidence": evidence, "invalidates_on": invalidates_on(f"http_probe.{sid}", "pass", evidence)})
        services.append({"url": url, "status": "up" if ok else "down", "checked_at": at})
    # B-102: probe every artefact the ledger commits for the site to serve, so a published
    # "pass" (records.proofs) can only stand while the served URL really answers.
    for pid, url in (published or []):
        at = clock()
        r = fetch(url)
        status_code = r.get("http_status")
        ok = status_code == 200 and "error" not in r
        evidence = {"url": url, "method": "GET", "http_status": status_code, "expected_status": 200,
                    "bytes": r.get("bytes"), "scope": SCOPE_PUBLISHED}
        if r.get("error"):
            evidence["error"] = r["error"]
        checks.append({"id": f"http_probe.published.{pid}", "status": "pass" if ok else "fail",
                       "observed_at": at, "evidence": evidence,
                       "invalidates_on": invalidates_on(f"http_probe.published.{pid}", "pass", evidence)})
        services.append({"url": url, "status": "up" if ok else "down", "checked_at": at})
    return checks, services


def build(base_dir: str = BASE_DIR, *, probe: bool = False, fetch: Fetch = http_get,
          clock: Callable[[], str] = _now, commit: Optional[str] = None) -> Dict[str, Any]:
    state = _load(base_dir, "state.json")
    anchors = _load(base_dir, "ledger/anchors.json")
    last = anchors[-1] if isinstance(anchors, list) and anchors else {}
    generated_at = clock()
    checks = file_checks(base_dir, state, generated_at)
    checks += verifier_checks(base_dir, generated_at)
    published = published_artifacts(base_dir)
    if probe:
        authored = _load(base_dir, core.DISCOVERY_FILES["x402_manifest_sha256"])
        pchecks, services = probe_checks(state, fetch, clock, authored, published)
        checks += pchecks
    else:
        services = [{"url": url, "status": "unprobed", "checked_at": None}
                    for _sid, url in SERVICES] + [{"url": url, "status": "unprobed", "checked_at": None}
                                                  for _pid, url in published]
    return {
        "schema": SCHEMA,
        "generated_at": generated_at,
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
    if not (isinstance(doc.get("generated_at"), str) and _TS.match(doc.get("generated_at") or "")):
        errs.append("generated_at must be an RFC 3339 UTC timestamp")
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
        if not isinstance(c, dict) or set(c) != CHECK_KEYS:
            errs.append("each check must be exactly {id,status,observed_at,evidence,invalidates_on}")
            continue
        if c["id"] in ids:
            errs.append(f"check id {c['id']} appears twice")
        ids.add(c["id"])
        if not (isinstance(c["invalidates_on"], str) and c["invalidates_on"].strip()):
            errs.append(f"check {c['id']} must name invalidates_on (the condition that retires it)")
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
    # A file written for an older root/skill/anchor is stale, not wrong: the scheduled job refreshes it.
    skill_check = next((c for c in doc["checks"] if c["id"] == "skill_sha256"), None)
    try:
        anchors = _load(base_dir, "ledger/anchors.json")
    except (OSError, ValueError):
        anchors = []
    last = anchors[-1] if isinstance(anchors, list) and anchors and isinstance(anchors[-1], dict) else {}
    same_snapshot = (doc["root"] == state.get("merkle_root")
                     and (skill_check or {}).get("evidence", {}).get("sha256") == state.get("skill_sha256")
                     and doc["anchor"].get("tx") == last.get("tx_hash"))
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
    errs += _verifier_contradictions(base_dir, claimed)
    return errs


def _verifier_contradictions(base_dir: str, claimed: Dict[str, Dict[str, Any]]) -> List[str]:
    """The published verifier inventory against a verifier run now (B-54/B-55).
    An error is a STALE PASS (published pass, not a pass now, or a published
    check that no longer exists) or a missing check of the ledger itself; a
    published warn/fail that has since healed is only stale. Detail strings may
    legitimately differ between machines and are not compared."""
    errs: List[str] = []
    truth = {c["id"]: c for c in verifier_checks(base_dir, "1970-01-01T00:00:00Z")}
    published = {cid: c for cid, c in claimed.items()
                 if isinstance(c.get("evidence"), dict) and c["evidence"].get("source") == VERIFIER_SOURCE}
    if not published:
        return ["the verifier's checks are not published (B-54): rebuild with scripts/verification.py"]
    for cid, real in truth.items():
        if cid not in published and not cid.startswith(TRANSIENT_PREFIXES):
            errs.append(f"check {cid} is run by the verifier but not published")
    for cid, got in published.items():
        real = truth.get(cid)
        if real is None:
            if got["status"] == "pass" and not cid.startswith(TRANSIENT_PREFIXES):
                errs.append(f"check {cid} is published as pass but the verifier no longer runs it (stale pass)")
        elif got["status"] == "pass" and real["status"] != "pass":
            errs.append(f"check {cid} is published as pass but the verifier now says {real['status']} "
                        f"(stale pass): {real['evidence']['detail'][:160]}")
        if real is not None and got.get("invalidates_on") != real["invalidates_on"]:
            errs.append(f"check {cid} names invalidates_on {got.get('invalidates_on')!r}, "
                        f"expected {real['invalidates_on']!r}")
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
