from __future__ import annotations

from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..models import DiscoveredURL
from ..urltools import canonicalize_url, host_matches
from .selenium_search import SeleniumInternalSearchProvider


class HTTPInternalSearchProvider(SeleniumInternalSearchProvider):
    """Static search pages use the existing rate-limited HTTP client, without Chrome."""

    name = "http_internal_search"

    def __init__(self, client, settings, db=None, full_scan=False):
        super().__init__(settings, db, full_scan)
        self.client = client

    def discover(self, site):
        self.stop_site = False
        self.known_urls = self.db.known_urls(site['id']) if self.db is not None else set()
        config = site.get("internal_search", {})
        if not config.get("enabled"):
            return
        seen_urls = set()
        for start_url, variant in self._search_variants(config):
            current_url = start_url
            seen_pages = set()
            max_pages = int(variant.get("max_pages", 100))
            for page in range(1, max_pages + 1):
                self._log(site, f"otvaranje provider={self.name} URL={current_url}")
                if not host_matches(current_url, site["domains"]):
                    raise ValueError(f"status=outside_domain; URL={current_url}")
                if not self.client.allowed(current_url):
                    raise ValueError(f"status=robots_disallowed; URL={current_url}")
                response = self.client.get(current_url)
                self._log(site, f"status=http_{response.status_code}; URL={response.url}")
                response.raise_for_status()
                soup = BeautifulSoup(response.text, "html.parser")
                records = soup.select(variant["result_link_css"])
                signature = tuple(
                    sorted(
                        {urljoin(str(response.url), a["href"]) for a in records if a.get("href")}
                    )
                )
                self._log(site, f"strana={page}; kandidata={len(signature)}; URL={response.url}")
                if not signature:
                    if variant.get("empty_result_css") and soup.select(variant["empty_result_css"]):
                        self._log(site, f"status=empty_results; URL={response.url}")
                        break
                    raise ValueError(
                        f"status=results_missing; URL={response.url}; selector={variant['result_link_css']}"
                    )
                if signature in seen_pages:
                    self.stop_site = True
                    self._log(site, f"status=repeated_page; prelazim na sledeći sajt; URL={response.url}")
                    return
                seen_pages.add(signature)
                statuses = []
                for a in records:
                    if not a.get("href"):
                        continue
                    href = urljoin(str(response.url), a["href"])
                    title = a.get_text(" ", strip=True)
                    container = a.find_parent("article") or a.parent
                    context = container.get_text(" ", strip=True)
                    status = self._candidate_status(
                        href, f"{href} {title} {context}", site, variant, seen_urls
                    )
                    self._log(site, f"kandidat status={status} strana={page} URL={href}")
                    if status in {'accepted', 'existing', 'duplicate'}:
                        statuses.append(status)
                    if status != "accepted":
                        continue
                    seen_urls.add(canonicalize_url(href))
                    yield DiscoveredURL(
                        url=href,
                        site_id=site["id"],
                        discovered_by=self.name,
                        query=start_url,
                        metadata={
                            "title": title,
                            "search_result_text": context,
                            "search_page_url": str(response.url),
                            "search_page_number": page,
                            "trusted_internal_search": variant.get("trust_search_results", True),
                        },
                    )
                if self._stop_duplicates(site, statuses, page, str(response.url)):
                    return
                if page == max_pages:
                    self._log(site, f"status=max_pages; limit={max_pages}; URL={response.url}")
                    break
                next_link = soup.select_one(variant.get("next_css", "a[rel='next']"))
                if not next_link:
                    self._log(site, f"status=no_next_link; URL={response.url}")
                    break
                current_url = self._next_url(
                    str(response.url), next_link.get("href", ""), variant, start_url, page
                )
