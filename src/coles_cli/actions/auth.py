from __future__ import annotations

import os
import re
import sys
import time

from coles_cli.browser import dismiss_cookie_banner, first_visible, goto_domcontentloaded, next_data, safe_attr, visible_text
from coles_cli.conf import COLES_BASE_URL, COLES_ORDERS_ACTIVE_URL
from coles_cli.exceptions import AuthenticationError, ColesUnavailableError, InteractiveAuthenticationRequired

SIGN_IN_LOCATORS = [
    lambda p: p.get_by_role("link", name=re.compile(r"log in|login|sign in|sign up", re.I)),
    lambda p: p.get_by_role("button", name=re.compile(r"log in|login|sign in|sign up", re.I)),
    lambda p: p.locator('a[href*="login"], a[href*="signin"], a[href*="sign-in"]'),
    lambda p: p.locator('button:has-text("Log in"), button:has-text("Sign in")'),
]
AUTHENTICATED_LOCATORS = [
    lambda p: p.locator('a[href*="/account/orders"]').filter(has_text=re.compile(r"orders|view|details", re.I)),
    lambda p: p.get_by_role("button", name=re.compile(r"log out|logout", re.I)),
    lambda p: p.get_by_role("link", name=re.compile(r"log out|logout", re.I)),
]
BLOCKING_LOCATORS = [
    lambda p: p.locator('text=/Access Denied|service unavailable|temporarily unavailable|technical issue/i'),
]


def _is_login_url(url: str) -> bool:
    lower = url.lower()
    return "login" in lower or "signin" in lower or "colesgroupprofile" in lower or "auth" in lower


def _is_coles_url(url: str) -> bool:
    return url.lower().startswith(COLES_BASE_URL) or "colesgroupprofile" in url.lower()


def _next_auth_state(page) -> bool | None:
    data = next_data(page)
    auth = (((data.get("props") or {}).get("pageProps") or {}).get("initialState") or {}).get("user", {}).get("auth", {})
    value = auth.get("authenticated")
    return value if isinstance(value, bool) else None


def _blocking_state(session, *, timeout_ms: int = 500) -> str | None:
    locator = first_visible(session.page, BLOCKING_LOCATORS, timeout_ms=timeout_ms)
    if locator is not None:
        return visible_text(locator) or "Coles appears unavailable or blocked"
    return None


def _account_hint(session) -> str | None:
    page = session.page
    try:
        hint = page.evaluate(
            r"""
            () => {
              const clean = (text) => (text || '').replace(/\s+/g, ' ').trim();
              const visible = (node) => {
                const rect = node && node.getBoundingClientRect();
                return !!rect && rect.width > 0 && rect.height > 0;
              };
              const isNoisy = (value) => !value || /^(account|profile|nav|btn|button|icon|menu)$/i.test(value) || /\$/.test(value);
              const extractHi = (value) => {
                const raw = (value || '').replace(/[ \t]+/g, ' ').trim();
                const match = raw.match(/^(hi|hello)[,\s]+([a-z][a-z'\-]{1,20})(?:\s|$)/i);
                return match ? `${match[1]} ${match[2]}`.replace(/\s+/g, ' ').trim() : '';
              };
              const candidates = Array.from(document.querySelectorAll(
                '[data-testid*="account" i], [data-testid*="user" i], [aria-label*="account" i], [aria-label*="profile" i], header a[href*="/account"], header button[href*="/account"]'
              )).filter(visible);
              for (const node of candidates) {
                let value = clean(node.getAttribute('aria-label') || node.getAttribute('title') || '');
                if (!isNoisy(value) && value.length <= 80) return value;
                const hi = extractHi(node.innerText || node.textContent || '');
                if (hi) return hi;
              }
              const hi = Array.from(document.querySelectorAll('header, [class*="header" i]'))
                .filter(visible)
                .flatMap((node) => Array.from(node.querySelectorAll('span, p, div')))
                .filter(visible)
                .map((node) => clean(node.innerText || node.textContent || ''))
                .filter((value) => /^(hi|hello)\s+\S+/i.test(value) && value.length <= 60);
              if (hi.length) return hi[0];
              return null;
            }
            """
        )
    except Exception:  # noqa: BLE001
        hint = None
    return hint if hint else None


def _cart_snapshot(session) -> dict:
    page = session.page
    try:
        return page.evaluate(
            r"""
            () => {
              const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
              const visible = (n) => { const r = n && n.getBoundingClientRect(); return !!r && r.width > 0 && r.height > 0; };
              const btn = Array.from(document.querySelectorAll(
                'header [data-testid*="trolley" i], header [data-testid*="cart" i], header [aria-label*="trolley" i], header [aria-label*="cart" i]'
              )).find(visible);
              if (!btn) return {available: false};
              const label = clean(btn.getAttribute('aria-label') || '');
              const text = clean(btn.innerText || '');
              const totalMatch = (label + ' ' + text).match(/\$\s*(\d+(?:\.\d{2})?)/);
              const total = totalMatch ? totalMatch[0].replace(/\s+/g, '') : '';
              const empty = !total || /^\$0(?:\.00)?$/.test(total);
              return {available: true, total, empty};
            }
            """
        )
    except Exception:  # noqa: BLE001
        return {"available": False}


def _header_auth_state(page, *, timeout_ms: int = 1000) -> bool | None:
    try:
        locator = page.locator('button[data-testid="header-user"], [data-testid="header-user"]').first
        locator.wait_for(state="visible", timeout=timeout_ms)
        text = visible_text(locator) or safe_attr(locator, "aria-label") or ""
    except Exception:  # noqa: BLE001
        return None
    if re.search(r"\b(log\s*in|login|sign\s*in|sign\s*up)\b", text, re.I):
        return False
    if re.search(r"\b(account|hi|hello)\b", text, re.I):
        return True
    return None


def _showing_sign_in(session, *, timeout_ms: int = 500) -> bool:
    page = session.page
    header_state = _header_auth_state(page, timeout_ms=timeout_ms)
    if header_state is not None:
        return not header_state
    if _account_page_looks_authenticated(page):
        return False
    if _is_login_url(page.url):
        return True
    return first_visible(page, SIGN_IN_LOCATORS, timeout_ms=timeout_ms) is not None


def _current_authenticated_account(session, *, timeout_ms: int = 1000) -> dict | None:
    page = session.page
    blocking = _blocking_state(session, timeout_ms=300)
    if blocking:
        raise ColesUnavailableError(blocking)
    if not _is_coles_url(page.url):
        return None
    header_state = _header_auth_state(page, timeout_ms=timeout_ms)
    if header_state is True:
        return {"ok": True, "authenticated": True, "state": "logged_in", "account": _account_hint(session), "url": page.url}
    if header_state is False:
        return None
    auth_state = _next_auth_state(page)
    if auth_state is True:
        return {"ok": True, "authenticated": True, "state": "logged_in", "account": _account_hint(session), "url": page.url}
    if auth_state is False and _showing_sign_in(session, timeout_ms=100):
        return None
    if _account_page_looks_authenticated(page):
        return {"ok": True, "authenticated": True, "state": "logged_in", "account": _account_hint(session), "url": page.url}
    if first_visible(page, AUTHENTICATED_LOCATORS, timeout_ms=timeout_ms) is not None and first_visible(page, SIGN_IN_LOCATORS, timeout_ms=100) is None:
        return {"ok": True, "authenticated": True, "state": "logged_in", "account": _account_hint(session), "url": page.url}
    return None


def _account_page_looks_authenticated(page) -> bool:
    if "/account/" not in page.url.lower():
        return False
    try:
        text = page.locator("body").inner_text(timeout=1000)
    except Exception:  # noqa: BLE001
        return False
    if re.search(r"log in\s*/?\s*sign up|log in or sign up|start shopping|sign in to", text, re.I):
        return False
    return bool(re.search(r"order history|current orders|past orders|order details|no current orders|no past orders", text, re.I))


def open_login_page(session) -> None:
    page = session.page
    goto_domcontentloaded(page, COLES_ORDERS_ACTIVE_URL)
    dismiss_cookie_banner(page)
    sign_in = first_visible(page, SIGN_IN_LOCATORS, timeout_ms=2500)
    if sign_in is None:
        return
    try:
        label = visible_text(sign_in) or safe_attr(sign_in, "aria-label") or "login"
        sign_in.click(timeout=5000)
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(1000)
        print(f"Opened Coles login from {label!r}", file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


def ensure_logged_in(session, *, url: str = COLES_ORDERS_ACTIVE_URL) -> dict:
    page = session.page
    goto_domcontentloaded(page, url)
    dismiss_cookie_banner(page)

    blocking = _blocking_state(session)
    if blocking:
        raise ColesUnavailableError(blocking)

    account = _current_authenticated_account(session, timeout_ms=700)
    if account is not None:
        return account

    if _showing_sign_in(session, timeout_ms=700):
        raise InteractiveAuthenticationRequired(
            "Interactive authentication is required. Run `coles login --interactive --wait --timeout 300`, "
            "complete Coles login manually in Camoufox, then rerun this command."
        )

    if clean_fallback_url := (COLES_ORDERS_ACTIVE_URL if page.url != COLES_ORDERS_ACTIVE_URL else ""):
        goto_domcontentloaded(page, clean_fallback_url)
        dismiss_cookie_banner(page)
        blocking = _blocking_state(session)
        if blocking:
            raise ColesUnavailableError(blocking)
        account = _current_authenticated_account(session, timeout_ms=1000)
        if account is not None:
            return account
        if _showing_sign_in(session, timeout_ms=700):
            raise InteractiveAuthenticationRequired(
                "Interactive authentication is required. Run `coles login --interactive --wait --timeout 300`, "
                "complete Coles login manually in Camoufox, then rerun this command."
            )

    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        blocking = _blocking_state(session, timeout_ms=300)
        if blocking:
            raise ColesUnavailableError(blocking)
        account = _current_authenticated_account(session, timeout_ms=500)
        if account is not None:
            return account
        if _showing_sign_in(session, timeout_ms=300):
            raise InteractiveAuthenticationRequired("Coles is showing a login flow")
        time.sleep(0.5)

    raise AuthenticationError(f"Coles did not reach an authenticated account page; current URL: {page.url}")


def auth_status(session) -> dict:
    try:
        result = ensure_logged_in(session)
        result["cart"] = _cart_snapshot(session)
        return result
    except InteractiveAuthenticationRequired as exc:
        return {
            "ok": True,
            "authenticated": False,
            "state": "signed_out",
            "message": str(exc),
            "next_command": "coles login --interactive --wait --timeout 300",
        }
    except ColesUnavailableError as exc:
        return {"ok": True, "authenticated": False, "state": "unavailable", "message": str(exc)}
    except AuthenticationError as exc:
        return {"ok": True, "authenticated": False, "state": "unknown", "message": str(exc)}


def interactive_auth(session, wait: bool = False, timeout: int = 300) -> dict:
    open_login_page(session)
    page = session.page
    if os.environ.get("COLES_CLI_WORKER") == "1" and not wait:
        wait = True
    if wait:
        print(f"Complete Coles login in the Camoufox browser. Waiting up to {timeout} seconds...", file=sys.stderr)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if page.is_closed():
                    raise InteractiveAuthenticationRequired("Coles login browser window was closed before login completed")
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, InteractiveAuthenticationRequired):
                    raise
            try:
                page.wait_for_load_state("domcontentloaded")
            except Exception:  # noqa: BLE001
                pass
            try:
                account = _current_authenticated_account(session)
                if account is not None:
                    return account
            except ColesUnavailableError:
                raise
            time.sleep(2)
        raise InteractiveAuthenticationRequired(f"Coles login was not completed within {timeout} seconds")

    print("Complete Coles login in the Camoufox browser, then press Enter here.", file=sys.stderr)
    input()
    return ensure_logged_in(session)
