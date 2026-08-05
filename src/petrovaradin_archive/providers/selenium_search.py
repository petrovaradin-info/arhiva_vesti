from __future__ import annotations

import time
from collections.abc import Iterator
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

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

    def _driver(self):
        browser = self.settings.get("browser", "chrome").lower()
        if browser == "firefox":
            options = webdriver.FirefoxOptions()
            if self.settings.get("headless", True):
                options.add_argument("-headless")
            driver = webdriver.Firefox(options=options)
            driver.set_page_load_timeout(int(self.settings.get("page_load_timeout_seconds", 30)))
            return driver
        options = webdriver.ChromeOptions()
        if self.settings.get("headless", True):
            options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--ignore-certificate-errors")
        options.add_argument("--window-size=1440,1200")
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(int(self.settings.get("page_load_timeout_seconds", 30)))
        return driver

    def discover(self, site: dict) -> Iterator[DiscoveredURL]:
        config = site.get("internal_search", {})
        if not config.get("enabled"):
            return
        driver = self._driver()
        seen_pages: set[tuple[str, ...]] = set()
        seen_urls: set[str] = set()
        try:
            start_urls = config.get("start_urls", [config["start_url"]])
            for start_url in start_urls:
                try:
                    driver.get(start_url)
                except WebDriverException as exc:
                    print(f"  [{site['id']}] početna strana nije učitana: {exc.msg}", flush=True)
                    continue
                yield from self._walk_pages(driver, site, config, start_url, seen_pages, seen_urls)
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
                    canonical_href = canonicalize_url(href)
                    blocked = any(href.startswith(prefix) for prefix in site.get("blocklist", []))
                    non_article = is_non_article_url(href, site.get("exclude_url_patterns", []))
                    if (
                        href
                        and canonical_href not in seen_urls
                        and host_matches(href, site["domains"])
                        and contains_keyword(searchable, self.keywords)
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
                            },
                        )
                if relevant_on_page == 0:
                    print(f"  [{site['id']}] nema novih relevantnih rezultata; kraj", flush=True)
                    break
                if config.get("page_url_template"):
                    try:
                        driver.get(config["page_url_template"].format(page=page_number + 1))
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
