from __future__ import annotations

import time
import re
from collections.abc import Iterator
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from ..models import DiscoveredURL
from ..keywords import contains_keyword
from ..urltools import canonicalize_url, host_matches, is_non_article_url


class SeleniumInternalSearchProvider:
    name = "selenium_internal_search"

    def __init__(self, settings: dict):
        self.settings = settings.get("selenium", {})
        self.keywords = settings.get("keywords", ["Petrovaradin", "Петроварадин"])

    def _driver(self, config: dict):
        browser = self.settings.get("browser", "chrome").lower()
        if browser == "firefox":
            options = webdriver.FirefoxOptions()
            if config.get("headless", self.settings.get("headless", True)):
                options.add_argument("-headless")
            driver = webdriver.Firefox(options=options)
            driver.set_page_load_timeout(int(self.settings.get("page_load_timeout_seconds", 30)))
            return driver
        options = webdriver.ChromeOptions()
        if config.get("headless", self.settings.get("headless", True)):
            options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--ignore-certificate-errors")
        options.add_argument("--window-size=1440,1200")
        if config.get("user_data_dir"):
            options.add_argument(f"--user-data-dir={config['user_data_dir']}")
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(int(self.settings.get("page_load_timeout_seconds", 30)))
        return driver

    @staticmethod
    def _replace_search_term(url: str, keyword: str) -> str:
        markers = (
            "petrovaradin",
            quote("Петроварадин", safe=""),
            quote("петроварадин", safe=""),
        )
        result = url
        for marker in markers:
            replacement = quote(keyword, safe="")
            result = re.sub(re.escape(marker), replacement, result, flags=re.IGNORECASE)
        return result

    def _search_variants(self, config: dict) -> list[tuple[str, dict]]:
        configured = config.get("start_urls", [config["start_url"]])
        variants: list[tuple[str, dict]] = []
        seen: set[str] = set()
        for start_url in configured:
            has_term = "petrovaradin" in start_url.casefold() or "%d0%bf%d0%b5%d1%82" in start_url.casefold()
            keywords = self.keywords if config.get("search_both_scripts", True) and has_term else [None]
            for keyword in keywords:
                variant = dict(config)
                url = self._replace_search_term(start_url, keyword) if keyword else start_url
                if variant.get("page_url_template") and keyword:
                    variant["page_url_template"] = self._replace_search_term(
                        variant["page_url_template"], keyword
                    )
                if url not in seen:
                    seen.add(url)
                    variants.append((url, variant))
        return variants

    def discover(self, site: dict) -> Iterator[DiscoveredURL]:
        config = site.get("internal_search", {})
        if not config.get("enabled"):
            return
        driver = self._driver(config)
        seen_pages: set[tuple[str, ...]] = set()
        seen_urls: set[str] = set()
        try:
            for start_url, variant_config in self._search_variants(config):
                try:
                    driver.get(start_url)
                except WebDriverException as exc:
                    print(f"  [{site['id']}] početna strana nije učitana: {exc.msg}", flush=True)
                    continue
                challenge_wait = int(variant_config.get("challenge_wait_seconds", 0))
                if challenge_wait and not driver.find_elements(
                    By.CSS_SELECTOR, variant_config["result_link_css"]
                ):
                    print(
                        f"  [{site['id']}] čeka se ručna provera u browseru ({challenge_wait}s)",
                        flush=True,
                    )
                    try:
                        WebDriverWait(driver, challenge_wait).until(
                            lambda current: bool(current.find_elements(
                                By.CSS_SELECTOR, variant_config["result_link_css"]
                            ))
                        )
                    except TimeoutException:
                        pass
                yield from self._walk_pages(
                    driver, site, variant_config, start_url, seen_pages, seen_urls
                )
        finally:
            driver.quit()

    def _walk_pages(self, driver, site, config, start_url, seen_pages, seen_urls):
            for page_number in range(1, int(config.get("max_pages", 100)) + 1):
                time.sleep(float(self.settings.get("page_wait_seconds", 2)))
                result_elements = driver.find_elements(By.CSS_SELECTOR, config["result_link_css"])
                result_records = []
                for element in result_elements:
                    try:
                        href = element.get_attribute("href")
                        if not href:
                            continue
                        anchor_text = element.text.strip()
                        title = element.get_attribute("title")
                        try:
                            context_text = element.find_element(
                                By.XPATH,
                                "ancestor::*[self::article or "
                                "contains(@class, 'gsc-webResult') or "
                                "contains(@class, 'search-result')][1]",
                            ).text.strip()
                        except Exception:
                            context_text = anchor_text
                        result_records.append((href, anchor_text, title, context_text))
                    except Exception:
                        # Dynamic search widgets can replace a result while it is read.
                        continue
                hrefs = tuple(record[0] for record in result_records)
                # Page number alone is not proof of a new page: some search engines
                # repeat the last result set for every out-of-range page number.
                page_signature = hrefs
                if page_signature in seen_pages:
                    break
                seen_pages.add(page_signature)
                print(
                    f"  [{site['id']}] strana {page_number}: {len(hrefs)} kandidata; {driver.current_url}",
                    flush=True,
                )
                relevant_on_page = 0
                for href, anchor_text, title, context_text in result_records:
                    searchable = " ".join((href, anchor_text, title or "", context_text))
                    trusted_search = config.get("trust_search_results", True)
                    canonical_href = canonicalize_url(href)
                    blocked = any(href.startswith(prefix) for prefix in site.get("blocklist", []))
                    non_article = is_non_article_url(href, site.get("exclude_url_patterns", []))
                    if (
                        href
                        and canonical_href not in seen_urls
                        and host_matches(href, site["domains"])
                        and (trusted_search or contains_keyword(searchable, self.keywords))
                        and not blocked
                        and not non_article
                    ):
                        seen_urls.add(canonical_href)
                        relevant_on_page += 1
                        yield DiscoveredURL(
                            url=href, site_id=site["id"], discovered_by=self.name,
                            query=start_url,
                            metadata={
                                "title": anchor_text or title,
                                "search_result_text": context_text,
                                "search_page_url": driver.current_url,
                                "search_page_number": page_number,
                                "trusted_internal_search": trusted_search,
                            },
                        )
                if not hrefs:
                    print(f"  [{site['id']}] nema rezultata; kraj", flush=True)
                    break
                if config.get("pagination_mode") == "infinite_scroll":
                    old_count = len(hrefs)
                    driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
                    try:
                        WebDriverWait(
                            driver, int(self.settings.get("timeout_seconds", 20))
                        ).until(
                            lambda current: len(current.find_elements(
                                By.CSS_SELECTOR, config["result_link_css"]
                            )) > old_count
                        )
                    except TimeoutException:
                        break
                    continue
                if config.get("page_url_template"):
                    try:
                        next_page = page_number + int(config.get("page_template_offset", 1))
                        driver.get(config["page_url_template"].format(page=next_page))
                    except WebDriverException:
                        break
                    continue
                if config.get("pagination_mode") == "gsc":
                    parts = urlsplit(start_url)
                    fragment = dict(parse_qsl(parts.fragment, keep_blank_values=True))
                    fragment["gsc.page"] = str(page_number + 1)
                    next_url = urlunsplit(
                        (parts.scheme, parts.netloc, parts.path, parts.query, urlencode(fragment))
                    )
                    try:
                        driver.get(next_url)
                    except WebDriverException:
                        break
                    continue
                next_elements = driver.find_elements(By.CSS_SELECTOR, config["next_css"])
                if not next_elements:
                    break
                next_url = next_elements[0].get_attribute("href")
                if next_url:
                    try:
                        driver.get(urljoin(driver.current_url, next_url))
                    except WebDriverException:
                        break
                else:
                    try:
                        driver.execute_script("arguments[0].click()", next_elements[0])
                        WebDriverWait(driver, int(self.settings.get("timeout_seconds", 20))).until(
                            lambda current: tuple(
                                element.get_attribute("href")
                                for element in current.find_elements(
                                    By.CSS_SELECTOR, config["result_link_css"]
                                )
                                if element.get_attribute("href")
                            ) != hrefs
                        )
                    except TimeoutException:
                        break
