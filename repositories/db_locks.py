"""Cross-process locks backed by PostgreSQL advisory locks.

Every lock in the bot-commerce plan (checkout, PayOS webhook, bot switch,
provisioning) needs the same primitive: "only one worker may run this block at a
time, and the lock must never survive a crash". PostgreSQL advisory locks give
exactly that with zero new infrastructure:

- ``pg_try_advisory_xact_lock`` is transaction-scoped: it is released
  automatically when the transaction commits, rolls back, or the connection
  dies. A naive ``locks`` table would leave rows behind after a crash; this
  cannot.
- The key is a ``bigint``, so the human-readable lock name is hashed
  deterministically (SHA-256 -> signed 64-bit) into a stable key.

This replaces the Redis ``SET NX EX`` sketch used in earlier design drafts. The
call shape is deliberately the same (an async context manager), so moving to
Redis later is a mechanical change confined to this one module.

Why not Redis yet: the deployment runs a single uvicorn worker and ``asyncpg``
is already a dependency, so advisory locks remove a blocker rather than add one.

Degraded mode: when ``DATABASE_POOL_URL`` is not configured there is no direct
pool, so we fall back to an in-process ``asyncio.Lock``. That still serialises a
single worker (correct today) but NOT multiple workers - if you ever run more
than one worker you MUST configure ``DATABASE_POOL_URL``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from repositories.direct_db import get_direct_pool

logger = logging.getLogger("db_locks")


class LockTimeout(RuntimeError):
    """Raised when a lock could not be acquired within the requested timeout."""


def advisory_key(name: str) -> int:
    """Deterministic signed 64-bit advisory-lock key for a lock name."""
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


_process_locks: dict[str, asyncio.Lock] = {}


def _process_lock(name: str) -> asyncio.Lock:
    lock = _process_locks.get(name)
    if lock is None:
        lock = asyncio.Lock()
        _process_locks[name] = lock
    return lock


@asynccontextmanager
async def advisory_lock(name: str, *, timeout: float = 10.0) -> AsyncIterator[None]:
    """Hold a cross-process lock for the duration of the ``async with`` block.

    Raises :class:`LockTimeout` instead of blocking forever, so an HTTP request
    returns a clean 409/503 rather than hanging.
    """
    pool = await get_direct_pool()
    if pool is None:
        logger.debug("advisory_lock(%s): no direct pool, using in-process lock", name)
        lock = _process_lock(name)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=timeout)
        except asyncio.TimeoutError:
            raise LockTimeout(f"Could not acquire lock {name!r} within {timeout}s")
        try:
            yield
        finally:
            lock.release()
        return

    key = advisory_key(name)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    async with pool.acquire() as connection:
        async with connection.transaction():
            while True:
                acquired = await connection.fetchval(
                    "SELECT pg_try_advisory_xact_lock($1)", key
                )
                if acquired:
                    break
                if loop.time() >= deadline:
                    raise LockTimeout(
                        f"Could not acquire advisory lock {name!r} within {timeout}s"
                    )
                await asyncio.sleep(0.05)
            yield


@asynccontextmanager
async def try_advisory_lock(name: str) -> AsyncIterator[bool]:
    """Non-blocking variant: yields ``True`` when the lock was acquired.

    Use it when the caller has a cheap success path (return the existing order
    instead of blocking) and should not wait:

        async with try_advisory_lock(f"checkout:{cart_id}") as acquired:
            if not acquired:
                raise HTTPException(409, "Dang xu ly, vui long doi")
            ...
    """
    pool = await get_direct_pool()
    if pool is None:
        lock = _process_lock(name)
        if lock.locked():
            yield False
            return
        await lock.acquire()
        try:
            yield True
        finally:
            lock.release()
        return

    key = advisory_key(name)
    async with pool.acquire() as connection:
        async with connection.transaction():
            acquired = bool(
                await connection.fetchval("SELECT pg_try_advisory_xact_lock($1)", key)
            )
            yield acquired
