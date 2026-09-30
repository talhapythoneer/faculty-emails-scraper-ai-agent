"""Keyword scoring, fuzzy name matching and confidence levels."""
from __future__ import annotations

import re
from functools import lru_cache

from rapidfuzz import fuzz

from .urls import domain_label, path_words, subdomain

SMALL_WORDS = {"of", "the", "and", "at", "in", "for", "a", "an", "on"}
GENERIC_NAME_WORDS = SMALL_WORDS | {"university", "college", "institute", "school", "community", "campus", "state",
                                    "saint", "st"}


def norm(text: str) -> str:
    s = (text or "").lower().replace("&", " and ")
    s = re.sub(r"['’]", "", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


@lru_cache(maxsize=8192)
def kw_regex(keyword: str, prefix: bool = False) -> re.Pattern:
    """Whole-word regex for a normalised keyword (prefix=True drops the trailing word boundary)."""
    tail = "" if prefix else r"(?![a-z0-9])"
    return re.compile(r"(?<![a-z0-9])" + re.escape(norm(keyword)) + tail)


def keyword_score(text: str, weights: dict) -> float:
    """Best positive keyword weight + sum of all matching negative weights."""
    t = norm(text)
    if not t:
        return 0.0
    best, negative = 0.0, 0.0
    for kw, w in weights.items():
        if kw_regex(str(kw)).search(t):
            w = float(w)
            if w > 0:
                best = max(best, w)
            else:
                negative += w
    return best + negative


def link_score(text: str, url: str, weights: dict, url_factor: float = 0.5) -> float:
    return keyword_score(text, weights) + url_factor * keyword_score(path_words(url), weights)


def tier_bonus(count: int, tiers) -> float:
    """tiers = [[min_count, points], ...] sorted from highest min_count down."""
    for min_count, points in tiers:
        if count >= min_count:
            return float(points)
    return 0.0


def acronyms(name: str) -> set[str]:
    words = [w for w in norm(name).split() if w not in SMALL_WORDS]
    out = {"".join(w[0] for w in words)}
    return {a for a in out if len(a) >= 2}


def domain_matches_name(host: str, name: str) -> bool:
    """Does the domain look like the institution name? (e.g. bellarmine.edu, eku.edu, umdearborn.edu)"""
    labels = [domain_label(host)] + [s for s in subdomain(host).split(".") if s and s != "www"]
    tokens = [t for t in norm(name).split() if t not in GENERIC_NAME_WORDS]
    concat = "".join(norm(name).split())
    acrs = acronyms(name)
    for label in labels:
        label = label.replace("-", "")
        if len(label) < 2:
            continue
        for t in tokens:
            if len(t) >= 4 and (t in label or (len(t) >= 5 and t[:5] in label) or (len(label) >= 4 and label in t)):
                return True
        if label in acrs or any(len(label) >= 3 and len(a) >= 3 and (a.startswith(label) or label.startswith(a))
                                for a in acrs):
            return True
        if len(label) >= 5 and label in concat:
            return True
    return False


def fuzzy_contains(needle: str, haystack: str, threshold: float) -> bool:
    a, b = norm(needle), norm(haystack)
    if not a or not b:
        return False
    return fuzz.partial_ratio(a, b) >= threshold


def confidence_level(score: float, high: float, medium: float) -> str:
    if score >= high:
        return "High"
    if score >= medium:
        return "Medium"
    return "Low"
