"""Rules for deciding which program names are qualifying bachelor's majors (no AI)."""
from __future__ import annotations

import re

from .html_utils import collapse
from .scoring import kw_regex, norm

# "B.S.", "BA", "BSN", "Bachelor", "Major" - used to recognise program listings.
DEGREE_MARKER_RE = re.compile(r"(?<![A-Za-z])B\.?\s?[SA]\.?(?:N\.?)?(?![A-Za-z])|(?i:\bbachelor)|(?i:\bmajors?\b)")

GENERIC_LINK_TEXT = {"learn more", "read more", "more", "view program", "view", "details", "more info",
                     "more information", "explore", "click here", "program details", "visit", "apply",
                     "request info", "see more", "view more", "view all", "show more", "load more", "program page"}


MAJOR_TAIL_RE = re.compile(r"\s+major\b(?P<tail>.*)$", re.I)
DELIVERY_RE = re.compile(r"\b(main campus|online|regional|hybrid|on campus|on-campus)\b", re.I)


LEVEL_TAG_RE = re.compile(r"\s+(Undergraduate|Graduate|Professional)\b(?=\s*[–—|-]|\s+(?:Graduate|Undergraduate|"
                          r"Program|Professional)\b|\s*$)")


def clean_program_name(text: str) -> str:
    t = collapse(text)
    t = re.sub(r"\s*[»›→>]+\s*$", "", t)
    t = re.sub(r"(?i)\s*(learn more|read more|view program|more info)\s*$", "", t)
    # program-finder cards: "Applied Physics Major Main campus engineering" -> "Applied Physics"
    # (only after "Major": a "Minor / Graduate / Certificate" tail is kept so the rules can still exclude it)
    m = MAJOR_TAIL_RE.search(t)
    if m and DELIVERY_RE.search(m.group("tail")) and not re.search(r"graduate|minor|certificat", m.group("tail"), re.I):
        t = t[: m.start()].strip(" -–|,")
    # catalog program finders: "Accounting Major, BSM Undergraduate – New Orleans ..." -> "Accounting Major, BSM";
    # a Graduate / Professional tag is kept as "(graduate)" so the exclusion rules still drop it
    m = LEVEL_TAG_RE.search(t)
    if m and ("," in t[: m.start()] or re.search(r"\b(major|minor)\b", t[: m.start()], re.I)):
        head = t[: m.start()].strip(" -–|,")
        t = head if m.group(1).lower() == "undergraduate" else f"{head} (graduate)"
    return "" if t.lower() in GENERIC_LINK_TEXT else t


def degree_code(name: str) -> str:
    """'Bachelor of Arts in Psychology' / 'B.A. in Psychology' / 'Psychology, BA' -> 'ba' (used to drop duplicates)."""
    low = (name or "").lower()
    if re.search(r"\bb\.?\s?s\.?\s?n\b|bachelor of science in nursing", low):
        return "bsn"
    if re.search(r"bachelor of fine arts|\bb\.?\s?f\.?\s?a\b", low):
        return "bfa"
    if re.search(r"bachelor of science|\bb\.?\s?s\b\.?", low):
        return "bs"
    if re.search(r"bachelor of arts|\bb\.?\s?a\b\.?", low):
        return "ba"
    return ""


def program_key(name: str) -> str:
    """Same major, same degree -> same key ('B.A. in Psychology' == 'Bachelor of Arts in Psychology')."""
    core = norm(core_name(name) or name)
    return re.sub(r"^(in|of)\s+", "", core)


def core_name(name: str) -> str:
    """'Biology, B.S.' -> 'Biology';  'Bachelor of Science in Nursing (BSN)' -> 'Nursing'."""
    s = re.sub(r"\([^)]*\)", " ", name or "")
    s = re.sub(r"(?i)\bbachelor(?:'s|’s)?\s+of\s+(?:science|arts|fine arts|applied science|social work)\s*(?:in\s+)?",
               " ", s)
    s = re.sub(r"(?i)\bbachelor(?:'s|’s)?\b", " ", s)
    s = re.sub(r"(?<![A-Za-z])(?:B\.?\s?S\.?\s?N\.?|B\.?\s?S\.?\s?E\.?|B\.?\s?F\.?\s?A\.?|B\.?\s?S\.?|B\.?\s?A\.?)"
               r"(?![A-Za-z])", " ", s)
    s = re.sub(r"(?i)\b(major|degree|program|online)\b", " ", s)
    s = re.sub(r"^\s*(?i:in)\s+", " ", s)
    s = re.sub(r"[,:;|/–—-]+", " ", s)
    return collapse(s)


def slug_variants(core: str) -> list[str]:
    """URL fragments that likely identify a program: 'exercise-science', 'exercisescience', 'exercise'..."""
    words = [w for w in norm(core).split() if w not in ("and", "of", "the", "in", "for")]
    if not words:
        return []
    out = ["-".join(words), "_".join(words), "".join(words)]
    long_words = [w for w in words if len(w) >= 5]
    if long_words:
        out.append(long_words[0])
    return list(dict.fromkeys(v for v in out if len(v) >= 4))


class MajorMatcher:
    def __init__(self, cfg: dict):
        mc = cfg["majors"]
        self.targets = [(str(k), kw_regex(str(k))) for k in mc["target_keywords"]]
        self.ambiguous = [kw_regex(str(k), prefix=True) for k in mc.get("ambiguous_keywords", [])]
        self.exclude = [re.compile(p, re.I) for p in mc.get("exclude_patterns", [])]
        self.noise = [re.compile(p, re.I) for p in mc.get("noise_patterns", [])]
        self.min_len = int(mc.get("min_name_length", 3))
        self.max_len = int(mc.get("max_name_length", 90))

    def target(self, name: str) -> str:
        t = norm(name)
        for kw, rx in self.targets:
            if rx.search(t):
                return kw
        return ""

    def is_ambiguous(self, name: str) -> bool:
        t = norm(name)
        return any(rx.search(t) for rx in self.ambiguous)

    def excluded(self, name: str) -> str:
        low = (name or "").lower()
        for rx in self.exclude:
            if rx.search(low):
                return rx.pattern
        return ""

    def is_noise(self, name: str) -> bool:
        if not (self.min_len <= len(name or "") <= self.max_len):
            return True
        low = collapse(name).lower()
        return any(rx.search(low) for rx in self.noise)

    def classify(self, name: str) -> tuple[str, str]:
        """-> ("include", keyword) | ("ambiguous", "") | ("excluded", pattern) | ("noise", "") | ("other", "")"""
        if self.is_noise(name):
            return "noise", ""
        reason = self.excluded(name)
        if reason:
            return "excluded", reason
        kw = self.target(name)
        if kw:
            return "include", kw
        if self.is_ambiguous(name):
            return "ambiguous", ""
        return "other", ""
