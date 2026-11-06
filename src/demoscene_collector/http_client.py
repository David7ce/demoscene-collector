"""Shared HTTP client with rate limiting, retries and caching.

Guarantees:

* honest, explicit User-Agent (from config) on every request;
* per-host minimum interval between requests;
* exponential backoff with jitter for transient failures (429 / 5xx / timeouts),
  honouring ``Retry-After``;
* no retry for permanent failures (other 4xx, denied access, invalid ids);
* bounded response size — bodies are truncated above ``max_response_bytes``;
* optional on-disk cache for GET responses where the source permits caching;
* secrets (API keys, Authorization headers) are never logged.
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import requests

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "DemosceneCollector/0.1 (+local research script)"


class HttpClientError(Exception):
    """Base class for HTTP client failures."""


class PermanentFailure(HttpClientError):
    """A failure that must not be retried (4xx other than 429, denied access…)."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class TransientFailure(HttpClientError):
    """Retries exhausted for a transient failure."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class ResponseTooLarge(HttpClientError):
    pass


@dataclass
class HttpConfig:
    user_agent: str = DEFAULT_USER_AGENT
    timeout: float = 20.0
    max_retries: int = 3
    backoff_base: float = 0.75
    backoff_max: float = 20.0
    max_response_bytes: int = 20_000_000
    default_min_interval: float = 1.0
    per_host_interval: dict[str, float] = field(default_factory=dict)
    backoff_jitter: float = 0.35


@dataclass
class HttpResult:
    url: str                 # final URL after redirects
    status: int
    headers: dict[str, str]
    body: bytes
    elapsed: float
    from_cache: bool = False
    attempts: int = 1

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def header(self, name: str, default: str | None = None) -> str | None:
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return default


class HttpClient:
    """Session wrapper used by every adapter and the verification stage."""

    def __init__(self, config: HttpConfig | None = None, *,
                 cache_dir: str | Path | None = None,
                 session: requests.Session | None = None,
                 sleep=time.sleep,
                 rng: random.Random | None = None):
        self.config = config or HttpConfig()
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": self.config.user_agent, "Accept": "*/*"})
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._last_request: dict[str, float] = {}
        self._sleep = sleep
        self._rng = rng or random.Random()

    # -- rate limiting ------------------------------------------------------
    def _host_of(self, url: str) -> str:
        return (urlsplit(url).netloc or "").lower()

    def _wait_for_slot(self, host: str) -> None:
        interval = self.config.per_host_interval.get(host, self.config.default_min_interval)
        if interval <= 0:
            return
        last = self._last_request.get(host)
        if last is None:
            return
        delta = time.monotonic() - last
        if delta < interval:
            self._sleep(interval - delta)

    # -- cache --------------------------------------------------------------
    @staticmethod
    def _cache_key_url(url: str, params: dict[str, Any] | None) -> str:
        """Full deterministic request URL used as cache key.

        Query parameters are part of the identity of a request: without them
        every ``…/oembed?url=…`` call would collide on the bare endpoint and
        serve one cached response for all videos.
        """
        if not params:
            return url
        query = urlencode(sorted((str(k), str(v)) for k, v in params.items()))
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}{query}"

    def _cache_path(self, url: str) -> Path | None:
        if not self.cache_dir:
            return None
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def _cache_read(self, url: str, ttl: float | None) -> HttpResult | None:
        path = self._cache_path(url)
        if not path or not path.exists():
            return None
        import json

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        age = time.time() - payload.get("stored_at", 0)
        if ttl is not None and age > ttl:
            return None
        import base64

        return HttpResult(
            url=payload.get("url", url),
            status=int(payload.get("status", 0)),
            headers=payload.get("headers", {}),
            body=base64.b64decode(payload.get("body_b64", "")),
            elapsed=0.0,
            from_cache=True,
        )

    def _cache_write(self, url: str, result: HttpResult) -> None:
        path = self._cache_path(url)
        if not path:
            return
        import base64
        import json

        payload = {
            "url": result.url,
            "status": result.status,
            "headers": result.headers,
            "body_b64": base64.b64encode(result.body).decode("ascii"),
            "stored_at": time.time(),
        }
        try:
            path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:  # pragma: no cover - cache is best effort
            log.debug("cache write failed for %s", url)

    # -- core request -------------------------------------------------------
    def request(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                params: dict[str, Any] | None = None, allow_redirects: bool = True,
                cache: bool = False, cache_ttl: float | None = None,
                max_bytes: int | None = None) -> HttpResult:
        method = method.upper()
        cache_url = self._cache_key_url(url, params)
        if method == "GET" and cache:
            cached = self._cache_read(cache_url, cache_ttl)
            if cached is not None:
                log.debug("cache hit %s", cache_url)
                return cached

        host = self._host_of(url)
        attempts = 0
        last_error: Exception | None = None
        while attempts <= self.config.max_retries:
            attempts += 1
            self._wait_for_slot(host)
            self._last_request[host] = time.monotonic()
            started = time.monotonic()
            try:
                response = self.session.request(
                    method, url, headers=headers, params=params,
                    timeout=self.config.timeout, allow_redirects=allow_redirects, stream=True,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                if attempts > self.config.max_retries:
                    break
                delay = self._backoff(attempts)
                log.warning("transient network error %s (%s) attempt=%s sleep=%.2fs",
                            url, type(exc).__name__, attempts, delay)
                self._sleep(delay)
                continue
            except requests.exceptions.TooManyRedirects as exc:
                # A redirect loop is a property of the link, not a transient
                # hiccup: record it instead of crashing the whole stage.
                raise PermanentFailure(f"redirect loop: {exc}", status=None) from exc
            except requests.RequestException as exc:
                # Any other transport-level failure (invalid URL, SSL error,
                # malformed chunked encoding, …): report, never crash a stage.
                raise TransientFailure(f"{type(exc).__name__}: {exc}") from exc

            body = self._read_body(response, max_bytes or self.config.max_response_bytes)
            elapsed = time.monotonic() - started
            result = HttpResult(
                url=str(response.url),
                status=response.status_code,
                headers=dict(response.headers),
                body=body,
                elapsed=elapsed,
                attempts=attempts,
            )
            response.close()

            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempts > self.config.max_retries:
                    raise TransientFailure(
                        f"retries exhausted for {url} (HTTP {result.status})",
                        status=result.status,
                    )
                delay = self._retry_after(response) or self._backoff(attempts)
                log.warning("HTTP %s from %s, retrying in %.2fs", response.status_code, url, delay)
                self._sleep(delay)
                continue

            if 400 <= response.status_code < 500:
                raise PermanentFailure(
                    f"HTTP {response.status_code} for {url}", status=response.status_code
                )

            if method == "GET" and cache:
                self._cache_write(cache_url, result)
            return result

        raise TransientFailure(f"network failure for {url}: {last_error}")

    def get(self, url: str, **kwargs: Any) -> HttpResult:
        return self.request("GET", url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> HttpResult:
        kwargs.setdefault("allow_redirects", True)
        return self.request("HEAD", url, **kwargs)

    # -- helpers ------------------------------------------------------------
    def _read_body(self, response: requests.Response, max_bytes: int) -> bytes:
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=65536):
            chunks.append(chunk)
            total += len(chunk)
            if total >= max_bytes:
                log.debug("response truncated at %s bytes (%s)", total, response.url)
                break
        return b"".join(chunks)[:max_bytes]

    def _backoff(self, attempt: int) -> float:
        base = min(self.config.backoff_max, self.config.backoff_base * (2 ** (attempt - 1)))
        jitter = base * self.config.backoff_jitter
        return max(0.05, base + self._rng.uniform(-jitter, jitter))

    def _retry_after(self, response: requests.Response) -> float | None:
        value = response.headers.get("Retry-After")
        if not value:
            return None
        try:
            seconds = float(value)
        except ValueError:
            # HTTP-date form: treat conservatively as a fixed wait
            return 5.0
        return min(seconds, 120.0)


def build_client(sources_config: dict[str, Any], *, cache_dir: str | Path | None = None,
                 offline: bool = False) -> HttpClient:
    """Create a client from ``config/sources.yaml`` (defaults + per-host overrides)."""
    defaults = sources_config.get("defaults", {})
    per_host: dict[str, float] = {}
    for name, cfg in sources_config.get("sources", {}).items():
        base = cfg.get("base_url") or cfg.get("api_base_url")
        if not base:
            continue
        host = _host(base)
        interval = cfg.get("min_interval_seconds", defaults.get("min_interval_seconds", 1.0))
        per_host[host] = float(interval)
        api_base = cfg.get("api_base_url")
        if api_base:
            per_host[_host(api_base)] = float(
                cfg.get("min_interval_seconds", defaults.get("min_interval_seconds", 1.0))
            )
    config = HttpConfig(
        user_agent=os.environ.get("DEMOSCENE_UA", defaults.get("user_agent", DEFAULT_USER_AGENT)),
        timeout=float(defaults.get("timeout_seconds", 20.0)),
        max_retries=int(defaults.get("max_retries", 3)),
        max_response_bytes=int(defaults.get("max_response_bytes", 20_000_000)),
        default_min_interval=float(defaults.get("min_interval_seconds", 1.0)),
        per_host_interval=per_host,
    )
    client = HttpClient(config, cache_dir=None if offline else cache_dir)
    return client


def _host(url: str) -> str:
    return (urlsplit(url).netloc or "").lower()
