from html import unescape
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from ..models import DiscoveredURL
from ..urltools import canonicalize_url, host_matches
from .search_progress import SearchProgress
from .selenium_search import SeleniumInternalSearchProvider


class WordPressSearchProvider(SeleniumInternalSearchProvider):
    """Public REST search; no browser or automated challenge solving."""

    name = 'wordpress_search'

    def __init__(self, client, settings, db=None, full_scan=False):
        super().__init__(settings, db, full_scan)
        self.client = client

    def discover(self, site):
        config = site.get('wordpress_search', {})
        endpoint = config.get('endpoint')
        if not endpoint:
            self._log(site, 'status=unsupported; wordpress_search endpoint nije podešen')
            return
        if not host_matches(endpoint, site['domains']):
            raise ValueError(f'status=outside_domain; URL={endpoint}')
        seen, signatures = set(), set()
        config = dict(config, newest_first=True)
        self.existing_count = 0
        self.known_urls = self.db.known_urls(site['id']) if self.db is not None else set()
        for keyword in self.keywords:
            signatures.clear()
            query = endpoint + '?' + urlencode({'search': keyword, 'orderby': 'date', 'order': 'desc'})
            progress = SearchProgress(self.db, site, self.name, query, config,
                                      self.full_scan, known_urls=self.known_urls)
            max_pages = int(config.get('max_pages', 200))
            per_page = min(100, max(1, int(config.get('per_page', 100))))
            for page in range(1, max_pages + 1):
                url = query + '&' + urlencode({
                    'page': page, 'per_page': per_page, '_fields': 'id,link,date,title,excerpt',
                })
                self._log(site, f'otvaranje provider={self.name}; strana={page}; URL={url}')
                if not self.client.allowed(url):
                    raise ValueError(f'status=robots_disallowed; URL={url}')
                response = self.client.get(url)
                self._log(site, f'status=http_{response.status_code}; URL={response.url}')
                response.raise_for_status()
                if not host_matches(str(response.url), site['domains']):
                    raise ValueError(f'status=outside_domain; URL={response.url}')
                try:
                    posts = response.json()
                except ValueError as exc:
                    raise ValueError(f'status=api_unavailable_or_challenge; URL={url}') from exc
                if not isinstance(posts, list) or any(
                    not isinstance(post, dict) or not post.get('link') for post in posts
                ):
                    raise ValueError(f'status=invalid_api_response; URL={url}')
                signature = tuple(sorted(post['link'] for post in posts))
                if posts and signature in signatures:
                    raise ValueError(f'status=repeated_page; URL={url}')
                signatures.add(signature)
                eligible = []
                for post in posts:
                    href = post['link']
                    title = BeautifulSoup(unescape(post.get('title', {}).get('rendered', '')), 'html.parser').get_text(' ', strip=True)
                    status = self._candidate_status(href, title, site, config, seen)
                    self._log(site, f'kandidat status={status}; strana={page}; URL={href}')
                    if status in {'accepted', 'existing', 'duplicate'}:
                        eligible.append(href)
                    if status == 'existing':
                        self.existing_count += 1
                        seen.add(canonicalize_url(href))
                    if status != 'accepted':
                        continue
                    seen.add(canonicalize_url(href))
                    yield DiscoveredURL(
                        url=href, site_id=site['id'], discovered_by=self.name, query=query,
                        metadata={'title': title, 'published_at': post.get('date'),
                                  'search_page_url': url, 'search_page_number': page,
                                  'trusted_internal_search': True},
                    )
                self._log(site, f'strana={page}; kandidata={len(posts)}; URL={url}')
                total_pages = response.headers.get('X-WP-TotalPages')
                if not posts or (total_pages is not None and page >= int(total_pages)):
                    progress.complete()
                    self._log(site, f'status=end_results; URL={url}')
                    break
                if progress.should_stop(eligible):
                    self._log(site, f'status=known_pages; uzastopnih={progress.streak}; URL={url}')
                    break
                if page == max_pages:
                    self._log(site, f'status=max_pages; limit={max_pages}; URL={url}')
