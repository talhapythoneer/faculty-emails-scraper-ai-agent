"""Module 2: find each institution's undergraduate programs / majors page (no AI), with a confidence score.

Order: Google (Serper API) for the programs page -> Google for the academics page (fallback, also a hub to hop
from) -> crawl the website (homepage menus, catalog, sitemap) only for rows Google could not settle.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse
from dataclasses import dataclass, field

from .common.browser import close_browser, get_browser
from .common.config import resolve_path
from .common.db import DB
from .common.excel import (Institution, filter_institutions, load_institutions, new_workbook, read_table,
                           save_workbook, sync_overrides, write_sheet)
from .common.fetch import Fetcher, Page, sitemap_urls
from .common.google import Search
from .common.html_utils import extract_links, looks_dynamic, visible_text
from .common.majors import DEGREE_MARKER_RE, MajorMatcher
from .common.runner import run_parallel
from .m1_websites import sync_m1_overrides
from .common.scoring import confidence_level, link_score, tier_bonus
from .common.urls import (SiteScope, ensure_scheme, has_skip_extension, host_of, looks_like_url, path_words,
                          registered_domain, site_key, subdomain, url_key)

log = logging.getLogger("m2")
MODULE = "m2"
COLUMNS = ["unitid", "institution", "state", "website_url", "domain", "programs_url", "confidence_score",
           "confidence_level", "method", "page_type", "degree_marker_count", "target_link_count", "rendered",
           "academics_url", "steps", "top3_candidates", "programs_url_override", "notes"]
OVERRIDE_SHEETS = ("Programs", "Review")
CATALOG_HOST_PREFIXES = ("catalog.", "catalogs.", "catalogue.", "bulletin.", "bulletins.")
CATALOG_MARKERS = ("acalog", "courseleaf", "smartcatalog", "coursedog")
DEPT_PATH_RE = re.compile(r"/(school|college|department|dept|division)[-_]of[-_]|[-_]department/")


@dataclass
class Cand:
    url: str
    text: str = ""
    link_score: float = 0.0
    methods: set = field(default_factory=set)
    checked: bool = False
    final: float = -999.0
    markers: int = 0
    targets: int = 0
    rendered: bool = False
    page_type: str = ""
    google_rank: int = 0   # best Google rank (0 = not from Google)


def detect_page_type(page: Page, rendered: bool = False) -> str:
    host = host_of(page.final_url)
    low = page.html[:300000].lower()
    if host.startswith(CATALOG_HOST_PREFIXES) or any(m in low for m in CATALOG_MARKERS):
        return "catalog"
    letters = sum(1 for l in extract_links(page.soup(), page.final_url) if re.fullmatch(r"[A-Za-z]", l.text or ""))
    if letters >= 15:
        return "az_tabs"
    if rendered or re.search(r">\s*(load|show|view|see)\s+more\b", low):
        return "js_finder"
    return "static_list"


class ProgramsFinder:
    """Google first (programs page, then academics page as fallback); crawl the site only when Google is not sure."""

    def __init__(self, cfg: dict, db: DB, fetcher: Fetcher, search: Search, browser_provider,
                 google_only: bool = False):
        self.cfg = cfg
        self.mc = cfg["m2"]
        self.db = db
        self.fetcher = fetcher
        self.search = search
        self.browser_provider = browser_provider
        self.google_only = google_only
        self.matcher = MajorMatcher(cfg)
        self.weights = self.mc["link_keywords"]
        self.academics_weights = self.mc["academics_keywords"]
        self.url_factor = float(self.mc.get("url_factor", 0.5))
        self.generic = set(cfg["m1"]["generic_subdomains"])

    def _score(self, text: str, url: str) -> float:
        return link_score(text, url, self.weights, self.url_factor)

    def _url_adjust(self, url: str) -> float:
        """Online-only sub-sites (onlinedegrees.x.edu) and one department's pages (/school-of-math-cs/) are rarely
        the institution's full list of majors."""
        pts = 0.0
        sub = subdomain(url)
        for word, value in (self.mc.get("host_penalties") or {}).items():
            if word in sub:
                pts += float(value)
        if DEPT_PATH_RE.search(urlparse(url).path.lower()):
            pts += float(self.mc.get("dept_path_penalty", 0))
        return pts

    def _high(self, best: Cand | None) -> bool:
        return best is not None and best.final >= float(self.mc["high_threshold"])

    def find(self, inst: Institution, website: str) -> dict:
        mc = self.mc
        notes: list[str] = []
        if not website:
            return {"status": "no_website", "website_url": "", "programs_url": "", "confidence_score": 0,
                    "confidence_level": "Low", "notes": "No website from Module 1"}
        website = ensure_scheme(website)
        reg = registered_domain(website)
        site = site_key(website, self.generic)  # campus host (d.umn.edu) or the whole domain (x.edu)
        scope = SiteScope({reg}, mc.get("catalog_vendor_domains", []))
        cands: dict[str, Cand] = {}
        steps: list[str] = []

        def add(url: str, text: str, score: float, method: str) -> Cand | None:
            if not scope.contains(url) or has_skip_extension(url):
                return None
            key = url_key(url)
            c = cands.get(key)
            if c is None:
                c = cands[key] = Cand(url=url, text=text, link_score=score)
            elif score > c.link_score:
                c.link_score, c.text = score, text or c.text
            c.methods.add(method)
            return c

        # 1) Google: the programs page (a second query variant only if the first is not convincing)
        best = None
        if self.search.available:
            for i, template in enumerate(mc["google_queries"]):
                if i and self._high(best):
                    break
                query = template.format(site=site, domain=site, name=inst.name)
                self._validate(self._google_cands(query, add, "google" if i == 0 else f"google{i + 1}"))
                steps.append(f"google{i + 1 if i else ''}")
                best = self._best(cands)
        else:
            notes.append("Google search not available")

        # 2) academics page (fallback): may list majors itself, and its links are candidates too
        academics_url = ""
        if not self._high(best) and self.search.available:
            academics_url = self._academics(site, inst, add)
            steps.append("academics")
            best = self._best(cands)

        # 3) crawl the website (the original method) only when Google was not convincing
        crawled = False
        if not self._high(best) and not self.google_only:
            self._crawl(website, reg, scope, cands, add, notes)
            steps.append("crawl")
            crawled = True
            best = self._best(cands)

        checked = sorted((c for c in cands.values() if c.checked and c.final > -999), key=lambda c: -c.final)
        top3 = "; ".join(f"{c.url} ({c.final:.0f})" for c in checked[:3])
        base = {"website_url": website, "domain": reg, "academics_url": academics_url, "top3_candidates": top3,
                "steps": ", ".join(steps), "google_only": self.google_only and not crawled}
        if best is None or (best.markers == 0 and best.targets == 0):
            fallback = academics_url or website
            notes.append("no programs page found - using the " + ("academics page" if academics_url else "homepage"))
            return {**base, "status": "not_found", "programs_url": fallback, "confidence_score": 0,
                    "confidence_level": "Low",
                    "method": "academics_fallback" if academics_url else "homepage_fallback",
                    "page_type": "", "notes": "; ".join(notes)}

        return {**base, "status": "ok", "programs_url": best.url, "confidence_score": round(best.final),
                "confidence_level": confidence_level(best.final, mc["high_threshold"], mc["medium_threshold"]),
                "method": ", ".join(sorted(best.methods)), "page_type": best.page_type,
                "degree_marker_count": best.markers, "target_link_count": best.targets,
                "rendered": "Y" if best.rendered else "", "notes": "; ".join(notes)}

    def _google_cands(self, query: str, add, method: str) -> list[Cand]:
        """Top in-scope results, scored by title/URL keywords plus a bonus for Google's rank."""
        mc = self.mc
        results = self.search.search(query)
        rank_bonus = [float(b) for b in mc.get("google_rank_bonus", [])]
        out: list[Cand] = []
        for r in results.organic[: int(mc["google_top_results"])]:
            url = self.search.resolve(r)
            bonus = rank_bonus[r.rank - 1] if 0 < r.rank <= len(rank_bonus) else 0.0
            s = self._score(r.title, url)
            if s >= 0:  # the floor is for titles without keywords; news/blog/graduate penalties stay
                s = max(s, float(mc["google_min_score"]))
            c = add(url, r.title, s + bonus, method)
            if c is not None and (not c.google_rank or r.rank < c.google_rank):
                c.google_rank = r.rank
            if c is not None and c not in out:
                out.append(c)
        return out[: int(mc["google_validate_top_n"])]

    def _academics(self, site: str, inst: Institution, add) -> str:
        mc = self.mc
        results = self.search.search(mc["academics_query"].format(site=site, domain=site, name=inst.name))
        best_url, best_score = "", 0.0
        for r in results.organic[: int(mc["google_top_results"])]:
            s = link_score(r.title, r.url, self.academics_weights, self.url_factor) - 3 * (r.rank - 1)
            if s > best_score:
                best_url, best_score = self.search.resolve(r), s
        if not best_url:
            return ""
        page = self.fetcher.get(best_url)
        if page is None or not page.ok or not page.is_html:
            return ""
        hub = add(page.final_url, "academics", max(self._score("academics", page.final_url), 0.0), "academics")
        hop = []
        for link in extract_links(page.soup(), page.final_url):
            s = self._score(link.text, link.url)
            if s > 0:
                c = add(link.url, link.text, s + float(mc["second_hop_penalty"]), "academics")
                if c is not None and c not in hop:
                    hop.append(c)
        hop.sort(key=lambda c: -c.link_score)
        self._validate(([hub] if hub is not None else []) + hop[: int(mc["second_hop_top_n"])])
        return page.final_url

    def _crawl(self, website: str, reg: str, scope: SiteScope, cands: dict, add, notes: list[str]) -> None:
        mc = self.mc

        # a) homepage navigation
        home = self.fetcher.get(website)
        if home is not None and home.ok:
            final_reg = registered_domain(home.final_url)
            if final_reg and final_reg != reg:
                scope.add(final_reg)
                notes.append(f"homepage redirects to {host_of(home.final_url)}")
            for link in extract_links(home.soup(), home.final_url):
                s = self._score(link.text, link.url)
                if s > 0:
                    add(link.url, link.text, s, "homepage")
        else:
            notes.append("homepage could not be loaded")

        # b) course catalog sub-site (Acalog / CourseLeaf...)
        if not any(host_of(c.url).startswith(CATALOG_HOST_PREFIXES) for c in cands.values()):
            for sub in mc.get("catalog_subdomains", []):
                page = self.fetcher.get(f"https://{sub}.{reg}/", allow_browser=False)
                if page is not None and page.ok and page.is_html:
                    add(page.final_url, f"{sub} (catalog)", float(mc["catalog_base_score"]), "catalog")
                    for link in extract_links(page.soup(), page.final_url):
                        s = self._score(link.text, link.url)
                        if s > 0:
                            add(link.url, link.text, s + float(mc["catalog_link_bonus"]), "catalog")
                    break

        # c) sitemap
        if mc.get("use_sitemap", True):
            scored = []
            for u in sitemap_urls(self.fetcher, website, int(mc["sitemap_max_files"]), int(mc["sitemap_max_urls"])):
                if scope.contains(u) and not has_skip_extension(u):
                    s = self._score(path_words(u), u)
                    if s > 0:
                        scored.append((s, u))
            scored.sort(key=lambda x: (-x[0], len(x[1])))
            for s, u in scored[: int(mc["sitemap_top_n"])]:
                add(u, path_words(u), s, "sitemap")

        # d) second hop from the best homepage / catalog links
        hop = sorted((c for c in cands.values() if c.methods & {"homepage", "catalog"}),
                     key=lambda c: -c.link_score)[: int(mc["second_hop_top_n"])]
        for c in hop:
            page = self.fetcher.get(c.url)
            if page is None or not page.ok or not page.is_html:
                continue
            for link in extract_links(page.soup(), page.final_url):
                s = self._score(link.text, link.url)
                if s > 0:
                    add(link.url, link.text, s + float(mc["second_hop_penalty"]), "second_hop")

        # e) open the best unchecked candidates and check they really list degrees
        self._validate(sorted((c for c in cands.values() if not c.checked), key=lambda c: -c.link_score)
                       [: int(mc["validate_top_n"])])

    def _stats(self, page: Page) -> tuple[int, int]:
        soup = page.soup()
        markers = len(DEGREE_MARKER_RE.findall(visible_text(soup)))
        targets = set()
        for link in extract_links(soup, page.final_url):
            kw = self.matcher.target(link.text)
            if kw and not self.matcher.excluded(link.text):
                targets.add(kw)
        return markers, len(targets)

    def _validate(self, cands: list[Cand]) -> None:
        mc = self.mc
        for c in cands:
            if c.checked:
                continue
            c.checked = True
            page = self.fetcher.get(c.url)
            if page is None or not page.ok or not page.is_html:
                continue
            c.url = page.final_url
            markers, targets = self._stats(page)
            rendered = False
            # Google's top results are worth a Chrome render even when the HTML does not look like a JS app
            trusted = 0 < c.google_rank <= int(mc.get("google_render_top_n", 2))
            if (self.browser_provider is not None and markers < int(mc["render_if_markers_below"])
                    and (trusted or looks_dynamic(page.html))):
                rp = self.fetcher.get(c.url, render=True, expand=True)
                if rp is not None and rp.ok:
                    m2, t2 = self._stats(rp)
                    if m2 + t2 > markers + targets:
                        markers, targets, page, rendered = m2, t2, rp, True
            c.markers, c.targets, c.rendered = markers, targets, rendered
            c.page_type = detect_page_type(page, rendered)
            c.final = (c.link_score + tier_bonus(markers, mc["marker_bonus"]) + tier_bonus(targets, mc["target_bonus"])
                       + (float(mc["agreement_bonus"]) if len(c.methods) >= 2 else 0.0) + self._url_adjust(c.url))

    def _best(self, cands: dict[str, Cand]) -> Cand | None:
        valid = [c for c in cands.values() if c.checked and c.final > -999]
        if not valid:
            return None
        best = max(valid, key=lambda c: (c.final, c.targets, -len(c.url)))
        if best.page_type == "catalog":
            # Column D is read by the client: prefer the website's own majors page when it is nearly as good.
            margin = float(self.mc.get("catalog_tie_margin", 0))
            alt = [c for c in valid if c.page_type != "catalog" and c.final >= best.final - margin
                   and c.targets >= best.targets - 1]
            if alt:
                best = max(alt, key=lambda c: (c.final, c.targets, -len(c.url)))
        return best


def sync_m2_overrides(db: DB, path) -> None:
    """programs_url_override, or a hand-edited programs_url (differs from what Module 2 found), counts as the fix.

    Call it only while the workbook still matches the database (before processing), never right after it.
    """
    baseline = {uid: d.get("programs_url", "") for uid, d in db.all_results(MODULE).items()}
    sync_overrides(db, MODULE, path, "programs_url_override", OVERRIDE_SHEETS, edited_column="programs_url",
                   baseline=baseline)


def export(cfg: dict, db: DB, all_insts: list[Institution], out_path) -> None:
    # overrides were synced at the start of run(); edits typed into programs_url come back in programs_url_override
    results = db.all_results(MODULE)
    overrides = db.get_overrides(MODULE)
    rows = []
    for inst in all_insts:
        d = results.get(inst.unitid)
        if d is None:
            continue
        row = {c: d.get(c, "") for c in COLUMNS}
        row.update(unitid=inst.unitid, institution=inst.name, state=inst.state,
                   programs_url_override=overrides.get(inst.unitid, ""))
        if d.get("status") == "error":
            row["confidence_level"] = "Low"
        rows.append(row)
    review = [r for r in rows if r.get("confidence_level") != "High"]
    wb = new_workbook()
    write_sheet(wb, "Programs", COLUMNS, rows, level_col="confidence_level", override_col="programs_url_override")
    write_sheet(wb, "Review", COLUMNS, review, level_col="confidence_level", override_col="programs_url_override")
    path = save_workbook(wb, out_path)
    counts = {lvl: sum(1 for r in rows if r.get("confidence_level") == lvl) for lvl in ("High", "Medium", "Low")}
    log.info("Saved %s: %d rows (High %d, Medium %d, Low %d). Fix Medium/Low rows in 'programs_url_override'.",
             path, len(rows), counts["High"], counts["Medium"], counts["Low"])


def run(cfg: dict, args) -> None:
    paths = cfg["paths"]
    db = DB(resolve_path(paths["db_file"]))
    m1_path = resolve_path(paths["m1_output"])
    out_path = resolve_path(paths["m2_output"])
    all_insts = load_institutions(resolve_path(paths["input_file"]), paths.get("input_sheet"))
    sync_m2_overrides(db, out_path)

    if not args.export_only:
        if not m1_path.exists():
            log.error("Module 1 output not found (%s). Run: python run.py m1", m1_path)
            return
        sync_m1_overrides(db, m1_path)
        m1_rows = {r["unitid"]: r for r in read_table(m1_path, "Websites") if r.get("unitid")}
        m1_overrides = db.get_overrides("m1")

        insts = filter_institutions(all_insts, args.state, args.unitid)
        todo = []
        for inst in insts:
            row = m1_rows.get(inst.unitid)
            if row is None:
                continue
            website = (m1_overrides.get(inst.unitid) or row.get("official_url", "")).strip()
            m1_note = ""
            if website and not looks_like_url(website):  # e.g. "permanently closed" typed during review
                m1_note, website = website, ""
            key = website or f"note:{m1_note}"
            prev = db.get_result(MODULE, inst.unitid)
            if (prev and not args.force and prev["input_key"] == key
                    and prev["data"].get("status") != "error"):
                d = prev["data"]
                # a --google-only row that was not High is crawled on the next normal run
                if args.google_only or not d.get("google_only") or d.get("confidence_level") == "High":
                    continue
            todo.append((inst, website, m1_note, key))
        if args.limit:
            todo = todo[: args.limit]
        log.info("%d institutions selected, %d to process", len(insts), len(todo))

        if todo:
            use_cache = not args.no_cache
            browser_provider = None if args.no_browser else (lambda: get_browser(cfg))
            fetcher = Fetcher(cfg, db, browser_provider, use_cache=use_cache)
            search = Search(cfg, db, browser_provider, use_cache)
            if not search.is_api:
                log.warning("No SERPER_API_KEYS: Google searches go through Chrome (slow). Add the key to .env.")
            finder = ProgramsFinder(cfg, db, fetcher, search, browser_provider, args.google_only)

            def work(item):
                inst, website, m1_note, _ = item
                result = finder.find(inst, website)
                if m1_note:
                    result.update(m1_note=m1_note, notes=f"Module 1 review: {m1_note}")
                return result

            def save(item, result):
                inst, key = item[0], item[3]
                db.put_result(MODULE, inst.unitid, key, result)
                log.info("%s -> %s [%s %s]", inst.name, result.get("programs_url", ""),
                         result.get("confidence_level", ""), result.get("confidence_score", ""))

            try:
                run_parallel(todo, work, args.workers or int(cfg["m2"]["workers"]), "M2 programs", save,
                             label=lambda it: it[0].name)
            except KeyboardInterrupt:
                log.warning("Stopped by user - saving what is done so far.")
            finally:
                search.close()
                close_browser()

    export(cfg, db, all_insts, out_path)
