"""Pouët adapter — primary discovery source.

Access method: the weekly JSON dump published at https://data.pouet.net/
(preferred: an official published dataset) plus the read-only API at
https://api.pouet.net/ (15 requests / 60 s) for optional detail lookups.

The dump file is stored under ``data/input/`` and reused across runs; the dump
index at ``data.pouet.net/json.php`` is only requested when the file is missing
and the run is not ``--offline``.
"""

from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path
from typing import Any, Iterator

from ..http_client import HttpClient, HttpClientError
from ..models import CreatorRef, ProductionCandidate, ReleaseCandidate, VideoCandidate
from ..normalization import normalize_artifact_type, normalize_whitespace
from .base import ProbeResult, SourceAdapter, SourceError, probe_http

log = logging.getLogger(__name__)

VIDEO_LINK_MARKERS = ("youtube", "youtu.be", "vimeo")

_ARTIFACT_BY_SUFFIX = {
    ".zip": "archive", ".rar": "archive", ".lha": "archive", ".lzh": "archive",
    ".7z": "archive", ".gz": "archive", ".tar": "archive", ".arj": "archive",
    ".adf": "disk image", ".adz": "disk image", ".d64": "disk image",
    ".d71": "disk image", ".d81": "disk image", ".g64": "disk image",
    ".ipf": "disk image", ".adf.gz": "disk image", ".st": "disk image",
    ".stx": "disk image", ".img": "disk image",
    ".exe": "executable", ".com": "executable", ".jar": "executable",
    ".prg": "executable", ".sys": "executable",
    ".avi": "video capture", ".mpg": "video capture", ".mpeg": "video capture",
    ".mp4": "video capture", ".mkv": "video capture", ".webm": "video capture",
    ".mov": "video capture",
    ".mod": "other", ".xm": "other", ".it": "other", ".mp3": "other",
}


def artifact_type_for(url: str, label: str | None = None) -> str:
    from urllib.parse import urlsplit

    label_norm = normalize_whitespace(label).lower()
    if label_norm:
        mapped = normalize_artifact_type(label_norm)
        if mapped not in ("executable", "other"):
            return mapped
        if "source" in label_norm or "github" in label_norm:
            return "source code"
    name = urlsplit(url).path.lower()
    for suffix, artifact in _ARTIFACT_BY_SUFFIX.items():
        if name.endswith(suffix):
            return artifact
    if label_norm and ("source" in label_norm or "github" in label_norm):
        return "source code"
    if label_norm and ("video" in label_norm or "youtube" in label_norm):
        return "video capture"
    return "other"


def is_video_link(url: str, label: str | None = None) -> bool:
    lowered = f"{url} {label or ''}".lower()
    return any(marker in lowered for marker in VIDEO_LINK_MARKERS)


class PouetAdapter(SourceAdapter):
    name = "pouet"
    source_type = "database_dump"
    base_url = "https://www.pouet.net/"
    access_method = "published_json_dump"

    # -- probe --------------------------------------------------------------
    def probe(self, client: HttpClient) -> ProbeResult:
        stats_url = (self.config.get("api_base_url") or "https://api.pouet.net/") + "v1/stats"
        probe = probe_http(self, client, stats_url)
        if not probe.reachable:
            return probe
        probe.api_documented = True
        probe.auth_required = False
        probe.detail += " (official API, keyless, documented rate limit 15 req/60s)"
        return probe

    # -- dump handling ------------------------------------------------------
    def dump_index(self, client: HttpClient) -> dict[str, Any]:
        url = self.config.get("dump_index_url", "https://data.pouet.net/json.php")
        try:
            result = client.get(url, cache=True, cache_ttl=6 * 3600)
        except HttpClientError as exc:
            raise SourceError(f"cannot read Pouët dump index: {exc}") from exc
        try:
            return json.loads(result.text)
        except ValueError as exc:
            raise SourceError(f"dump index is not valid JSON: {exc}") from exc

    def latest_dump_url(self, index: dict[str, Any], dataset: str = "prods") -> str:
        dumps = index.get("dumps", {})
        for dump_date in sorted(dumps, reverse=True):
            entry = dumps[dump_date].get(dataset)
            if entry and entry.get("url"):
                return str(entry["url"])
        raise SourceError(f"dump index has no dataset {dataset!r}")

    def ensure_dump(self, client: HttpClient, dump_dir: Path, dataset: str = "prods") -> Path:
        """Return the local dump path, downloading it only when missing."""
        matches = sorted(dump_dir.glob(f"pouetdatadump-{dataset}-*.json.*"))
        if matches:
            return matches[-1]
        url = self.latest_dump_url(self.dump_index(client), dataset)
        target = dump_dir / url.rsplit("/", 1)[-1]
        dump_dir.mkdir(parents=True, exist_ok=True)
        log.info("downloading Pouët dump %s", url)
        result = client.get(url, cache=False)
        target.write_bytes(result.body)
        return target

    # -- parsing ------------------------------------------------------------
    def iter_dump(self, path: Path) -> Iterator[dict[str, Any]]:
        """Stream productions out of a dump file (``.json``, ``.json.gz`` or ``.xz``)."""
        if path.suffix == ".gz":
            handle = gzip.open(path, "rt", encoding="utf-8")
        elif path.suffix == ".xz":
            import lzma

            handle = lzma.open(path, "rt", encoding="utf-8")
        else:
            handle = path.open("r", encoding="utf-8")
        with handle:
            payload = json.load(handle)
        if isinstance(payload, dict) and "prods" in payload:
            yield from payload["prods"]
        elif isinstance(payload, list):
            yield from payload
        else:
            raise SourceError(f"unexpected dump structure in {path.name}")

    def dump_date(self, path: Path) -> str:
        if path.suffix == ".gz":
            handle = gzip.open(path, "rt", encoding="utf-8")
        elif path.suffix == ".xz":
            import lzma

            handle = lzma.open(path, "rt", encoding="utf-8")
        else:
            handle = path.open("r", encoding="utf-8")
        with handle:
            payload = json.load(handle)
        return str(payload.get("dump_date", "unknown")) if isinstance(payload, dict) else "unknown"

    def discover(self, client: HttpClient, **kwargs: Any) -> Iterator[ProductionCandidate]:
        dump_dir = Path(kwargs["dump_dir"])
        offline = bool(kwargs.get("offline", False))
        limit = kwargs.get("limit")
        if offline:
            matches = sorted(dump_dir.glob("pouetdatadump-prods-*"))
            if not matches:
                raise SourceError(
                    f"--offline set but no Pouët dump found in {dump_dir}; "
                    "download pouetdatadump-prods-*.json.gz first"
                )
            path = matches[-1]
        else:
            path = self.ensure_dump(client, dump_dir, "prods")
        log.info("reading Pouët dump %s", path.name)
        produced = 0
        for raw in self.iter_dump(path):
            try:
                candidate = parse_prod(raw)
            except Exception as exc:  # one bad record must not abort discovery
                log.warning("skipping unparsable Pouët record %s: %s", raw.get("id"), exc)
                continue
            yield candidate
            produced += 1
            if limit and produced >= int(limit):
                return

    # -- optional detail lookup ---------------------------------------------
    def fetch_prod_detail(self, client: HttpClient, prod_id: str) -> dict[str, Any] | None:
        api_base = self.config.get("api_base_url", "https://api.pouet.net/")
        url = f"{api_base}v1/prod"
        try:
            result = client.get(url, params={"id": prod_id})
        except HttpClientError as exc:
            log.warning("Pouët API lookup failed for %s: %s", prod_id, exc)
            return None
        try:
            payload = json.loads(result.text)
        except ValueError:
            return None
        return payload if payload.get("success") else None


# ---------------------------------------------------------------------------
# Parsing helpers (pure functions, unit-tested)
# ---------------------------------------------------------------------------

def _cross_ids(raw: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in ("demozoo", "csdb", "zxdemo", "sceneorg"):
        value = raw.get(key)
        if value and str(value) not in ("0", "", "None"):
            out[key] = str(value)
    return out


def _groups(raw: dict[str, Any]) -> list[CreatorRef]:
    refs: list[CreatorRef] = []
    for group in raw.get("groups") or []:
        name = normalize_whitespace(group.get("name"))
        if name:
            refs.append(CreatorRef(name=name, role="group"))
    return refs


def _credits(raw: dict[str, Any]) -> list[CreatorRef]:
    refs: list[CreatorRef] = []
    for credit in raw.get("credits") or []:
        user = credit.get("user") or {}
        name = normalize_whitespace(user.get("nickname"))
        if not name:
            continue
        role = normalize_whitespace(credit.get("role")) or "unknown"
        refs.append(CreatorRef(name=name, role=role))
    return refs


def _platform_names(raw: dict[str, Any]) -> list[str]:
    platforms = raw.get("platforms") or {}
    names: list[str] = []
    if isinstance(platforms, dict):
        for entry in platforms.values():
            name = normalize_whitespace(entry.get("name") if isinstance(entry, dict) else entry)
            if name:
                names.append(name)
    elif isinstance(platforms, list):
        for entry in platforms:
            name = normalize_whitespace(entry.get("name") if isinstance(entry, dict) else entry)
            if name:
                names.append(name)
    return names


def _releases(raw: dict[str, Any]) -> list[ReleaseCandidate]:
    releases: list[ReleaseCandidate] = []
    primary = normalize_whitespace(raw.get("download"))
    if primary:
        releases.append(
            ReleaseCandidate(url=primary, source_id="pouet",
                             artifact_type=artifact_type_for(primary, "primary"),
                             link_label="primary download")
        )
    for link in raw.get("downloadLinks") or []:
        label = normalize_whitespace(link.get("type"))
        url = normalize_whitespace(link.get("link"))
        if not url or is_video_link(url, label):
            continue  # video links are handled by _videos()
        releases.append(
            ReleaseCandidate(url=url, source_id="pouet",
                             artifact_type=artifact_type_for(url, label),
                             link_label=label or None)
        )
    return releases


def _videos(raw: dict[str, Any]) -> list[VideoCandidate]:
    videos: list[VideoCandidate] = []
    seen: set[str] = set()
    candidates: list[tuple[str, str | None]] = []
    primary = normalize_whitespace(raw.get("download"))
    if primary and is_video_link(primary, None):
        candidates.append((primary, "primary download"))
    for link in raw.get("downloadLinks") or []:
        label = normalize_whitespace(link.get("type"))
        url = normalize_whitespace(link.get("link"))
        if url and is_video_link(url, label):
            candidates.append((url, label or None))
    for url, label in candidates:
        lowered = f"{url}".lower()
        if "youtu" not in lowered and "vimeo" not in lowered:
            continue  # e.g. dhs.tv pages: not a video host we support
        try:
            video = VideoCandidate(
                url=url, source_id="pouet", link_type=label,
                video_kind=_video_kind(label, url),
                match_confidence=0.9,
                match_evidence="explicit link published on the Pouët production page",
                verification_status="untested",
            )
        except ValueError as exc:
            log.debug("ignoring unusable video link %s: %s", url, exc)
            continue
        if video.canonical_url in seen:
            continue
        seen.add(video.canonical_url)
        videos.append(video)
    return videos


def _video_kind(label: str | None, url: str) -> str:
    text = (label or "").lower()
    if "live" in text or "recording" in text:
        return "party recording"
    if "review" in text or "commentary" in text:
        return "review"
    if "interview" in text:
        return "interview"
    if "trailer" in text:
        return "trailer"
    return "demo capture"


def _description(raw: dict[str, Any], groups: list[CreatorRef], type_name: str) -> str:
    """A short, factual, fully source-derived summary (never invented detail)."""
    label = {"4k": "4K", "8k": "8K", "16k": "16K", "32k": "32K", "64k": "64K",
             "256b": "256-byte", "1k": "1K", "128b": "128-byte"}.get(
                 type_name.lower(), type_name or "Demo")
    head = f"{label} production"
    if groups:
        head += " by " + ", ".join(g.name for g in groups[:4])
    parts: list[str] = [head]
    date_text = normalize_whitespace(raw.get("releaseDate"))
    if date_text:
        parts.append(f"released {date_text}")
    party = raw.get("party") or {}
    party_name = normalize_whitespace(party.get("name")) if isinstance(party, dict) else ""
    place = raw.get("party_place")
    compo = normalize_whitespace(raw.get("party_compo_name"))
    year = ""
    if isinstance(party, dict):
        year = normalize_whitespace(raw.get("party_year")) or normalize_whitespace(str(party.get("year") or ""))
    if party_name:
        result = " ".join(x for x in (party_name, year) if x)
        if place and str(place) not in ("", "None"):
            ordinal = {"1": "1st", "2": "2nd", "3": "3rd"}.get(str(place), f"{place}th")
            result += f": {ordinal} place" + (f" in {compo}" if compo and compo != "none" else "")
        elif compo and compo != "none":
            result += f": {compo}"
        parts.append(result)
    text = ". ".join(parts)
    return text[:1].upper() + text[1:] + "."


def parse_prod(raw: dict[str, Any]) -> ProductionCandidate:
    """Convert a Pouët dump record into a :class:`ProductionCandidate`."""
    prod_id = str(raw.get("id") or "").strip()
    if not prod_id:
        raise ValueError("record without id")
    title = normalize_whitespace(raw.get("name"))
    types = [normalize_whitespace(t) for t in (raw.get("types") or []) if normalize_whitespace(t)]
    primary_type = normalize_whitespace(raw.get("type"))
    type_name = primary_type or (types[0] if types else "other") or "other"
    if not primary_type and types:
        type_name = types[0]

    groups = _groups(raw)
    credits = _credits(raw)
    popularity = {
        "voteup": _int_or_none(raw.get("voteup")),
        "votedown": _int_or_none(raw.get("votedown")),
        "voteavg": _float_or_none(raw.get("voteavg")),
        "rank": _int_or_none(raw.get("rank")),
        "popularity": _float_or_none(raw.get("popularity")),
    }
    significance = {
        "placings": raw.get("placings") or [],
        "awards": raw.get("awards") or [],
        "party": raw.get("party"),
        "party_compo_name": raw.get("party_compo_name"),
        "invitation": raw.get("invitation"),
        "types": types,
    }

    candidate = ProductionCandidate(
        source_id="pouet",
        source_record_id=prod_id,
        title=title,
        source_url=f"https://www.pouet.net/prod.php?which={prod_id}",
        production_type=primary_type or (types[0] if types else "other"),
        release_date=normalize_whitespace(raw.get("releaseDate")) or None,
        description=_description(raw, groups, type_name),
        groups=groups,
        credits=credits,
        platforms=_platform_names(raw),
        releases=_releases(raw),
        videos=_videos(raw),
        cross_ids=_cross_ids(raw),
        popularity=popularity,
        significance=significance,
        raw={
            "pouet_id": prod_id,
            "types": types,
            "screenshot": raw.get("screenshot"),
            "cdc": raw.get("cdc"),
            "added_date": raw.get("addedDate"),
        },
    )
    return candidate


def _int_or_none(value: Any) -> int | None:
    if value in (None, "", "None"):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value in (None, "", "None"):
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None
