"""Bot checks on websites (Cloudflare, reCAPTCHA, hCaptcha...): detect them in Chrome and try to pass them by
clicking the checkbox, like a person would. Used by Browser.render (undetected-chromedriver)."""
from __future__ import annotations

import json
import logging
import random
import time
from typing import Callable

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By

log = logging.getLogger(__name__)

CLICKABLE = ("cloudflare", "recaptcha", "hcaptcha")
SHORT_PAGE_WORDS = 200  # a real page has more text than a bot-check screen

PAGE_INFO_JS = r"""
const body = document.body ? (document.body.innerText || '') : '';
return {
  title: (document.title || '').toLowerCase(),
  words: body.split(/\s+/).filter(Boolean).length,
  text: body.slice(0, 1500).toLowerCase(),
  frames: Array.from(document.querySelectorAll('iframe')).map(f => ((f.src || '') + ' ' + (f.title || '')).toLowerCase()),
  html: document.documentElement ? document.documentElement.outerHTML.slice(0, 150000).toLowerCase() : ''
};
"""
CF_TITLES = ("just a moment", "attention required! | cloudflare", "one moment, please", "please wait...")
OTHER_MARKERS = ("px-captcha", "_incapsula_resource", "captcha-delivery.com", "perimeterx")
# wording of a bot-check screen; a short page is only treated as blocked when it reads like one, so a small contact
# form with an embedded CAPTCHA widget does not get the whole website skipped
HUMAN_TEXT = ("verify you are human", "verifying you are human", "are you a robot", "not a robot", "security check",
              "checking your browser", "confirm you are human", "press & hold", "press and hold",
              "security of your connection", "security verification", "unusual traffic", "complete the captcha",
              "captcha")


def detect(driver) -> str | None:
    """'cloudflare' / 'recaptcha' / 'hcaptcha' / 'other' when the page is a bot-check screen, else None."""
    try:
        info = driver.execute_script(PAGE_INFO_JS) or {}
    except WebDriverException:
        return None
    title, words, text = info.get("title", ""), int(info.get("words", 0)), info.get("text", "")
    frames, html = info.get("frames", []), info.get("html", "")
    short = words < SHORT_PAGE_WORDS
    reads_like_check = any(t in text or t in title for t in HUMAN_TEXT)
    if title.startswith(CF_TITLES) or (short and reads_like_check and "challenges.cloudflare.com" in html):
        return "cloudflare"
    if not (short and reads_like_check):
        return None
    if any("recaptcha" in f and ("anchor" in f or "i'm not a robot" in f) for f in frames):
        return "recaptcha"
    if any("hcaptcha" in f for f in frames):
        return "hcaptcha"
    if any(m in html for m in OTHER_MARKERS) or "cf-chl" in html:
        return "other" if "cf-chl" not in html else "cloudflare"
    return "other"


def _pause(a: float = 0.4, b: float = 1.1) -> None:
    time.sleep(random.uniform(a, b))


def _click_left_of(driver, el) -> bool:
    """Click near the left edge of a widget, where the checkbox sits (works even when it is in a closed shadow DOM)."""
    w = int(el.size.get("width") or 0)
    if w < 40:
        return False
    x = -w // 2 + random.randint(22, 32)
    y = random.randint(-3, 3)
    ActionChains(driver).move_to_element(el).pause(random.uniform(0.3, 0.7)) \
        .move_to_element_with_offset(el, x, y).pause(random.uniform(0.2, 0.5)).click().perform()
    return True


def _click_cloudflare(driver) -> str:
    try:  # the widget host: the element just before the hidden cf-turnstile-response field (closed shadow DOM)
        host = driver.execute_script(
            "const i = document.querySelector('input[name=\"cf-turnstile-response\"]');"
            "return i ? (i.previousElementSibling || i.parentElement.firstElementChild) : null;")
        if host is not None and host.is_displayed() and _click_left_of(driver, host):
            return "clicked"
    except WebDriverException:
        pass
    for sel in ('iframe[src*="challenges.cloudflare.com"]', 'div.cf-turnstile', '#turnstile-wrapper'):
        for el in driver.find_elements(By.CSS_SELECTOR, sel):
            try:
                if el.is_displayed() and _click_left_of(driver, el):
                    return "clicked"
            except WebDriverException:
                continue
    return "none"


def _click_in_frame(driver, frame_match: Callable[[str], bool], box_selector: str) -> str:
    frames = [f for f in driver.find_elements(By.TAG_NAME, "iframe")
              if frame_match(((f.get_attribute("src") or "") + " " + (f.get_attribute("title") or "")).lower())]
    for f in frames:
        try:
            if not f.is_displayed():
                continue
            driver.switch_to.frame(f)
            try:
                boxes = driver.find_elements(By.CSS_SELECTOR, box_selector)
                if boxes:
                    if boxes[0].get_attribute("aria-checked") == "true":
                        return "clicked"
                    _pause()
                    ActionChains(driver).move_to_element(boxes[0]).pause(random.uniform(0.2, 0.5)).click().perform()
                    return "clicked"
            finally:
                driver.switch_to.default_content()
        except WebDriverException:
            try:
                driver.switch_to.default_content()
            except WebDriverException:
                pass
    return "none"


def _picture_puzzle(driver) -> bool:
    """reCAPTCHA / hCaptcha opened an image challenge: clicking cannot solve it."""
    for f in driver.find_elements(By.TAG_NAME, "iframe"):
        try:
            src = (f.get_attribute("src") or "").lower()
            if ("recaptcha" in src and "bframe" in src) or ("hcaptcha" in src and "challenge" in src):
                if f.is_displayed() and (f.size.get("height") or 0) > 150:
                    return True
        except WebDriverException:
            continue
    return False


def click(driver, kind: str) -> str:
    """'clicked', 'none' (nothing to click) or 'image' (a picture puzzle appeared)."""
    if kind == "cloudflare":
        return _click_cloudflare(driver)
    if kind == "recaptcha":
        res = _click_in_frame(driver, lambda s: "recaptcha" in s and "anchor" in s, "#recaptcha-anchor")
    elif kind == "hcaptcha":
        res = _click_in_frame(driver, lambda s: "hcaptcha" in s and "checkbox" in s, "#checkbox")
    else:
        return "none"
    if res == "clicked":
        time.sleep(random.uniform(2.5, 4.0))
        if _picture_puzzle(driver):
            return "image"
    return res


# ------------------------------------------------------------------ Cloudflare: act while the driver is detached
# Turnstile notices input that arrives through the automation connection. Detaching chromedriver for a few seconds
# (undetected_chromedriver.Chrome.reconnect) and reloading / clicking with the real mouse meanwhile looks like a
# normal person's browser.
TURNSTILE_BOX_JS = r"""
const inp = document.querySelector('input[name="cf-turnstile-response"]');
let host = inp ? (inp.previousElementSibling || inp.parentElement.firstElementChild) : null;
if (!host) host = document.querySelector('div.cf-turnstile, #turnstile-wrapper, iframe[src*="challenges.cloudflare.com"]');
if (!host) return null;
const r = host.getBoundingClientRect();
return {x: r.left, y: r.top, w: r.width, h: r.height, sx: window.screenX, sy: window.screenY,
        ow: window.outerWidth, oh: window.outerHeight, iw: window.innerWidth, ih: window.innerHeight,
        dpr: window.devicePixelRatio || 1};
"""


def _detached(driver, seconds: float) -> None:
    """Detach chromedriver for a few seconds (the page keeps running), then attach again."""
    if hasattr(driver, "reconnect"):
        driver.reconnect(seconds)
        try:
            driver.switch_to.window(driver.window_handles[-1])
        except WebDriverException:
            pass
    else:
        time.sleep(seconds)


def reload_detached(driver, url: str, seconds: float = 8.0) -> None:
    """Open the URL again while detached: Cloudflare's automatic check often passes this way, with no click."""
    driver.execute_script(f"setTimeout(function(){{ window.location.href = {json.dumps(url)}; }}, 600);")
    _detached(driver, seconds)


def _chrome_content_rect(title: str):
    """(hwnd, (left, top, right, bottom)) of the web-page area of the Chrome window showing `title`, in physical
    screen pixels. Asking Windows directly handles several monitors, page zoom and display scaling."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # physical pixels everywhere
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
    found = []
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def text_of(hwnd):
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value

    def class_of(hwnd):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        return buf.value

    def on_top(hwnd, _):
        if user32.IsWindowVisible(hwnd) and class_of(hwnd) == "Chrome_WidgetWin_1" and text_of(hwnd).startswith(title):
            found.append(hwnd)
        return True

    user32.EnumWindows(enum_proc(on_top), 0)
    for hwnd in found:
        kids = []

        def on_child(child, _):
            if class_of(child) == "Chrome_RenderWidgetHostHWND" and user32.IsWindowVisible(child):
                kids.append(child)
            return True

        user32.EnumChildWindows(hwnd, enum_proc(on_child), 0)
        for child in kids:
            r = wintypes.RECT()
            user32.GetWindowRect(child, ctypes.byref(r))
            if r.right - r.left > 200 and r.bottom - r.top > 200:
                return hwnd, (r.left, r.top, r.right, r.bottom)
    return None


def _real_click(hwnd, x: int, y: int) -> None:
    """Bring the window to the front, glide the mouse to (x, y) and click, with Windows' own mouse events."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE if minimized
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.3)
    p = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    steps = random.randint(12, 20)
    for i in range(1, steps + 1):
        t = i / steps
        user32.SetCursorPos(int(p.x + (x - p.x) * t + random.uniform(-2, 2)), int(p.y + (y - p.y) * t + random.uniform(-2, 2)))
        time.sleep(random.uniform(0.01, 0.03))
    user32.SetCursorPos(x, y)
    time.sleep(random.uniform(0.1, 0.25))
    user32.mouse_event(0x0002, 0, 0, 0, 0)  # left down
    time.sleep(random.uniform(0.06, 0.12))
    user32.mouse_event(0x0004, 0, 0, 0, 0)  # left up


def gui_click_turnstile(driver, seconds: float = 6.0) -> bool:
    """Click the Turnstile checkbox with the real mouse while chromedriver is detached. False when not possible
    (not Windows, the widget or the Chrome window was not found)."""
    import sys

    if sys.platform != "win32":
        return False
    try:
        box = driver.execute_script(TURNSTILE_BOX_JS)
        title = driver.title or ""
    except WebDriverException:
        return False
    if not box or box["w"] < 150 or box["h"] < 40 or not title:
        return False
    try:
        found = _chrome_content_rect(title)
    except Exception as e:
        log.debug("Could not locate the Chrome window: %s", e)
        return False
    if not found:
        return False
    hwnd, (left, top, right, _bottom) = found
    scale = (right - left) / max(1.0, float(box["iw"]))  # physical pixels per CSS pixel (zoom x display scaling)
    x = int(left + (box["x"] + random.uniform(24, 32)) * scale)
    y = int(top + (box["y"] + box["h"] / 2 + random.uniform(-3, 3)) * scale)
    if hasattr(driver, "reconnect"):
        try:
            driver.service.stop()
        except Exception:
            pass
    try:
        _real_click(hwnd, x, y)
    except Exception as e:
        log.debug("Mouse click failed: %s", e)
    time.sleep(seconds)
    if hasattr(driver, "reconnect"):
        driver.reconnect(0.5)
        try:
            driver.switch_to.window(driver.window_handles[-1])
        except WebDriverException:
            pass
    return True


def solve(driver, kind: str, timeout: float = 45.0, reclick_every: float = 12.0, url: str = "",
          gui_click: bool = True) -> bool:
    """Try to get past the check. True if passed.

    Cloudflare: wait (it often passes by itself) -> reload while detached -> click the checkbox with the real mouse
    while detached (gui_click) -> click through the driver. reCAPTCHA / hCaptcha: click the "I'm not a robot" box;
    give up when a picture puzzle appears (clicking cannot solve those).
    """
    end = time.time() + timeout
    time.sleep(random.uniform(3.0, 5.0))
    reloaded = False
    last_click = 0.0
    while time.time() < end:
        current = detect(driver)
        if current is None:
            time.sleep(random.uniform(1.5, 3.0))  # let the real page finish loading
            return True
        if current == "cloudflare" and url and not reloaded:
            reloaded = True
            log.debug("Cloudflare check: reloading while detached")
            reload_detached(driver, url)
            continue
        if current in CLICKABLE and time.time() - last_click > reclick_every:
            last_click = time.time()
            if current == "cloudflare" and gui_click and gui_click_turnstile(driver):
                log.debug("Cloudflare check: clicked with the mouse")
                continue
            res = click(driver, current)
            log.debug("%s check: click -> %s", current, res)
            if res == "image":
                log.info("%s check turned into a picture puzzle - cannot be solved by clicking", current)
                return False
        time.sleep(2.0)
    return False
