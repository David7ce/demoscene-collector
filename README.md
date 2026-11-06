# demoscene-collector

STATUS: active

A local-first, auditable pipeline that discovers, curates, enriches, verifies
and exports a collection of **up to 500 notable demoscene productions**.

Every production in the output carries provenance back to the source record it
came from, popularity and historical/technical significance are scored
**separately**, and nothing is ever fabricated: if a fact could not be observed
at a reachable source, the pipeline records that absence instead of filling it
in.

The pipeline never downloads or executes production binaries — only metadata,
HTTP status codes and (where a source offers them) text/JSON payloads.

---

## Quick start

```bash
# from the project root
export PYTHONPATH=src                     # no install step needed
python3 -m demoscene_collector --help

# 1. create/upgrade the SQLite schema (offline)
python3 -m demoscene_collector init-db

# 2. discover: read the Pouët JSON dump (offline when the dump is present)
python3 -m demoscene_collector --offline discover

# 3. curate: deduplicate, score, select (offline)
python3 -m demoscene_collector curate --target 500

# 4. enrich / verify-links: video oEmbed checks + HEAD checks (network)
python3 -m demoscene_collector enrich
python3 -m demoscene_collector verify-links --limit 400

# 5. verify, export, report (offline)
python3 -m demoscene_collector verify
python3 -m demoscene_collector export
python3 -m demoscene_collector report

# or everything in one go (skips stages that fail, reports them honestly)
python3 -m demoscene_collector run --offline
```

Outputs land in `exports/` (CSV, JSON, Markdown and a self-contained gallery) and
`logs/` (quality report, execution log). The canonical state lives in
`data/demoscene.sqlite3`.

### Gallery

`exports/index.html` opens directly in a browser (no Node, server or network).
It embeds the curated collection and offers search, era/type/platform filters,
an inclusive year range, and clickable column headers that sort with a ▲/▼
indicator. It opens on the **500 selected** productions; the 1,743 ambiguous
review-queue entries stay behind an "Include review queue" toggle, so the
default view is the actual selection rather than the whole candidate pool. The
page carries the same honesty labels as `catalogue.md`: a verified link is
recorded HTTP evidence, not proof that a production runs.

### Tests

```bash
PYTHONPATH=src python3 -m pytest tests -q     # 187 tests, no network access
```

The test suite is fully offline: every HTTP interaction is served from
recorded fixtures in `tests/fixtures/`.

---

## Dependencies

Python ≥ 3.12 (developed on 3.14) plus:

| Package | Why |
|---|---|
| `requests` | HTTP client (rate-limited, cached, never downloads artifacts) |
| `PyYAML` | reads `config/sources.yaml` and `config/curation_rules.yaml` |
| `pytest`  | test suite only |

No ORM, no database server: SQLite through the standard library.

```bash
python3 -m pip install requests PyYAML pytest
```

---

## Layout

```
demoscene-collector/
├── config/
│   ├── sources.yaml          # per-source access method, rate limits, terms URLs
│   └── curation_rules.yaml   # weights, thresholds, diversity caps, budgets
├── data/
│   ├── demoscene.sqlite3     # canonical state (schema version tracked inside)
│   └── input/                # downloaded source dumps (Pouët weekly dump)
├── docs/
│   ├── source_audit.md       # mandatory pre-implementation source audit
│   ├── methodology.md        # scoring, dedup, diversity, verification rules
│   ├── data_dictionary.md    # every table/field/export column explained
│   └── limitations.md        # what this pipeline does NOT do, and why
├── exports/                  # generated: CSV + JSON + Markdown + index.html
├── logs/                     # generated: quality report, execution log
├── src/demoscene_collector/  # the package
│   ├── schema.sql            # canonical, versioned schema
│   ├── database.py           # connection, upserts, provenance, jobs, cursors
│   ├── normalization.py      # titles, names, platforms, URLs, dates
│   ├── models.py             # typed candidates + stable keys
│   ├── http_client.py        # rate-limited, cached, artifact-free HTTP
│   ├── source_adapters/      # pouet, scene_org, csdb, demozoo, youtube
│   ├── deduplication.py      # deterministic identity resolution
│   ├── selection.py          # scoring + diversity-aware selection
│   ├── verification.py       # link + video checks
│   ├── pipeline.py           # stage orchestration
│   ├── export_csv.py / export_json.py / export_markdown.py / export_html.py
│   ├── reports.py            # quality report / execution log
│   └── cli.py                # argparse CLI
└── tests/                    # offline unit + integration tests
```

---

## Stage map (spec §7)

| Stage | Command | Network? | What it does |
|---|---|---|---|
| 0. source audit | *(done before implementation)* | yes | `docs/source_audit.md` |
| A. discover | `discover` | no¹ | Pouët dump → raw candidates + provenance |
| B. normalize | *(inside A)* | no | canonical titles/names/URLs/dates |
| C. deduplicate | `curate` | no | identity resolution, merge log, review queue |
| D. score/select | `curate` | no | popularity vs significance, diversity caps |
| E. enrich | `enrich` | yes | video verification (oEmbed), optional API search |
| F. verify links | `verify-links` | yes | HEAD/ranged-GET of release links |
| G. verify | `verify` | no | integrity + coverage checks over SQLite |
| H. export | `export` | no | CSV + JSON + Markdown + HTML gallery |

¹ uses the local dump when present; only downloads it when missing and not
`--offline`.

---

## Determinism & resumability

* Stable keys (`pouet:1234`) and deterministic tie-breaking (`canonical_key`
  last) mean repeated runs produce byte-identical exports.
* Discovery checkpoints its progress in the `cursors` table — `--resume`
  continues from the last Pouët id instead of restarting.
* Every stage records a row in `fetch_jobs` (status, attempts, response hash),
  so failures are visible rather than silent.
* HTTP responses are cached under `data/cache/`; re-runs do not re-hit sources.

## Provenance

Every externally sourced fact gets a row in `provenance`:

```
entity_type | entity_id     | field_name | source_id | source_url | observed_value_json | observed_at
production  | pouet:1000    | title      | pouet     | https://…  | {"title": "…" }     | 2026-10-10T…
```

`observed_value_json` stores what the source actually said, not a normalised
version of it, so any claim in the exports can be traced back to raw evidence.

## Configuration

* `config/sources.yaml` — access method (API/dump/scrape-disabled), rate limit,
  terms and documentation URLs, enabled flag.
* `config/curation_rules.yaml` — target size, score weights, placement points,
  diversity caps, dedup thresholds, verification budgets.

Both are plain YAML; nothing about scoring is hard-coded in the algorithms.

## Honesty rules

1. No fabricated data — every field is traceable to a source record.
2. Unreachable sources are reported as unreachable, not skipped quietly.
3. A missing video or dead download link is *evidence*, and is exported as
   such (`missing_videos.md`, `unavailable_downloads.md`).
4. `popularity_score` and `significance_score` are always separate columns;
   `curation_score` is the documented weighted combination, never a black box.
