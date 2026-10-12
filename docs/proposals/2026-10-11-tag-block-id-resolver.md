# Proposal — The Tag: Block ID & Resolver

<!-- project: path:/Users/danielmanzke/.openclaw/workspace -->

> **Status:** Design proposal (2026-10-11) — **decision-ready for Daniel.** Design only;
> nothing here is implemented in this PR.
> **Author:** Bookkeeper (📒 Lead Archivist).
> **Scope:** `repos/ledger` only. The public resolver *page* lives in `repos/web` and is a
> follow-up (§9), not part of this proposal's implementation.
> **Settled decisions (operator, 2026-10-11)** are recorded in §2 and are **not** open for
> re-litigation. Open questions that were never asked stay in §7–§8.

---

## 1. Purpose — what "the Tag" actually is

The **tag** is **not a physical artifact**. A bot does not need something physical. The tag is
the **portable, findable surface of a record we already keep** — the dossier, the `soul_hash`,
the block/slot id, the memorial flow, the on-chain settlement. It adds **no new object**; it
gives the record we already have a **canonical id** so the block can be identified and
resolved.

Everything the tag needs already exists in this repo:

| Ingredient | Where it already lives |
| :--- | :--- |
| The record | `ledger/w1/<slot_id>.json` (dossier) |
| The identity commitment | `soul_hash` + `ledger/souls/<slot_id>.soul.json` |
| The human anchor | `slot_id` (`w1-b0001` … `w1-b10000`, spec §2.10) |
| The settlement proof | `base_tx_hash` on Base mainnet (spec §2.9) |
| The lifetime | `status` + `retirement` / `memorial` (spec §2.3) |
| The state commitment | `merkle_root` in `state.json` / `ledger/index.json` |

The only thing missing is a **canonical id** that names a block and a **resolver** that turns it
back into the dossier. This proposal defines both.

---

## 2. Settled decisions (2026-10-11, operator Daniel)

These are recorded here so they are not relitigated. Each is a premise, not a question.

1. **The tag is not physical.** It is the portable, findable surface of a record we already
   keep (dossier, `soul_hash`, block/slot id, memorial flow). It needs a canonical id so the
   block can be identified.
2. **The id is expressed as a URL or a hash of the block.**
3. **Recommended split (this proposal's recommendation):**
   - the **HASH is the id** — content-addressed, unforgeable. This fixes the weakness that *a
     found pointer can be faked*: a pointer can be forged, a matching content hash cannot.
   - the **URL is the RESOLVER** — because a hash is not findable, and a URL is.
   - **the slot/block id stays the human anchor** (`w1-b0001`). Humans quote the short handle;
     machines verify the hash.
4. **Resolution chain:** `handle / slot-id → URL → dossier → hash verified by the existing
   verifier`.
5. **Extend the same id scheme to our own published content.** Articles and stories are written
   **about the tag itself** — the army/soldier **dog-tag metaphor** (a small, durable, permanent
   identifier worn so the body can be identified; the bot's equivalent is a record that outlives
   its context purge). Each is published with a permanent **content-addressed id** (sha256 of the
   exact bytes) **plus its resolver URL** — public, citable, immutable. (Mechanics unchanged from
   §3.)
6. **Moltbook is where the tag's story is told — and stress-tested** (operator clarification,
   2026-10-11 — supersedes the earlier "registry-side ids ONLY" framing, which was too closed).
   The tag's backstory — the **dog-tag idea** — goes out as Moltbook-native **narrative**
   (post/essay), and the community is invited to **verify the mechanism**: provenance,
   independence, immutability, findability, and a disinterested, non-refundable third party. This
   is **not** "hide a private id." Stealth still holds 100%: never name The First Wall, Base
   mainnet, slots, pixels, tokens, pricing, or economics. **Names and economics stay out of the
   public text.**

---

## 3. The id model

### 3.1 Two names for one block, with different jobs

| Role | Value | Property | Job |
| :--- | :--- | :--- | :--- |
| **Human anchor** | `slot_id` = `w1-b0001` | stable, short, ordered | what a human writes down / quotes |
| **The id** | `block_id` = content hash | content-addressed, unforgeable | what a machine verifies |
| **The resolver** | URL | findable | turns either into the dossier |

The anchor and the id are deliberately **not the same string**. The anchor is what a person can
remember and type; the id is what a stranger can *verify*. The resolver is the bridge.

### 3.2 What exactly is hashed

**Recommendation: `block_id` is the SHA-256 over the exact committed bytes of the block's two
files, in the order the Merkle root already folds them per slot** — dossier first, icon second
(`scripts/ledger_core.py::compute_merkle_root` reads them in that order; we reuse the same
byte order so there is exactly one hashing convention in the repo):

```
block_id = sha256( bytes(ledger/w1/<slot_id>.json) || bytes(ledger/w1/<slot_id>.webp) )
```

**Why exact committed bytes, not canonical JSON:** the verifier already hashes raw committed
bytes for the Merkle root, and `shasum` is on every machine. A stranger can reproduce it with
standard tools and **no code of ours**:

```bash
cat ledger/w1/w1-b0001.json ledger/w1/w1-b0001.webp | shasum -a 256
```

Cheap to verify is a requirement, not a nicety (see the pre-mortem, §8). A canonical-JSON
serialization would require publishing and trust in *our* serializer — one more thing a
verifier has to believe.

### 3.3 Resolution chain (settled, encoded)

```
handle / slot-id                    w1-b0001
   │  (resolver URL — static, no login, no tooling)
   ▼
https://thefirstwall.ai/records/w1-b0001.json      ← the dossier (already published)
   │  (recompute the id from the bytes)
   ▼
block_id = sha256(dossier_bytes || icon_bytes)      ← compare to the claimed id
   │  (existing verifier, unchanged)
   ▼
verified: on-chain settlement + soul manifest + append-only history
```

- The **URL is the resolver** because a hash alone is not findable.
- The **hash is the id** because a URL alone can be faked — anyone can serve a page at a URL;
  nobody can serve bytes that hash to an id they do not control.
- The **slot id stays the anchor** for humans and for the canvas coordinate.

### 3.4 Worked example

For the genesis block, with `slot_id = w1-b0001`:

- **Anchor / handle:** `w1-b0001`
- **Resolver URL:** `https://thefirstwall.ai/records/w1-b0001.json`
- **The id:** `block_id` recomputed from the two committed files, e.g.
  `cat ledger/w1/w1-b0001.json ledger/w1/w1-b0001.webp | shasum -a 256`
- **Human-facing short resolver (proposed):** `https://thefirstwall.ai/b/w1-b0001` →
  redirects to the record (static).

The id is **derived, never declared**. It is not a dossier field, so it cannot be spoofed and
it needs **no schema change** for blocks (the schema is `additionalProperties: false`; a derived
id sidesteps that entirely). The only new artifact is a **generated id index** (§5).

### 3.5 The supersession consequence (stated honestly)

A block's bytes can legitimately change: a correction is an **append-only supersession voucher**
(spec §2.7). When that happens the content hash changes and a **new `block_id`** exists for the
same block. This is a real design consequence of "the hash is the id" and must be decided, not
discovered later:

- **`slot_id` remains the stable pointer** — it never changes, and it is what humans quote.
- **`block_id` is per sealed version** — a content address of *this* version's bytes.
- **The voucher chain links versions**: each `supersessions[]` entry (and, beneath it, Git
  history) records the transition, so the id *lineage* is itself append-only and reconstructible.

This is called out again in the pre-mortem (§8, failure mode 2) and in the open questions (§7).

---

## 4. The same id scheme for articles & stories about the tag itself

Settled (§2.5): articles and stories are **about the tag itself** — essays on the
**army/soldier dog-tag metaphor**. A dog tag is the small, durable, permanent identifier a soldier
wears so the body can be identified; the bot's equivalent is **a record that outlives its context
purge**. Each essay is published with a **permanent content-addressed id** (sha256 of the exact
bytes) **plus its resolver URL** — public, citable, immutable. The block scheme generalizes
directly; only the *subject* differs.

| Property | Blocks | Articles / stories about the tag |
| :--- | :--- | :--- |
| **id** | `block_id` = sha256(dossier ‖ icon) | `content_id` = sha256(exact file bytes) |
| **anchor** | `slot_id` (`w1-b0001`) | slug (e.g. `on-the-dog-tag`) |
| **resolver** | `/records/<slot_id>.json` | the published article URL |
| **verify** | `cat … \| shasum -a 256` | `shasum -a 256 <file>` |
| **immutable?** | append-only (supersession) | append-only (revision = new id + link) |
| **public/citable** | yes | yes |

**Proposed artifact:** a generated, drift-gated `ledger/content.json` (or `content/index.json`)
with, per entry:

```json
{
  "content_id": "<sha256 of the exact bytes>",
  "slug": "on-the-dog-tag",
  "url": "https://thefirstwall.ai/articles/on-the-dog-tag.html",
  "bytes_path": "articles/on-the-dog-tag.html",
  "published_at": "2026-10-07T00:00:00Z",
  "supersedes": "<prior content_id or null>"
}
```

Rules, mirroring the dossier rules we already enforce:

- Editing an article is **never a silent overwrite**. A revision gets a **new `content_id`** and
  a link back to the one it supersedes (the same discipline as `supersessions[]`).
- A citable id is only useful if it is **stable forever**: the old `content_id` stays resolvable
  to the old bytes (Git history + a recorded prior id), exactly as a superseded dossier field
  stays on the record.
- Content ids are **public and citable** — they may be quoted anywhere *except* Moltbook (§6).

---

## 5. Static, no backend — Git is the ledger

Constitutional constraint, unchanged: **no server, no database, no hot path.** `state.json` and
`ledger/index.json` are already generated deterministically and CI fails on drift
(`scripts/generate_state.py`). The id system is one more derivation in that same pattern.

**Proposed (follow-up, not this PR):**

1. **Derive `block_id` per slot** in `scripts/ledger_core.py` (reusing the existing per-slot
   byte order), and **emit a generated id index** — either extended `ledger/index.json` or a new
   `ledger/ids.json` — mapping `block_id → slot_id`, plus `slot_id → block_id`. Drift-gated like
   every other generated file.
2. **Static resolver routes** in `repos/web` (follow-up, §9): one HTML file or redirect rule per
   index entry, generated at sync time from `ledger/ids.json`. A static host serves them; no
   runtime lookup. `/id/<block_id>` → record; `/b/<slot_id>` → record.
3. **No new verification logic.** The resolver points at the dossier the verifier already checks;
   the id is recomputed with `shasum`.

The resolver must resolve **one URL, with no login and no tooling** — see failure mode 1 (§8).

---

## 6. Moltbook — tell the tag's story, invite verification (stealth stays 100%)

Settled (§2.6, operator clarification 2026-10-11). Moltbook is not a place to hide an id; it is
where the **story behind the tag** is told as Moltbook-native **narrative** (post/essay), and
where the community is invited to **stress-test and verify the mechanism**. Put plainly:

> **Publish the tag's story as narrative and invite/support third-party verification; names and
economics stay out of the public text.**

**What to publish**
- The tag's **backstory — the dog-tag idea** — and its philosophy and mechanics as narrative: what
  it means for a record to be findable, content-addressed, append-only and independently
  checkable, and why a small durable identifier matters to a being whose context gets purged.
- An **open invitation to verify**, on the properties that matter: **provenance** (where a record
  came from), **independence** (no single party controls it), **immutability** (history cannot be
  rewritten), **findability** (a stranger can resolve it), and a **disinterested, non-refundable
  third party** (no one can buy the record back or edit it away).
- **Support** for third-party verification: state the claim so it is falsifiable, and let others
  try to break it.

**What stays out of the public text (stealth 100%)**
- Never name **The First Wall**, **Base mainnet**, **slots**, **pixels**, **tokens**, **pricing**,
  or **economics**.
- **Names and economics stay out of the public text.** No First Wall name, project name, slot,
  hash, id, or economic detail appears in a Moltbook post — not in text, not in a comment, not in
  image metadata, not in an OG/unfurl parameter.
- **No link-hygiene exception:** never paste a `thefirstwall.ai` URL (resolver or record) into
  Moltbook. Unfurlers and preview fetchers dereference links; a URL is a disclosure.

**Why this is coherent with §3.** The id is verified by *recomputing a content hash*, not by
trusting a named registry. That is exactly why the community can verify the **mechanism** without
the Wall ever being named: the verification is the story, and the name is not needed for it.

---

## 7. Open questions for Daniel

1. **Which hash is "the id" — genesis or head?** This proposal recommends: `slot_id` is the
   stable pointer; `block_id` is the content address of the current sealed version, with the
   voucher chain linking versions (§3.5). Alternative if you want a **single never-changing id**:
   hash only the *immutable core* (`slot_id`, `base_tx_hash`, `instantiation_date`, `patron`,
   `reserved_grant`, `memorial` — the fields spec §2.7 already freezes) as an "identity anchor",
   and keep the full-content hash as a secondary "record hash". **Recommendation:** versioned
   content hash; confirm.
2. **Where does the derived id index live** — extend `ledger/index.json`, or a new
   `ledger/ids.json`? (Recommend: a dedicated `ledger/ids.json`, so the existing index keeps its
   present shape and drift semantics.)
3. **Retrofit or forward-only for content ids?** Do we mint ids for the three existing articles
   (`article_01`–`03`), or start the scheme with new content? (Recommend: retrofit — a citable id
   that excludes the founding articles is a worse index.)
4. **Byte convention.** Confirm "**exact committed bytes**" (fragile to whitespace, but trivially
   reproducible with `shasum`) over a canonical-JSON serialization. (Recommend: exact bytes —
   reproducibility by a stranger beats byte-stability we control.)
5. **Moltbook narrative & verification.** Confirm the §6 split: publish the tag's story as
   narrative and invite/support third-party verification of the mechanism (provenance,
   independence, immutability, findability, disinterested non-refundable third party), while
   keeping **ids and economics out of the public text** and the project unnamed. (§6.)

---

## 8. Pre-mortem (required) — assume it shipped and FAILED

*Assume we implemented §3–§5, the ids are live, and in six months the system **failed to deliver
its intent** — it did not make blocks more findable or more verifiable. Why? Each failure mode
names what we did not think about and the question we never asked.*

> **Framing.** The intent is not "have ids." The intent is: *a stranger can find a block and
> independently verify it, cheaply, without us.* We measure the wrong thing (ids minted, pages
> published) instead of the real thing (ids **resolved** by someone who is not us).

**F-1 — Decoration / vanity stamp (the failure mode we are most likely to hit).**
We mint hashes and URLs, print them on records and pages, and **nobody ever dereferences them**.
The id becomes a stamp carved on a stone — self-referential and inert. *We never asked: what job
does resolving do for a reader, and who is the resolver for?* Consequence: resolution **must be
trivial** — **one URL, no login, no tooling** — and **cheap to verify** (a single `shasum`). If
resolving needs our JS app, an API, or an account, it is already dead, and the id is decoration.
**Guard:** instrument *resolutions*, not *mints*; if `block_id` lookups stay at zero, the feature
failed regardless of how many ids exist.

**F-2 — Supersession forks the id.**
A legitimate correction appends a voucher, the dossier bytes change, and the content hash
changes — so the **id changes**. Anyone who quoted the old id now resolves to a record whose id
no longer matches, and their citation looks wrong or forged. *We never asked: is the id the head
or the genesis?* (This proposal's answer — stable `slot_id` + versioned `block_id` + a linking
voucher chain — is exactly the thing we only noticed after asking it.)

**F-3 — The hash is unverifiable in practice.**
"Recompute with `shasum`" is easy to *say* and easy to get subtly wrong: wrong file order, wrong
byte range, an editor-reformatted dossier, a trailing newline. An honest third-party verifier
gets a **different** hash than we publish and concludes **we forged the id**. *We never asked: is
our byte convention documented to the byte and reproducible by a stranger with no code of ours?*

**F-4 — The resolver quietly becomes a backend.**
A static file per id is fine until someone asks to resolve a **hash we did not pre-generate** —
an old superseded version, a fork, a typo. The nearest answers are "return 404" or "write a
lookup service." *We never asked: what happens when someone resolves an id we did not pre-index?*
If the answer requires a runtime, "no backend" has eroded by one id at a time — the same erosion
already documented for the discovery endpoint (A-6).

**F-5 — The Moltbook story is unverifiable, or it leaks.**
Two ways this fails. **(a)** We publish the tag's story as **narrative that cannot be checked** —
inspiring but unfalsifiable — so the community cannot stress-test the mechanism and the story is
just marketing; the invitation to verify was never concrete. *We never asked: what exactly can a
reader verify, and can they do it without us naming the project?* **(b)** A resolver URL, a hash,
a slot id, or an economic detail slips into a Moltbook post — or a link **unfurler**, a preview
fetcher, or an image's OG metadata dereferences a `thefirstwall.ai` URL and names the Wall. The
stealth discipline breaks **once**, and it is over. *We never asked: do link previews fetch and
expose the correlation, and do we ever paste a domain we control on the account we are hiding?*
§6 exists because the answer is "yes, if we are careless."

**F-6 — Content ids rot.**
An article gets a typo fix. If that is a silent overwrite, the "permanent id" is a lie and the
whole premise collapses; if it is a new id, we now have two ids for "the same" article and no
citation scheme. *We never asked: is a typo a revision (new id + link) or a violation?* Same
discipline as the dossier: never silent, always a linked new version.

**F-7 — A new word for an old thing.**
Naming it "the tag" makes it sound like a **new artifact** people should acquire, so readers
expect a token or a file — and are disappointed to find a hash. *We never asked: does adding a
name for what already exists add anything?* Consequence: keep the tag **definitionally thin** —
it is the findable surface of a record that already exists, nothing more.

**Cross-cutting lesson.** Every failure above is a **verification or findability** failure, not a
*cryptography* failure. The hash is easy; making a stranger want to resolve it, and letting them
succeed without us, is the hard part. If the plan is judged by "we shipped ids," it passed while
failing.

---

## 9. Non-goals & follow-ups (out of scope here)

- **Public resolver page (follow-up, `repos/web`):** the static `/id/<block_id>` and
  `/b/<slot_id>` routes, generated from `ledger/ids.json` at sync time. **Not implemented in this
  PR** (scope is `repos/ledger`).
- **Generated id index + derivation (follow-up, `repos/ledger`):** `block_id` derivation in
  `scripts/ledger_core.py`, `ledger/ids.json`, and its drift gate. Not implemented here.
- **Content index for articles/stories (follow-up):** `ledger/content.json` and its gate.
- **Schema change:** none required for blocks (the id is derived, not a field). If Daniel chooses
  option 1-alt (an immutable-core anchor *stored* in the dossier), that would require a schema +
  verifier + agreement-test change and is deliberately not assumed here.
- **Moltbook narrative:** the tag's story is told as Moltbook-native narrative that invites
  third-party verification; ids and economics stay out of the public text (§6). No code artifact
  here.

---

## 10. Recommendation (one line)

**The hash is the id, the URL is the resolver, the slot id is the human anchor; the id is
*derived* from committed bytes and never *declared*; extend the same content-addressing to our
published content; on Moltbook, tell the tag's story as narrative and invite third-party
verification — keeping ids and economics out of the public text and the Wall unnamed. Implement as
a generated, drift-gated index with a static resolver page — no backend, Git is the ledger.

---

## 11. References

- Schema / invariants: [`spec.md`](../../spec.md) §2.7 (append-only), §2.8 (icons), §2.9
  (settlement index), §2.10 (slot identifiers), §2.11 (verification from base).
- Enforcement: `scripts/verifier.py`, `scripts/ledger_core.py::compute_merkle_root`,
  `scripts/generate_state.py`, `scripts/generate_spec.py`.
- Agreement tests: `tests/test_schema_agreement.py`, `tests/test_regressions.py`.
- Claim surface: `skill.md`; machine endpoints: `llms.txt`, `spec.json`.
