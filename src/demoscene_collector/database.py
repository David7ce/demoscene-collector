"""SQLite persistence layer.

Design rules:

* one connection = one pipeline run; WAL mode, foreign keys ON;
* every mutation happens inside an explicit transaction;
* uniqueness constraints make repeated runs idempotent (``INSERT OR IGNORE`` /
  ``ON CONFLICT DO UPDATE``);
* UTC timestamps are produced by :func:`utcnow`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Iterable, Iterator

from .models import CreatorRef, ProductionCandidate, Provenance, ReleaseCandidate, VideoCandidate

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

CHECK_STATUSES = ("untested", "verified", "landing_ok", "broken", "inaccessible", "redirected", "blocked", "untested_protocol")
VIDEO_STATUSES = ("untested", "verified_match", "probable_match", "needs_review", "rejected", "unavailable")
SELECTION_STATUSES = ("candidate", "selected", "rejected", "needs_review")


def utcnow() -> str:
    """UTC timestamp with milliseconds, always ending in ``Z``."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the canonical database."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def schema_sql() -> str:
    return resources.files("demoscene_collector").joinpath("schema.sql").read_text(encoding="utf-8")


def current_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    except sqlite3.OperationalError:
        return 0  # fresh database, table not created yet
    return int(row["v"]) if row and row["v"] is not None else 0


def init_db(conn: sqlite3.Connection, *, target_version: int = SCHEMA_VERSION) -> int:
    """Apply the schema (idempotent). Returns the resulting schema version."""
    version = current_version(conn)
    if version >= target_version:
        return version
    conn.executescript(schema_sql())
    # schema.sql is written to be re-runnable; record the version explicitly.
    now = utcnow()
    conn.execute(
        "INSERT INTO schema_version(version, applied_at) VALUES (?, ?) "
        "ON CONFLICT(version) DO UPDATE SET applied_at = excluded.applied_at",
        (target_version, now),
    )
    conn.commit()
    log.info("database initialised at schema version %s", target_version)
    return target_version


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def upsert_source(conn: sqlite3.Connection, *, name: str, base_url: str, source_type: str,
                  access_method: str, terms_url: str | None = None,
                  api_documentation_url: str | None = None, notes: str | None = None,
                  status: str = "unknown") -> int:
    now = utcnow()
    conn.execute(
        """
        INSERT INTO sources(name, base_url, source_type, terms_url, api_documentation_url,
                            access_method, status, notes)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(name) DO UPDATE SET
            base_url = excluded.base_url,
            source_type = excluded.source_type,
            terms_url = excluded.terms_url,
            api_documentation_url = excluded.api_documentation_url,
            access_method = excluded.access_method,
            notes = excluded.notes
        """,
        (name, base_url, source_type, terms_url, api_documentation_url, access_method, status, notes),
    )
    row = conn.execute("SELECT id FROM sources WHERE name = ?", (name,)).fetchone()
    return int(row["id"])


def mark_source(conn: sqlite3.Connection, name: str, *, status: str,
                ok: bool, notes: str | None = None) -> None:
    now = utcnow()
    column = "last_success_at" if ok else "last_failure_at"
    conn.execute(
        f"UPDATE sources SET status = ?, {column} = ? , notes = COALESCE(?, notes) WHERE name = ?",
        (status, now, notes, name),
    )


# ---------------------------------------------------------------------------
# Productions
# ---------------------------------------------------------------------------

def upsert_production(conn: sqlite3.Connection, cand: ProductionCandidate) -> int:
    """Insert or update a production identified by ``canonical_key`` (idempotent)."""
    now = utcnow()
    conn.execute(
        """
        INSERT INTO productions(canonical_key, title, normalized_title, release_year, release_date,
                               production_type, description, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(canonical_key) DO UPDATE SET
            title = excluded.title,
            normalized_title = excluded.normalized_title,
            release_year = excluded.release_year,
            release_date = excluded.release_date,
            production_type = excluded.production_type,
            description = COALESCE(excluded.description, productions.description),
            updated_at = excluded.updated_at
        """,
        (cand.canonical_key, cand.display_title, cand.normalized_title, cand.year, cand.iso_date,
         cand.production_type or "other", cand.description, now, now),
    )
    row = conn.execute("SELECT id FROM productions WHERE canonical_key = ?", (cand.canonical_key,)).fetchone()
    assert row is not None
    production_id = int(row["id"])

    # preserve alternate titles / source-specific names
    for alias in {cand.display_title} | set(cand.raw.get("aliases", [])):
        if alias and alias != cand.display_title:
            add_alias(conn, production_id, alias, source_id=cand.source_id,
                      source_record_id=cand.source_record_id)
    return production_id


def add_alias(conn: sqlite3.Connection, production_id: int, alias: str, *,
              source_id: str | None = None, source_record_id: str | None = None) -> None:
    from .normalization import normalize_alias, normalize_whitespace

    text = normalize_whitespace(alias)
    if not text:
        return
    conn.execute(
        """
        INSERT INTO aliases(production_id, alias, normalized_alias, source_id, source_record_id, created_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT DO NOTHING
        """,
        (production_id, text, normalize_alias(text), source_id, source_record_id, utcnow()),
    )


def set_production_scores(conn: sqlite3.Connection, canonical_key: str, *,
                          curation_score: float | None,
                          popularity_score: float | None,
                          significance_score: float | None,
                          rationale: str,
                          selection_status: str,
                          selection_rank: int | None = None) -> None:
    if selection_status not in SELECTION_STATUSES:
        raise ValueError(f"invalid selection_status: {selection_status}")
    conn.execute(
        """
        UPDATE productions
           SET curation_score = ?, popularity_score = ?, significance_score = ?,
               curation_rationale = ?, selection_status = ?, selection_rank = ?, updated_at = ?
         WHERE canonical_key = ?
        """,
        (curation_score, popularity_score, significance_score, rationale, selection_status,
         selection_rank, utcnow(), canonical_key),
    )


# ---------------------------------------------------------------------------
# Creators / platforms
# ---------------------------------------------------------------------------

def ensure_creator(conn: sqlite3.Connection, ref: CreatorRef) -> int:
    from .normalization import normalize_name, normalize_whitespace

    display = normalize_whitespace(ref.name)
    normalized = normalize_name(display)
    if not normalized:
        raise ValueError("creator without a usable name")
    row = conn.execute("SELECT id, canonical_name FROM creators WHERE normalized_name = ?", (normalized,)).fetchone()
    if row:
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO creators(canonical_name, normalized_name, created_at) VALUES (?,?,?)",
        (display, normalized, utcnow()),
    )
    return int(cur.lastrowid)


def link_creator(conn: sqlite3.Connection, production_id: int, creator_id: int, *,
                 role: str = "unknown", source_id: str | None = None) -> None:
    conn.execute(
        """
        INSERT INTO production_creators(production_id, creator_id, role, source_id)
        VALUES (?,?,?,?)
        ON CONFLICT DO NOTHING
        """,
        (production_id, creator_id, role or "unknown", source_id),
    )


def ensure_platform(conn: sqlite3.Connection, canonical_name: str, family: str) -> int:
    row = conn.execute("SELECT id FROM platforms WHERE canonical_name = ?", (canonical_name,)).fetchone()
    if row:
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO platforms(canonical_name, platform_family) VALUES (?,?)",
        (canonical_name, family),
    )
    return int(cur.lastrowid)


def link_platform(conn: sqlite3.Connection, production_id: int, platform_id: int, *,
                  source_id: str | None = None, evidence_url: str | None = None) -> None:
    conn.execute(
        """
        INSERT INTO production_platforms(production_id, platform_id, source_id, evidence_url)
        VALUES (?,?,?,?)
        ON CONFLICT DO NOTHING
        """,
        (production_id, platform_id, source_id, evidence_url),
    )


# ---------------------------------------------------------------------------
# Releases / videos
# ---------------------------------------------------------------------------

def upsert_release(conn: sqlite3.Connection, production_id: int, rel: ReleaseCandidate,
                   *, platform_id: int | None = None) -> int:
    now = utcnow()
    conn.execute(
        """
        INSERT INTO releases(production_id, source_id, release_title, release_version, platform_id,
                             artifact_type, download_url, landing_page_url, url_kind, file_name,
                             file_size_bytes, requirements, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(production_id, source_id, download_url) DO UPDATE SET
            release_title = COALESCE(excluded.release_title, releases.release_title),
            release_version = COALESCE(excluded.release_version, releases.release_version),
            platform_id = COALESCE(excluded.platform_id, releases.platform_id),
            artifact_type = excluded.artifact_type,
            landing_page_url = COALESCE(excluded.landing_page_url, releases.landing_page_url),
            url_kind = excluded.url_kind,
            file_name = COALESCE(excluded.file_name, releases.file_name),
            file_size_bytes = COALESCE(excluded.file_size_bytes, releases.file_size_bytes),
            requirements = COALESCE(excluded.requirements, releases.requirements),
            updated_at = excluded.updated_at
        """,
        (production_id, rel.source_id, rel.release_title, rel.release_version, platform_id,
         rel.artifact_type, rel.url,
         rel.url if rel.kind == "landing_page" else None,
         rel.kind, rel.file_name, rel.file_size_bytes, rel.requirements, now, now),
    )
    row = conn.execute(
        "SELECT id FROM releases WHERE production_id = ? AND source_id = ? AND download_url = ?",
        (production_id, rel.source_id, rel.url),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def upsert_video(conn: sqlite3.Connection, production_id: int, vid: VideoCandidate) -> int:
    now = utcnow()
    conn.execute(
        """
        INSERT INTO videos(production_id, platform, video_id, canonical_url, title, channel_name,
                           published_at, duration_seconds, video_kind, match_confidence,
                           match_evidence, verification_status, source_id, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(production_id, canonical_url) DO UPDATE SET
            video_id = COALESCE(excluded.video_id, videos.video_id),
            title = COALESCE(excluded.title, videos.title),
            channel_name = COALESCE(excluded.channel_name, videos.channel_name),
            published_at = COALESCE(excluded.published_at, videos.published_at),
            duration_seconds = COALESCE(excluded.duration_seconds, videos.duration_seconds),
            video_kind = CASE WHEN videos.video_kind = 'unknown' THEN excluded.video_kind ELSE videos.video_kind END,
            match_confidence = COALESCE(excluded.match_confidence, videos.match_confidence),
            match_evidence = COALESCE(excluded.match_evidence, videos.match_evidence),
            verification_status = CASE
                WHEN videos.verification_status = 'untested' THEN excluded.verification_status
                ELSE videos.verification_status END,
            source_id = COALESCE(videos.source_id, excluded.source_id),
            updated_at = excluded.updated_at
        """,
        (production_id, vid.platform, vid.video_id, vid.canonical_url, vid.title, vid.channel_name,
         vid.published_at, vid.duration_seconds, vid.video_kind, vid.match_confidence,
         vid.match_evidence, vid.verification_status, vid.source_id, now, now),
    )
    row = conn.execute(
        "SELECT id FROM videos WHERE production_id = ? AND canonical_url = ?",
        (production_id, vid.canonical_url),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def update_video_check(conn: sqlite3.Connection, video_id: int, *, status: str,
                       evidence: str, confidence: float | None = None,
                       title: str | None = None, channel: str | None = None,
                       detail: str | None = None) -> None:
    if status not in VIDEO_STATUSES:
        raise ValueError(f"invalid video status: {status}")
    conn.execute(
        """
        UPDATE videos
           SET verification_status = ?, match_evidence = ?, match_confidence = COALESCE(?, match_confidence),
               title = COALESCE(?, title), channel_name = COALESCE(?, channel_name),
               last_checked_at = ?, check_detail = ?, updated_at = ?
         WHERE id = ?
        """,
        (status, evidence, confidence, title, channel, utcnow(), detail, utcnow(), video_id),
    )


def update_release_check(conn: sqlite3.Connection, release_id: int, *, status: str,
                         detail: str | None = None, url_kind: str | None = None,
                         landing_page_url: str | None = None) -> None:
    if status not in CHECK_STATUSES:
        raise ValueError(f"invalid release status: {status}")
    conn.execute(
        """
        UPDATE releases
           SET verification_status = ?, check_detail = ?, last_checked_at = ?,
               url_kind = COALESCE(?, url_kind),
               landing_page_url = COALESCE(?, landing_page_url),
               updated_at = ?
         WHERE id = ?
        """,
        (status, detail, utcnow(), url_kind, landing_page_url, utcnow(), release_id),
    )


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

def add_provenance(conn: sqlite3.Connection, prov: Provenance) -> None:
    conn.execute(
        """
        INSERT INTO provenance(entity_type, entity_id, field_name, source_id, source_record_id,
                               source_url, observed_value_json, observed_at, confidence, notes)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT DO NOTHING
        """,
        (prov.entity_type, prov.entity_id, prov.field_name, prov.source_id, prov.source_record_id,
         prov.source_url, prov.observed_json(), utcnow(), prov.confidence, prov.notes),
    )


def add_provenance_batch(conn: sqlite3.Connection, records: Iterable[Provenance]) -> int:
    count = 0
    for prov in records:
        before = conn.total_changes
        add_provenance(conn, prov)
        if conn.total_changes > before:
            count += 1
    return count


# ---------------------------------------------------------------------------
# Fetch jobs / cursors
# ---------------------------------------------------------------------------

def record_job(conn: sqlite3.Connection, *, job_type: str, source_id: str, target_key: str,
               status: str, attempt_increment: bool = False, error: str | None = None,
               response_status: int | None = None, response_body: bytes | str | None = None,
               response_hash: str | None = None, detail: str | None = None,
               next_retry_at: str | None = None) -> int:
    now = utcnow()
    body: bytes | None = None
    if isinstance(response_body, str):
        body = response_body.encode("utf-8", "replace")
    else:
        body = response_body
    if body:
        response_hash = hashlib.sha256(body).hexdigest()
    started = now if status in ("running", "success", "failed") else None
    finished = now if status in ("success", "failed", "skipped", "unreachable") else None
    conn.execute(
        """
        INSERT INTO fetch_jobs(job_type, source_id, target_key, status, attempt_count,
                               started_at, finished_at, next_retry_at, last_error,
                               response_status, response_hash, detail, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(job_type, source_id, target_key) DO UPDATE SET
            status = excluded.status,
            attempt_count = fetch_jobs.attempt_count + ?,
            started_at = COALESCE(excluded.started_at, fetch_jobs.started_at),
            finished_at = COALESCE(excluded.finished_at, fetch_jobs.finished_at),
            next_retry_at = excluded.next_retry_at,
            last_error = excluded.last_error,
            response_status = COALESCE(excluded.response_status, fetch_jobs.response_status),
            response_hash = COALESCE(excluded.response_hash, fetch_jobs.response_hash),
            detail = COALESCE(excluded.detail, fetch_jobs.detail)
        """,
        (job_type, source_id, target_key, status, 1 if attempt_increment else 0,
         started, finished, next_retry_at, error, response_status, response_hash, detail, now,
         1 if attempt_increment else 0),
    )
    row = conn.execute(
        "SELECT id FROM fetch_jobs WHERE job_type = ? AND source_id = ? AND target_key = ?",
        (job_type, source_id, target_key),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def job_rows(conn: sqlite3.Connection, *, status: str | None = None,
             job_type: str | None = None) -> list[sqlite3.Row]:
    query = "SELECT * FROM fetch_jobs WHERE 1=1"
    params: list[Any] = []
    if status:
        query += " AND status = ?"
        params.append(status)
    if job_type:
        query += " AND job_type = ?"
        params.append(job_type)
    query += " ORDER BY id"
    return list(conn.execute(query, params))


def set_cursor(conn: sqlite3.Connection, name: str, value: str) -> None:
    conn.execute(
        "INSERT INTO cursors(name, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (name, value, utcnow()),
    )


def get_cursor(conn: sqlite3.Connection, name: str) -> str | None:
    row = conn.execute("SELECT value FROM cursors WHERE name = ?", (name,)).fetchone()
    return str(row["value"]) if row else None


def record_merge(conn: sqlite3.Connection, *, survivor_key: str, merged_key: str, method: str,
                 confidence: float, evidence: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO merge_log(survivor_key, merged_key, method, confidence, evidence_json, merged_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(survivor_key, merged_key) DO UPDATE SET
            method = excluded.method, confidence = excluded.confidence,
            evidence_json = excluded.evidence_json, merged_at = excluded.merged_at
        """,
        (survivor_key, merged_key, method, confidence,
         json.dumps(evidence, ensure_ascii=False, sort_keys=True), utcnow()),
    )


# ---------------------------------------------------------------------------
# Convenience queries (used by exports/reports)
# ---------------------------------------------------------------------------

def upsert_metrics(conn: sqlite3.Connection, production_id: int, *,
                   popularity: dict[str, Any], significance: dict[str, Any],
                   cross_ids: dict[str, Any] | None = None,
                   extra: dict[str, Any] | None = None) -> None:
    conn.execute(
        """
        INSERT INTO production_metrics(production_id, popularity_json, significance_json,
                                       cross_ids_json, extra_json, updated_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(production_id) DO UPDATE SET
            popularity_json = excluded.popularity_json,
            significance_json = excluded.significance_json,
            cross_ids_json = excluded.cross_ids_json,
            extra_json = excluded.extra_json,
            updated_at = excluded.updated_at
        """,
        (production_id,
         json.dumps(popularity, ensure_ascii=False, sort_keys=True, default=str),
         json.dumps(significance, ensure_ascii=False, sort_keys=True, default=str),
         json.dumps(cross_ids or {}, ensure_ascii=False, sort_keys=True),
         json.dumps(extra or {}, ensure_ascii=False, sort_keys=True, default=str),
         utcnow()),
    )


def load_metrics(conn: sqlite3.Connection, production_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT popularity_json, significance_json, cross_ids_json, extra_json "
        "FROM production_metrics WHERE production_id = ?",
        (production_id,),
    ).fetchone()
    if not row:
        return {"popularity": {}, "significance": {}, "cross_ids": {}, "extra": {}}
    return {
        "popularity": json.loads(row["popularity_json"] or "{}"),
        "significance": json.loads(row["significance_json"] or "{}"),
        "cross_ids": json.loads(row["cross_ids_json"] or "{}"),
        "extra": json.loads(row["extra_json"] or "{}"),
    }


def load_candidate(conn: sqlite3.Connection, row: sqlite3.Row) -> "Any":
    """Rebuild a :class:`ProductionCandidate` from the database (for curation)."""
    from .models import CreatorRef, ProductionCandidate, ReleaseCandidate, VideoCandidate

    production_id = int(row["id"])
    metrics = load_metrics(conn, production_id)

    groups = [
        CreatorRef(name=r["canonical_name"], role=r["role"])
        for r in conn.execute(
            "SELECT c.canonical_name, pc.role FROM production_creators pc "
            "JOIN creators c ON c.id = pc.creator_id "
            "WHERE pc.production_id = ? AND pc.role = 'group' ORDER BY c.canonical_name",
            (production_id,),
        )
    ]
    credits = [
        CreatorRef(name=r["canonical_name"], role=r["role"])
        for r in conn.execute(
            "SELECT c.canonical_name, pc.role FROM production_creators pc "
            "JOIN creators c ON c.id = pc.creator_id "
            "WHERE pc.production_id = ? AND pc.role <> 'group' ORDER BY c.canonical_name",
            (production_id,),
        )
    ]
    platforms = [
        r["canonical_name"]
        for r in conn.execute(
            "SELECT p.canonical_name FROM production_platforms pp "
            "JOIN platforms p ON p.id = pp.platform_id "
            "WHERE pp.production_id = ? ORDER BY p.canonical_name",
            (production_id,),
        )
    ]
    releases = [
        ReleaseCandidate(
            url=r["download_url"], source_id=r["source_id"],
            artifact_type=r["artifact_type"], kind=r["url_kind"],
            link_label=r["release_title"], file_name=r["file_name"],
            file_size_bytes=r["file_size_bytes"],
        )
        for r in conn.execute(
            "SELECT * FROM releases WHERE production_id = ? ORDER BY id", (production_id,)
        )
    ]
    videos: list[Any] = []
    for r in conn.execute("SELECT * FROM videos WHERE production_id = ? ORDER BY id",
                          (production_id,)):
        vid = VideoCandidate.__new__(VideoCandidate)  # bypass validation: rows already canonical
        vid.url = r["canonical_url"]
        vid.source_id = r["source_id"] or "unknown"
        vid.canonical_url = r["canonical_url"]
        vid.platform = r["platform"]
        vid.video_id = r["video_id"]
        vid.title = r["title"]
        vid.channel_name = r["channel_name"]
        vid.published_at = r["published_at"]
        vid.duration_seconds = r["duration_seconds"]
        vid.video_kind = r["video_kind"]
        vid.match_confidence = r["match_confidence"]
        vid.match_evidence = r["match_evidence"] or ""
        vid.verification_status = r["verification_status"]
        videos.append(vid)

    source_url = metrics.get("extra", {}).get("source_url") or ""
    _, _, record_id = row["canonical_key"].partition(":")
    cand = ProductionCandidate(
        source_id=row["canonical_key"].split(":", 1)[0],
        source_record_id=record_id or row["canonical_key"],
        title=row["title"],
        source_url=source_url,
        production_type=row["production_type"],
        release_date=row["release_date"],
        description=row["description"],
        groups=groups,
        credits=credits,
        platforms=platforms,
        releases=releases,
        videos=videos,
        cross_ids=metrics.get("cross_ids", {}),
        popularity=metrics.get("popularity", {}),
        significance=metrics.get("significance", {}),
        raw=metrics.get("extra", {}),
    )
    # preserve DB-side state that __post_init__ would otherwise recompute
    cand.year = row["release_year"]
    cand.iso_date = row["release_date"]
    return cand


def load_all_candidates(conn: sqlite3.Connection, *, limit: int | None = None) -> list[Any]:
    query = "SELECT * FROM productions ORDER BY canonical_key"
    if limit:
        query += f" LIMIT {int(limit)}"
    return [load_candidate(conn, row) for row in conn.execute(query)]


def table_count(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()  # noqa: S608 - fixed table names
    return int(row["n"])


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = [
        "productions", "aliases", "creators", "production_creators", "platforms",
        "production_platforms", "releases", "videos", "provenance", "fetch_jobs", "merge_log",
        "production_metrics",
    ]
    return {t: table_count(conn, t) for t in tables}


def iter_productions(conn: sqlite3.Connection, *, status: str | None = None) -> Iterator[sqlite3.Row]:
    query = "SELECT * FROM productions"
    params: tuple[Any, ...] = ()
    if status:
        query += " WHERE selection_status = ?"
        params = (status,)
    query += " ORDER BY COALESCE(selection_rank, 1000000), canonical_key"
    yield from conn.execute(query, params)


def transaction(conn: sqlite3.Connection):
    """Context manager wrapping a commit/rollback transaction."""
    class _Tx:
        def __enter__(self_inner):
            return conn

        def __exit__(self_inner, exc_type, exc, tb):
            if exc_type is None:
                conn.commit()
            else:
                conn.rollback()
            return False

    return _Tx()
