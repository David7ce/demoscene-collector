"""CSDb adapter (csdb.dk).

CSDb has no published API and its robots.txt restricts several named crawlers.
This adapter therefore performs two things only:

* a reachability probe;
* single-link checks for release URLs that the Pouët dump already provides.

HTML scraping of CSDb pages is deliberately **not** implemented. Numeric CSDb
IDs found in Pouët records are stored as cross-reference provenance only; no
URL pattern is invented from them.
"""

from __future__ import annotations

import logging
from typing import Any

from ..http_client import HttpClient
from .base import ProbeResult, SourceAdapter, probe_http
from .scene_org import LinkCheck, check_link

log = logging.getLogger(__name__)


class CsdbAdapter(SourceAdapter):
    name = "csdb"
    source_type = "web_database"
    base_url = "https://csdb.dk/"
    access_method = "single_request_head"

    def probe(self, client: HttpClient) -> ProbeResult:
        robots = self.config.get("robots_url")
        detail_bits: list[str] = []
        if robots:
            try:
                result = client.get(str(robots))
                detail_bits.append(f"robots.txt HTTP {result.status}")
            except Exception as exc:  # robots.txt is informational only
                detail_bits.append(f"robots.txt unavailable ({type(exc).__name__})")
        probe = probe_http(self, client, self.base_url)
        if probe.reachable:
            probe.detail += "; " + "; ".join(detail_bits) if detail_bits else ""
            probe.detail += "; no API: HTML only, single-request checks"
            probe.api_documented = False
            probe.auth_required = False
        return probe

    def check(self, client: HttpClient, url: str) -> LinkCheck:
        return check_link(client, url)
