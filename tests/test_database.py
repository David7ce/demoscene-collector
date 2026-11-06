"""Tests 4–7 and 11 — database constraints, relationships, provenance, idempotency."""

from __future__ import annotations

import sqlite3

import pytest

from demoscene_collector import database as db
from demoscene_collector.models import (
    CreatorRef,
    ProductionCandidate,
    Provenance,
    ReleaseCandidate,
    VideoCandidate,
)


def make_candidate(key: str = "pouet:1", title: str = "First Contact") -> ProductionCandidate:
    source_id, _, record = key.partition(":")
    return ProductionCandidate(
        source_id=source_id, source_record_id=record, title=title,
        source_url="https://www.pouet.net/prod.php?which=1",
        release_date="1995-04-15",
        groups=[CreatorRef(name="Complex", role="group")],
        credits=[CreatorRef(name="jugi", role="Code")],
        platforms=["MS-Dos", "Windows"],
        releases=[ReleaseCandidate(url="https://files.scene.org/view/demos/x/first.zip",
                                   source_id="pouet")],
        videos=[VideoCandidate(url="https://www.youtube.com/watch?v=eZyLSHyUGBY",
                               source_id="pouet")],
    )


class TestSchema:
    def test_schema_version_recorded(self, conn):
        assert db.current_version(conn) == db.SCHEMA_VERSION
        assert db.init_db(conn) == db.SCHEMA_VERSION  # idempotent

    def test_foreign_keys_enforced(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO releases(production_id, source_id, download_url, created_at, updated_at)"
                " VALUES (99999, 'pouet', 'https://x/y.zip', '2026-01-01', '2026-01-01')"
            )

    def test_unique_canonical_key(self, conn):
        db.upsert_production(conn, make_candidate())
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO productions(canonical_key, title, normalized_title, created_at, updated_at)"
                " VALUES ('pouet:1', 'X', 'x', '2026-01-01', '2026-01-01')"
            )

    def test_invalid_year_rejected(self, conn):
        db.upsert_production(conn, make_candidate())
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE productions SET release_year = 1400 WHERE canonical_key = 'pouet:1'"
            )

    def test_invalid_selection_status_rejected(self, conn):
        with pytest.raises(ValueError):
            db.set_production_scores(conn, "pouet:1", curation_score=None,
                                     popularity_score=None, significance_score=None,
                                     rationale="{}", selection_status="maybe")

    def test_invalid_video_status_rejected(self, conn):
        with pytest.raises(ValueError):
            db.update_video_check(conn, 1, status="maybe", evidence="x")

    def test_timestamps_are_utc(self, conn):
        db.upsert_production(conn, make_candidate())
        row = conn.execute("SELECT created_at, updated_at FROM productions").fetchone()
        assert row["created_at"].endswith("Z")
        assert row["updated_at"].endswith("Z")


class TestRelationships:
    def test_multiple_creators_and_platforms_per_production(self, conn):
        cand = make_candidate()
        pid = db.upsert_production(conn, cand)
        for ref in cand.groups + cand.credits:
            db.link_creator(conn, pid, db.ensure_creator(conn, ref),
                            role=ref.role or "unknown", source_id="pouet")
        for name, family in cand.normalized_platforms:
            db.link_platform(conn, pid, db.ensure_platform(conn, name, family),
                             source_id="pouet", evidence_url=cand.source_url)
        conn.commit()
        creators = conn.execute("SELECT COUNT(*) AS n FROM production_creators").fetchone()["n"]
        platforms = conn.execute("SELECT COUNT(*) AS n FROM production_platforms").fetchone()["n"]
        assert creators == 2
        assert platforms == 2

    def test_many_to_many_creators(self, conn):
        first = make_candidate("pouet:1", "First Contact")
        second = make_candidate("pouet:2", "Second Contact")
        pid1 = db.upsert_production(conn, first)
        pid2 = db.upsert_production(conn, second)
        shared = db.ensure_creator(conn, CreatorRef(name="jugi", role="Code"))
        db.link_creator(conn, pid1, shared, role="Code", source_id="pouet")
        db.link_creator(conn, pid2, shared, role="Music", source_id="pouet")
        conn.commit()
        rows = list(conn.execute(
            "SELECT production_id, role FROM production_creators WHERE creator_id = ? ORDER BY role",
            (shared,)))
        assert [(r["production_id"], r["role"]) for r in rows] == [(pid1, "Code"), (pid2, "Music")]

    def test_creator_name_uniqueness_is_normalised(self, conn):
        a = db.ensure_creator(conn, CreatorRef(name="The Black Lotus"))
        b = db.ensure_creator(conn, CreatorRef(name="  the   black lotus "))
        assert a == b

    def test_multiple_videos_and_releases_per_production(self, conn):
        cand = make_candidate()
        pid = db.upsert_production(conn, cand)
        db.upsert_release(conn, pid, ReleaseCandidate(
            url="https://files.scene.org/view/a/one.zip", source_id="pouet"))
        db.upsert_release(conn, pid, ReleaseCandidate(
            url="https://files.scene.org/get/a/two.zip", source_id="pouet"))
        db.upsert_video(conn, pid, VideoCandidate(
            url="https://www.youtube.com/watch?v=eZyLSHyUGBY", source_id="pouet"))
        db.upsert_video(conn, pid, VideoCandidate(
            url="https://www.youtube.com/watch?v=bbbbbbbbbbbbb", source_id="pouet"))
        conn.commit()
        assert conn.execute("SELECT COUNT(*) AS n FROM releases").fetchone()["n"] == 2
        assert conn.execute("SELECT COUNT(*) AS n FROM videos").fetchone()["n"] == 2

    def test_release_uniqueness_per_production(self, conn):
        pid = db.upsert_production(conn, make_candidate())
        rel = ReleaseCandidate(url="https://files.scene.org/view/a/one.zip", source_id="pouet")
        db.upsert_release(conn, pid, rel)
        db.upsert_release(conn, pid, rel)
        conn.commit()
        assert conn.execute("SELECT COUNT(*) AS n FROM releases").fetchone()["n"] == 1


class TestProvenance:
    def test_provenance_written_and_preserved(self, conn):
        db.add_provenance(conn, Provenance(
            entity_type="production", entity_id="pouet:1", field_name="title",
            source_id="pouet", source_record_id="1",
            source_url="https://www.pouet.net/prod.php?which=1",
            observed_value={"title": "First Contact"}))
        conn.commit()
        row = conn.execute("SELECT * FROM provenance").fetchone()
        assert row["field_name"] == "title"
        assert "First Contact" in row["observed_value_json"]

    def test_conflicting_values_from_different_sources_are_kept(self, conn):
        for source, url in (("pouet", "https://pouet/prod.php?which=1"),
                            ("demozoo", "https://demozoo.org/productions/1/")):
            db.add_provenance(conn, Provenance(
                entity_type="production", entity_id="pouet:1", field_name="release_year",
                source_id=source, source_url=url, observed_value=1994))
        conn.commit()
        rows = list(conn.execute("SELECT * FROM provenance WHERE field_name='release_year'"))
        assert len(rows) == 2

    def test_identical_provenance_is_idempotent(self, conn):
        prov = Provenance(entity_type="production", entity_id="pouet:1", field_name="title",
                          source_id="pouet", source_url="https://pouet/x", observed_value="A")
        db.add_provenance(conn, prov)
        db.add_provenance(conn, prov)
        conn.commit()
        assert conn.execute("SELECT COUNT(*) AS n FROM provenance").fetchone()["n"] == 1

    def test_batch_returns_number_of_new_rows(self, conn):
        records = [
            Provenance(entity_type="production", entity_id="pouet:1", field_name=f"f{i}",
                       source_id="pouet", source_url="https://pouet/x", observed_value=i)
            for i in range(3)
        ]
        assert db.add_provenance_batch(conn, records) == 3
        assert db.add_provenance_batch(conn, records) == 0
        conn.commit()


class TestIdempotentImport:
    def test_repeated_upserts_do_not_duplicate(self, conn):
        cand = make_candidate()
        for _ in range(3):
            pid = db.upsert_production(conn, cand)
            db.link_creator(conn, pid, db.ensure_creator(conn, CreatorRef(name="jugi")),
                            role="Code", source_id="pouet")
            db.link_platform(conn, pid, db.ensure_platform(conn, "MS-DOS", "PC/MS-DOS"),
                             source_id="pouet")
            db.upsert_release(conn, pid, ReleaseCandidate(
                url="https://files.scene.org/view/a/one.zip", source_id="pouet"))
            db.upsert_video(conn, pid, VideoCandidate(
                url="https://www.youtube.com/watch?v=eZyLSHyUGBY", source_id="pouet"))
        conn.commit()
        counts = db.counts(conn)
        assert counts["productions"] == 1
        assert counts["production_creators"] == 1
        assert counts["production_platforms"] == 1
        assert counts["releases"] == 1
        assert counts["videos"] == 1

    def test_upsert_updates_but_keeps_id(self, conn):
        first = db.upsert_production(conn, make_candidate())
        updated = make_candidate(title="First Contact (Final)")
        second = db.upsert_production(conn, updated)
        conn.commit()
        assert first == second
        row = conn.execute("SELECT title FROM productions").fetchone()
        assert row["title"] == "First Contact (Final)"

    def test_job_rows_are_unique_per_target(self, conn):
        for _ in range(3):
            db.record_job(conn, job_type="discover", source_id="pouet", target_key="dump",
                          status="success", attempt_increment=True, response_body=b"abc")
        conn.commit()
        row = conn.execute("SELECT * FROM fetch_jobs").fetchone()
        assert row["attempt_count"] == 3
        assert len(row["response_hash"]) == 64

    def test_cursors_roundtrip(self, conn):
        assert db.get_cursor(conn, "missing") is None
        db.set_cursor(conn, "pouet:discover:last_id", "100")
        db.set_cursor(conn, "pouet:discover:last_id", "200")
        conn.commit()
        assert db.get_cursor(conn, "pouet:discover:last_id") == "200"

    def test_merge_log_is_recorded_once(self, conn):
        for _ in range(2):
            db.record_merge(conn, survivor_key="pouet:1", merged_key="other:1",
                            method="high", confidence=0.95, evidence={"title_ratio": 1.0})
        conn.commit()
        assert conn.execute("SELECT COUNT(*) AS n FROM merge_log").fetchone()["n"] == 1
