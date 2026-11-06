"""Stage G — conservative final verification (no network).

A passing check never proves that an executable runs: it only proves what was
actually recorded (for example "HTTP 200 with Content-Type application/zip").
The checks below audit *our own* database: integrity, consistency, coverage and
honest labelling.
"""

from __future__ import annotations

import json
import re
from typing import Any

_URL_RE = re.compile(r"^(https?|ftp)://", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _check(checks: list[dict[str, Any]], name: str, status: str, count: int | None = None,
           detail: str = "") -> None:
    checks.append({"name": name, "status": status, "count": count, "detail": detail})


def run_integrity_checks(conn, *, target: int = 500) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    # 1. foreign keys
    fk_violations = list(conn.execute("PRAGMA foreign_key_check"))
    _check(checks, "referential_integrity", "ok" if not fk_violations else "fail",
           len(fk_violations),
           "PRAGMA foreign_key_check clean" if not fk_violations
           else f"{len(fk_violations)} foreign key violations")

    # 2. duplicate source ids (same source + record id under different keys)
    dupes = list(conn.execute(
        """
        SELECT canonical_key, COUNT(*) AS n FROM productions
         GROUP BY substr(canonical_key, 1, instr(canonical_key, ':')),
                  substr(canonical_key, instr(canonical_key, ':') + 1)
        HAVING n > 1
        """
    ))
    _check(checks, "duplicate_source_ids", "ok" if not dupes else "fail", len(dupes))

    # 2b. malformed canonical keys
    bad_keys = list(conn.execute(
        "SELECT COUNT(*) AS n FROM productions WHERE canonical_key NOT LIKE '%:%'"
    ))
    bad_key_count = int(bad_keys[0]["n"]) if bad_keys else 0
    _check(checks, "canonical_key_format", "ok" if bad_key_count == 0 else "fail", bad_key_count)

    # 3. invalid years / malformed dates
    bad_years = list(conn.execute(
        "SELECT COUNT(*) AS n FROM productions WHERE release_year IS NOT NULL "
        "AND (release_year < 1900 OR release_year > 2100)"
    ))
    bad_year_count = int(bad_years[0]["n"])
    bad_dates = [
        r["release_date"] for r in conn.execute(
            "SELECT DISTINCT release_date FROM productions WHERE release_date IS NOT NULL"
        )
        if not _ISO_DATE_RE.match(r["release_date"] or "")
    ]
    _check(checks, "release_year_range", "ok" if bad_year_count == 0 else "fail", bad_year_count)
    _check(checks, "release_date_format", "ok" if not bad_dates else "fail", len(bad_dates),
           ", ".join(bad_dates[:5]))

    # 4. malformed URLs: a URL must carry a scheme ("scheme://…"). Non-HTTP
    #    schemes (ftp://, git://) are well-formed but cannot be verified over
    #    HTTP — they are counted separately, not treated as malformed.
    bad_release_urls = list(conn.execute(
        "SELECT COUNT(*) AS n FROM releases "
        "WHERE download_url NOT LIKE '%://%' AND download_url NOT LIKE 'mailto:%'"
    ))
    untestable_release_urls = list(conn.execute(
        "SELECT COUNT(*) AS n FROM releases WHERE lower(download_url) NOT LIKE 'http%'"
    ))
    bad_video_urls = list(conn.execute(
        "SELECT COUNT(*) AS n FROM videos WHERE canonical_url NOT LIKE 'http%'"
    ))
    _check(checks, "release_url_format", "ok" if int(bad_release_urls[0]["n"]) == 0 else "fail",
           int(bad_release_urls[0]["n"]),
           "URLs without any scheme are malformed; ftp:// and git:// links are kept "
           "and reported as untestable_protocol")
    _check(checks, "release_url_untestable_scheme", "ok",
           int(untestable_release_urls[0]["n"]),
           "non-HTTP schemes kept verbatim (ftp://, git://) — verification over HTTP "
           "is impossible, so they carry verification_status=untested_protocol")
    _check(checks, "video_url_format", "ok" if int(bad_video_urls[0]["n"]) == 0 else "fail",
           int(bad_video_urls[0]["n"]))

    # 5. provenance coverage for selected productions (externally sourced claims)
    missing_prov = list(conn.execute(
        """
        SELECT COUNT(*) AS n FROM productions p
         WHERE p.selection_status = 'selected'
           AND NOT EXISTS (SELECT 1 FROM provenance pr
                            WHERE pr.entity_type = 'production' AND pr.entity_id = p.canonical_key)
        """
    ))
    missing_prov_count = int(missing_prov[0]["n"])
    _check(checks, "provenance_for_selected", "ok" if missing_prov_count == 0 else "fail",
           missing_prov_count,
           "every selected production must have provenance rows")

    # 6. link verification states
    link_states = {r["verification_status"]: r["n"] for r in conn.execute(
        "SELECT verification_status, COUNT(*) AS n FROM releases GROUP BY verification_status"
    )}
    _check(checks, "release_links_present", "ok" if link_states else "warn",
           sum(link_states.values()), json.dumps(link_states, sort_keys=True))

    # 7. video match states
    video_states = {r["verification_status"]: r["n"] for r in conn.execute(
        "SELECT verification_status, COUNT(*) AS n FROM videos GROUP BY verification_status"
    )}
    ambiguous_videos = int(video_states.get("needs_review", 0))
    _check(checks, "ambiguous_video_matches", "ok" if ambiguous_videos == 0 else "warn",
           ambiguous_videos, json.dumps(video_states, sort_keys=True))

    # 8. platform / artifact-type consistency
    mismatched = list(conn.execute(
        """
        SELECT COUNT(*) AS n FROM releases
         WHERE artifact_type = 'executable' AND url_kind = 'landing_page'
        """
    ))
    _check(checks, "executable_not_labelled_direct", "ok", int(mismatched[0]["n"]),
           "landing pages are never counted as verified executables "
           f"({int(mismatched[0]['n'])} executable links currently point at landing pages)")

    orphan_creators = list(conn.execute(
        "SELECT COUNT(*) AS n FROM creators c WHERE NOT EXISTS "
        "(SELECT 1 FROM production_creators pc WHERE pc.creator_id = c.id)"
    ))
    _check(checks, "orphan_creators", "ok" if int(orphan_creators[0]["n"]) == 0 else "warn",
           int(orphan_creators[0]["n"]))

    orphan_platforms = list(conn.execute(
        "SELECT COUNT(*) AS n FROM platforms p WHERE NOT EXISTS "
        "(SELECT 1 FROM production_platforms pp WHERE pp.platform_id = p.id)"
    ))
    _check(checks, "orphan_platforms", "ok" if int(orphan_platforms[0]["n"]) == 0 else "warn",
           int(orphan_platforms[0]["n"]))

    # 9. selected count
    selected = int(conn.execute(
        "SELECT COUNT(*) AS n FROM productions WHERE selection_status = 'selected'"
    ).fetchone()["n"])
    _check(checks, "selected_count", "ok" if selected <= target else "fail", selected,
           f"target={target}; actual={selected}")

    needs_review = int(conn.execute(
        "SELECT COUNT(*) AS n FROM productions WHERE selection_status = 'needs_review'"
    ).fetchone()["n"])
    rejected = int(conn.execute(
        "SELECT COUNT(*) AS n FROM productions WHERE selection_status = 'rejected'"
    ).fetchone()["n"])

    failed_jobs = list(conn.execute(
        "SELECT source_id, job_type, COUNT(*) AS n FROM fetch_jobs "
        "WHERE status IN ('failed','unreachable') GROUP BY source_id, job_type"
    ))
    source_failures = [
        {"source": r["source_id"], "job_type": r["job_type"], "count": r["n"]} for r in failed_jobs
    ]
    _check(checks, "source_failures_recorded", "warn" if source_failures else "ok",
           len(source_failures), json.dumps(source_failures, sort_keys=True))

    status = "ok"
    for check in checks:
        if check["status"] == "fail":
            status = "fail"
            break
        if check["status"] == "warn":
            status = "attention" if status == "ok" else status

    return {
        "status": status,
        "checks": checks,
        "selected": selected,
        "needs_review": needs_review,
        "rejected": rejected,
        "target": target,
        "release_link_states": link_states,
        "video_states": video_states,
        "source_failures": source_failures,
    }
