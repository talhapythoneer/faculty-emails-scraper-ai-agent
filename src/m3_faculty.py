"""Module 3: qualifying bachelor's majors (Column E), faculty pages, and leadership emails (Column F).

Rules do the work; the AI is only asked about ambiguous program names, unstructured program pages,
picking a faculty link when no rule matches, and naming contacts whose card has no clear name/title.
"""
from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from .common.browser import close_browser, get_browser
from .common.config import resolve_path
from .common.contacts import (Contact, RoleClassifier, card_name, extract_contacts, is_name_like,
                              title_near_heading)
from .common.db import DB
from .common.emails import EmailFilter
from .common.excel import (Institution, filter_institutions, load_institutions, read_table, sync_overrides,
                           write_final)
from .common.fetch import Fetcher, Page, sitemap_urls
from .common.google import Search
from .common.html_utils import Link, collapse, extract_links, looks_dynamic, main_soup, visible_text
from .common.llm import LLM
from .common.majors import (DEGREE_MARKER_RE, MajorMatcher, clean_program_name, core_name, degree_code,
                            program_key, slug_variants)
from .common.pagination import collect_pages
from .common.runner import run_parallel
from .m2_programs import NOTE_MODULE, sync_client_notes, sync_m2_overrides
from .common.scoring import link_score, norm
from .common.urls import (SiteScope, has_skip_extension, looks_like_url, path_words, registered_domain, site_key,
                          url_key)

log = logging.getLogger("m3")
MODULE = "m3"
OFFICE_PROGRAM = "(institution offices)"
LEADERSHIP = ("chair", "program_director", "dean", "coordinator")

PROGRAM_COLUMNS = ["unitid", "institution", "state", "program", "decision", "method", "url", "reason"]
CONTACT_COLUMNS = ["unitid", "institution", "state", "department", "name", "title", "role", "email", "selected",
                   "programs", "source_url", "method"]
ROLE_ORDER = {"chair": 0, "program_director": 1, "dean": 2, "coordinator": 3, "office": 4, "professor": 5,
              "staff": 6, "unknown": 7, "emeritus": 8}

# Words that describe a program or a job rather than a subject (used to tell which department a title names).
GENERIC_SUBJECT_WORDS = {"science", "sciences", "studies", "study", "program", "programs", "degree", "major",
                         "applied", "general", "bachelor", "arts", "and", "with", "track", "concentration", "option",
                         "emphasis", "honors", "online", "combined", "accelerated"}
TITLE_ROLE_WORDS = {"chair", "chairs", "chairperson", "chairman", "chairwoman", "co", "vice", "department",
                    "departments", "dept", "head", "dean", "deans", "associate", "assistant", "assoc", "asst",
                    "director", "directors", "program", "programs", "prgrm", "coordinator", "coordinators", "of",
                    "the", "and", "for", "in", "at", "a", "an", "or", "school", "college", "division", "faculty",
                    "professor", "professors", "prof", "interim", "acting", "undergraduate", "graduate", "studies",
                    "senior", "lecturer", "instructor", "adjunct", "emeritus", "emerita", "full", "part", "time",
                    "f", "t", "ft", "pt", "visiting", "distinguished", "endowed", "chaired", "university", "academic",
                    "affairs", "advisor", "adviser", "advising", "staff", "member", "principal", "research",
                    "scientist", "center", "centre", "office", "executive", "administrative", "administrator",
                    "manager", "specialist", "with", "phd", "dr", "md", "rn", "edd", "ms", "ma", "mba", "mph", "bsn",
                    "msn", "dnp", "secretary", "liaison", "chief", "officer", "founding", "teaching", "clinical",
                    "tenure", "track", "term", "lab", "laboratory", "support", "services", "coordinating"}
# Titles naming only these broad areas fit any major ("Dean, College of Arts and Sciences", "Chair, Natural Sciences").
NO_MAJORS_NOTE_RE = re.compile(r"\bno\s+(qualifying|bachelor|undergraduate|target)", re.I)
JUNK_NAME_RE = re.compile(r"web\s?page|website|pronoun|gender|let.s\s+talk|\bemail\b|\bcontact\b|\bprofile\b|"
                          r"\bview\b|\bvisit\b|\bfaculty\b|\bstaff\b|\bdepartment\b|\bdegree\b|\boffice\b|\bjoin\b|"
                          r"\bzoom\b|\btour\b|\bclick\b|\bhere\b", re.I)
# stems that also start unrelated words: physi(cs) vs physi(cal plant), commu(nication) vs commu(nity)
STEM_NOT = {"physi": "cal", "commu": "nit"}
BROAD_SUBJECT_WORDS = {"natural", "science", "sciences", "stem", "mathematics", "math", "liberal", "arts", "health",
                       "medical", "professions", "professional", "pre", "life", "physical", "allied"}
FACULTY_PAGE_COLUMNS = ["unitid", "institution", "state", "program", "faculty_page_url", "emails_found", "method"]
REVIEW_COLUMNS = ["unitid", "institution", "state", "issues", "website_url", "programs_url", "m1_confidence",
                  "m2_confidence", "degrees_found", "emails_found", "notes"]


@dataclass
class Program:
    name: str
    url: str = ""
    method: str = "rule"
    keyword: str = ""


@dataclass
class Ctx:
    inst: Institution
    website: str
    programs_url: str
    reg: str
    scope: SiteScope
    site: str = ""                                        # campus host or domain for Google "site:" searches
    google_budget: int = 0
    google_done: set = field(default_factory=set)         # department names already searched
    requests: int = 0
    budget_hit: bool = False
    notes: list = field(default_factory=list)
    contacts: dict = field(default_factory=dict)          # email -> Contact
    dir_emails: dict = field(default_factory=dict)        # faculty page key -> emails found there
    wide_pages: set = field(default_factory=set)          # faculty page keys that turned out campus-wide
    prog_page_emails: dict = field(default_factory=dict)  # program page key -> emails found for it
    faculty_pages: list = field(default_factory=list)
    sitemap: list | None = None
    directories: list | None = None                       # campus directory index pages (found once)
    forced_wide: set = field(default_factory=set)         # directory pages: always keep only people naming the dept


PROFILE_HOSTS = ("directory.", "people.", "faculty.", "profiles.", "experts.")
DIRECTORY_TEXT_RE = re.compile(r"\b(faculty\s*(&|and)?\s*staff\s+directory|faculty\s+directory|staff\s+directory|"
                               r"campus\s+directory|people\s+directory|employee\s+directory)\b|^directory$", re.I)


def _dir_path(path: str) -> str:
    """'/directory/index.php' -> '/directory/' (an index page is its folder)."""
    return re.sub(r"/(index|default|home)\.[a-z0-9]+$", "/", path or "/")


TABLE_DEGREE_RE = re.compile(r"(?:B\.?\s?[ASF]\.?|B\.?\s?S\.?\s?N\.?|B\.?\s?B\.?\s?A\.?|B\.?\s?S\.?\s?W\.?|B\.?\s?M\.?|"
                             r"B\.?\s?A\.?\s?S\.?)(?:\s?(?:/|,|or|&)\s?(?:B\.?\s?[ASF]\.?|B\.?\s?S\.?\s?N\.?))*", re.I)
CARD_LINK_TEXT = {"view program page", "program page", "view program", "learn more", "read more", "more info",
                  "view details", "details", "explore", "explore program", "explore this program", "on campus",
                  "online", "hybrid", "on-campus", "in person", "learn more about this program"}
CARD_TITLE_SEL = "h1, h2, h3, h4, h5, h6, [class*='title'], [class*='name'], [class*='heading'], strong"


def _card_title(a) -> str:
    """Title of the card around a 'View Program Page' / 'On Campus' link: the nearest heading inside the same card."""
    node = a
    for _ in range(5):
        node = node.parent
        if node is None or node.name in ("body", "main", "html"):
            return ""
        if len(node.find_all("a", href=True)) > 6:
            return ""  # left the card: this container holds several programs
        for el in node.select(CARD_TITLE_SEL):
            if el is a or a in el.descendants:
                continue
            t = collapse(el.get_text(" ", strip=True))
            if 3 <= len(t) <= 90 and t.lower() not in CARD_LINK_TEXT:
                return t
    return ""


BUTTON_PREFIX_RE = re.compile(r"^(read\s+more(\s+about)?|learn\s+more(\s+about)?|view|visit(\s+the)?|explore|discover|"
                              r"see)\s+", re.I)
BUTTON_SUFFIX_RE = re.compile(r"\s*(program\s+page|program\s+details|page|learn\s+more|read\s+more|details)\s*$", re.I)
DELIVERY_SUFFIX_RE = re.compile(r"\s+[-–—]\s+(on[\s-]campus|online|hybrid|in[\s-]person)\s*$", re.I)
CREDENTIAL_RE = re.compile(r"(,?\s+(ph\.?\s?d|ed\.?\s?d|d\.?\s?b\.?\s?a|m\.?\s?b\.?\s?a|m\.?\s?s\.?\s?n?|m\.?\s?a|m\.?\s?ed|"
                           r"b\.?\s?[as]\.?|b\.?\s?s\.?\s?n|r\.?\s?n|d\.?\s?n\.?\s?p|m\.?\s?d|j\.?\s?d|lcsw|lpc|cpa|pe|"
                           r"faan|cne|atc|pt|dpt)\b\.?.*$)", re.I)


def display_program_name(name: str) -> str:
    """'READ MORE Biology' / 'View Biology (BS) Program Page' / 'Biology, BS - On Campus' -> the program name."""
    t = collapse(name)
    for _ in range(2):
        t = BUTTON_PREFIX_RE.sub("", t)
        t = BUTTON_SUFFIX_RE.sub("", t)
        t = DELIVERY_SUFFIX_RE.sub("", t)
    return t.strip(" -–|,:") or collapse(name)


def clean_degree_list(text: str, cfg: dict) -> str:
    """Column E: clean each name, drop duplicates that appear after cleaning (case-insensitive)."""
    sep = (cfg.get("output", {}) or {}).get("degree_separator", "; ")
    out, seen = [], set()
    for part in (text or "").split(sep.strip()):
        name = display_program_name(part)
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return sep.join(out)


def clean_person_name(name: str) -> str:
    """'George Allen, D.B.A.' -> 'George Allen'; "Stephanie Keeley '07, M.B.A." -> 'Stephanie Keeley';
    headings caught as names ('DEGREES OFFERED', 'Course Requirements') -> ''."""
    t = collapse(name)
    t = re.sub(r"\s+[’'‘]\d{2}\b", "", t)          # class year
    t = CREDENTIAL_RE.sub("", t).strip(" ,;-–")
    if not t or JUNK_NAME_RE.search(t) or re.search(r"\b(offered|requirements?|history|program|programs|degrees?|"
                                                    r"course|courses|major|minor|logo)\b", t, re.I):
        return ""
    return t if is_name_like(t) else ""


def _dept_label(programs) -> str:
    """Department column of the Contacts sheet: the majors' core names ('Biology, B.S.' -> 'Biology')."""
    names = {}
    for p in programs:
        if p == OFFICE_PROGRAM:
            label = "Institution offices"
        else:
            label = re.sub(r"^[\s,;:–—-]+|[\s,;:–—-]+$", "", core_name(p)) or p
        names.setdefault(label.lower(), label)
    return "; ".join(sorted(names.values(), key=str.lower))


def _shared_segments(a: str, b: str) -> int:
    sa, sb = [s for s in a.split("/") if s], [s for s in b.split("/") if s]
    n = 0
    for x, y in zip(sa, sb):
        if x != y:
            break
        n += 1
    return n


class FacultyExtractor:
    def __init__(self, cfg: dict, db: DB, fetcher: Fetcher, llm: LLM, search: Search, browser_provider):
        self.cfg = cfg
        self.mc = cfg["m3"]
        self.db = db
        self.fetcher = fetcher
        self.llm = llm
        self.search = search
        self.browser_provider = browser_provider
        self.generic = set(cfg["m1"]["generic_subdomains"])
        self.matcher = MajorMatcher(cfg)
        self.roles = RoleClassifier(cfg)
        self.email_filter = EmailFilter(cfg)
        self.fac_weights = self.mc["faculty_link_keywords"]
        self.office_weights = self.mc["office_link_keywords"]
        rejects = [r.lower() for r in self.mc.get("faculty_url_reject", []) if r]
        self.reject_re = re.compile("|".join(re.escape(r) for r in rejects)) if rejects else None
        self.reject_hosts = tuple(h.lower() for h in self.mc.get("faculty_host_reject", []) if h)
        pats = [x for x in self.mc.get("exclude_title_patterns", []) if x]
        self.exclude_title_re = re.compile("|".join(f"(?:{x})" for x in pats), re.I) if pats else None
        self.program_url_reject = tuple(x.lower() for x in self.mc.get("program_url_reject", []) if x)
        self.vendor_domains = cfg["m2"].get("catalog_vendor_domains", [])
        out = cfg.get("output", {})
        self.email_sep = out.get("email_separator", "; ")
        self.trailing_sep = bool(out.get("trailing_separator", True))
        self.degree_sep = out.get("degree_separator", "; ")

    # ------------------------------------------------------------------ main
    def process(self, inst: Institution, website: str, programs_url: str, page_type: str) -> dict:
        mc = self.mc
        base = website or programs_url
        if not base:
            return {"status": "no_website", "notes": "No website / programs URL from Modules 1-2"}
        reg = registered_domain(base)
        domains = {reg}
        programs_reg = registered_domain(programs_url) if programs_url else ""
        if programs_reg and programs_reg not in self.vendor_domains:
            domains.add(programs_reg)
        if self.search.is_api:
            budget = int(mc.get("google_per_institution_api", 30))
        else:
            budget = int(mc.get("google_fallback_per_institution", 0)) if self.search.available else 0
        ctx = Ctx(inst=inst, website=website or programs_url, programs_url=programs_url or website, reg=reg,
                  scope=SiteScope(domains, self.vendor_domains), site=site_key(base, self.generic),
                  google_budget=budget)

        pages = self._listing_pages(ctx, page_type)
        programs, rejected = self._classify(ctx, self._entries(pages), pages)
        if not programs:
            ctx.notes.append("no qualifying majors found on the programs page")

        self._contacts_for_programs(ctx, programs)
        self._finalize_contacts(ctx)
        selected = self._select(ctx)
        if len(selected) < int(mc.get("office_fallback_below", 0)) and self._office_fallback(ctx):
            self._finalize_contacts(ctx)
            selected = self._select(ctx)

        blocked = sorted(h for h in self.fetcher.blocked_hosts if ctx.scope.contains(f"https://{h}/"))
        if blocked:
            ctx.notes.append(f"website blocks automated access (CAPTCHA: {', '.join(blocked)}) - contacts may be missing")

        chosen = set(selected)
        degrees = list(dict.fromkeys(p.name for p in programs))
        emails = self.email_sep.join(selected)
        if emails and self.trailing_sep:
            emails += self.email_sep.strip()
        return {
            "status": "ok",
            "website_url": ctx.website,
            "programs_url": ctx.programs_url,
            "bacc_degrees": self.degree_sep.join(degrees),
            "emails": emails,
            "programs": [{"name": p.name, "url": p.url, "method": p.method} for p in programs],
            "rejected_programs": rejected[:300],
            "contacts": [self._contact_out(c, c.email in chosen) for c in ctx.contacts.values()],
            "faculty_pages": ctx.faculty_pages,
            "counts": {"degrees": len(degrees), "contacts": len(ctx.contacts), "selected": len(selected),
                       "requests": ctx.requests},
            "notes": "; ".join(dict.fromkeys(ctx.notes)),
        }

    def _fetch(self, ctx: Ctx, url: str, **kwargs) -> Page | None:
        if ctx.requests >= int(self.mc["max_requests_per_institution"]):
            if not ctx.budget_hit:
                ctx.budget_hit = True
                ctx.notes.append("page budget reached - some pages skipped")
            return None
        ctx.requests += 1
        return self.fetcher.get(url, **kwargs)

    def _scoped_links(self, ctx: Ctx, page: Page, main_only: bool = False) -> list[Link]:
        soup = main_soup(page.html) if main_only else page.soup()
        return [l for l in extract_links(soup, page.final_url)
                if ctx.scope.contains(l.url) and not has_skip_extension(l.url)]

    # --------------------------------------------------------- 3a: programs
    def _listing_pages(self, ctx: Ctx, page_type: str) -> list[Page]:
        mc = self.mc

        def fetch(u):
            return self._fetch(ctx, u)

        render_first = page_type == "js_finder" and self.browser_provider is not None
        start = self._fetch(ctx, ctx.programs_url, render=render_first, expand=render_first)
        if render_first and (start is None or not start.ok):
            start = self._fetch(ctx, ctx.programs_url)
        if start is None or not start.ok:
            ctx.notes.append("programs page could not be loaded")
            return []
        pages = collect_pages(start, fetch, int(mc["max_listing_pages"]))
        if self.browser_provider is not None and not render_first:
            found = self._candidate_count(pages)
            if found < int(mc["render_if_programs_below"]):  # too few majors: the list may be drawn by JavaScript
                rendered = self._fetch(ctx, ctx.programs_url, render=True, expand=True)
                if rendered is not None and rendered.ok:
                    rpages = collect_pages(rendered, fetch, int(mc["max_listing_pages"]))
                    if self._candidate_count(rpages) > found:
                        pages = rpages
                        ctx.notes.append("programs page rendered in Chrome")
        return pages

    def _candidate_count(self, pages: list[Page]) -> int:
        # real major matches only: a few vague menu links ("Health Plan...") must not stop the Chrome render
        return sum(1 for e in self._entries(pages) if self.matcher.classify(e.text)[0] == "include")

    def _entries(self, pages: list[Page]) -> list[Link]:
        out, seen = [], set()
        for p in pages:
            soup = main_soup(p.html)
            links = extract_links(soup, p.final_url)
            if len(links) < 5:
                links = extract_links(p.soup(), p.final_url)
            for link in links:
                name = clean_program_name(link.text)
                if not name or has_skip_extension(link.url):
                    continue
                key = (norm(name), url_key(link.url))
                if key not in seen:
                    seen.add(key)
                    out.append(Link(name, link.url))
            # program cards whose link only says "View Program Page" / "On Campus" / "Online": the name is the card's
            # title (MGH, Westminster)
            for a in soup.find_all("a", href=True):
                text = collapse(a.get_text(" ", strip=True)).lower().strip(" ›»>")
                if text not in CARD_LINK_TEXT:
                    continue
                url = urljoin(p.final_url, a["href"])
                title = _card_title(a)
                name = clean_program_name(title) if title else ""
                if not name or has_skip_extension(url):
                    continue
                key = (norm(name), url_key(url))
                if key not in seen:
                    seen.add(key)
                    out.append(Link(name, url))
            # program tables: "Art | BA | X | Visual & Performing Arts" (degree in its own column, Worcester State)
            for tr in soup.find_all("tr"):
                cells = tr.find_all(["td", "th"])
                if len(cells) < 2:
                    continue
                texts = [collapse(c.get_text(" ", strip=True)) for c in cells]
                deg = next((t for t in texts[1:] if TABLE_DEGREE_RE.fullmatch(t)), "")
                name = clean_program_name(texts[0])
                if not deg or not name or not (3 <= len(name) <= 90) or TABLE_DEGREE_RE.fullmatch(name):
                    continue
                a = cells[0].find("a", href=True)
                url = urljoin(p.final_url, a["href"]) if a else ""
                key = (norm(name), url_key(url) if url else "")
                if key not in seen:
                    seen.add(key)
                    out.append(Link(f"{name}, {deg}", url))
            # programs printed as plain text, e.g. "<li>Biology (B.S.)</li>"
            for el in soup.find_all(["li", "td", "h2", "h3", "h4", "h5"]):
                if el.find("a"):
                    continue
                text = collapse(el.get_text(" ", strip=True))
                if not (3 <= len(text) <= 90) or not DEGREE_MARKER_RE.search(text):
                    continue
                key = (norm(text), "")
                if key not in seen:
                    seen.add(key)
                    out.append(Link(text, ""))
        return out

    def _classify(self, ctx: Ctx, entries: list[Link], pages: list[Page]) -> tuple[list[Program], list[dict]]:
        mc = self.mc
        included: list[Program] = []
        ambiguous: list[Link] = []
        rejected: list[dict] = []
        for e in entries:
            if e.url and any(x in urlparse(e.url).path.lower() for x in self.program_url_reject):
                continue  # e.g. /student-life/resources/well-being-and-fitness is not a major
            kind, info = self.matcher.classify(e.text)
            if kind == "include":
                included.append(Program(e.text, e.url, "rule", info))
            elif kind == "ambiguous":
                ambiguous.append(e)
            elif kind == "excluded" and (self.matcher.target(e.text) or self.matcher.is_ambiguous(e.text)):
                rejected.append({"name": e.text, "url": e.url, "reason": f"excluded by rule {info}"})

        if ambiguous:
            batch = ambiguous[: int(mc["max_ambiguous_for_llm"])]
            decisions = (self.llm.classify_programs(ctx.inst.unitid, ctx.inst.name, [a.text for a in batch])
                         if self.llm.enabled else None)
            for i, a in enumerate(ambiguous):
                if decisions is not None and decisions.get(i):
                    included.append(Program(a.text, a.url, "ai"))
                elif decisions is not None and i < len(batch):
                    rejected.append({"name": a.text, "url": a.url, "reason": "ambiguous - AI said not a match"})
                else:
                    rejected.append({"name": a.text, "url": a.url, "reason": "ambiguous - not checked by AI"})

        if len(included) < int(mc["llm_text_fallback_below"]) and self.llm.enabled and pages:
            included += self._ai_programs_from_text(ctx, pages, {norm(p.name) for p in included})

        # one entry per major + degree: "B.A. in Psychology" and "Bachelor of Arts in Psychology" are the same
        unique, seen = [], set()
        with_degree = {program_key(p.name) for p in included if degree_code(p.name)}
        for p in included:
            pk, code = program_key(p.name), degree_code(p.name)
            if not code and pk in with_degree:
                continue  # "Psychology" next to "Psychology, B.A."
            if (pk, code) not in seen:
                seen.add((pk, code))
                unique.append(p)
        return unique, rejected

    def _ai_programs_from_text(self, ctx: Ctx, pages: list[Page], known: set) -> list[Program]:
        mc = self.mc
        text = "\n".join(visible_text(main_soup(p.html)) for p in pages)[: int(mc["llm_text_max_chars"])]
        if len(DEGREE_MARKER_RE.findall(text)) < 3:
            return []
        links, seen = [], set()
        for p in pages:
            for link in self._scoped_links(ctx, p, main_only=True):
                if link.text and url_key(link.url) not in seen:
                    seen.add(url_key(link.url))
                    links.append(link)
        links = links[: int(mc.get("llm_text_max_links", 80))]
        items = self.llm.extract_programs_from_text(ctx.inst.unitid, ctx.inst.name, text, links) or []
        page_text = norm(text)
        out = []
        for item in items:
            name = collapse(item.get("name", ""))
            if not name or norm(name) in known or norm(name) not in page_text or self.matcher.excluded(name):
                continue
            idx = item.get("link_index", -1)
            url = links[idx].url if isinstance(idx, int) and 0 <= idx < len(links) else ""
            out.append(Program(name, url, "ai_text"))
            known.add(norm(name))
        if out:
            ctx.notes.append(f"{len(out)} majors read from page text by AI")
        return out

    # ------------------------------------------------ 3b/3c: faculty contacts
    def _contacts_for_programs(self, ctx: Ctx, programs: list[Program]) -> None:
        limit = int(self.mc["max_programs_for_contacts"])
        for prog in programs[:limit]:
            if ctx.budget_hit:
                break
            page = None
            key = url_key(prog.url) if prog.url else ""
            if prog.url and ctx.scope.contains(prog.url) and not has_skip_extension(prog.url):
                if key in ctx.prog_page_emails:  # several degrees share one program page
                    self._attach(ctx, ctx.prog_page_emails[key], prog.name)
                    continue
                page = self._fetch(ctx, prog.url)
            found: set[str] = set()
            if page is not None and page.ok:
                found |= self._harvest(ctx, page, prog.name)
            tries = 0
            for fac_url in self._find_faculty_pages(ctx, prog, page):  # best first, fetched lazily
                got = self._harvest_directory(ctx, fac_url, prog.name)
                found |= got
                tries += 1
                useful = [e for e in got if not self.email_filter.is_generic(e)]
                if useful or tries >= int(self.mc.get("max_faculty_page_tries", 5)):
                    break
            if key:
                ctx.prog_page_emails[key] = found
        if len(programs) > limit:
            ctx.notes.append(f"contacts searched for the first {limit} of {len(programs)} majors")

    @staticmethod
    def _attach(ctx: Ctx, emails, prog_name: str) -> None:
        for e in emails:
            c = ctx.contacts.get(e)
            if c is not None:
                c.programs.add(prog_name)

    def _harvest(self, ctx: Ctx, page: Page, prog_name: str, name_hint: str = "", title_hint: str = "") -> set[str]:
        found = extract_contacts(page.soup(), page.final_url, self.roles, int(self.mc["max_card_chars"]))
        if name_hint and found:
            self._apply_name_hint(page, found, name_hint, title_hint)
        emails = set()
        for c in found:
            c.programs.add(prog_name)
            if c.email in ctx.contacts:
                ctx.contacts[c.email].merge(c, self.roles)
            else:
                ctx.contacts[c.email] = c
            emails.add(c.email)
        return emails

    def _apply_name_hint(self, page: Page, found: list[Contact], name_hint: str, title_hint: str = "") -> None:
        """On a profile page, give the person's name (from the directory link) to their own email."""
        words = norm(name_hint).split()
        last = words[-1] if words else ""
        target = found[0] if len(found) == 1 else None
        if target is None and len(last) >= 3:
            target = next((c for c in found if last[:5] in c.email.split("@")[0]), None)
        if target is None:
            return
        if not target.name:
            target.name = name_hint
        if not target.title:
            # the card on the listing is the most reliable title ("…; Chair of the Biology Department"); a heading on
            # the profile page can be a breadcrumb such as "Faculty & Staff"
            title = title_hint or title_near_heading(page.soup(), self.roles)
            if title:
                target.title = title
                target.role = self.roles.classify(title)

    def _harvest_directory(self, ctx: Ctx, url: str, prog_name: str) -> set[str]:
        mc = self.mc
        key = url_key(url)
        if key in ctx.dir_emails:
            emails = ctx.dir_emails[key]
            if key in ctx.wide_pages:
                emails = self._only_dept(ctx, emails, prog_name)
            self._attach(ctx, emails, prog_name)
            return set(emails)
        ctx.dir_emails[key] = set()
        before = {e for e, c in ctx.contacts.items() if prog_name in c.programs}
        start = self._fetch(ctx, url)
        if start is None or not start.ok:
            return set()
        pages = collect_pages(start, lambda u: self._fetch(ctx, u), int(mc["max_directory_pages"]))
        found: set[str] = set()
        for p in pages:
            found |= self._harvest(ctx, p, prog_name)
        method = "listing"

        def named(emails):
            return [e for e in emails if e in ctx.contacts and ctx.contacts[e].name]

        # the list may be drawn by JavaScript (Berea): open it in Chrome when the page shows neither emails nor
        # profile cards
        if (len(named(found)) < 2 and self.browser_provider is not None and mc.get("render_if_no_emails", True)
                and start.via != "browser"
                and ("email" in start.html.lower() or not self._profile_links(ctx, pages))):
            rendered = self._fetch(ctx, url, render=True, expand=True)
            if rendered is not None and rendered.ok:
                new = self._harvest(ctx, rendered, prog_name) - found
                if new or len(self._profile_links(ctx, [rendered])) > len(self._profile_links(ctx, pages)):
                    found |= new
                    pages = [rendered]
                    method = "rendered"
        if len(named(found)) < 2:
            from_profiles = self._follow_profiles(ctx, pages, prog_name)
            if from_profiles:
                found |= from_profiles
                method = "profiles"
        ctx.dir_emails[key] = found
        if len(found) > int(mc.get("wide_page_emails", 60)) or key in ctx.forced_wide:
            # a campus-wide directory: keep only the people whose title / listing names this department
            ctx.wide_pages.add(key)
            kept = self._only_dept(ctx, found, prog_name)
            for e in found - kept - before:
                ctx.contacts[e].programs.discard(prog_name)
            ctx.notes.append(f"campus-wide directory for {prog_name}: kept {len(kept)} of {len(found)}")
            found, method = kept, method + ", campus-wide"
        ctx.faculty_pages.append({"program": prog_name, "url": start.final_url, "emails": len(found),
                                  "method": method})
        return found

    def _only_dept(self, ctx: Ctx, emails, prog_name: str) -> set[str]:
        stems = self._stems(prog_name)
        return {e for e in emails if e in ctx.contacts
                and self._mentions(f"{ctx.contacts[e].title} {ctx.contacts[e].snippet}", stems)}

    def _fits_program(self, c: Contact, prog: str, strict: bool = False) -> bool:
        """False when the person's title names another department ('Department Chair, Art & Design' for Biology).

        strict=True (fallback professors): the title or the page must name the department itself.
        """
        stems = self._stems(prog)
        if not stems:
            return True
        # the page PATH only: "/people/?department=biology" is often a JS filter over everyone (EKU)
        src_path = urlparse(c.source_url).path.lower()
        if (self._mentions(re.sub(r"[^a-z0-9]+", " ", src_path), stems)
                or any(sl in src_path for sl in slug_variants(core_name(prog)))):
            return True  # found on this department's own pages
        subject = [w for w in norm(c.title).split() if w not in TITLE_ROLE_WORDS and not w.isdigit()]
        if subject and self._mentions(" ".join(subject), stems):
            return True
        if strict:
            return False
        return not subject or all(w in BROAD_SUBJECT_WORDS for w in subject)

    def _profile_links(self, ctx: Ctx, pages: list[Page]) -> list[tuple]:
        """Links to one person's profile: (priority, order, name, title, url), chairs / directors first.

        A profile link starts with a person's name, and its URL is a profile path (/people/, /faculty-staff/...), sits
        under the listing page (/biology/faculty-staff/dr-sarah-blank), or is on a directory sub-site
        (directory.midway.edu/terry-adams.html).
        """
        keywords = [k.lower() for k in self.mc["profile_url_keywords"]]
        cands, seen = [], set()
        for p in pages:
            base = url_key(p.final_url)
            base_path = _dir_path(urlparse(p.final_url).path.lower()).rstrip("/")
            for a in p.soup().find_all("a", href=True):
                name, title = card_name(a.get_text(" ", strip=True))
                if not name:
                    continue
                url = urljoin(p.final_url, a["href"])
                key = url_key(url)
                if key in seen or key == base or not ctx.scope.contains(url) or not self._page_ok(url):
                    continue
                parts = urlparse(url)
                path, host = parts.path.lower(), (parts.hostname or "").lower()
                if not (any(k in path for k in keywords) or (base_path and path.startswith(base_path + "/"))
                        or host.startswith(PROFILE_HOSTS)):
                    continue  # "Impartial Love" / "Serving Appalachia" menu links are not people
                seen.add(key)
                holder = a.parent.parent if a.parent is not None and a.parent.parent is not None else a.parent
                context = f"{title} " + (collapse(holder.get_text(" ", strip=True))[:300] if holder is not None else "")
                cands.append((self.roles.priority(self.roles.classify(context)), len(cands), name, title, url))
        cands.sort()
        return cands

    def _follow_profiles(self, ctx: Ctx, pages: list[Page], prog_name: str) -> set[str]:
        found: set[str] = set()
        for _, _, name, title, url in self._profile_links(ctx, pages)[: int(self.mc["max_profiles_per_directory"])]:
            page = self._fetch(ctx, url)
            if page is not None and page.ok:
                found |= self._harvest(ctx, page, prog_name, name_hint=name, title_hint=title)
        return found

    def _directory_pages(self, ctx: Ctx) -> list[str]:
        """Campus faculty / staff directory index pages (found once per institution)."""
        if ctx.directories is None:
            ctx.directories = []
            cands = []
            home = self._fetch(ctx, ctx.website)
            if home is not None and home.ok:
                cands += [l.url for l in self._scoped_links(ctx, home)
                          if DIRECTORY_TEXT_RE.search(collapse(l.text)) and self._page_ok(l.url)]
            cands.append(f"https://directory.{ctx.reg}/")
            for u in list(dict.fromkeys(cands))[:3]:
                page = self._fetch(ctx, u, allow_browser=u.startswith("https://directory.") is False)
                if page is not None and page.ok and page.is_html and ctx.scope.contains(page.final_url):
                    if url_key(page.final_url) not in {url_key(d) for d in ctx.directories}:
                        ctx.directories.append(page.final_url)
        return ctx.directories

    # ---------------------------------------------- department relevance (precision rules)
    @staticmethod
    def _stems(program: str) -> set[str]:
        """'Bachelor of Science in Biochemistry and Molecular Biology' -> {'bioch', 'molec', 'biolo'}."""
        words = norm(core_name(program) or program).split()
        return {w[:5] for w in words if len(w) >= 4 and w not in GENERIC_SUBJECT_WORDS}

    @staticmethod
    def _mentions(text: str, stems: set[str], slugs: list[str] = ()) -> bool:
        t = norm(text)
        return (any(re.search(rf"\b{re.escape(st)}" + (f"(?!{STEM_NOT[st]})" if st in STEM_NOT else ""), t)
                    for st in stems)
                or any(sl and sl in text.lower() for sl in slugs))

    def _page_ok(self, url: str) -> bool:
        """News / blog / event pages and athletics / alumni sub-sites are never faculty pages."""
        parts = urlparse(url)
        host = (parts.hostname or "").lower()
        if any(host.startswith(h) for h in self.reject_hosts):
            return False
        path = parts.path.lower()
        if re.search(r"/(19|20)\d{2}/\d{1,2}/", path):  # dated posts: /2026/05/new-faculty-hires/
            return False
        return not (self.reject_re and self.reject_re.search(path))

    def _contact_out(self, c: Contact, selected: bool) -> dict:
        """Contact for the output: linked only to the majors their title / page fits (the Contacts sheet department)."""
        d = c.to_dict(selected=selected)
        d["programs"] = [p for p in d["programs"] if p == OFFICE_PROGRAM or self._fits_program(c, p)]
        if d["name"] and JUNK_NAME_RE.search(d["name"]):
            d["name"] = ""  # "Amy's Webpage", "Preferred Gender Pronouns", "Let's Talk": page text, not a name
        return d

    def _link_fits_dept(self, link: Link, current_url: str, slugs: list[str], stems: set[str]) -> bool:
        """The link names the department (URL or text), or sits in the same site section as the current page."""
        path = urlparse(link.url).path.lower()
        if any(sl in path for sl in slugs) or self._mentions(f"{link.text} {path_words(link.url)}", stems):
            return True
        return _shared_segments(_dir_path(path), _dir_path(urlparse(current_url).path.lower())) >= 2

    def _faculty_link_score(self, link: Link, current_url: str, slugs: list[str]) -> float:
        s = link_score(link.text, link.url, self.fac_weights)
        if s <= 0:
            return s
        path = urlparse(link.url).path.lower()
        has_slug = bool(slugs) and any(sl in path for sl in slugs)
        if has_slug:
            s += 15
        elif _shared_segments(path, urlparse(current_url).path.lower()) >= 2:
            s += 10
        if _dir_path(path).strip("/").count("/") == 0 and not has_slug:
            s -= 15  # "/directory", "/directory/index.php" style campus-wide directories
        return s

    def _best_faculty_link(self, links: list[Link], current_url: str, slugs: list[str], stems: set[str]) -> str:
        threshold = float(self.mc["faculty_link_threshold"])
        current = url_key(current_url)
        best, best_score = "", threshold - 0.001
        for link in links:
            if url_key(link.url) == current or not self._page_ok(link.url):
                continue
            if not self._link_fits_dept(link, current_url, slugs, stems):
                continue  # e.g. the Political Science faculty link on a Psychology page
            s = self._faculty_link_score(link, current_url, slugs)
            if s > best_score:
                best, best_score = link.url, s
        return best

    @staticmethod
    def _dept_score(link: Link, core: str, current_url: str) -> float:
        if url_key(link.url) == url_key(current_url):
            return 0.0
        c = norm(core)
        if not c:
            return 0.0
        t, u = norm(link.text), path_words(link.url)
        s = 0.0
        if c in t:
            s += 20
        if c in u:
            s += 10
        if re.search(r"\b(department|dept|school|division|program|home)\b", t):
            s += 10
        if re.search(r"\b(department|departments|dept|school|division)\b", u):
            s += 5
        if re.search(r"\b(apply|admission|admissions|tuition|course|courses|catalog|news|event|events|minor|"
                     r"career|careers)\b", t):
            s -= 20
        return s

    def _find_faculty_pages(self, ctx: Ctx, prog: Program, page: Page | None):
        """Faculty-page candidates, best first. The caller stops at the first page that lists emails, so a wrong
        pick (e.g. a news story about "new faculty") falls through to the next source instead of ending the search."""
        mc = self.mc
        seen: set[str] = set()

        def fresh(u: str) -> bool:
            if not u or url_key(u) in seen:
                return False
            seen.add(url_key(u))
            return True

        core = core_name(prog.name)
        slugs = slug_variants(core)
        stems = self._stems(prog.name)
        pool: list[Link] = []
        if page is not None and page.ok:
            best = (self._best_faculty_link(self._scoped_links(ctx, page, main_only=True), page.final_url, slugs,
                                            stems)
                    or self._best_faculty_link(self._scoped_links(ctx, page), page.final_url, slugs, stems))
            if fresh(best):
                yield best
            links = self._scoped_links(ctx, page)
            pool += links
            ranked = sorted(((self._dept_score(l, core, page.final_url), l) for l in links), key=lambda x: -x[0])
            for score, link in ranked[:2]:
                if score < float(mc["dept_link_threshold"]):
                    break
                dept = self._fetch(ctx, link.url)
                if dept is None or not dept.ok:
                    continue
                dept_links = self._scoped_links(ctx, dept)
                best = self._best_faculty_link(dept_links, dept.final_url, slugs, stems)
                if fresh(best):
                    yield best
                pool += dept_links

        # Google before the sitemap and the AI: one search per department name (cheap with the Serper API)
        dept_key = norm(core or prog.name)
        if dept_key and dept_key not in ctx.google_done and ctx.google_budget > 0:
            ctx.google_done.add(dept_key)
            ctx.google_budget -= 1
            for u in self._faculty_from_google(ctx, core or prog.name, slugs, stems):
                if fresh(u):
                    yield u

        # the campus directory: first its department pages (directory.x.edu/nursing-department.html), then the
        # full list, keeping only the people whose title names this major
        for d_url in self._directory_pages(ctx):
            dpage = self._fetch(ctx, d_url)
            if dpage is None or not dpage.ok:
                continue
            dept_pages = [l.url for l in self._scoped_links(ctx, dpage)
                          if not card_name(l.text)[0] and self._page_ok(l.url)
                          and (any(sl in urlparse(l.url).path.lower() for sl in slugs) or self._mentions(l.text, stems))]
            for u in dept_pages[:2]:
                if fresh(u):
                    yield u
            ctx.forced_wide.add(url_key(d_url))
            if fresh(d_url):
                yield d_url

        best = self._faculty_from_sitemap(ctx, slugs)
        if fresh(best):
            yield best

        if self.llm.enabled and pool:
            unique = list({url_key(l.url): l for l in pool if l.text and self._page_ok(l.url)}.values())
            pick = self.llm.pick_faculty_link(ctx.inst.unitid, ctx.inst.name, prog.name,
                                              unique[: int(mc["llm_max_links"])])
            if pick and ctx.scope.contains(pick) and self._page_ok(pick) and fresh(pick):
                yield pick

    def _faculty_from_sitemap(self, ctx: Ctx, slugs: list[str]) -> str:
        if not slugs:
            return ""
        if ctx.sitemap is None:
            m2 = self.cfg["m2"]
            ctx.sitemap = sitemap_urls(self.fetcher, ctx.website, int(m2["sitemap_max_files"]),
                                       int(m2["sitemap_max_urls"]))
        threshold = float(self.mc["faculty_link_threshold"])
        best, best_score = "", threshold - 0.001
        for u in ctx.sitemap:
            path = urlparse(u).path.lower()
            if not any(s in path for s in slugs) or not ctx.scope.contains(u) or not self._page_ok(u):
                continue
            s = link_score("", u, self.fac_weights, url_factor=1.0) - path.strip("/").count("/")
            if s > best_score:
                best, best_score = u, s
        return best

    def _faculty_from_google(self, ctx: Ctx, program: str, slugs: list[str], stems: set[str]) -> list[str]:
        query = self.mc["google_query"].format(site=ctx.site, domain=ctx.site, program=program)
        results = self.search.search(query)

        def strong(r) -> bool:  # title or URL names this department (not just a mention in the snippet)
            path = urlparse(r.url).path.lower()
            return any(sl in path for sl in slugs) or self._mentions(f"{r.title} {path_words(r.url)}", stems)

        hits = [r for r in results.organic[:6] if ctx.scope.contains(r.url) and not has_skip_extension(r.url)
                and self._page_ok(r.url)]
        named = [r for r in hits if strong(r)]
        weak = [r for r in hits if not strong(r) and self._mentions(r.snippet, stems)]
        threshold = float(self.mc["faculty_link_threshold"])
        for r in named:
            if link_score(r.title, r.url, self.fac_weights) >= threshold:
                return [self.search.resolve(r)]
        # open the best results and look for this department's faculty link; only a page that names the
        # department is used itself (a page that merely mentions it, e.g. Physical Therapy for Neuroscience, is not)
        for r in (named + weak)[:2]:
            url = self.search.resolve(r)
            page = self._fetch(ctx, url)
            if page is None or not page.ok:
                continue
            best = self._best_faculty_link(self._scoped_links(ctx, page), page.final_url, slugs, stems)
            if best:
                return [best]
            if r in named:
                return [url]
        return []

    def _office_fallback(self, ctx: Ctx) -> bool:
        """Undergraduate research / advising / career offices (the client accepts these emails too)."""
        mc = self.mc
        threshold = float(mc["office_link_threshold"])
        cands: dict[str, tuple[float, str]] = {}
        home = self._fetch(ctx, ctx.website)
        if home is not None and home.ok:
            for link in self._scoped_links(ctx, home):
                s = link_score(link.text, link.url, self.office_weights)
                if s >= threshold:
                    cands[url_key(link.url)] = max(cands.get(url_key(link.url), (0.0, "")), (s, link.url))
        if ctx.sitemap is None:
            m2 = self.cfg["m2"]
            ctx.sitemap = sitemap_urls(self.fetcher, ctx.website, int(m2["sitemap_max_files"]),
                                       int(m2["sitemap_max_urls"]))
        for u in ctx.sitemap:
            if ctx.scope.contains(u):
                s = link_score(path_words(u), u, self.office_weights, url_factor=0.0)
                if s >= threshold:
                    cands[url_key(u)] = max(cands.get(url_key(u), (0.0, "")), (s, u))
        added = False
        for _, url in sorted(cands.values(), key=lambda x: -x[0])[: int(mc["office_max_pages"])]:
            page = self._fetch(ctx, url)
            if page is not None and page.ok and self._harvest(ctx, page, OFFICE_PROGRAM):
                added = True
        if added:
            ctx.notes.append("checked undergraduate research / advising / career office pages")
        return added

    # ------------------------------------------------ 3d/3e: verify + select
    def _finalize_contacts(self, ctx: Ctx) -> None:
        """Drop generic mailboxes and other-domain emails, then let the AI name unclear contacts."""
        counts = Counter(registered_domain(e.split("@", 1)[1]) for e in ctx.contacts)
        allowed = set(ctx.scope.domains)
        for domain, n in counts.items():
            if domain not in allowed and domain.endswith(".edu") and n >= int(self.mc["min_emails_for_extra_domain"]):
                allowed.add(domain)
                ctx.notes.append(f"emails use domain {domain}")
        kept, other = {}, 0
        for email, c in ctx.contacts.items():
            if self.email_filter.is_generic(email):
                continue
            if registered_domain(email.split("@", 1)[1]) not in allowed:
                other += 1
                continue
            kept[email] = c
        if other:
            ctx.notes.append(f"{other} emails from other domains ignored")
        ctx.contacts = kept
        self._enrich(ctx)

    def _enrich(self, ctx: Ctx) -> None:
        if not self.llm.enabled:
            return
        mc = self.mc
        todo = [c for c in ctx.contacts.values()
                if not c.enriched and c.snippet and (not c.name or c.role == "unknown")]
        todo = todo[: int(mc["max_llm_contacts"])]
        size = max(1, int(mc["llm_contacts_batch"]))
        for i in range(0, len(todo), size):
            batch = todo[i:i + size]
            answers = self.llm.extract_contacts(ctx.inst.unitid, [(c.email, c.snippet) for c in batch]) or {}
            for c in batch:
                c.enriched = True
                a = answers.get(c.email)
                if not a:
                    continue
                snippet = norm(c.snippet)
                name, title = collapse(a.get("name", "")), collapse(a.get("title", ""))
                if name and not c.name and norm(name) and norm(name) in snippet:
                    c.name = name
                if title and not c.title and norm(title) and norm(title) in snippet:
                    c.title = title
                role = a.get("role", "unknown")
                if c.role == "unknown" and role != "unknown":
                    c.role = role
                    c.method = "ai"

    def _select(self, ctx: Ctx) -> list[str]:
        """Client rule: chairs / directors / deans / coordinators first; otherwise 2-3 professors per program."""
        mc = self.mc
        by_program: dict[str, list[Contact]] = defaultdict(list)
        for c in ctx.contacts.values():
            for p in sorted(c.programs):
                by_program[p].append(c)
        picked: list[Contact] = []
        for prog, people in by_program.items():
            if self.exclude_title_re is not None:
                # title, or the name of an office mailbox ("Financial Aid", "Tech Support")
                people = [c for c in people if not self.exclude_title_re.search(f"{c.title or ''} | {c.name or ''}")]
            if prog != OFFICE_PROGRAM:
                people = [c for c in people if self._fits_program(c, prog)]
            leaders = [c for c in people if c.role in LEADERSHIP]
            offices = [c for c in people if c.role == "office"]
            if prog == OFFICE_PROGRAM:
                picked += leaders + offices
                continue
            if leaders:
                picked += leaders
            else:
                # fallback people must clearly belong to this department (title or department page)
                strict = [c for c in people if prog == OFFICE_PROGRAM or self._fits_program(c, prog, strict=True)]
                professors = [c for c in strict if c.role == "professor"]
                # not admin staff; a nameless email is fine when it comes from the department's own faculty page
                # (strict fit already requires the title or the page to name the department)
                others = [c for c in strict if c.role == "unknown"]
                picked += (professors or others)[: int(mc["fallback_professors_per_program"])]
            picked += offices[: int(mc["max_office_emails_per_program"])]
        ordered = sorted(picked, key=lambda c: self.roles.priority(c.role))
        emails = list(dict.fromkeys(c.email for c in ordered))
        cap = int(mc.get("max_emails_per_institution", 0) or 0)
        return emails[:cap] if cap else emails


# ---------------------------------------------------------------- export
def _client_notes(d1: dict, d2: dict, d3: dict | None) -> str:
    if d2.get("m1_note"):  # e.g. "permanently closed", typed in place of a URL during Module 1 review
        return d2["m1_note"]
    if d3 is not None and d3.get("status") == "skipped":  # e.g. "no undergraduate programs" (Module 2 review)
        return d3.get("note", "")
    # Module 1/2 confidence is internal (reviewed by hand before Module 3): it stays in the Review sheet's
    # m1_confidence / m2_confidence columns, never in the client's NOTES column.
    notes = []
    if d3 is not None:
        if d3.get("status") == "error":
            notes.append("automatic extraction failed")
        elif not d3.get("bacc_degrees"):
            notes.append("no qualifying majors found")
        if d3 is not None and d3.get("status") != "error" and not d3.get("emails"):
            notes.append("no emails found (contact forms only?)")
        if "blocks automated access" in (d3.get("notes") or ""):
            notes.append("some department pages block automated access (CAPTCHA)" if d3.get("emails")
                         else "website blocks automated access (CAPTCHA)")
    return "; ".join(notes)


def export(cfg: dict, db: DB, all_insts: list[Institution], out_path, live: bool = False) -> bool:
    """Write output/03_final.xlsx from the database. live=True (after each institution during a run): no
    timestamped copy if the file is open in Excel, and no summary log. Returns False if the file was locked."""
    paths = cfg["paths"]
    m1, m2, m3 = db.all_results("m1"), db.all_results("m2"), db.all_results(MODULE)
    m1_over, m2_over = db.get_overrides("m1"), db.get_overrides("m2")
    m2_path = resolve_path(paths["m2_output"])
    if m2_path.exists():
        sync_client_notes(db, m2_path)  # notes typed in 02_programs.xlsx since the last run
    client_notes = db.get_overrides(NOTE_MODULE)
    fills, program_rows, contact_rows, review_rows, page_rows = {}, [], [], [], []
    for inst in all_insts:
        uid = inst.unitid
        d1, d2, d3 = m1.get(uid, {}), m2.get(uid, {}), m3.get(uid)
        if not (d1 or d2 or d3):
            continue
        website = m1_over.get(uid) or d1.get("official_url", "")
        if not looks_like_url(website):  # a review note, not an address
            website = ""
        m2_fix = m2_over.get(uid, "")
        if m2_fix and not looks_like_url(m2_fix):
            programs_url = ""
        else:
            programs_url = m2_fix or d2.get("programs_url", "") or website
        base = {"unitid": uid, "institution": inst.name, "state": inst.state}
        fill = {"website": programs_url, "notes": _client_notes(d1, d2, d3)}
        if d3 is not None and d3.get("status") != "error":
            fill["bacc"] = clean_degree_list(d3.get("bacc_degrees", ""), cfg)
            fill["emails"] = d3.get("emails", "")
        reviewed = client_notes.get(uid, "").strip()
        if reviewed:  # a reviewer's note (02_programs.xlsx, client_note) replaces the automatic note
            fill["notes"] = reviewed
            if NO_MAJORS_NOTE_RE.search(reviewed):
                fill["bacc"], fill["emails"] = "", ""  # the reviewer says there are no qualifying majors
        fills[uid] = fill

        if d3 is None or d3.get("status") == "skipped" or d2.get("m1_note"):
            continue  # skipped on purpose during review: nothing to list or re-check
        for p in d3.get("programs", []):
            program_rows.append({**base, "program": display_program_name(p["name"]), "decision": "included",
                                 "method": p.get("method", ""),
                                 "url": p.get("url", "")})
        for p in d3.get("rejected_programs", []):
            program_rows.append({**base, "program": p["name"], "decision": "excluded", "url": p.get("url", ""),
                                 "reason": p.get("reason", "")})
        for f in d3.get("faculty_pages", []):  # which page each major's contacts came from, and how
            page_rows.append({**base, "program": f.get("program", ""), "faculty_page_url": f.get("url", ""),
                              "emails_found": f.get("emails", 0), "method": f.get("method", "")})
        rows = []
        for c in d3.get("contacts", []):
            if not c.get("programs"):
                continue  # only seen on a campus-wide page, not linked to any major
            rows.append({**base, "department": _dept_label(c["programs"]), "name": clean_person_name(c.get("name", "")),
                         "title": c.get("title", ""), "role": c.get("role", ""), "email": c.get("email", ""),
                         "selected": bool(c.get("selected")), "programs": "; ".join(c["programs"]),
                         "source_url": c.get("source_url", ""), "method": c.get("method", "")})
        rows.sort(key=lambda r: (r["department"].lower(), not r["selected"], ROLE_ORDER.get(r["role"], 9)))
        contact_rows += rows
        issues = [n for n in fill["notes"].split("; ") if n]
        if issues and not reviewed:  # only real problems (no majors / no emails / CAPTCHA / failed); technical notes stay in "notes"
            review_rows.append({**base, "issues": "; ".join(issues), "website_url": website,
                                "programs_url": programs_url, "m1_confidence": d1.get("confidence_level", ""),
                                "m2_confidence": d2.get("confidence_level", ""),
                                "degrees_found": d3.get("counts", {}).get("degrees", 0),
                                "emails_found": d3.get("counts", {}).get("selected", 0),
                                "notes": d3.get("notes", "")})

    path = write_final(resolve_path(paths["input_file"]), paths.get("input_sheet"), out_path, fills, [
        ("Programs", PROGRAM_COLUMNS, program_rows, None),
        ("Contacts", CONTACT_COLUMNS, contact_rows, None),
        ("Faculty pages", FACULTY_PAGE_COLUMNS, page_rows, None),
        ("Review", REVIEW_COLUMNS, review_rows, None),
    ], fallback=not live)
    if path is None:
        return False
    if live:
        return True
    done = [m3[u] for u in m3 if m3[u].get("status") != "error"]
    log.info("Saved %s: %d institutions processed, %d with majors, %d with emails, %d rows to review.",
             path, len(done), sum(1 for d in done if d.get("bacc_degrees")),
             sum(1 for d in done if d.get("emails")), len(review_rows))
    return True


def run(cfg: dict, args) -> None:
    paths = cfg["paths"]
    db = DB(resolve_path(paths["db_file"]))
    m2_path = resolve_path(paths["m2_output"])
    out_path = resolve_path(paths["m3_output"])
    all_insts = load_institutions(resolve_path(paths["input_file"]), paths.get("input_sheet"))

    if not args.export_only:
        if not m2_path.exists():
            log.error("Module 2 output not found (%s). Run: python run.py m2", m2_path)
            return
        sync_m2_overrides(db, m2_path)
        m2_rows = {r["unitid"]: r for r in read_table(m2_path, "Programs") if r.get("unitid")}
        m2_overrides = db.get_overrides("m2")

        insts = filter_institutions(all_insts, args.state, args.unitid)
        todo = []
        for inst in insts:
            row = m2_rows.get(inst.unitid)
            if row is None:
                continue
            override = m2_overrides.get(inst.unitid, "").strip()
            if override and not looks_like_url(override):
                # a note typed during Module 2 review (e.g. "no undergraduate programs"): skip, keep the note
                key = f"note:{override}"
                prev = db.get_result(MODULE, inst.unitid)
                if args.force or not prev or prev["input_key"] != key:
                    db.put_result(MODULE, inst.unitid, key, {"status": "skipped", "note": override,
                                                             "notes": f"Module 2 review: {override}"})
                continue
            website = row.get("website_url", "").strip()
            programs_url = override or row.get("programs_url", "").strip() or website
            page_type = "" if override else row.get("page_type", "")
            key = f"{website}|{programs_url}"
            prev = db.get_result(MODULE, inst.unitid)
            if getattr(args, "retry_blocked", False):
                # only institutions whose website showed a bot check (CAPTCHA) that could not be passed last time
                if not prev or "blocks automated access" not in (prev["data"].get("notes") or ""):
                    continue
            elif prev and not args.force and prev["input_key"] == key and prev["data"].get("status") != "error":
                continue
            todo.append((inst, website, programs_url, page_type, key))
        if args.limit:
            todo = todo[: args.limit]
        log.info("%d institutions selected, %d to process", len(insts), len(todo))

        if todo:
            use_cache = not args.no_cache
            browser_provider = None if args.no_browser else (lambda: get_browser(cfg))
            fetcher = Fetcher(cfg, db, browser_provider, use_cache=use_cache)
            llm = LLM(cfg, db, MODULE)
            search = Search(cfg, db, browser_provider, use_cache)
            extractor = FacultyExtractor(cfg, db, fetcher, llm, search, browser_provider)

            def work(item):
                inst, website, programs_url, page_type, _ = item
                return extractor.process(inst, website, programs_url, page_type)

            locked_warned = False

            def save(item, result):
                nonlocal locked_warned
                inst, key = item[0], item[4]
                db.put_result(MODULE, inst.unitid, key, result)
                counts = result.get("counts", {})
                log.info("%s: %d majors, %d emails selected (%d contacts found)", inst.name,
                         counts.get("degrees", 0), counts.get("selected", 0), counts.get("contacts", 0))
                # keep the spreadsheet current after every institution, so a stopped run loses nothing
                try:
                    if export(cfg, db, all_insts, out_path, live=True):
                        locked_warned = False
                    elif not locked_warned:
                        locked_warned = True
                        log.warning("%s is open in Excel - live updates paused until you close it "
                                    "(results are safe in the database).", out_path.name)
                except Exception:
                    log.exception("Could not update %s (results are safe in the database)", out_path.name)

            try:
                run_parallel(todo, work, args.workers or int(cfg["m3"]["workers"]), "M3 faculty", save,
                             label=lambda it: it[0].name)
            except KeyboardInterrupt:
                log.warning("Stopped by user - saving what is done so far.")
            finally:
                search.close()
                llm.close()
                close_browser()

    export(cfg, db, all_insts, out_path)
