from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterator
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..http import PoliteClient
from ..keywords import contains_keyword
from ..models import DiscoveredURL
from ..urltools import host_matches


class RSSProvider:
    name = "rss"

    def __init__(self, client: PoliteClient, keywords: list[str]):
        self.client = client
        self.keywords = keywords

    def _feed_urls(self, site: dict) -> list[str]:
        feeds = list(site.get("feeds", []))
        if not site.get("rss_auto_discover", True) or not site.get("domains"):
            return feeds
        homepage = f"https://{site['domains'][0]}/"
        try:
            if not self.client.allowed(homepage):
                return feeds
            response = self.client.get(homepage)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, "html.parser")
            for link in soup.select("link[rel~='alternate'][href]"):
                media_type = (link.get("type") or "").lower()
                if "rss" in media_type or "atom" in media_type:
                    candidate = urljoin(str(response.url), link["href"])
                    if host_matches(candidate, site["domains"]):
                        feeds.append(candidate)
        except Exception:
            pass
        return list(dict.fromkeys(feeds))

    def discover(self, site: dict) -> Iterator[DiscoveredURL]:
        seen: set[str] = set()
        for feed_url in self._feed_urls(site):
            try:
                if not self.client.allowed(feed_url):
                    continue
                response = self.client.get(feed_url)
                response.raise_for_status()
                root = ET.fromstring(response.content)
            except Exception as exc:
                print(f"  [{site['id']}] RSS preskočen {feed_url}: {exc}", flush=True)
                continue
            for entry in root.iter():
                if not (entry.tag.endswith("item") or entry.tag.endswith("entry")):
                    continue
                title = description = published = ""
                url = ""
                for child in entry:
                    tag = child.tag.rsplit("}", 1)[-1].lower()
                    if tag == "title":
                        title = "".join(child.itertext()).strip()
                    elif tag in {"description", "summary", "content"}:
                        description += " ".join(child.itertext()).strip()
                    elif tag in {"pubdate", "published", "updated"}:
                        published = (child.text or "").strip()
                    elif tag == "link":
                        url = (child.get("href") or child.text or "").strip()
                searchable = BeautifulSoup(f"{title} {description}", "html.parser").get_text(" ")
                if (
                    url and url not in seen
                    and host_matches(url, site["domains"])
                    and contains_keyword(searchable, self.keywords)
                ):
                    seen.add(url)
                    yield DiscoveredURL(
                        url=url, site_id=site["id"], discovered_by=self.name,
                        query=feed_url,
                        metadata={"title": title, "feed_description": searchable,
                                  "published": published, "feed_url": feed_url},
                    )
