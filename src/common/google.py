"""Google search, cached in SQLite: the Serper.dev API (exact URLs, no CAPTCHAs) or the shared Chrome session.

Search() picks the backend (config `search.backend`). In Chrome, Google wraps result links in opaque redirects (/goto?url=<token>), so the real address is read from the
<cite> line printed under each title ("https://www.x.edu › academics › majors"). Other links on the page
(knowledge-panel "Website" button, AI Overview sources) are matched to a titled result through the token.
"""
from __future__ import annotations

import html as html_lib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field

import httpx

from bs4.element import Comment
from typing import Callable, Optional
from urllib.parse import parse_qs, urljoin, urlparse

from .config import resolve_path
from .db import DB
from .html_utils import collapse, make_soup
from .urls import clean_google_href, ensure_scheme, is_google_url, url_key

log = logging.getLogger(__name__)

KP_TEXTS = {"website", "visit website", "official website", "official site", "web site"}
AD_SELECTORS = ("#tads", "#tadsb", "#bottomads", "[data-text-ad]", "#rhs")
CONTAINERS = ("#rso", "#search", "#center_col", "#main")
BREADCRUMB_RE = re.compile(r"\s*[›»]\s*")
DATA_URL_RE = re.compile(r"https?://[^\s\"'<>\\,\]]+")
DEBUG_DIR = resolve_path("output/debug/google")
GOOGLE_BASE = "https://www.google.com"
SERPER_URL = "https://google.serper.dev/search"


@dataclass
class GoogleResult:
    url: str             # best known address (exact, or rebuilt from the <cite> breadcrumb)
    title: str
    rank: int            # position among titled results (0 for other links)
    kind: str = "organic"  # "organic" = titled search result; "link" = other link (AI Overview, cards...)
    exact: bool = True   # False when the path was rebuilt from the breadcrumb
    goto: str = ""       # Google redirect URL (open it in Chrome to get the exact address)
    snippet: str = ""    # text under the title (Serper only)


@dataclass
class GoogleResults:
    organic: list = field(default_factory=list)
    links: list = field(default_factory=list)
    kp_website: str = ""
    no_results: bool = False
    page_query: str = ""

    @property
    def empty(self) -> bool:
        return not (self.organic or self.links or self.kp_website)


def normalize_query(q: str) -> str:
    return collapse((q or "").replace("“", '"').replace("”", '"')).lower()


def _heading(a):
    h = a.find("h3") or a.find(attrs={"role": "heading"})
    if h is None:
        h = a.find_parent("h3") or a.find_parent(attrs={"role": "heading"})
    return h


def _is_goto(href: str) -> bool:
    return href.startswith("/goto?") or "google.com/goto?" in href


def _goto_token(href: str) -> str:
    return (parse_qs(urlparse(href).query).get("url") or [""])[0]


def _cite_url(a) -> tuple[str, bool]:
    """'https://www.x.edu › academics › majors' -> ('https://www.x.edu/academics/majors', exact?)"""
    cite = a.find("cite")
    if cite is None:
        return "", False
    parts = [p for p in BREADCRUMB_RE.split(collapse(cite.get_text(" ", strip=True))) if p]
    if not parts:
        return "", False
    base = parts[0].strip()
    if " " in base or "." not in base:
        return "", False  # e.g. "24.6K+ followers" on social media results
    base = ensure_scheme(base).rstrip("/")
    segments = []
    for p in parts[1:]:
        if p.endswith(("...", "…")) or " " in p:
            break
        segments.append(p)
    exact = len(parts) == 1
    return (base + "/" + "/".join(segments)) if segments else base + "/", exact


def _resolve_anchor(a, tokens: dict) -> tuple[str, bool, str]:
    """(url, exact, goto) for a result anchor, or ('', False, '') when the address is unknown."""
    href = (a.get("href") or "").strip()
    if _is_goto(href):
        url, exact = _cite_url(a)
        if url:
            return url, exact, urljoin(GOOGLE_BASE, href)
    return _resolve_href(href, tokens)


def _resolve_href(href: str, tokens: dict) -> tuple[str, bool, str]:
    direct = clean_google_href(href)
    if direct and not is_google_url(direct):
        return direct, True, ""
    if not _is_goto(href):
        return "", False, ""
    goto = urljoin(GOOGLE_BASE, href)
    token = _goto_token(href)
    if token in tokens:
        return tokens[token][0], tokens[token][1], goto
    if len(token) >= 60:
        for t, (u, ex) in tokens.items():
            if t[:60] == token[:60]:
                return u, ex, goto
    return "", False, goto


def _ai_source_urls(soup) -> list[str]:
    """Source URLs of Google's AI answer, stored as JSON in <!--TgQPHd|||[...]--> comments next to each chip."""
    out = []
    for c in soup.find_all(string=lambda t: isinstance(t, Comment)):
        text = html_lib.unescape(str(c))
        if not text.startswith("TgQPHd"):
            continue
        text = text.replace("\\u003d", "=").replace("\\u0026", "&")
        for u in DATA_URL_RE.findall(text):
            if not is_google_url(u) and not re.search(r"gstatic\.com|googleusercontent\.com", u) and u not in out:
                out.append(u)
    return out


def parse_google_html(html: str) -> GoogleResults:
    soup = make_soup(html)
    ai_urls = _ai_source_urls(soup)
    box = soup.find("textarea", attrs={"name": "q"}) or soup.find("input", attrs={"name": "q"})
    page_query = ""
    if box is not None:
        page_query = box.get_text() if box.name == "textarea" else box.get("value", "")
    kp_hrefs = [a["href"].strip() for a in soup.find_all("a", href=True)
                if collapse(a.get_text(" ", strip=True)).lower() in KP_TEXTS
                or collapse(a.get("aria-label") or "").lower() in KP_TEXTS]

    for sel in AD_SELECTORS:
        for el in soup.select(sel):
            if not getattr(el, "decomposed", False):
                el.decompose()
    container = soup
    for sel in CONTAINERS:
        el = soup.select_one(sel)
        if el is not None and el.find("a", href=True):
            container = el
            break

    tokens: dict[str, tuple[str, bool]] = {}
    organic, seen = [], set()
    anchors = container.find_all("a", href=True)
    for a in anchors:  # titled results, in page order
        heading = _heading(a)
        if heading is None:
            continue
        url, exact, goto = _resolve_anchor(a, tokens)
        if not url or is_google_url(url):
            continue
        if goto:
            tokens[_goto_token(goto)] = (url, exact)
        key = url_key(url)
        if key in seen:
            continue
        seen.add(key)
        organic.append(GoogleResult(url, collapse(heading.get_text(" ", strip=True)), len(organic) + 1,
                                    "organic", exact, goto))

    links = []
    for a in anchors:  # other links whose address is known (direct, or same token as a titled result)
        if _heading(a) is not None:
            continue
        url, exact, goto = _resolve_anchor(a, tokens)
        if not url or is_google_url(url) or url_key(url) in seen:
            continue
        seen.add(url_key(url))
        title = collapse(a.get_text(" ", strip=True)) or collapse(a.get("aria-label") or "")
        links.append(GoogleResult(url, title[:200], 0, "link", exact, goto))
    for url in ai_urls:
        if url_key(url) not in seen:
            seen.add(url_key(url))
            links.append(GoogleResult(url, "", 0, "link", True, ""))

    kp = ""
    for href in kp_hrefs:
        url, _, _ = _resolve_href(href, tokens)
        if url and not is_google_url(url):
            kp = url
            break

    no_results = "did not match any documents" in html or "No results found for" in html
    return GoogleResults(organic, links, kp, no_results, page_query)


def resolve_result_url(browser_provider: Optional[Callable], result: GoogleResult) -> str:
    """Exact address of a result: open Google's redirect in Chrome when the path was only rebuilt."""
    if result.exact or not result.goto or browser_provider is None:
        return result.url
    browser = browser_provider()
    if browser is None:
        return result.url
    _, final, ok, _ = browser.render(result.goto)
    if ok and final and not is_google_url(final):
        return final
    return result.url


def _dump(query: str, html: str) -> None:
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9]+", "_", query)[:80].strip("_") or "query"
        path = DEBUG_DIR / f"{name}.html"
        path.write_text(html, encoding="utf-8")
        log.warning("Saved the Google page for inspection: %s", path)
    except OSError:
        pass


def _chrome_cached(db: DB, query: str) -> GoogleResults | None:
    cached = db.get_page("google:" + query)
    if cached is None or not cached["html"]:
        return None
    results = parse_google_html(cached["html"])
    if results.page_query and normalize_query(results.page_query) != normalize_query(query):
        return None
    return results


def google_search(db: DB, browser_provider: Optional[Callable], query: str, use_cache: bool = True) -> GoogleResults:
    """Search in the shared Chrome session (Search below adds the API backend)."""
    key = "google:" + query
    if use_cache:
        cached = _chrome_cached(db, query)
        if cached is not None:
            return cached
    browser = browser_provider() if browser_provider else None
    if browser is None:
        return GoogleResults()
    html = browser.google_search(query)
    results = parse_google_html(html)
    if results.page_query and normalize_query(results.page_query) != normalize_query(query):
        log.warning("Google showed results for a different query (%r) - ignoring this page for: %s",
                    results.page_query, query)
        _dump(query, html)
        return GoogleResults()
    if not results.organic:
        log.warning("Google page had no titled results for: %s (%d other links, knowledge panel: %s)",
                    query, len(results.links), "yes" if results.kp_website else "no")
        _dump(query, html)
    if not results.empty or results.no_results:
        db.put_page(key, key, 200, "text/html", html, "google")
    return results


def parse_serper_json(text: str) -> GoogleResults:
    data = json.loads(text)
    organic, links, seen = [], [], set()
    for item in data.get("organic") or []:
        url = (item.get("link") or "").strip()
        if not url.startswith(("http://", "https://")) or url_key(url) in seen:
            continue
        seen.add(url_key(url))
        organic.append(GoogleResult(url, collapse(item.get("title", "")), len(organic) + 1, "organic", True, "",
                                    collapse(item.get("snippet", ""))))
    for item in data.get("organic") or []:
        for sl in item.get("sitelinks") or []:
            url = (sl.get("link") or "").strip()
            if url.startswith(("http://", "https://")) and url_key(url) not in seen:
                seen.add(url_key(url))
                links.append(GoogleResult(url, collapse(sl.get("title", "")), 0, "link", True, ""))
    kp = ((data.get("knowledgeGraph") or {}).get("website") or "").strip()
    page_query = (data.get("searchParameters") or {}).get("q", "")
    return GoogleResults(organic, links, kp, not organic, page_query)


class SearchError(RuntimeError):
    def __init__(self, message: str, fatal: bool = False, key_dead: bool = False):
        super().__init__(message)
        self.fatal = fatal        # no API searches possible any more (all keys used up)
        self.key_dead = key_dead  # this key is out of credits / invalid: move to the next key


def serper_keys(cfg: dict) -> list[str]:
    """SERPER_API_KEYS (comma/space separated) + SERPER_API_KEY from .env, then config, in order, without repeats."""
    sc = cfg.get("search", {}) or {}
    raw = [os.getenv("SERPER_API_KEYS", ""), os.getenv("SERPER_API_KEY", "")]
    cfg_keys = sc.get("serper_api_keys") or []
    raw += [cfg_keys] if isinstance(cfg_keys, str) else list(cfg_keys)
    raw.append(sc.get("serper_api_key") or "")
    keys: list[str] = []
    for chunk in raw:
        for k in re.split(r"[\s,;]+", str(chunk or "")):
            if k and k not in keys:
                keys.append(k)
    return keys


class Search:
    """One Google search interface for all modules.

    backend "serper": Serper.dev API. Several keys can be given (SERPER_API_KEYS); they are used one by one, moving
    to the next when a key runs out of credits. If every key fails, Chrome is used when a browser is allowed.
    backend "chrome": the undetected Chrome session. "auto" = serper when a key is set, otherwise chrome.
    Cached results from either backend are reused, so re-runs cost no API credits.
    """

    def __init__(self, cfg: dict, db: DB, browser_provider: Optional[Callable] = None, use_cache: bool = True):
        sc = cfg.get("search", {}) or {}
        self.db = db
        self.browser_provider = browser_provider
        self.use_cache = use_cache
        self.keys = serper_keys(cfg)
        self._key_idx = 0
        self.params = {"gl": sc.get("gl", "us"), "hl": sc.get("hl", "en"), "num": int(sc.get("num", 10))}
        self.chrome_fallback = bool(sc.get("chrome_fallback", True))
        backend = str(sc.get("backend", "auto")).lower()
        if backend == "auto":
            backend = "serper" if self.keys else "chrome"
        if backend == "serper" and not self.keys:
            log.warning("search.backend is 'serper' but no SERPER_API_KEYS are set - using Chrome instead.")
            backend = "chrome"
        self.backend = backend
        self._client = httpx.Client(timeout=float(sc.get("timeout", 30))) if backend == "serper" else None
        self._serper_down = False
        self._lock = threading.Lock()
        self.api_calls = 0
        self.calls_per_key: dict[int, int] = {}
        log.info("Google search backend: %s", f"Serper API ({len(self.keys)} key(s))" if backend == "serper"
                 else "Chrome")

    @property
    def is_api(self) -> bool:
        return self.backend == "serper" and not self._serper_down

    @property
    def available(self) -> bool:
        return self.is_api or self.browser_provider is not None

    def search(self, query: str) -> GoogleResults:
        if self.use_cache:
            cached = self._cached(query)
            if cached is not None:
                return cached
        if self.is_api:
            try:
                return self._serper(query)
            except SearchError as e:
                if e.fatal:
                    self._serper_down = True
                    log.error("Serper API stopped working (%s) - no more API searches this run.", e)
                else:
                    log.warning("Serper search failed for %r: %s", query, e)
                if not self.chrome_fallback:
                    return GoogleResults()
        if self.browser_provider is None:
            return GoogleResults()
        return google_search(self.db, self.browser_provider, query, use_cache=False)

    def resolve(self, result: GoogleResult) -> str:
        return resolve_result_url(self.browser_provider, result)

    def _cached(self, query: str) -> GoogleResults | None:
        row = self.db.get_page("serper:" + query)
        if row is not None and row["html"]:
            try:
                return parse_serper_json(row["html"])
            except ValueError:
                pass
        return _chrome_cached(self.db, query)

    def _key_label(self, idx: int) -> str:
        return f"key {idx + 1}/{len(self.keys)} (...{self.keys[idx][-4:]})"

    def _serper(self, query: str) -> GoogleResults:
        """Use the current key; when it is out of credits or invalid, switch to the next one and retry."""
        while True:
            with self._lock:
                idx = self._key_idx
            if idx >= len(self.keys):
                raise SearchError("all Serper keys are used up or invalid", fatal=True)
            try:
                results = self._serper_once(query, self.keys[idx])
            except SearchError as e:
                if not e.key_dead:
                    raise
                with self._lock:
                    if self._key_idx == idx:  # another thread may have switched already
                        self._key_idx += 1
                        nxt = (f"switching to {self._key_label(self._key_idx)}" if self._key_idx < len(self.keys)
                               else "no keys left")
                        log.warning("Serper %s stopped working (%s) - %s.", self._key_label(idx), e, nxt)
                continue
            with self._lock:
                self.api_calls += 1
                self.calls_per_key[idx] = self.calls_per_key.get(idx, 0) + 1
            return results

    def _serper_once(self, query: str, key: str) -> GoogleResults:
        body = {"q": query, **self.params}
        last = ""
        for attempt in range(3):
            if attempt:
                time.sleep(2 ** attempt)
            try:
                r = self._client.post(SERPER_URL, json=body,
                                      headers={"X-API-KEY": key, "Content-Type": "application/json"})
            except httpx.HTTPError as e:
                last = f"{type(e).__name__}: {e}"
                continue
            if r.status_code == 200:
                try:
                    results = parse_serper_json(r.text)
                except ValueError as e:
                    raise SearchError(f"bad JSON from Serper: {e}")
                self.db.put_page("serper:" + query, SERPER_URL, 200, "application/json", r.text, "serper")
                return results
            last = f"HTTP {r.status_code}: {r.text[:200]}"
            body_low = r.text.lower()
            if r.status_code in (401, 402, 403) or "credit" in body_low or "api key" in body_low:
                raise SearchError(last, key_dead=True)  # out of credits / invalid key
            if r.status_code == 400:
                raise SearchError(last)  # a problem with this query only
        raise SearchError(last)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
        if self.api_calls:
            per_key = ", ".join(f"{self._key_label(i)}: {n}" for i, n in sorted(self.calls_per_key.items()))
            log.info("Serper API searches this run: %d (%s)", self.api_calls, per_key)
