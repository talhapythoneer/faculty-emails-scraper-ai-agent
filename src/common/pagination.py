"""Following pagination on listing pages: rel=next, "Next" links, numbered pages, ?page=N, A-Z tabs."""
from __future__ import annotations

import re
from typing import Callable
from urllib.parse import parse_qs, urldefrag, urljoin, urlparse

from .html_utils import collapse
from .urls import url_key

NEXT_TEXTS = {"next", "next page", "next ›", "next »", "next >", "›", "»", ">", ">>", "older", "more results"}
PAGE_PARAMS = {"page", "pg", "p", "paged", "cpage", "start", "offset", "pageno", "page_num", "pagenum", "pagenumber"}


def _param_name(key: str) -> str:
    # "filter[cpage]" -> "cpage"
    return key.lower().rstrip("]").split("[")[-1]


def _is_page_variant(url: str, page_url: str) -> bool:
    a, b = urlparse(url), urlparse(page_url)
    if a.netloc.lower() != b.netloc.lower():
        return False
    if any(_param_name(k) in PAGE_PARAMS for k in parse_qs(a.query)):
        return True
    if re.search(r"/page/\d+/?$", a.path):
        return True
    same_path = a.path.rstrip("/") == b.path.rstrip("/")
    if same_path and a.query != b.query:
        return True
    # /programs/a, /programs/b ... (A-Z tabs as separate paths)
    parent_a = a.path.rstrip("/").rsplit("/", 1)[0]
    parent_b = b.path.rstrip("/").rsplit("/", 1)[0]
    return bool(re.search(r"/[a-z]$", a.path.rstrip("/").lower())) and parent_a in (parent_b, b.path.rstrip("/"))


def pagination_links(soup, page_url: str) -> list[str]:
    out = []
    for tag in soup.find_all(["a", "link"], href=True):
        rel = tag.get("rel") or []
        if "next" in [r.lower() for r in rel]:
            out.append(urldefrag(urljoin(page_url, tag["href"]))[0])
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = urldefrag(urljoin(page_url, href))[0]
        text = collapse(a.get_text(" ", strip=True)).lower()
        aria = collapse(a.get("aria-label") or a.get("title") or "").lower()
        if text in NEXT_TEXTS or aria.startswith("next") or "next page" in aria:
            if urlparse(url).netloc.lower() == urlparse(page_url).netloc.lower():
                out.append(url)
        elif re.fullmatch(r"\d{1,3}", text) and 2 <= int(text) <= 300 and _is_page_variant(url, page_url):
            out.append(url)
        elif re.fullmatch(r"[a-z]", text) and _is_page_variant(url, page_url):
            out.append(url)
    base = url_key(page_url)
    return [u for u in dict.fromkeys(out) if url_key(u) != base]


def collect_pages(start_page, fetch: Callable, max_pages: int) -> list:
    """Return the start page plus every pagination page reachable from it (up to max_pages)."""
    pages = [start_page]
    seen = {url_key(start_page.url), url_key(start_page.final_url)}
    queue = pagination_links(start_page.soup(), start_page.final_url)
    while queue and len(pages) < max_pages:
        url = queue.pop(0)
        key = url_key(url)
        if key in seen:
            continue
        seen.add(key)
        page = fetch(url)
        if page is None or not page.ok:
            continue
        pages.append(page)
        for nxt in pagination_links(page.soup(), page.final_url):
            if url_key(nxt) not in seen:
                queue.append(nxt)
    return pages
