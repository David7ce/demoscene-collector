"""Validated internal models (dataclasses).

Adapters produce these; the database layer persists them. Construction fails
loudly on missing required fields so bad source data never reaches SQLite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .normalization import (
    classify_url,
    display_title,
    normalize_alias,
    normalize_name,
    normalize_platform,
    normalize_title,
    normalize_url,
    normalize_whitespace,
    parse_release_date,
)


@dataclass(frozen=True)
class Provenance:
    """Evidence for one externally sourced claim."""

    entity_type: str          # production | creator | platform | release | video
    entity_id: str            # canonical key or stable identifier of the entity
    field_name: str
    source_id: str            # e.g. "pouet"
    source_record_id: str | None = None
    source_url: str = ""
    observed_value: Any = None
    confidence: str = "high"  # high | medium | low
    notes: str = ""

    def observed_json(self) -> str:
        import json

        return json.dumps(self.observed_value, ensure_ascii=False, sort_keys=True, default=str)


@dataclass
class CreatorRef:
    name: str
    role: str | None = None

    @property
    def normalized_name(self) -> str:
        return normalize_name(self.name)


@dataclass
class VideoCandidate:
    url: str
    source_id: str
    link_type: str | None = None        # raw label from the source page
    video_kind: str = "unknown"         # demo capture | party recording | review | interview | trailer | unknown
    title: str | None = None
    channel_name: str | None = None
    duration_seconds: int | None = None
    published_at: str | None = None
    match_confidence: float | None = None
    match_evidence: str = ""
    verification_status: str = "untested"

    def __post_init__(self) -> None:
        from .normalization import canonical_video_url

        info = canonical_video_url(self.url)
        self.canonical_url = info.canonical_url  # type: ignore[attr-defined]
        self.platform = info.platform            # type: ignore[attr-defined]
        self.video_id = info.video_id            # type: ignore[attr-defined]
        if info.canonical_url == "":
            raise ValueError(f"unrecognised video URL: {self.url!r}")

    canonical_url: str = field(init=False, default="")
    platform: str = field(init=False, default="")
    video_id: str | None = field(init=False, default=None)


@dataclass
class ReleaseCandidate:
    """An original downloadable artifact (metadata only, never downloaded)."""

    url: str
    source_id: str
    artifact_type: str = "other"        # executable | archive | disk image | source code | video capture | documentation | other
    link_label: str | None = None       # raw label from the source ("party version", …)
    release_title: str | None = None
    release_version: str | None = None
    platform: str | None = None
    file_name: str | None = None
    file_size_bytes: int | None = None
    requirements: str | None = None
    kind: str = "unknown"               # direct_download | landing_page | unknown
    kind_reason: str = ""

    def __post_init__(self) -> None:
        self.url = normalize_url(self.url)
        info = classify_url(self.url)
        if self.kind in ("", "unknown"):
            self.kind = info.kind
            self.kind_reason = info.reason
        if not self.file_name:
            from urllib.parse import urlsplit

            name = urlsplit(self.url).path.rstrip("/").rsplit("/", 1)
            if len(name) == 2 and "." in name[1]:
                self.file_name = name[1]


@dataclass
class ProductionCandidate:
    """A production as observed at one source (before deduplication)."""

    source_id: str
    source_record_id: str
    title: str
    source_url: str
    production_type: str | None = None
    release_date: str | None = None       # raw, as provided by the source
    description: str | None = None
    groups: list[CreatorRef] = field(default_factory=list)
    credits: list[CreatorRef] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    releases: list[ReleaseCandidate] = field(default_factory=list)
    videos: list[VideoCandidate] = field(default_factory=list)
    cross_ids: dict[str, str] = field(default_factory=dict)   # {"demozoo": "63820", …}
    popularity: dict[str, Any] = field(default_factory=dict)   # votes, rank, …
    significance: dict[str, Any] = field(default_factory=dict) # placings, awards, …
    raw: dict[str, Any] = field(default_factory=dict)

    # -- derived, computed in __post_init__ ---------------------------------
    canonical_key: str = ""
    normalized_title: str = ""
    display_title: str = ""
    year: int | None = None
    iso_date: str | None = None
    normalized_platforms: list[tuple[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.display_title = display_title(self.title)
        self.normalized_title = normalize_title(self.title)
        if not self.display_title:
            raise ValueError(f"production from {self.source_id} has no title")
        if not self.source_record_id:
            raise ValueError(f"production {self.display_title!r} has no source record id")
        self.canonical_key = f"{self.source_id}:{self.source_record_id}"
        self.source_url = normalize_url(self.source_url)
        parsed = parse_release_date(self.release_date)
        self.year = parsed.year
        self.iso_date = parsed.iso_date
        if self.year is None and not self.iso_date:
            # fall back to any year evidence provided separately
            year_hint = self.raw.get("year") or self.significance.get("year")
            if isinstance(year_hint, int) and 1900 <= year_hint <= 2100:
                self.year = year_hint
        seen: list[tuple[str, str]] = []
        for raw in self.platforms:
            canonical, family = normalize_platform(raw)
            if (canonical, family) not in seen:
                seen.append((canonical, family))
        self.normalized_platforms = seen
        if not self.production_type:
            self.production_type = "other"


def stable_json(value: Any) -> str:
    """Deterministic JSON encoding used for provenance payloads."""
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


__all__ = [
    "CreatorRef",
    "ProductionCandidate",
    "Provenance",
    "ReleaseCandidate",
    "VideoCandidate",
    "normalize_alias",
    "stable_json",
]
