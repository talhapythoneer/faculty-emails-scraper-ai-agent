"""AI calls used by Module 3, only where the rules can't decide. Responses are cached in SQLite.

Providers (config `llm.provider`):
  groq      - Groq's OpenAI-compatible API (GROQ_API_KEY). Free tier: per-model limits on tokens per minute/day,
              so calls wait on "too fast" and switch to the next model in `llm.models` when a daily limit is hit.
  anthropic - Claude (ANTHROPIC_API_KEY).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time

import httpx

from .db import DB

log = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
ROLE_ENUM = ["chair", "program_director", "dean", "coordinator", "office", "professor", "staff", "emeritus", "unknown"]

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {"decisions": {"type": "array", "items": {
        "type": "object",
        "properties": {"index": {"type": "integer"}, "include": {"type": "boolean"}},
        "required": ["index", "include"], "additionalProperties": False}}},
    "required": ["decisions"], "additionalProperties": False,
}
PROGRAMS_SCHEMA = {
    "type": "object",
    "properties": {"programs": {"type": "array", "items": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "link_index": {"type": "integer"}},
        "required": ["name", "link_index"], "additionalProperties": False}}},
    "required": ["programs"], "additionalProperties": False,
}
PICK_SCHEMA = {
    "type": "object",
    "properties": {"index": {"type": "integer"}},
    "required": ["index"], "additionalProperties": False,
}
CONTACTS_SCHEMA = {
    "type": "object",
    "properties": {"contacts": {"type": "array", "items": {
        "type": "object",
        "properties": {"email": {"type": "string"}, "name": {"type": "string"}, "title": {"type": "string"},
                       "role": {"type": "string", "enum": ROLE_ENUM}},
        "required": ["email", "name", "title", "role"], "additionalProperties": False}}},
    "required": ["contacts"], "additionalProperties": False,
}


def _numbered_links(links) -> str:
    return "\n".join(f"{i}. {l.text[:120]} -> {l.url}" for i, l in enumerate(links))


def _trim(text: str, max_chars: int) -> str:
    """Cut at a line break so numbered lists stay whole (keeps requests under the per-minute token limit)."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    cut = text.rfind("\n", 0, max_chars)
    return text[: cut if cut > max_chars // 2 else max_chars]


class LLM:
    def __init__(self, cfg: dict, db: DB, module: str = "m3"):
        lc = cfg.get("llm", {}) or {}
        self.provider = str(lc.get("provider", "groq")).lower()
        self.criteria = (lc.get("criteria") or "").strip()
        self.db = db
        self.module = module
        self.max_tokens = int(lc.get("max_tokens", 4096))
        self.max_prompt_chars = int(lc.get("max_prompt_chars", 0) or 0)
        self.timeout = float(lc.get("timeout", 120))
        self.max_retries = int(lc.get("max_retries", 4))
        self._lock = threading.Lock()

        if self.provider == "groq":
            self.models = list(lc.get("models") or ["openai/gpt-oss-120b"])
            self.api_key = os.getenv("GROQ_API_KEY", "")
            # one effort for every task ("medium"), or per task: {default: medium, extract_contacts: low}
            self.reasoning_effort = lc.get("reasoning_effort", "medium")
            self.max_wait = float(lc.get("max_wait_seconds", 90))
        else:
            self.models = [lc.get("model", "claude-haiku-4-5")]
            self.api_key = os.getenv("ANTHROPIC_API_KEY", "")
        self._model_idx = 0

        key_name = "GROQ_API_KEY" if self.provider == "groq" else "ANTHROPIC_API_KEY"
        wanted = bool(lc.get("enabled", True))
        self.enabled = wanted and bool(self.api_key)
        if wanted and not self.api_key:
            log.warning("%s is not set - AI steps are skipped (rules only).", key_name)
        self.client = None
        if self.enabled and self.provider == "groq":
            self.client = httpx.Client(timeout=self.timeout)
        elif self.enabled:
            import anthropic
            self.client = anthropic.Anthropic(max_retries=self.max_retries, timeout=self.timeout)
        if self.enabled:
            log.info("AI: %s, model %s", self.provider, self.models[0])

    @property
    def model(self) -> str:
        return self.models[min(self._model_idx, len(self.models) - 1)]

    def _effort(self, purpose: str) -> str:
        e = getattr(self, "reasoning_effort", "")
        if isinstance(e, dict):
            return str(e.get(purpose) or e.get("default") or "")
        return str(e or "")

    def _call(self, unitid: str, purpose: str, system: str, user: str, schema: dict):
        if not self.enabled:
            return None
        if self.max_prompt_chars and len(system) + len(user) > self.max_prompt_chars:
            user = _trim(user, self.max_prompt_chars - len(system))
            log.debug("AI prompt for %s trimmed to %d chars", purpose, len(user))
        # cache key without the model (a switch to the fallback model does not re-ask answered questions), but
        # with the reasoning effort: answers from a lower effort are not reused when the effort is raised
        key = hashlib.sha256(json.dumps([self.provider, self._effort(purpose), system, user, schema],
                                        sort_keys=True).encode()).hexdigest()
        cached = self.db.get_llm_cache(key)
        if cached is not None:
            return json.loads(cached)
        if self.provider == "groq":
            result = self._groq(purpose, system, user, schema)
        else:
            result = self._anthropic(purpose, system, user, schema)
        if result is None:
            return None
        text, model, tin, tout = result
        self.db.log_llm(self.module, unitid, purpose, model, tin, tout)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            log.warning("AI returned invalid JSON for %s", purpose)
            return None
        self.db.put_llm_cache(key, json.dumps(data))
        return data

    # --- Groq ------------------------------------------------------------------
    def _groq(self, purpose: str, system: str, user: str, schema: dict):
        attempts = 0
        json_retry = None  # after an answer that failed the JSON check: (other model, more room)
        while self.enabled:
            model = json_retry[0] if json_retry else self.model
            body = {
                "model": model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "response_format": {"type": "json_schema",
                                    "json_schema": {"name": purpose, "strict": True, "schema": schema}},
                "max_completion_tokens": json_retry[1] if json_retry else self.max_tokens,
                "temperature": 0,
            }
            effort = self._effort(purpose)
            if model.startswith("openai/gpt-oss") and effort:
                body["reasoning_effort"] = effort
            try:
                r = self.client.post(GROQ_URL, json=body, headers={"Authorization": f"Bearer {self.api_key}"})
            except httpx.HTTPError as e:
                attempts += 1
                if attempts > self.max_retries:
                    log.warning("AI connection error (%s): %s", purpose, e)
                    return None
                time.sleep(2 ** attempts)
                continue

            if r.status_code == 200:
                data = r.json()
                choice = (data.get("choices") or [{}])[0]
                usage = data.get("usage") or {}
                tin, tout = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
                if choice.get("finish_reason") != "stop":
                    log.warning("AI call %s stopped early (%s)", purpose, choice.get("finish_reason"))
                    self.db.log_llm(self.module, "", purpose, model, tin, tout)
                    return None
                return (choice.get("message") or {}).get("content") or "", model, tin, tout

            msg = r.text[:300]
            if r.status_code in (401, 403):
                log.error("AI authentication failed - check GROQ_API_KEY (%s). AI steps disabled.", msg)
                self.enabled = False
                return None
            if r.status_code == 429:
                wait = self._retry_after(r)
                daily = re.search(r"per day|\(TPD\)|\(RPD\)", r.text, re.I)
                if daily or wait > self.max_wait:
                    self._next_model(f"daily limit reached ({msg[:120]})")
                    attempts = 0
                    continue
                attempts += 1
                if attempts > self.max_retries + 4:
                    log.warning("AI still rate-limited after %d tries (%s) - skipping this call", attempts, purpose)
                    return None
                log.debug("AI rate limit (%s), waiting %.0fs", purpose, wait)
                time.sleep(wait)
                continue
            if r.status_code == 413 or "request too large" in r.text.lower():
                log.warning("AI request too large for the free tier (%s) - skipping this call", purpose)
                return None
            if r.status_code >= 500:
                attempts += 1
                if attempts > self.max_retries:
                    log.warning("AI API error %s (%s): %s", r.status_code, purpose, msg)
                    return None
                time.sleep(2 ** attempts)
                continue
            if r.status_code == 400 and json_retry is None and re.search(r"json_validate_failed|expected schema", r.text):
                # the answer ran out of room or broke the format (long pages): once more, other model, more room
                others = [m for m in self.models if m != model]
                json_retry = (others[0] if others else model, max(self.max_tokens * 2, 16384))
                log.info("AI answer for %s failed the JSON check - retrying once with %s", purpose, json_retry[0])
                continue
            log.error("AI request rejected (%s): HTTP %s %s", purpose, r.status_code, msg)
            return None
        return None

    @staticmethod
    def _retry_after(r: httpx.Response) -> float:
        try:
            return max(1.0, float(r.headers.get("retry-after", "")))
        except ValueError:
            m = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", r.text)
            return (int(m.group(1) or 0) * 60 + float(m.group(2))) if m else 10.0

    def _next_model(self, reason: str) -> None:
        with self._lock:
            if self._model_idx + 1 < len(self.models):
                self._model_idx += 1
                log.warning("AI model switched to %s: %s", self.models[self._model_idx], reason)
            elif self.enabled:
                self.enabled = False
                log.error("AI disabled for the rest of this run - every model hit its free limit (%s). "
                          "Re-run tomorrow: finished rows are kept and AI answers are cached.", reason)

    # --- Anthropic ---------------------------------------------------------------
    def _anthropic(self, purpose: str, system: str, user: str, schema: dict):
        import anthropic
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
        except anthropic.AuthenticationError as e:
            log.error("AI authentication failed - check ANTHROPIC_API_KEY (%s). AI steps disabled.", e)
            self.enabled = False
            return None
        except anthropic.BadRequestError as e:
            log.error("AI request rejected (%s): %s", purpose, e)
            return None
        except anthropic.RateLimitError as e:
            log.warning("AI rate limit (%s), skipping this call: %s", purpose, e)
            return None
        except anthropic.APIStatusError as e:
            log.warning("AI API error %s (%s): %s", e.status_code, purpose, e)
            return None
        except anthropic.APIConnectionError as e:
            log.warning("AI connection error (%s): %s", purpose, e)
            return None
        if resp.stop_reason in ("max_tokens", "refusal"):
            log.warning("AI call %s stopped early (%s)", purpose, resp.stop_reason)
            return None
        text = next((b.text for b in resp.content if b.type == "text"), "")
        return text, self.model, resp.usage.input_tokens, resp.usage.output_tokens

    # --- tasks -----------------------------------------------------------------
    def classify_programs(self, unitid: str, institution: str, names: list[str]) -> dict[int, bool] | None:
        system = ("You screen academic program names for a university outreach mailing list.\n" + self.criteria +
                  "\nDecide from the program name only. include=true only for undergraduate bachelor's MAJORS that "
                  "fit the criteria. include=false for minors, certificates, associate or graduate programs, "
                  "pre-professional tracks, student services, offices, and anything that is not an academic major.")
        user = f"Institution: {institution}\n\nProgram names:\n" + "\n".join(f"{i}. {n}" for i, n in enumerate(names))
        data = self._call(unitid, "classify_programs", system, user, CLASSIFY_SCHEMA)
        if data is None:
            return None
        return {d["index"]: bool(d["include"]) for d in data.get("decisions", []) if isinstance(d.get("index"), int)}

    def extract_programs_from_text(self, unitid: str, institution: str, text: str, links) -> list[dict] | None:
        system = ("You read the text of a university's undergraduate programs page and list the bachelor's degree "
                  "MAJORS that fit these criteria:\n" + self.criteria +
                  "\nCopy each program name exactly as it appears in the page text. Never invent programs. "
                  "Exclude minors, certificates, associate and graduate programs.")
        user = (f"Institution: {institution}\n\nPAGE TEXT:\n{text}\n\nLINKS (index. text -> url):\n"
                f"{_numbered_links(links)}\n\nFor each qualifying major give its name and the index of the link "
                "that points to its page (-1 if none).")
        data = self._call(unitid, "extract_programs", system, user, PROGRAMS_SCHEMA)
        return None if data is None else data.get("programs", [])

    def pick_faculty_link(self, unitid: str, institution: str, program: str, links) -> str:
        system = "You help find a university department's faculty directory page."
        user = (f"Institution: {institution}\nProgram: {program}\n\nWhich link is most likely the faculty/staff "
                "directory or people page of the department that runs this program (a page listing faculty with "
                "contact details)? Answer -1 if none fits.\n\nLINKS (index. text -> url):\n" + _numbered_links(links))
        data = self._call(unitid, "pick_faculty_link", system, user, PICK_SCHEMA)
        if not data:
            return ""
        idx = data.get("index", -1)
        return links[idx].url if isinstance(idx, int) and 0 <= idx < len(links) else ""

    def extract_contacts(self, unitid: str, snippets: list[tuple[str, str]]) -> dict[str, dict] | None:
        system = ("Each snippet is the text around one email address on a university web page. For each email, "
                  "return the person's name and job title exactly as written in the snippet (empty string if not "
                  "present) and a role category: chair = department chair/head; program_director = director of a "
                  "program, center or undergraduate studies; dean = dean or associate/assistant dean; coordinator = "
                  "undergraduate coordinator or academic advisor; office = a shared office/department mailbox, not a "
                  "person; professor = professor, lecturer or instructor; staff = other staff; emeritus = "
                  "emeritus/retired faculty; unknown = cannot tell.")
        user = "\n\n".join(f"[{i}] EMAIL: {e}\nSNIPPET: {t}" for i, (e, t) in enumerate(snippets))
        data = self._call(unitid, "extract_contacts", system, user, CONTACTS_SCHEMA)
        if data is None:
            return None
        return {c["email"].strip().lower(): c for c in data.get("contacts", []) if c.get("email")}

    def close(self) -> None:
        if self.provider == "groq" and self.client is not None:
            self.client.close()
