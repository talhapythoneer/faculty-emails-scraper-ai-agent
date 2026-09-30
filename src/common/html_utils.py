"""HTML parsing helpers."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin

from bs4 import BeautifulSoup
from bs4.element import CData, Comment, Declaration, Doctype, ProcessingInstruction

WS_RE = re.compile(r"\s+")
SKIP_STRINGS = (Comment, Declaration, Doctype, CData, ProcessingInstruction)
SKIP_PARENTS = {"script", "style", "noscript", "template", "svg", "head", "title", "meta"}
NAV_SELECTORS = ("header", "nav", "footer", "[role=navigation]", "[role=banner]", "[role=contentinfo]")
LOAD_MORE_RE = re.compile(r">\s*(load|show|view|see)\s+(more|all)\b", re.I)
JS_APP_RE = re.compile(r"__NEXT_DATA__|data-reactroot|ng-version|id=\"app\"|id=\"root\"|data-v-app|x-data=", re.I)
ANCHOR_RE = re.compile(r"<a\s", re.I)


@dataclass
class Link:
    text: str
    url: str


def collapse(text: str) -> str:
    return WS_RE.sub(" ", text or "").strip()


def make_soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html or "", "lxml")
    except Exception:
        return BeautifulSoup(html or "", "html.parser")


def main_soup(html: str) -> BeautifulSoup:
    """Soup with header / nav / footer removed (main page content only)."""
    soup = make_soup(html)
    for sel in NAV_SELECTORS:
        for el in soup.select(sel):
            if not getattr(el, "decomposed", False):
                el.decompose()
    return soup


def extract_links(soup: BeautifulSoup, base_url: str) -> list[Link]:
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            continue
        try:
            url = urldefrag(urljoin(base_url, href))[0]
        except ValueError:
            continue
        if not url.startswith(("http://", "https://")):
            continue
        text = collapse(a.get_text(" ", strip=True)) or collapse(a.get("aria-label") or a.get("title") or "")
        key = (text.lower(), url)
        if key in seen:
            continue
        seen.add(key)
        out.append(Link(text, url))
    return out


def visible_text(soup: BeautifulSoup) -> str:
    parts = []
    for s in soup.find_all(string=True):
        if isinstance(s, SKIP_STRINGS) or (s.parent is not None and s.parent.name in SKIP_PARENTS):
            continue
        t = s.strip()
        if t:
            parts.append(collapse(t))
    return "\n".join(parts)


def page_title(soup: BeautifulSoup) -> tuple[str, str]:
    title = collapse(soup.title.get_text(" ", strip=True)) if soup.title else ""
    og = soup.find("meta", attrs={"property": "og:site_name"})
    return title, collapse(og.get("content", "")) if og else ""


def looks_dynamic(html: str) -> bool:
    """True when the page probably loads its list with JavaScript ("load more", JS app, very few links)."""
    return bool(LOAD_MORE_RE.search(html) or JS_APP_RE.search(html) or len(ANCHOR_RE.findall(html)) < 30)
