"""HTTP fetching with a SQLite cache, per-host politeness, and a Chrome fallback for blocked / JS pages."""
from __future__ import annotations

import html as html_lib
import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urldefrag

import httpx

from .db import DB
from .html_utils import make_soup
from .urls import ensure_scheme, host_of, origin

log = logging.getLogger(__name__)

TEXT_TYPES = ("html", "xml", "text", "json")
BLOCK_MARKERS = re.compile(r"just a moment\.\.\.|cf-chl|cf-browser-verification|attention required! \| cloudflare|"
                           r"_incapsula_resource|px-captcha|checking your browser|enable javascript and cookies", re.I)
ERROR_PAGE_RE = re.compile(r"\b(403|404|429|500|502|503)?\s*(forbidden|service unavailable|access denied|"
                           r"not found|too many requests|bad gateway|request blocked)\b", re.I)
ANCHOR_RE = re.compile(r"<a\s", re.I)
TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)
DNS_ERRORS = ("GETADDRINFO", "NAME OR SERVICE", "NODENAME", "NAME RESOLUTION", "NO ADDRESS")
LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.I | re.S)


@dataclass
class Page:
    url: str
    final_url: str
    status: int
    html: str
    content_type: str = ""
    via: str = "http"
    _soup: object = field(default=None, repr=False, compare=False)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400 and bool(self.html)

    @property
    def is_html(self) -> bool:
        return "html" in (self.content_type or "text/html")

    def soup(self):
        if self._soup is None:
            self._soup = make_soup(self.html)
        return self._soup


class Fetcher:
    def __init__(self, cfg: dict, db: DB, browser_provider: Optional[Callable] = None, use_cache: bool = True):
        fc = cfg.get("fetch", {})
        headers = {
            "User-Agent": fc.get("user_agent", "Mozilla/5.0"),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        timeout = httpx.Timeout(float(fc.get("timeout", 25)), connect=float(fc.get("connect_timeout", 10)))
        limits = httpx.Limits(max_connections=50, max_keepalive_connections=20)
        self._client = httpx.Client(headers=headers, timeout=timeout, limits=limits, follow_redirects=True)
        self._insecure = httpx.Client(headers=headers, timeout=timeout, limits=limits, follow_redirects=True,
                                      verify=False)
        self.db = db
        self.browser_provider = browser_provider
        self.use_cache = use_cache
        self.browser_fallback = bool(fc.get("browser_fallback", True))
        self.per_host_delay = float(fc.get("per_host_delay", 1.0))
        self.retries = int(fc.get("retries", 1))
        self.max_bytes = int(fc.get("max_bytes", 15_000_000))
        self._host_next: dict[str, float] = {}
        self._host_lock = threading.Lock()
        # bot checks (Cloudflare / CAPTCHA) that could not be passed: retry policy + sites given up on this run
        bc = cfg.get("browser", {})
        self.challenge_retries = int(bc.get("challenge_retries", 1))
        self.challenge_cooldown = [float(x) for x in bc.get("challenge_cooldown_minutes", [2, 4])]
        self.blocked_hosts: dict[str, str] = {}             # host -> kind of check
        self._challenge_locks: dict[str, threading.Lock] = {}

    def get(self, url: str, render: bool = False, expand: bool = False, allow_browser: bool = True) -> Optional[Page]:
        """Fetch a page. render=True loads it in Chrome (expand=True also clicks "load more" / scrolls)."""
        url = urldefrag(ensure_scheme(url))[0]
        if not url:
            return None
        key = (("render+expand:" if expand else "render:") + url) if render else url
        if self.use_cache:
            cached = self.db.get_page(key)
            if cached is not None:
                return Page(url, cached["final_url"] or url, cached["status"], cached["html"],
                            cached["content_type"], "cache")
        if render:
            page = self._render(url, expand)
            if page is not None and page.ok:
                self._store(key, page)
            return page

        page = self._http(url)
        if allow_browser and self.browser_fallback and self.browser_provider and self._needs_browser(page):
            log.debug("Chrome fallback for %s", url)
            rendered = self._render(url, expand=False)
            if rendered is not None and rendered.ok and not BLOCK_MARKERS.search(rendered.html[:6000]):
                page = rendered
        if page is not None and self._cacheable(page):
            self._store(key, page)
        return page

    # --- internals -----------------------------------------------------------
    def _needs_browser(self, page: Optional[Page]) -> bool:
        if page is None:
            return True
        if page.status in (403, 429, 503):
            return True
        if page.status >= 400 or not page.is_html:
            return False
        if BLOCK_MARKERS.search(page.html[:6000]) and len(page.html) < 80000:
            return True
        # JavaScript shell: almost no links and almost no text in the raw HTML
        if len(ANCHOR_RE.findall(page.html)) >= 5:
            return False
        text = TAG_RE.sub(" ", SCRIPT_RE.sub(" ", page.html))
        return len(text.split()) < 150

    @staticmethod
    def _cacheable(page: Page) -> bool:
        return 0 < page.status < 500 and page.status not in (403, 429)

    def _store(self, key: str, page: Page) -> None:
        self.db.put_page(key, page.final_url, page.status, page.content_type, page.html, page.via)

    def _wait_host(self, host: str) -> None:
        with self._host_lock:
            now = time.time()
            slot = max(now, self._host_next.get(host, 0.0))
            self._host_next[host] = slot + self.per_host_delay
        if slot > now:
            time.sleep(slot - now)

    def _http(self, url: str) -> Optional[Page]:
        self._wait_host(host_of(url))
        client, attempts, err = self._client, 0, None
        while attempts <= self.retries:
            try:
                return self._do(client, url)
            except httpx.ConnectError as e:
                msg = str(e).upper()
                if client is self._client and ("CERTIFICATE" in msg or "SSL" in msg):
                    client = self._insecure  # many college sites have broken certificate chains
                    continue
                if any(s in msg for s in DNS_ERRORS):
                    return None
                err = e
            except httpx.TransportError as e:
                err = e
            except Exception as e:  # malformed URLs, decoding problems...
                log.debug("Fetch error %s: %s", url, e)
                return None
            attempts += 1
            if attempts <= self.retries:
                time.sleep(1.5 * attempts)
        log.debug("Giving up on %s: %s", url, err)
        return None

    def _do(self, client: httpx.Client, url: str) -> Page:
        with client.stream("GET", url) as r:
            ctype = r.headers.get("content-type", "").lower()
            final = str(r.url)
            if ctype and not any(t in ctype for t in TEXT_TYPES):
                return Page(url, final, r.status_code, "", ctype)
            clen = r.headers.get("content-length", "")
            if clen.isdigit() and int(clen) > self.max_bytes:
                return Page(url, final, r.status_code, "", ctype)
            body = bytearray()
            for chunk in r.iter_bytes():
                body.extend(chunk)
                if len(body) > self.max_bytes:
                    break
            encoding = r.charset_encoding or "utf-8"
            try:
                text = bytes(body).decode(encoding, errors="replace")
            except LookupError:
                text = bytes(body).decode("utf-8", errors="replace")
            return Page(url, final, r.status_code, text, ctype or "text/html")

    def _render(self, url: str, expand: bool) -> Optional[Page]:
        browser = self.browser_provider() if self.browser_provider else None
        if browser is None:
            return None
        host = host_of(url)
        if host in self.blocked_hosts:
            return Page(url, url, 403, "", "text/html", "browser")  # gave up on this site earlier in the run
        html, final, ok, challenge = browser.render(url, expand=expand)
        if challenge:
            html, final, ok, challenge = self._retry_after_challenge(browser, url, expand, challenge)
            if challenge:
                return Page(url, final or url, 403, "", "text/html", "browser")
        status = 200 if ok else 0
        if ok:
            # Chrome cannot see the HTTP status: a short "403 Forbidden" / "503 Service Unavailable" page is an error,
            # not content (and must not be cached as the page)
            text = TAG_RE.sub(" ", SCRIPT_RE.sub(" ", html[:20000]))
            m = ERROR_PAGE_RE.search(" ".join(text.split())[:300])
            if m and len(text.split()) < 80:
                status = int(m.group(1)) if m.group(1) else 403
            elif len(text.split()) < 20:
                status = 0  # blank page (scripts failed to draw it): not content, and not cached
        return Page(url, final or url, status, html if ok else "", "text/html", "browser")

    def _retry_after_challenge(self, browser, url: str, expand: bool, challenge: str):
        """The bot check was not passed: close Chrome, wait a few minutes, open the same URL again with a fresh
        session. If it still fails, give up on this website for the rest of the run (its other pages are skipped
        at once instead of costing minutes each). One thread at a time per website."""
        host = host_of(url)
        with self._host_lock:
            lock = self._challenge_locks.setdefault(host, threading.Lock())
        with lock:
            if host in self.blocked_hosts:
                return "", url, False, self.blocked_hosts[host]
            final, ok, html = url, False, ""
            for attempt in range(1, self.challenge_retries + 1):
                lo, hi = (self.challenge_cooldown + self.challenge_cooldown)[:2]
                wait = random.uniform(lo, hi) * 60
                log.warning("%s check on %s not passed - closing Chrome, waiting %.1f min, then trying again "
                            "(%d/%d)", challenge.capitalize(), host, wait / 60, attempt, self.challenge_retries)
                browser.close()
                time.sleep(wait)
                html, final, ok, challenge = browser.render(url, expand=expand)
                if not challenge:
                    log.info("%s opened after waiting", host)
                    return html, final, ok, ""
            self.blocked_hosts[host] = challenge
            log.warning("%s keeps showing a %s check - skipping its other pages for the rest of this run",
                        host, challenge)
            return html, final, ok, challenge


def _sitemap_priority(url: str) -> int:
    low = url.lower()
    if any(k in low for k in ("page", "academic", "program", "department", "major", "people", "faculty")):
        return 0
    if any(k in low for k in ("post", "news", "event", "tag", "category", "author", "story", "blog", "image")):
        return 2
    return 1


def sitemap_urls(fetcher: Fetcher, site_url: str, max_files: int = 8, max_urls: int = 30000) -> list[str]:
    """Page URLs listed in the site's sitemap(s) (found through robots.txt or the usual locations)."""
    base = origin(site_url)
    queue: list[str] = []
    robots = fetcher.get(base + "robots.txt", allow_browser=False)
    if robots is not None and robots.ok:
        for line in robots.html.splitlines():
            if line.lower().startswith("sitemap:"):
                queue.append(line.split(":", 1)[1].strip())
    if not queue:
        queue = [base + "sitemap.xml", base + "sitemap_index.xml", base + "wp-sitemap.xml"]

    urls, seen, files = [], set(), 0
    while queue and files < max_files and len(urls) < max_urls:
        queue.sort(key=_sitemap_priority)
        sm = queue.pop(0)
        if sm in seen or sm.lower().endswith(".gz"):
            continue
        seen.add(sm)
        files += 1
        page = fetcher.get(sm, allow_browser=False)
        if page is None or not page.ok:
            continue
        is_index = "<sitemapindex" in page.html[:3000].lower()
        for loc in LOC_RE.findall(page.html):
            loc = html_lib.unescape(loc.replace("<![CDATA[", "").replace("]]>", "").strip())
            if not loc.startswith("http"):
                continue
            if is_index or loc.lower().endswith(".xml"):
                if loc not in seen:
                    queue.append(loc)
            else:
                urls.append(loc)
    return urls[:max_urls]
