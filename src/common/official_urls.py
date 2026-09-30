"""Official website per unitid from the College Scorecard API or an IPEDS HD file (cross-check for Module 1)."""
from __future__ import annotations

import csv
import json
import logging
import os

from .config import resolve_path
from .fetch import Fetcher
from .urls import ensure_scheme

log = logging.getLogger(__name__)

SCORECARD_API = "https://api.data.gov/ed/collegescorecard/v1/schools"


class OfficialUrlLookup:
    def __init__(self, cfg: dict, fetcher: Fetcher):
        sc = cfg.get("scorecard", {}) or {}
        self.enabled = bool(sc.get("enabled", True))
        self.api_key = os.getenv("SCORECARD_API_KEY") or sc.get("api_key") or ""
        self.fetcher = fetcher
        self.table: dict[str, str] = {}
        if sc.get("ipeds_hd_csv"):
            path = resolve_path(sc["ipeds_hd_csv"])
            if path.exists():
                self._load_csv(path)
            else:
                log.warning("IPEDS file not found: %s", path)
        if self.enabled and not self.api_key and not self.table:
            log.info("No SCORECARD_API_KEY / IPEDS file: official-website cross-check is off (optional).")

    def _load_csv(self, path) -> None:
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
            for row in csv.DictReader(f):
                row = {(k or "").strip().upper(): (v or "").strip() for k, v in row.items()}
                if row.get("UNITID") and row.get("WEBADDR"):
                    self.table[row["UNITID"]] = row["WEBADDR"]
        log.info("Loaded %d websites from IPEDS file", len(self.table))

    def get(self, unitid: str) -> str:
        if not self.enabled:
            return ""
        if unitid in self.table:
            return ensure_scheme(self.table[unitid])
        if not self.api_key:
            return ""
        url = f"{SCORECARD_API}?id={unitid}&fields=id,school.name,school.school_url&api_key={self.api_key}"
        page = self.fetcher.get(url, allow_browser=False)
        if page is None or not page.ok:
            return ""
        try:
            results = json.loads(page.html).get("results") or []
        except ValueError:
            return ""
        return ensure_scheme((results[0].get("school.school_url") or "").strip()) if results else ""
