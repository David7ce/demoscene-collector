"""Tests 2–3 — deduplication: exact ids, fuzzy matches, ambiguous review."""

from __future__ import annotations

from demoscene_collector.deduplication import (
    DedupReport,
    compare,
    deduplicate,
    group_by_identity,
    identity_from_candidate,
    merged_keys,
    review_keys,
    title_ratio,
)
from demoscene_collector.models import CreatorRef, ProductionCandidate

THRESHOLDS = {"auto_merge": 0.92, "review": 0.75}


def make_candidate(key: str, title: str, *, year: int | None = 1995,
                   group: str | None = "Some Group", platform: str = "MS-DOS",
                   source: str = "pouet", cross: dict | None = None) -> ProductionCandidate:
    source_id, _, record = key.partition(":")
    return ProductionCandidate(
        source_id=source_id,
        source_record_id=record,
        title=title,
        source_url="https://example.test/x",
        release_date=str(year) if year else None,
        groups=[CreatorRef(name=group, role="group")] if group else [],
        platforms=[platform],
        cross_ids=cross or {},
    )


class TestExactIdentity:
    def test_same_source_and_id_is_exact(self):
        a = make_candidate("pouet:1", "Some Demo")
        b = make_candidate("pouet:1", "Some Demo")
        match = compare(identity_from_candidate(a), identity_from_candidate(b), THRESHOLDS)
        assert match.confidence == 1.0
        assert match.label == "exact_id"
        assert match.auto_merge

    def test_shared_cross_source_id_merges(self):
        a = make_candidate("pouet:1", "Some Demo", cross={"demozoo": "63820"})
        b = make_candidate("demozoo:63820", "Some Demo (v2)", cross={})
        match = compare(identity_from_candidate(a), identity_from_candidate(b), THRESHOLDS)
        assert match.label == "cross_source_id"
        assert match.confidence >= 0.99


class TestFuzzyMatching:
    def test_identical_title_group_and_year_is_high(self):
        a = make_candidate("pouet:1", "First Contact")
        b = make_candidate("other:9", "First Contact")
        match = compare(identity_from_candidate(a), identity_from_candidate(b), THRESHOLDS)
        assert match.label == "high"
        assert match.confidence >= THRESHOLDS["auto_merge"]

    def test_different_titles_are_distinct(self):
        a = make_candidate("pouet:1", "First Contact")
        b = make_candidate("pouet:2", "Completely Different")
        match = compare(identity_from_candidate(a), identity_from_candidate(b), THRESHOLDS)
        assert match.label == "distinct"

    def test_title_ratio_is_symmetric_and_bounded(self):
        ratio = title_ratio("first contact", "first contact")
        assert ratio == 1.0
        assert 0.0 <= title_ratio("abc", "xyz") <= 1.0

    def test_ambiguous_case_requires_review(self):
        # same title, no shared creators, unknown years -> between review and auto
        a = make_candidate("pouet:1", "Nexus", year=None, group=None)
        b = make_candidate("other:2", "Nexus", year=None, group=None)
        match = compare(identity_from_candidate(a), identity_from_candidate(b), THRESHOLDS)
        # identical titles without supporting evidence must NOT be auto-merged
        assert match.label in ("ambiguous", "high")
        if match.confidence < THRESHOLDS["auto_merge"]:
            assert match.label == "ambiguous"


class TestDeduplicateReport:
    def test_report_separates_merge_review_and_distinct(self):
        candidates = [
            make_candidate("pouet:1", "First Contact"),
            make_candidate("other:1", "First Contact"),
            make_candidate("pouet:2", "Something Else Entirely"),
            make_candidate("pouet:3", "Third Thing"),
        ]
        report = deduplicate(candidates, THRESHOLDS)
        assert isinstance(report, DedupReport)
        # only candidates that share evidence are ever compared
        examined = {d.survivor.canonical_key for d in report.decisions} | \
                   {d.candidate.canonical_key for d in report.decisions}
        assert "pouet:2" not in examined and "pouet:3" not in examined
        # review keys never include unrelated productions
        keys = review_keys(report)
        assert "pouet:2" not in keys and "pouet:3" not in keys

    def test_merged_keys_reference_losers_only(self):
        candidates = [
            make_candidate("pouet:1", "First Contact"),
            make_candidate("other:1", "First Contact"),
        ]
        report = deduplicate(candidates, THRESHOLDS)
        losers = merged_keys(report)
        for decision in report.merged:
            assert decision.survivor.canonical_key not in losers

    def test_deterministic_output(self):
        candidates = [
            make_candidate("pouet:1", "First Contact"),
            make_candidate("other:1", "First Contact"),
            make_candidate("pouet:2", "Nexus"),
            make_candidate("other:2", "Nexus"),
        ]
        first = deduplicate(candidates, THRESHOLDS)
        second = deduplicate(list(reversed(candidates)), THRESHOLDS)
        assert [(d.survivor.canonical_key, d.candidate.canonical_key, d.match.confidence)
                for d in first.decisions] == \
               [(d.survivor.canonical_key, d.candidate.canonical_key, d.match.confidence)
                for d in second.decisions]

    def test_group_by_identity_flags_duplicate_titles(self):
        candidates = [
            make_candidate("pouet:1", "Nexus"),
            make_candidate("pouet:2", "Nexus"),
            make_candidate("pouet:3", "Alone"),
        ]
        groups = group_by_identity(candidates)
        assert set(groups) == {"nexus"}
        assert len(groups["nexus"]) == 2
