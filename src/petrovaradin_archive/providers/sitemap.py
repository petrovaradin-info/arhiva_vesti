from __future__ import annotations

import gzip
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from urllib.parse import urljoin

from ..http import PoliteClient
from ..models import DiscoveredURL
from ..urltools import host_matches


class SitemapProvider:
    name = "sitemap"

    def __init__(self, client: PoliteClient, keywords: list[str], max_sitemaps: int = 10000):
        self.client = client
        self.keywords = [word.casefold() for word in keywords]
        self.max_sitemaps = max_sitemaps

    def discover(self, site: dict) -> Iterator[DiscoveredURL]:
        queue = list(site.get("sitemaps", []))
        seen: set[str] = set()
        while queue and len(seen) < self.max_sitemaps:
            sitemap_url = queue.pop(0)
            if sitemap_url in seen:
                continue
            seen.add(sitemap_url)
            try:
                response = self.client.get(sitemap_url)
                response.raise_for_status()
                content = response.content
                if sitemap_url.endswith(".gz"):
                    content = gzip.decompress(content)
                root = ET.fromstring(content)
            except Exception:
                continue
            namespace = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
            locs = [node.text.strip() for node in root.iter() if node.tag.endswith("loc") and node.text]
            if root.tag.endswith("sitemapindex"):
                queue.extend(urljoin(sitemap_url, loc) for loc in locs)
                continue
            for url in locs:
                if not host_matches(url, site["domains"]):
                    continue
                # URL keyword is a cheap first pass. Pages without keyword in the slug
                # Pages without the keyword in the slug are found by internal search.
                if any(keyword in url.casefold() for keyword in self.keywords):
                    yield DiscoveredURL(url=url, site_id=site["id"], discovered_by=self.name)
