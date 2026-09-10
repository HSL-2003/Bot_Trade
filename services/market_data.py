"""Shared market-data board (Phase 1 · item 1.4).

Goal: N bots (one per trading account) currently each run their own
``start_price_feed_loop`` and hit 2-4 external quote APIs per symbol per second.
That multiplies external load linearly with accounts. This module provides a
single shared "market board" that:

- any writer (a dedicated feed task OR one elected bot) can ``publish()`` to,
- any consumer (every bot, WebSocket fan-out) can ``read()`` from — reading one
  consistent, immutable-ish snapshot instead of racing each other's scalars.

It also owns the shared clock so consumers don't each probe external feeds.

The board is intentionally *provider-agnostic*: it does not know about Binance
or TradingView. Wiring an actual long-lived feed task into the FastAPI lifespan
is the application's job (and is the next refactor step — Phase 2). This module
only establishes the shared, race-free data surface.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional


class MarketDataBoard:
    """Thread-safe-ish (asyncio) shared quote/state snapshot store."""

    def __init__(self) -> None:
        self._snapshot: dict[str, Any] = {}
        self._version: int = 0
        self._lock = asyncio.Lock()
        self._subscribers: set[asyncio.Queue] = set()

    async def publish(self, update: dict[str, Any]) -> int:
        """Replace the whole snapshot atomically and broadcast a tick.

        Callers (the single feed task) should pass a fully-consistent dict so
        readers never observe a half-updated market state.
        Returns the new version number.
        """
        async with self._lock:
            self._snapshot = dict(update)
            self._version += 1
            version = self._version
        # Fan out outside the lock (fast, does not block other publishes).
        for queue in list(self._subscribers):
            try:
                queue.put_nowait((version, self._snapshot))
            except asyncio.QueueFull:
                # Slow consumer — drop its stale tick; it will surface the next
                # publish. This is exactly the backpressure WebSocket fan-out
                # needs at scale.
                pass
        return version

    def read(self) -> tuple[int, dict[str, Any]]:
        """Return (version, snapshot). Snapshot is safe to read concurrently."""
        return self._version, self._snapshot

    def get_quote(self, symbol: str) -> Optional[dict[str, Any]]:
        """Return a quote dict for ``symbol`` from the latest snapshot."""
        quotes = self._snapshot.get("quotes") or {}
        return quotes.get(symbol)

    async def subscribe(self) -> tuple[int, dict[str, Any]]:
        """Coroutine subscription: yields (version, snapshot) on each publish.

        Used by WebSocket fan-out nodes instead of per-client polling/full state.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=4)
        self._subscribers.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(queue)


_default_board: Optional[MarketDataBoard] = None


def get_market_board() -> MarketDataBoard:
    """Process-wide shared market board (single source of truth for quotes)."""
    global _default_board
    if _default_board is None:
        _default_board = MarketDataBoard()
    return _default_board