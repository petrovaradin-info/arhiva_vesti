from __future__ import annotations

import re
import time
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote, urljoin

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    SessionNotCreatedException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from ..keywords import contains_keyword
from ..models import DiscoveredURL
from ..urltools import canonicalize_url, host_matches, is_non_article_url
from .search_progress import SearchProgress


class SeleniumInternalSearchProvider:
    name = "selenium_internal_search"

    def __init__(self, settings: dict, db=None, full_scan=False):
        self.db = db
        self.full_scan = full_scan
        self.known_urls = set()
        self.existing_count = 0
        self.last_url = ""
        self.settings = settings.get("selenium", {})
        self.keywords = settings.get("keywords", ["Petrovaradin", "Петроварадин"])
        self.root = Path(__file__).resolve().parents[3]

    @staticmethod
    def _log(site, message):
        print(f"  [{site['id']}] {message}", flush=True)

    def _driver(self, config: dict):
        browser = self.settings.get("browser", "chrome").lower()
        headless = config.get("headless", self.settings.get("headless", True))
        if browser == "firefox":
            options = webdriver.FirefoxOptions()
            if headless:
                options.add_argument("-headless")
            driver = webdriver.Firefox(options=options)
        else:
            options = webdriver.ChromeOptions()
            if headless:
                options.add_argument("--headless=new")
            options.add_argument("--disable-gpu")
            options.add_argument("--ignore-certificate-errors")
            options.add_argument("--window-size=1440,1200")
            if config.get("user_data_dir"):
                profile = Path(config["user_data_dir"])
                if not profile.is_absolute():
                    profile = self.root / profile
                profile = profile.resolve()
                profile.mkdir(parents=True, exist_ok=True)
                options.add_argument(f"--user-data-dir={profile}")
            driver = webdriver.Chrome(options=options)
        try:
            driver.set_page_load_timeout(int(self.settings.get("page_load_timeout_seconds", 30)))
        except Exception:
            driver.quit()
            raise
        return driver

    @staticmethod
    def _replace_search_term(url: str, keyword: str) -> str:
        markers = ("petrovaradin", quote("Петроварадин", safe=""), quote("петроварадин", safe=""))
        pattern = "|".join(re.escape(marker) for marker in markers)
        return re.sub(pattern, lambda _: quote(keyword, safe=""), url, flags=re.IGNORECASE)

    def _search_variants(self, config: dict) -> list[tuple[str, dict]]:
        variants, seen = [], set()
        for start_url in config.get("start_urls") or [config["start_url"]]:
            has_term = ("petrovaradin" in start_url.casefold()
                        or "%d0%bf%d0%b5%d1%82" in start_url.casefold())
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

    def _navigate(self, driver, site, url):
        self.last_url = url
        self._log(site, f"otvaranje URL={url}")
        try:
            driver.get(url)
        except WebDriverException as exc:
            self._log(site, f"status=navigation_error URL={url}; {exc.msg}")
            raise
        self._log(site, f"status=loaded URL={driver.current_url}")
        if "too many requests" in driver.title.casefold():
            raise WebDriverException(f"status=rate_limited; HTTP 429 stranica; URL={url}")

    def discover(self, site: dict) -> Iterator[DiscoveredURL]:
        self.existing_count = 0
        self.known_urls = self.db.known_urls(site['id']) if self.db is not None else set()
        config = site.get("internal_search", {})
        if not config.get("enabled"):
            return
        variants = self._search_variants(config)
        if not variants:
            return
        driver, temporary_profile = None, None
        self._log(site, f"browser=start; planirani URL={variants[0][0]}; "
                  f"profil={config.get('user_data_dir', 'automatski')}")
        try:
            try:
                driver = self._driver(config)
            except SessionNotCreatedException as exc:
                self._log(site, f"status=browser_start_error; nijedan URL nije otvoren; {exc.msg}")
                if (not config.get("retry_with_fresh_profile", False)
                        or self.settings.get("browser", "chrome").lower() != "chrome"):
                    raise
                temporary_profile = TemporaryDirectory(prefix="archive-chrome-")
                self._log(site, "browser=retry; novi privremeni profil; postojeći profil ostaje sačuvan")
                driver = self._driver(dict(config, user_data_dir=temporary_profile.name))
            seen_urls = set()
            if config.get("bootstrap_url"):
                self._navigate(driver, site, config["bootstrap_url"])
                time.sleep(float(config.get("bootstrap_wait_seconds", 3)))
            for start_url, variant_config in variants:
                try:
                    self._navigate(driver, site, start_url)
                    stop_reason = yield from self._walk_pages(
                        driver, site, variant_config, start_url, set(), seen_urls
                    )
                    if stop_reason == "repeated_page":
                        return
                except WebDriverException as exc:
                    self._log(site, f"status=search_error query={start_url}; "
                              f"URL={self.last_url}; {exc.msg}")
                    raise
        finally:
            try:
                if driver is not None:
                    try:
                        driver.quit()
                    except WebDriverException:
                        pass
            finally:
                if temporary_profile:
                    temporary_profile.cleanup()

    @staticmethod
    def _signature(driver, config):
        try:
            hrefs = [e.get_attribute("href") for e in driver.find_elements(
                By.CSS_SELECTOR, config["result_link_css"]
            )]
            return tuple(sorted({canonicalize_url(href) for href in hrefs if href}))
        except StaleElementReferenceException:
            return ()

    @staticmethod
    def _challenge(driver):
        return any(marker in driver.title.casefold() for marker in (
            "just a moment", "attention required", "security verification"
        ))

    def _wait_results(self, driver, site, config, previous=None):
        def ready(current):
            signature = self._signature(current, config)
            if signature and signature != previous:
                return True
            empty_css = config.get("empty_result_css")
            return bool(empty_css and any(e.is_displayed() for e in current.find_elements(
                By.CSS_SELECTOR, empty_css
            )))

        timeout = float(config.get("results_timeout_seconds", self.settings.get("timeout_seconds", 20)))
        try:
            WebDriverWait(driver, timeout).until(ready)
        except TimeoutException:
            if self._challenge(driver):
                seconds = int(config.get("challenge_wait_seconds", 0))
                if seconds:
                    self._log(site, f"status=challenge; ručna provera do {seconds}s; URL={driver.current_url}")
                    try:
                        WebDriverWait(driver, seconds).until(ready)
                        return
                    except TimeoutException:
                        pass
                raise TimeoutException(f"status=challenge; URL={driver.current_url}")
            if previous and self._signature(driver, config) == previous:
                self._log(site, f"status=unchanged_results; URL={driver.current_url}")
                return
            raise TimeoutException(
                f"status=results_timeout; URL={driver.current_url}; "
                f"selector={config['result_link_css']} (nije potvrđeno da nema rezultata)"
            )

    def _candidate_status(self, href, searchable, site, config, seen_urls):
        if not host_matches(href, site["domains"]):
            return "outside_domain"
        if any(href.startswith(prefix) for prefix in site.get("blocklist", [])):
            return "blocklist"
        if is_non_article_url(href, site.get("exclude_url_patterns", [])):
            return "non_article"
        if canonicalize_url(href) in seen_urls:
            return "duplicate"
        if not config.get("trust_search_results", True) and not contains_keyword(searchable, self.keywords):
            return "no_keyword"
        if canonicalize_url(href) in self.known_urls:
            return "existing"
        return "accepted"

    def _click(self, driver, site, element):
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'})", element)
        try:
            element.click()
        except ElementClickInterceptedException:
            self._log(site, f"status=click_intercepted; ponovni klik paginacije; URL={self.last_url}")
            driver.execute_script("arguments[0].click()", element)

    def _walk_pages(self, driver, site, config, start_url, seen_pages, seen_urls):
        progress = SearchProgress(self.db, site, self.name, start_url, config,
                                  self.full_scan, known_urls=self.known_urls)
        self._log(site, f"poznatih={len(progress.known)}; incremental={progress.incremental}; query={start_url}")
        previous = None
        max_pages = int(config.get("max_pages", 100))
        for page_number in range(1, max_pages + 1):
            self._wait_results(driver, site, config, previous)
            page_url = driver.current_url
            time.sleep(float(self.settings.get("page_wait_seconds", 2)))
            records = []
            for element in driver.find_elements(By.CSS_SELECTOR, config["result_link_css"]):
                try:
                    href = element.get_attribute("href")
                    if not href:
                        continue
                    anchor_text = element.text.strip()
                    title = element.get_attribute("title") or ""
                    try:
                        context = element.find_element(
                            By.XPATH, "ancestor::*[self::article or contains(@class, 'gsc-webResult') "
                            "or contains(@class, 'search-result')][1]"
                        ).text.strip()
                    except WebDriverException:
                        context = anchor_text
                    records.append((href, anchor_text, title, context))
                except StaleElementReferenceException:
                    self._log(site, f"status=stale_element; URL={driver.current_url}")
            signature = tuple(sorted({canonicalize_url(r[0]) for r in records}))
            self._log(site, f"strana {page_number}: {len(records)} kandidata; URL={driver.current_url}")
            if not signature:
                self._log(site, "status=empty_results; kraj")
                progress.complete()
                return
            if signature in seen_pages:
                compared = "prethodnoj" if signature == previous else "ranijoj"
                self._log(site, f"status=repeated_page; strana={page_number}; "
                          f"ponavljaju se isti URL-ovi kao na {compared} stranici; "
                          f"verovatno nepostojeća stranica; završavam ovaj sajt; "
                          f"jedinstvenih_URL={len(signature)}; URL={page_url}")
                return "repeated_page"
            seen_pages.add(signature)
            counts = Counter()
            eligible = []
            for href, anchor_text, title, context in records:
                status = self._candidate_status(
                    href, f"{href} {anchor_text} {title} {context}", site, config, seen_urls
                )
                counts[status] += 1
                if status in {"accepted", "existing", "duplicate"}:
                    eligible.append(href)
                if status == "existing":
                    self.existing_count += 1
                    seen_urls.add(canonicalize_url(href))
                self._log(site, f"kandidat status={status} strana={page_number} URL={href}")
                if status == "accepted":
                    seen_urls.add(canonicalize_url(href))
                    yield DiscoveredURL(
                        url=href, site_id=site["id"], discovered_by=self.name, query=start_url,
                        metadata={"title": anchor_text or title, "search_result_text": context,
                                  "search_page_url": page_url,
                                  "search_page_number": page_number,
                                  "trusted_internal_search": config.get("trust_search_results", True)},
                    )
            self._log(site, f"strana={page_number} završena; " + "; ".join(
                f"{key}={value}" for key, value in sorted(counts.items())
            ))
            if progress.should_stop(eligible):
                self._log(site, f"status=known_pages; kraj; uzastopnih={progress.streak}; URL={page_url}")
                return
            if page_number == max_pages:
                self._log(site, f"status=max_pages; limit={max_pages}; URL={driver.current_url}")
                return
            previous = signature
            mode = config.get("pagination_mode")
            if mode == "gsc":
                # Use the widget's real cursor; invented fragment pages may not exist.
                cursors = driver.find_elements(By.CSS_SELECTOR, ".gsc-cursor-page")
                next_elements = [e for e in cursors if e.text.strip() == str(page_number + 1)]
                if not next_elements:
                    next_elements = driver.find_elements(By.CSS_SELECTOR, ".gsc-cursor-next-page")
                next_elements = [e for e in next_elements if e.is_displayed()]
                if not next_elements:
                    self._log(site, f"status=no_next_cursor; kraj GSC; URL={driver.current_url}")
                    return
                self._log(site, f"GSC klik strana={page_number + 1}; URL={driver.current_url}")
                self._click(driver, site, next_elements[0])
            elif mode == "infinite_scroll":
                self._log(site, f"akcija=scroll; URL={driver.current_url}")
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
            else:
                next_elements = driver.find_elements(By.CSS_SELECTOR, config.get("next_css", "a[rel='next']"))
                if next_elements:
                    next_url = next_elements[0].get_attribute("href")
                    if next_url:
                        self._navigate(driver, site, urljoin(driver.current_url, next_url))
                    else:
                        self._log(site, f"akcija=next_click; URL={driver.current_url}")
                        self._click(driver, site, next_elements[0])
                elif config.get("page_url_template") and not config.get("require_next_link", False):
                    next_page = page_number + int(config.get("page_template_offset", 1))
                    self._navigate(driver, site, config["page_url_template"].format(page=next_page))
                else:
                    self._log(site, f"status=no_next_link; kraj; URL={driver.current_url}")
                    progress.complete()
                    return
