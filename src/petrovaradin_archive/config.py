from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def load_config(config_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    settings = load_yaml(config_dir / "settings.yaml")
    sites = load_yaml(config_dir / "sites.yaml").get("sites", [])
    return settings, sites


def env(name: str, default: str | None = None) -> str | None:
    return os.getenv(name, default)

