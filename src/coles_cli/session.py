from __future__ import annotations

import contextlib
import fcntl
import logging
import random
import shutil
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError

from coles_cli.conf import (
    BROWSER_DEFAULT_TIMEOUT_MS,
    DEFAULT_MAX_PACE_S,
    DEFAULT_MIN_PACE_S,
    HUMAN_MOUSE_MAX_TIME_S,
    browser_headless,
    coles_cli_home,
)
from coles_cli.profile_locks import remove_stale_chromium_locks

logger = logging.getLogger(__name__)


def profile_dir(name: str) -> Path:
    return coles_cli_home() / "profiles" / name


def clear_profile(name: str) -> None:
    shutil.rmtree(profile_dir(name), ignore_errors=True)


def _locks_dir() -> Path:
    return coles_cli_home() / "locks"


@contextlib.contextmanager
def session_lock(name: str):
    path = _locks_dir() / f"{name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class ColesSession:
    """Camoufox-backed browser session with a persistent local profile."""

    def __init__(self, name: str):
        self.name = name
        self.context = None
        self.page = None
        self._browser_cm = None

    def __enter__(self) -> "ColesSession":
        self.ensure_browser()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def ensure_browser(self) -> None:
        if self.page is not None:
            try:
                if not self.page.is_closed() and self.context is not None and self.context.browser is not None and self.context.browser.is_connected():
                    return
            except PlaywrightError:
                pass
            self.close()
        from camoufox.sync_api import Camoufox

        path = profile_dir(self.name)
        path.mkdir(parents=True, exist_ok=True)
        remove_stale_chromium_locks(path)
        self._browser_cm = Camoufox(
            persistent_context=True,
            user_data_dir=str(path),
            headless=browser_headless(),
            humanize=HUMAN_MOUSE_MAX_TIME_S,
            os=["macos", "windows", "linux"],
            locale="en-AU",
        )
        self.context = self._browser_cm.__enter__()
        self.context.set_default_timeout(BROWSER_DEFAULT_TIMEOUT_MS)
        self.context.set_default_navigation_timeout(BROWSER_DEFAULT_TIMEOUT_MS)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        logger.debug("Opened Camoufox profile %s", path)

    def wait(self, min_delay: float = DEFAULT_MIN_PACE_S, max_delay: float = DEFAULT_MAX_PACE_S) -> None:
        time.sleep(random.uniform(min_delay, max_delay))
        if self.page:
            try:
                self.page.wait_for_load_state("domcontentloaded")
            except PlaywrightError:
                pass

    def close(self) -> None:
        try:
            if self.context:
                try:
                    self.context.close()
                except PlaywrightError:
                    pass
            if self._browser_cm:
                try:
                    self._browser_cm.__exit__(None, None, None)
                except PlaywrightError:
                    pass
        finally:
            self.context = None
            self.page = None
            self._browser_cm = None
