# Data dictionary

Everything the pipeline stores and exports. Schema version **1**
(`src/demoscene_collector/schema.sql`).

Conventions:

* **Stable key** = `canonical_key`, e.g. `pouet:1000` — the join key used in
  exports and provenance rows.
* All timestamps are UTC ISO-8601 with milliseconds: `2026-10-10T12:00:00.000Z`.
* `NULL`/empty means *the source did not provide this* — never a guess.
* Scores are floats in 0..1.

---

## SQLite tables

### `sources`

One row per configured source, plus its last observed status.

| Column | Type | Meaning |
|---|---|---|
| `name` | TEXT | source id used everywhere else (`pouet`, `scene.org`, `csdb`, `demozoo`, `youtube`) |
| `base_url`, `source_type` | TEXT | endpoint and kind (`dataset`, `api`, `website`) |
| `terms_url`, `api_documentation_url` | TEXT | terms-of-use and API docs (from the source audit) |
| `access_method` | TEXT | `api`, `published dump`, `single-request verification`, `disabled` |
| `status` | TEXT | `unknown/ok/degraded/unreachable/disabled/error` |
| `last_success_at`, `last_failure_at`, `notes` | TEXT | probe history from `audit-sources` |

### `productions`

One row per *canonical* production (after deduplication).

| Column | Meaning |
|---|---|
| `canonical_key` | stable id, `pouet:<id>` |
| `title` | display title as published (case/punctuation preserved) |
| `normalized_title` | matching key (lowercase, punctuation-free) |
| `release_year`, `release_date` | parsed year / raw-or-ISO date |
| `production_type` | Pouët type (`demo`, `intro`, `4k`, `wild`, …) |
| `description` | short, fully source-derived summary |
| `popularity_score` | weighted mean of votes + rank (**community reception only**) |
| `significance_score` | weighted mean of placement + awards + age (**historical/technical**) |
| `curation_score` | documented weighted sum of all components |
| `curation_rationale` | JSON: every component value, its weight, and the cell |
| `selection_status` | `candidate / selected / rejected / needs_review` |
| `selection_rank` | 1..N within the selected set |
| `created_at`, `updated_at` | row timestamps |

### `aliases`

Alternate titles kept for merged/duplicate records — nothing is lost on merge.

| Column | Meaning |
|---|---|
| `production_id` | survivor production |
| `alias`, `normalized_alias` | the other record's title (display / matching form) |
| `source_id`, `source_record_id` | where that alias was observed |

### `merge_log`

Non-destructive audit trail of deduplication decisions.

| Column | Meaning |
|---|---|
| `survivor_key`, `merged_key` | canonical keys |
| `method` | `exact_id`, `cross_source_id`, `high`, … |
| `confidence` | 0..1 match confidence |
| `evidence_json` | title ratio, creator/year/platform overlap, cross-ids |

### `creators`, `production_creators`

| Column | Meaning |
|---|---|
| `creators.canonical_name` | name as published |
| `creators.normalized_name` | matching key (unique) |
| `production_creators.role` | Pouët role (`group`, `code`, `music`, `gfx`, `unknown`, …) |

### `platforms`, `production_platforms`

| Column | Meaning |
|---|---|
| `canonical_name` | normalised platform name (`Commodore 64`, `Amiga`, …) |
| `platform_family` | diversity bucket: `PC/MS-DOS`, `Windows`, `Amiga`, `Commodore 64`, `Atari ST`, `Other` |
| `evidence_url` | where the platform was observed |

### `releases`

Original downloadable artifacts — **metadata only, never downloaded**.

| Column | Meaning |
|---|---|
| `download_url` | the link as published |
| `landing_page_url` | companion page when the link is not direct |
| `url_kind` | `direct_download` / `landing_page` / `unknown` (from URL shape) |
| `artifact_type` | `executable`, `archive`, `disk image`, `source code`, `video capture`, `documentation`, `landing page`, `other` |
| `file_name`, `file_size_bytes`, `checksum`, `requirements` | when the source states them |
| `verification_status` | `untested / verified / landing_ok / broken / inaccessible / redirected / blocked / untested_protocol` |
| `last_checked_at`, `check_detail` | verification history |

### `videos`

| Column | Meaning |
|---|---|
| `platform`, `video_id`, `canonical_url` | host, id, normalised URL |
| `title`, `channel_name`, `published_at`, `duration_seconds` | from oEmbed/authoritative source |
| `video_kind` | `demo capture`, `party recording`, `review`, `interview`, `trailer`, `unknown` |
| `match_confidence`, `match_evidence` | 0..1 and the *textual* reason for the match |
| `verification_status` | `untested / verified_match / probable_match / needs_review / rejected / unavailable` |

### `provenance`

The evidence behind **every** externally sourced claim.

| Column | Meaning |
|---|---|
| `entity_type` | `production`, `release`, `video`, … |
| `entity_id` | stable key (`pouet:1000`) or row id as text |
| `field_name` | which claim this supports (`title`, `release_year`, …) |
| `source_id`, `source_record_id`, `source_url` | where it was observed |
| `observed_value_json` | **raw** value as the source stated it |
| `observed_at`, `confidence` | when (`high/medium/low`) |

### `fetch_jobs`, `cursors`

Operational tables: one row per stage/source attempt (status, attempts, HTTP
status, response hash, error text) and named checkpoints for `--resume`.

### `production_metrics`

Scoring inputs kept in-database (`popularity_json`, `significance_json`,
`cross_ids_json`) so scoring and exports are reproducible from SQLite alone.

---

## CSV exports (`exports/*.csv`, UTF-8-BOM, RFC-4180)

| File | Grain | Notable columns |
|---|---|---|
| `productions.csv` | 1 row / production | scores + `selection_status`, `selection_rank`, `curation_rationale` (JSON) |
| `creators.csv` | 1 row / creator | `canonical_name`, `normalized_name` |
| `production_creators.csv` | 1 row / (production, creator, role) | `role` |
| `platforms.csv` | 1 row / platform | `platform_family` |
| `releases.csv` | 1 row / release | `download_url`, `url_kind`, `verification_status` |
| `videos.csv` | 1 row / video | `canonical_url`, `video_kind`, `match_confidence`, `verification_status` |
| `provenance.csv` | 1 row / observation | `observed_value_json`, `source_url` |
| `verification_report.csv` | 1 row / checked entity | `entity_type`, `entity_id`, `status`, `score`, `detail` |

The CSVs cover the **whole population**; the JSON/Markdown exports cover the
curated selection + review queue.

## `exports/demoscene.json`

```jsonc
{
  "schema_version": 1,
  "generator": "demoscene-collector …",
  "generated_at": "…",
  "selection_filter": ["selected", "needs_review"],
  "summary": { "counts": {…}, "population": 102973, "note": "…" },
  "productions": [
    {
      "canonical_key": "pouet:1000",
      "source": "pouet", "source_record_id": "1000",
      "title": "…", "release_year": 1993, "release_date": "…",
      "production_type": "demo", "description": "…",
      "scores": { "popularity": 0.71, "significance": 0.84,
                  "curation": 0.77, "rationale": { "components": {…}, … } },
      "selection": { "status": "selected", "rank": 1 },
      "creators":   [ { "name": "…", "role": "code" } ],
      "platforms":  [ { "name": "Amiga", "family": "Amiga" } ],
      "releases":   [ { "download_url": "…", "verification_status": "verified", … } ],
      "videos":     [ { "url": "…", "video_kind": "demo capture", … } ],
      "cross_ids":  { "demozoo": "63820" },
      "provenance": [ { "field": "title", "source": "pouet", "source_url": "…",
                        "value": {…}, "observed_at": "…" } ]
    }
  ]
}
```

## Markdown exports

| File | Purpose |
|---|---|
| `README.md` | how the collection was built, counts, caveats |
| `catalogue.md` | the selected productions, grouped by era |
| `missing_videos.md` | selected productions with **no** verified video |
| `unavailable_downloads.md` | release links that failed verification |
| `review_required.md` | ambiguous dedup records + unverified matches |

## Logs

| File | Purpose |
|---|---|
| `logs/quality_report.md` | counts, coverage, failures, review queue size |
| `logs/execution_log.md` | per-stage execution record (status, timings, errors) |
