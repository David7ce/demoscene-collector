"""Stage D — curation: scoring and deterministic, diversity-aware selection.

Popularity and historical/technical significance are computed as **separate**
sub-scores; only an explicit weighted sum (``curation_score``) combines them.
Every component value is kept in ``curation_rationale`` (JSON) so an auditor can
see exactly why a production received its score.

The selection algorithm is deterministic:

1. apply configured exclusions (candidates are rejected with a reason, never
   dropped silently);
2. sort candidates by score, tie-breaking on canonical key;
3. fill minimum quotas per platform family and per era when enough candidates
   exist;
4. fill (platform family × era) cells up to ``max_per_cell``;
5. fill any remaining budget by score alone.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .normalization import normalize_title

MAX_VOTE_REFERENCE = 500.0   # log-normalisation reference (thumb-ups)
HISTORIC_START_YEAR = 1980
HISTORIC_SPAN = 60           # 1980 -> 2040


@dataclass
class ScoredCandidate:
    candidate: Any
    canonical_key: str
    components: dict[str, float]
    popularity: float
    significance: float
    curation: float
    cell: tuple[str, str]           # (platform family, era)
    rationale: str

    @property
    def title(self) -> str:
        return self.candidate.display_title


@dataclass
class SelectionResult:
    selected: list[ScoredCandidate] = field(default_factory=list)
    rejected: list[tuple[ScoredCandidate, str]] = field(default_factory=list)
    scored: list[ScoredCandidate] = field(default_factory=list)

    @property
    def selected_count(self) -> int:
        return len(self.selected)


def era_for(year: int | None, era_rules: Sequence[dict[str, Any]]) -> str:
    if year is None:
        for era in era_rules:
            if era.get("start") is None and era.get("end") is None:
                return str(era.get("name", "unknown"))
        return "unknown"
    for era in era_rules:
        start = era.get("start")
        end = era.get("end")
        if start is None or end is None:
            continue
        if int(start) <= year <= int(end):
            return str(era.get("name"))
    return "unknown"


def _votes_score(voteup: int | None) -> float:
    if not voteup or voteup <= 0:
        return 0.0
    return min(1.0, math.log1p(voteup) / math.log1p(MAX_VOTE_REFERENCE))


def _rank_score(rank: int | None, population: int) -> float:
    if not rank or rank <= 0 or population <= 0:
        return 0.0
    # Pouët rank 1 = best. Map to 0..1 over the observed ranked population.
    return max(0.0, min(1.0, (population - rank) / population))


def _placement_score(placings: Sequence[dict[str, Any]], points: dict[int, float],
                     participation: float) -> float:
    best = 0.0
    for placing in placings:
        if not isinstance(placing, dict):
            continue
        raw_rank = placing.get("ranking")
        try:
            rank = int(str(raw_rank)) if raw_rank not in (None, "", "None") else None
        except (TypeError, ValueError):
            rank = None
        if rank is None:
            best = max(best, participation)
            continue
        if rank in points:
            best = max(best, float(points[rank]))
        elif rank > 8:
            best = max(best, participation)
    return min(1.0, best)


def _awards_score(awards: Sequence[Any]) -> float:
    return min(1.0, len(list(awards)) / 2.0)


def _age_score(year: int | None) -> float:
    if year is None:
        return 0.2  # unknown age: no historical seniority evidence, small floor
    return max(0.05, min(1.0, 1.0 - (year - HISTORIC_START_YEAR) / HISTORIC_SPAN))


def _provenance_score(cand: Any) -> float:
    score = 0.0
    if getattr(cand, "groups", None):
        score += 0.4
    if getattr(cand, "credits", None):
        score += 0.3
    if getattr(cand, "normalized_platforms", None):
        score += 0.3
    return min(1.0, score)


def score_candidate(cand: Any, rules: dict[str, Any], *, population: int) -> ScoredCandidate:
    weights = {k: float(v) for k, v in dict(rules.get("weights", {})).items()}
    type_name = (getattr(cand, "production_type", None) or "other").lower()
    in_scope = {str(t).lower() for t in rules.get("in_scope_types", [])}
    out_scope = {str(t).lower() for t in rules.get("out_of_scope_types", [])}
    out_factor = float(rules.get("out_of_scope_type_factor", 0.35))
    if type_name in in_scope:
        type_relevance = 1.0
    elif type_name in out_scope:
        type_relevance = out_factor
    else:
        type_relevance = 1.0  # unknown-but-real production types are not penalised

    has_video = bool(getattr(cand, "videos", None))
    has_download = bool(getattr(cand, "releases", None))
    penalise_video = bool(rules.get("penalise_missing_video", True))
    video_evidence = 1.0 if has_video else (0.0 if penalise_video else 0.5)

    popularity_raw = dict(getattr(cand, "popularity", {}) or {})
    significance_raw = dict(getattr(cand, "significance", {}) or {})
    placement_points = {int(k): float(v) for k, v in dict(
        rules.get("placement_points", {})).items()}
    participation = float(rules.get("participation_points", 0.10))

    components: dict[str, float] = {
        "votes": _votes_score(popularity_raw.get("voteup")),
        "rank": _rank_score(popularity_raw.get("rank"), population),
        "placement": _placement_score(significance_raw.get("placings") or [],
                                      placement_points, participation),
        "awards": _awards_score(significance_raw.get("awards") or []),
        "age": _age_score(getattr(cand, "year", None)),
        "video_evidence": video_evidence,
        "download_evidence": 1.0 if has_download else 0.0,
        "provenance": _provenance_score(cand),
        "type_relevance": type_relevance,
    }

    curation = 0.0
    for key, weight in weights.items():
        curation += weight * components.get(key, 0.0)

    popularity_keys = {str(k) for k in rules.get("popularity_weight_keys", ["votes", "rank"])}
    significance_keys = {str(k) for k in rules.get(
        "significance_weight_keys", ["placement", "awards", "age"])}
    pop_weight = sum(weights.get(k, 0.0) for k in popularity_keys) or 1.0
    sig_weight = sum(weights.get(k, 0.0) for k in significance_keys) or 1.0
    popularity = sum(weights.get(k, 0.0) * components.get(k, 0.0)
                     for k in popularity_keys) / pop_weight
    significance = sum(weights.get(k, 0.0) * components.get(k, 0.0)
                       for k in significance_keys) / sig_weight

    diversity = dict(rules.get("diversity", {}))
    families = list(diversity.get("platform_families", []))
    era_rules = list(diversity.get("eras", []))
    family = _primary_family(cand, families)
    era = era_for(getattr(cand, "year", None), era_rules)

    rationale = json.dumps(
        {
            "components": {k: round(v, 4) for k, v in components.items()},
            "weights": {k: round(v, 4) for k, v in weights.items()},
            "popularity": round(popularity, 4),
            "significance": round(significance, 4),
            "curation": round(curation, 4),
            "cell": [family, era],
            "note": (
                "components are evidence-based (0..1); popularity and significance "
                "are reported separately and are not interchangeable"
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return ScoredCandidate(
        candidate=cand,
        canonical_key=cand.canonical_key,
        components=components,
        popularity=round(popularity, 4),
        significance=round(significance, 4),
        curation=round(curation, 4),
        cell=(family, era),
        rationale=rationale,
    )


def _primary_family(cand: Any, families: Sequence[str]) -> str:
    normalized = getattr(cand, "normalized_platforms", []) or []
    if normalized:
        family = normalized[0][1]
        if not families or family in families:
            return family
        return family
    return families[-1] if families else "Other"


def _excluded(cand: Any, exclusions: Sequence[dict[str, Any]]) -> str | None:
    for rule in exclusions or []:
        pattern = str(rule.get("pattern", "")).lower()
        if not pattern:
            continue
        if pattern in normalize_title(getattr(cand, "display_title", "")) or \
           pattern in (getattr(cand, "normalized_title", "") or ""):
            return f"excluded by rule: {rule.get('pattern')}"
    return None


def sort_key(scored: ScoredCandidate, tie_break: str = "canonical_key") -> tuple:
    return (-scored.curation, -scored.significance, scored.canonical_key)


def select(candidates: Sequence[Any], rules: dict[str, Any]) -> SelectionResult:
    """Score all candidates and choose up to ``target_size`` productions."""
    target = int(rules.get("target_size", 500))
    exclusions = list(rules.get("exclusions", []))
    diversity = dict(rules.get("diversity", {}))
    max_per_cell = int(diversity.get("max_per_cell", 30))
    min_per_family = int(diversity.get("min_per_platform_family", 0))
    min_per_era = int(diversity.get("min_per_era", 0))
    families = [str(f) for f in diversity.get("platform_families", [])]
    era_names = [str(e.get("name")) for e in diversity.get("eras", [])]
    tie_break = str(rules.get("tie_break", "canonical_key"))
    population = len(candidates)

    result = SelectionResult()
    scored: list[ScoredCandidate] = []
    for cand in candidates:
        reason = _excluded(cand, exclusions)
        item = score_candidate(cand, rules, population=population)
        result.scored.append(item)
        if reason:
            result.rejected.append((item, reason))
            continue
        scored.append(item)

    scored.sort(key=lambda s: sort_key(s, tie_break))

    chosen: list[ScoredCandidate] = []
    chosen_keys: set[str] = set()
    cell_counts: dict[tuple[str, str], int] = {}
    family_counts: dict[str, int] = {}
    era_counts: dict[str, int] = {}

    def try_take(item: ScoredCandidate, *, ignore_cell_cap: bool = False) -> bool:
        if item.canonical_key in chosen_keys:
            return False
        family, era = item.cell
        if not ignore_cell_cap and cell_counts.get((family, era), 0) >= max_per_cell:
            return False
        chosen.append(item)
        chosen_keys.add(item.canonical_key)
        cell_counts[(family, era)] = cell_counts.get((family, era), 0) + 1
        family_counts[family] = family_counts.get(family, 0) + 1
        era_counts[era] = era_counts.get(era, 0) + 1
        return True

    # Pass 1 — minimum representation per platform family / era (when feasible).
    if min_per_family > 0:
        for family in families:
            needed = min_per_family - family_counts.get(family, 0)
            for item in scored:
                if needed <= 0 or len(chosen) >= target:
                    break
                if item.cell[0] != family:
                    continue
                if try_take(item):
                    needed -= 1
    if min_per_era > 0:
        for era in era_names:
            if era == "unknown":
                continue
            needed = min_per_era - era_counts.get(era, 0)
            for item in scored:
                if needed <= 0 or len(chosen) >= target:
                    break
                if item.cell[1] != era:
                    continue
                if item.canonical_key in chosen_keys:
                    continue
                if try_take(item):
                    needed -= 1

    # Pass 2 — cell caps.
    for item in scored:
        if len(chosen) >= target:
            break
        try_take(item)

    # Pass 3 — fill remaining budget by score (caps no longer bind).
    for item in scored:
        if len(chosen) >= target:
            break
        try_take(item, ignore_cell_cap=True)

    chosen.sort(key=lambda s: sort_key(s, tie_break))
    result.selected = chosen
    selected_keys = {c.canonical_key for c in chosen}
    rejected_keys = {item.canonical_key for item, _ in result.rejected}
    for item in result.scored:
        if item.canonical_key in selected_keys:
            continue
        if item.canonical_key in rejected_keys:
            continue
        reason = _rejection_reason(item, cell_counts, target)
        result.rejected.append((item, reason))
    return result


def _rejection_reason(item: ScoredCandidate, cell_counts: dict, target: int) -> str:
    family, era = item.cell
    count = cell_counts.get((family, era), 0)
    return (
        f"not selected: curation_score={item.curation:.4f} below cutoff for target={target}; "
        f"cell {family} × {era} filled with {count} entries"
    )
