from __future__ import annotations

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

CHROMIUM_LOCK_FILES = ("SingletonLock", "SingletonCookie", "SingletonSocket")


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _lock_pid(profile_path: Path) -> int | None:
    lock_path = profile_path / "SingletonLock"
    if not lock_path.exists() and not lock_path.is_symlink():
        return None
    try:
        value = os.readlink(lock_path) if lock_path.is_symlink() else lock_path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"(?:^|[-_])(\d+)$", value.strip())
    return int(match.group(1)) if match else None


def remove_stale_chromium_locks(profile_path: Path) -> None:
    pid = _lock_pid(profile_path)
    if pid is None or _pid_is_running(pid):
        return
    removed = []
    for name in CHROMIUM_LOCK_FILES:
        path = profile_path / name
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning("Could not remove stale Chromium profile lock %s: %s", path, exc)
        else:
            removed.append(name)
    if removed:
        logger.info("Removed stale Chromium profile locks for dead pid %s in %s: %s", pid, profile_path, ", ".join(removed))
