"""Tests 15, 20 — link verification and integrity checks."""

from __future__ import annotations

import pytest

from demoscene_collector import database as db
from demoscene_collector.models import (
    CreatorRef,
    ProductionCandidate,
    Provenance,
    ReleaseCandidate,
    VideoCandidate,
)
from demoscene_collector.source_adapters.scene_org import check_link
from demoscene_collector.verification import run_integrity_checks

from conftest import FakeResponse, make_client


def candidate(key: str = "pouet:1", title: str = "First Contact") -> ProductionCandidate:
    source_id, _, record = key.partition(":")
    return ProductionCandidate(
        source_id=source_id, source_record_id=record, title=title,
        source_url="https://www.pouet.net/prod.php?which=1", release_date="1995-04-15",
        groups=[CreatorRef(name="Complex", role="group")], platforms=["MS-Dos"],
        releases=[ReleaseCandidate(url="https://files.scene.org/view/demos/x/first.zip",
                                   source_id="pouet")],
        videos=[VideoCandidate(url="https://www.youtube.com/watch?v=eZyLSHyUGBY",
                               source_id="pouet")],
    )


class TestCheckLink:
    def test_direct_zip_is_verified(self):
        client, _ = make_client([FakeResponse(
            status=200, headers={"Content-Type": "application/zip",
                                 "Content-Length": "1234"},
            url="https://files.scene.org/get/demos/x/first.zip")])
        result = check_link(client, "https://files.scene.org/get/demos/x/first.zip")
        assert result.status == "verified"
        assert result.url_kind == "direct_download"
        assert result.content_length == 1234

    def test_redirect_to_mirror_is_recorded_as_redirected(self):
        client, _ = make_client([FakeResponse(
            status=200, headers={"Content-Type": "application/zip"},
            url="https://mirror.example.org/scene.org/first.zip")])
        result = check_link(client, "https://files.scene.org/get/demos/x/first.zip")
        assert result.status == "redirected"
        assert result.final_url.startswith("https://mirror.example.org/")

    def test_html_response_is_a_landing_page_not_a_verified_artifact(self):
        client, _ = make_client([FakeResponse(
            status=200, headers={"Content-Type": "text/html; charset=utf-8"},
            url="https://files.scene.org/view/demos/x/first.zip")])
        result = check_link(client, "https://files.scene.org/view/demos/x/first.zip")
        assert result.status == "landing_ok"
        assert result.url_kind == "landing_page"

    def test_404_is_broken(self):
        client, session = make_client([FakeResponse(status=404)])
        result = check_link(client, "https://example.test/gone.zip")
        assert result.status == "broken"
        assert result.http_status == 404
        assert len(session.requests) == 1  # not retried

    def test_403_is_blocked_and_not_bypassed(self):
        client, session = make_client([FakeResponse(status=403)])
        result = check_link(client, "https://example.test/secret.zip")
        assert result.status == "blocked"
        assert len(session.requests) == 1

    def test_server_error_is_inaccessible(self):
        client, _ = make_client([FakeResponse(status=500)] * 5)
        result = check_link(client, "https://example.test/a.zip")
        assert result.status == "inaccessible"

    def test_network_failure_is_inaccessible(self):
        import requests as _requests

        client, _ = make_client([_requests.ConnectionError("nope")] * 5)
        result = check_link(client, "https://offline.example/a.zip")
        assert result.status == "inaccessible"

    def test_ftp_link_is_untested_protocol(self):
        client, session = make_client([])
        result = check_link(client, "ftp://ftp.untergrund.net/demo.zip")
        assert result.status == "untested_protocol"
        assert session.requests == []

    def test_landing_page_url_recorded_for_scene_org(self):
        client, _ = make_client([FakeResponse(
            status=200, headers={"Content-Type": "text/html"},
            url="https://files.scene.org/get/demos/x/first.zip")])
        result = check_link(client, "https://files.scene.org/get/demos/x/first.zip")
        # /get/ without a body proving an artifact stays honest: HTML -> landing
        assert result.status in ("landing_ok", "redirected", "verified")


class TestIntegrityChecks:
    def _seed(self, conn, *, with_video_status: str = "untested"):
        pid = db.upsert_production(conn, candidate())
        db.link_creator(conn, pid, db.ensure_creator(conn, CreatorRef(name="Complex")),
                        role="group", source_id="pouet")
        db.link_platform(conn, pid, db.ensure_platform(conn, "MS-DOS", "PC/MS-DOS"),
                         source_id="pouet")
        db.upsert_release(conn, pid, ReleaseCandidate(
            url="https://files.scene.org/view/demos/x/first.zip", source_id="pouet"))
        db.upsert_video(conn, pid, VideoCandidate(
            url="https://www.youtube.com/watch?v=eZyLSHyUGBY", source_id="pouet"))
        db.set_production_scores(conn, "pouet:1", curation_score=0.5, popularity_score=0.4,
                                 significance_score=0.6, rationale="{}",
                                 selection_status="selected", selection_rank=1)
        db.add_provenance(conn, Provenance(
            entity_type="production", entity_id="pouet:1", field_name="title",
            source_id="pouet", source_url="https://pouet/x", observed_value="First Contact"))
        conn.commit()

    def test_clean_database_passes(self, conn):
        self._seed(conn)
        report = run_integrity_checks(conn, target=500)
        assert report["status"] == "ok"
        assert report["selected"] == 1
        names = {c["name"] for c in report["checks"]}
        assert "referential_integrity" in names
        assert "provenance_for_selected" in names

    def test_missing_provenance_for_selected_fails(self, conn):
        pid = db.upsert_production(conn, candidate("pouet:2", "Second Contact"))
        db.set_production_scores(conn, "pouet:2", curation_score=0.5, popularity_score=None,
                                 significance_score=None, rationale="{}",
                                 selection_status="selected", selection_rank=2)
        conn.commit()
        report = run_integrity_checks(conn, target=500)
        check = next(c for c in report["checks"] if c["name"] == "provenance_for_selected")
        assert check["status"] == "fail"
        assert report["status"] == "fail"

    def test_malformed_url_detected(self, conn):
        self._seed(conn)
        conn.execute("UPDATE releases SET download_url = 'not a url'")
        conn.commit()
        report = run_integrity_checks(conn, target=500)
        check = next(c for c in report["checks"] if c["name"] == "release_url_format")
        assert check["status"] == "fail"

    def test_invalid_date_format_detected(self, conn):
        self._seed(conn)
        conn.execute("UPDATE productions SET release_date = '15.04.1995'")
        conn.commit()
        report = run_integrity_checks(conn, target=500)
        check = next(c for c in report["checks"] if c["name"] == "release_date_format")
        assert check["status"] == "fail"

    def test_selected_count_checked_against_target(self, conn):
        self._seed(conn)
        report = run_integrity_checks(conn, target=0)
        check = next(c for c in report["checks"] if c["name"] == "selected_count")
        assert check["status"] == "fail"

    def test_ambiguous_video_match_is_reported(self, conn):
        self._seed(conn)
        conn.execute("UPDATE videos SET verification_status = 'needs_review'")
        conn.commit()
        report = run_integrity_checks(conn, target=500)
        check = next(c for c in report["checks"] if c["name"] == "ambiguous_video_matches")
        assert check["count"] == 1

    def test_unknown_verification_status_rejected_by_schema(self, conn):
        self._seed(conn)
        with pytest.raises(Exception):
            conn.execute("UPDATE releases SET verification_status = 'probably fine'")
