from __future__ import annotations

import gzip
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from urllib.parse import urljoin

from ..content import content_kind, extract_text
from ..database import ArchiveDB
from ..http import PoliteClient
from ..keywords import contains_keyword
from ..models import DiscoveredURL
from ..urltools import host_matches, is_non_article_url


class SitemapContentProvider:
    """Resumable full-content scan for sites whose search function is incomplete."""

    name = "sitemap_content_scan"

    def __init__(self, client: PoliteClient, db: ArchiveDB, keywords: list[str], settings: dict):
        self.client = client
        self.db = db
        self.keywords = keywords
        config = settings.get("sitemap_content_scan", {})
        self.max_urls = int(config.get("max_urls_per_site_run", 1000))
        self.max_sitemaps = int(config.get("max_sitemaps_per_site", 100))

    def discover(self, site: dict) -> Iterator[DiscoveredURL]:
        queue = list(site.get("sitemaps", []))
        seen_sitemaps: set[str] = set()
        checked = 0
        while queue and len(seen_sitemaps) < self.max_sitemaps and checked < self.max_urls:
            sitemap_url = queue.pop(0)
            if sitemap_url in seen_sitemaps:
                continue
            seen_sitemaps.add(sitemap_url)
            try:
                response = self.client.get(sitemap_url)
                response.raise_for_status()
                content = response.content
                if sitemap_url.endswith(".gz"):
                    content = gzip.decompress(content)
                root = ET.fromstring(content)
            except Exception as exc:
                print(f"  [{site['id']}] sitemap scan preskočen {sitemap_url}: {exc}", flush=True)
                continue
            locs = [node.text.strip() for node in root.iter()
                    if node.tag.endswith("loc") and node.text]
            if root.tag.endswith("sitemapindex"):
                queue.extend(urljoin(sitemap_url, loc) for loc in locs)
                continue
            for url in locs:
                if checked >= self.max_urls:
                    break
                if (not host_matches(url, site["domains"])
                        or is_non_article_url(url, site.get("exclude_url_patterns", []))
                        or self.db.was_content_scanned(site["id"], url)):
                    continue
                checked += 1
                try:
                    if not self.client.allowed(url):
                        self.db.record_content_scan(site["id"], url, False, "Blocked by robots.txt")
                        continue
                    response = self.client.get(url)
                    response.raise_for_status()
                    kind = content_kind(str(response.url), response.headers.get("content-type", ""))
                    text = extract_text(response.content, kind, str(response.url))
                    matched = contains_keyword(f"{url} {text}", self.keywords)
                    self.db.record_content_scan(site["id"], url, matched)
                    if matched:
                        yield DiscoveredURL(
                            url=url, site_id=site["id"], discovered_by=self.name,
                            query=sitemap_url,
                            metadata={"title": "", "content_scan_kind": kind},
                        )
                except Exception as exc:
                    self.db.record_content_scan(site["id"], url, False, str(exc))
        print(f"  [{site['id']}] pregledano novih sitemap dokumenata: {checked}", flush=True)
