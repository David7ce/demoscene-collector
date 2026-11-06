"""Tests 1 — unicode, whitespace, title, URL and date normalisation."""

from __future__ import annotations

import pytest

from demoscene_collector.normalization import (
    classify_url,
    canonical_video_url,
    display_title,
    normalize_artifact_type,
    normalize_name,
    normalize_platform,
    normalize_title,
    normalize_url,
    normalize_whitespace,
    parse_release_date,
)


class TestWhitespaceAndUnicode:
    def test_collapses_repeated_whitespace(self):
        assert normalize_whitespace("  a \t  b \n c  ") == "a b c"

    def test_unicode_nfc_normalisation(self):
        precomposed = "caf\u00e9"     # é as one code point
        decomposed = "cafe\u0301"     # e + combining acute
        assert precomposed != decomposed
        assert normalize_whitespace(decomposed) == precomposed

    def test_control_whitespace_normalised(self):
        assert normalize_whitespace("line1\r\nline2") == "line1 line2"
        assert normalize_whitespace("trailing   ") == "trailing"

    def test_none_and_empty(self):
        assert normalize_whitespace(None) == ""
        assert normalize_whitespace("") == ""

    def test_display_title_keeps_case_and_punctuation(self):
        assert display_title("  First \t Contact!  ") == "First Contact!"


class TestTitleNormalisation:
    def test_case_and_punctuation_insensitive(self):
        assert normalize_title("First Contact!") == normalize_title("FIRST  contact")

    @pytest.mark.parametrize("variant", [
        "Celestial Motion",
        "celestial  motion",
        "Celestial_Motion",
        "celestial-motion",
        "Celestial/Motion",
        "Céléstial Motîon",
    ])
    def test_stylised_variants_match(self, variant):
        assert normalize_title(variant) == "celestial motion"

    def test_diacritics_removed_for_matching_only(self):
        assert normalize_title("Mojibake") == "mojibake"
        assert normalize_title("Überdemo") == "uberdemo"

    def test_names_use_the_same_rules(self):
        assert normalize_name("  The Black Lotus ") == "the black lotus"


class TestUrlNormalisation:
    def test_host_lowercased_and_fragment_dropped(self):
        assert normalize_url("HTTPS://Example.COM/Path#frag") == "https://example.com/Path"

    def test_tracking_parameters_removed_and_query_sorted(self):
        out = normalize_url("https://example.com/x?b=2&utm_source=tw&a=1&fbclid=zz")
        assert out == "https://example.com/x?a=1&b=2"

    def test_identifying_query_parameters_preserved(self):
        assert normalize_url("https://x.test/prod.php?which=42") == \
            "https://x.test/prod.php?which=42"

    def test_default_ports_stripped(self):
        assert normalize_url("https://example.test:443/a") == "https://example.test/a"

    def test_empty_input(self):
        assert normalize_url("") == ""
        assert normalize_url(None) == ""


class TestUrlClassification:
    def test_scene_org_view_is_a_landing_page(self):
        info = classify_url("https://files.scene.org/view/demos/groups/tbl/pc/astral.zip")
        assert info.kind == "landing_page"

    def test_scene_org_get_is_a_direct_download(self):
        info = classify_url("https://files.scene.org/get/demos/groups/tbl/pc/astral.zip")
        assert info.kind == "direct_download"

    def test_artifact_extension_is_direct(self):
        assert classify_url("https://ftp.example.org/pub/demo.zip").kind == "direct_download"
        assert classify_url("https://x.test/file.d64").kind == "direct_download"
        assert classify_url("https://x.test/demo.exe").kind == "direct_download"

    def test_html_content_type_wins_over_extension(self):
        info = classify_url("https://x.test/download.zip",
                             content_type="text/html; charset=utf-8")
        assert info.kind == "landing_page"

    def test_http_with_no_evidence_is_unknown(self):
        assert classify_url("https://csdb.dk/release/?id=123").kind == "unknown"

    def test_ftp_scheme_is_unknown_and_flagged(self):
        info = classify_url("ftp://ftp.untergrund.net/demo.zip")
        assert info.kind == "unknown"
        assert info.reason.startswith("scheme:")

    def test_empty_url(self):
        assert classify_url("").kind == "unknown"


class TestVideoUrlCanonicalisation:
    def test_watch_url(self):
        info = canonical_video_url("https://www.youtube.com/watch?v=eZyLSHyUGBY")
        assert info.platform == "youtube"
        assert info.video_id == "eZyLSHyUGBY"
        assert info.canonical_url == "https://www.youtube.com/watch?v=eZyLSHyUGBY"

    def test_short_url_and_tracking(self):
        info = canonical_video_url("https://youtu.be/eZyLSHyUGBY?si=abc123&t=10")
        assert info.video_id == "eZyLSHyUGBY"

    def test_embed_and_shorts_paths(self):
        assert canonical_video_url("https://www.youtube.com/embed/abcDEF12345").video_id == \
            "abcDEF12345"
        assert canonical_video_url("https://www.youtube.com/shorts/abcDEF12345").video_id == \
            "abcDEF12345"

    def test_vimeo(self):
        info = canonical_video_url("https://vimeo.com/12345678")
        assert info.platform == "vimeo"
        assert info.canonical_url == "https://vimeo.com/12345678"

    def test_unsupported_host_is_reported(self):
        info = canonical_video_url("https://dhs.tv/clip/1")
        assert info.platform == "other"

    def test_invalid_id_is_rejected(self):
        info = canonical_video_url("https://www.youtube.com/watch?v=@@@")
        assert info.video_id is None
        assert info.canonical_url == ""


class TestDates:
    @pytest.mark.parametrize("value,year,iso", [
        ("1997-03-15", 1997, "1997-03-15"),
        ("1997", 1997, None),
        (None, None, None),
        ("", None, None),
        ("1997-13-45", None, None),
        ("March 1997", None, None),
        ("1899", None, None),
    ])
    def test_parse_release_date(self, value, year, iso):
        parsed = parse_release_date(value)
        assert parsed.year == year
        assert parsed.iso_date == iso


class TestPlatformMapping:
    @pytest.mark.parametrize("raw,canonical,family", [
        ("MS-Dos", "MS-DOS", "PC/MS-DOS"),
        ("Windows", "Windows", "Windows"),
        ("Amiga OCS/ECS", "Amiga OCS/ECS", "Amiga"),
        ("Amiga AGA", "Amiga AGA", "Amiga"),
        ("Commodore 64", "Commodore 64", "Commodore 64"),
        ("Atari ST", "Atari ST", "Atari ST"),
        ("Atari STe", "Atari STe", "Atari ST"),
        ("ZX Spectrum", "ZX Spectrum", "Other"),
        ("Something Never Seen", "Something Never Seen", "Other"),
        ("", "Unknown", "Other"),
    ])
    def test_platform_families(self, raw, canonical, family):
        assert normalize_platform(raw) == (canonical, family)


class TestArtifactTypes:
    @pytest.mark.parametrize("label,expected", [
        ("Youtube", "video capture"),
        ("video", "video capture"),
        ("online version", "landing page"),
        ("source", "source code"),
        ("github", "source code"),
        ("soundtrack", "documentation"),
        ("party version", "archive"),
        ("weird label", "executable"),
        (None, "other"),
    ])
    def test_label_mapping(self, label, expected):
        assert normalize_artifact_type(label) == expected
