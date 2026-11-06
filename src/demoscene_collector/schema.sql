-- ---------------------------------------------------------------------------
-- demoscene-collector schema (version 1)
-- Canonical, versioned SQLite schema. Migrations append new versions below.
-- All timestamps are UTC ISO-8601 with milliseconds: 2026-10-10T12:00:00.000Z
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Sources
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sources (
    id                      INTEGER PRIMARY KEY,
    name                    TEXT NOT NULL UNIQUE,
    base_url                TEXT NOT NULL,
    source_type             TEXT NOT NULL,
    terms_url               TEXT,
    api_documentation_url   TEXT,
    access_method           TEXT NOT NULL,
    last_success_at         TEXT,
    last_failure_at         TEXT,
    status                  TEXT NOT NULL DEFAULT 'unknown'
                          CHECK (status IN ('unknown','ok','degraded','unreachable','disabled','error')),
    notes                   TEXT
);

-- ---------------------------------------------------------------------------
-- Productions
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS productions (
    id                  INTEGER PRIMARY KEY,
    canonical_key       TEXT NOT NULL UNIQUE,
    title               TEXT NOT NULL,
    normalized_title    TEXT NOT NULL,
    release_year        INTEGER CHECK (release_year IS NULL OR (release_year >= 1900 AND release_year <= 2100)),
    release_date        TEXT,
    production_type     TEXT NOT NULL DEFAULT 'other',
    description         TEXT,
    popularity_score    REAL,
    significance_score  REAL,
    curation_score      REAL,
    curation_rationale  TEXT,
    selection_status    TEXT NOT NULL DEFAULT 'candidate'
                          CHECK (selection_status IN ('candidate','selected','rejected','needs_review')),
    selection_rank      INTEGER,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_productions_norm_title ON productions(normalized_title);
CREATE INDEX IF NOT EXISTS idx_productions_year       ON productions(release_year);
CREATE INDEX IF NOT EXISTS idx_productions_status     ON productions(selection_status);
CREATE INDEX IF NOT EXISTS idx_productions_type       ON productions(production_type);

CREATE TABLE IF NOT EXISTS aliases (
    id                  INTEGER PRIMARY KEY,
    production_id       INTEGER NOT NULL REFERENCES productions(id) ON DELETE CASCADE,
    alias               TEXT NOT NULL,
    normalized_alias    TEXT NOT NULL,
    source_id           TEXT,
    source_record_id    TEXT,
    created_at          TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_aliases_unique
    ON aliases(production_id, normalized_alias, IFNULL(source_id,''), IFNULL(source_record_id,''));
CREATE INDEX IF NOT EXISTS idx_aliases_normalized ON aliases(normalized_alias);

-- Merge history: never destructive, keeps an audit trail of dedup decisions.
CREATE TABLE IF NOT EXISTS merge_log (
    id              INTEGER PRIMARY KEY,
    survivor_key    TEXT NOT NULL,
    merged_key      TEXT NOT NULL,
    method          TEXT NOT NULL,
    confidence      REAL NOT NULL,
    evidence_json   TEXT NOT NULL,
    merged_at       TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_merge_log_unique ON merge_log(survivor_key, merged_key);

-- ---------------------------------------------------------------------------
-- Creators / groups
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS creators (
    id              INTEGER PRIMARY KEY,
    canonical_name  TEXT NOT NULL,
    normalized_name TEXT NOT NULL UNIQUE,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS production_creators (
    id            INTEGER PRIMARY KEY,
    production_id INTEGER NOT NULL REFERENCES productions(id) ON DELETE CASCADE,
    creator_id    INTEGER NOT NULL REFERENCES creators(id) ON DELETE CASCADE,
    role          TEXT NOT NULL DEFAULT 'unknown',
    source_id     TEXT,
    UNIQUE (production_id, creator_id, role)
);
CREATE INDEX IF NOT EXISTS idx_prod_creators_creator ON production_creators(creator_id);

-- ---------------------------------------------------------------------------
-- Platforms
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS platforms (
    id             INTEGER PRIMARY KEY,
    canonical_name TEXT NOT NULL UNIQUE,
    platform_family TEXT NOT NULL DEFAULT 'Other'
);
CREATE INDEX IF NOT EXISTS idx_platforms_family ON platforms(platform_family);

CREATE TABLE IF NOT EXISTS production_platforms (
    id            INTEGER PRIMARY KEY,
    production_id INTEGER NOT NULL REFERENCES productions(id) ON DELETE CASCADE,
    platform_id   INTEGER NOT NULL REFERENCES platforms(id) ON DELETE CASCADE,
    source_id     TEXT,
    evidence_url  TEXT,
    UNIQUE (production_id, platform_id)
);

-- ---------------------------------------------------------------------------
-- Releases (original downloadable artifacts; metadata only)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS releases (
    id                  INTEGER PRIMARY KEY,
    production_id       INTEGER NOT NULL REFERENCES productions(id) ON DELETE CASCADE,
    source_id           TEXT NOT NULL,
    release_title       TEXT,
    release_version     TEXT,
    platform_id         INTEGER REFERENCES platforms(id) ON DELETE SET NULL,
    artifact_type       TEXT NOT NULL DEFAULT 'other'
                          CHECK (artifact_type IN ('executable','archive','disk image','source code',
                                                   'video capture','documentation','other','landing page')),
    download_url        TEXT NOT NULL,
    landing_page_url    TEXT,
    url_kind            TEXT NOT NULL DEFAULT 'unknown'
                          CHECK (url_kind IN ('direct_download','landing_page','unknown')),
    file_name           TEXT,
    file_size_bytes     INTEGER,
    checksum            TEXT,
    requirements        TEXT,
    verification_status TEXT NOT NULL DEFAULT 'untested'
                          CHECK (verification_status IN ('untested','verified','landing_ok','broken',
                                                         'inaccessible','redirected','blocked','untested_protocol')),
    last_checked_at     TEXT,
    check_detail        TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_releases_unique ON releases(production_id, source_id, download_url);
CREATE INDEX IF NOT EXISTS idx_releases_status ON releases(verification_status);
CREATE INDEX IF NOT EXISTS idx_releases_url   ON releases(download_url);

-- ---------------------------------------------------------------------------
-- Videos
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS videos (
    id                  INTEGER PRIMARY KEY,
    production_id       INTEGER NOT NULL REFERENCES productions(id) ON DELETE CASCADE,
    platform            TEXT NOT NULL DEFAULT 'youtube',
    video_id            TEXT,
    canonical_url       TEXT NOT NULL,
    title               TEXT,
    channel_name        TEXT,
    published_at        TEXT,
    duration_seconds    INTEGER,
    video_kind          TEXT NOT NULL DEFAULT 'unknown'
                          CHECK (video_kind IN ('demo capture','party recording','review',
                                                'interview','trailer','unknown')),
    match_confidence    REAL,
    match_evidence      TEXT,
    verification_status TEXT NOT NULL DEFAULT 'untested'
                          CHECK (verification_status IN ('untested','verified_match','probable_match',
                                                         'needs_review','rejected','unavailable')),
    source_id           TEXT,
    last_checked_at     TEXT,
    check_detail        TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_videos_unique ON videos(production_id, canonical_url);
CREATE INDEX IF NOT EXISTS idx_videos_video_id ON videos(video_id);
CREATE INDEX IF NOT EXISTS idx_videos_status   ON videos(verification_status);

-- ---------------------------------------------------------------------------
-- Provenance: evidence behind every externally sourced claim.
-- entity_id holds the stable key of the entity (e.g. 'pouet:1000' or the
-- numeric row id as a string) -- see docs/data_dictionary.md.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS provenance (
    id                  INTEGER PRIMARY KEY,
    entity_type         TEXT NOT NULL,
    entity_id           TEXT NOT NULL,
    field_name          TEXT NOT NULL,
    source_id           TEXT NOT NULL,
    source_record_id    TEXT,
    source_url          TEXT NOT NULL,
    observed_value_json TEXT NOT NULL,
    observed_at         TEXT NOT NULL,
    confidence          TEXT NOT NULL DEFAULT 'high'
                          CHECK (confidence IN ('high','medium','low')),
    notes               TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_provenance_unique
    ON provenance(entity_type, entity_id, field_name, source_id, source_url);
CREATE INDEX IF NOT EXISTS idx_provenance_entity ON provenance(entity_type, entity_id);

-- ---------------------------------------------------------------------------
-- Fetch jobs: resumability, retries, error reporting
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fetch_jobs (
    id              INTEGER PRIMARY KEY,
    job_type        TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    target_key      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','running','success','failed','skipped','unreachable')),
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    started_at      TEXT,
    finished_at     TEXT,
    next_retry_at   TEXT,
    last_error      TEXT,
    response_status INTEGER,
    response_hash   TEXT,
    detail          TEXT,
    created_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fetch_jobs_unique ON fetch_jobs(job_type, source_id, target_key);
CREATE INDEX IF NOT EXISTS idx_fetch_jobs_status ON fetch_jobs(status);

-- ---------------------------------------------------------------------------
-- Cursors / checkpoints for resumable discovery
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cursors (
    name        TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Scoring inputs (source-observed metrics) kept inside the database so that
-- curation and exports are reproducible from SQLite alone.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS production_metrics (
    production_id     INTEGER PRIMARY KEY REFERENCES productions(id) ON DELETE CASCADE,
    popularity_json   TEXT NOT NULL,
    significance_json TEXT NOT NULL,
    cross_ids_json    TEXT,
    extra_json        TEXT,
    updated_at        TEXT NOT NULL
);
