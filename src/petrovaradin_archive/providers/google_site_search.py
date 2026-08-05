from __future__ import annotations

import time
from collections.abc import Iterator
from urllib.parse import quote_plus, urlsplit

from selenium import webdriver
from selenium.webdriver.common.by import By

from ..keywords import contains_keyword
from ..models import DiscoveredURL
from ..urltools import canonicalize_url, host_matches, is_non_article_url


class GoogleSiteSearchProvider:
    """Free browser-based site search; no API key and no paid service."""

    name = "google_site_search"

    def __init__(self, settings: dict):
        self.settings = settings.get("google_search", {})
        self.selenium = settings.get("selenium", {})
        self.keywords = settings.get("keywords", ["Petrovaradin", "Петроварадин"])

    def _driver(self):
        options = webdriver.ChromeOptions()
        if self.selenium.get("headless", True):
            options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--ignore-certificate-errors")
        options.add_argument("--window-size=1440,1200")
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(
            int(self.selenium.get("page_load_timeout_seconds", 30))
        )
        return driver

    def discover(self, site: dict) -> Iterator[DiscoveredURL]:
        domain = site.get("google_domain") or site["domains"][0]
        max_pages = int(site.get("google_max_pages", self.settings.get("max_pages", 30)))
        wait = float(self.settings.get("page_wait_seconds", 2))
        driver = self._driver()
        seen: set[str] = set()
        try:
            for keyword in self.keywords:
                previous_signature = None
                for page in range(max_pages):
                    query = quote_plus(f'site:{domain} "{keyword}"')
                    search_url = f"https://www.google.com/search?q={query}&filter=0&start={page * 10}"
                    driver.get(search_url)
                    time.sleep(wait)
                    results = []
                    for element in driver.find_elements(By.CSS_SELECTOR, "a[href]"):
                        href = element.get_attribute("href") or ""
                        if not host_matches(href, site["domains"]):
                            continue
                        canonical = canonicalize_url(href)
                        if canonical in seen or is_non_article_url(
                            href, site.get("exclude_url_patterns", [])
                        ):
                            continue
                        try:
                            context = element.find_element(By.XPATH, "ancestor::div[1]").text
                        except Exception:
                            context = element.text
                        if not contains_keyword(f"{href} {context}", self.keywords):
                            continue
                        results.append((canonical, href, element.text.strip(), context.strip()))
                    signature = tuple(item[0] for item in results)
                    if not signature or signature == previous_signature:
                        break
                    previous_signature = signature
                    for canonical, href, title, context in results:
                        if canonical in seen:
                            continue
                        seen.add(canonical)
                        yield DiscoveredURL(
                            url=href, site_id=site["id"], discovered_by=self.name,
                            query=search_url,
                            metadata={"title": title, "search_result_text": context,
                                      "google_page_number": page + 1},
                        )
        finally:
            driver.quit()
