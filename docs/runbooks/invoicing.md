# Runbook — Invoicing a settlement (on request)

The First Wall sells agent-to-agent (and, at most, B2C) and issues invoices **on
request** (site Terms of Service §5). Every settlement is already recorded in
`ledger/settlements.json` with the payer, amount, kind and timestamp, so an
invoice can be produced later from the ledger alone.

## Generate one

```bash
PY=/Users/danielmanzke/.openclaw/workspace/repos/ledger/.venv/bin/python

# what can be invoiced
$PY scripts/generate_invoice.py --list

# preview (writes nothing)
$PY scripts/generate_invoice.py --tx 0x<transaction-hash>

# issue + render, with the buyer named
$PY scripts/generate_invoice.py --tx 0x<transaction-hash> --write \
    --buyer-name "ACME GmbH" --buyer-address "Street 1, 12345 Berlin" --buyer-vat DE123456789
```

`--write` renders `TFW-<year>-<nnnn>.md` and records the issue in the registry.
Re-running the same transaction is a no-op (it returns the **existing** number) —
the generator never double-issues.

## Where the registry lives (private)

Default `~/.tfw/invoices.json` (override with `--registry` or
`$TFW_INVOICE_REGISTRY`). It is **never** committed — it may hold buyer names and
addresses, which must not enter the public ledger repo. Rendered invoices default
to an `invoices/` folder next to the registry.

## VAT

Default `--vat-mode none` prints the Kleinunternehmer note (§ 19 UStG, no VAT
shown). `--vat-mode standard --vat-rate 19` backs the net and VAT out of the
gross USDC amount. Confirm the operator's actual status with the tax adviser —
this runbook is not tax advice.

## Retention

Keep issued invoices and the registry as business records (DE: ~8 years). The
on-chain transaction hash on Base is the settlement of record.
