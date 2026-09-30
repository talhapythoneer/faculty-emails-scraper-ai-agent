"""Finding emails in HTML (mailto, plain text, [at]/[dot] obfuscation, Cloudflare protection) and filtering them."""
from __future__ import annotations

import re
from urllib.parse import unquote

from bs4.element import NavigableString, Tag

from .html_utils import SKIP_STRINGS

EMAIL_RE = re.compile(r"(?<![\w.+-])([A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})(?![\w-])")
EMAIL_FULL_RE = re.compile(r"[a-z0-9._%+'-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}")
_AT = r"(?:\s*[\[\(\{<]\s*at\s*[\]\)\}>]\s*|\s+at\s+|\s*@\s*)"
_DOT = r"(?:\s*[\[\(\{<]\s*dot\s*[\]\)\}>]\s*|\s+dot\s+|\s*\.\s*)"
OBFUSCATED_RE = re.compile(r"(?<![\w.])[A-Za-z0-9._%+-]+" + _AT + r"[A-Za-z0-9-]+(?:" + _DOT + r"[A-Za-z0-9-]+)*"
                           + _DOT + r"(?:edu|com|org|net|gov|us)(?![A-Za-z0-9])", re.I)
OBF_HINT_RE = re.compile(r"[\[\(\{<]\s*at\s*[\]\)\}>]|\S\s+at\s+\S+\s+dot\s+\S", re.I)
BAD_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "css", "js", "ico", "bmp", "tif", "tiff", "pdf", "mp4"}
BAD_DOMAINS = ("example.com", "example.edu", "domain.com", "email.com", "yourdomain.com", "sentry.io",
               "wixpress.com", "company.com", "university.edu", "school.edu")
SKIP_PARENTS = {"script", "style", "noscript", "template"}


def decode_cfemail(hexstr: str) -> str:
    """Decode Cloudflare's email protection (data-cfemail / /cdn-cgi/l/email-protection#...)."""
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))
    except ValueError:
        return ""


def deobfuscate(text: str) -> str:
    t = re.sub(r"\s*[\[\(\{<]\s*at\s*[\]\)\}>]\s*|\s+at\s+", "@", text, flags=re.I)
    t = re.sub(r"\s*[\[\(\{<]\s*dot\s*[\]\)\}>]\s*|\s+dot\s+", ".", t, flags=re.I)
    t = re.sub(r"\s*@\s*", "@", t)
    t = re.sub(r"\s*\.\s*", ".", t)
    return t.replace(" ", "")


def clean_email(raw: str) -> str:
    e = unquote(raw or "").strip().strip("<>()[]{}\"'").strip().rstrip(".,;:").lower()
    if e.startswith("mailto:"):
        e = e[7:]
    if not EMAIL_FULL_RE.fullmatch(e):
        return ""
    local, domain = e.rsplit("@", 1)
    if len(local) > 64 or domain.rsplit(".", 1)[-1] in BAD_TLDS or domain.endswith(BAD_DOMAINS):
        return ""
    return e


def _mailto_parts(href: str) -> list[str]:
    return re.split(r"[,;]", unquote(href.strip()[7:]).split("?")[0])


def find_email_nodes(soup) -> list[tuple[str, Tag]]:
    """All distinct emails on a page, in document order, with the element they were found in."""
    out, seen = [], set()

    def add(raw: str, el) -> None:
        e = clean_email(raw)
        if e and e not in seen and el is not None:
            seen.add(e)
            out.append((e, el))

    for node in soup.descendants:
        if isinstance(node, Tag):
            if node.name == "a":
                href = (node.get("href") or "").strip()
                low = href.lower()
                if low.startswith("mailto:"):
                    for part in _mailto_parts(href):
                        add(part, node)
                elif "/cdn-cgi/l/email-protection#" in low:
                    add(decode_cfemail(href.split("#", 1)[1]), node)
            cf = node.get("data-cfemail")
            if cf:
                add(decode_cfemail(cf), node)
        elif isinstance(node, NavigableString) and not isinstance(node, SKIP_STRINGS):
            parent = node.parent
            if parent is None or parent.name in SKIP_PARENTS:
                continue
            text = str(node)
            hits = [m.group(1) for m in EMAIL_RE.finditer(text)] if "@" in text else []
            if not hits and ("@" in text or OBF_HINT_RE.search(text)):
                hits = [deobfuscate(m.group(0)) for m in OBFUSCATED_RE.finditer(text)]
            for h in hits:
                add(h, parent)
    return out


def emails_in_tag(tag: Tag) -> set[str]:
    found = set()
    for m in EMAIL_RE.finditer(tag.get_text(" ")):
        e = clean_email(m.group(1))
        if e:
            found.add(e)
    anchors = ([tag] if tag.name == "a" else []) + tag.find_all("a", href=True)
    for a in anchors:
        href = (a.get("href") or "").strip()
        if href.lower().startswith("mailto:"):
            found.update(e for e in (clean_email(p) for p in _mailto_parts(href)) if e)
        elif "/cdn-cgi/l/email-protection#" in href.lower():
            e = clean_email(decode_cfemail(href.split("#", 1)[1]))
            if e:
                found.add(e)
    protected = ([tag] if tag.get("data-cfemail") else []) + tag.find_all(attrs={"data-cfemail": True})
    for el in protected:
        e = clean_email(decode_cfemail(el.get("data-cfemail", "")))
        if e:
            found.add(e)
    return found


class EmailFilter:
    """Drops mailboxes that are never useful (noreply, admissions, webmaster...)."""

    def __init__(self, cfg: dict):
        ec = cfg.get("emails", {})
        self.exact = {x.lower() for x in ec.get("generic_local_parts", [])}
        self.prefixes = tuple(x.lower() for x in ec.get("generic_local_prefixes", []))

    def is_generic(self, email: str) -> bool:
        local = email.split("@", 1)[0].lower()
        if local in self.exact or (bool(self.prefixes) and local.startswith(self.prefixes)):
            return True
        # "msu.admissions", "cas-webteam": any part that is a generic mailbox name
        parts = [x for x in re.split(r"[._+-]", local) if x]
        return len(parts) > 1 and any(x in self.exact or (self.prefixes and x.startswith(self.prefixes)) for x in parts)
