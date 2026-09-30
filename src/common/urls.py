"""URL helpers: normalisation, registered domains, site scope."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urlparse, urlunparse

import tldextract

# Uses the public-suffix snapshot bundled with tldextract (no network call).
_EXTRACT = tldextract.TLDExtract(suffix_list_urls=())

SKIP_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".doc", ".docx", ".xls", ".xlsx",
                   ".ppt", ".pptx", ".zip", ".mp4", ".mp3", ".mov", ".ics", ".css", ".js", ".xml", ".rss")
TRACKING_PARAMS = ("utm_", "gclid", "fbclid", "mc_cid", "mc_eid", "_ga")


def ensure_scheme(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        return "https://" + url
    return url


def host_of(url_or_host: str) -> str:
    try:
        host = urlparse(ensure_scheme(url_or_host)).hostname or ""
    except ValueError:
        return ""
    return host.lower().rstrip(".")


def strip_www(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def registered_domain(url_or_host: str) -> str:
    host = host_of(url_or_host)
    if not host:
        return ""
    ext = _EXTRACT(host)
    if ext.domain and ext.suffix:
        return f"{ext.domain}.{ext.suffix}".lower()
    return host


def domain_label(url_or_host: str) -> str:
    return (_EXTRACT(host_of(url_or_host)).domain or "").lower()


def subdomain(url_or_host: str) -> str:
    return (_EXTRACT(host_of(url_or_host)).subdomain or "").lower()


def site_key(url: str, generic_subdomains) -> str:
    """Group URLs by site: 'admissions.x.edu' -> 'x.edu', but keep campus hosts like 'd.umn.edu'."""
    host = strip_www(host_of(url))
    reg = registered_domain(host)
    if not reg or host == reg:
        return reg or host
    labels = host[: -len(reg)].rstrip(".").split(".")
    return reg if any(label in generic_subdomains for label in labels) else host


def origin(url: str) -> str:
    p = urlparse(ensure_scheme(url))
    return f"{p.scheme}://{p.netloc}/"


def normalize_url(url: str) -> str:
    p = urlparse(ensure_scheme(url))
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                       if not k.lower().startswith(TRACKING_PARAMS)], doseq=True)
    path = p.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", query, ""))


def url_key(url: str) -> str:
    """Scheme- and www-insensitive key used to de-duplicate URLs."""
    return re.sub(r"^https?://(www\.)?", "", normalize_url(url))


def looks_like_url(value: str) -> bool:
    """'www.x.edu' / 'https://x.edu/a' -> True; a note such as 'permanently closed' -> False."""
    v = (value or "").strip()
    return bool(v) and not re.search(r"\s", v) and "." in host_of(v)


def is_http(url: str) -> bool:
    return urlparse(url or "").scheme in ("http", "https")


def has_skip_extension(url: str) -> bool:
    return urlparse(url or "").path.lower().endswith(SKIP_EXTENSIONS)


def path_words(url: str) -> str:
    p = urlparse(url or "")
    text = unquote(f"{p.path} {p.query}").lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def clean_google_href(href: str) -> str:
    if not href:
        return ""
    if href.startswith("/url?") or "google.com/url?" in href:
        qs = parse_qs(urlparse(href).query)
        href = (qs.get("q") or qs.get("url") or [""])[0]
    return href if href.startswith(("http://", "https://")) else ""


def is_google_url(url: str) -> bool:
    host = host_of(url)
    return bool(re.search(r"(^|\.)google\.[a-z.]+$|googleusercontent\.com$|gstatic\.com$|googleadservices\.com$|"
                          r"(^|\.)youtube\.com$", host))


class SiteScope:
    """Registered domains that belong to one institution (plus catalog vendor domains)."""

    def __init__(self, domains, extra_domains=()):
        self.domains = {d.lower() for d in domains if d}
        self.extra = {d.lower() for d in extra_domains if d}

    def add(self, domain: str) -> None:
        if domain:
            self.domains.add(domain.lower())

    def contains(self, url: str) -> bool:
        if not is_http(url):
            return False
        reg = registered_domain(url)
        return reg in self.domains or reg in self.extra
