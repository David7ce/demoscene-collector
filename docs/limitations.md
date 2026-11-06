# Limitations

What this pipeline does **not** do, and why. Everything here is a deliberate
scope decision or an observed environmental constraint — both are recorded in
`docs/source_audit.md` with the probe evidence from 2026-10-10.

---

## Sources

**Demozoo is unreachable from this environment.** `api.demozoo.org` did not
resolve and `demozoo.org` timed out at the TCP level during the source audit.
The Demozoo adapter therefore performs a probe only and records an honest
failure; no Demozoo record was ever fetched, so Demozoo can contribute no
titles, credits or links. Demozoo ids that Pouët itself publishes
(`cross_ids.demozoo`) are still kept — they cost nothing and make a future
merge possible the moment Demozoo is reachable.

**No YouTube API key.** `YOUTUBE_API_KEY` is not set, so the pipeline cannot
run YouTube *search*. What it does instead: verifies video links that sources
explicitly published, via the keyless oEmbed endpoint. Consequence: videos
that exist but were never linked from Pouët/CSDb/scene.org are invisible to
this pipeline. It deliberately does **not** scrape YouTube search HTML —
that would violate the source's terms and be brittle.

**CSDb and scene.org have no public API.** They are used for what a
single-request contract allows: an existence/verification request per URL
(scene.org `/view/` = landing page, `/get/` = direct download; both verified
live during the audit). No crawling, no bulk listing, no second request per
page. Their richer metadata (CSDb credits, scene.org file listings) is
therefore largely absent from the collection.

**Pouët coverage is Pouët's coverage.** The entire population comes from one
Pouët dump. Productions that Pouët never listed, or listed without party
results, cannot be discovered here. Party results (`placings`) only exist in
the dump when Pouët recorded them — significance scores for unrecorded
compo results are simply absent, not zero-inferred into the selection.

## Data quality

* **Symbol-only names/titles** (`__`, `+$`, `!!!`, …) are real in the
  demoscene. Normalisation preserves them as distinct keys, but any *matching*
  involving them is only as good as the exact characters recorded.
* **Invisible markup** exists in the dump: 74 Pouët credit nicknames consist
  solely of `U+200E`. These credits are skipped and counted, not repaired.
* **Title collisions are common.** Generic titles (`intro`, `megademo`,
  `untitled`) merge only with creator + year + cross-source corroboration;
  everything else stays separate, which means some true duplicates may survive
  in the `needs_review` queue rather than being auto-merged.
* **Dates** are only as precise as the source: many records carry a year but
  no full date; the pipeline stores `NULL` rather than inventing "1 January".

## Scoring & selection

* Scores measure **documented evidence**, not artistic or historical
  importance. A technically groundbreaking prod with few votes scores low on
  popularity — by design, because popularity and significance are reported
  separately.
* `age` gives early productions a mild seniority bonus (weight 0.05). This is
  a documented bias toward historical seniority, not a claim that older =
  better.
* Diversity caps (`max_per_cell` = 30, minimums per family/era) can exclude a
  higher-scoring production in favour of a lower-scoring but underrepresented
  platform/era. Every such decision is visible in `selection_status` +
  `curation_rationale`.
* No machine-learn ranking, no embeddings, no LLM judging: the weights are
  hand-set in `config/curation_rules.yaml` and may not match any particular
  scene historian's taste.

## Verification

* Link checks are point-in-time. A `verified` status means "answered 200 on
  2026-10-10", not "will work forever". `last_checked_at` records when.
* `HEAD` is unreliable on some servers; the fallback is a ranged `GET`, and
  anything still ambiguous is `needs_review`, never a confident verdict.
* `ftp://`, `sftp://` and other non-HTTP schemes cannot be checked at all —
  recorded as `untested_protocol`.
* **No binary is ever downloaded or executed**, so "this demo actually runs"
  is out of scope by design. `verified` describes link reachability only.
* Redirect chains, geo-blocks and login walls (e.g. some archive.org items)
  produce `inaccessible`/`redirected`, which is not the same as `broken`.

## Engineering

* Single-process, single-machine; no parallel fetching (rate limits are
  respected by serialising requests per host).
* The canonical database is SQLite — fine for 100k records, not a substitute
  for a server database at millions.
* Heavy ingest writes are slow on the mounted Windows filesystem (`/mnt/d`);
  running the ingest against a Linux-native scratch database and copying the
  result back is measurably faster. Exports and reports are unaffected.
* HTTP caching is keyed by request; a stale cache entry can be served within
  its TTL. Delete `data/cache/` to force fresh requests.
* Idempotency is based on stable keys and `ON CONFLICT` upserts; two runs of
  `discover` on the same dump converge to the same state, but `updated_at`
  timestamps will differ (they are metadata, never inputs to scoring).
