from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from selenium import webdriver

from .database import ArchiveDB
from .content import content_kind, extract_text, safe_extension, url_extension
from .http import PoliteClient
from .urltools import host_matches


class Downloader:
    def __init__(self, db: ArchiveDB, client: PoliteClient, html_dir: Path,
                 keywords: list[str], max_retries: int = 3, selenium_settings: dict | None = None,
                 file_dir: Path | None = None, text_dir: Path | None = None,
                 archive_settings: dict | None = None):
        self.db = db
        self.client = client
        self.html_dir = html_dir
        self.keywords = [word.casefold() for word in keywords]
        self.max_retries = max_retries
        self.selenium_settings = selenium_settings or {}
        self.file_dir = file_dir or html_dir.parent / "files"
        self.text_dir = text_dir or html_dir.parent / "text"
        self.archive_settings = archive_settings or {}
        self.max_document_bytes = int(self.archive_settings.get("max_document_mb", 30)) * 1024 * 1024
        self.max_image_bytes = int(self.archive_settings.get("max_image_mb", 8)) * 1024 * 1024
        self.max_assets = int(self.archive_settings.get("max_assets_per_page", 25))
        self.max_total_asset_bytes = int(
            self.archive_settings.get("max_total_assets_mb", 60)
        ) * 1024 * 1024
        self.max_asset_archive_bytes = int(
            self.archive_settings.get("max_asset_archive_mb", 2048)
        ) * 1024 * 1024
        self.driver = None

    def _render(self, url: str) -> tuple[bytes, str]:
        if self.driver is None:
            options = webdriver.ChromeOptions()
            if self.selenium_settings.get("headless", True):
                options.add_argument("--headless=new")
            options.add_argument("--disable-gpu")
            options.add_argument("--ignore-certificate-errors")
            options.add_argument("--window-size=1440,1200")
            self.driver = webdriver.Chrome(options=options)
            self.driver.set_page_load_timeout(
                int(self.selenium_settings.get("page_load_timeout_seconds", 30))
            )
        self.driver.get(url)
        return self.driver.page_source.encode("utf-8"), self.driver.current_url

    def close(self) -> None:
        if self.driver is not None:
            self.driver.quit()
            self.driver = None

    def run(self, limit: int = 100, site_id: str | None = None,
            mode: str = "selenium") -> dict[str, int]:
        counts = {name: 0 for name in (
            "archived", "no_keyword", "manual_capture_needed", "bad_redirect",
            "retry", "unavailable"
        )}
        for row in self.db.pending(limit, site_id):
            try:
                if not self.client.allowed(row["url"]):
                    raise PermissionError("Blocked by robots.txt")
                response = None
                requested_kind = content_kind(row["url"])
                use_browser = mode in {"selenium", "hybrid"} and requested_kind == "html"
                rendered = use_browser
                if use_browser:
                    strategy = "selenium"
                    try:
                        content, final_url = self._render(row["url"])
                        content_type = "text/html; charset=utf-8"
                        http_status = None
                        headers = {}
                    except Exception:
                        self.close()
                        strategy = "selenium_fresh_session"
                        try:
                            content, final_url = self._render(row["url"])
                            content_type = "text/html; charset=utf-8"
                            http_status = None
                            headers = {}
                        except Exception:
                            self.close()
                            strategy = "http_fallback"
                            rendered = False
                            response = self.client.get(row["url"])
                            response.raise_for_status()
                            content_type = response.headers.get("content-type", "")
                            content = response.content
                            final_url = str(response.url)
                            http_status = response.status_code
                            headers = dict(response.headers)
                else:
                    strategy = mode
                    request_limit = (
                        self.max_image_bytes if requested_kind == "image" else
                        self.max_document_bytes if requested_kind in {"pdf", "document"} else
                        self.client.max_bytes
                    )
                    response = self.client.get(row["url"], max_bytes=request_limit)
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "")
                    content = response.content
                    final_url = str(response.url)
                    http_status = response.status_code
                    headers = dict(response.headers)
                kind = content_kind(final_url, content_type)
                if kind not in {"html", "pdf", "document", "image"}:
                    raise ValueError(f"Unsupported content type: {content_type}")
                size_limit = self.max_image_bytes if kind == "image" else (
                    self.max_document_bytes if kind in {"pdf", "document"} else self.client.max_bytes
                )
                if len(content) > size_limit:
                    raise ValueError(f"Response exceeds {size_limit} byte archive limit")
                text = extract_text(content, kind, final_url).casefold()
                source = self.db.source(row["site_id"])
                domains = json.loads(source["domains_json"] or "[]") if source else []
                if domains and not host_matches(final_url, domains):
                    self.db.mark_bad_redirect(row["id"], final_url)
                    counts["bad_redirect"] += 1
                    continue
                discovery_context = " ".join((
                    row["url"], row["query"] or "", row["metadata_json"] or ""
                )).casefold()
                matched = any(word in text for word in self.keywords)
                # Binary attachments discovered from a keyword-bearing result may
                # be scanned or use a legacy format with no extractable text.
                if kind != "html" and not text.strip():
                    matched = any(word in discovery_context for word in self.keywords)
                if not matched and mode == "hybrid" and kind == "html":
                    content, final_url = self._render(row["url"])
                    text = extract_text(content, "html", final_url).casefold()
                    matched = any(word in text for word in self.keywords)
                    rendered = True
                if not matched:
                    self.db.mark_no_keyword(
                        row["id"], http_status=http_status,
                        final_url=final_url, content_type=content_type,
                    )
                    counts["no_keyword"] += 1
                    continue
                digest, archive_path, text_path = self._store_content(
                    content, final_url, content_type, kind
                )
                folder = Path(archive_path).parent
                metadata_path = folder / f"{digest}.json"
                metadata_path.write_text(json.dumps({
                    "requested_url": row["url"], "final_url": final_url,
                    "http_status": http_status, "content_type": content_type,
                    "sha256": digest, "matches_keyword": True, "rendered_by_selenium": rendered,
                    "media_kind": kind, "size_bytes": len(content),
                    "extracted_text_path": text_path,
                    "capture_strategy": strategy,
                    "headers": headers,
                }, ensure_ascii=False, indent=2), encoding="utf-8")
                self.db.mark_archived(
                    row["id"], http_status=http_status, final_url=final_url,
                    content_type=content_type, content_sha256=digest, archive_path=archive_path,
                    media_kind=kind, size_bytes=len(content), extracted_text_path=text_path,
                    extracted_text=text if kind != "html" else None,
                )
                if kind == "html":
                    self._queue_assets(row["id"], content, final_url, domains)
                counts["archived"] += 1
            except Exception as exc:
                if isinstance(exc, PermissionError):
                    self.db.mark_blocked(row["id"], str(exc))
                    counts["manual_capture_needed"] += 1
                    continue
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                permanent = status in {404, 410}
                retry = row["attempts"] + 1 < self.max_retries and not permanent
                self.db.mark_failed(row["id"], str(exc), retry, status)
                counts["retry" if retry else "unavailable"] += 1
        return counts

    def _store_content(self, content: bytes, final_url: str, content_type: str,
                       kind: str, extracted: str | None = None) -> tuple[str, str, str | None]:
        digest = hashlib.sha256(content).hexdigest()
        host = urlsplit(final_url).hostname or "unknown"
        if kind == "html":
            folder = self.html_dir / host
            folder.mkdir(parents=True, exist_ok=True)
            archive_path = folder / f"{digest}.html.gz"
            if not archive_path.exists():
                with gzip.open(archive_path, "wb") as handle:
                    handle.write(content)
        else:
            folder = self.file_dir / host
            folder.mkdir(parents=True, exist_ok=True)
            extension = safe_extension(final_url, content_type, kind)
            archive_path = folder / f"{digest}.{extension}"
            if not archive_path.exists():
                archive_path.write_bytes(content)

        if extracted is None:
            extracted = extract_text(content, kind, final_url)
        text_path = None
        if extracted.strip() and kind != "html":
            text_folder = self.text_dir / host
            text_folder.mkdir(parents=True, exist_ok=True)
            output = text_folder / f"{digest}.txt.gz"
            if not output.exists():
                with gzip.open(output, "wt", encoding="utf-8") as handle:
                    handle.write(extracted)
            text_path = str(output)
        return digest, str(archive_path), text_path

    def _asset_candidates(self, content: bytes, base_url: str) -> list[tuple[str, str]]:
        soup = BeautifulSoup(content, "html.parser")
        candidates: list[tuple[str, str]] = []
        if self.archive_settings.get("save_linked_documents", True):
            document_extensions = set(self.archive_settings.get(
                "document_extensions", ["pdf", "doc", "docx", "odt", "rtf", "txt", "csv", "xls", "xlsx"]
            ))
            for element in soup.select("a[href]"):
                url = urljoin(base_url, element.get("href", ""))
                if url_extension(url) in document_extensions:
                    candidates.append((url, "document"))
        if self.archive_settings.get("save_article_images", True):
            selectors = "article img[src], main img[src], .entry-content img[src], .post-content img[src]"
            for element in soup.select(selectors):
                url = urljoin(base_url, element.get("src", ""))
                if url:
                    candidates.append((url, "image"))
        deduplicated = []
        seen = set()
        for url, kind in candidates:
            if url not in seen:
                seen.add(url)
                deduplicated.append((url, kind))
        return deduplicated[:self.max_assets]

    def _queue_assets(self, row_id: int, content: bytes, final_url: str,
                      allowed_domains: list[str]) -> None:
        for asset_url, hinted_kind in self._asset_candidates(content, final_url):
            asset_id = self.db.add_asset(row_id, asset_url, hinted_kind)
            if asset_id is None:
                continue
            if not host_matches(asset_url, allowed_domains):
                self.db.mark_asset_failed(asset_id, "external", "Asset is outside source domains")

    def run_assets(self, limit: int = 100, site_id: str | None = None,
                   kind: str | None = None) -> dict[str, int]:
        counts = {name: 0 for name in (
            "archived", "robots_blocked", "size_limited", "unsupported", "failed"
        )}
        per_page_totals: dict[int, int] = {}
        archived_total = self.db.archived_asset_bytes()
        for row in self.db.pending_assets(limit, site_id, kind):
            asset_url = row["url"]
            hinted_kind = row["kind"]
            asset_id = row["id"]
            if not self.client.allowed(asset_url):
                self.db.mark_asset_failed(asset_id, "robots_blocked", "Blocked by robots.txt")
                counts["robots_blocked"] += 1
                continue
            try:
                limit = self.max_image_bytes if hinted_kind == "image" else self.max_document_bytes
                already = per_page_totals.get(row["url_id"], 0)
                remaining = self.max_total_asset_bytes - already
                global_remaining = self.max_asset_archive_bytes - archived_total
                remaining = min(remaining, global_remaining)
                if remaining <= 0:
                    self.db.mark_asset_failed(asset_id, "size_limited", "Asset archive budget exhausted")
                    counts["size_limited"] += 1
                    continue
                response = self.client.get(asset_url, max_bytes=min(limit, remaining))
                response.raise_for_status()
                kind = content_kind(str(response.url), response.headers.get("content-type", ""))
                if kind not in {"pdf", "document", "image"}:
                    self.db.mark_asset_failed(asset_id, "unsupported", f"Unsupported content: {kind}")
                    counts["unsupported"] += 1
                    continue
                payload = response.content
                extracted = extract_text(payload, kind, str(response.url))
                digest, archive_path, text_path = self._store_content(
                    payload, str(response.url), response.headers.get("content-type", ""), kind,
                    extracted=extracted,
                )
                self.db.mark_asset_archived(
                    asset_id, final_url=str(response.url),
                    content_type=response.headers.get("content-type", ""), size_bytes=len(payload),
                    content_sha256=digest, archive_path=archive_path,
                    extracted_text_path=text_path,
                    extracted_text=extracted,
                )
                per_page_totals[row["url_id"]] = already + len(payload)
                archived_total += len(payload)
                counts["archived"] += 1
            except Exception as exc:
                status = "size_limited" if "exceeds configured limit" in str(exc) else "failed"
                if status == "failed" and row["attempts"] + 1 < self.max_retries:
                    status = "retry"
                self.db.mark_asset_failed(asset_id, status, str(exc))
                counts["failed" if status == "retry" else status] += 1
        return counts
