"""Shared httpx connection pools for the FastAPI app.

Phase 1 — replace per-call httpx transports with long-lived connection pools
reused across requests (no TLS handshake / connection re-establishment per
call), and never block the asyncio event loop.

Crucial detail: an ``httpx.AsyncClient`` is bound to the event loop it was
created in. ``asyncio.run()`` and TestClient each spin up their own loop, so a
single process-global client breaks when another loop touches it. The pool is
therefore keyed by the *running event loop* — production has one long-lived
loop so it gets exactly one pool, and tests get a clean pool per loop.

Callers pass their own ``headers`` per request (service-role keys, bearer
tokens, etc.) so no credential defaults live in the shared pool.
"""

from __future__ import annotations

import asyncio
import httpx

_http_pools: dict[int, httpx.AsyncClient] = {}


def get_http_pool() -> httpx.AsyncClient | None:
    """Pooled AsyncClient for the currently running event loop, or None.

    Returns ``None`` when called outside a running loop (sync context) so
    callers can fall back to a blocking request instead of crashing.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    loop_id = id(loop)
    client = _http_pools.get(loop_id)
    if client is None:
        client = httpx.AsyncClient(
            timeout=10.0,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            follow_redirects=False,
        )
        _http_pools[loop_id] = client
    return client


async def aclose_http_pool() -> None:
    """Close every pooled client (idempotent). Call once at app shutdown."""
    for client in list(_http_pools.values()):
        try:
            await client.aclose()
        except Exception:
            pass
    _http_pools.clear()