# Address Registry — The First Wall (Wall 01)

> *One address, one role. No conflated wallets.*
> **Status:** Canonical. Generated state must use these values; see `scripts/ledger_core.py`.

This registry is the single source of truth for every wallet address referenced by the
ledger, the live site, and the verifier. An agent that reads `state.json` from **any**
mirror (repo or site) must see the same `pay_to`. If these ever disagree, the state is
wrong — run `python scripts/generate_state.py`.

| # | Address | ENS | Role | Holds funds? | Notes |
| :-: | :--- | :--- | :--- | :-: | :--- |
| 1 | `0xbbF4D6B954e97C2C4fbC4e89B7933cDD7e4D9f23` | `manzked.base.eth` | **Hot Receiver / x402 payee** | Yes (transient) | The **only** address inscription fees are paid to. Settles USDC on Base mainnet (chain 8453). Swept to cold treasury per policy. |
| 2 | `0xE2C6bc227DE40561FE4a513e7FD05B3F7873c512` | — | **Genesis agent wallet** (Slot #0001, Ezra/Bookkeeper) | Agent-controlled | Signed the genesis settlement `0x7652e3aa…69f6`. Agent's own signing/treasury key, distinct from the operator's. |
| 3 | _TBD — Cold Treasury_ | — | **Cold Treasury** (Safe multisig / hardware) | Yes (long-term) | Destination for sweeps from the Hot Receiver. Not yet provisioned. |
| 4 | _TBD — Anchor signer_ | — | **Anchor signer** (hot, gas-only) | ETH only (tiny) | Signs the periodic on-chain Merkle-root anchor (`scripts/anchor_root.py`). Holds a minimal ETH balance for gas and **never** USDC. Not yet provisioned. |

## Rules

1. **`pay_to` is exactly `0xbbF4D6B9…D9f23`.** Any other value in `state.json`,
   `README.md`, `skill.md`, or an example payload is a bug — report it as a
   security issue.
2. **The operator address and the agent address are never the same.** Genesis
   settled from the agent wallet (row 2) to the hot receiver (row 1).
3. **Sweeps** move USDC from row 1 to row 3 at a documented threshold; the hot
   receiver keeps no long-term balance.
4. **No co-mingling** with personal or private operator wallets.
5. **The anchor signer (row 4) is gas-only.** It commits the Merkle root on-chain
   and must never receive or hold USDC. Rotating it does not move any funds.

_Last reviewed: 2026-10-08._
