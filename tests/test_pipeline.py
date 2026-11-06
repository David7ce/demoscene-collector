"""Tests 8, 11, 12 — pipeline stages: checkpoints, idempotency, resume, curation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from demoscene_collector import database as db
from demoscene_collector.cli import main
from demoscene_collector.pipeline import Pipeline


@pytest.fixture
def pipeline(settings, dump_file) -> Pipeline:
    pipe = Pipeline(settings, offline=True)
    db.init_db(pipe.conn)
    yield pipe
    pipe.close()


class TestDiscovery:
    def test_offline_discovery_ingests_dump(self, pipeline):
        stats = pipeline.discover(limit=10)
        assert stats["productions"] == 10
        assert stats["dump_sha256"]
        assert db.table_count(pipeline.conn, "productions") == 10

    def test_repeated_discovery_is_idempotent(self, pipeline):
        first = pipeline.discover(limit=10)
        second = pipeline.discover(limit=10)
        assert first["productions"] == 10
        assert second["productions"] == 10
        assert db.table_count(pipeline.conn, "productions") == 10
        assert db.table_count(pipeline.conn, "releases") == first["releases"]
        assert db.table_count(pipeline.conn, "videos") == first["videos"]

    def test_checkpoint_cursor_is_persisted(self, pipeline):
        pipeline.discover(limit=10)
        cursor = db.get_cursor(pipeline.conn, "pouet:discover:last_id")
        assert cursor is not None and int(cursor) > 0

    def test_resume_skips_already_ingested_records(self, pipeline):
        pipeline.discover(limit=10)
        resumed = pipeline.discover(resume=True)
        assert resumed["skipped_existing"] == 10
        assert resumed["productions"] == 20  # fixture dump holds 30 records
        assert db.table_count(pipeline.conn, "productions") == 30

    def test_fetch_job_records_dump_hash(self, pipeline):
        pipeline.discover(limit=5)
        jobs = db.job_rows(pipeline.conn, job_type="discover")
        assert jobs and jobs[-1]["status"] == "success"
        assert jobs[-1]["response_hash"] and len(jobs[-1]["response_hash"]) == 64

    def test_provenance_written_for_every_record(self, pipeline):
        pipeline.discover(limit=7)
        rows = list(pipeline.conn.execute(
            "SELECT * FROM provenance WHERE field_name = 'source_record'"))
        assert len(rows) == 7

    def test_discover_offline_without_dump_reports_error(self, settings, tmp_path):
        (settings.root / "data" / "input" / "pouetdatadump-prods-x.json.gz").unlink(missing_ok=True)
        pipe = Pipeline(settings, offline=True)
        db.init_db(pipe.conn)
        with pytest.raises(Exception):
            pipe.discover()
        pipe.close()


class TestCuration:
    def test_target_size_respected(self, pipeline):
        pipeline.discover(limit=30)
        stats = pipeline.curate(target=5)
        assert stats["selected"] == 5
        selected = list(pipeline.conn.execute(
            "SELECT * FROM productions WHERE selection_status='selected' ORDER BY selection_rank"))
        assert [r["selection_rank"] for r in selected] == [1, 2, 3, 4, 5]

    def test_curation_is_deterministic(self, pipeline):
        pipeline.discover(limit=30)
        first = pipeline.curate(target=8)
        first_keys = [r["canonical_key"] for r in pipeline.conn.execute(
            "SELECT canonical_key FROM productions WHERE selection_status='selected' "
            "ORDER BY selection_rank")]
        pipeline.curate(target=8)
        second_keys = [r["canonical_key"] for r in pipeline.conn.execute(
            "SELECT canonical_key FROM productions WHERE selection_status='selected' "
            "ORDER BY selection_rank")]
        assert first_keys == second_keys
        assert first["selected"] == 8

    def test_scores_and_rationale_recorded(self, pipeline):
        pipeline.discover(limit=30)
        pipeline.curate(target=5)
        row = pipeline.conn.execute(
            "SELECT * FROM productions WHERE selection_status='selected' LIMIT 1").fetchone()
        assert row["curation_score"] is not None
        rationale = json.loads(row["curation_rationale"])
        assert "components" in rationale and "popularity" in rationale
        assert "significance" in rationale

    def test_rejected_candidates_keep_reasons(self, pipeline):
        pipeline.discover(limit=30)
        pipeline.curate(target=5)
        rows = list(pipeline.conn.execute(
            "SELECT curation_rationale FROM productions WHERE selection_status='rejected'"))
        assert rows
        for row in rows:
            assert json.loads(row["curation_rationale"]).get("reason")

    def test_selected_have_field_level_provenance(self, pipeline):
        pipeline.discover(limit=30)
        pipeline.curate(target=5)
        missing = list(pipeline.conn.execute(
            """
            SELECT canonical_key FROM productions p
             WHERE p.selection_status='selected'
               AND NOT EXISTS (SELECT 1 FROM provenance pr
                                WHERE pr.entity_id = p.canonical_key AND pr.field_name='title')
            """))
        assert missing == []

    def test_non_selected_are_not_given_field_provenance(self, pipeline):
        pipeline.discover(limit=30)
        pipeline.curate(target=5)
        count = pipeline.conn.execute(
            "SELECT COUNT(*) AS n FROM provenance WHERE field_name='title'").fetchone()["n"]
        assert count == 5

    def test_exclusion_rule_rejects_with_reason(self, pipeline, settings):
        pipeline.discover(limit=30)
        rules = dict(settings.rules)
        rules["exclusions"] = [{"pattern": "test production 3", "reason": "test exclusion"}]
        from demoscene_collector.selection import select

        candidates = db.load_all_candidates(pipeline.conn)
        result = select(candidates, rules)
        rejected_reasons = [reason for _, reason in result.rejected]
        assert any("excluded by rule" in reason for reason in rejected_reasons)


class TestEnrichment:
    def test_offline_enrichment_marks_videos_untested(self, pipeline):
        pipeline.discover(limit=10)
        pipeline.curate(target=5)
        stats = pipeline.enrich()
        assert stats["videos_checked"] == 0
        rows = list(pipeline.conn.execute("SELECT verification_status FROM videos"))
        assert rows and all(r["verification_status"] == "untested" for r in rows)

    def test_missing_video_without_api_key_is_reported(self, pipeline):
        pipeline.discover(limit=30)
        pipeline.curate(target=15)
        # simulate productions that never had a video link in any source record
        pipeline.conn.execute("DELETE FROM videos")
        pipeline.conn.commit()
        stats = pipeline.enrich()
        assert stats["productions_without_video"] > 0
        assert stats["search_unavailable"] == stats["productions_without_video"]
        assert stats["search_queries"] == 0
        jobs = db.job_rows(pipeline.conn, job_type="video_search")
        assert jobs
        for job in jobs:
            assert job["status"] == "skipped"
            assert "YOUTUBE_API_KEY" in (job["detail"] or "")

    def test_enrichment_is_idempotent(self, pipeline):
        pipeline.discover(limit=10)
        pipeline.curate(target=5)
        first = pipeline.enrich()
        second = pipeline.enrich()
        assert first["videos_checked"] == second["videos_checked"]


class TestLinkVerificationOffline:
    def test_offline_makes_no_requests(self, pipeline):
        pipeline.discover(limit=10)
        pipeline.curate(target=5)
        stats = pipeline.verify_links()
        assert stats["checked"] == 0

    def test_budget_and_host_caps_configured(self, pipeline, settings):
        verification = settings.verification
        assert verification["max_link_checks"] > 0
        assert verification["max_link_checks_per_host"] > 0


class TestIntegrity:
    def test_verify_passes_after_full_flow(self, pipeline):
        pipeline.discover(limit=30)
        pipeline.curate(target=10)
        pipeline.enrich()
        report = pipeline.verify(target=10)
        assert report["status"] == "ok"
        assert report["selected"] == 10


class TestCli:
    def test_init_db(self, settings, capsys):
        code = main(["--root", str(settings.root), "--offline", "init-db"])
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["schema_version"] == 1

    def test_discover_curate_export_flow(self, settings, dump_file, capsys):
        root = str(settings.root)
        assert main(["--root", root, "--offline", "discover", "--limit", "20"]) == 0
        capsys.readouterr()
        assert main(["--root", root, "--offline", "curate", "--target", "5"]) == 0
        capsys.readouterr()
        assert main(["--root", root, "--offline", "verify", "--target", "5"]) == 0
        capsys.readouterr()
        assert main(["--root", root, "--offline", "export"]) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["consistency"]["ok"] is True
        exports = settings.exports_dir
        for name in ("productions.csv", "demoscene.json", "catalogue.md",
                     "verification_report.csv"):
            assert (exports / name).exists()

    def test_audit_sources_dry_run_makes_no_requests(self, settings, capsys):
        code = main(["--root", str(settings.root), "audit-sources", "--dry-run"])
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert all(item["status"] == "skipped" for item in payload)

    def test_report_command(self, settings, capsys):
        main(["--root", str(settings.root), "--offline", "init-db"])
        capsys.readouterr()
        assert main(["--root", str(settings.root), "--offline", "report"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert Path(payload["report"]).exists()

    def test_unknown_config_is_a_usage_error(self, tmp_path, capsys):
        code = main(["--root", str(tmp_path / "nowhere"), "init-db"])
        assert code == 2
        assert "configuration error" in capsys.readouterr().err
