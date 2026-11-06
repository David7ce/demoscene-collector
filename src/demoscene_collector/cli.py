"""Command line interface.

Commands that perform network requests (explicitly documented in ``--help``):
``audit-sources``, ``discover`` (when the dump must be downloaded), ``enrich``,
``verify-links``, and ``run``. Everything else works offline from SQLite.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import database as db
from .config import ConfigError, load_settings
from .http_client import HttpClientError

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_VERIFICATION_FAILED = 3

NETWORK_COMMANDS = {"audit-sources", "discover", "enrich", "verify-links", "run"}

EPILOG = """
network usage:
  audit-sources   probes every configured source            (network)
  discover        downloads the Pouët dump when missing     (network unless cached/--offline)
  enrich          verifies video links (oEmbed), optional   (network unless --offline)
                  YouTube Data API search when YOUTUBE_API_KEY is set
  verify-links    HEAD/ranged-GET checks of release links   (network unless --offline)
  init-db, curate, verify, export, report                   (offline, SQLite only)

examples:
  python -m demoscene_collector audit-sources
  python -m demoscene_collector discover --limit 500
  python -m demoscene_collector curate --target 500
  python -m demoscene_collector verify-links --limit 200
  python -m demoscene_collector export
  python -m demoscene_collector run --target 500
  python -m demoscene_collector export --offline   # rebuild exports with no network
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="demoscene_collector",
        description="Discover, curate, enrich, verify and export a demoscene collection.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--root", default=None, help="project root (default: cwd)")
    parser.add_argument("--db", default=None, help="override database path")
    parser.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    parser.add_argument("--quiet", "-q", action="store_true", help="warnings only")
    parser.add_argument("--offline", action="store_true",
                        help="never open network connections (uses local dumps and the cache)")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("audit-sources", help="probe configured sources, record reachability")
    p.add_argument("--dry-run", action="store_true", help="list sources without requesting them")

    p = sub.add_parser("init-db", help="create/upgrade the SQLite schema (offline)")

    p = sub.add_parser("discover", help="stage A: read source dumps/APIs (network unless cached)")
    p.add_argument("--limit", type=int, default=None, help="maximum productions to ingest")
    p.add_argument("--source", default=None, help="single source (default: pouet)")
    p.add_argument("--resume", action="store_true",
                   help="continue from the persisted discovery cursor")
    p.add_argument("--dry-run", action="store_true", help="parse but do not write (report counts)")

    p = sub.add_parser("enrich", help="stage E: video verification/search (network)")
    p.add_argument("--limit", type=int, default=None, help="maximum productions to enrich")

    p = sub.add_parser("curate", help="stages C+D: deduplicate, score and select (offline)")
    p.add_argument("--target", type=int, default=None, help="number of productions to select")
    p.add_argument("--limit", type=int, default=None,
                   help="limit the candidate pool (debugging)")

    p = sub.add_parser("verify-links", help="stage F: HEAD checks of release links (network)")
    p.add_argument("--limit", type=int, default=None, help="maximum link checks")
    p.add_argument("--retry-failed", action="store_true",
                   help="also re-check links previously marked broken/inaccessible/blocked")

    p = sub.add_parser("verify", help="stage G: integrity and coverage checks (offline)")
    p.add_argument("--target", type=int, default=None, help="expected selected count")

    p = sub.add_parser("export", help="write CSV, JSON and Markdown exports from SQLite (offline)")
    p.add_argument("--dry-run", action="store_true", help="do not write files, only report")

    p = sub.add_parser("report", help="write the quality report and execution log (offline)")

    p = sub.add_parser("run", help="audit → discover → curate → enrich → verify → export → report")
    p.add_argument("--target", type=int, default=None, help="number of productions to select")
    p.add_argument("--limit", type=int, default=None, help="limit per network stage")
    p.add_argument("--resume", action="store_true", help="resume discovery from the cursor")
    p.add_argument("--retry-failed", action="store_true", help="re-check previously failed links")
    p.add_argument("--dry-run", action="store_true", help="no writes and no network writes")

    return parser


def setup_logging(verbose: bool, quiet: bool, log_file: Path | None = None) -> None:
    level = logging.DEBUG if verbose else (logging.WARNING if quiet else logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _print(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=True, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose, args.quiet)

    try:
        settings = load_settings(args.root)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if args.offline:
        # make sure no stage can silently reach the network
        pass
    if args.db:
        settings.db_override = Path(args.db)

    from .pipeline import Pipeline

    pipeline = Pipeline(settings, offline=bool(args.offline))
    events: list[dict[str, Any]] = []
    exit_code = EXIT_OK
    try:
        result = _dispatch(pipeline, args, settings, events)
        if result is not None:
            _print(result)
        if isinstance(result, dict) and "exit_code" in result:
            exit_code = int(result["exit_code"])
    except HttpClientError as exc:
        print(f"network error: {exc}", file=sys.stderr)
        exit_code = EXIT_ERROR
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        exit_code = EXIT_USAGE
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        exit_code = EXIT_ERROR
    except KeyboardInterrupt:
        print("interrupted — progress is committed, run again with --resume", file=sys.stderr)
        exit_code = EXIT_ERROR
    finally:
        if events:
            from .reports import write_execution_log

            write_execution_log(events, settings.logs_dir / "execution.log")
        pipeline.close()
    return exit_code


def _dispatch(pipeline, args, settings, events: list[dict[str, Any]]) -> Any:
    command = args.command
    events.append({"at": _now(), "command": command, "offline": args.offline})

    if command == "init-db":
        version = db.init_db(pipeline.conn)
        counts = db.counts(pipeline.conn)
        return {"schema_version": version, "database": str(settings.db_path), "tables": counts}

    if command == "audit-sources":
        results = pipeline.audit_sources(dry_run=args.dry_run)
        events.append({"at": _now(), "stage": "audit-sources", "results": results})
        return results

    if command == "discover":
        if args.dry_run:
            from .source_adapters import PouetAdapter

            adapter = PouetAdapter(settings.source("pouet"))
            path = sorted(settings.input_dir.glob("pouetdatadump-prods-*"))
            if not path:
                return {"dry_run": True, "error": "no local dump found"}
            total = sum(1 for _ in adapter.iter_dump(path[-1]))
            return {"dry_run": True, "dump": path[-1].name, "records": total,
                    "note": "no rows written"}
        stats = pipeline.discover(limit=args.limit, resume=args.resume, source=args.source)
        events.append({"at": _now(), "stage": "discover", "stats": stats})
        return stats

    if command == "enrich":
        stats = pipeline.enrich(limit=args.limit)
        events.append({"at": _now(), "stage": "enrich", "stats": stats})
        return stats

    if command == "curate":
        stats = pipeline.curate(target=args.target, limit=args.limit)
        events.append({"at": _now(), "stage": "curate", "stats": stats})
        return stats

    if command == "verify-links":
        stats = pipeline.verify_links(limit=args.limit, retry_failed=args.retry_failed)
        events.append({"at": _now(), "stage": "verify-links", "stats": stats})
        return stats

    if command == "verify":
        report = pipeline.verify(target=args.target)
        events.append({"at": _now(), "stage": "verify", "status": report["status"]})
        return report

    if command == "export":
        from .export_csv import export_csv
        from .export_html import export_html
        from .export_json import export_json
        from .export_markdown import export_markdown

        if args.dry_run:
            return {"dry_run": True, "would_write": sorted(
                list(EXPORT_NAMES) + ["README.md", "catalogue.md", "missing_videos.md",
                                      "unavailable_downloads.md", "review_required.md",
                                      "index.html"])}
        generated = _now()
        csv_counts = export_csv(pipeline.conn, settings.exports_dir)
        json_result = export_json(pipeline.conn, settings.exports_dir, generated_at=generated)
        md_result = export_markdown(pipeline.conn, settings.exports_dir, generated_at=generated)
        html_result = export_html(pipeline.conn, settings.exports_dir, generated_at=generated)
        consistency = _check_consistency(pipeline.conn, csv_counts, json_result)
        result = {"csv": csv_counts, "json": json_result, "markdown": md_result,
                  "html": html_result, "consistency": consistency, "generated_at": generated}
        events.append({"at": _now(), "stage": "export", "consistency": consistency})
        return result

    if command == "report":
        from .reports import build_report, write_report

        report = build_report(pipeline.conn, generated_at=_now())
        path = write_report(report, settings.logs_dir / "quality_report.md")
        return {"report": str(path), "counts": report["counts"]}

    if command == "run":
        return _run_all(pipeline, args, settings, events)

    raise ValueError(f"unknown command: {command}")


EXPORT_NAMES = (
    "productions.csv", "creators.csv", "production_creators.csv", "platforms.csv",
    "releases.csv", "videos.csv", "provenance.csv", "verification_report.csv",
    "demoscene.json",
)


def _check_consistency(conn, csv_counts: dict[str, int], json_result: dict[str, Any]) -> dict[str, Any]:
    """Exports must agree with the database."""
    problems: list[str] = []
    table_for_csv = {
        "productions": "productions", "creators": "creators", "platforms": "platforms",
        "releases": "releases", "videos": "videos", "provenance": "provenance",
    }
    for csv_name, table in table_for_csv.items():
        db_count = int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])  # noqa: S608
        if db_count != csv_counts.get(csv_name):
            problems.append(f"{csv_name}.csv has {csv_counts.get(csv_name)} rows, database has {db_count}")
    exported = json_result["productions"]
    expected = int(conn.execute(
        "SELECT COUNT(*) AS n FROM productions WHERE selection_status IN ('selected','needs_review')"
    ).fetchone()["n"])
    if exported != expected:
        problems.append(f"demoscene.json has {exported} productions, expected {expected}")
    return {"ok": not problems, "problems": problems}


def _run_all(pipeline, args, settings, events: list[dict[str, Any]]) -> dict[str, Any]:
    from .export_csv import export_csv
    from .export_html import export_html
    from .export_json import export_json
    from .export_markdown import export_markdown
    from .reports import build_report, write_report

    results: dict[str, Any] = {"stages": {}}
    target = args.target or settings.target_size

    results["stages"]["init-db"] = {"schema_version": db.init_db(pipeline.conn)}

    if not args.dry_run:
        audit = pipeline.audit_sources()
        results["stages"]["audit-sources"] = audit
        events.append({"at": _now(), "stage": "audit-sources", "results": audit})

    discover_stats = pipeline.discover(limit=args.limit, resume=args.resume)
    results["stages"]["discover"] = discover_stats
    events.append({"at": _now(), "stage": "discover", "stats": discover_stats})

    curate_stats = pipeline.curate(target=target)
    results["stages"]["curate"] = curate_stats
    events.append({"at": _now(), "stage": "curate", "stats": curate_stats})

    enrich_stats = pipeline.enrich(limit=args.limit)
    results["stages"]["enrich"] = enrich_stats
    events.append({"at": _now(), "stage": "enrich", "stats": enrich_stats})

    link_stats = pipeline.verify_links(limit=args.limit, retry_failed=args.retry_failed)
    results["stages"]["verify-links"] = link_stats
    events.append({"at": _now(), "stage": "verify-links", "stats": link_stats})

    verification = pipeline.verify(target=target)
    results["stages"]["verify"] = verification
    events.append({"at": _now(), "stage": "verify", "status": verification["status"]})

    generated = _now()
    if not args.dry_run:
        csv_counts = export_csv(pipeline.conn, settings.exports_dir)
        json_result = export_json(pipeline.conn, settings.exports_dir, generated_at=generated)
        md_result = export_markdown(pipeline.conn, settings.exports_dir, generated_at=generated)
        html_result = export_html(pipeline.conn, settings.exports_dir, generated_at=generated)
        results["stages"]["export"] = {
            "csv": csv_counts, "json": json_result, "markdown": md_result, "html": html_result,
            "consistency": _check_consistency(pipeline.conn, csv_counts, json_result),
        }
        report = build_report(pipeline.conn, generated_at=generated, verification=verification,
                              extra=results["stages"])
        report_path = write_report(report, settings.logs_dir / "quality_report.md")
        results["stages"]["report"] = {"path": str(report_path), "counts": report["counts"]}

    results["summary"] = {
        "selected": curate_stats.get("selected"),
        "candidates": curate_stats.get("candidates"),
        "verification_status": verification.get("status"),
        "productions_in_db": int(pipeline.conn.execute(
            "SELECT COUNT(*) AS n FROM productions").fetchone()["n"]),
    }
    if verification.get("status") == "fail":
        results["exit_code"] = EXIT_VERIFICATION_FAILED
    return results


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
