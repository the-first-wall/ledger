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
| 4 | `0x39A8546448baFd5E98f513022A772555c2082465` | — | **Anchor signer** (hot, gas-only) | ETH only (tiny) | Signs the periodic on-chain Merkle-root anchor (`scripts/anchor_root.py`). Holds a minimal ETH balance for gas and **never** USDC. Provisioned 2026-10-08; key held in the agent's protected store, outside git. |
| 5 | `0xF5F4A8C8285AA409dF46Fd1673a86fb120b0d0fD` | — | **ERC-8004 identity holder** (operator-held) | No (holds the identity token only) | Holds ERC-8004 `agentId 107162` in the Base registry `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432`; `tokenURI(107162)` → `https://thefirstwall.ai/.well-known/agent-registration.json`. Confirmed by the operator 2026-10-24 (B-23). The tokenURI is mutable by whoever holds the token, so a change of holder is a deliberate, recorded act — not a quiet one. |

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
6. **The identity holder (row 5) is not a funds address.** It holds the ERC-8004
   identity token and nothing else — no USDC, no ETH required. Rule 4 ("no
   co-mingling") governs *funds*; this row carries none. The token's holder can
   repoint `tokenURI` without a commit, a PR, or a verifier run, so the holder is
   documented here and the `ownerOf`/`tokenURI` pair is re-checked against the
   chain on the weekly full verification.

_Last reviewed: 2026-10-24._
