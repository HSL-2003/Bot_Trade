"""Notification service - in-app notifications with real-time fan-out.

Delivery uses two independent channels, both fed by the same
``public.notifications`` row (see ``supabase/notification_migration.sql``):

1. Persistent (source of truth) - every notification is inserted into the table,
   so a client that is offline still sees the full history on reconnect.
2. Real-time (best effort) - the row is pushed to whoever is connected now:
   - Web dashboard: an in-process push handler registered by ``app.py`` fans the
     payload out over the existing ``/ws`` WebSocket.
   - Mobile (Flutter): Supabase Realtime ``postgres_changes`` on
     ``public.notifications`` filtered by ``user_id`` - no extra server work per
     client, and it keeps working when the backend restarts.

Design rules:
- A notification must never break the business flow that produced it. Delivery
  failures are logged and swallowed (a lost toast must not roll back a paid
  order), but a failure to *persist* is reported by returning ``None`` so the
  caller can decide.
- Writes prefer the direct asyncpg pool (``DATABASE_POOL_URL``) and fall back to
  the Supabase REST API, matching the rest of the codebase.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Awaitable, Callable, Optional

from services.async_http import get_http_pool

logger = logging.getLogger("notifications")

# Stable notification types. The Flutter app maps these to icons and deep links,
# so treat them as public API: add new ones, never rename existing ones.
TYPE_ORDER_PAID = "ORDER_PAID"
TYPE_BOT_ACTIVATED = "BOT_ACTIVATED"
TYPE_BOT_SWITCH_REQUESTED = "BOT_SWITCH_REQUESTED"
TYPE_BOT_SWITCH_APPROVED = "BOT_SWITCH_APPROVED"
TYPE_BOT_SWITCH_REJECTED = "BOT_SWITCH_REJECTED"
TYPE_BOT_SWITCH_DEFERRED = "BOT_SWITCH_DEFERRED"
TYPE_BOT_SWITCH_EXECUTED = "BOT_SWITCH_EXECUTED"
TYPE_REFUND_REQUESTED = "REFUND_REQUESTED"
TYPE_REFUND_COMPLETED = "REFUND_COMPLETED"
TYPE_VPS_PAYMENT_FAILED = "VPS_PAYMENT_FAILED"
TYPE_VPS_SUSPENDED = "VPS_SUSPENDED"
TYPE_VPS_TERMINATED = "VPS_TERMINATED"
TYPE_PROVISIONING_FAILED = "PROVISIONING_FAILED"

# Admin-facing types (default audience is the admin team).
TYPE_ADMIN_NEW_SWITCH_REQUEST = "ADMIN_NEW_SWITCH_REQUEST"
TYPE_ADMIN_REFUND_REQUEST = "ADMIN_REFUND_REQUEST"
TYPE_ADMIN_PROVISIONING_ALERT = "ADMIN_PROVISIONING_ALERT"
TYPE_ADMIN_SECURITY_ALERT = "ADMIN_SECURITY_ALERT"

PushHandler = Callable[[str, dict], Awaitable[None]]

_push_handlers: list[PushHandler] = []


def _json_safe(value: Any) -> Any:
    """Coerce a value into something ``json`` can always encode.

    Notification payloads carry values straight out of the database: asyncpg
    hands back ``UUID``/``Decimal``/``datetime`` objects, and PostgREST hands back
    strings, so the same call site must work for both. Without this, persistence
    failed with ``TypeError: Object of type UUID is not JSON serializable`` and -
    because notification delivery must never break a business flow - the failure
    was silent (observed live on the first paid order, 2026-09-21).
    """
    return json.loads(json.dumps(value, default=str))


def register_push_handler(handler: PushHandler) -> None:
    """Register a real-time delivery callback (called by ``app.py`` at startup).

    The handler receives ``(user_id, payload)``. Exceptions are logged and
    ignored so one broken socket cannot block delivery to the others.
    """
    if handler not in _push_handlers:
        _push_handlers.append(handler)


async def _broadcast(user_id: str, payload: dict) -> None:
    for handler in list(_push_handlers):
        try:
            await handler(user_id, payload)
        except Exception:
            logger.warning(
                "Notification push handler failed for user %s", user_id, exc_info=True
            )


def _rest_headers(key: str) -> dict:
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


async def _insert(user_id: str, payload: dict) -> Optional[str]:
    """Persist one notification. Returns its id, or ``None`` when unwritable."""
    from repositories.direct_db import get_direct_pool

    pool = await get_direct_pool()
    if pool is not None:
        row = await pool.fetchrow(
            "INSERT INTO notifications (user_id, type, title, message, data, severity) "
            "VALUES ($1, $2, $3, $4, $5::jsonb, $6) RETURNING id",
            user_id,
            payload["type"],
            payload["title"],
            payload["message"],
            json.dumps(payload["data"]),
            payload["severity"],
        )
        return str(row["id"]) if row else None
    return await _insert_via_rest(user_id, payload)


async def _insert_via_rest(user_id: str, payload: dict) -> Optional[str]:
    base = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base or not key:
        logger.warning(
            "Notifications disabled: SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set"
        )
        return None
    client = get_http_pool()
    if client is None:
        logger.warning("Notifications disabled: no running event loop")
        return None
    response = await client.post(
        f"{base}/rest/v1/notifications",
        headers={**_rest_headers(key), "Prefer": "return=representation"},
        json={
            "user_id": user_id,
            "type": payload["type"],
            "title": payload["title"],
            "message": payload["message"],
            "data": payload["data"],
            "severity": payload["severity"],
        },
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"notification insert failed: HTTP {response.status_code} {response.text[:200]}"
        )
    rows = response.json()
    return str(rows[0]["id"]) if rows else None


async def notify_user(
    user_id: str,
    ntype: str,
    title: str,
    message: str,
    data: Optional[dict] = None,
    severity: str = "info",
) -> Optional[str]:
    """Persist a notification for one user and push it in real time.

    Never raises for a delivery problem: business flows (a paid order, a switch
    approval) must not fail because a toast could not be shown. Returns the
    notification id, or ``None`` when it could not be persisted.
    """
    payload = {
        "type": ntype,
        "title": title,
        "message": message,
        # Normalised once here so every sink (JSONB column, REST body, WebSocket
        # frame) is guaranteed encodable even when callers pass database types.
        "data": _json_safe(data or {}),
        "severity": severity,
    }
    try:
        notification_id = await _insert(user_id, payload)
    except Exception:
        logger.error("Failed to persist notification for user %s", user_id, exc_info=True)
        return None
    await _broadcast(user_id, {**payload, "id": notification_id})
    return notification_id


async def notify_all_admins(
    ntype: str,
    title: str,
    message: str,
    data: Optional[dict] = None,
    severity: str = "info",
) -> int:
    """Notify every active admin. Returns how many were notified."""
    admin_ids = await _admin_user_ids()
    for user_id in admin_ids:
        await notify_user(user_id, ntype, title, message, data, severity)
    return len(admin_ids)


async def _admin_user_ids() -> list[str]:
    """User ids whose ``user_profiles.roles`` contains ``"admin"``."""
    from repositories.direct_db import get_direct_pool

    pool = await get_direct_pool()
    if pool is not None:
        rows = await pool.fetch(
            "SELECT user_id FROM user_profiles "
            "WHERE roles @> $1::jsonb AND is_active = true",
            json.dumps(["admin"]),
        )
        return [str(row["user_id"]) for row in rows]

    base = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    client = get_http_pool()
    if not base or not key or client is None:
        return []
    response = await client.get(
        f"{base}/rest/v1/user_profiles",
        headers=_rest_headers(key),
        params={
            "roles": f'cs.{json.dumps(["admin"])}',
            "is_active": "eq.true",
            "select": "user_id",
        },
    )
    if response.status_code >= 400:
        logger.warning(
            "Could not resolve admin recipients: HTTP %s", response.status_code
        )
        return []
    return [str(row["user_id"]) for row in response.json()]


async def list_notifications(
    user_id: str, *, limit: int = 30, unread_only: bool = False
) -> list[dict]:
    """Newest-first notifications for one user (backing the in-app inbox)."""
    limit = max(1, min(int(limit), 100))
    from repositories.direct_db import get_direct_pool

    pool = await get_direct_pool()
    if pool is not None:
        sql = (
            "SELECT id, type, title, message, data, severity, read_at, created_at "
            "FROM notifications WHERE user_id = $1"
        )
        if unread_only:
            sql += " AND read_at IS NULL"
        sql += " ORDER BY created_at DESC LIMIT $2"
        rows = await pool.fetch(sql, user_id, limit)
        return [dict(row) for row in rows]

    base = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    client = get_http_pool()
    if not base or not key or client is None:
        return []
    params = {
        "user_id": f"eq.{user_id}",
        "select": "id,type,title,message,data,severity,read_at,created_at",
        "order": "created_at.desc",
        "limit": str(limit),
    }
    if unread_only:
        params["read_at"] = "is.null"
    response = await client.get(
        f"{base}/rest/v1/notifications", headers=_rest_headers(key), params=params
    )
    if response.status_code >= 400:
        return []
    return response.json()


async def mark_read(user_id: str, notification_id: Optional[str] = None) -> int:
    """Mark one notification (or all of them) as read. Returns the row count."""
    stamp = "timezone('utc', now())"
    from repositories.direct_db import get_direct_pool

    pool = await get_direct_pool()
    if pool is not None:
        if notification_id:
            status = await pool.execute(
                f"UPDATE notifications SET read_at = {stamp} "
                "WHERE id = $1 AND user_id = $2 AND read_at IS NULL",
                notification_id,
                user_id,
            )
        else:
            status = await pool.execute(
                f"UPDATE notifications SET read_at = {stamp} "
                "WHERE user_id = $1 AND read_at IS NULL",
                user_id,
            )
        return int(status.split()[-1]) if status else 0

    base = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    client = get_http_pool()
    if not base or not key or client is None:
        return 0
    params = {"user_id": f"eq.{user_id}", "read_at": "is.null"}
    if notification_id:
        params["id"] = f"eq.{notification_id}"
    response = await client.patch(
        f"{base}/rest/v1/notifications",
        headers={**_rest_headers(key), "Prefer": "return=representation"},
        params=params,
        json={"read_at": _utc_now_iso()},
    )
    if response.status_code >= 400:
        return 0
    return len(response.json())


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()

