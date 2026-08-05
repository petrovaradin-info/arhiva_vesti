from __future__ import annotations

import csv
from pathlib import Path

from .database import ArchiveDB
from .models import DiscoveredURL
from .urltools import host_matches


def import_urls(path: Path, db: ArchiveDB, sites: list[dict], source: str = "manual_import") -> int:
    urls: list[str] = []
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                url = row.get("url") or row.get("link")
                if url:
                    urls.append(url.strip())
    else:
        urls = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    added = 0
    for url in urls:
        site = next((item for item in sites if host_matches(url, item["domains"])), None)
        if site:
            added += db.add(DiscoveredURL(url=url, site_id=site["id"], discovered_by=source))
    return added

