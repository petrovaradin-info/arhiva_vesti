"""Persistent coverage is separate from URL download state."""
import hashlib
import json

from ..urltools import canonicalize_url


class SearchProgress:
    def __init__(self, db, site, provider, query, config, full_scan=False, known_urls=None):
        self.db = db
        self.known = (known_urls if known_urls is not None
                      else db.known_urls(site['id']) if db is not None else set())
        # Changing selectors, filtering or the query requires a fresh baseline.
        identity = {
            'site': site['id'], 'provider': provider, 'query': query,
            'config': {k: v for k, v in config.items() if k not in {
                'max_pages', 'headless', 'user_data_dir', 'challenge_wait_seconds',
                'known_pages_to_stop', 'full_scan',
            }},
            'domains': site['domains'], 'blocklist': site.get('blocklist', []),
            'exclude': site.get('exclude_url_patterns', []),
        }
        self.key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        self.threshold = max(2, int(config.get('known_pages_to_stop', 2)))
        self.streak = 0
        self.incremental = bool(
            db is not None and not full_scan and config.get('newest_first', False)
            and config.get('pagination_mode') not in {'gsc', 'infinite_scroll'}
            and db.search_completed(self.key)
        )

    def should_stop(self, eligible_urls):
        urls = {canonicalize_url(url) for url in eligible_urls}
        self.streak = self.streak + 1 if urls and urls <= self.known else 0
        return self.incremental and self.streak >= self.threshold

    def complete(self):
        if self.db is not None:
            self.db.mark_search_completed(self.key)
