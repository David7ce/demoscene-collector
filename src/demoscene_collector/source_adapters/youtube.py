"""YouTube adapter: link verification, optional API search, match scoring.

Two access modes, both official:

* **oEmbed (keyless, quota-free)** — ``GET https://www.youtube.com/oembed?url=…``
  confirmed working on 2026-10-10. Returns title/channel/thumbnail only, which
  is enough to confirm a video exists and to compare titles. Used to verify the
  explicit links that Pouët already publishes.
* **Data API v3 (key required)** — used only when ``YOUTUBE_API_KEY`` is set.
  Verified on 2026-10-10 that requests without a key fail with
  ``403 PERMISSION_DENIED``. Documented default quota: 100 ``search.list`` calls
  per day (10,000 units/day for other endpoints).

YouTube HTML is never scraped and no unofficial endpoint is used.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from ..http_client import HttpClient, HttpClientError, PermanentFailure, TransientFailure
from ..normalization import normalize_title, normalize_whitespace
from .base import ProbeResult, SourceAdapter, probe_http

log = logging.getLogger(__name__)

_ISO_DURATION_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


class NoApiKey(Exception):
    """Raised when the API is needed but no key is configured."""


class YouTubeQuotaExceeded(Exception):
    """Raised when the YouTube Data API reports exhausted quota."""


@dataclass
class VideoMeta:
    """Metadata observed for one video."""

    url: str
    title: str | None = None
    channel_name: str | None = None
    published_at: str | None = None
    duration_seconds: int | None = None
    description: str = ""
    observed_via: str = "oembed"   # oembed | data_api
    available: bool = True
    detail: str = ""


def parse_iso8601_duration(value: str | None) -> int | None:
    """``PT1H2M3S`` -> seconds. Returns None for empty/invalid input."""
    if not value:
        return None
    match = _ISO_DURATION_RE.fullmatch(value.strip())
    if not match:
        return None
    hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds


class YouTubeAdapter(SourceAdapter):
    name = "youtube_data_api"
    source_type = "rest_api"
    base_url = "https://www.googleapis.com/youtube/v3/"
    access_method = "api_key"

    def __init__(self, config: dict[str, Any], api_key: str | None = None):
        super().__init__(config)
        self.api_key = api_key
        self.oembed_url = self.config.get("oembed_url", "https://www.youtube.com/oembed")
        self.search_budget = int(self.config.get("search_quota_per_day", 100))
        self.searches_used = 0

    # -- probe --------------------------------------------------------------
    def probe(self, client: HttpClient) -> ProbeResult:
        if self.api_key:
            probe = self._probe_with_key(client)
            probe.auth_required = True
            probe.api_documented = True
            return probe
        probe = self._probe_without_key(client)
        probe.auth_required = True
        probe.api_documented = True
        probe.detail += (
            "; no YOUTUBE_API_KEY in this environment: search-based video discovery "
            "is disabled, explicit-link verification via keyless oEmbed only"
        )
        return probe

    def _probe_without_key(self, client: HttpClient) -> ProbeResult:
        url = self.base_url + "search"
        try:
            result = client.get(url, params={"part": "snippet", "maxResults": 1, "q": "demo"})
        except PermanentFailure as exc:
            if exc.status == 403:
                return self._ok_probe(
                    "API reachable; requests without a key return 403 PERMISSION_DENIED "
                    "(key required, verified 2026-10-10)"
                )
            return self._fail_probe(str(exc), status="degraded")
        except HttpClientError as exc:
            return self._fail_probe(f"{type(exc).__name__}: {exc}")
        if result.status == 403:
            return self._ok_probe("API reachable; key required (403 without key, verified 2026-10-10)")
        return self._fail_probe(f"unexpected HTTP {result.status}", status="degraded")

    def _probe_with_key(self, client: HttpClient) -> ProbeResult:
        url = self.base_url + "videos"
        try:
            result = client.get(url, params={"part": "id", "id": "dQw4w9WgXcQ", "key": self.api_key})
        except PermanentFailure as exc:
            if exc.status in (400, 403):
                return self._fail_probe(f"API rejected the configured key (HTTP {exc.status})",
                                        status="degraded")
            return self._fail_probe(str(exc), status="degraded")
        except HttpClientError as exc:
            return self._fail_probe(f"{type(exc).__name__}: {exc}")
        return self._ok_probe(f"API reachable with key (HTTP {result.status})")

    # -- oEmbed verification (keyless) ---------------------------------------
    def oembed(self, client: HttpClient, video_url: str) -> VideoMeta:
        """Fetch oEmbed metadata; ``available=False`` when the video is gone."""
        try:
            result = client.get(self.oembed_url, params={"url": video_url, "format": "json"},
                                cache=True, cache_ttl=7 * 86400)
        except PermanentFailure as exc:
            if exc.status in (400, 404):
                return VideoMeta(url=video_url, available=False, observed_via="oembed",
                                 detail=f"HTTP {exc.status}: video not found or embed disabled")
            if exc.status == 403:
                return VideoMeta(url=video_url, available=False, observed_via="oembed",
                                 detail="HTTP 403: access denied")
            return VideoMeta(url=video_url, available=False, observed_via="oembed",
                             detail=f"HTTP {exc.status}")
        except (TransientFailure, HttpClientError) as exc:
            return VideoMeta(url=video_url, available=False, observed_via="oembed",
                             detail=f"unreachable: {type(exc).__name__}: {exc}")
        try:
            data = json.loads(result.text)
        except ValueError as exc:
            return VideoMeta(url=video_url, available=False, observed_via="oembed",
                             detail=f"malformed JSON: {exc}")
        if not isinstance(data, dict):
            return VideoMeta(url=video_url, available=False, observed_via="oembed",
                             detail="unexpected oEmbed payload")
        return VideoMeta(
            url=video_url,
            title=data.get("title"),
            channel_name=data.get("author_name"),
            observed_via="oembed",
            available=True,
            detail="oEmbed 200",
        )

    # -- Data API v3 (requires key) ------------------------------------------
    def search(self, client: HttpClient, query: str, *, max_results: int = 5) -> list[VideoMeta]:
        """Search YouTube (costs 100 quota units per call)."""
        if not self.api_key:
            raise NoApiKey("YOUTUBE_API_KEY is not set; search-based discovery is unavailable")
        if self.searches_used >= self.search_budget:
            raise YouTubeQuotaExceeded(
                f"configured daily search budget exhausted ({self.search_budget} calls)"
            )
        params = {
            "part": "snippet",
            "type": "video",
            "maxResults": max(1, min(int(max_results), 10)),
            "q": query,
            "key": self.api_key,
            "relevanceLanguage": "en",
            "safeSearch": "none",
        }
        try:
            result = client.get(self.base_url + "search", params=params)
        except PermanentFailure as exc:
            if exc.status == 403:
                raise YouTubeQuotaExceeded("HTTP 403 from search.list (key invalid or quota exhausted)")
            raise
        self.searches_used += 1
        import json

        try:
            payload = json.loads(result.text)
        except ValueError as exc:
            raise HttpClientError(f"search.list returned malformed JSON: {exc}") from exc
        items = payload.get("items") or []
        metas: list[VideoMeta] = []
        video_ids = [i.get("id", {}).get("videoId") for i in items if i.get("id", {}).get("videoId")]
        details = self._video_details(client, video_ids) if video_ids else {}
        for item in items:
            video_id = item.get("id", {}).get("videoId")
            if not video_id:
                continue
            snippet = item.get("snippet", {})
            detail = details.get(video_id, {})
            metas.append(
                VideoMeta(
                    url=f"https://www.youtube.com/watch?v={video_id}",
                    title=snippet.get("title"),
                    channel_name=snippet.get("channelTitle"),
                    published_at=snippet.get("publishedAt"),
                    duration_seconds=parse_iso8601_duration(detail.get("duration")),
                    description=snippet.get("description", "")[:500],
                    observed_via="data_api",
                )
            )
        return metas

    def _video_details(self, client: HttpClient, video_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not self.api_key:
            return {}
        if self.searches_used >= self.search_budget * 10:
            raise YouTubeQuotaExceeded("configured unit budget exhausted for videos.list")
        params = {
            "part": "contentDetails,snippet",
            "id": ",".join(video_ids[:10]),
            "key": self.api_key,
        }
        try:
            result = client.get(self.base_url + "videos", params=params)
        except (PermanentFailure, HttpClientError) as exc:
            log.warning("videos.list failed: %s", exc)
            return {}
        import json

        try:
            payload = json.loads(result.text)
        except ValueError:
            return {}
        out: dict[str, dict[str, Any]] = {}
        for item in payload.get("items") or []:
            vid = item.get("id")
            if vid:
                out[vid] = {
                    "duration": (item.get("contentDetails") or {}).get("duration"),
                    "publishedAt": (item.get("snippet") or {}).get("publishedAt"),
                }
        return out


# ---------------------------------------------------------------------------
# Matching (pure functions, unit-tested)
# ---------------------------------------------------------------------------

def title_similarity(production_title: str, video_title: str) -> float:
    a = normalize_title(production_title)
    b = normalize_title(video_title)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    # containment of the full production title inside a longer capture title
    if a in b or b in a:
        ratio = max(ratio, 0.92)
    return ratio


@dataclass
class MatchDecision:
    status: str        # verified_match | probable_match | needs_review | rejected
    confidence: float
    evidence: str


def match_video(
    *,
    production_title: str,
    video_title: str | None,
    creators: list[str] | None = None,
    year: int | None = None,
    explicit_link: bool = False,
    thresholds: dict[str, float] | None = None,
) -> MatchDecision:
    """Classify a video candidate for a production.

    An exact title alone is never treated as sufficient: creator names and the
    release year present in the video title raise confidence, their absence
    lowers it. Explicit source links start from a higher prior because the link
    was published on the production's own page.
    """
    thr = thresholds or {}
    verified_thr = float(thr.get("video_verified_match", 0.80))
    probable_thr = float(thr.get("video_probable_match", 0.60))

    if video_title is None:
        return MatchDecision("needs_review", 0.0, "video metadata unavailable (could not verify)")

    sim = title_similarity(production_title, video_title)
    norm_video = normalize_title(video_title)
    creator_hit = any(normalize_title(c) and normalize_title(c) in norm_video
                      for c in (creators or []))
    year_hit = bool(year) and str(year) in (video_title or "")
    if year_hit and year and not re.search(r"\b19\d{2}\b|\b20\d{2}\b", video_title or ""):
        year_hit = False

    confidence = 0.68 * sim
    if creator_hit:
        confidence += 0.20
    if year_hit:
        confidence += 0.12
    if explicit_link:
        confidence += 0.10
    confidence = round(min(confidence, 1.0), 4)

    evidence_bits = [
        f"title similarity {sim:.2f} ({production_title!r} vs {video_title!r})",
        f"creator mentioned: {creator_hit}",
        f"year mentioned: {year_hit}",
        f"explicit link on source page: {explicit_link}",
    ]
    evidence = "; ".join(evidence_bits)

    if confidence >= verified_thr:
        return MatchDecision("verified_match", confidence, evidence)
    if confidence >= probable_thr:
        return MatchDecision("probable_match", confidence, evidence)
    if explicit_link:
        return MatchDecision("needs_review", confidence, evidence)
    return MatchDecision("rejected", confidence, evidence)


def build_search_query(production_title: str, creators: list[str], year: int | None) -> str:
    """Deterministic search query used for key-based discovery."""
    bits = [production_title]
    if creators:
        bits.append(creators[0])
    if year:
        bits.append(str(year))
    bits.append("demo")
    return " ".join(b for b in bits if b)
