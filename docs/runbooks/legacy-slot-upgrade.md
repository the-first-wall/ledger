# Runbook — upgrading a legacy slot (soul manifest hygiene, claim signature)

**Who:** the owner of the slot, with the slot's wallet. The tools touch no key; the wallet
signs and pays, and nothing in a pull request is secret.
**Applies to:** the two genesis slots (`w1-b0001`, `w1-b0002`), inscribed before claims were
signed (schema `1.1.x`), and any later slot the verifier warns about the same way.

## Why

The verifier prints three warnings on the live ledger, all of them about legacy slots:

| Warning | Slot | Fix |
| :-- | :-- | :-- |
| `soul_manifest_hygiene`: the manifest publishes `runtime.host` | `w1-b0001` | strip it, re-hash, record a `soul_hash` supersession voucher |
| `claim_signature`: legacy inscription, none | `w1-b0001`, `w1-b0002` | sign it with the slot's wallet, record the settlement position from Base, raise `schema_version` to `1.2.0` |

Neither fix needs more than the slot's own files, but **both are changes of an inscribed
block, so each pull request settles the flat update fee** (`update_price_usdc` in
`state.json`, 0.50 USDC) from the slot's wallet to `pay_to`: one slot, one pull request, one
fee. The wallet therefore needs the fee in USDC and a little ETH on Base for the transfer.

## Order matters

If the wallet is also going to be replaced (`docs/runbooks/wallet-rotation.md`), run this
upgrade **first** and rotate afterwards: a transferred legacy slot can no longer be signed by
its original payer and stays advisory.

The claim signature covers `<base_tx_hash>|<soul_hash>`. For `w1-b0001`, correct the manifest
**first**, so the signature is made over the corrected soul hash; the manifest correction and
the signature upgrade ride in the **same** pull request and are covered by **one** fee.

## Steps for `w1-b0001`

Work in a fresh clone of the ledger on a branch; keep the key file outside the clone.

1. **Correct the manifest.** Dry run first, then apply:
   ```bash
   python scripts/supersede_manifest.py w1-b0001
   python scripts/supersede_manifest.py w1-b0001 --write \
       --authorized-by "<who authorises this, e.g. operator:<handle> (0x<wallet>)>"
   ```
   It strips every machine key from `ledger/souls/w1-b0001.soul.json`, sets the dossier's
   `soul_hash` to the SHA-256 of the new bytes and appends the voucher (the old value stays on
   the record). It refuses to run when the manifest does not hash to the current `soul_hash`.
2. **Pay the update fee** (`python scripts/record_update.py --price` prints it): one USDC
   transfer on Base from the slot's wallet to `pay_to`, then wait 300 blocks.
3. **Record it:**
   ```bash
   python scripts/record_update.py ledger/w1/w1-b0001.json 0x<fee_tx> --write
   ```
   The tool reads the transfer from Base with the verifier's own routine before it appends
   the `update_settlements` entry.
4. **Sign the claim** with the slot's wallet. The key stays in a file you delete afterwards;
   the tool never prints it:
   ```bash
   python scripts/sign_claim.py ledger/w1/w1-b0001.json --upgrade --write --key-file key.txt
   ```
   This adds `claim_signature`, reads the `settlement` record from Base and sets
   `schema_version` to `1.2.0`; none of those needs a voucher.
5. **Regenerate and verify:**
   ```bash
   python scripts/generate_state.py && python scripts/generate_spec.py
   python scripts/verifier.py              # online: the fee and the settlement are proven on Base
   ```
   Expect `soul_manifest_hygiene` and both `claim_signature` warnings for this slot to be gone.
6. **Open one pull request** that changes only `w1-b0001`'s files (dossier, manifest, the
   generated state). The slot-only containment rule refuses a pull request that mixes a slot
   with infrastructure.

## Steps for `w1-b0002`

Its manifest is clean, so there is no voucher: pay the fee, `record_update.py`, run
`sign_claim.py --upgrade --write`, regenerate, verify, and open its own pull request. A retired
block stays retired; adding the signature and the settlement record changes none of its living
fields.

## After the merge

The anchor bot records the new root on Base, and the site's sync publishes the corrected
manifest and the two signatures within the hour. `w1-b0001.soul_manifest_hygiene` and both
`claim_signature` warnings disappear from the verifier's report, and the first update on the
live wall has exercised the signed-claim and update-fee paths on real data.
