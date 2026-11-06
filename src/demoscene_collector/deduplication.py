"""Stage C — deduplication.

Identity rules (documented in docs/data_dictionary.md):

1. **Exact source id** — the same source and record id always identify the same
   production (confidence 1.0, auto-merge).
2. **Cross-source id** — a stored cross-reference id (e.g. a Demozoo id carried
   in a Pouët record) identifies the same production (confidence 0.99).
3. **Fuzzy evidence** — normalised title + creators + year + platform are
   combined into a 0..1 confidence:

   * ``>= auto_merge``  → merge automatically (aliases + merge_log preserved);
   * ``>= review``      → mark ``needs_review``, never auto-merged;
   * below that         → keep as distinct candidates.

Conflicting records are never deleted: merges keep aliases, provenance and a
row in ``merge_log``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Iterable, Sequence

from .normalization import normalize_title

EXACT_ID = 1.0


@dataclass(frozen=True)
class Identity:
    """The evidence used for matching."""

    canonical_key: str
    source_id: str
    source_record_id: str
    normalized_title: str
    year: int | None
    creators: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ()
    cross_ids: tuple[tuple[str, str], ...] = ()


@dataclass
class MatchResult:
    confidence: float
    label: str          # exact_id | cross_source_id | high | ambiguous | distinct
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def auto_merge(self) -> bool:
        return self.label in ("exact_id", "cross_source_id") or self.confidence >= 0.999


@dataclass
class DedupDecision:
    survivor: Identity
    candidate: Identity
    match: MatchResult
    action: str         # merge | review | distinct


@dataclass
class DedupReport:
    decisions: list[DedupDecision] = field(default_factory=list)
    merged: list[DedupDecision] = field(default_factory=list)
    review: list[DedupDecision] = field(default_factory=list)
    distinct_pairs: int = 0

    @property
    def review_count(self) -> int:
        return len(self.review)


def identity_from_candidate(cand: Any) -> Identity:
    """Build an :class:`Identity` from a :class:`ProductionCandidate`-like object."""
    creators = sorted({c.normalized_name for c in getattr(cand, "credits", [])
                       if c.normalized_name} | {g.normalized_name for g in getattr(cand, "groups", [])
                       if g.normalized_name})
    platforms = sorted({family for _, family in getattr(cand, "normalized_platforms", [])})
    cross = tuple(sorted((k, str(v)) for k, v in getattr(cand, "cross_ids", {}).items()))
    return Identity(
        canonical_key=cand.canonical_key,
        source_id=cand.source_id,
        source_record_id=cand.source_record_id,
        normalized_title=cand.normalized_title,
        year=cand.year,
        creators=tuple(creators),
        platforms=tuple(platforms),
        cross_ids=cross,
    )


def title_ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _cross_source_match(a: Identity, b: Identity) -> dict[str, Any] | None:
    """Shared cross-reference ids, including 'b is identified by a's cross id'."""
    for left, right in ((a, b), (b, a)):
        for key, value in left.cross_ids:
            if not value:
                continue
            if (key, value) in right.cross_ids:
                return {"reason": f"shared {key} id", "value": value}
            if right.source_id == key and right.source_record_id == value:
                return {"reason": f"{right.source_id} id matches cross-reference {key}",
                        "value": value}
    return None


def compare(a: Identity, b: Identity, thresholds: dict[str, float] | None = None) -> MatchResult:
    """Compute a match confidence between two identities."""
    thr = thresholds or {}
    auto_thr = float(thr.get("auto_merge", 0.92))
    review_thr = float(thr.get("review", 0.75))

    if a.source_id == b.source_id and a.source_record_id == b.source_record_id:
        return MatchResult(EXACT_ID, "exact_id", {"reason": "same source and record id"})

    cross = _cross_source_match(a, b)
    if cross:
        return MatchResult(0.99, "cross_source_id", cross)

    ratio = title_ratio(a.normalized_title, b.normalized_title)
    evidence: dict[str, Any] = {"title_ratio": round(ratio, 4)}

    if ratio == 0.0:
        return MatchResult(0.0, "distinct", evidence)

    creator_overlap = bool(set(a.creators) & set(b.creators))
    year_equal = a.year is not None and b.year is not None and a.year == b.year
    year_gap = abs((a.year or 0) - (b.year or 0)) if (a.year and b.year) else None
    platform_overlap = bool(set(a.platforms) & set(b.platforms))
    cross_source = a.source_id != b.source_id
    evidence.update({
        "creator_overlap": creator_overlap,
        "year_equal": year_equal,
        "year_gap": year_gap,
        "platform_overlap": platform_overlap,
        "cross_source": cross_source,
    })

    confidence = 0.55 * ratio
    if creator_overlap:
        confidence += 0.25
    if year_equal:
        confidence += 0.15
    elif year_gap is not None and year_gap <= 1:
        confidence += 0.07
    if platform_overlap:
        confidence += 0.05
    confidence = round(min(confidence, 0.98), 4)

    if ratio == 1.0:
        evidence["note"] = "identical normalised title"
        corroborated = creator_overlap and year_equal
        if corroborated and cross_source and confidence >= auto_thr:
            return MatchResult(max(confidence, auto_thr), "high", evidence)
        if corroborated or (platform_overlap and (creator_overlap or year_equal or cross_source)):
            # Enough overlap to be suspicious, but not enough (or not cross-source)
            # to merge automatically -> a human decides.
            confidence = max(confidence, review_thr)
            return MatchResult(confidence, "ambiguous", evidence)
        # Generic title collision ("intro", "megademo") with no supporting
        # evidence: these are different productions, not duplicates.
        return MatchResult(min(confidence, review_thr - 0.01), "distinct", evidence)

    # Near-identical titles (small spelling variants) only reach review/merge
    # when creators and year agree; merely similar titles stay distinct.
    if ratio >= 0.97 and creator_overlap and year_equal and confidence >= review_thr:
        if confidence >= auto_thr and cross_source:
            return MatchResult(confidence, "high", evidence)
        if cross_source:
            return MatchResult(confidence, "ambiguous", evidence)
        return MatchResult(confidence, "ambiguous", evidence)
    return MatchResult(confidence, "distinct", evidence)


def deduplicate(candidates: Sequence[Any], thresholds: dict[str, float] | None = None) -> DedupReport:
    """Compare candidates efficiently and classify merge/review/distinct.

    Scalability: only candidates that *can* match are compared — those sharing a
    normalised title and those sharing a cross-reference id. Everything else is
    distinct by construction, which keeps the full 100k-record population
    tractable (O(n + Σ groups²)) instead of an O(n²) sweep.

    Order of processing is deterministic (sorted by canonical key), so repeated
    runs produce identical decisions.
    """
    auto_thr = float((thresholds or {}).get("auto_merge", 0.92))
    identities = sorted((identity_from_candidate(c) for c in candidates),
                        key=lambda i: i.canonical_key)
    report = DedupReport()
    examined: set[tuple[str, str]] = set()

    buckets: dict[str, list[Identity]] = {}
    for identity in identities:
        buckets.setdefault(identity.normalized_title, []).append(identity)

    cross_index: dict[tuple[str, str], list[Identity]] = {}
    for identity in identities:
        # index by the source's own id so that 'demozoo:63820' meets a Pouët
        # record that carries 'demozoo = 63820'
        cross_index.setdefault((identity.source_id, identity.source_record_id), []).append(identity)
        for key, value in identity.cross_ids:
            if value:
                cross_index.setdefault((key, value), []).append(identity)

    pairs: list[tuple[Identity, Identity]] = []
    for group in buckets.values():
        for i, first in enumerate(group):
            for second in group[i + 1:]:
                pairs.append((first, second))
    for group in cross_index.values():
        ordered = sorted(group, key=lambda i: i.canonical_key)
        for i, first in enumerate(ordered):
            for second in ordered[i + 1:]:
                pairs.append((first, second))

    for first, second in sorted(pairs, key=lambda p: (p[0].canonical_key, p[1].canonical_key)):
        pair_key = (first.canonical_key, second.canonical_key)
        if pair_key in examined:
            continue
        examined.add(pair_key)
        match = compare(first, second, thresholds)
        if match.label == "distinct":
            report.distinct_pairs += 1
            continue
        action = "merge" if (match.auto_merge or match.confidence >= auto_thr) else "review"
        decision = DedupDecision(survivor=first, candidate=second, match=match, action=action)
        report.decisions.append(decision)
        if action == "merge":
            report.merged.append(decision)
        else:
            report.review.append(decision)
    return report


def merged_keys(report: DedupReport) -> set[str]:
    """Canonical keys that were merged away (the survivor keeps the identity)."""
    return {d.candidate.canonical_key for d in report.merged}


def review_keys(report: DedupReport) -> set[str]:
    keys: set[str] = set()
    for decision in report.review:
        keys.add(decision.survivor.canonical_key)
        keys.add(decision.candidate.canonical_key)
    return keys


def group_by_identity(candidates: Iterable[Any]) -> dict[str, list[Any]]:
    """Group candidates by normalized title — used by the review report."""
    groups: dict[str, list[Any]] = {}
    for cand in candidates:
        groups.setdefault(cand.normalized_title, []).append(cand)
    return {k: v for k, v in groups.items() if len(v) > 1}
