from __future__ import annotations

import json
import logging
from collections.abc import Callable

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from coles_cli.conf import HUMAN_TYPE_DELAY_MS
from coles_cli.exceptions import ElementNotFoundError

logger = logging.getLogger(__name__)

LocatorFactory = Callable[[Page], Locator]


def first_visible(page: Page, factories: list[LocatorFactory], *, timeout_ms: int = 1500) -> Locator | None:
    for factory in factories:
        locator = factory(page).first
        try:
            locator.wait_for(state="visible", timeout=timeout_ms)
            return locator
        except (PlaywrightTimeoutError, PlaywrightError):
            continue
    return None


def goto_domcontentloaded(page: Page, url: str) -> None:
    try:
        page.goto(url, wait_until="domcontentloaded")
    except PlaywrightTimeoutError:
        if page.url == "about:blank":
            raise
    try:
        page.wait_for_load_state("domcontentloaded")
    except PlaywrightTimeoutError:
        if page.url == "about:blank":
            raise


def require_visible(page: Page, factories: list[LocatorFactory], *, label: str, timeout_ms: int = 2500) -> Locator:
    locator = first_visible(page, factories, timeout_ms=timeout_ms)
    if locator is None:
        raise ElementNotFoundError(f"Could not find visible {label}")
    return locator


def human_fill(locator: Locator, text: str) -> None:
    locator.click()
    try:
        locator.fill("")
    except PlaywrightError:
        locator.press("Meta+A")
        locator.press("Backspace")
    locator.press_sequentially(text, delay=HUMAN_TYPE_DELAY_MS)


def visible_text(locator: Locator | None) -> str:
    if locator is None:
        return ""
    try:
        return normalize_space(locator.inner_text(timeout=1000))
    except PlaywrightError:
        return ""


def safe_attr(locator: Locator | None, name: str) -> str | None:
    if locator is None:
        return None
    try:
        return locator.get_attribute(name, timeout=1000)
    except PlaywrightError:
        return None


def clean_url(url: str | None) -> str | None:
    if not url:
        return None
    return url.split("#", 1)[0]


def normalize_space(text: str | None) -> str:
    return " ".join(str(text or "").split())


def next_data(page: Page) -> dict:
    try:
        raw = page.locator("#__NEXT_DATA__").first.text_content(timeout=1000)
    except PlaywrightError:
        raw = None
    if not raw:
        try:
            raw = page.evaluate("() => document.getElementById('__NEXT_DATA__')?.textContent || ''")
        except PlaywrightError:
            raw = ""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def dismiss_cookie_banner(page: Page) -> None:
    locators = [
        lambda p: p.get_by_role("button", name="Accept all"),
        lambda p: p.get_by_role("button", name="Accept All"),
        lambda p: p.get_by_role("button", name="I accept"),
        lambda p: p.locator("#onetrust-accept-btn-handler"),
        lambda p: p.locator('button:has-text("Accept")'),
    ]
    button = first_visible(page, locators, timeout_ms=400)
    if button is None:
        return
    try:
        button.click(timeout=1000)
        page.wait_for_timeout(300)
    except PlaywrightError:
        pass
