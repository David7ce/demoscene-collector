"""Normalisation helpers (Stage B).

All functions are pure and deterministic. Original spelling and punctuation are
always preserved in the canonical display columns; normalised columns are used
for matching only.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
# Characters that regularly appear in demoscene titles and break naive matching.
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
# Unicode categories treated as invisible markup (control + format characters).
_INVISIBLE_CATS = frozenset({"Cc", "Cf"})
# ASCII whitespace that must survive invisible-character stripping.
_ASCII_WHITESPACE = frozenset(" \t\n\r\f\v")
_TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "ref",
    }
)


def _strip_invisibles(text: str) -> str:
    """Drop control/format characters (zero-width spaces, LTR/RTL marks, …).

    Real Pouët dump records contain credit nicknames consisting solely of an
    invisible ``U+200E`` left-to-right mark; such characters are markup, not
    content, so they are removed before any other normalisation. ASCII
    whitespace controls (``\\t\\n\\r`` …) are kept — they carry layout meaning
    and are handled by the whitespace collapse.
    """
    out: list[str] = []
    for ch in text:
        if ch in _ASCII_WHITESPACE:
            out.append(ch)
        elif unicodedata.category(ch) in _INVISIBLE_CATS:
            continue
        else:
            out.append(ch)
    return "".join(out)


def normalize_whitespace(value: str | None) -> str:
    """Unicode-normalise, strip ends, collapse repeated whitespace."""
    if not value:
        return ""
    text = unicodedata.normalize("NFC", str(value))
    text = _strip_invisibles(text)
    return _WS_RE.sub(" ", text).strip()


def display_title(value: str | None) -> str:
    """Canonical *display* title: cleaned whitespace, original case/punctuation."""
    return normalize_whitespace(value)


def normalize_title(value: str | None) -> str:
    """Matching value for titles: lowercase, punctuation-free, whitespace-collapsed."""
    cleaned = normalize_whitespace(value)
    text = cleaned.lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    # Convert common stylised separators to spaces before stripping punctuation.
    text = text.replace("_", " ").replace("/", " ").replace("-", " ").replace("+", " ")
    text = _PUNCT_RE.sub("", text)
    result = _WS_RE.sub(" ", text).strip()
    if result:
        return result
    # Symbol-only titles and handles are real in the demoscene ("!!!", "__",
    # "+$"). Falling back to the whitespace-cleaned original keeps them
    # distinct; collapsing them all into "" would merge unrelated records.
    return cleaned.casefold()


def normalize_name(value: str | None) -> str:
    """Matching value for person/group names (same rules as titles)."""
    return normalize_title(value)


def normalize_alias(value: str | None) -> str:
    return normalize_title(value)


# ---------------------------------------------------------------------------
# URL normalisation and classification
# ---------------------------------------------------------------------------

_DIRECT_EXTENSIONS = frozenset(
    {
        ".zip", ".gz", ".xz", ".bz2", ".7z", ".rar", ".lha", ".lzx", ".arj",
        ".exe", ".com", ".bat", ".scr", ".jar", ".apk",
        ".adf", ".adz", ".d64", ".d71", ".d81", ".g64", ".t64", ".sfx",
        ".stx", ".st", ".rom", ".bin", ".img", ".ima", ".ipf", ".adf.gz",
        ".prg", ".crt", ".sid", ".mod", ".xm", ".it", ".s3m", ".med", ".nsf",
        ".png", ".jpg", ".jpeg", ".gif", ".webp",
        ".mp4", ".avi", ".mpg", ".mpeg", ".mkv", ".webm", ".mov",
        ".mp3", ".ogg", ".opus", ".wav", ".flac", ".mod.zip",
        ".pdf", ".txt", ".nfo", ".diz", ".md", ".c", ".cpp", ".h", ".asm", ".bas", ".pas",
        ".tar",
    }
)

# Hosts whose /view/ (or similar) paths are known landing pages.
_LANDING_PREFIXES = ("/view/", "/prod.php", "/release.php", "/productions/")


@dataclass(frozen=True)
class UrlInfo:
    """Result of :func:`classify_url`."""

    url: str
    normalized_url: str
    scheme: str
    host: str
    kind: str  # direct_download | landing_page | unknown
    reason: str


def normalize_url(url: str | None, *, keep_fragment: bool = False) -> str:
    """Normalise a URL: lowercase scheme/host, drop default ports, drop tracking
    parameters and (by default) the fragment. Query parameters that identify a
    resource are preserved and sorted for stability.
    """
    if not url:
        return ""
    raw = normalize_whitespace(url)
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw
    scheme = (parts.scheme or "").lower()
    netloc = (parts.netloc or "").lower()
    if scheme in ("http", "https") and netloc.endswith(":80"):
        netloc = netloc[: -len(":80")]
    elif scheme in ("http", "https") and netloc.endswith(":443"):
        netloc = netloc[: -len(":443")]
    path = parts.path or ""
    query_pairs = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    ]
    query_pairs.sort()
    query = urlencode(query_pairs)
    fragment = parts.fragment if keep_fragment else ""
    return urlunsplit((scheme, netloc, path, query, fragment))


def classify_url(url: str | None, content_type: str | None = None) -> UrlInfo:
    """Classify a URL as a direct artifact link, a landing page, or unknown.

    Rules (conservative — we never claim "direct" without evidence):

    * scheme not in (http, https)          -> kind "unknown" (e.g. ftp://)
    * ``files.scene.org/view/...``         -> landing page
    * ``files.scene.org/get/...``          -> direct download (302 -> mirror)
    * path ends with a known artifact ext. -> direct download
    * response Content-Type is HTML        -> landing page
    * response Content-Type is a known binary type -> direct download
    """
    raw = normalize_whitespace(url)
    if not raw:
        return UrlInfo("", "", "", "", "unknown", "empty")
    normalized = normalize_url(raw)
    try:
        parts = urlsplit(normalized)
    except ValueError:  # pragma: no cover - urlsplit rarely raises after normalize
        return UrlInfo(raw, normalized, "", "", "unknown", "unparseable")
    scheme = parts.scheme.lower()
    host = (parts.netloc or "").lower()
    path = parts.path
    if scheme not in ("http", "https"):
        return UrlInfo(raw, normalized, scheme, host, "unknown", f"scheme:{scheme or 'none'}")

    ct = (content_type or "").lower().split(";")[0].strip()

    if any(p in path for p in ("/view/",)):
        return UrlInfo(raw, normalized, scheme, host, "landing_page", "view-prefix")
    if any(path.startswith(p) for p in ("/get/", "/get:")):
        return UrlInfo(raw, normalized, scheme, host, "direct_download", "get-prefix")

    suffix = _suffix_of(path)
    if suffix in _DIRECT_EXTENSIONS:
        if ct and "html" in ct:
            return UrlInfo(raw, normalized, scheme, host, "landing_page", "html-content-type")
        return UrlInfo(raw, normalized, scheme, host, "direct_download", f"extension:{suffix}")

    if ct:
        if "html" in ct:
            return UrlInfo(raw, normalized, scheme, host, "landing_page", "html-content-type")
        if ct.startswith(("image/", "video/", "audio/", "application/")):
            return UrlInfo(raw, normalized, scheme, host, "direct_download", f"content-type:{ct}")

    if any(p in path for p in _LANDING_PREFIXES):
        return UrlInfo(raw, normalized, scheme, host, "landing_page", "landing-path")

    return UrlInfo(raw, normalized, scheme, host, "unknown", "no-evidence")


def _suffix_of(path: str) -> str:
    name = path.rstrip("/").rsplit("/", 1)[-1].lower()
    # handle compound suffixes like .tar.gz / .adf.gz
    for compound in (".tar.gz", ".tar.xz", ".mod.zip"):
        if name.endswith(compound):
            return compound
    idx = name.rfind(".")
    return name[idx:] if idx > 0 else ""


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedDate:
    year: int | None
    iso_date: str | None
    evidence: str


def parse_release_date(value: str | None) -> ParsedDate:
    """Parse ``YYYY-MM-DD`` or ``YYYY`` into (year, iso_date).

    Returns ``ParsedDate(None, None, "none")`` for anything not supported —
    years are only set when the source actually provided one.
    """
    if value is None:
        return ParsedDate(None, None, "none")
    text = normalize_whitespace(str(value))
    if not text:
        return ParsedDate(None, None, "empty")
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return ParsedDate(None, None, "invalid-date")
        return ParsedDate(d.year, d.isoformat(), "iso-date")
    m = re.fullmatch(r"(\d{4})", text)
    if m:
        year = int(m.group(1))
        if 1900 <= year <= 2100:
            return ParsedDate(year, None, "year-only")
        return ParsedDate(None, None, "implausible-year")
    return ParsedDate(None, None, "unparseable")


# ---------------------------------------------------------------------------
# Platform normalisation (controlled mapping)
# ---------------------------------------------------------------------------

#: raw platform name (lowercased) -> (canonical name, family)
PLATFORM_FAMILIES: dict[str, tuple[str, str]] = {
    # PC / MS-DOS family
    "ms-dos": ("MS-DOS", "PC/MS-DOS"),
    "ms dos": ("MS-DOS", "PC/MS-DOS"),
    "ms-dos/gus": ("MS-DOS", "PC/MS-DOS"),
    "pc": ("PC", "PC/MS-DOS"),
    "dos": ("MS-DOS", "PC/MS-DOS"),
    # Windows family
    "windows": ("Windows", "Windows"),
    "windows 32": ("Windows", "Windows"),
    "windows 64": ("Windows", "Windows"),
    "win32": ("Windows", "Windows"),
    "win64": ("Windows", "Windows"),
    # Amiga family
    "amiga ocs/ecs": ("Amiga OCS/ECS", "Amiga"),
    "amiga aga": ("Amiga AGA", "Amiga"),
    "amiga odyssey": ("Amiga Odyssey", "Amiga"),
    "amiga": ("Amiga", "Amiga"),
    "amigaos 4": ("AmigaOS 4", "Amiga"),
    # Commodore 64 family
    "commodore 64": ("Commodore 64", "Commodore 64"),
    "c64": ("Commodore 64", "Commodore 64"),
    "c16/116/plus4": ("C16/Plus4", "Other"),
    # Atari ST family
    "atari st": ("Atari ST", "Atari ST"),
    "atari ste": ("Atari STe", "Atari ST"),
    "atari falcon 030": ("Atari Falcon 030", "Atari ST"),
    "atari tt": ("Atari TT", "Atari ST"),
    "atari lynx": ("Atari Lynx", "Other"),
    "atari jaguar": ("Atari Jaguar", "Other"),
    # Other historically significant platforms
    "zx spectrum": ("ZX Spectrum", "Other"),
    "amstrad cpc": ("Amstrad CPC", "Other"),
    "atari xl/xe": ("Atari XL/XE", "Other"),
    "apple ii": ("Apple II", "Other"),
    "apple ii+": ("Apple II", "Other"),
    "bbc micro": ("BBC Micro", "Other"),
    "oric": ("Oric", "Other"),
    "vc20": ("Commodore VIC-20", "Other"),
    "vic-20": ("Commodore VIC-20", "Other"),
    "gameboy": ("Game Boy", "Other"),
    "gameboy advance": ("Game Boy Advance", "Other"),
    "nintendo 64": ("Nintendo 64", "Other"),
    "playstation": ("PlayStation", "Other"),
    "playstation 2": ("PlayStation 2", "Other"),
    "sega genesis/mega drive": ("Sega Mega Drive", "Other"),
    "snes/super famicom": ("Super Nintendo", "Other"),
    "ti-99/4a": ("TI-99/4A", "Other"),
    "Enterprise": ("Enterprise 64/128", "Other"),
    # Modern / other
    "linux": ("Linux", "Other"),
    "macosx intel": ("macOS", "Other"),
    "macosx ppc": ("macOS", "Other"),
    "macos": ("macOS", "Other"),
    "javascript": ("JavaScript/Web", "Other"),
    "html5": ("HTML5/Web", "Other"),
    "flash": ("Flash", "Other"),
    "java": ("Java", "Other"),
    "shadertoy": ("Shadertoy/Web", "Other"),
    "wild": ("Wild", "Other"),
    "animation/video": ("Animation/Video", "Other"),
    "android": ("Android", "Other"),
    "ios": ("iOS", "Other"),
}


def normalize_platform(raw_name: str | None) -> tuple[str, str]:
    """Map a raw platform label to ``(canonical_name, platform_family)``.

    Unknown labels keep their original (whitespace-normalised) name and fall
    back to the ``Other`` family rather than being dropped.
    """
    name = normalize_whitespace(raw_name)
    if not name:
        return ("Unknown", "Other")
    key = name.lower()
    if key in PLATFORM_FAMILIES:
        return PLATFORM_FAMILIES[key]
    # tolerate case variations such as "MS-Dos" (observed in the Pouët dump)
    for alias, value in PLATFORM_FAMILIES.items():
        if alias.lower() == key:
            return value
    return (name, "Other")


@dataclass(frozen=True)
class VideoUrlInfo:
    platform: str
    video_id: str | None
    canonical_url: str
    reason: str


_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{6,20}$")


def canonical_video_url(url: str | None) -> VideoUrlInfo:
    """Extract (platform, video_id, canonical_url) from a video link.

    Supported hosts: youtube.com, youtu.be, youtube-nocookie.com, vimeo.com.
    Raises nothing: unknown hosts yield ``platform="other"`` with an empty
    canonical URL only when the URL is unusable.
    """
    raw = normalize_whitespace(url)
    if not raw:
        return VideoUrlInfo("unknown", None, "", "empty")
    try:
        parts = urlsplit(normalize_url(raw))
    except ValueError:
        return VideoUrlInfo("unknown", None, "", "unparseable")
    host = (parts.netloc or "").lower().removeprefix("www.")
    path = parts.path
    video_id: str | None = None
    if host in ("youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com"):
        if path.startswith("/watch"):
            for key, value in parse_qsl(parts.query):
                if key == "v" and value:
                    video_id = value
                    break
        elif path.startswith(("/embed/", "/v/", "/shorts/", "/live/")):
            segments = [s for s in path.split("/") if s]
            if len(segments) >= 2:
                video_id = segments[1]
    elif host == "youtu.be":
        segments = [s for s in path.split("/") if s]
        if segments:
            video_id = segments[0]
    elif host == "vimeo.com":
        segments = [s for s in path.split("/") if s]
        if segments and segments[0].isdigit():
            video_id = segments[0]
        return VideoUrlInfo(
            "vimeo",
            video_id,
            f"https://vimeo.com/{video_id}" if video_id else "",
            "vimeo",
        )
    else:
        return VideoUrlInfo("other", None, normalize_url(raw), f"host:{host or 'none'}")

    if video_id:
        video_id = video_id.split("?")[0]
        if _YOUTUBE_ID_RE.match(video_id):
            return VideoUrlInfo(
                "youtube",
                video_id,
                f"https://www.youtube.com/watch?v={video_id}",
                "canonical",
            )
        return VideoUrlInfo("youtube", None, "", f"invalid-id:{video_id}")
    return VideoUrlInfo("youtube", None, "", "no-video-id")


def normalize_artifact_type(link_type: str | None) -> str:
    """Map a source-provided link type label to a controlled artifact type.

    Returned values match the ``releases.artifact_type`` CHECK constraint:
    ``executable | archive | disk image | source code | video capture |
    documentation | landing page | other``.
    """
    text = normalize_whitespace(link_type).lower()
    if not text:
        return "other"
    if "youtube" in text or "youtu.be" in text:
        return "video capture"
    if text.startswith("video"):
        return "video capture"
    if text in ("online", "online version", "web", "minisite", "website"):
        return "landing page"
    if "source" in text or text in ("github", "git"):
        return "source code"
    if "soundtrack" in text or "music" in text or "soundcloud" in text:
        return "documentation"
    if "disk" in text or "party version" in text or "whdload" in text:
        return "archive"
    return "executable"
