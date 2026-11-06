"""Reports: execution log and final quality report (``logs/``).

Reports are generated from the database and the recorded job history — they are
never hand-written, so they always reflect what actually ran.
"""

from __future__ import annotations

import json
import platform as py_platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import database as db


def _counts(conn) -> dict[str, int]:
    return db.counts(conn)


def _status_counts(conn) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    out["productions"] = {
        r["selection_status"]: r["n"] for r in conn.execute(
            "SELECT selection_status, COUNT(*) AS n FROM productions GROUP BY selection_status")
    }
    out["videos"] = {
        r["verification_status"]: r["n"] for r in conn.execute(
            "SELECT verification_status, COUNT(*) AS n FROM videos GROUP BY verification_status")
    }
    out["releases"] = {
        r["verification_status"]: r["n"] for r in conn.execute(
            "SELECT verification_status, COUNT(*) AS n FROM releases GROUP BY verification_status")
    }
    return out


def _job_summary(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT source_id, job_type, status, COUNT(*) AS n, MAX(finished_at) AS last_finished
          FROM fetch_jobs
         GROUP BY source_id, job_type, status
         ORDER BY source_id, job_type, status
        """
    )
    return [dict(r) for r in rows]


def _source_summary(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT name, base_url, source_type, access_method, status, last_success_at, "
        "last_failure_at, notes FROM sources ORDER BY name"
    )
    return [dict(r) for r in rows]


def build_report(conn, *, generated_at: str, verification: dict[str, Any] | None = None,
                 extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "generated_at": generated_at,
        "environment": {
            "python": sys.version.split()[0],
            "platform": py_platform.platform(),
        },
        "counts": _counts(conn),
        "statuses": _status_counts(conn),
        "jobs": _job_summary(conn),
        "sources": _source_summary(conn),
        "verification": verification or {},
        "pipeline": extra or {},
    }


def write_report(report: dict[str, Any], path: Path) -> Path:
    c = report["counts"]
    statuses = report["statuses"]
    lines = [
        "# Demoscene collector — execution and quality report",
        "",
        f"Generated: {report['generated_at']}",
        f"Python: {report['environment']['python']} · {report['environment']['platform']}",
        "",
        "## Row counts",
        "",
        "| table | rows |",
        "| --- | --- |",
    ]
    for table, count in c.items():
        lines.append(f"| {table} | {count} |")
    lines += ["", "## Selection", ""]
    for key, value in sorted(statuses["productions"].items()):
        lines.append(f"- productions `{key}`: **{value}**")
    lines += ["", "## Video verification", ""]
    for key, value in sorted(statuses["videos"].items()):
        lines.append(f"- videos `{key}`: **{value}**")
    if not statuses["videos"]:
        lines.append("- (no videos recorded)")
    lines += ["", "## Release link verification", ""]
    for key, value in sorted(statuses["releases"].items()):
        lines.append(f"- releases `{key}`: **{value}**")
    if not statuses["releases"]:
        lines.append("- (no releases recorded)")

    lines += ["", "## Sources", ""]
    for source in report["sources"]:
        lines.append(f"- **{source['name']}** ({source['status']}) — {source['access_method']}"
                     f" · last success {source['last_success_at'] or 'never'}"
                     f" · last failure {source['last_failure_at'] or 'never'}")
        if source.get("notes"):
            lines.append(f"  - {source['notes']}")

    lines += ["", "## Recorded jobs", "",
              "| source | job | status | count | last finished |",
              "| --- | --- | --- | --- | --- |"]
    for job in report["jobs"]:
        lines.append(f"| {job['source_id']} | {job['job_type']} | {job['status']} | {job['n']} "
                     f"| {job['last_finished'] or '-'} |")

    if report.get("verification"):
        lines += ["", "## Integrity checks", "",
                  "| check | status | count | detail |", "| --- | --- | --- | --- |"]
        for check in report["verification"].get("checks", []):
            detail = (check.get("detail") or "").replace("|", "\\|")[:160]
            lines.append(f"| {check['name']} | {check['status']} | {check.get('count')} | {detail} |")

    if report.get("pipeline"):
        lines += ["", "## Pipeline stage results (this run)", "", "```json",
                  json.dumps(report["pipeline"], indent=2, sort_keys=True, default=str), "```"]

    lines += [
        "",
        "## Honest limitations",
        "",
        "- Link verification records HTTP observations only; it never executes binaries.",
        "- `landing_ok` means a page resolved, not that the artifact exists.",
        "- Sources listed as unreachable contributed no data in this run.",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_execution_log(events: list[dict[str, Any]], path: Path) -> Path:
    """Append-style JSON-lines log of the current process run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, sort_keys=True, default=str) + "\n")
    return path
