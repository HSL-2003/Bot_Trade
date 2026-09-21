"""Critical-path database access (Phase 1 · item 1.3).

Trading writes (trade open/close, account state) go straight to PostgreSQL via
``asyncpg`` through the Supabase pooler (transaction mode, port 6543) instead of
round-tripping through PostgREST HTTP. This removes 30-120 ms of HTTP/serialization
latency from the close-order hot path that previously blocked the event loop.

Design rules (from the architecture review, "Bẫy 3"):
- NO silent fallback. If ``DATABASE_POOL_URL`` is configured, this writer is the
  ONLY path for the writes it owns; if it fails, the operation raises so the
  execution pipeline can retry or halt — never try PostgREST as a second path
  (that is how duplicate/state-drift bugs are born).
- Configuration is explicit: without ``DATABASE_POOL_URL`` the app keeps using
  PostgREST everywhere (existing deployment — unchanged). Setting the env var
  switches the critical path on. Whenever the var is SET, asyncpg must be
  installed; a missing package is a hard, clear error, not a silent fallback.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from services.execution_pipeline import logger


class DirectDbError(RuntimeError):
    """Raised when the direct database path is configured but unusable."""


_pools: dict[int, Any] = {}


async def _get_pool():
    """Return the asyncpg pool bound to the *currently running* event loop.

    Like the httpx pool in ``async_http.py``, the pool must be keyed by the
    running loop: ``asyncio.run()``, TestClient and uvicorn each own a different
    loop, and a pool created on a closed loop fails with
    ``RuntimeError: Event loop is closed``. Production has one long-lived loop,
    so it still ends up with exactly one pool. Raises ``DirectDbError`` with a
    clear message when configured but asyncpg is not installed.
    """
    import asyncio

    try:
        import asyncpg  # noqa: F401
    except ImportError:
        raise DirectDbError(
            "DATABASE_POOL_URL is set but 'asyncpg' is not installed. "
            "Install it (pip install asyncpg) or unset DATABASE_POOL_URL. "
            "There is intentionally no silent fallback to PostgREST."
        )

    loop_id = id(asyncio.get_running_loop())
    pool = _pools.get(loop_id)
    if pool is not None:
        return pool
    url = os.getenv("DATABASE_POOL_URL", "")
    if not url:
        raise DirectDbError("DATABASE_POOL_URL is not set")
    import asyncpg as apg

    # statement_cache_size=0 is REQUIRED behind the Supabase transaction pooler
    # (PgBouncer): server-side prepared statements do not survive connection
    # multiplexing and fail intermittently with "prepared statement already
    # exists". Verified against the live pooler (6543).
    pool = await apg.create_pool(
        url,
        min_size=1,
        max_size=6,
        timeout=15.0,
        statement_cache_size=0,
    )
    _pools[loop_id] = pool
    return pool


async def get_direct_pool():
    """Public accessor: the asyncpg pool, or ``None`` when not configured.

    Unlike the private ``_get_pool`` this returns ``None`` for an unconfigured
    deployment, so optional features (notifications, advisory locks) can degrade
    gracefully, while the critical trade path keeps its fail-fast behaviour.

    A configured-but-broken setup still raises ``DirectDbError`` (missing
    asyncpg, bad URL) - that is a deployment error, not something to paper over.
    """
    if not os.getenv("DATABASE_POOL_URL", ""):
        return None
    return await _get_pool()


async def close_direct_db() -> None:
    """Close every pooled connection (idempotent). Call once at app shutdown."""
    for pool in list(_pools.values()):
        try:
            await pool.close()
        except Exception:
            pass
    _pools.clear()


async def record_trade_close_direct(
    *,
    account_id: str,
    ticket: int,
    close_price: Optional[float],
    profit: Optional[float],
) -> None:
    """Direct-write a closed trade to trade_orders (fail-fast).

    Only reached when DATABASE_POOL_URL is configured. The caller (execution
    pipeline) owns retry/backoff; this either writes or raises.

    NOTE: requires a unique index on (account_id, broker_ticket) for idempotent
    upserts; if it does not exist yet the UPDATE below still works (single row
    per ticket as long as opens also use record_trade_open_direct).
    """
    pool = await _get_pool()
    await pool.execute(
        """
        UPDATE trade_orders
           SET status = 'closed',
               close_price = $3,
               profit = $4,
               closed_at = timezone('utc', now()),
               updated_at = timezone('utc', now())
         WHERE account_id = $1 AND broker_ticket = $2 AND is_active = true
        """,
        account_id,
        ticket,
        close_price,
        profit,
    )


async def record_trade_open_direct(
    *,
    account_id: str,
    ticket: int,
    symbol: str,
    side: str,
    volume: float,
    open_price: Optional[float],
) -> None:
    """Direct-write an open trade to trade_orders (fail-fast).

    Upserts by (account_id, broker_ticket) so a broker-confirmed fill that raced
    with a cancellation can never create a duplicate open row.
    """
    pool = await _get_pool()
    await pool.execute(
        """
        INSERT INTO trade_orders
            (account_id, broker_ticket, symbol, side, order_type, quantity,
             entry_price, status, is_active, submitted_at, filled_at, created_at, updated_at)
        VALUES ($1, $2, $3, $4, 'MARKET', $5, $6, 'filled', true,
                timezone('utc', now()), timezone('utc', now()),
                timezone('utc', now()), timezone('utc', now()))
        ON CONFLICT (account_id, broker_ticket)
        DO UPDATE SET status = 'filled',
                      entry_price = EXCLUDED.entry_price,
                      updated_at = timezone('utc', now())
        """,
        account_id,
        ticket,
        symbol,
        side,
        volume,
        open_price,
    )