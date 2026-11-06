"""Tests 16–20 — CSV/JSON/Markdown exports: encoding, escaping, consistency."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

import pytest

from demoscene_collector import database as db
from demoscene_collector.export_csv import export_csv
from demoscene_collector.export_html import export_html
from demoscene_collector.export_json import build_document, export_json
from demoscene_collector.export_markdown import (
    export_markdown,
    md_escape,
    write_missing_videos,
    write_unavailable_downloads,
)
from demoscene_collector.models import (
    CreatorRef,
    ProductionCandidate,
    Provenance,
    ReleaseCandidate,
    VideoCandidate,
)

GENERATED = "2026-10-10T00:00:00Z"

TRICKY_TITLE = 'First, Contact "Final" v2\nline two — café'


def seed(conn, *, title: str = TRICKY_TITLE) -> None:
    cand = ProductionCandidate(
        source_id="pouet", source_record_id="1", title=title,
        source_url="https://www.pouet.net/prod.php?which=1",
        release_date="1995-04-15", production_type="demo",
        description="Demo by Complex, released 1995-04-15.",
        groups=[CreatorRef(name="Complex", role="group")],
        credits=[CreatorRef(name="jugi", role="Code")],
        platforms=["MS-Dos"],
        releases=[
            ReleaseCandidate(url="https://files.scene.org/view/demos/x/first.zip",
                             source_id="pouet"),
            ReleaseCandidate(url="ftp://ftp.untergrund.net/old/demo.zip", source_id="pouet"),
        ],
        videos=[VideoCandidate(url="https://www.youtube.com/watch?v=eZyLSHyUGBY",
                               source_id="pouet")],
        popularity={"voteup": 85, "rank": 12}, significance={"placings": []},
    )
    pid = db.upsert_production(conn, cand)
    db.link_creator(conn, pid, db.ensure_creator(conn, CreatorRef(name="Complex")),
                    role="group", source_id="pouet")
    db.link_creator(conn, pid, db.ensure_creator(conn, CreatorRef(name="jugi")),
                    role="Code", source_id="pouet")
    db.link_platform(conn, pid, db.ensure_platform(conn, "MS-DOS", "PC/MS-DOS"),
                     source_id="pouet", evidence_url=cand.source_url)
    for rel in cand.releases:
        db.upsert_release(conn, pid, rel)
    for vid in cand.videos:
        db.upsert_video(conn, pid, vid)
    db.upsert_metrics(conn, pid, popularity=cand.popularity, significance=cand.significance,
                      cross_ids={"demozoo": "63820"}, extra={"source_url": cand.source_url})
    db.set_production_scores(conn, cand.canonical_key, curation_score=0.71,
                             popularity_score=0.5, significance_score=0.8,
                             rationale='{"components": {"votes": 0.9}}',
                             selection_status="selected", selection_rank=1)
    db.add_provenance(conn, Provenance(
        entity_type="production", entity_id=cand.canonical_key, field_name="title",
        source_id="pouet", source_record_id="1", source_url=cand.source_url,
        observed_value=title))
    conn.commit()


class TestCsv:
    def test_utf8_bom_written(self, conn, tmp_path):
        seed(conn)
        export_csv(conn, tmp_path)
        raw = (tmp_path / "productions.csv").read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")

    def test_quoting_and_multiline_fields(self, conn, tmp_path):
        seed(conn)
        export_csv(conn, tmp_path)
        with (tmp_path / "productions.csv").open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        assert len(rows) == 1
        # whitespace (including newlines) is normalised at ingestion, commas and
        # quotes must survive a CSV round-trip untouched
        assert rows[0]["title"] == TRICKY_TITLE.replace("\n", " ")
        assert "," in rows[0]["title"] and '"' in rows[0]["title"]
        assert "café" in rows[0]["title"]

    def test_raw_multiline_field_is_quoted_correctly(self, conn, tmp_path):
        seed(conn)
        conn.execute("UPDATE productions SET description = 'line one\nline two, with comma'")
        conn.commit()
        export_csv(conn, tmp_path)
        with (tmp_path / "productions.csv").open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        assert rows[0]["description"] == "line one\nline two, with comma"

    def test_one_entity_per_row(self, conn, tmp_path):
        seed(conn)
        export_csv(conn, tmp_path)
        with (tmp_path / "releases.csv").open(encoding="utf-8-sig", newline="") as handle:
            release_rows = list(csv.DictReader(handle))
        with (tmp_path / "videos.csv").open(encoding="utf-8-sig", newline="") as handle:
            video_rows = list(csv.DictReader(handle))
        assert len(release_rows) == 2
        assert len(video_rows) == 1

    def test_verification_report_includes_link_states(self, conn, tmp_path):
        seed(conn)
        export_csv(conn, tmp_path)
        with (tmp_path / "verification_report.csv").open(encoding="utf-8-sig",
                                                          newline="") as handle:
            rows = list(csv.DictReader(handle))
        statuses = {r["status"] for r in rows}
        assert "untested" in statuses
        assert "selected" in statuses


class TestJson:
    def test_document_schema(self, conn, tmp_path):
        seed(conn)
        result = export_json(conn, tmp_path, generated_at=GENERATED)
        document = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
        for key in ("schema_version", "generator", "generated_at", "summary", "productions"):
            assert key in document
        prod = document["productions"][0]
        for key in ("canonical_key", "title", "creators", "platforms", "releases", "videos",
                    "provenance", "scores", "selection"):
            assert key in prod

    def test_referential_consistency(self, conn, tmp_path):
        seed(conn)
        export_json(conn, tmp_path, generated_at=GENERATED)
        document = json.loads((tmp_path / "demoscene.json").read_text(encoding="utf-8"))
        prod = document["productions"][0]
        assert prod["releases"] and all("verification_status" in r for r in prod["releases"])
        assert prod["videos"] and all(v["url"].startswith("https://") for v in prod["videos"])
        assert prod["provenance"] and all(p["source_url"] for p in prod["provenance"])

    def test_counts_match_database(self, conn, tmp_path):
        seed(conn)
        result = export_json(conn, tmp_path, generated_at=GENERATED)
        assert result["counts"]["productions"] == 1
        assert result["counts"]["selected"] == 1
        assert result["productions"] == 1

    def test_deterministic_output(self, conn, tmp_path):
        seed(conn)
        first = export_json(conn, tmp_path / "a", generated_at=GENERATED)
        second = export_json(conn, tmp_path / "b", generated_at=GENERATED)
        left = Path(first["path"]).read_text(encoding="utf-8")
        right = Path(second["path"]).read_text(encoding="utf-8")
        assert left == right

    def test_no_secrets_or_headers_in_export(self, conn, tmp_path):
        seed(conn)
        export_json(conn, tmp_path, generated_at=GENERATED)
        text = (tmp_path / "demoscene.json").read_text(encoding="utf-8")
        for banned in ("Authorization", "api_key", "YOUTUBE_API_KEY", "Set-Cookie"):
            assert banned not in text


class TestMarkdown:
    def test_md_escape(self):
        assert md_escape("a*b_c") == "a\\*b\\_c"
        assert md_escape("[link](x)") == "\\[link\\](x)"
        assert md_escape("a|b") == "a\\|b"
        assert md_escape(None) == ""

    def test_markdown_files_written(self, conn, tmp_path):
        seed(conn)
        results = export_markdown(conn, tmp_path, generated_at=GENERATED)
        for name in ("README.md", "catalogue.md", "missing_videos.md",
                     "unavailable_downloads.md", "review_required.md"):
            assert (tmp_path / name).exists()
        assert results["catalogue"]["entries"] == 1

    def test_catalogue_contains_entry_fields(self, conn, tmp_path):
        seed(conn)
        export_markdown(conn, tmp_path, generated_at=GENERATED)
        text = (tmp_path / "catalogue.md").read_text(encoding="utf-8")
        assert "First, Contact" in text
        assert "verified" in text or "untested" in text
        assert "pouet:1" in text

    def test_deterministic_markdown(self, conn, tmp_path):
        seed(conn)
        export_markdown(conn, tmp_path / "a", generated_at=GENERATED)
        export_markdown(conn, tmp_path / "b", generated_at=GENERATED)
        for name in ("catalogue.md", "missing_videos.md", "review_required.md"):
            assert (tmp_path / "a" / name).read_text() == (tmp_path / "b" / name).read_text()

    def test_missing_video_report_groups_statuses(self, conn, tmp_path):
        seed(conn)
        result = write_missing_videos(conn, tmp_path / "missing_videos.md",
                                      generated_at=GENERATED)
        assert result["unverified"] == 1  # link exists but has not been checked
        text = (tmp_path / "missing_videos.md").read_text(encoding="utf-8")
        assert "unverified" in text

    def test_unavailable_downloads_lists_untested_protocol(self, conn, tmp_path):
        seed(conn)
        result = write_unavailable_downloads(conn, tmp_path / "unavailable_downloads.md",
                                             generated_at=GENERATED)
        # the ftp:// link cannot be checked over HTTP
        text = (tmp_path / "unavailable_downloads.md").read_text(encoding="utf-8")
        assert "untested" in text or "untested_protocol" in text
        assert "ftp://" in text

    def test_broken_link_is_reported(self, conn, tmp_path):
        seed(conn)
        conn.execute("UPDATE releases SET verification_status = 'broken', "
                     "check_detail = 'HTTP 404' WHERE download_url LIKE 'https%'")
        conn.commit()
        result = write_unavailable_downloads(conn, tmp_path / "u.md", generated_at=GENERATED)
        assert result["counts"].get("broken") == 1

    def test_build_document_is_json_serialisable(self, conn):
        seed(conn)
        document = build_document(conn, generated_at=GENERATED)
        json.dumps(document, sort_keys=True)  # must not raise
        assert document["schema_version"] == "1.0"


class TestHtml:
    def test_gallery_written_and_payload_parses(self, conn, tmp_path):
        seed(conn)
        result = export_html(conn, tmp_path, generated_at=GENERATED)
        page = (tmp_path / "index.html").read_text(encoding="utf-8")
        assert result["entries"] == 1
        assert result["counts"]["selected"] == 1
        assert "<table>" in page
        assert 'id="review"' in page
        assert "Include review queue" in page
        assert 'id="year-from"' in page and 'id="year-to"' in page
        assert 'id="platform"' in page
        assert 'data-key="year"' in page and 'data-key="download"' in page
        payload = re.search(
            r'<script id="data" type="application/json">(.*?)</script>', page, re.S
        )
        assert payload is not None
        data = json.loads(payload.group(1))
        assert data["entries"][0]["title"].startswith("First, Contact")
        assert "café" in data["entries"][0]["title"]
        assert data["entries"][0]["videos"][0]["status"]
        assert data["generated_at"] == GENERATED

    def test_title_cannot_break_out_of_the_script_tag(self, conn, tmp_path):
        seed(conn, title="</script><b>boom</b>")
        export_html(conn, tmp_path, generated_at=GENERATED)
        page = (tmp_path / "index.html").read_text(encoding="utf-8")
        assert "</script><b>boom</b>" not in page
        assert "\\u003c/script\\u003e" in page

    def test_gallery_is_deterministic(self, conn, tmp_path):
        seed(conn)
        export_html(conn, tmp_path / "a", generated_at=GENERATED)
        export_html(conn, tmp_path / "b", generated_at=GENERATED)
        assert (tmp_path / "a" / "index.html").read_text() == \
            (tmp_path / "b" / "index.html").read_text()
