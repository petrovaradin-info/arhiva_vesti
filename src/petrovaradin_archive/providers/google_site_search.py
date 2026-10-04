from __future__ import annotations

import time
from collections.abc import Iterator
from urllib.parse import quote_plus

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By

from ..models import DiscoveredURL
from ..urltools import canonicalize_url, host_matches
from .selenium_search import SeleniumInternalSearchProvider


class GoogleSiteSearchProvider:
    """Free browser-based site search; no API key and no paid service."""

    name = "google_site_search"

    def __init__(self, settings: dict):
        self.known_urls = set()
        self.stop_site = False
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
        self.stop_site = False
        domain = site.get("google_domain") or site["domains"][0]
        max_pages = int(site.get("google_max_pages", self.settings.get("max_pages", 30)))
        wait = float(self.settings.get("page_wait_seconds", 2))
        audit = SeleniumInternalSearchProvider({"keywords": self.keywords})
        audit.known_urls = self.known_urls
        audit._log(site, f"browser=start provider={self.name}; domen={domain}")
        driver = self._driver()
        seen: set[str] = set()
        try:
            for keyword in self.keywords:
                previous_signature = None
                for page in range(max_pages):
                    query = quote_plus(f'site:{domain} "{keyword}"')
                    search_url = f"https://www.google.com/search?q={query}&filter=0&start={page * 10}"
                    audit._navigate(driver, site, search_url)
                    time.sleep(wait)
                    if ("/sorry/" in driver.current_url
                            or driver.find_elements(By.CSS_SELECTOR, "form[action*='sorry'], #captcha-form")):
                        raise TimeoutException(f"status=challenge; URL={driver.current_url}")
                    results = []
                    statuses = []
                    page_urls = set()
                    for element in driver.find_elements(By.CSS_SELECTOR, "a[href]"):
                        href = element.get_attribute("href") or ""
                        if not host_matches(href, site["domains"]):
                            continue
                        canonical = canonicalize_url(href)
                        page_urls.add(canonical)
                        try:
                            context = element.find_element(By.XPATH, "ancestor::div[1]").text
                        except WebDriverException:
                            context = element.text
                        status = audit._candidate_status(
                            href, f"{href} {context}", site,
                            {"trust_search_results": False}, seen,
                        )
                        audit._log(site, f"provider={self.name} strana={page + 1} "
                                   f"status={status} URL={href}")
                        if status in {'accepted', 'existing', 'duplicate'}:
                            statuses.append(status)
                        if status != "accepted":
                            continue
                        results.append((canonical, href, element.text.strip(), context.strip()))
                    signature = tuple(sorted(page_urls))
                    audit._log(site, f"provider={self.name} strana={page + 1} "
                               f"kandidata={len(results)} URL={search_url}")
                    if audit._stop_duplicates(site, statuses, page + 1, search_url):
                        self.stop_site = True
                        return
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
