"""Runtime configuration.

Keeping environment parsing here prevents the HTTP gateway and trading domain
from each having their own interpretation of deployment settings.
"""

import os
from typing import FrozenSet


SUPPORTED_SYMBOLS: FrozenSet[str] = frozenset({"XAUUSD", "USOIL", "EURUSD", "GBPUSD"})


def csv_values(value: str, default: str) -> list[str]:
    raw = value or default
    return [item.strip() for item in raw.split(",") if item.strip()]


def allowed_origins() -> list[str]:
    origins = csv_values(os.getenv("ALLOWED_ORIGINS", "http://127.0.0.1:8000"), "http://127.0.0.1:8000")
    # Never combine a "*" wildcard with allow_credentials=True (which app.py sets).
    # A wildcard origin with credentialed CORS is silently rejected by browsers and
    # signals a mis-configuration, so we fail closed by dropping it and refusing to
    # run without an explicit origin allow-list.
    filtered = [o for o in origins if o != "*" and o.strip()]
    if filtered:
        return filtered
    raise RuntimeError("ALLOWED_ORIGINS must be an explicit origin allow-list (wildcard '*' is not supported with credentials)")


def agents_enabled() -> bool:
    """Agent code execution is opt-in, never enabled by deployment defaults."""
    return os.getenv("ENABLE_SDLC_AGENTS", "false").lower() == "true"
