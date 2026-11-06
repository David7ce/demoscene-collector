"""Source adapter registry."""

from __future__ import annotations

from typing import Any

from .base import ProbeResult, SourceAdapter, SourceError, SourceUnreachable
from .csdb import CsdbAdapter
from .demozoo import DemozooAdapter
from .pouet import PouetAdapter, parse_prod
from .scene_org import LinkCheck, SceneOrgAdapter, check_link
from .youtube import (
    MatchDecision,
    NoApiKey,
    VideoMeta,
    YouTubeAdapter,
    YouTubeQuotaExceeded,
    build_search_query,
    match_video,
    parse_iso8601_duration,
    title_similarity,
)

ADAPTERS: dict[str, type[SourceAdapter]] = {
    "pouet": PouetAdapter,
    "scene_org": SceneOrgAdapter,
    "csdb": CsdbAdapter,
    "demozoo": DemozooAdapter,
    "youtube_data_api": YouTubeAdapter,
}


def build_adapters(sources_config: dict[str, Any], *, youtube_api_key: str | None = None) -> dict[str, SourceAdapter]:
    """Instantiate adapters for every *enabled* source in config/sources.yaml."""
    adapters: dict[str, SourceAdapter] = {}
    for name, cfg in sources_config.get("sources", {}).items():
        cls = ADAPTERS.get(name)
        if cls is None:
            continue
        if not cfg.get("enabled", True):
            continue
        if cls is YouTubeAdapter:
            adapters[name] = cls(cfg, api_key=youtube_api_key)
        else:
            adapters[name] = cls(cfg)
    return adapters


__all__ = [
    "ADAPTERS",
    "CsdbAdapter",
    "DemozooAdapter",
    "LinkCheck",
    "MatchDecision",
    "NoApiKey",
    "ProbeResult",
    "PouetAdapter",
    "SceneOrgAdapter",
    "SourceAdapter",
    "SourceError",
    "SourceUnreachable",
    "VideoMeta",
    "YouTubeAdapter",
    "YouTubeQuotaExceeded",
    "build_adapters",
    "build_search_query",
    "check_link",
    "match_video",
    "parse_iso8601_duration",
    "parse_prod",
    "title_similarity",
]
