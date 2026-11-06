"""JSON export — versioned, deterministic, nested.

``exports/demoscene.json`` contains the curated collection (productions whose
selection status is ``selected`` or ``needs_review``) with nested creators,
platforms, releases, videos and provenance. The full candidate population stays
in SQLite and in the CSV exports; ``summary.population`` records the counts so
nothing is hidden.

Determinism: keys are emitted with ``sort_keys=True`` and every list is ordered
by an explicit, stable key. No secrets, cookies or HTTP headers are included.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"
GENERATOR = "demoscene-collector/0.1.0"

INCLUDE_STATUSES = ("selected", "needs_review")


def _json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (int, float, str, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value]
    return str(value)


def _loads(text: str | None, default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


def build_document(conn, *, generated_at: str) -> dict[str, Any]:
    placeholders = ",".join("?" for _ in INCLUDE_STATUSES)
    productions: list[dict[str, Any]] = []

    rows = list(conn.execute(
        f"""
        SELECT * FROM productions
         WHERE selection_status IN ({placeholders})
         ORDER BY COALESCE(selection_rank, 1000000), canonical_key
        """,
        INCLUDE_STATUSES,
    ))
    for row in rows:
        pid = int(row["id"])
        key = row["canonical_key"]
        creators = [
            {"name": r["canonical_name"], "role": r["role"]}
            for r in conn.execute(
                "SELECT c.canonical_name, pc.role FROM production_creators pc "
                "JOIN creators c ON c.id = pc.creator_id WHERE pc.production_id = ? "
                "ORDER BY pc.role, c.canonical_name",
                (pid,),
            )
        ]
        platforms = [
            {"name": r["canonical_name"], "family": r["platform_family"]}
            for r in conn.execute(
                "SELECT p.canonical_name, p.platform_family FROM production_platforms pp "
                "JOIN platforms p ON p.id = pp.platform_id WHERE pp.production_id = ? "
                "ORDER BY p.canonical_name",
                (pid,),
            )
        ]
        releases = [
            {
                "id": r["id"], "source": r["source_id"], "artifact_type": r["artifact_type"],
                "download_url": r["download_url"], "landing_page_url": r["landing_page_url"],
                "url_kind": r["url_kind"], "file_name": r["file_name"],
                "file_size_bytes": r["file_size_bytes"],
                "verification_status": r["verification_status"],
                "last_checked_at": r["last_checked_at"], "check_detail": r["check_detail"],
            }
            for r in conn.execute("SELECT * FROM releases WHERE production_id = ? ORDER BY id", (pid,))
        ]
        videos = [
            {
                "id": r["id"], "platform": r["platform"], "video_id": r["video_id"],
                "url": r["canonical_url"], "title": r["title"], "channel": r["channel_name"],
                "published_at": r["published_at"], "duration_seconds": r["duration_seconds"],
                "video_kind": r["video_kind"], "match_confidence": r["match_confidence"],
                "match_evidence": r["match_evidence"],
                "verification_status": r["verification_status"],
                "last_checked_at": r["last_checked_at"],
            }
            for r in conn.execute("SELECT * FROM videos WHERE production_id = ? ORDER BY id", (pid,))
        ]
        provenance = [
            {
                "field": r["field_name"], "source": r["source_id"],
                "source_record_id": r["source_record_id"], "source_url": r["source_url"],
                "value": _json(_loads(r["observed_value_json"], None)),
                "observed_at": r["observed_at"], "confidence": r["confidence"],
                "notes": r["notes"],
            }
            for r in conn.execute(
                "SELECT * FROM provenance WHERE entity_type = 'production' AND entity_id = ? "
                "ORDER BY field_name, source_id, source_url",
                (key,),
            )
        ]
        metrics = _loads(None, None)
        metrics_row = conn.execute(
            "SELECT popularity_json, significance_json, cross_ids_json FROM production_metrics "
            "WHERE production_id = ?",
            (pid,),
        ).fetchone()
        productions.append({
            "canonical_key": key,
            "source": key.split(":", 1)[0],
            "source_record_id": key.split(":", 1)[-1],
            "title": row["title"],
            "release_year": row["release_year"],
            "release_date": row["release_date"],
            "production_type": row["production_type"],
            "description": row["description"],
            "scores": {
                "popularity": row["popularity_score"],
                "significance": row["significance_score"],
                "curation": row["curation_score"],
                "rationale": _json(_loads(row["curation_rationale"], None)),
            },
            "selection": {
                "status": row["selection_status"],
                "rank": row["selection_rank"],
            },
            "creators": creators,
            "platforms": platforms,
            "releases": releases,
            "videos": videos,
            "cross_ids": _json(_loads(metrics_row["cross_ids_json"], {})) if metrics_row else {},
            "provenance": provenance,
        })

    counts = {
        "productions": _count(conn, "productions"),
        "creators": _count(conn, "creators"),
        "platforms": _count(conn, "platforms"),
        "releases": _count(conn, "releases"),
        "videos": _count(conn, "videos"),
        "provenance": _count(conn, "provenance"),
        "selected": _status_count(conn, "selected"),
        "needs_review": _status_count(conn, "needs_review"),
        "rejected": _status_count(conn, "rejected"),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "generator": GENERATOR,
        "generated_at": generated_at,
        "selection_filter": list(INCLUDE_STATUSES),
        "summary": {
            "counts": counts,
            "population": counts["productions"],
            "note": (
                "The complete candidate population lives in SQLite and in the CSV exports; "
                "this file contains the curated collection plus the review queue."
            ),
        },
        "productions": productions,
    }


def _count(conn, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])  # noqa: S608


def _status_count(conn, status: str) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) AS n FROM productions WHERE selection_status = ?", (status,)
    ).fetchone()["n"])


def export_json(conn, exports_dir: Path, *, generated_at: str) -> dict[str, Any]:
    document = build_document(conn, generated_at=generated_at)
    path = Path(exports_dir) / "demoscene.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
    return {"path": str(path), "productions": len(document["productions"]),
            "counts": document["summary"]["counts"]}
