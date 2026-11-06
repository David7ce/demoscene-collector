# Methodology

How `demoscene-collector` turns ~103k raw Pouët records into a curated
collection of ≤ 500 notable productions. Every rule below is implemented in
code and configured in `config/curation_rules.yaml`; nothing is hard-coded
inside algorithms.

Access date for all source observations: **2026-10-10**.

---

## 1. Discovery (Stage A)

Primary source: the **Pouët weekly JSON dump**
(`pouetdatadump-prods-20261007.json.gz`, 102,973 productions, dump date
2026-10-07 04:30:01 UTC, SHA-256 recorded in `fetch_jobs.response_hash`).

Chosen over the Pouët API because a published, immutable, checksum-able dump
gives complete, reproducible coverage without rate-limit throttling, and over
scraping because it is an official dataset (see `docs/source_audit.md`).

For each dump record the adapter keeps **only what the record contains**:

* identity: `pouet:<id>` (stable canonical key), source URL
* title (display form preserved, incl. case/punctuation), release date
* production type, groups + credited users + roles, platforms
* release/download links (classified `direct_download` vs `landing_page`)
* video links (YouTube/Vimeo only — hosts we can verify)
* cross-ids (`demozoo`, `csdb`, `zxdemo`, `sceneorg`) — kept even though
  Demozoo is unreachable, so merges remain possible later
* raw popularity (`voteup/votedown/voteavg/rank/popularity`) and significance
  evidence (`placings`, `awards`, `party`, `invitation`, `types`)

One `provenance` row per production records the observation with the dump
URL, the raw JSON value and an observation timestamp.

Records that fail to parse (or have no usable title) are skipped with a
logged warning — never silently dropped; the skip is counted in the stage
stats written to `fetch_jobs.detail`.

### Normalisation (Stage B)

| Field | Rule |
|---|---|
| title (display) | NFC, invisible control/format chars removed, whitespace collapsed — **case and punctuation preserved** |
| title (matching key) | lowercase, diacritics stripped (NFKD), `_ / - +` → space, punctuation removed; if that is empty (symbol-only titles like `!!!`), fall back to the cleaned original so distinct symbol titles never collide |
| names | same rules as titles |
| platforms | mapped to canonical name + family (`Amiga`, `Commodore 64`, `PC/MS-DOS`, `Windows`, `Atari ST`, `Other`) |
| URLs | scheme/host lowercased, default ports and `www.` removed, tracking params (`utm_*`, `fbclid`, …) stripped, fragment dropped |
| dates | parsed to ISO; year kept separately; unparseable → `NULL` (never guessed) |

Invisible characters matter in practice: 74 Pouët credit nicknames consist of
nothing but a `U+200E` left-to-right mark. They are stripped and the credit is
skipped (counted, not silently discarded).

---

## 2. Deduplication (Stage C)

Identity resolution is deterministic and evidence-based. Two records can only
be compared when they *could* match — same normalised title, or a shared
cross-reference id — which makes the full 103k population tractable
(`O(n + Σ group²)`) and keeps everything else distinct by construction.

Confidence = weighted evidence, capped at 0.98:

| Evidence | Contribution |
|---|---|
| title similarity (normalised, `SequenceMatcher`) | × 0.55 |
| shared creator/group name | + 0.25 |
| identical release year | + 0.15 (± 1 year: + 0.07) |
| platform overlap | + 0.05 |
| shared cross-id (`demozoo=…`, or A's cross-id equals B's own id) | → 0.99, always a merge |

Decision bands (thresholds configurable):

* **≥ 0.92 and cross-source** → automatic merge. The losing record becomes an
  alias; the merge is appended to `merge_log` with evidence JSON (non-destructive
  audit trail).
* **0.75–0.92, or identical-title pairs lacking cross-source corroboration** →
  `needs_review`. Never auto-merged.
* **identical normalised title with no creator/year/platform overlap** →
  **distinct**. Generic titles (`intro`, `megademo`, `untitled`) collide
  constantly; without corroboration a collision is not a duplicate.
* **below 0.75** → distinct.

Merge survivor is chosen deterministically: the record with more evidence
rows, then the lower canonical key.

---

## 3. Scoring (Stage D)

`popularity_score` and `significance_score` are computed **independently** and
exported as separate columns. `curation_score` is the explicit weighted sum of
all components; each component (0..1) and its weight is stored as JSON in
`curation_rationale`, so every score is auditable.

### Components (evidence-based, never artistic judgement)

| Component | Weight | Definition |
|---|---|---|
| `votes` | 0.15 | `log1p(voteup) / log1p(500)`, capped at 1 — Pouët thumb-ups |
| `rank` | 0.10 | Pouët all-time rank mapped over the observed ranked population |
| `placement` | 0.25 | best competition result: 1st = 1.00, 2nd = 0.80, 3rd = 0.65 … 8th = 0.15, finalist = 0.10 |
| `awards` | 0.15 | recorded awards, `min(1, count / 2)` |
| `age` | 0.05 | historical seniority, `1 − (year − 1980) / 60` (floor 0.05; unknown year = 0.20) |
| `video_evidence` | 0.10 | an explicit, verifiable video link exists |
| `download_evidence` | 0.10 | an original release link exists |
| `provenance` | 0.05 | groups (0.4) + credits (0.3) + platforms (0.3) recorded |
| `type_relevance` | 0.05 | in-scope type = 1.0; configured out-of-scope types (games, tools) = 0.35 |

**Popularity sub-score** = weighted mean of `votes`, `rank` (weights renormalised).
**Significance sub-score** = weighted mean of `placement`, `awards`, `age`.

A missing video is a small documentation penalty only (weight 0.10, switchable
via `penalise_missing_video`); historically significant prods without surviving
captures are never dropped for that reason — they are exported to
`missing_videos.md` instead.

---

## 4. Diversity-aware selection

Pure score-ranking would fill the collection with 1990s PC demos. Selection
therefore runs three passes over the score-ordered list:

1. **Minimum representation** — at least `min_per_platform_family` (20) per
   family and `min_per_era` (10) per era, when enough candidates exist.
2. **Cell caps** — at most `max_per_cell` (30) per (platform family × era) cell.
3. **Budget fill** — remaining slots by score alone.

Ties always break on `canonical_key`, so the selection is byte-identical
across runs. Candidates excluded by a configured rule are marked
`selection_status='rejected'` with the reason — never deleted.

---

## 5. Enrichment & verification (Stages E–F)

**Videos.** Only links explicitly published by a source are verified — no
guessing at video ids. YouTube/Vimeo URLs are checked through the public
**oEmbed** endpoint (no API key needed): HTTP 200 + a returned title ⇒
`verified_match`; 404/410 ⇒ `unavailable`; anything else ⇒ `needs_review`.
Title similarity against the production title (`≥ 0.80`) additionally feeds
`match_confidence`. YouTube *search* requires `YOUTUBE_API_KEY`; without it the
stage reports "search unavailable" honestly instead of scraping.

**Release links.** One `HEAD` request per URL (falling back to a ranged `GET`
when the host rejects `HEAD`). Results:

| Status | Meaning |
|---|---|
| `verified` | direct artifact answered 200/206 |
| `landing_ok` | the URL is a landing page that answered 200 (e.g. scene.org `/view/`) |
| `broken` | 404/410 |
| `inaccessible` | 401/403 (e.g. archive.org login-walled items) |
| `redirected` | 3xx that did not resolve |
| `blocked` | 429 / rate-limited |
| `untested_protocol` | `ftp://` and friends — cannot be checked over HTTP, recorded as such |

Budgets come from config (`max_link_checks`, `max_link_checks_per_host`) and
every check writes `last_checked_at` + `check_detail`. No artifact is ever
downloaded or executed.

---

## 6. Verification (Stage G)

`verify` re-reads the database and checks structural invariants: every selected
production has ≥ 1 provenance row, scores are populated, CSV/JSON/Markdown
exports agree with SQLite row counts, no orphan links, and review-queue records
are flagged. Failures exit non-zero and are printed in the quality report.

---

## 7. Determinism

* stable canonical keys (`pouet:<id>`), sorted iteration everywhere;
* explicit tie-breaks ending in `canonical_key`;
* timestamps only in `created_at/updated_at`/`observed_at`, never inside
  scored or sorted fields;
* exports are written in id order → repeated runs on the same input produce
  byte-identical CSV/JSON/Markdown.
