"""Source adapter base classes."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterator

from ..database import utcnow
from ..http_client import HttpClient, HttpClientError
from ..models import ProductionCandidate

log = logging.getLogger(__name__)


@dataclass
class ProbeResult:
    source: str
    reachable: bool
    status: str                 # ok | degraded | unreachable | disabled | error
    detail: str
    observed_at: str = field(default_factory=utcnow)
    api_documented: bool | None = None
    auth_required: bool | None = None


class SourceError(Exception):
    """A source-level failure that should be recorded, not crashed on."""


class SourceUnreachable(SourceError):
    pass


class SourceAdapter(ABC):
    """One adapter per source; no adapter may write to the database directly."""

    name: str = "base"
    source_type: str = "unknown"
    base_url: str = ""
    access_method: str = "unknown"
    terms_url: str | None = None
    api_documentation_url: str | None = None
    notes: str = ""

    def __init__(self, config: dict[str, Any]):
        self.config = dict(config)
        self.base_url = self.config.get("base_url", self.base_url)
        self.access_method = self.config.get("access_method", self.access_method)
        self.terms_url = self.config.get("terms_url") or self.terms_url
        self.api_documentation_url = (
            self.config.get("api_documentation_url") or self.api_documentation_url
        )
        self.notes = str(self.config.get("notes", self.notes)).strip()

    # -- lifecycle ----------------------------------------------------------
    @abstractmethod
    def probe(self, client: HttpClient) -> ProbeResult:
        """Check reachability and record the outcome honestly."""

    def discover(self, client: HttpClient, **kwargs: Any) -> Iterator[ProductionCandidate]:
        """Yield production candidates. Optional for non-discovery sources."""
        return iter(())

    # -- shared helpers -----------------------------------------------------
    def _ok_probe(self, detail: str, *, api_documented: bool | None = None,
                  auth_required: bool | None = None) -> ProbeResult:
        return ProbeResult(self.name, True, "ok", detail,
                           api_documented=api_documented, auth_required=auth_required)

    def _fail_probe(self, detail: str, *, status: str = "unreachable") -> ProbeResult:
        return ProbeResult(self.name, False, status, detail)


def probe_http(adapter: SourceAdapter, client: HttpClient, url: str, *,
               timeout: float | None = None, expect: tuple[int, ...] = (200,)) -> ProbeResult:
    """Generic reachability probe for HTML/JSON endpoints."""
    try:
        result = client.get(url)
    except HttpClientError as exc:
        return adapter._fail_probe(f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # pragma: no cover - defensive
        return adapter._fail_probe(f"{type(exc).__name__}: {exc}", status="error")
    if result.status in expect:
        return adapter._ok_probe(f"HTTP {result.status} from {url}")
    if result.status in (401, 403):
        return adapter._fail_probe(f"HTTP {result.status} (access denied) from {url}",
                                   status="degraded")
    return adapter._fail_probe(f"HTTP {result.status} from {url}", status="degraded")
