from __future__ import annotations

import os
from pathlib import Path

COLES_BASE_URL = "https://www.coles.com.au"
COLES_HOME_URL = f"{COLES_BASE_URL}/"
COLES_ORDERS_ACTIVE_URL = f"{COLES_BASE_URL}/account/orders?status=active"
COLES_ORDERS_PAST_URL = f"{COLES_BASE_URL}/account/orders?status=past"

BROWSER_DEFAULT_TIMEOUT_MS = 30_000
BROWSER_LOGIN_TIMEOUT_MS = 60_000
HUMAN_TYPE_DELAY_MS = 45
HUMAN_MOUSE_MAX_TIME_S = 0.225
DEFAULT_MIN_PACE_S = 0.6
DEFAULT_MAX_PACE_S = 1.4
WORKER_IDLE_TIMEOUT_S = 900
DEFAULT_CHECKOUT_TIMEOUT_S = 180


def coles_cli_home() -> Path:
    return Path(os.environ.get("COLES_CLI_HOME") or Path.home() / ".coles-cli")


def browser_headless() -> bool:
    return os.environ.get("COLES_CLI_HEADLESS", "").lower() in {"1", "true", "yes", "on"}


def load_dotenv_file(path: Path | None = None) -> dict[str, str]:
    """Load KEY=VALUE pairs from .env without overriding existing env vars."""
    env_path = path or Path.cwd() / ".env"
    if not env_path.exists():
        return {}

    loaded = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_dotenv_line(raw_line)
        if parsed is None:
            continue
        key, value = parsed
        if key in os.environ:
            continue
        os.environ[key] = value
        loaded[key] = value
    return loaded


def _parse_dotenv_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].strip()
    if "=" not in stripped:
        return None
    key, value = stripped.split("=", 1)
    key = key.strip()
    if not key:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    elif " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return key, value
