from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMETERS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
NON_ARTICLE_PATH_PARTS = {"komentar", "komentari", "comments", "comment"}


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    port = f":{parts.port}" if parts.port and parts.port not in (80, 443) else ""
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMETERS
    ]
    return urlunsplit((scheme, host + port, path, urlencode(sorted(query)), ""))


def url_hash(url: str) -> str:
    return hashlib.sha256(canonicalize_url(url).encode("utf-8")).hexdigest()


def host_matches(url: str, domains: list[str]) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == domain.lower() or host.endswith("." + domain.lower()) for domain in domains)


def is_non_article_url(url: str, patterns: list[str] | None = None) -> bool:
    parts = urlsplit(url)
    segments = {segment.casefold() for segment in parts.path.split("/") if segment}
    if segments & NON_ARTICLE_PATH_PARTS:
        return True
    query = {key.casefold(): value.casefold() for key, value in parse_qsl(parts.query)}
    if any(value in NON_ARTICLE_PATH_PARTS for value in query.values()):
        return True
    return any(re.search(pattern, url, re.IGNORECASE) for pattern in (patterns or []))
