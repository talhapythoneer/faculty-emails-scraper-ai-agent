"""One shared Chrome session (undetected-chromedriver) for Google searches and JavaScript-heavy pages."""
from __future__ import annotations

import logging
import random
import re
import sys
import threading
import time
from urllib.parse import quote_plus, urlparse

from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from . import challenges
from .config import resolve_path

log = logging.getLogger(__name__)

ERROR_PAGE_RE = re.compile(r'id="main-frame-error"|class="neterror"|ERR_NAME_NOT_RESOLVED|ERR_CONNECTION_|'
                           r'ERR_TIMED_OUT|ERR_SSL_|ERR_ADDRESS_UNREACHABLE', re.I)
DEAD_SESSION_RE = re.compile(r"no such window|target window already closed|invalid session id|chrome not reachable|"
                             r"disconnected|session deleted|web view not found|no such session", re.I)

# Number of titled results on the page (used to know when results have really loaded).
COUNT_RESULTS_JS = r"""
const root = document.querySelector('#search') || document.querySelector('#center_col') || document.body;
return root ? root.querySelectorAll('a h3, a [role="heading"]').length : 0;
"""


def _norm_query(q: str) -> str:
    return " ".join((q or "").replace("“", '"').replace("”", '"').split()).lower()

# Returns the first visible "Load more / Show more / View all" control that does not navigate away.
FIND_LOAD_MORE_JS = r"""
const re = /^\s*(load|show|view|see)\s+(more|all)\b/i;
const els = Array.from(document.querySelectorAll('button, a, [role="button"]'));
for (const e of els) {
  const t = (e.innerText || '').trim();
  if (!t || t.length > 40 || !re.test(t)) continue;
  if (e.tagName === 'A') {
    const h = (e.getAttribute('href') || '').trim().toLowerCase();
    if (h && !h.startsWith('#') && !h.startsWith('javascript')) continue;
  }
  if (e.disabled) continue;
  const r = e.getBoundingClientRect();
  if (r.width > 0 && r.height > 0) return e;
}
return null;
"""


class CaptchaTimeout(RuntimeError):
    """A Google CAPTCHA was not solved within google.captcha_wait_seconds."""


class Browser:
    def __init__(self, cfg: dict):
        self.bcfg = cfg.get("browser", {})
        self.gcfg = cfg.get("google", {})
        self.lock = threading.RLock()
        self.driver = None
        self._searches = 0
        self._last_search = 0.0

    # --- lifecycle -----------------------------------------------------------
    def _ensure(self) -> None:
        if self.driver is not None:
            return
        import undetected_chromedriver as uc

        opts = uc.ChromeOptions()
        opts.add_argument("--lang=en-US")
        opts.add_argument("--window-size=1280,900")
        opts.add_argument("--no-first-run")
        opts.add_argument("--no-default-browser-check")
        profile = resolve_path(self.bcfg.get("profile_dir", "output/chrome_profile"))
        profile.mkdir(parents=True, exist_ok=True)
        kwargs = {"options": opts, "user_data_dir": str(profile),
                  "headless": bool(self.bcfg.get("headless", False)), "use_subprocess": True}
        if self.bcfg.get("chrome_version_main"):
            kwargs["version_main"] = int(self.bcfg["chrome_version_main"])
        if self.bcfg.get("chrome_binary"):
            kwargs["browser_executable_path"] = str(self.bcfg["chrome_binary"])
        log.info("Starting Chrome (undetected-chromedriver)...")
        self.driver = uc.Chrome(**kwargs)
        self.driver.set_page_load_timeout(int(self.bcfg.get("page_load_timeout", 45)))

    def close(self) -> None:
        with self.lock:
            if self.driver is not None:
                try:
                    self.driver.quit()
                except Exception:
                    pass
                self.driver = None

    @staticmethod
    def _is_dead(error: Exception) -> bool:
        return bool(DEAD_SESSION_RE.search(str(error)))

    def _restart(self) -> None:
        log.warning("Chrome window was closed or crashed - starting a new one.")
        self.close()
        self._ensure()

    def _get(self, url: str) -> None:
        try:
            self.driver.get(url)
        except TimeoutException:
            log.debug("Page load timeout, using what loaded: %s", url)

    # --- Google --------------------------------------------------------------
    def google_search(self, query: str) -> str:
        """Type the query into Google like a person and return the results page HTML."""
        with self.lock:
            self._ensure()
            self._pace()
            for attempt in range(2):
                try:
                    return self._search_once(query)
                except WebDriverException as e:
                    if attempt == 0 and self._is_dead(e):
                        self._restart()
                        continue
                    raise
            return ""

    def _search_once(self, query: str) -> str:
        d = self.driver
        try:
            box = self._find_search_box() if "google." in (d.current_url or "") else None
            if box is None:
                self._get(self.gcfg.get("home_url", "https://www.google.com/?hl=en&gl=us"))
                self._handle_consent()
                self._wait_captcha()
                box = self._find_search_box()
            if box is None:
                raise RuntimeError("Google search box not found")
            box.click()
            box.send_keys(Keys.CONTROL, "a")
            box.send_keys(Keys.DELETE)
            box.send_keys(query)
            time.sleep(random.uniform(0.2, 0.5))
            box.send_keys(Keys.ENTER)
        except CaptchaTimeout:
            raise
        except Exception as e:
            if isinstance(e, WebDriverException) and self._is_dead(e):
                raise
            log.debug("Typing into Google failed (%s); using the search URL directly", e)
            self._get(self._search_url(query))
        if not self._wait_results(query):
            log.debug("Results page did not switch to the new query; loading the search URL directly")
            self._get(self._search_url(query))
            self._wait_results(query)
        self._searches += 1
        self._last_search = time.time()
        return d.page_source

    def _search_url(self, query: str) -> str:
        return self.gcfg.get("search_url", "https://www.google.com/search?hl=en&gl=us&q=") + quote_plus(query)

    def _pace(self) -> None:
        if not self._last_search:
            return
        g = self.gcfg
        gap = random.uniform(float(g.get("min_delay_seconds", 8)), float(g.get("max_delay_seconds", 20)))
        every = int(g.get("long_break_every", 25) or 0)
        if every and self._searches and self._searches % every == 0:
            lo, hi = g.get("long_break_seconds", [60, 120])
            extra = random.uniform(float(lo), float(hi))
            log.info("Taking a %.0fs break from Google...", extra)
            gap += extra
        wait = self._last_search + gap - time.time()
        if wait > 0:
            time.sleep(wait)

    def _find_search_box(self):
        for sel in ('textarea[name="q"]', 'input[name="q"]'):
            for el in self.driver.find_elements(By.CSS_SELECTOR, sel):
                try:
                    if el.is_displayed():
                        return el
                except WebDriverException:
                    continue
        return None

    def _handle_consent(self) -> None:
        for label in ("Accept all", "I agree", "Accept", "Reject all"):
            try:
                buttons = self.driver.find_elements(By.XPATH, f"//button[normalize-space(.)='{label}'] | "
                                                              f"//div[@role='button'][normalize-space(.)='{label}']")
                for b in buttons:
                    if b.is_displayed():
                        b.click()
                        time.sleep(1.5)
                        return
            except WebDriverException:
                continue

    def _is_captcha(self) -> bool:
        try:
            if "/sorry/" in (self.driver.current_url or ""):
                return True
            src = self.driver.page_source[:300000].lower()
        except WebDriverException:
            return False
        return ("captcha-form" in src or "unusual traffic from your computer" in src
                or ("recaptcha" in src and "our systems have detected" in src))

    def _click_captcha_checkbox(self) -> bool:
        """Click the "I'm not a robot" checkbox inside the reCAPTCHA iframe. True if clicked (or already ticked)."""
        d = self.driver
        try:
            frames = [f for f in d.find_elements(By.TAG_NAME, "iframe")
                      if "recaptcha" in (f.get_attribute("src") or "") and "anchor" in (f.get_attribute("src") or "")]
            if not frames:
                return False
            d.switch_to.frame(frames[0])
            try:
                box = WebDriverWait(d, 8).until(EC.element_to_be_clickable((By.ID, "recaptcha-anchor")))
                if box.get_attribute("aria-checked") == "true":
                    return True
                time.sleep(random.uniform(0.6, 1.5))
                ActionChains(d).move_to_element(box).pause(random.uniform(0.2, 0.5)).click().perform()
                return True
            finally:
                d.switch_to.default_content()
        except WebDriverException as e:
            if self._is_dead(e):
                raise
            log.debug("Could not click the reCAPTCHA checkbox: %s", e)
            try:
                d.switch_to.default_content()
            except WebDriverException:
                pass
            return False

    def _wait_captcha(self) -> None:
        if not self._is_captcha():
            return
        wait_s = float(self.gcfg.get("captcha_wait_seconds", 900))
        auto = bool(self.gcfg.get("auto_click_captcha", True))
        clicked = False
        if auto:
            time.sleep(random.uniform(1.5, 3.0))
            clicked = self._click_captcha_checkbox()
        if clicked:
            log.warning("Google CAPTCHA detected - clicked the checkbox automatically, waiting for it to clear...")
        else:
            log.warning("Google CAPTCHA detected - please solve it in the Chrome window (waiting up to %.0f min).",
                        wait_s / 60)
            self._beep()
        start = time.time()
        limit, last_beep, last_click = start + wait_s, start, start
        asked_user = not clicked
        while time.time() < limit:
            time.sleep(3)
            if not self._is_captcha():
                log.info("CAPTCHA solved, continuing.")
                time.sleep(random.uniform(2, 4))
                return
            if auto and time.time() - last_click > 30:  # e.g. the checkbox expired or the page reloaded
                self._click_captcha_checkbox()
                last_click = time.time()
            if not asked_user and time.time() - start > 20:
                log.warning("CAPTCHA still showing (probably an image challenge) - please solve it in the Chrome "
                            "window.")
                self._beep()
                asked_user, last_beep = True, time.time()
            elif asked_user and time.time() - last_beep > 60:
                self._beep()
                last_beep = time.time()
        raise CaptchaTimeout("CAPTCHA was not solved in time")

    def _wait_results(self, query: str) -> bool:
        """Wait until the page shows results for *this* query (title starts with it) and titled results loaded.
        Returns False if the page never switched to the query (e.g. the old results page is still shown)."""
        wanted = _norm_query(query)
        deadline = time.time() + 25
        ready_at = None
        while time.time() < deadline:
            if self._is_captcha():
                self._wait_captcha()
                deadline, ready_at = time.time() + 25, None
                continue
            if _norm_query(self.driver.title).startswith(wanted):
                ready_at = ready_at or time.time()
                titled = self.driver.execute_script(COUNT_RESULTS_JS) or 0
                if titled >= 3 or time.time() - ready_at > 10:
                    time.sleep(random.uniform(1.5, 2.5))  # let late blocks (AI Overview, panel) finish
                    return True
            time.sleep(0.5)
        log.warning("Google results for this query did not appear within 25s")
        return False

    @staticmethod
    def _beep() -> None:
        try:
            import winsound
            winsound.Beep(1000, 600)
        except Exception:
            sys.stdout.write("\a")
            sys.stdout.flush()

    # --- rendering -----------------------------------------------------------
    def render(self, url: str, expand: bool = False) -> tuple[str, str, bool, str]:
        """Load a page in Chrome. Returns (html, final_url, ok, challenge).

        challenge is '' normally, or the kind of bot check ('cloudflare', 'recaptcha', 'hcaptcha', 'other') that
        could not be passed; the caller decides whether to close Chrome, wait and retry (see Fetcher._render).
        """
        with self.lock:
            self._ensure()
            try:
                self.driver.current_url  # noqa: B018 - cheap liveness check
            except WebDriverException as e:
                if not self._is_dead(e):
                    return "", url, False, ""
                self._restart()
            d = self.driver
            try:
                d.get(url)
            except TimeoutException:
                log.debug("Page load timeout, using what loaded: %s", url)
            except WebDriverException as e:
                log.debug("Chrome could not open %s: %s", url, e)
                return "", url, False, ""
            time.sleep(float(self.bcfg.get("render_wait_seconds", 3)))
            try:
                kind = challenges.detect(d)
                if kind:
                    log.info("%s check on %s - trying to pass it...", kind.capitalize(), urlparse(url).hostname)
                    if not challenges.solve(d, kind, float(self.bcfg.get("challenge_solve_seconds", 45)), url=url,
                                            gui_click=bool(self.bcfg.get("challenge_mouse_click", True))):
                        return "", d.current_url or url, False, kind
                    log.info("%s check passed on %s", kind.capitalize(), urlparse(url).hostname)
                if expand:
                    self._expand()
                html, final = d.page_source, d.current_url
            except WebDriverException as e:
                log.debug("Chrome error on %s: %s", url, e)
                return "", url, False, ""
            ok = bool(html) and not ERROR_PAGE_RE.search(html[:20000])
            return html, final, ok, ""

    def _expand(self) -> None:
        d = self.driver
        for _ in range(int(self.bcfg.get("max_load_more_clicks", 30))):
            el = d.execute_script(FIND_LOAD_MORE_JS)
            if el is None:
                break
            before = len(d.page_source)
            try:
                d.execute_script("arguments[0].scrollIntoView({block: 'center'}); arguments[0].click();", el)
            except WebDriverException:
                break
            time.sleep(1.5)
            if len(d.page_source) == before:
                time.sleep(1.5)
                if len(d.page_source) == before:
                    break
        last = 0
        for _ in range(int(self.bcfg.get("max_scrolls", 15))):
            height = d.execute_script("return document.body ? document.body.scrollHeight : 0")
            if height == last:
                break
            last = height
            d.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(1.2)


_instance: Browser | None = None
_instance_lock = threading.Lock()


def get_browser(cfg: dict) -> Browser:
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = Browser(cfg)
        return _instance


def close_browser() -> None:
    global _instance
    with _instance_lock:
        if _instance is not None:
            _instance.close()
            _instance = None
