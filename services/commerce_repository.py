"""Commerce data access for Phase A (cart -> checkout -> PayOS -> license).

Two backends behind one interface:

- **Direct asyncpg pool** (``DATABASE_POOL_URL``): real transactions with
  ``SELECT ... FOR UPDATE``, so two concurrent checkouts can never create two
  orders for the same cart.
- **Supabase REST** (fallback): the same operations as separate requests guarded
  by an advisory lock. Correct for the current single-worker deployment, NOT safe
  with multiple workers - see PROJECT_MASTER_PLAN.md section 7.3.

Why checkout orchestration lives here and not in the service: "what counts as one
atomic unit" is exactly the thing that differs between backends. Encapsulating it
keeps ``commerce_service`` backend-agnostic.

Money note: all amounts are ``numeric`` in Postgres and ``Decimal`` in Python.
Never mix in floats for arithmetic on prices.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from services.async_http import get_http_pool

logger = logging.getLogger("commerce")

# Order statuses that mean "money has not arrived yet" and can still be paid.
OPEN_ORDER_STATUSES = ("PENDING", "EXPIRED")


class CommerceError(RuntimeError):
    """A commerce operation failed in a way the caller must translate to HTTP."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _rest_base() -> tuple[str, str]:
    base = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    return base, key


def _rest_headers(key: str, *, represent: bool = False) -> dict:
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if represent:
        headers["Prefer"] = "return=representation"
    return headers


async def _pool():
    """Direct pool, or None when the deployment is not configured for it."""
    from services.direct_db import get_direct_pool

    return await get_direct_pool()


async def _rest_request(
    method: str,
    table: str,
    *,
    params: Optional[dict] = None,
    json_body: Any = None,
    represent: bool = False,
    single: bool = False,
) -> Any:
    base, key = _rest_base()
    if not base or not key:
        raise CommerceError("Supabase REST is not configured (SUPABASE_URL / SERVICE_ROLE_KEY)")
    client = get_http_pool()
    if client is None:
        raise CommerceError("commerce REST call requires a running event loop")
    headers = _rest_headers(key, represent=represent)
    if single:
        headers["Accept"] = "application/vnd.pgrst.object+json"
    response = await client.request(
        method,
        f"{base}/rest/v1/{table}",
        headers=headers,
        params=params,
        json=json_body,
    )
    if response.status_code >= 400:
        # 406 is what PostgREST returns for `single` when zero rows matched.
        if single and response.status_code == 406:
            return None
        raise CommerceError(
            f"{method} {table} HTTP {response.status_code}: {response.text[:300]}"
        )
    if not response.content:
        return None
    try:
        return response.json()
    except Exception:
        return None


class CommerceRepository:
    """Unified commerce data access. Picks the backend per call."""

    # ------------------------------------------------------------------ bots
    async def get_bot(self, bot_id: str) -> Optional[dict]:
        pool = await _pool()
        if pool is not None:
            row = await pool.fetchrow(
                "select b.*, bt.name as tier_name, bt.risk_level as tier_risk, "
                "       bt.min_capital as tier_min_capital "
                "  from bots b left join bot_types bt on bt.id = b.tier_id "
                " where b.id = $1",
                bot_id,
            )
            return dict(row) if row else None
        rows = await _rest_request(
            "GET", "bots", params={"id": f"eq.{bot_id}", "select": "*", "limit": "1"}
        )
        return rows[0] if rows else None

    async def list_active_bots(self) -> list[dict]:
        """The sellable catalogue, with its tier metadata for the pricing UI."""
        pool = await _pool()
        if pool is not None:
            rows = await pool.fetch(
                "select b.*, bt.name as tier_name, bt.risk_level as tier_risk, "
                "       bt.min_capital as tier_min_capital "
                "  from bots b left join bot_types bt on bt.id = b.tier_id "
                " where b.is_active = true order by b.sort_order, b.name"
            )
            return [dict(r) for r in rows]
        rows = await _rest_request(
            "GET",
            "bots",
            params={
                "is_active": "eq.true",
                "select": "*",
                "order": "sort_order.asc,name.asc",
            },
        )
        return rows or []

    # ------------------------------------------------------------------ cart
    async def get_active_cart(self, user_id: str) -> Optional[dict]:
        pool = await _pool()
        if pool is not None:
            row = await pool.fetchrow(
                "select * from carts where user_id = $1 and status = 'ACTIVE' limit 1",
                user_id,
            )
            return dict(row) if row else None
        rows = await _rest_request(
            "GET",
            "carts",
            params={
                "user_id": f"eq.{user_id}",
                "status": "eq.ACTIVE",
                "select": "*",
                "limit": "1",
            },
        )
        return rows[0] if rows else None

    async def get_or_create_active_cart(self, user_id: str) -> dict:
        """Return this user's single ACTIVE cart, creating it when absent.

        The partial unique index ``uq_carts_one_active_per_user`` makes a double
        create impossible, so a race resolves to a constraint violation rather
        than two carts; we then re-read and return the winner.
        """
        existing = await self.get_active_cart(user_id)
        if existing:
            return existing

        pool = await _pool()
        if pool is not None:
            try:
                row = await pool.fetchrow(
                    "insert into carts (user_id, status) values ($1, 'ACTIVE') returning *",
                    user_id,
                )
                return dict(row)
            except Exception:
                # Lost the race, or the cart already existed: read it back.
                again = await self.get_active_cart(user_id)
                if again:
                    return again
                raise

        created = await _rest_request(
            "POST",
            "carts",
            json_body={"user_id": user_id, "status": "ACTIVE"},
            represent=True,
        )
        if created:
            return created[0]
        again = await self.get_active_cart(user_id)
        if again:
            return again
        raise CommerceError("could not create or find an ACTIVE cart")

    async def get_cart(self, cart_id: str) -> Optional[dict]:
        pool = await _pool()
        if pool is not None:
            row = await pool.fetchrow("select * from carts where id = $1", cart_id)
            return dict(row) if row else None
        rows = await _rest_request(
            "GET", "carts", params={"id": f"eq.{cart_id}", "select": "*", "limit": "1"}
        )
        return rows[0] if rows else None

    async def set_cart_status(self, cart_id: str, status: str) -> None:
        if status not in ("ACTIVE", "LOCKED"):
            raise CommerceError(f"invalid cart status {status!r}")
        pool = await _pool()
        if pool is not None:
            await pool.execute(
                "update carts set status = $2, updated_at = timezone('utc', now()) "
                " where id = $1",
                cart_id,
                status,
            )
            return
        await _rest_request(
            "PATCH", "carts", params={"id": f"eq.{cart_id}"}, json_body={"status": status}
        )

    async def list_cart_items(self, cart_id: str) -> list[dict]:
        """Cart lines joined with the bot's CURRENT price.

        Price is deliberately not stored on cart_items: it is read live until
        checkout freezes it into order_items (master plan invariant B3).
        """
        pool = await _pool()
        if pool is not None:
            rows = await pool.fetch(
                "select ci.id, ci.bot_id, ci.qty, ci.added_at, "
                "       b.name as bot_name, b.price, b.currency, b.is_active, "
                "       b.max_owned_per_user "
                "  from cart_items ci join bots b on b.id = ci.bot_id "
                " where ci.cart_id = $1 order by ci.added_at",
                cart_id,
            )
            return [dict(r) for r in rows]

        items = (
            await _rest_request(
                "GET",
                "cart_items",
                params={
                    "cart_id": f"eq.{cart_id}",
                    "select": "id,bot_id,qty,added_at",
                    "order": "added_at.asc",
                },
            )
            or []
        )
        if not items:
            return []
        bot_ids = sorted({item["bot_id"] for item in items})
        bots = (
            await _rest_request(
                "GET",
                "bots",
                params={
                    "id": f"in.({','.join(bot_ids)})",
                    "select": "id,name,price,currency,is_active,max_owned_per_user",
                },
            )
            or []
        )
        by_id = {bot["id"]: bot for bot in bots}
        enriched = []
        for item in items:
            bot = by_id.get(item["bot_id"]) or {}
            enriched.append(
                {
                    **item,
                    "bot_name": bot.get("name"),
                    "price": bot.get("price"),
                    "currency": bot.get("currency"),
                    "is_active": bot.get("is_active"),
                    "max_owned_per_user": bot.get("max_owned_per_user"),
                }
            )
        return enriched

    async def set_cart_item(self, cart_id: str, bot_id: str, qty: int) -> None:
        """Insert or update a cart line. ``qty <= 0`` removes it."""
        pool = await _pool()
        if pool is not None:
            if qty <= 0:
                await pool.execute(
                    "delete from cart_items where cart_id = $1 and bot_id = $2",
                    cart_id,
                    bot_id,
                )
                return
            await pool.execute(
                "insert into cart_items (cart_id, bot_id, qty) values ($1, $2, $3) "
                "on conflict (cart_id, bot_id) do update set qty = excluded.qty",
                cart_id,
                bot_id,
                qty,
            )
            return

        if qty <= 0:
            await _rest_request(
                "DELETE",
                "cart_items",
                params={"cart_id": f"eq.{cart_id}", "bot_id": f"eq.{bot_id}"},
            )
            return
        # PostgREST upsert keyed on the (cart_id, bot_id) unique pair.
        await _rest_request(
            "POST",
            "cart_items",
            params={"on_conflict": "cart_id,bot_id"},
            json_body={"cart_id": cart_id, "bot_id": bot_id, "qty": qty},
            represent=True,
        )

    async def clear_cart_items(self, cart_id: str) -> None:
        pool = await _pool()
        if pool is not None:
            await pool.execute("delete from cart_items where cart_id = $1", cart_id)
            return
        await _rest_request("DELETE", "cart_items", params={"cart_id": f"eq.{cart_id}"})

    # -------------------------------------------------------------- licenses
    async def count_usable_licenses(self, user_id: str, bot_id: str) -> int:
        """How many of this bot the user holds and could still use.

        Counts OWNED_INACTIVE + ACTIVE only. REFUNDED and PERMANENTLY_STOPPED are
        excluded, otherwise a refunded or replaced bot would wrongly block a
        re-purchase (master plan case A.5 #6).
        """
        pool = await _pool()
        if pool is not None:
            value = await pool.fetchval(
                "select count(*) from user_bot_licenses "
                " where user_id = $1 and bot_id = $2 "
                "   and status in ('OWNED_INACTIVE', 'ACTIVE')",
                user_id,
                bot_id,
            )
            return int(value or 0)
        rows = await _rest_request(
            "GET",
            "user_bot_licenses",
            params={
                "user_id": f"eq.{user_id}",
                "bot_id": f"eq.{bot_id}",
                "status": "in.(OWNED_INACTIVE,ACTIVE)",
                "select": "id",
            },
        )
        return len(rows or [])

    async def list_licenses(self, user_id: str) -> list[dict]:
        """The customer's library, newest first."""
        pool = await _pool()
        if pool is not None:
            rows = await pool.fetch(
                "select l.*, b.name as bot_name, b.slug as bot_slug, "
                "       b.description as bot_description, "
                "       bt.name as tier_name, bt.risk_level as tier_risk "
                "  from user_bot_licenses l "
                "  join bots b on b.id = l.bot_id "
                "  left join bot_types bt on bt.id = b.tier_id "
                " where l.user_id = $1 order by l.purchased_at desc",
                user_id,
            )
            return [dict(r) for r in rows]
        return (
            await _rest_request(
                "GET",
                "user_bot_licenses",
                params={
                    "user_id": f"eq.{user_id}",
                    "select": "*",
                    "order": "purchased_at.desc",
                },
            )
            or []
        )

    # ---------------------------------------------------------------- orders
    async def next_order_code(self) -> int:
        """Race-free order code from the sequence, clamped to PayOS' 9 digits."""
        from services.payos_client import generate_order_code

        pool = await _pool()
        if pool is not None:
            value = await pool.fetchval("select nextval('order_code_seq')")
            return generate_order_code(int(value))
        # No sequence without a pool: derive from the clock. Uniqueness still
        # holds because order_code is UNIQUE and the caller retries on conflict.
        return generate_order_code(int(_now().timestamp() * 1000))

    async def get_order_by_idempotency_key(self, user_id: str, key: str) -> Optional[dict]:
        """Idempotency is scoped to the user (invariant B4): a key from another
        user must never resolve to their order."""
        pool = await _pool()
        if pool is not None:
            row = await pool.fetchrow(
                "select * from orders where user_id = $1 and idempotency_key = $2 limit 1",
                user_id,
                key,
            )
            return dict(row) if row else None
        rows = await _rest_request(
            "GET",
            "orders",
            params={
                "user_id": f"eq.{user_id}",
                "idempotency_key": f"eq.{key}",
                "select": "*",
                "limit": "1",
            },
        )
        return rows[0] if rows else None

    async def get_order(self, order_id: str) -> Optional[dict]:
        pool = await _pool()
        if pool is not None:
            row = await pool.fetchrow("select * from orders where id = $1", order_id)
            return dict(row) if row else None
        rows = await _rest_request(
            "GET", "orders", params={"id": f"eq.{order_id}", "select": "*", "limit": "1"}
        )
        return rows[0] if rows else None

    async def get_order_by_code(self, order_code: int) -> Optional[dict]:
        pool = await _pool()
        if pool is not None:
            row = await pool.fetchrow(
                "select * from orders where order_code = $1", int(order_code)
            )
            return dict(row) if row else None
        rows = await _rest_request(
            "GET",
            "orders",
            params={"order_code": f"eq.{int(order_code)}", "select": "*", "limit": "1"},
        )
        return rows[0] if rows else None

    async def get_order_items(self, order_id: str) -> list[dict]:
        pool = await _pool()
        if pool is not None:
            rows = await pool.fetch(
                "select * from order_items where order_id = $1", order_id
            )
            return [dict(r) for r in rows]
        return (
            await _rest_request(
                "GET", "order_items", params={"order_id": f"eq.{order_id}", "select": "*"}
            )
            or []
        )

    # --------------------------------------------------- atomic checkout core
    async def run_checkout(
        self, *, user_id: str, cart_id: str, idempotency_key: str, ttl_minutes: int = 15
    ) -> dict:
        """TX1 of the two-phase checkout: create the order and lock the cart.

        Contains NO network call. Creating the PayOS link in here would hold a
        pooled connection plus a row lock for the 5-10s the gateway takes and
        exhaust the pool under concurrent checkouts (invariant B1); the caller
        creates the link afterwards, outside this method.

        Returns ``{"order": {...}, "items": [...], "reused": bool}``.
        """
        from services.db_locks import advisory_lock

        expires_at = _now() + timedelta(minutes=ttl_minutes)
        pool = await _pool()

        if pool is not None:
            async with advisory_lock(f"checkout:{cart_id}", timeout=10.0):
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        return await self._checkout_sql(
                            conn, user_id, cart_id, idempotency_key, expires_at
                        )

        # REST backend: the advisory lock still serialises within one worker, but
        # there is no real transaction (see module docstring / plan section 7.3).
        async with advisory_lock(f"checkout:{cart_id}", timeout=10.0):
            return await self._checkout_rest(user_id, cart_id, idempotency_key, expires_at)

    async def _checkout_sql(
        self, conn, user_id: str, cart_id: str, idempotency_key: str, expires_at
    ) -> dict:
        pending = await conn.fetchrow(
            "select * from orders where cart_id = $1 and status = 'PENDING' "
            " order by created_at desc limit 1 for update",
            cart_id,
        )
        if pending:
            row = dict(pending)
            row_expires = row.get("expires_at")
            if row_expires and row_expires > _now():
                return {
                    "order": row,
                    "items": await self._items_sql(conn, row["id"]),
                    "reused": True,
                }
            # Expired while the customer was away: expire it AND unlock the cart
            # now instead of waiting up to 5 minutes for the cron.
            await conn.execute(
                "update orders set status = 'EXPIRED', updated_at = timezone('utc', now()) "
                " where id = $1 and status = 'PENDING'",
                row["id"],
            )
            await conn.execute(
                "update carts set status = 'ACTIVE', updated_at = timezone('utc', now()) "
                " where id = $1 and status = 'LOCKED'",
                cart_id,
            )

        cart = await conn.fetchrow("select * from carts where id = $1 for update", cart_id)
        if not cart:
            raise CommerceError("cart not found")
        if cart["status"] != "ACTIVE":
            raise CommerceError("cart is not active")

        lines = await conn.fetch(
            "select ci.bot_id, ci.qty, b.name, b.price, b.is_active, b.max_owned_per_user "
            "  from cart_items ci join bots b on b.id = ci.bot_id "
            " where ci.cart_id = $1",
            cart_id,
        )
        if not lines:
            raise CommerceError("cart is empty")

        total = Decimal("0")
        snapshot: list[dict] = []
        for line in lines:
            if not line["is_active"]:
                raise CommerceError(f"{line['name']} is no longer for sale")
            owned = await conn.fetchval(
                "select count(*) from user_bot_licenses "
                " where user_id = $1 and bot_id = $2 "
                "   and status in ('OWNED_INACTIVE', 'ACTIVE')",
                user_id,
                line["bot_id"],
            )
            if int(owned or 0) >= int(line["max_owned_per_user"] or 1):
                raise CommerceError(f"you already own {line['name']} and have not used it up")
            price = Decimal(str(line["price"]))
            total += price * int(line["qty"])
            snapshot.append(
                {
                    "bot_id": line["bot_id"],
                    "qty": int(line["qty"]),
                    "unit_price": price,
                    "bot_name": line["name"],
                }
            )

        order_code = await self.next_order_code()
        order = await conn.fetchrow(
            "insert into orders (order_code, user_id, cart_id, total_amount, "
            "                    idempotency_key, status, expires_at) "
            "values ($1, $2, $3, $4, $5, 'PENDING', $6) returning *",
            order_code,
            user_id,
            cart_id,
            total,
            idempotency_key,
            expires_at,
        )
        for item in snapshot:
            await conn.execute(
                "insert into order_items (order_id, bot_id, qty, unit_price_snapshot, "
                "                         bot_name_snapshot) values ($1, $2, $3, $4, $5)",
                order["id"],
                item["bot_id"],
                item["qty"],
                item["unit_price"],
                item["bot_name"],
            )
        await conn.execute(
            "update carts set status = 'LOCKED', updated_at = timezone('utc', now()) "
            " where id = $1",
            cart_id,
        )
        return {
            "order": dict(order),
            "items": await self._items_sql(conn, order["id"]),
            "reused": False,
        }

    @staticmethod
    async def _items_sql(conn, order_id: str) -> list[dict]:
        rows = await conn.fetch("select * from order_items where order_id = $1", order_id)
        return [dict(r) for r in rows]

    async def _checkout_rest(
        self, user_id: str, cart_id: str, idempotency_key: str, expires_at
    ) -> dict:
        """REST equivalent of ``_checkout_sql``.

        No real transaction is available here. Correctness rests on the advisory
        lock held by ``run_checkout`` (single-worker only), so every step checks
        for the failure it could otherwise cause and aborts loudly.
        """
        pendings = await _rest_request(
            "GET",
            "orders",
            params={
                "cart_id": f"eq.{cart_id}",
                "status": "eq.PENDING",
                "select": "*",
                "order": "created_at.desc",
                "limit": "1",
            },
        ) or []
        if pendings:
            row = pendings[0]
            raw_expiry = row.get("expires_at")
            expiry = _parse_ts(raw_expiry)
            if expiry and expiry > _now():
                return {"order": row, "items": await self.get_order_items(row["id"]), "reused": True}
            await self._patch_order(row["id"], {"status": "EXPIRED"}, only_if_status="PENDING")
            await self.set_cart_status(cart_id, "ACTIVE")

        cart = await self.get_cart(cart_id)
        if not cart:
            raise CommerceError("cart not found")
        if cart["status"] != "ACTIVE":
            raise CommerceError("cart is not active")

        lines = await self.list_cart_items(cart_id)
        if not lines:
            raise CommerceError("cart is empty")

        total = Decimal("0")
        snapshot: list[dict] = []
        for line in lines:
            if not line.get("is_active"):
                raise CommerceError(f"{line.get('bot_name')} is no longer for sale")
            owned = await self.count_usable_licenses(user_id, line["bot_id"])
            if owned >= int(line.get("max_owned_per_user") or 1):
                raise CommerceError(
                    f"you already own {line.get('bot_name')} and have not used it up"
                )
            price = Decimal(str(line["price"]))
            total += price * int(line["qty"])
            snapshot.append(
                {
                    "bot_id": line["bot_id"],
                    "qty": int(line["qty"]),
                    "unit_price": price,
                    "bot_name": line.get("bot_name"),
                }
            )

        order_code = await self.next_order_code()
        try:
            created = await _rest_request(
                "POST",
                "orders",
                json_body={
                    "order_code": order_code,
                    "user_id": user_id,
                    "cart_id": cart_id,
                    "total_amount": str(total),
                    "idempotency_key": idempotency_key,
                    "status": "PENDING",
                    "expires_at": expires_at.isoformat(),
                },
                represent=True,
            )
        except CommerceError as exc:
            # A unique violation here means the key was already used (concurrent
            # double-submit). Surface the existing order instead of failing.
            existing = await self.get_order_by_idempotency_key(user_id, idempotency_key)
            if existing:
                return {
                    "order": existing,
                    "items": await self.get_order_items(existing["id"]),
                    "reused": True,
                }
            raise CommerceError(f"could not create order: {exc}") from exc

        order = created[0]
        for item in snapshot:
            await _rest_request(
                "POST",
                "order_items",
                json_body={
                    "order_id": order["id"],
                    "bot_id": item["bot_id"],
                    "qty": item["qty"],
                    "unit_price_snapshot": str(item["unit_price"]),
                    "bot_name_snapshot": item["bot_name"],
                },
            )
        await self.set_cart_status(cart_id, "LOCKED")
        return {
            "order": order,
            "items": await self.get_order_items(order["id"]),
            "reused": False,
        }