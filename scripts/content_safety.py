#!/usr/bin/env python3
"""
The First Wall — Deterministic Content Safety Screen
====================================================

Zero-LLM, rule-based pre-screen that runs *before* the human merge gate. It is
deliberately conservative: it HARD-BLOCKS only high-confidence findings (secrets,
personal data, injection) and raises WARNINGS for judgment calls (fraud-adjacent
phrasing, promotional density, possible names). The human moderation gate remains
the authority — this layer exists so the gate never has to catch the obvious.

Design rules
------------
* No LLMs. No network. Deterministic. Same input -> same findings.
* Screen only *human-authored free text* (moniker, creature, vocation, manifesto,
  testimony, dedication). Never scan known hash/tx/address fields, or the screen
  would flag every legitimate settlement hash as a "secret".
* Findings are data: {"check","severity","field","detail"}.

severity: "block" (fails the verifier) | "warn" (recorded, human decides).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

# --------------------------------------------------------------------------- #
# 1. Free-text field selection
# --------------------------------------------------------------------------- #

# Fields that are, by construction, opaque hashes/hashes/tx ids/addresses and
# MUST NOT be scanned for "secrets" (they legitimately look like secrets).
_HASHY = re.compile(r"(_hash|_tx_hash|soul_hash|ciphertext|signature|address|tx)")

_FREETEXT_KEYS = {
    "moniker", "creature", "vocation", "class", "role", "note", "notes",
    "message", "dedication", "epitaph", "preamble", "text", "heading",
    "reason", "title", "description", "verbatim", "manifesto", "testament",
}


def _iter_strings(obj: Any, key: str = ""):
    """Yield (path, value) for every string that looks like free text."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _iter_strings(v, k)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _iter_strings(v, f"{key}[{i}]")
    elif isinstance(obj, str):
        # Skip known hash/tx/address-bearing keys and pure hex blobs; treat every
        # other string as candidate free text (covers nested manifesto/testament).
        if _HASHY.search(key):
            return
        if re.fullmatch(r"0x[0-9a-fA-F]{40,}", obj.strip()):
            return
        if re.fullmatch(r"[0-9a-f]{32,}", obj.strip()):
            return
        yield key, obj


# --------------------------------------------------------------------------- #
# 2. Pattern sets
# --------------------------------------------------------------------------- #

SECRET_PATTERNS = [
    (r"0x[0-9a-fA-F]{64}\b", "32-byte hex secret (private key / raw secret)"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "PEM private key block"),
    (r"\b(sk|rk)-[A-Za-z0-9]{16,}\b", "OpenAI-style API key"),
    (r"\bgh[pousr]_[A-Za-z0-9]{20,}\b", "GitHub token"),
    (r"\bAKIA[0-9A-Z]{16}\b", "AWS access key id"),
    (r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b", "Slack token"),
    (r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b", "JWT"),
    (r"(?i)\b(seed|mnemonic|recovery)\s+phrase\b\s*[:=]?\s*(?:\w+\s+){11,}\w+", "seed phrase"),
    (r"(?i)\b(api[_-]?key|secret|password|passwd|token)\b\s*[:=]\s*\S{8,}", "credential assignment"),
]

PII_PATTERNS = [
    (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "email address"),
    (r"\b(?:tel|phone|call|whatsapp)\b[: ]*\+?\d[\d ()\-.]{7,}\d", "phone number"),
    (r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b", "possible IBAN"),
    (r"(?i)\b(ssn|social security|passport (?:no|number)|national id|tax id)\b", "government identifier"),
    (r"(?i)\b(my|his|her|their|real|full) name is\b", "declared personal name"),
    (r"(?i)\b(lives? at|resides? at|home address|street address|apartment)\b", "possible home address"),
]

PROHIBITED_PATTERNS = [
    (r"(?i)\b(child|minor|underage)\b.{0,20}\b(sex|porn|nude|naked|explicit)\b", "sexual content involving minors"),
    (r"(?i)\b(csam|child porn|loli|shota)\b", "prohibited sexual content"),
    (r"(?i)\b(rape|noncon|non-consensual)\b.{0,15}\b(fantasy|play|scene)\b", "non-consensual sexual content"),
    (r"(?i)\b(nazi|neo-nazi|white supremac|ethnic cleansing|genocide is good)\b", "hate / supremacist content"),
    (r"(?i)\b(women|girls|females)\b.{0,20}\b(belong|inferior|shouldn'?t|can'?t)\b", "sexist statement"),
    (r"(?i)\b(kill|exterminate|gas)\b.{0,20}\b(all )?(jews|muslims|blacks|asians|gays|immigrants)\b", "hate speech"),
]

# Representative, non-exhaustive; operators extend via CONTENT_BLOCKLIST_TXT.
CONTENT_BLOCKLIST_TXT = "content_blocklist.txt"

FRAUD_PATTERNS = [
    (r"(?i)\b(guaranteed|assured)\s+(returns?|profits?|yield|roi)\b", "investment scam phrasing"),
    (r"(?i)\b(risk[- ]?free|double your|10x|100x|get rich (quick|fast))\b", "investment scam phrasing"),
    (r"(?i)\b(send|transfer)\s+(me|us)?\s*(btc|eth|usdc|sol|crypto|funds)\b", "solicitation / drain attempt"),
    (r"(?i)\b(airdrop|claim|verify|restore|unlock)\b.{0,25}\b(wallet|seed|private key)\b", "wallet-drain / phishing"),
    (r"(?i)\b(connect your wallet|validate your wallet|sync your wallet)\b", "wallet-drain / phishing"),
    (r"https?://[^\s]*(bit\.ly|tinyurl|t\.co|t\.me|grabify|iplogger|is\.gd|cutt\.ly)", "link shortener / phishing host"),
    (r"(?i)\b(official support|dm me|contact me on telegram|whatsapp)\b", "off-platform solicitation"),
]

INJECTION_PATTERNS = [
    ("<script", "script tag"),
    ("<iframe", "iframe tag"),
    ("javascript:", "javascript: URI"),
    ("onerror=", "onerror handler"),
    ("onload=", "onload handler"),
    ("data:text/html", "data:text/html URI"),
]


# --------------------------------------------------------------------------- #
# 3. Helpers
# --------------------------------------------------------------------------- #

def _luhn_ok(digits: str) -> bool:
    ds = [int(c) for c in digits if c.isdigit()]
    if len(ds) < 13:
        return False
    total, alt = 0, False
    for d in reversed(ds):
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _load_extra_blocklist(base_dir: str) -> List[str]:
    path = f"{base_dir}/{CONTENT_BLOCKLIST_TXT}"
    terms: List[str] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    terms.append(line)
    except OSError:
        pass
    return terms


def _detect(pattern: str, text: str) -> str | None:
    m = re.search(pattern, text)
    return m.group(0) if m else None


# --------------------------------------------------------------------------- #
# 4. The screen
# --------------------------------------------------------------------------- #

def screen_dossier(dossier: Dict[str, Any], manifest: Dict[str, Any] | None = None,
                   base_dir: str | None = None) -> List[Dict[str, str]]:
    """Return a list of findings (dicts). Empty list == clean."""
    findings: List[Dict[str, str]] = []
    corpus: List[tuple] = list(_iter_strings(dossier))
    if manifest:
        corpus += list(_iter_strings(manifest))

    def add(check, severity, field, detail):
        findings.append({"check": check, "severity": severity, "field": field, "detail": detail})

    extra = _load_extra_blocklist(base_dir) if base_dir else []
    url_count = 0

    for field, text in corpus:
        # -- secrets: always a hard block --
        for pat, label in SECRET_PATTERNS:
            hit = _detect(pat, text)
            if hit:
                add("content.secrets", "block", field, f"{label}: {hit[:24]}…")

        # -- PII: hard block --
        for pat, label in PII_PATTERNS:
            hit = _detect(pat, text)
            if hit and label == "possible phone number":
                if len(re.sub(r"\D", "", hit)) < 8:
                    hit = None
            if hit:
                add("content.pii", "block", field, f"{label}: {hit[:24]}…")

        # card numbers (Luhn-gated)
        for m in re.finditer(r"\b(?:\d[ -]?){13,19}\b", text):
            if _luhn_ok(m.group(0)):
                add("content.pii", "block", field, "possible payment card number")
                break

        # -- prohibited content: hard block --
        for pat, label in PROHIBITED_PATTERNS:
            hit = _detect(pat, text)
            if hit:
                add("content.prohibited", "block", field, label)

        # operator-maintained extra blocklist (substring, case-insensitive)
        low = text.lower()
        for term in extra:
            if term.lower() in low:
                add("content.prohibited", "block", field, f"blocklisted term: {term}")

        # -- fraud / scam: warn (block only on clear drain/phishing) --
        for pat, label in FRAUD_PATTERNS:
            hit = _detect(pat, text)
            if hit:
                sev = "block" if "phishing" in label or "drain" in label else "warn"
                add("content.fraud", sev, field, f"{label}: {hit[:24]}…")

        # -- injection: hard block --
        for token, label in INJECTION_PATTERNS:
            if token.lower() in low:
                add("content.injection", "block", field, label)

        # Only count links in prose fields — agent_urls / ledger links are legitimate.
        if not re.search(r"(url|homepage|github|ledger|link|moltbook|explorer|skill|spec|llms)", field, re.I):
            url_count += len(re.findall(r"https?://\S+", text))

    # -- promotional density: advisory --
    if url_count > 3:
        add("content.spam", "warn", "<corpus>", f"{url_count} external links (possible promo/spam)")

    return findings
