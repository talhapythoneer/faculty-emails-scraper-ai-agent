"""Module 1: find each institution's official website through Google (Serper API or Chrome), with a confidence score."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from tqdm import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm

from .common.browser import CaptchaTimeout, close_browser, get_browser
from .common.config import resolve_path
from .common.db import DB
from .common.excel import (Institution, filter_institutions, load_institutions, new_workbook, save_workbook,
                           sync_overrides, write_sheet)
from .common.fetch import Fetcher
from .common.google import Search
from .common.official_urls import OfficialUrlLookup
from .common.scoring import confidence_level, domain_matches_name, fuzzy_contains, norm
from .common.urls import ensure_scheme, host_of, is_http, origin, registered_domain, site_key, strip_www

log = logging.getLogger("m1")
MODULE = "m1"
COLUMNS = ["unitid", "institution", "city", "state", "official_url", "domain", "confidence_score",
           "confidence_level", "method", "google_title", "scorecard_url", "top3_candidates", "url_override", "notes"]
OVERRIDE_SHEETS = ("Websites", "Review")


@dataclass
class Candidate:
    key: str
    titles: list = field(default_factory=list)
    ranks: list = field(default_factory=list)
    sources: set = field(default_factory=set)
    score: float = 0.0
    signals: list = field(default_factory=list)
    urls: list = field(default_factory=list)


def campus_qualifier(name: str) -> str:
    """'University of Michigan-Ann Arbor' -> 'Ann Arbor'; 'University of Maine at Farmington' -> 'Farmington'."""
    m = re.search(r"\s*[-–—]\s*([A-Z][\w .'&]+)$", name)
    if m:
        return m.group(1).strip()
    m = re.search(r"\b(?:at|in)\s+([A-Z][\w .'&]+)$", name)
    return m.group(1).strip() if m else ""


def resolve(inst: Institution, cfg: dict, fetcher: Fetcher, lookup: OfficialUrlLookup, search: Search) -> dict:
    mc = cfg["m1"]
    w = mc["weights"]
    generic = set(mc["generic_subdomains"])
    blocked = [b.lower() for b in mc["third_party_domains"]]
    notes: list[str] = []

    query = mc["query_template"].format(name=inst.name, city=inst.city, state=inst.state).strip()
    results = search.search(query)

    cands: dict[str, Candidate] = {}

    def add(url: str, source: str, title: str = "", rank: int | None = None) -> None:
        url = ensure_scheme(url)
        if not is_http(url):
            return
        key = site_key(url, generic)
        if not key:
            return
        c = cands.setdefault(key, Candidate(key=key))
        c.sources.add(source)
        if source == "organic" and not c.ranks:
            c.urls.insert(0, url)  # the top organic URL is the best source for the homepage address
        else:
            c.urls.append(url)
        if title:
            c.titles.append(title)
        if rank:
            c.ranks.append(rank)

    for r in results.organic[: int(mc["top_results"])]:
        add(r.url, "organic", r.title, r.rank)
    # Links outside the titled results (AI Overview sources, cards) often include the official site too.
    link_sites: set[str] = set()
    for r in results.links:
        key = site_key(ensure_scheme(r.url), generic)
        if key and key not in link_sites and len(link_sites) >= int(mc.get("other_links_max", 8)):
            continue
        link_sites.add(key)
        add(r.url, "page_link", r.title)
    if results.kp_website:
        add(results.kp_website, "knowledge_panel")
    scorecard_url = lookup.get(inst.unitid)
    if scorecard_url:
        add(scorecard_url, "scorecard")

    if not cands:
        return {"status": "no_candidates", "official_url": "", "domain": "", "confidence_score": 0,
                "confidence_level": "Low", "method": "", "scorecard_url": scorecard_url,
                "notes": "No Google results"}

    sc_key = site_key(scorecard_url, generic) if scorecard_url else ""
    sc_reg = registered_domain(scorecard_url) if scorecard_url else ""
    qualifier = norm(campus_qualifier(inst.name))
    campus_seen = bool(qualifier) and any(qualifier in norm(" ".join(c.titles + [c.key])) for c in cands.values())
    for c in cands.values():
        reg = registered_domain(c.key)
        if any(reg == b or c.key == b or c.key.endswith("." + b) for b in blocked):
            c.score += w["third_party"]
            c.signals.append("third-party")
        # Multi-campus systems: if some result names the campus, penalise results that don't.
        if campus_seen and qualifier not in norm(" ".join(c.titles + [c.key])):
            c.score += w["campus_mismatch"]
            c.signals.append("campus not in title")
        if "knowledge_panel" in c.sources:
            c.score += w["knowledge_panel"]
            c.signals.append("knowledge panel")
        if reg.endswith(".edu"):
            c.score += w["edu"]
            c.signals.append(".edu")
        if sc_key:
            if c.key == sc_key:
                c.score += w["scorecard"]
                c.signals.append("scorecard")
            elif reg == sc_reg:
                c.score += w["scorecard_partial"]
                c.signals.append("scorecard domain")
        if domain_matches_name(c.key, inst.name):
            c.score += w["domain_name_match"]
            c.signals.append("domain~name")
        if any(fuzzy_contains(inst.name, t, mc["title_match_threshold"]) for t in c.titles):
            c.score += w["title_match"]
            c.signals.append("title~name")
        if c.ranks and min(c.ranks) == 1:
            c.score += w["rank1"]
            c.signals.append("rank 1")

    order = sorted(cands.values(), key=lambda c: (-c.score, min(c.ranks) if c.ranks else 99))
    best = order[0]
    level = confidence_level(best.score, mc["high_threshold"], mc["medium_threshold"])
    if (len(order) > 1 and level == "High" and order[1].score >= best.score - mc["ambiguity_margin"]
            and registered_domain(order[1].key) != registered_domain(best.key)):
        level = "Medium"
        notes.append(f"close runner-up: {order[1].key}")

    # Homepage = scheme + host of a Google URL on this exact site (keeps "www." when Google shows it).
    official_url = next((origin(u) for u in best.urls if strip_www(host_of(u)) == best.key), f"https://{best.key}/")
    official_url = re.sub(r"^http://", "https://", official_url)
    if "third-party" in best.signals:
        official_url, level = "", "Low"
        notes.append("only third-party sites found")

    if "knowledge_panel" in best.sources:
        method = "knowledge_panel"
    elif "organic" in best.sources:
        method = "organic"
    elif "page_link" in best.sources:
        method = "ai_overview_or_other_link"
    else:
        method = "scorecard"
    top3 = "; ".join(f"{c.key} ({c.score:.0f}: {', '.join(c.signals)})" for c in order[:3])
    return {"status": "ok", "official_url": official_url, "domain": registered_domain(official_url),
            "confidence_score": round(best.score), "confidence_level": level, "method": method,
            "google_title": best.titles[0] if best.titles else "", "scorecard_url": scorecard_url,
            "top3_candidates": top3,
            "notes": "; ".join(notes)}


def sync_m1_overrides(db: DB, path) -> None:
    """url_override, or a hand-edited official_url (differs from what Module 1 found), counts as the website.

    Call it only while the workbook still matches the database (before processing), never right after it.
    """
    baseline = {uid: d.get("official_url", "") for uid, d in db.all_results(MODULE).items()}
    sync_overrides(db, MODULE, path, "url_override", OVERRIDE_SHEETS, edited_column="official_url", baseline=baseline)


def export(cfg: dict, db: DB, all_insts: list[Institution], out_path) -> None:
    # overrides were synced at the start of run(); edits typed into official_url come back in url_override
    results = db.all_results(MODULE)
    overrides = db.get_overrides(MODULE)
    rows = []
    for inst in all_insts:
        d = results.get(inst.unitid)
        if d is None:
            continue
        row = {c: d.get(c, "") for c in COLUMNS}
        row.update(unitid=inst.unitid, institution=inst.name, city=inst.city, state=inst.state,
                   url_override=overrides.get(inst.unitid, ""))
        if d.get("status") == "error":
            row["confidence_level"] = "Low"
        rows.append(row)
    review = [r for r in rows if r.get("confidence_level") != "High"]
    wb = new_workbook()
    write_sheet(wb, "Websites", COLUMNS, rows, level_col="confidence_level", override_col="url_override")
    write_sheet(wb, "Review", COLUMNS, review, level_col="confidence_level", override_col="url_override")
    path = save_workbook(wb, out_path)
    counts = {lvl: sum(1 for r in rows if r.get("confidence_level") == lvl) for lvl in ("High", "Medium", "Low")}
    log.info("Saved %s: %d rows (High %d, Medium %d, Low %d). Fix Medium/Low rows in 'url_override'.",
             path, len(rows), counts["High"], counts["Medium"], counts["Low"])


def run(cfg: dict, args) -> None:
    paths = cfg["paths"]
    db = DB(resolve_path(paths["db_file"]))
    out_path = resolve_path(paths["m1_output"])
    all_insts = load_institutions(resolve_path(paths["input_file"]), paths.get("input_sheet"))
    sync_m1_overrides(db, out_path)

    if not args.export_only:
        insts = filter_institutions(all_insts, args.state, args.unitid)
        todo = []
        for inst in insts:
            prev = db.get_result(MODULE, inst.unitid)
            if args.force or prev is None or prev["data"].get("status") in ("error", "no_candidates"):
                todo.append(inst)
        if args.limit:
            todo = todo[: args.limit]
        log.info("%d institutions selected, %d to process", len(insts), len(todo))

        if todo:
            use_cache = not args.no_cache
            fetcher = Fetcher(cfg, db, browser_provider=None, use_cache=use_cache)
            lookup = OfficialUrlLookup(cfg, fetcher)

            def browser_provider():
                return get_browser(cfg)

            search = Search(cfg, db, browser_provider, use_cache)

            try:
                with logging_redirect_tqdm():
                    for inst in tqdm(todo, desc="M1 websites", unit="inst", dynamic_ncols=True):
                        try:
                            data = resolve(inst, cfg, fetcher, lookup, search)
                        except CaptchaTimeout:
                            log.error("CAPTCHA was not solved - stopping. Re-run the same command to continue.")
                            break
                        except Exception as e:
                            log.exception("Failed: %s", inst.name)
                            data = {"status": "error", "notes": f"error: {e}"}
                        db.put_result(MODULE, inst.unitid, "", data)
                        log.info("%s -> %s [%s %s]", inst.name, data.get("official_url", ""),
                                 data.get("confidence_level", ""), data.get("confidence_score", ""))
            except KeyboardInterrupt:
                log.warning("Stopped by user - saving what is done so far.")
            finally:
                search.close()
                close_browser()

    export(cfg, db, all_insts, out_path)
