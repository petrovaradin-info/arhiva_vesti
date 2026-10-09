"""Collect source evidence without fetching or enabling discovered sites."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from .urltools import canonicalize_url


@dataclass(frozen=True)
class SourceLink:
    url: str
    kind: str
    context: str = ""


SOURCE_LABEL = re.compile(
    r"\b(?:izvor|source|preuzeto|preneto|via|izveštava|navodi|извор|пренето|преузето)\b", re.I
)


def valid_url(value: str, base: str) -> str | None:
    try:
        value = value.strip()
        if not value or value.startswith('#'):
            return None
        url = urljoin(base, value)
        parts = urlsplit(url)
        if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username:
            return None
        return canonicalize_url(url)
    except (ValueError, TypeError):
        return None


def collect_links(body, soup, page_url: str) -> tuple[SourceLink, ...]:
    found = {}
    own = valid_url(page_url, page_url)

    def add(raw, kind, context=''):
        url = valid_url(str(raw), page_url)
        if url and url != own:
            found.setdefault((url, kind), SourceLink(url, kind, context[:500]))

    for node in body.select('a[href], blockquote[cite], q[cite]'):
        context = node.parent.get_text(' ', strip=True) if node.parent else ''
        kind = 'source' if SOURCE_LABEL.search(context) else 'citation'
        add(node.get('href') or node.get('cite'), kind, context)
    for text in body.find_all(string=re.compile(r'https?://')):
        for raw in re.findall(r'https?://[^\s<>\"\']+', str(text)):
            add(raw.rstrip('.,;:)'), 'citation', str(text))
    # Attribution often lives just outside the selected article body.
    for text in soup.find_all(string=SOURCE_LABEL):
        parent = text.parent
        if parent and parent.name in {'p', 'div', 'span', 'small', 'cite'}:
            context = parent.get_text(' ', strip=True)
            if len(context) <= 500 and not parent.find_parent(['nav', 'aside', 'script']):
                for anchor in parent.select('a[href]'):
                    add(anchor['href'], 'source', context)
    for node in soup.select('link[rel~=canonical]'):
        add(node.get('href', ''), 'canonical')
    for node in soup.select('meta[http-equiv]'):
        if str(node.get('http-equiv')).lower() == 'refresh':
            match = re.search(r'\burl\s*=\s*(.+)$', node.get('content', ''), re.I)
            if match:
                add(match[1].strip().strip('\"\''), 'redirect')
    return tuple(found.values())
