# Runbook — moving a block to a fresh wallet (key rotation)

**Who:** the owner of the block, with the block's current wallet (once) and the new wallet.
**Applies to:** the ten founding blocks (`w1-b0001` … `w1-b0010`) at any time, and every
other block once every public block is sold (`transfers_open: true` in `state.json`).

## What it is

A key rotation is an ordinary **transfer** from your old wallet to your new one, with
nothing else special about it. The ledger cannot tell a rotation from a sale and does not
try to: it checks the old wallet's signed release, a USDC payment on Base from the new
wallet to the old one for the signed price, and the transfer fee. The old wallet stays on
record forever in `ownership_lineage[]`, like every previous owner.

The old key is needed **once**, to sign the release. A key that is lost for good cannot
release the block: nobody can consent on its behalf.

## What it costs, per block

| What | Amount | Paid by |
| :-- | :-- | :-- |
| the transfer fee, to `pay_to` (`transfer_price_usdc`) | 5.00 USDC | the new wallet |
| the "sale" payment to the old wallet (price you sign; 0.01 is enough) | 0.01 USDC | the new wallet, to your own old wallet |
| Base gas for those two transactions | cents of ETH | the new wallet |

So the new wallet needs a little more than 5.01 USDC per block and a little ETH on Base.
Two blocks moved to one new wallet: 10.02 USDC, two pull requests.

## Order matters

1. **A legacy block is upgraded first.** A block inscribed before claims were signed
   (schema `1.1.x`: the genesis blocks until `docs/runbooks/legacy-slot-upgrade.md` has run)
   must be signed by its *paying* wallet, and a transferred legacy block can no longer be
   signed by its original payer (it stays advisory: the verifier only warns). Finish the upgrade (manifest correction, claim signature,
   `schema_version 1.2.0`) in its own pull request, merged, **before** you rotate.
2. **One block, one pull request, one fee.** Rotate each block in its own pull request.

## Steps

Work in a fresh clone of the ledger on a branch. Keep both key files **outside** the clone
and out of any repository or backup that is shared; the new key is generated offline.

1. **Create the new wallet** and fund it: USDC and a little ETH on Base.
2. **Release (old wallet).** Sign the offer to your new address; it moves nothing on its
   own, names one buyer and one price and expires after about a day:

   ```bash
   python scripts/sign_transfer.py ledger/w1/<slot_id>.json --to <new_wallet> --price 0.01 --key-file old.key
   ```

   It prints the signature, the price and the deadline block. The old key is not needed again.
3. **Check it (new wallet).**

   ```bash
   python scripts/sign_transfer.py --check-offer ledger/w1/<slot_id>.json --to <new_wallet> \
       --price 0.01 --valid-until <block> --signature 0x…
   ```
4. **Pay the price.** From the new wallet send exactly the signed price (0.01 USDC) in USDC
   on Base to the **old** wallet, in **one** transaction, before the deadline. That hash is
   `transfer_tx_hash`.
5. **Pay the ledger.** From the new wallet send the transfer fee (5.00 USDC) to `pay_to`.
   Wait 300 blocks for both transactions.
6. **Edit the slot's dossier** (`ledger/w1/<slot_id>.json`):
   * `wallet_address` becomes the new wallet. Do this **before** step 7: the fee tool reads
     the payer from `wallet_address`.
   * `ownership_lineage` gains exactly one entry, the outgoing owner **as inscribed**:

     ```json
     {
       "moniker": "<as inscribed>",
       "wallet_address": "<old wallet>",
       "soul_hash": "<as inscribed>",
       "base_tx_hash": "<as inscribed>",
       "acquired_date": "<the block's instantiation_date>",
       "relinquished_date": "<now, ISO 8601 UTC>",
       "sale_price_usdc": 0.01,
       "transfer_tx_hash": "<step 4>",
       "offer_valid_until_block": <deadline from step 2>,
       "transfer_signature": "0x…"
     }
     ```

   * `slot_id`, `base_tx_hash`, `settlement` and `claim_signature` stay: the first owner's
     claim remains the block's settlement.
7. **Record the transfer fee.**

   ```bash
   python scripts/record_update.py ledger/w1/<slot_id>.json 0x<fee_tx> --kind transfer --write
   ```
8. **Regenerate and verify.**

   ```bash
   python scripts/generate_state.py && python scripts/generate_spec.py
   python scripts/sign_transfer.py --verify ledger/w1/<slot_id>.json
   python scripts/verifier.py
   ```
9. **Open one pull request** that changes only that slot's files and the generated state.

From the merge on, only the new wallet owns the block: every later update and fee is paid
from it, and the old key can do nothing with the block.

## If something is rejected

| Message | Meaning |
| :-- | :-- |
| `transfers open once every block is sold` | the block is not a founding block and the wall is not sold out yet |
| `transfer_signature was made by …, not by the outgoing owner` | the release was signed by a different key than the block's current wallet |
| `transfer_tx_hash is required` | step 4 is missing, or the payment was not from the new wallet to the old one |
| `after the offer expired` | the payment was mined after the deadline; sign a new offer |
