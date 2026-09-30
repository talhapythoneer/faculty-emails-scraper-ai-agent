"""Turning email occurrences on a page into contacts (name, title, role) from the page structure."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4.element import Tag

from .emails import emails_in_tag, find_email_nodes
from .html_utils import collapse

STOP_PARENTS = {"body", "html", "[document]", "main", "table", "tbody", "thead", "ul", "ol", "dl"}
PARTICLES = {"de", "del", "della", "la", "le", "van", "von", "der", "den", "da", "di", "du", "bin", "al", "el"}
NON_NAME_WORDS = {
    "department", "dept", "office", "professor", "associate", "assistant", "chair", "director", "dean",
    "coordinator", "program", "programs", "university", "college", "school", "center", "centre", "institute",
    "faculty", "staff", "directory", "email", "e-mail", "phone", "fax", "contact", "contacts", "us", "view",
    "profile", "website", "read", "more", "learn", "campus", "home", "about", "news", "events", "apply", "visit",
    "give", "search", "menu", "biology", "chemistry", "physics", "psychology", "nursing", "science", "sciences",
    "health", "engineering", "studies", "research", "undergraduate", "graduate", "admissions", "advising", "career",
    "services", "student", "students", "location", "hours", "mailing", "address", "information", "request", "info",
    "academic", "academics", "majors", "minors", "our", "meet", "team", "people", "emeritus", "emerita",
    "lecturer", "instructor", "adjunct", "visiting", "senior", "clinical", "teaching", "executive", "administrative",
    "manager", "specialist", "secretary", "connect", "follow", "share", "download", "cv", "curriculum", "vitae",
    "publications", "education", "main", "general", "inquiries", "questions", "map", "directions", "the", "and",
    "of", "for", "to", "with", "welcome", "support", "lab", "laboratory", "resources", "overview",
}
CREDENTIALS = (r"ph\.?\s?d|m\.?\s?d|ed\.?\s?d|d\.?\s?n\.?\s?p|psy\.?\s?d|j\.?\s?d|m\.?\s?s\.?\s?n|m\.?\s?p\.?\s?h|"
               r"m\.?\s?b\.?\s?a|m\.?\s?f\.?\s?a|m\.?\s?s|m\.?\s?a|r\.?\s?n|faan|facsm|atc|dvm|pharm\.?\s?d|lcsw|"
               r"ccc-slp|p\.?\s?e|cpa|mph|msn|bsn|aprn|fnp")
CRED_TAIL_RE = re.compile(r",\s*(?:" + CREDENTIALS + r")\b.*$", re.I)
PREFIX_RE = re.compile(r"^(?:dr|prof|professor|mr|mrs|ms|mx)\.?\s+", re.I)


@dataclass
class Contact:
    email: str
    name: str = ""
    title: str = ""
    role: str = "unknown"
    source_url: str = ""
    snippet: str = ""
    programs: set = field(default_factory=set)
    method: str = "rule"
    enriched: bool = False

    def merge(self, other: "Contact", roles: "RoleClassifier") -> None:
        if not self.name and other.name:
            self.name = other.name
        if roles.priority(other.role) < roles.priority(self.role):
            self.role = other.role
            self.title = other.title or self.title
            self.method = other.method
        elif not self.title and other.title:
            self.title = other.title
        if not self.snippet and other.snippet:
            self.snippet = other.snippet
        self.programs |= other.programs

    def to_dict(self, selected: bool = False) -> dict:
        return {"email": self.email, "name": self.name, "title": self.title, "role": self.role,
                "source_url": self.source_url, "programs": sorted(self.programs), "method": self.method,
                "selected": selected}


class RoleClassifier:
    def __init__(self, cfg: dict):
        rc = cfg["roles"]
        self.patterns = [(role, re.compile(p, re.I)) for role, p in rc["patterns"].items()]
        self.ignore = [re.compile(p, re.I) for p in rc.get("ignore_phrases", [])]
        self.staff = [re.compile(p, re.I) for p in rc.get("staff_phrases", [])]
        self.order = list(rc["selection_priority"])
        self.any_role = re.compile("|".join(f"(?:{p})" for p in rc["patterns"].values()), re.I)
        self.office_keywords = [k.lower() for k in rc.get("office_email_keywords", [])]

    def classify(self, text: str) -> str:
        if not text:
            return "unknown"
        t = text
        for rx in self.ignore:
            t = rx.sub(" ", t)
        for role, rx in self.patterns:
            if role == "emeritus" and rx.search(t):
                return "emeritus"
        if any(rx.search(t) for rx in self.staff):
            return "staff"
        for role, rx in self.patterns:
            if rx.search(t):
                return role
        return "unknown"

    def has_role(self, text: str) -> bool:
        return bool(self.any_role.search(text or ""))

    def priority(self, role: str) -> int:
        return self.order.index(role) if role in self.order else len(self.order)

    def is_office_email(self, email: str) -> bool:
        local = email.split("@", 1)[0].lower()
        return any(k in local for k in self.office_keywords)


def clean_person_name(text: str) -> str:
    t = collapse(re.sub(r"\([^)]*\)", " ", text or ""))
    t = CRED_TAIL_RE.sub("", t)
    t = PREFIX_RE.sub("", t)
    return collapse(t.strip(" ,;:-|"))


def _name_token_ok(tok: str) -> bool:
    if tok.lower() in PARTICLES:
        return True
    if not tok[0].isupper():
        return False
    return all(ch.isalpha() or ch in "'’-." for ch in tok)


def is_name_like(text: str) -> bool:
    t = collapse(text)
    if not t or len(t) > 45 or "@" in t or any(ch.isdigit() for ch in t):
        return False
    toks = t.replace(",", " ").split()
    if not 2 <= len(toks) <= 5 or not all(_name_token_ok(x) for x in toks):
        return False
    if {x.lower().strip(".,") for x in toks} & NON_NAME_WORDS:
        return False
    return sum(1 for x in toks if x[0].isupper()) >= 2


CARD_TITLE_START_RE = re.compile(
    r"\b(Professor|Assistant|Associate|Chair|Chairperson|Director|Lecturer|Instructor|Coordinator|Dean|Emerit\w*|"
    r"Visiting|Adjunct|Head|Senior|Program|Department|Staff|Administrative|Laboratory|Lab|Faculty|Clinical|"
    r"Executive|Manager|Specialist|Advisor|Adviser|Librarian|Registrar|President|Provost)\b")
HONORIFIC_RE = re.compile(r"^(dr|prof|mr|mrs|ms|mx|rev)\.?\s+", re.I)
DEGREE_SUFFIX_RE = re.compile(r",?\s*\b(ph\.?\s?d|m\.?d|ed\.?d|d\.?n\.?p|m\.?s\.?n|r\.?n|mba|mph)\.?\s*$", re.I)


def card_name(text: str) -> tuple[str, str]:
    """Name and title from a profile card's link text.

    'SB Dr. Sarah A. Blank Associate Professor of Anatomy; Chair of Biology' -> ('Sarah A. Blank', 'Associate ...').
    Returns ('', '') when the text does not start with a person's name.
    """
    t = collapse(text)
    if not t or "@" in t:
        return "", ""
    m = CARD_TITLE_START_RE.search(t)
    name, rest = (t[: m.start()], t[m.start():]) if m else (t, "")
    toks = name.replace(",", " , ").split()
    while len(toks) > 2 and re.fullmatch(r"[A-Z]{1,3}", toks[0]):  # initials badge in front of the name
        toks = toks[1:]
    name = HONORIFIC_RE.sub("", " ".join(toks).replace(" , ", ", ")).strip(" ,;:-–|")
    name = DEGREE_SUFFIX_RE.sub("", name).strip(" ,;:-–|")
    return (name, rest.strip(" ,;:-–|")[:160]) if is_name_like(name) else ("", "")


def text_lines(node) -> list[str]:
    raw = node.get_text("\n") if isinstance(node, Tag) else str(node)
    lines = []
    for line in raw.split("\n"):
        line = collapse(line)
        if line and (not lines or lines[-1] != line):
            lines.append(line)
    return lines[:25]


def find_card(el: Tag, email: str, max_chars: int = 700) -> Tag:
    """Climb from the email's element to the largest ancestor that still belongs to this one person."""
    best = el
    for _ in range(8):
        parent = best.parent
        if parent is None or not isinstance(parent, Tag) or parent.name in STOP_PARENTS:
            break
        if len(parent.get_text(" ", strip=True)) > max_chars:
            break
        if emails_in_tag(parent) - {email}:
            break
        best = parent
    return best


def guess_name(card: Tag, lines: list[str]) -> str:
    candidates = [collapse(t.get_text(" ", strip=True))
                  for t in card.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "strong", "b", "a", "span"], limit=40)]
    candidates += lines[:6]
    for cand in candidates:
        name = clean_person_name(cand)
        if is_name_like(name):
            return name
    return ""


def guess_title(lines: list[str], roles: RoleClassifier, email: str = "", name: str = "") -> str:
    best, best_p = "", None
    for line in lines:
        if len(line) > 160 or "@" in line or (name and line == name):
            continue
        if roles.has_role(line):
            p = roles.priority(roles.classify(line))
            if best_p is None or p < best_p:
                best, best_p = line, p
    return best


def title_near_heading(soup, roles: RoleClassifier) -> str:
    """Job title printed near the page's main heading (used on individual profile pages)."""
    heading = soup.find("h1") or soup.find("h2")
    node = heading
    for _ in range(3):
        if node is None or node.parent is None:
            break
        node = node.parent
        title = guess_title(text_lines(node), roles)
        if title:
            return title
    return ""


def extract_contacts(soup, page_url: str, roles: RoleClassifier, max_card_chars: int = 700) -> list[Contact]:
    out = []
    for email, el in find_email_nodes(soup):
        card = find_card(el, email, max_card_chars)
        lines = text_lines(card)
        name = guess_name(card, lines)
        title = guess_title(lines, roles, email, name)
        role = roles.classify(title) if title else "unknown"
        if role == "unknown" and not name and roles.is_office_email(email):
            role = "office"
        snippet = " | ".join(lines)[:500]
        out.append(Contact(email=email, name=name, title=title, role=role, source_url=page_url, snippet=snippet))
    return out
