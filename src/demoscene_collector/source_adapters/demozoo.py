"""Demozoo adapter — reachability probe only.

Observed on 2026-10-10 (see docs/source_audit.md):

* ``https://demozoo.org/`` resolves (Cloudflare addresses) but every TCP
  connection times out from this network;
* ``api.demozoo.org`` does not exist (DNS NXDOMAIN);
* the API documentation could not be retrieved (site unreachable, no Wayback
  snapshot for ``demozoo.org/api-docs/``).

Because no endpoint, schema or authentication method could be *observed*, this
adapter deliberately implements no API calls. It probes reachability and raises
:class:`SourceUnreachable` so the pipeline records an honest failure instead of
pretending the source was used. Pouët records keep their Demozoo cross-reference
IDs so the source can be enabled later without re-discovery.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

from ..http_client import HttpClient, HttpClientError
from ..models import ProductionCandidate
from .base import ProbeResult, SourceAdapter, SourceUnreachable

log = logging.getLogger(__name__)


class DemozooAdapter(SourceAdapter):
    name = "demozoo"
    source_type = "web_database"
    base_url = "https://demozoo.org/"
    access_method = "unreachable_probe_only"

    def probe(self, client: HttpClient) -> ProbeResult:
        url = self.base_url
        timeout = float(self.config.get("probe_timeout_seconds", 12))
        original_timeout = client.config.timeout
        client.config.timeout = timeout
        try:
            result = client.get(url)
        except HttpClientError as exc:
            return self._fail_probe(
                f"unreachable from this network: {type(exc).__name__}: {exc} "
                "(TCP timeout; api.demozoo.org does not resolve)"
            )
        except Exception as exc:  # pragma: no cover - defensive
            return self._fail_probe(f"unreachable: {type(exc).__name__}: {exc}")
        finally:
            client.config.timeout = original_timeout
        if result.status == 200:
            return self._ok_probe(
                "HTTP 200 — source reachable again; API details must be re-audited before use",
                api_documented=None, auth_required=None,
            )
        return self._fail_probe(f"HTTP {result.status}", status="degraded")

    def discover(self, client: HttpClient, **kwargs: Any) -> Iterator[ProductionCandidate]:
        probe = self.probe(client)
        if not probe.reachable:
            raise SourceUnreachable(
                "demozoo is unreachable from this network; no schema may be assumed"
            )
        raise SourceUnreachable(
            "demozoo is reachable but its API was never audited from this environment; "
            "run `audit-sources` and update docs/source_audit.md first"
        )
