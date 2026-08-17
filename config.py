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
    return csv_values(os.getenv("ALLOWED_ORIGINS", "http://127.0.0.1:8000"), "http://127.0.0.1:8000")


def agents_enabled() -> bool:
    """Agent code execution is opt-in, never enabled by deployment defaults."""
    return os.getenv("ENABLE_SDLC_AGENTS", "false").lower() == "true"
