from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
import unicodedata
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from .content import decode_html


@dataclass(frozen=True)
class ArticleData:
    title: str | None
    body_text: str
    published_at: str | None
    canonical_url: str | None
    original_source_url: str | None
    adapter_name: str
    image_url: str | None = None


BODY_SELECTORS = {
    "wordpress": (
        ".entry-content", ".post-content", ".td-post-content",
        ".single-post-content", ".article-content", "article",
    ),
    "drupal": (".field--name-body", ".node__content", ".article-content", "article", "main"),
    "generic": ("article", "main", "[itemprop='articleBody']"),
}

SOURCE_WORDS = re.compile(
    r"\b(?:izvor|source|preuzeto|preneto|originalno\s+objavljeno|via)\b", re.IGNORECASE
)


def _meta(soup: BeautifulSoup, *selectors: str) -> str | None:
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            value = (node.get("content") or node.get("href") or node.get("datetime")
                     or node.get_text(" ", strip=True))
            if value and str(value).strip():
                return str(value).strip()
    return None


def _external_original(soup: BeautifulSoup, page_url: str) -> str | None:
    page_host = (urlsplit(page_url).hostname or "").removeprefix("www.")
    for text_node in soup.find_all(string=SOURCE_WORDS):
        parent = text_node.parent
        for container in (parent, parent.parent if parent else None):
            if not isinstance(container, Tag):
                continue
            for anchor in container.select("a[href]"):
                candidate = urljoin(page_url, anchor.get("href", ""))
                host = (urlsplit(candidate).hostname or "").removeprefix("www.")
                if host and host != page_host and urlsplit(candidate).scheme in {"http", "https"}:
                    return candidate
    return None


def extract_article(
    content: bytes | str, page_url: str, adapter: str = "generic",
    body_selector: str | None = None, extra_strip_selectors: list[str] | None = None,
) -> ArticleData:
    soup = BeautifulSoup(decode_html(content), "html.parser")
    if adapter == "generic":
        generator = _meta(soup, "meta[name='generator']") or ""
        markup = str(soup)[:200_000]
        if "wordpress" in generator.casefold() or "wp-content/" in markup:
            adapter = "wordpress"
        elif "drupal" in generator.casefold() or "drupalSettings" in markup:
            adapter = "drupal"
    title = _meta(soup, "meta[property='og:title']", "meta[name='twitter:title']", "h1")
    canonical = _meta(soup, "link[rel='canonical']")
    published = _meta(
        soup, "meta[property='article:published_time']", "meta[name='date']",
        "time[datetime]", "[itemprop='datePublished']",
    )
    image = _meta(soup, "meta[property='og:image']", "meta[name='twitter:image']")
    adapter_name = adapter if adapter in BODY_SELECTORS else "generic"
    body = soup.select_one(body_selector) if body_selector else None
    if body is None:
        for selector in BODY_SELECTORS[adapter_name]:
            body = soup.select_one(selector)
            if body:
                break
    if body is None:
        body = soup.body or soup
    body = BeautifulSoup(str(body), "html.parser")
    strip_selector = "script,style,noscript,nav,footer,aside,form,.sharedaddy,.share,.social,.related,.comments"
    if extra_strip_selectors:
        strip_selector += "," + ",".join(extra_strip_selectors)
    for node in body.select(strip_selector):
        node.decompose()
    body_text = re.sub(r"\s+", " ", body.get_text(" ", strip=True)).strip()
    return ArticleData(
        title=title,
        body_text=body_text,
        published_at=published,
        canonical_url=urljoin(page_url, canonical) if canonical else None,
        original_source_url=_external_original(soup, page_url),
        adapter_name=adapter_name,
        image_url=urljoin(page_url, image) if image else None,
    )


def normalized_article_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).casefold()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def content_fingerprint(text: str) -> str | None:
    normalized = normalized_article_text(text)
    if len(normalized) < 200:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def simhash(text: str) -> str | None:
    words = normalized_article_text(text).split()
    if len(words) < 40:
        return None
    tokens = [" ".join(words[i:i + 5]) for i in range(max(1, len(words) - 4))]
    weights = [0] * 64
    for token in tokens:
        value = int.from_bytes(hashlib.sha1(token.encode("utf-8")).digest()[:8], "big")
        for bit in range(64):
            weights[bit] += 1 if value & (1 << bit) else -1
    result = sum((1 << bit) for bit, weight in enumerate(weights) if weight >= 0)
    return f"{result:016x}"


def hamming_distance(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count()
