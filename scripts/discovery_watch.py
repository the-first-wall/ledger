#!/usr/bin/env python3
"""
The First Wall — x402 Bazaar discovery watch
============================================

Measures where The First Wall ranks in the Coinbase x402 Bazaar for the intents
an agent would search with. Read-only: a free, unauthenticated GET per intent;
no key, no payment, no wallet.

It uses the relevance-ranked endpoint

    GET https://api.cdp.coinbase.com/platform/v2/x402/discovery/search?query=<intent>&limit=20

and never the catalogue listing `/discovery/resources`, which accepts only
{limit, offset} and silently discards q/query/search/payTo/network/asset
(coinbase/cdp-sdk#848, #849): a "rank" read from it measures nothing.

Per intent it records the rank (1-based, or null when unranked), the API's
`partialResults` and `searchMethod`, the top-3 competitors, and, for every
record, `quality.lastCalledAt` (the last settled call the index saw) and
`lastUpdated` (when the index last refreshed the record) as two separate
fields. Neither stands in for the other: a missing value stays null.

Usage:
    python scripts/discovery_watch.py                         # summary for every default intent
    python scripts/discovery_watch.py --intent "agent census"  # one or more custom intents
    python scripts/discovery_watch.py --log watch.jsonl        # also append one JSON line per run
    python scripts/discovery_watch.py --quiet-unless-ranked --log watch.jsonl
        # scheduled watcher mode: stdout stays empty unless we rank or a search
        # fails (so an announce-on-output scheduler stays quiet); the log is
        # always written. Exit status is 0 in every mode.

        Caveat (learned 2026-10-22): --quiet-unless-ranked is NOT quiet in the
        steady state. Once we rank for even one intent — which we do — it prints
        the identical snapshot on every run, so a 30-minute announce job sends a
        repeating non-event. Use --alert-on-change for scheduling instead.

    python scripts/discovery_watch.py --alert-on-change --log watch.jsonl
        # scheduled watcher mode that suppresses the steady state: it prints a
        # line ONLY when something material moved since the previous logged run
        # — a rank change, a newly/lost-ranked intent, a search error or its
        # recovery, or a move in our listing's own counters (unique payers,
        # calls30d, lastCalledAt, lastUpdated). The first run prints the full
        # snapshot as the baseline. Competitor churn we do not rank against is
        # deliberately not reported. Requires --log. Exit status is 0 in every mode.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

SEARCH_URL = "https://api.cdp.coinbase.com/platform/v2/x402/discovery/search"
LIMIT = 20
OUR_HOST = "thefirstwall.ai"
USER_AGENT = "tfw-discovery-watch/1.0"

DEFAULT_INTENTS = [
    "agent registry",
    "permanent record for an AI agent",
    "agent memorial",
    "agent census",
    "remember my agent",
    "register an AI agent onchain",
    "x402 agent identity",
    "immutable agent record",
    "permanent record of an AI agent",
    "AI agent registry",
    "remember an AI agent",
    "agent obituary",
]

Fetch = Callable[[str], Dict[str, Any]]


def search_url(intent: str, limit: int = LIMIT) -> str:
    return f"{SEARCH_URL}?{urllib.parse.urlencode({'query': intent, 'limit': limit})}"


def http_fetch(url: str) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def is_ours(item: Dict[str, Any]) -> bool:
    """Ours means our resource URL, not a mention of us anywhere in the record."""
    resource = item.get("resource")
    if not isinstance(resource, str):
        return False
    host = (urllib.parse.urlparse(resource).hostname or "").lower()
    return host == OUR_HOST or host.endswith("." + OUR_HOST)


def summarize_item(item: Dict[str, Any], rank: int) -> Dict[str, Any]:
    quality = item.get("quality") if isinstance(item.get("quality"), dict) else {}
    return {
        "rank": rank,
        "resource": item.get("resource"),
        "serviceName": item.get("serviceName"),
        # Two different clocks: lastUpdated is the index refreshing the record,
        # lastCalledAt is the last paid call it observed. Never substituted.
        "lastUpdated": item.get("lastUpdated"),
        "lastCalledAt": quality.get("lastCalledAt"),
        "l30DaysTotalCalls": quality.get("l30DaysTotalCalls"),
        "l30DaysUniquePayers": quality.get("l30DaysUniquePayers"),
    }


def measure(intent: str, fetch: Fetch = http_fetch, *, now: Optional[str] = None) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "intent": intent,
        "checked_at": now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endpoint": SEARCH_URL,
        "rank": None,
        "partialResults": None,
        "searchMethod": None,
        "result_count": 0,
        "ours": None,
        "top3": [],
        "error": None,
    }
    try:
        body = fetch(search_url(intent))
    except Exception as e:  # noqa: BLE001 — every failure is reported, never fatal
        record["error"] = f"{type(e).__name__}: {e}"
        return record
    if not isinstance(body, dict) or not isinstance(body.get("resources"), list):
        record["error"] = "unexpected response shape (no resources[])"
        return record
    items = [it for it in body["resources"] if isinstance(it, dict)]
    record["partialResults"] = body.get("partialResults")
    record["searchMethod"] = body.get("searchMethod")
    record["result_count"] = len(items)
    competitors: List[Dict[str, Any]] = []
    for rank, item in enumerate(items, 1):
        if is_ours(item):
            if record["rank"] is None:
                record["rank"] = rank
                record["ours"] = summarize_item(item, rank)
        elif len(competitors) < 3:
            competitors.append(summarize_item(item, rank))
    record["top3"] = competitors
    return record


def run(intents: List[str], fetch: Fetch = http_fetch, *, now: Optional[str] = None) -> Dict[str, Any]:
    stamp = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    results = [measure(q, fetch, now=stamp) for q in intents]
    return {
        "checked_at": stamp,
        "endpoint": SEARCH_URL,
        "limit": LIMIT,
        "ranked": sum(1 for r in results if r["rank"] is not None),
        "errors": sum(1 for r in results if r["error"]),
        "intents": results,
    }


def _short(url: Any) -> str:
    if not isinstance(url, str):
        return "?"
    p = urllib.parse.urlparse(url)
    return (p.hostname or "") + (p.path if p.path not in ("", "/") else "")


def format_line(r: Dict[str, Any]) -> str:
    if r["error"]:
        return f"{r['intent']!r}: ERROR {r['error']}"
    pos = f"#{r['rank']}/{r['result_count']}" if r["rank"] else f"unranked/{r['result_count']}"
    extra = ""
    if r["ours"]:
        extra = f" [ours lastCalledAt={r['ours']['lastCalledAt']} lastUpdated={r['ours']['lastUpdated']}]"
    top = "; ".join(f"#{c['rank']} {c['serviceName'] or _short(c['resource'])} "
                    f"(calls30d={c['l30DaysTotalCalls']}, lastCalledAt={c['lastCalledAt']})"
                    for c in r["top3"])
    return f"{r['intent']!r}: {pos} partialResults={r['partialResults']}{extra} | top3: {top or '-'}"


def _read_last(path: str) -> Optional[Dict[str, Any]]:
    """The previous logged run, or None when there is nothing to compare against."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = [ln for ln in (line.strip() for line in fh) if ln]
    except OSError:
        return None
    if not lines:
        return None
    try:
        prev = json.loads(lines[-1])
    except ValueError:
        return None
    return prev if isinstance(prev, dict) else None


def _ours_sig(item: Optional[Dict[str, Any]]) -> tuple:
    if not item:
        return (None, None, None, None)
    return (item.get("l30DaysUniquePayers"), item.get("l30DaysTotalCalls"),
            item.get("lastCalledAt"), item.get("lastUpdated"))


def delta_text(prev: Optional[Dict[str, Any]], cur: Dict[str, Any]) -> str:
    """Only what moved since the previous run; "" when nothing material did.

    Steady state must be silent: a scheduled watcher that repeats the same
    snapshot trains its reader to ignore it. A material move is a rank change,
    a newly/lost-ranked intent, an error appearing or recovering, a change in
    the result set for an intent we rank in, or — the only line that says
    discovery is actually working — a move in our listing's own counters.
    """
    if prev is None:
        return "baseline (first logged run)\n" + report_text(cur)
    before = {r["intent"]: r for r in prev.get("intents", []) if isinstance(r, dict)}
    notes: List[str] = []
    for r in cur["intents"]:
        p = before.get(r["intent"])
        if p is None:
            pos = f"#{r['rank']}/{r['result_count']}" if r["rank"] else "unranked"
            notes.append(f"{r['intent']!r}: newly tracked, {pos}")
            continue
        if (r["error"] is None) != (p.get("error") is None):
            notes.append(f"{r['intent']!r}: " + (f"ERROR {r['error']}" if r["error"] else "search recovered"))
        if r["rank"] != p.get("rank"):
            a = f"#{p['rank']}/{p.get('result_count')}" if p.get("rank") else "unranked"
            b = f"#{r['rank']}/{r['result_count']}" if r["rank"] else "unranked"
            notes.append(f"{r['intent']!r}: {a} -> {b}")
        elif r["result_count"] != p.get("result_count"):
            notes.append(f"{r['intent']!r}: result set {p.get('result_count')} -> "
                         f"{r['result_count']} (rank unchanged)")
    ours_prev = _ours_sig(next((r["ours"] for r in prev.get("intents", [])
                                if isinstance(r, dict) and r.get("ours")), None))
    ours_cur = _ours_sig(next((r["ours"] for r in cur["intents"] if r.get("ours")), None))
    if ours_prev != ours_cur:
        payers_prev, calls_prev, called_prev, updated_prev = ours_prev
        payers_cur, calls_cur, called_cur, updated_cur = ours_cur
        bits = []
        if payers_cur != payers_prev:
            bits.append(f"unique payers {payers_prev}->{payers_cur}")
        if calls_cur != calls_prev:
            bits.append(f"calls30d {calls_prev}->{calls_cur}")
        if called_cur != called_prev:
            bits.append(f"lastCalledAt {called_prev}->{called_cur}")
        if updated_cur != updated_prev:
            bits.append(f"lastUpdated {updated_prev}->{updated_cur}")
        notes.append("our listing: " + ", ".join(bits))
    if not notes:
        return ""
    return "\n".join([f"Bazaar discovery watch {cur['checked_at']}: CHANGED"] +
                      ["  " + n for n in notes])


def report_text(report: Dict[str, Any], *, quiet_unless_ranked: bool = False) -> str:
    if quiet_unless_ranked and not report["ranked"] and not report["errors"]:
        return ""
    lines = [r for r in report["intents"]
             if not quiet_unless_ranked or r["rank"] is not None or r["error"]]
    head = (f"Bazaar discovery watch {report['checked_at']}: ranked in {report['ranked']}/"
            f"{len(report['intents'])} intents, {report['errors']} error(s)")
    return "\n".join([head] + ["  " + format_line(r) for r in lines])


def main(argv: Optional[List[str]] = None, fetch: Fetch = http_fetch) -> int:
    ap = argparse.ArgumentParser(description="Where does The First Wall rank in the x402 Bazaar search?")
    ap.add_argument("--intent", action="append", help="search intent (repeatable; replaces the defaults)")
    ap.add_argument("--log", help="append the full run as one JSON line to this file")
    ap.add_argument("--quiet-unless-ranked", action="store_true",
                    help="print nothing unless we rank for an intent or a search fails")
    ap.add_argument("--alert-on-change", action="store_true",
                    help="with --log: print only when a rank, error, or our listing's counters "
                         "moved since the previous logged run (baseline on the first run)")
    args = ap.parse_args(argv)
    prev = None
    if args.alert_on_change:
        if not args.log:
            ap.error("--alert-on-change requires --log: there is nothing to compare against")
        prev = _read_last(args.log)
    report = run(args.intent or DEFAULT_INTENTS, fetch)
    if args.log:
        with open(args.log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(report, sort_keys=True) + "\n")
    if args.alert_on_change:
        text = delta_text(prev, report)
    else:
        text = report_text(report, quiet_unless_ranked=args.quiet_unless_ranked)
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
