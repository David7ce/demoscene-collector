"""Tests 8–10, 13–15 — adapters, HTTP resilience, video extraction, URL kinds."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
import requests

from demoscene_collector.http_client import (
    HttpClient,
    HttpClientError,
    PermanentFailure,
    TransientFailure,
)
from demoscene_collector.source_adapters import (
    NoApiKey,
    PouetAdapter,
    SceneOrgAdapter,
    YouTubeAdapter,
    match_video,
    parse_iso8601_duration,
    parse_prod,
    title_similarity,
)
from demoscene_collector.source_adapters.pouet import artifact_type_for, is_video_link

from conftest import FakeResponse, make_client, sample_pouet_records


class TestHttpClientResilience:
    def test_429_retries_then_succeeds(self):
        client, session = make_client([
            FakeResponse(status=429, headers={"Retry-After": "1"}),
            FakeResponse(status=200, body="ok"),
        ])
        result = client.get("https://example.test/a")
        assert result.status == 200
        assert len(session.requests) == 2

    def test_5xx_retries_then_fails(self):
        client, session = make_client([
            FakeResponse(status=503), FakeResponse(status=503), FakeResponse(status=503),
            FakeResponse(status=503),
        ])
        with pytest.raises(TransientFailure):
            client.get("https://example.test/a")
        # bounded retries: initial attempt + max_retries
        assert len(session.requests) == 3  # max_retries default 2 in make_client

    def test_timeout_is_retried_then_reported(self):
        client, session = make_client([requests.Timeout("boom")] * 5)
        with pytest.raises(TransientFailure):
            client.get("https://example.test/a")
        assert len(session.requests) == 3

    def test_connection_error_is_transient(self):
        client, _ = make_client([requests.ConnectionError("refused")] * 5)
        with pytest.raises(TransientFailure):
            client.get("https://example.test/a")

    def test_404_is_permanent_and_not_retried(self):
        client, session = make_client([FakeResponse(status=404)])
        with pytest.raises(PermanentFailure):
            client.get("https://example.test/a")
        assert len(session.requests) == 1

    def test_403_authentication_failure_is_not_retried(self):
        client, session = make_client([FakeResponse(status=403, body="denied")])
        with pytest.raises(PermanentFailure) as exc:
            client.get("https://example.test/a")
        assert exc.value.status == 403
        assert len(session.requests) == 1

    def test_retry_after_header_is_honoured(self):
        delays: list[float] = []
        client, session = make_client([
            FakeResponse(status=429, headers={"Retry-After": "7"}),
            FakeResponse(status=200, body="ok"),
        ])
        client._sleep = delays.append
        client.get("https://example.test/a")
        assert delays and abs(delays[0] - 7.0) < 0.01

    def test_response_size_is_bounded(self):
        client, _ = make_client([FakeResponse(status=200, body=b"x" * 5000)])
        client.config.max_response_bytes = 1000
        result = client.get("https://example.test/a")
        assert len(result.body) <= 1000

    def test_malformed_json_is_returned_as_text_not_crash(self):
        client, _ = make_client([FakeResponse(status=200, body="{not json")])
        result = client.get("https://example.test/a")
        with pytest.raises(ValueError):
            json.loads(result.text)

    def test_user_agent_is_sent(self):
        client, session = make_client([FakeResponse(status=200, body="ok")])
        client.get("https://example.test/a")
        assert session.headers.get("User-Agent") == "TestAgent/0.1"

    def test_cache_avoids_second_request(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            client, session = make_client([FakeResponse(status=200, body="ok")])
            client.cache_dir = Path(tmp)
            client.get("https://example.test/a", cache=True)
            client.get("https://example.test/a", cache=True)
            assert len(session.requests) == 1

    def test_cache_key_includes_query_parameters(self):
        """Regression: params used to be ignored, so every oEmbed lookup for a
        different video returned the first cached response."""
        import json as _json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            client, session = make_client([
                FakeResponse(status=200, body=_json.dumps({"title": "Video A"})),
                FakeResponse(status=200, body=_json.dumps({"title": "Video B"})),
            ])
            client.cache_dir = Path(tmp)
            a = client.get("https://example.test/oembed",
                           params={"url": "https://yt.test/watch?v=aaa"}, cache=True)
            b = client.get("https://example.test/oembed",
                           params={"url": "https://yt.test/watch?v=bbb"}, cache=True)
            assert len(session.requests) == 2
            assert _json.loads(a.text)["title"] == "Video A"
            assert _json.loads(b.text)["title"] == "Video B"

    def test_cache_key_is_order_independent(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            client, session = make_client([FakeResponse(status=200, body="ok")])
            client.cache_dir = Path(tmp)
            client.get("https://example.test/a", params={"x": "1", "y": "2"}, cache=True)
            client.get("https://example.test/a", params={"y": "2", "x": "1"}, cache=True)
            assert len(session.requests) == 1

    def test_too_many_redirects_is_reported_not_raised(self):
        """Regression: an unhandled TooManyRedirects crashed the whole
        verify-links stage."""
        import requests

        client, _ = make_client([requests.exceptions.TooManyRedirects("loop")])
        with pytest.raises(PermanentFailure) as excinfo:
            client.head("https://example.test/loop")
        assert "redirect loop" in str(excinfo.value)

    def test_unexpected_transport_error_becomes_transient_failure(self):
        import requests

        client, _ = make_client([requests.exceptions.InvalidURL("bad url")])
        with pytest.raises(TransientFailure):
            client.get("https://example.test/x")

    def test_per_host_rate_limit_is_applied(self):
        waited: list[float] = []
        client, session = make_client(
            [FakeResponse(status=200, body="a"), FakeResponse(status=200, body="b")],
            per_host_interval={"example.test": 2.0},
        )
        client._sleep = waited.append
        client.get("https://example.test/a")
        client.get("https://example.test/b")
        assert waited and waited[0] > 1.0


class TestPouetParsing:
    def test_parse_record_fields(self, pouet_records):
        cand = parse_prod(pouet_records[0])
        assert cand.canonical_key == "pouet:1"
        assert cand.display_title == "Test Production 1"
        assert cand.year == 1989  # 1988 + (1 % 30)
        assert cand.production_type == "demo"
        assert {g.name for g in cand.groups} == {"Group 2"}
        assert {c.role for c in cand.credits} == {"Code", "Music"}
        assert cand.popularity["voteup"] == 10
        assert cand.significance["placings"] == []

    def test_video_links_extracted_and_canonicalised(self, pouet_records):
        cand = parse_prod(pouet_records[0])
        assert cand.videos, "expected an explicit YouTube link"
        assert all(v.platform == "youtube" for v in cand.videos)
        assert all(v.canonical_url.startswith("https://www.youtube.com/watch?v=")
                   for v in cand.videos)
        assert cand.videos[0].video_kind == "demo capture"

    def test_release_links_skip_video_links(self, pouet_records):
        cand = parse_prod(pouet_records[0])
        urls = [r.url for r in cand.releases]
        assert not any("youtube" in u for u in urls)
        assert any(r.kind == "landing_page" for r in cand.releases)

    def test_artifact_type_classification(self):
        assert artifact_type_for("https://files.scene.org/x/demo.zip") == "archive"
        assert artifact_type_for("https://x.test/game.d64") == "disk image"
        assert artifact_type_for("https://x.test/intro.exe") == "executable"
        assert artifact_type_for("https://x.test/cap.avi") == "video capture"
        assert artifact_type_for("https://x.test/a", "source") == "source code"
        assert artifact_type_for("https://x.test/a", "online version") == "landing page"

    def test_video_link_detection(self):
        assert is_video_link("https://www.youtube.com/watch?v=abc", None)
        assert is_video_link("https://vimeo.com/1", "Youtube")
        assert not is_video_link("https://files.scene.org/a.zip", "online")

    def test_cross_ids_are_recorded(self, pouet_records):
        cand = parse_prod(pouet_records[2])  # id 3 -> demozoo set
        assert cand.cross_ids.get("demozoo") == "5003"

    def test_description_is_source_derived(self, pouet_records):
        cand = parse_prod(pouet_records[1])  # i=2 -> even -> party placement
        assert "Test Party" in (cand.description or "")
        assert cand.description.endswith(".")

    def test_dump_streaming_and_date(self, dump_file):
        adapter = PouetAdapter({"base_url": "https://www.pouet.net/"})
        records = list(adapter.iter_dump(dump_file))
        assert len(records) == 30
        assert adapter.dump_date(dump_file).startswith("2026-01-07")

    def test_discover_offline_without_dump_raises(self, tmp_path):
        adapter = PouetAdapter({"base_url": "https://www.pouet.net/"})
        client, _ = make_client([])
        with pytest.raises(Exception):
            list(adapter.discover(client, dump_dir=tmp_path / "empty", offline=True))

    def test_discover_honours_limit(self, dump_file):
        adapter = PouetAdapter({"base_url": "https://www.pouet.net/"})
        client, session = make_client([])
        out = list(adapter.discover(client, dump_dir=dump_file.parent, offline=True, limit=5))
        assert len(out) == 5
        assert session.requests == []  # offline: no network at all


class TestSceneOrgAdapter:
    def test_view_to_get_translation(self):
        adapter = SceneOrgAdapter({"base_url": "https://files.scene.org/"})
        assert adapter.to_direct_url(
            "https://files.scene.org/view/demos/x/demo.zip") == \
            "https://files.scene.org/get/demos/x/demo.zip"
        assert adapter.landing_url(
            "https://files.scene.org/get/demos/x/demo.zip") == \
            "https://files.scene.org/view/demos/x/demo.zip"

    def test_foreign_urls_are_returned_unchanged(self):
        adapter = SceneOrgAdapter({"base_url": "https://files.scene.org/"})
        assert adapter.to_direct_url("https://csdb.dk/release/?id=1") == \
            "https://csdb.dk/release/?id=1"


class TestYouTubeAdapter:
    def test_iso8601_duration(self):
        assert parse_iso8601_duration("PT1H2M3S") == 3723
        assert parse_iso8601_duration("PT45S") == 45
        assert parse_iso8601_duration("PT2M") == 120
        assert parse_iso8601_duration("") is None
        assert parse_iso8601_duration("nonsense") is None

    def test_search_without_key_raises(self):
        adapter = YouTubeAdapter({"base_url": "https://www.googleapis.com/youtube/v3/"},
                                 api_key=None)
        client, session = make_client([])
        with pytest.raises(NoApiKey):
            adapter.search(client, "query")
        assert session.requests == []

    def test_probe_without_key_records_403(self):
        adapter = YouTubeAdapter({"base_url": "https://www.googleapis.com/youtube/v3/"},
                                 api_key=None)
        client, _ = make_client([FakeResponse(status=403, body='{"error":{}}')])
        probe = adapter.probe(client)
        assert probe.reachable is True
        assert probe.auth_required is True
        assert "403" in probe.detail

    def test_oembed_returns_metadata(self):
        payload = {"title": "The Black Lotus - Astral Blur (1997)",
                   "author_name": "DemosceneVids"}
        adapter = YouTubeAdapter({"oembed_url": "https://www.youtube.com/oembed"}, api_key=None)
        client, _ = make_client([FakeResponse(status=200, body=json.dumps(payload))])
        meta = adapter.oembed(client, "https://www.youtube.com/watch?v=eZyLSHyUGBY")
        assert meta.available is True
        assert meta.title.startswith("The Black Lotus")

    def test_oembed_404_marks_unavailable(self):
        adapter = YouTubeAdapter({"oembed_url": "https://www.youtube.com/oembed"}, api_key=None)
        client, _ = make_client([FakeResponse(status=404, body="Not Found")])
        meta = adapter.oembed(client, "https://www.youtube.com/watch?v=missing")
        assert meta.available is False

    def test_oembed_malformed_json_marks_unavailable(self):
        adapter = YouTubeAdapter({"oembed_url": "https://www.youtube.com/oembed"}, api_key=None)
        client, _ = make_client([FakeResponse(status=200, body="<html>oops</html>")])
        meta = adapter.oembed(client, "https://www.youtube.com/watch?v=x")
        assert meta.available is False
        assert "malformed" in meta.detail


class TestVideoMatching:
    def test_matching_title_with_creator_and_year_is_verified(self):
        decision = match_video(
            production_title="Astral Blur",
            video_title="The Black Lotus - Astral Blur (1997) [60fps]",
            creators=["The Black Lotus"], year=1997, explicit_link=True,
        )
        assert decision.status == "verified_match"
        assert decision.confidence >= 0.8

    def test_unrelated_video_is_rejected(self):
        decision = match_video(
            production_title="Astral Blur",
            video_title="Funny cat compilation 2024",
            creators=["The Black Lotus"], year=1997, explicit_link=False,
        )
        assert decision.status == "rejected"

    def test_unrelated_video_from_explicit_link_needs_review(self):
        decision = match_video(
            production_title="Astral Blur",
            video_title="Funny cat compilation 2024",
            creators=["The Black Lotus"], year=1997, explicit_link=True,
        )
        assert decision.status == "needs_review"

    def test_missing_metadata_needs_review(self):
        decision = match_video(production_title="X", video_title=None)
        assert decision.status == "needs_review"

    def test_title_alone_is_not_enough_for_verified(self):
        decision = match_video(
            production_title="Nexus",
            video_title="Nexus",
            creators=[], year=None, explicit_link=False,
        )
        assert decision.status in ("probable_match", "needs_review", "rejected")
        assert decision.status != "verified_match" or decision.confidence >= 0.8

    def test_title_similarity_bounds(self):
        assert title_similarity("a", "a") == 1.0
        assert 0.0 <= title_similarity("alpha", "beta") <= 1.0
        assert title_similarity("", "beta") == 0.0
