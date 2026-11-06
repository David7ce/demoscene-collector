"""Configuration loading (``config/sources.yaml``, ``config/curation_rules.yaml``).

Secrets are read from environment variables only:

* ``YOUTUBE_API_KEY`` — YouTube Data API v3 key (optional).

No secret is ever written to the database, logs or exports.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_SOURCES = "config/sources.yaml"
DEFAULT_RULES = "config/curation_rules.yaml"


class ConfigError(Exception):
    pass


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"configuration file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ConfigError(f"configuration root must be a mapping: {path}")
    return data


@dataclass
class Settings:
    root: Path
    sources: dict[str, Any]
    rules: dict[str, Any]
    youtube_api_key: str | None = None
    db_override: Path | None = None

    # -- helpers ------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        if self.db_override is not None:
            return Path(self.db_override)
        return self.root / "data" / "demoscene.sqlite3"

    @property
    def cache_dir(self) -> Path:
        return self.root / "data" / "cache"

    @property
    def input_dir(self) -> Path:
        return self.root / "data" / "input"

    @property
    def exports_dir(self) -> Path:
        return self.root / "exports"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def docs_dir(self) -> Path:
        return self.root / "docs"

    def source(self, name: str) -> dict[str, Any]:
        try:
            return dict(self.sources["sources"][name])
        except KeyError as exc:
            raise ConfigError(f"unknown source: {name}") from exc

    def enabled_sources(self) -> list[str]:
        return [n for n, cfg in self.sources.get("sources", {}).items() if cfg.get("enabled", True)]

    # -- rules --------------------------------------------------------------
    @property
    def target_size(self) -> int:
        return int(self.rules.get("target_size", 500))

    @property
    def weights(self) -> dict[str, float]:
        weights = dict(self.rules.get("weights", {}))
        return {k: float(v) for k, v in weights.items()}

    @property
    def thresholds(self) -> dict[str, float]:
        return {k: float(v) for k, v in self.rules.get("thresholds", {}).items()}

    @property
    def verification(self) -> dict[str, Any]:
        return dict(self.rules.get("verification", {}))


def load_settings(root: str | Path | None = None, *,
                  sources_file: str | Path | None = None,
                  rules_file: str | Path | None = None) -> Settings:
    base = Path(root) if root else Path.cwd()
    sources_path = Path(sources_file) if sources_file else base / DEFAULT_SOURCES
    rules_path = Path(rules_file) if rules_file else base / DEFAULT_RULES
    settings = Settings(
        root=base,
        sources=_load_yaml(sources_path),
        rules=_load_yaml(rules_path),
        youtube_api_key=os.environ.get("YOUTUBE_API_KEY") or None,
    )
    return settings
