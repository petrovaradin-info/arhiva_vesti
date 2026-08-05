from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import parse_qs, quote_plus, unquote, urlsplit

from bs4 import BeautifulSoup

from ..http import PoliteClient
from ..keywords import contains_keyword
from ..models import DiscoveredURL
from ..urltools import canonicalize_url, host_matches, is_non_article_url


class DuckDuckGoSiteSearchProvider:
    """API-free site search using DuckDuckGo's public HTML results."""

    name = "duckduckgo_site_search"

    def __init__(self, client: PoliteClient, keywords: list[str]):
        self.client = client
        self.keywords = keywords

    @staticmethod
    def _target_url(href: str) -> str:
        query = parse_qs(urlsplit(href).query)
        if query.get("uddg"):
            return unquote(query["uddg"][0])
        return href

    def discover(self, site: dict) -> Iterator[DiscoveredURL]:
        domain = site.get("google_domain") or site["domains"][0]
        seen: set[str] = set()
        for keyword in self.keywords:
            query = quote_plus(f'site:{domain} "{keyword}"')
            search_url = f"https://html.duckduckgo.com/html/?q={query}"
            try:
                response = self.client.get(search_url)
                response.raise_for_status()
            except Exception as exc:
                print(f"  [{site['id']}] DuckDuckGo preskočen: {exc}", flush=True)
                continue
            soup = BeautifulSoup(response.content, "html.parser")
            for result in soup.select(".result"):
                link = result.select_one("a.result__a[href]")
                if link is None:
                    continue
                url = self._target_url(link.get("href", ""))
                canonical = canonicalize_url(url)
                context = result.get_text(" ", strip=True)
                if (canonical in seen or not host_matches(url, site["domains"])
                        or is_non_article_url(url, site.get("exclude_url_patterns", []))
                        or not contains_keyword(f"{url} {context}", self.keywords)):
                    continue
                seen.add(canonical)
                yield DiscoveredURL(
                    url=url, site_id=site["id"], discovered_by=self.name,
                    query=search_url,
                    metadata={"title": link.get_text(" ", strip=True),
                              "search_result_text": context},
                )
