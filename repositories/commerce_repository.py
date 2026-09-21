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


def _parse_ts(value: Any) -> Optional[datetime]:
    """Parse a timestamptz from asyncpg or PostgREST into an aware UTC datetime.

    Both backends return timestamps differently (``datetime`` vs ISO string, with
    or without offset), and comparing a naive datetime to an aware one raises
    ``TypeError`` at runtime. Normalising here keeps every caller total.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


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
    from repositories.direct_db import get_direct_pool

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
        from repositories.payos_client import generate_order_code

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
        from repositories.db_locks import advisory_lock

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

    # ------------------------------------------------- post-checkout operations
    async def _patch_order(
        self, order_id: str, fields: dict, *, only_if_status: Optional[str] = None
    ) -> bool:
        """Update order columns, optionally guarded by the current status.

        The guard makes the write a compare-and-swap: when ``only_if_status`` is
        given and the row already moved on, zero rows match and this returns
        False, so the caller treats "someone else got there first" as a normal
        outcome instead of an error.

        ``fields`` keys are internal constants (never user input), so they are
        interpolated as column names; values are always parameterised.
        """
        keys = list(fields)
        if not keys:
            return True
        pool = await _pool()
        if pool is not None:
            assignments = ", ".join(f"{key} = ${i + 2}" for i, key in enumerate(keys))
            sql = (
                f"update orders set {assignments}, "
                "updated_at = timezone('utc', now()) where id = $1"
            )
            args: list[Any] = [order_id, *[fields[key] for key in keys]]
            if only_if_status is not None:
                args.append(only_if_status)
                sql += f" and status = ${len(args)}"
            result = await pool.execute(sql, *args)
            return result.endswith(" 1")

        params = {"id": f"eq.{order_id}"}
        if only_if_status is not None:
            params["status"] = f"eq.{only_if_status}"
        body = {**fields, "updated_at": _now().isoformat()}
        changed = await _rest_request(
            "PATCH", "orders", params=params, json_body=body, represent=True
        )
        return bool(changed)

    async def attach_payment_link(
        self, order_id: str, *, payment_link_id: str, checkout_url: Optional[str] = None
    ) -> bool:
        """Phase-2 write: store the gateway link on an order that already exists."""
        return await self._patch_order(
            order_id,
            {
                "payos_payment_link_id": payment_link_id,
                "payos_payment_link_url": checkout_url,
            },
        )

    async def mark_order_paid(
        self, order_code: int, *, received_amount: Any
    ) -> Optional[dict]:
        """Atomically move an order to PAID; None when it was already handled.

        Accepts both ``PENDING`` and ``EXPIRED`` as source states: a customer who
        transfers late must still receive what they paid for (case A.5 #7), flagged
        with ``late_payment``. This single compare-and-swap is what makes two
        simultaneous PayOS webhooks credit the licence exactly once.
        """
        pool = await _pool()
        if pool is not None:
            row = await pool.fetchrow(
                """
                update orders
                   set status = 'PAID',
                       paid_at = timezone('utc', now()),
                       received_amount = $2,
                       late_payment = (status = 'EXPIRED'),
                       manual_review_reason = null,
                       updated_at = timezone('utc', now())
                 where order_code = $1 and status = any($3::text[])
                 returning *
                """,
                order_code,
                received_amount,
                list(OPEN_ORDER_STATUSES),
            )
            return dict(row) if row else None

        current = await self.get_order_by_code(order_code)
        if not current or current.get("status") not in OPEN_ORDER_STATUSES:
            return None
        was_late = current.get("status") == "EXPIRED"
        changed = await self._patch_order(
            current["id"],
            {
                "status": "PAID",
                "paid_at": _now().isoformat(),
                "received_amount": str(received_amount),
                "late_payment": was_late,
            },
            only_if_status=current.get("status"),
        )
        if not changed:
            return None
        current.update({"status": "PAID", "late_payment": was_late})
        return current

    async def grant_licenses_for_order(self, order_id: str, user_id: str) -> int:
        """Give the buyer one ``OWNED_INACTIVE`` licence per paid order item.

        The caller guarantees single execution: only the webhook that won
        ``mark_order_paid`` reaches here. The existence check keeps a manual
        re-run (support tooling) from double-granting.
        """
        items = await self.get_order_items(order_id)
        pool = await _pool()
        granted = 0
        for item in items:
            if pool is not None:
                exists = await pool.fetchval(
                    "select 1 from user_bot_licenses "
                    " where order_id = $1 and bot_id = $2 limit 1",
                    order_id,
                    item["bot_id"],
                )
                if exists:
                    continue
                await pool.execute(
                    "insert into user_bot_licenses (user_id, bot_id, order_id, status) "
                    "values ($1, $2, $3, 'OWNED_INACTIVE')",
                    user_id,
                    item["bot_id"],
                    order_id,
                )
                granted += 1
            else:
                existing = await _rest_request(
                    "GET",
                    "user_bot_licenses",
                    params={
                        "order_id": f"eq.{order_id}",
                        "bot_id": f"eq.{item['bot_id']}",
                        "select": "id",
                        "limit": "1",
                    },
                ) or []
                if existing:
                    continue
                await _rest_request(
                    "POST",
                    "user_bot_licenses",
                    json_body={
                        "user_id": user_id,
                        "bot_id": item["bot_id"],
                        "order_id": order_id,
                        "status": "OWNED_INACTIVE",
                    },
                )
                granted += 1
        return granted

    async def fail_order(self, order_id: str, reason: str) -> None:
        """Mark an order FAILED and release its cart so the buyer can retry.

        Used when the gateway link could not be created: the order never had a
        chance to be paid, so keeping the cart locked would strand the customer.
        """
        order = await self.get_order(order_id)
        await self._patch_order(
            order_id,
            {"status": "FAILED", "manual_review_reason": reason},
            only_if_status="PENDING",
        )
        if order and order.get("cart_id"):
            await self.set_cart_status(order["cart_id"], "ACTIVE")

    async def flag_manual_review(self, order_id: str, reason: str) -> None:
        """Record why a payment needs a human. Deliberately does NOT change status.

        An amount mismatch must not auto-reject (case A.5 #5): the money is real,
        so the order stays payable while an admin reconciles it.
        """
        await self._patch_order(order_id, {"manual_review_reason": reason})

    async def set_order_refund_status(
        self, order_id: str, status: str, *, provider_txn_id: Optional[str] = None
    ) -> None:
        """Move an order through the refund states (REFUND_PENDING -> REFUNDED)."""
        fields: dict[str, Any] = {"status": status}
        if provider_txn_id is not None:
            fields["refund_provider_txn_id"] = provider_txn_id
        await self._patch_order(order_id, fields)

    async def licenses_for_order(self, order_id: str) -> list[dict]:
        """Licenses bought by one order — the refund eligibility check reads this."""
        pool = await _pool()
        if pool is not None:
            rows = await pool.fetch(
                "select * from user_bot_licenses where order_id = $1", order_id
            )
            return [dict(r) for r in rows]
        return (
            await _rest_request(
                "GET",
                "user_bot_licenses",
                params={"order_id": f"eq.{order_id}", "select": "*"},
            )
            or []
        )

    async def refund_licenses_for_order(self, order_id: str) -> int:
        """Set every license of a refunded order to REFUNDED (full-order policy).

        PayOS refunds per payment link, so a single bot inside a combo cannot be
        refunded alone (plan decision P8) — hence the whole set, no filtering.
        """
        pool = await _pool()
        if pool is not None:
            updated = await pool.execute(
                "update user_bot_licenses set status = 'REFUNDED' where order_id = $1",
                order_id,
            )
            return int(str(updated).rsplit(" ", 1)[-1] or 0)
        rows = await self.licenses_for_order(order_id)
        for row in rows:
            await _rest_request(
                "PATCH",
                "user_bot_licenses",
                params={"id": f"eq.{row['id']}"},
                json_body={"status": "REFUNDED"},
            )
        return len(rows)

    async def list_orders_for_user(self, user_id: str, *, limit: int = 50) -> list[dict]:
        """A buyer's order history, newest first (Thư viện / order tracking)."""
        pool = await _pool()
        if pool is not None:
            rows = await pool.fetch(
                "select * from orders where user_id = $1 "
                " order by created_at desc limit $2",
                user_id,
                limit,
            )
            return [dict(r) for r in rows]
        return (
            await _rest_request(
                "GET",
                "orders",
                params={
                    "user_id": f"eq.{user_id}",
                    "select": "*",
                    "order": "created_at.desc",
                    "limit": str(limit),
                },
            )
            or []
        )

    async def list_orders_for_reconciliation(
        self, *, status: Optional[str] = None, limit: int = 100
    ) -> list[dict]:
        """Admin reconciliation feed: newest orders, optionally filtered by status."""
        pool = await _pool()
        if pool is not None:
            if status:
                rows = await pool.fetch(
                    "select * from orders where status = $1 "
                    " order by created_at desc limit $2",
                    status,
                    limit,
                )
            else:
                rows = await pool.fetch(
                    "select * from orders order by created_at desc limit $1", limit
                )
            return [dict(r) for r in rows]
        params: dict[str, Any] = {"select": "*", "order": "created_at.desc",
                                  "limit": str(limit)}
        if status:
            params["status"] = f"eq.{status}"
        return await _rest_request("GET", "orders", params=params) or []

    async def expire_stale_orders(self, *, limit: int = 100) -> int:
        """Cron body: expire unpaid orders past their deadline and free their carts.

        ``skip locked`` lets a second worker run this concurrently without
        blocking or double-processing the same row.
        """
        pool = await _pool()
        if pool is not None:
            cart_ids = await pool.fetch(
                """
                with stale as (
                    select id from orders
                     where status = 'PENDING'
                       and expires_at is not null
                       and expires_at < timezone('utc', now())
                     order by expires_at
                     limit $1
                     for update skip locked
                )
                update orders o
                   set status = 'EXPIRED', updated_at = timezone('utc', now())
                  from stale s
                 where o.id = s.id
                 returning o.cart_id
                """,
                limit,
            )
            carts = [row["cart_id"] for row in cart_ids if row["cart_id"]]
            if carts:
                await pool.execute(
                    "update carts set status = 'ACTIVE', updated_at = timezone('utc', now()) "
                    " where id = any($1::uuid[]) and status = 'LOCKED'",
                    carts,
                )
            expired = len(cart_ids)
            if expired:
                logger.info("expired %d stale order(s)", expired)
            return expired

        stale = await _rest_request(
            "GET",
            "orders",
            params={
                "status": "eq.PENDING",
                "expires_at": f"lt.{_now().isoformat()}",
                "select": "id,cart_id",
                "order": "expires_at.asc",
                "limit": str(limit),
            },
        ) or []
        for row in stale:
            if await self._patch_order(row["id"], {"status": "EXPIRED"}, only_if_status="PENDING"):
                if row.get("cart_id"):
                    await self.set_cart_status(row["cart_id"], "ACTIVE")
        if stale:
            logger.info("expired %d stale order(s) via REST", len(stale))
        return len(stale)

    async def list_orders_for_polling(
        self, *, window_minutes: int = 30, limit: int = 25
    ) -> list[dict]:
        """Unpaid orders that still have a gateway link, for the polling fallback.

        This is the safety net for a webhook that never arrived: the checkout flow
        must never depend solely on a callback.
        """
        since = (_now() - timedelta(minutes=window_minutes)).isoformat()
        pool = await _pool()
        if pool is not None:
            rows = await pool.fetch(
                "select * from orders "
                " where status = 'PENDING' and payos_payment_link_id is not null "
                "   and created_at >= $1 "
                " order by created_at asc limit $2",
                _now() - timedelta(minutes=window_minutes),
                limit,
            )
            return [dict(r) for r in rows]
        return (
            await _rest_request(
                "GET",
                "orders",
                params={
                    "status": "eq.PENDING",
                    "payos_payment_link_id": "not.is.null",
                    "created_at": f"gte.{since}",
                    "select": "*",
                    "order": "created_at.asc",
                    "limit": str(limit),
                },
            )
            or []
        )