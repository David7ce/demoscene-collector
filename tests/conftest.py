"""Shared pytest fixtures.

Tests never touch the real network: HTTP is exercised through
:class:`FakeSession`, a minimal stand-in for ``requests.Session`` that replays
scripted responses and records every request.
"""

from __future__ import annotations

import gzip
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from demoscene_collector.config import load_settings
from demoscene_collector.http_client import HttpConfig, HttpClient
from demoscene_collector import database as db

FIXTURES = Path(__file__).parent / "fixtures"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Fake HTTP
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status: int = 200, body: bytes | str = b"",
                 headers: dict[str, str] | None = None, url: str = "https://example.test/",
                 chunk_error: Exception | None = None):
        self.status_code = status
        if isinstance(body, str):
            body = body.encode("utf-8")
        self._body = body
        self.headers = headers or {}
        self.url = url
        self._chunk_error = chunk_error

    def iter_content(self, chunk_size: int = 1):
        if self._chunk_error is not None:
            raise self._chunk_error
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self) -> None:  # pragma: no cover - parity with requests
        pass


class FakeSession:
    """Replays scripted responses; raises scripted exceptions; records requests."""

    def __init__(self, script: list[Any] | None = None):
        self.headers: dict[str, str] = {}
        self.script = list(script or [])
        self.requests: list[dict[str, Any]] = []

    def add(self, *items: Any) -> "FakeSession":
        self.script.extend(items)
        return self

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append({"method": method, "url": url, "kwargs": kwargs})
        if not self.script:
            raise AssertionError(f"unexpected request: {method} {url}")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            item = item(method, url, kwargs)
        return item


def make_client(script: list[Any] | None = None, **config_overrides: Any) -> tuple[HttpClient, FakeSession]:
    session = FakeSession(script)
    config = HttpConfig(
        user_agent="TestAgent/0.1",
        timeout=1.0,
        max_retries=config_overrides.pop("max_retries", 2),
        default_min_interval=0.0,
        per_host_interval=config_overrides.pop("per_host_interval", {}),
        **config_overrides,
    )
    client = HttpClient(config, session=session, sleep=lambda _s: None)
    return client, session


# ---------------------------------------------------------------------------
# Database / settings
# ---------------------------------------------------------------------------

@pytest.fixture
def conn(tmp_path: Path):
    path = tmp_path / "test.sqlite3"
    connection = db.connect(path)
    db.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture
def settings(tmp_path: Path):
    root = tmp_path / "project"
    (root / "config").mkdir(parents=True)
    (root / "data" / "input").mkdir(parents=True)
    shutil.copy(PROJECT_ROOT / "config" / "sources.yaml", root / "config" / "sources.yaml")
    shutil.copy(PROJECT_ROOT / "config" / "curation_rules.yaml", root / "config" / "curation_rules.yaml")
    return load_settings(root)


# ---------------------------------------------------------------------------
# Sample source data
# ---------------------------------------------------------------------------

def sample_pouet_records(count: int = 12) -> list[dict[str, Any]]:
    """Deterministic Pouët-shaped records covering the fields we consume."""
    records = []
    platforms_cycle = [
        {"67": {"name": "MS-Dos", "icon": "k_msdos.gif", "slug": "msdos"}},
        {"72": {"name": "Commodore 64", "icon": "k_c64.gif", "slug": "c64"}},
        {"65": {"name": "Amiga OCS/ECS", "icon": "k_amiga.gif", "slug": "amiga"}},
        {"70": {"name": "Atari ST", "icon": "k_atari.gif", "slug": "atarist"}},
    ]
    for i in range(1, count + 1):
        year = 1988 + (i % 30)
        record = {
            "id": str(i),
            "name": f"Test Production {i}",
            "type": "demo",
            "types": ["demo"],
            "releaseDate": f"{year}-0{(i % 9) + 1}-15",
            "voteup": str(10 * i),
            "votedown": "2",
            "voteavg": "0.7000",
            "rank": str(i * 100),
            "popularity": 50.0 + i,
            "download": f"https://files.scene.org/view/demos/test/prod{i}.zip",
            "downloadLinks": [
                {"type": "online", "link": f"https://files.scene.org/view/demos/test/prod{i}-online.zip"},
                {"type": "Youtube", "link": f"https://www.youtube.com/watch?v=aaaaaaaaaaa{i:02d}"},
            ],
            "groups": [{"id": str(100 + i), "name": f"Group {i % 4 + 1}", "acronym": "",
                        "disambiguation": "", "web": None, "addedUser": "1",
                        "addedDate": "2000-01-01", "csdb": None, "zxdemo": None,
                        "demozoo": None}],
            "platforms": platforms_cycle[(i - 1) % len(platforms_cycle)],
            "credits": [
                {"user": {"id": str(200 + i), "nickname": f"scener{i % 3 + 1}",
                          "level": "user"}, "role": "Code"},
                {"user": {"id": str(300 + i), "nickname": f"musician{i}",
                          "level": "user"}, "role": "Music"},
            ],
            "placings": ([{"party": {"id": "1", "name": "Test Party", "web": None},
                           "compo": "1", "ranking": str((i % 3) + 1), "year": str(year),
                           "compo_name": "pc demo"}] if i % 2 == 0 else []),
            "awards": ([{"id": "1", "prodID": str(i), "categoryID": "2",
                         "awardType": "winner"}] if i % 5 == 0 else []),
            "party": ({"id": "1", "name": "Test Party", "web": None, "addedDate": None,
                       "addedUser": None} if i % 2 == 0 else None),
            "party_compo_name": ("pc demo" if i % 2 == 0 else None),
            "party_place": (str((i % 3) + 1) if i % 2 == 0 else None),
            "party_year": (str(year) if i % 2 == 0 else None),
            "demozoo": str(5000 + i) if i % 3 == 0 else "0",
            "csdb": "0",
            "zxdemo": "0",
            "sceneorg": str(9000 + i) if i % 6 == 0 else "0",
            "cdc": 0,
            "screenshot": f"https://content.pouet.net/files/screenshots/00001/000000{i:04d}.jpg",
        }
        records.append(record)
    return records


@pytest.fixture
def pouet_records() -> list[dict[str, Any]]:
    return sample_pouet_records()


@pytest.fixture
def dump_file(tmp_path: Path) -> Path:
    """A gzipped Pouët dump written into the project fixture root."""
    payload = {"dump_date": "2026-01-07 04:30:01", "prods": sample_pouet_records(30)}
    path = tmp_path / "project" / "data" / "input" / "pouetdatadump-prods-20260107.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return path
