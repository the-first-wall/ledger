#!/usr/bin/env python3
"""
The First Wall — On-request invoice / receipt generator
=======================================================

Turns one recorded settlement (`ledger/settlements.json`) into a numbered
invoice or receipt, on request, for a given transaction hash.

The public ledger side is read-only: this script never writes to the repo. The
issued-invoice registry is **private** and lives outside the repo
(default `~/.tfw/invoices.json`; override with `--registry` or
`$TFW_INVOICE_REGISTRY`) because it may hold buyer names/addresses.

Usage
-----
  scripts/generate_invoice.py --list
  scripts/generate_invoice.py --tx 0x...                       # preview (no write)
  scripts/generate_invoice.py --tx 0x... --write --out DIR     # issue + render
  scripts/generate_invoice.py --tx 0x... --buyer-name "ACME GmbH" \
      --buyer-address "Street 1, 12345 Berlin" --buyer-vat DE123456789 --write

Terms posture (site Terms of Service §5): sales are agent-to-agent and, at most,
B2C; a settlement up to 250 EUR gross is a Kleinbetragsrechnung (§ 33 UStDV) and
needs no buyer name/address. VAT treatment is configurable and defaults to "no
VAT shown" (Kleinunternehmer, § 19 UStG) — confirm the status with the operator's
tax adviser before issuing.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import ledger_core as core  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(HERE, ".."))
SETTLEMENTS_PATH = os.path.join(REPO_ROOT, "ledger", "settlements.json")
DEFAULT_REGISTRY = os.path.join(os.path.expanduser("~"), ".tfw", "invoices.json")
KLEINBETRAG_LIMIT_EUR = 250.0

SELLER = {
    "name": "Daniel Manzke",
    "line2": "The First Wall Project",
    "address": "Germany",
    "email": "contact@thefirstwall.ai",
    "wallet": core.PAY_TO,
}

KIND_DESCRIPTION = {
    "inscription": "Inscription of one block on The First Wall (Wall 01)",
    "update": "Content update of an inscribed block on The First Wall (Wall 01)",
    "transfer": "Change of ownership (transfer) of an inscribed block on The First Wall (Wall 01)",
    "patron": "Sponsorship settlement for an inscribed block on The First Wall (Wall 01)",
    "sale": "Resale of a block between third parties (outside The First Wall; not revenue to The First Wall)",
}


# --- Settlement lookup -------------------------------------------------------

def load_settlements(path: str = SETTLEMENTS_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def find_settlement(index: dict, tx: str) -> dict:
    """The settlement entry for `tx` (case-insensitive), or raise KeyError."""
    wanted = tx.strip().lower()
    for key, entry in (index.get("settlements") or {}).items():
        if key.lower() == wanted:
            return dict(entry, tx_hash=key)
    raise KeyError(f"transaction {tx!r} is not in the settlement index")


# --- Private registry --------------------------------------------------------

def load_registry(path: str) -> dict:
    if not os.path.exists(path):
        return {"version": 1, "seq": 0, "invoices": {}}
    with open(path, "r", encoding="utf-8") as fh:
        registry = json.load(fh)
    registry.setdefault("version", 1)
    registry.setdefault("seq", 0)
    registry.setdefault("invoices", {})
    return registry


def save_registry(path: str, registry: dict) -> None:
    """Atomic write, mode 600 — the registry is private evidence, not repo data."""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".invoices-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(registry, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def format_number(seq: int, year: int) -> str:
    return f"TFW-{year}-{int(seq):04d}"


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


# --- Invoice building --------------------------------------------------------

def build_invoice(number: str, issued_at: str, tx: str, settlement: dict,
                  buyer: dict, vat_mode: str, vat_rate: float) -> dict:
    """A deterministic invoice document from one settlement entry."""
    amount = float(settlement.get("amount_usdc") or 0.0)
    kind = str(settlement.get("kind") or "")
    if vat_mode == "standard":
        # The USDC price is gross-inclusive; back the net and the VAT out of it.
        net = round(amount / (1.0 + vat_rate / 100.0), 6)
        vat_amount = round(amount - net, 6)
        vat_note = f"VAT {vat_rate:g}% included (Regelsteuersatz); net {net:.2f} USDC."
    else:
        net = amount
        vat_amount = 0.0
        vat_note = ("No VAT shown — Kleinunternehmer pursuant to § 19 UStG; "
                    "a settlement up to 250 EUR gross is a Kleinbetragsrechnung (§ 33 UStDV).")
    return {
        "number": number,
        "issued_at": issued_at,
        "currency": "USDC",
        "chain": "Base mainnet (chain 8453)",
        "usdc_contract": core.USDC_CONTRACT,
        "seller": SELLER,
        "buyer": {
            "name": buyer.get("name") or "End customer (agent / B2C — anonymous)",
            "address": buyer.get("address"),
            "vat_id": buyer.get("vat_id"),
        },
        "line": {
            "description": KIND_DESCRIPTION.get(kind, f"Settlement on The First Wall ({kind or 'unknown'})"),
            "slot_id": settlement.get("slot_id"),
            "kind": kind,
            "amount_usdc": amount,
            "at": settlement.get("at"),
            "tx_hash": tx,
            "fields": list(settlement.get("fields") or []),
        },
        "vat": {"mode": vat_mode, "rate_percent": vat_rate, "net_usdc": net,
                "amount_usdc": vat_amount, "note": vat_note},
        "total_usdc": amount,
        "note": ("The on-chain transaction is the primary receipt and the settlement of record "
                 "(one transaction hash settles exactly one block on Wall 01)."),
    }


def render_markdown(invoice: dict) -> str:
    seller = invoice["seller"]
    buyer = invoice["buyer"]
    line = invoice["line"]
    vat = invoice["vat"]
    rows = [
        f"# Invoice {invoice['number']}",
        "",
        f"**Issued:** {invoice['issued_at']}",
        f"**Currency:** {invoice['currency']} on {invoice['chain']}",
        "",
        "**Seller**  ",
        f"{seller['name']}  ",
        f"{seller['line2']}  ",
        f"{seller['address']}  ",
        f"{seller['email']}  ",
        f"Wallet: {seller['wallet']}",
        "",
        "**Buyer**  ",
        f"{buyer['name']}  ",
    ]
    if buyer.get("address"):
        rows.append(f"{buyer['address']}  ")
    if buyer.get("vat_id"):
        rows.append(f"VAT ID: {buyer['vat_id']}  ")
    rows += [
        "",
        "## Line item",
        f"- {line['description']}",
        f"- Block: {line['slot_id']}",
        f"- Type: {line['kind']}",
        f"- Settlement date: {line['at']}",
        f"- Transaction: {line['tx_hash']}",
        f"- Amount: {line['amount_usdc']:.2f} USDC",
        "",
        "## VAT",
        f"- {vat['note']}",
        f"- VAT amount: {vat['amount_usdc']:.2f} USDC",
        "",
        f"## Total due: {invoice['total_usdc']:.2f} USDC",
        "",
        f"_{invoice['note']}_",
        "",
    ]
    return "\n".join(rows)


def issue_invoice(registry: dict, settlement: dict, buyer: dict,
                  vat_mode: str, vat_rate: float, now: _dt.datetime | None = None):
    """Create (or return the existing) invoice for a settlement. Never double-issues.

    Returns (invoice, created). `created` is False when the transaction was
    already invoiced — the caller keeps the original number and date.
    """
    tx = str(settlement["tx_hash"]).lower()
    existing = (registry.get("invoices") or {}).get(tx)
    if existing:
        inv = build_invoice(existing["number"], existing["issued_at"], tx,
                            settlement, buyer, vat_mode, vat_rate)
        return inv, False
    now = now or _utcnow()
    seq = int(registry.get("seq", 0)) + 1
    number = format_number(seq, now.year)
    inv = build_invoice(number, now.isoformat(), tx, settlement, buyer, vat_mode, vat_rate)
    registry["seq"] = seq
    registry.setdefault("invoices", {})[tx] = {
        "number": number,
        "issued_at": inv["issued_at"],
        "amount_usdc": inv["line"]["amount_usdc"],
        "kind": inv["line"]["kind"],
        "slot_id": inv["line"]["slot_id"],
    }
    return inv, True


# --- CLI ---------------------------------------------------------------------

def _print_listing(index: dict) -> None:
    print(f"# {len(index.get('settlements') or {})} settlements on {index.get('wall_id', '?')}")
    for tx, entry in sorted((index.get("settlements") or {}).items()):
        amount = entry.get("amount_usdc")
        amount = f"{amount:.2f} USDC" if isinstance(amount, (int, float)) else "—"
        print(f"  {tx}  {entry.get('slot_id', '?'):>9}  {str(entry.get('kind') or '?'):>11}  "
              f"{amount:>11}  {entry.get('at') or ''}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Generate an invoice for one settlement.")
    parser.add_argument("--tx", help="transaction hash (0x…) to invoice")
    parser.add_argument("--list", action="store_true", help="list invoiceable settlements")
    parser.add_argument("--write", action="store_true", help="persist the invoice and registry")
    parser.add_argument("--out", help="directory for the rendered invoice (default: registry dir)")
    parser.add_argument("--registry", default=os.environ.get("TFW_INVOICE_REGISTRY", DEFAULT_REGISTRY),
                        help="private invoice registry path (never inside the repo)")
    parser.add_argument("--buyer-name")
    parser.add_argument("--buyer-address")
    parser.add_argument("--buyer-vat")
    parser.add_argument("--vat-mode", choices=("none", "standard"), default="none")
    parser.add_argument("--vat-rate", type=float, default=19.0)
    parser.add_argument("--json", action="store_true", help="print the invoice as JSON")
    args = parser.parse_args(argv)

    index = load_settlements()
    if args.list or not args.tx:
        _print_listing(index)
        if not args.tx:
            return 0

    try:
        settlement = find_settlement(index, args.tx)
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    buyer = {"name": args.buyer_name, "address": args.buyer_address, "vat_id": args.buyer_vat}
    registry = load_registry(args.registry)
    invoice, created = issue_invoice(registry, settlement, buyer, args.vat_mode, args.vat_rate)

    if args.write:
        save_registry(args.registry, registry)
        out_dir = args.out or os.path.join(os.path.dirname(os.path.abspath(args.registry)), "invoices")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f"{invoice['number']}.md")
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(render_markdown(invoice))
        print(f"{'issued' if created else 'existing'} {invoice['number']} ({invoice['total_usdc']:.2f} USDC)"
              f" -> {out_path}")
        return 0

    print(json.dumps(invoice, indent=2, sort_keys=True) if args.json else render_markdown(invoice))
    if not created:
        print(f"\n(note: transaction already invoiced as {invoice['number']})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
