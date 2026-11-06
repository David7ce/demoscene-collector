"""Scene.org file archive adapter.

Used for two things only:

1. classifying links as landing pages vs direct downloads (the URL model was
   observed on 2026-10-10: ``/view/<path>`` is a landing page, ``/get/<path>``
   302-redirects to a mirror and is the artifact itself);
2. verifying single links with ``HEAD`` (falling back to a small ranged ``GET``).

The archive is never crawled; no whole file is ever downloaded.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from ..http_client import HttpClient, HttpClientError, PermanentFailure, TransientFailure
from ..normalization import classify_url, normalize_url
from .base import ProbeResult, SourceAdapter, probe_http

log = logging.getLogger(__name__)


@dataclass
class LinkCheck:
    url: str
    final_url: str
    status: str          # verified | landing_ok | broken | inaccessible | redirected | blocked | untested | untested_protocol
    http_status: int | None
    url_kind: str        # direct_download | landing_page | unknown
    content_type: str | None = None
    content_length: int | None = None
    detail: str = ""
    landing_page_url: str | None = None


class SceneOrgAdapter(SourceAdapter):
    name = "scene_org"
    source_type = "file_archive"
    base_url = "https://files.scene.org/"
    access_method = "single_request_head"

    def probe(self, client: HttpClient) -> ProbeResult:
        return probe_http(self, client, self.base_url)

    # -- URL helpers ---------------------------------------------------------
    def to_direct_url(self, url: str) -> str:
        """Translate a ``/view/<path>`` landing URL into ``/get/<path>``.

        The translation was verified live on 2026-10-10; if the path does not
        match the model the original URL is returned unchanged.
        """
        normalized = normalize_url(url)
        parts = urlsplit(normalized)
        if "files.scene.org" not in (parts.netloc or "").lower():
            return normalized
        if parts.path.startswith("/view/"):
            return normalized.replace("/view/", "/get/", 1)
        return normalized

    def landing_url(self, url: str) -> str | None:
        normalized = normalize_url(url)
        parts = urlsplit(normalized)
        if "files.scene.org" not in (parts.netloc or "").lower():
            return None
        if parts.path.startswith("/get/"):
            return normalized.replace("/get/", "/view/", 1)
        if parts.path.startswith("/view/"):
            return normalized
        return None


def check_link(client: HttpClient, url: str, *, prefer_head: bool = True,
               allow_get: bool = True) -> LinkCheck:
    """Verify that a link resolves, without downloading the artifact.

    Conservative rules:

    * ``HEAD`` first; if the server rejects it, a single small ``GET`` with
      ``Range: bytes=0-0`` is attempted (never a full download);
    * a 200 with an HTML body means a **landing page**, not a verified artifact;
    * a redirect is recorded with its final URL;
    * 401/403 → ``blocked``; 404/410 and other 4xx → ``broken``;
    * network errors after retries → ``inaccessible``;
    * non-HTTP schemes (ftp://…) → ``untested_protocol``.
    """
    info = classify_url(url)
    if info.scheme not in ("http", "https"):
        return LinkCheck(url=url, final_url=url, status="untested_protocol",
                         http_status=None, url_kind=info.kind,
                         detail=f"cannot verify scheme {info.scheme or 'none'} over HTTP")
    try:
        result = _head_or_get(client, url, prefer_head=prefer_head, allow_get=allow_get)
    except PermanentFailure as exc:
        if exc.status in (401, 403, 429):
            return LinkCheck(url=url, final_url=url,
                             status="blocked" if exc.status in (401, 403) else "inaccessible",
                             http_status=exc.status, url_kind=info.kind,
                             detail=f"HTTP {exc.status} (access denied / rate limited)")
        return LinkCheck(url=url, final_url=url, status="broken", http_status=exc.status,
                         url_kind=info.kind, detail=f"HTTP {exc.status}")
    except TransientFailure as exc:
        return LinkCheck(url=url, final_url=url, status="inaccessible", http_status=exc.status,
                         url_kind=info.kind, detail=f"retries exhausted: {exc}")
    except HttpClientError as exc:
        return LinkCheck(url=url, final_url=url, status="inaccessible", http_status=None,
                         url_kind=info.kind, detail=f"{type(exc).__name__}: {exc}")

    content_type = result.header("Content-Type")
    final_info = classify_url(result.url, content_type)
    kind = final_info.kind
    if result.status in (401, 403):
        return LinkCheck(url=url, final_url=result.url, status="blocked", http_status=result.status,
                         url_kind=kind, content_type=content_type,
                         detail=f"HTTP {result.status}")
    if result.status == 404 or result.status == 410 or 400 <= result.status < 500:
        return LinkCheck(url=url, final_url=result.url, status="broken", http_status=result.status,
                         url_kind=kind, content_type=content_type, detail=f"HTTP {result.status}")
    if result.status >= 500:
        return LinkCheck(url=url, final_url=result.url, status="inaccessible",
                         http_status=result.status, url_kind=kind, content_type=content_type,
                         detail=f"HTTP {result.status} (server error)")

    redirected = normalize_url(result.url) != normalize_url(url)
    landing = _as_landing(kind, content_type)
    size = _int_header(result.header("Content-Length"))
    if landing:
        return LinkCheck(
            url=url, final_url=result.url, status="landing_ok", http_status=result.status,
            url_kind="landing_page", content_type=content_type, content_length=size,
            detail="resolves to an HTML page (landing page, artifact not proven)",
            landing_page_url=url,
        )
    if kind == "direct_download":
        status = "redirected" if redirected else "verified"
        return LinkCheck(
            url=url, final_url=result.url, status=status, http_status=result.status,
            url_kind="direct_download", content_type=content_type, content_length=size,
            detail=f"HTTP {result.status} {content_type or ''}".strip(),
            landing_page_url=None,
        )
    return LinkCheck(url=url, final_url=result.url, status="verified" if not redirected else "redirected",
                     http_status=result.status, url_kind="unknown", content_type=content_type,
                     content_length=size,
                     detail="resolves, but content type gives no artifact evidence")


def _head_or_get(client: HttpClient, url: str, *, prefer_head: bool, allow_get: bool):
    if prefer_head:
        try:
            return client.head(url)
        except PermanentFailure as exc:
            # Only fall back when the server rejects the HEAD method itself;
            # a definitive 404/410 is already the answer.
            if exc.status in (405, 501) and allow_get:
                return _range_get(client, url)
            raise
    if allow_get:
        return _range_get(client, url)
    raise PermanentFailure("GET disabled for this check", status=None)


def _range_get(client: HttpClient, url: str):
    """Small, ranged GET: enough to see status/headers, never the whole file."""
    return client.get(url, headers={"Range": "bytes=0-1023"})


def _as_landing(kind: str, content_type: str | None) -> bool:
    if kind == "landing_page":
        return True
    if kind == "unknown" and content_type and "html" in content_type.lower():
        return True
    return False


def _int_header(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None
