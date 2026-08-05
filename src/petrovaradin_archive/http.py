from __future__ import annotations

import time
import urllib.robotparser
from collections import defaultdict

import httpx


class PoliteClient:
    def __init__(self, settings: dict, user_agent: str):
        request = settings.get("request", {})
        self.delay = float(request.get("delay_seconds", 1.5))
        self.respect_robots = bool(request.get("respect_robots_txt", True))
        self.max_bytes = int(request.get("max_response_mb", 20)) * 1024 * 1024
        self.tls_insecure_hosts = {
            host.lower() for host in request.get("tls_insecure_hosts", [])
        }
        self.client = httpx.Client(
            follow_redirects=True,
            timeout=float(request.get("timeout_seconds", 30)),
            headers={"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml,application/xml"},
        )
        self.last_request: dict[str, float] = defaultdict(float)
        self.robots: dict[str, urllib.robotparser.RobotFileParser] = {}

    def _request(self, url: str) -> httpx.Response:
        try:
            return self.client.get(url)
        except httpx.ConnectError:
            host = (httpx.URL(url).host or "").lower()
            if host not in self.tls_insecure_hosts:
                raise
            return httpx.get(
                url, follow_redirects=True, timeout=self.client.timeout,
                headers=dict(self.client.headers), verify=False,
            )

    def _wait(self, host: str) -> None:
        remaining = self.delay - (time.monotonic() - self.last_request[host])
        if remaining > 0:
            time.sleep(remaining)

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parsed = httpx.URL(url)
        root = f"{parsed.scheme}://{parsed.host}"
        if root not in self.robots:
            parser = urllib.robotparser.RobotFileParser(root + "/robots.txt")
            try:
                # RobotFileParser.read() uses urllib's own User-Agent. Some sites
                # reject that request even though they serve robots.txt to our
                # configured crawler, after which RobotFileParser treats the
                # whole site as disallowed. Fetch with the same httpx client and
                # User-Agent used for archive requests, then only use the parser
                # for interpreting the returned rules.
                response = self._request(root + "/robots.txt")
                if response.status_code in {401, 403}:
                    parser.disallow_all = True
                elif response.status_code == 404:
                    parser.allow_all = True
                elif response.is_success:
                    parser.parse(response.text.splitlines())
                else:
                    # Preserve the existing fail-open behaviour for temporary
                    # robots endpoint failures (timeouts and 5xx responses).
                    parser.allow_all = True
            except Exception:
                parser.allow_all = True
            self.robots[root] = parser
        return self.robots[root].can_fetch(self.client.headers["User-Agent"], url)

    def get(self, url: str, max_bytes: int | None = None) -> httpx.Response:
        host = httpx.URL(url).host or ""
        self._wait(host)
        response = self._request(url)
        self.last_request[host] = time.monotonic()
        effective_max = self.max_bytes if max_bytes is None else max_bytes
        if len(response.content) > effective_max:
            raise ValueError(f"Response exceeds configured limit: {url}")
        return response

    def close(self) -> None:
        self.client.close()
