"""CSV exports (Stage: export).

Files are written to ``exports/`` as UTF-8 **with BOM** (``utf-8-sig``) so that
Excel and LibreOffice detect the encoding. ``csv`` handles quoting, embedded
commas, quotes and newlines. Row order is deterministic (ordered by id/key).
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Iterable, Sequence

EXPORT_TABLES = {
    "productions": (
        "id", "canonical_key", "title", "release_year", "release_date", "production_type",
        "description", "popularity_score", "significance_score", "curation_score",
        "selection_status", "selection_rank", "curation_rationale", "created_at", "updated_at",
    ),
    "creators": ("id", "canonical_name", "normalized_name"),
    "production_creators": ("production_id", "canonical_key", "creator_id", "creator_name",
                            "role", "source_id"),
    "platforms": ("id", "canonical_name", "platform_family"),
    "releases": (
        "id", "production_id", "canonical_key", "source_id", "release_title", "artifact_type",
        "download_url", "landing_page_url", "url_kind", "file_name", "file_size_bytes",
        "verification_status", "last_checked_at", "check_detail",
    ),
    "videos": (
        "id", "production_id", "canonical_key", "platform", "video_id", "canonical_url", "title",
        "channel_name", "published_at", "duration_seconds", "video_kind", "match_confidence",
        "match_evidence", "verification_status", "source_id", "last_checked_at",
    ),
    "provenance": (
        "id", "entity_type", "entity_id", "field_name", "source_id", "source_record_id",
        "source_url", "observed_value_json", "observed_at", "confidence", "notes",
    ),
}

QUERIES: dict[str, str] = {
    "productions": """
        SELECT id, canonical_key, title, release_year, release_date, production_type, description,
               popularity_score, significance_score, curation_score, selection_status,
               selection_rank, curation_rationale, created_at, updated_at
          FROM productions ORDER BY id
    """,
    "creators": "SELECT id, canonical_name, normalized_name FROM creators ORDER BY id",
    "production_creators": """
        SELECT pc.production_id, p.canonical_key, pc.creator_id, c.canonical_name AS creator_name,
               pc.role, pc.source_id
          FROM production_creators pc
          JOIN productions p ON p.id = pc.production_id
          JOIN creators c ON c.id = pc.creator_id
         ORDER BY pc.production_id, pc.creator_id, pc.role
    """,
    "platforms": "SELECT id, canonical_name, platform_family FROM platforms ORDER BY id",
    "releases": """
        SELECT r.id, r.production_id, p.canonical_key, r.source_id, r.release_title,
               r.artifact_type, r.download_url, r.landing_page_url, r.url_kind, r.file_name,
               r.file_size_bytes, r.verification_status, r.last_checked_at, r.check_detail
          FROM releases r JOIN productions p ON p.id = r.production_id
         ORDER BY r.id
    """,
    "videos": """
        SELECT v.id, v.production_id, p.canonical_key, v.platform, v.video_id, v.canonical_url,
               v.title, v.channel_name, v.published_at, v.duration_seconds, v.video_kind,
               v.match_confidence, v.match_evidence, v.verification_status, v.source_id,
               v.last_checked_at
          FROM videos v JOIN productions p ON p.id = v.production_id
         ORDER BY v.id
    """,
    "provenance": """
        SELECT id, entity_type, entity_id, field_name, source_id, source_record_id, source_url,
               observed_value_json, observed_at, confidence, notes
          FROM provenance ORDER BY id
    """,
}

VERIFICATION_QUERY = """
    SELECT 'production' AS entity_type, canonical_key AS entity_id, selection_status AS status,
           curation_score AS score, curation_rationale AS detail
      FROM productions
     WHERE selection_status IN ('selected', 'needs_review')
    UNION ALL
    SELECT 'release', p.canonical_key || '#' || r.id, r.verification_status, NULL,
           r.download_url || ' — ' || COALESCE(r.check_detail, '')
      FROM releases r JOIN productions p ON p.id = r.production_id
     WHERE p.selection_status IN ('selected', 'needs_review')
    UNION ALL
    SELECT 'video', p.canonical_key || '#' || v.id, v.verification_status, v.match_confidence,
           v.canonical_url || ' — ' || COALESCE(v.match_evidence, '')
      FROM videos v JOIN productions p ON p.id = v.production_id
     WHERE p.selection_status IN ('selected', 'needs_review')
"""


def write_csv(path: Path, columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> int:
    """Write one CSV file (UTF-8 BOM, RFC-4180 quoting). Returns the row count."""
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
        writer.writerow(list(columns))
        for row in rows:
            writer.writerow(["" if value is None else value for value in row])
            count += 1
    return count


def export_csv(conn, exports_dir: Path) -> dict[str, int]:
    results: dict[str, int] = {}
    for name, query in QUERIES.items():
        columns = EXPORT_TABLES[name]
        rows = (tuple(r) for r in conn.execute(query))
        results[name] = write_csv(Path(exports_dir) / f"{name}.csv", columns, rows)
    results["verification_report"] = write_csv(
        Path(exports_dir) / "verification_report.csv",
        ("entity_type", "entity_id", "status", "score", "detail"),
        (tuple(r) for r in conn.execute(VERIFICATION_QUERY)),
    )
    return results
