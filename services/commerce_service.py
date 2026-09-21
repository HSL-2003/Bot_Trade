"""Phase-A commerce orchestration: two-phase checkout, PayOS webhook, crons.

Separation of duties (master plan section 7.3):
- ``commerce_repository`` owns what one atomic DB unit looks like per backend.
- ``payos_client`` owns gateway wire format.
- THIS module owns business sequencing only: when to touch the gateway, what the
  amount-mismatch policy is, and in which order state transitions happen.

Hard rules encoded here (each traceable to a plan invariant):
- B1  the gateway call NEVER happens inside a database transaction.
- N3  an idempotent retry with an existing order that has no link re-creates the
      link instead of returning a dead order (P6 regression guard).
- P9  webhook verification fails closed; a bad signature never touches the DB.
- A.5 #5  amount mismatch <=1% is auto-accepted and logged; beyond that the order
      is flagged for manual review, never auto-rejected.
- A.5 #7  a webhook arriving after expiry still completes the purchase, flagged
      ``late_payment`` - the customer's money is never "swallowed".
- Docs constraint  ``description`` sent to PayOS is the 9-digit order code, well
      under the 9-character limit for non-linked bank accounts.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Optional

from services import notification_service
from repositories import payos_client
from repositories.commerce_repository import CommerceError, CommerceRepository
from repositories.payos_client import PayOSError

logger = logging.getLogger("commerce.service")

REFUND_WINDOW_DAYS = 7
AMOUNT_MISMATCH_AUTO_ACCEPT = Decimal("0.01")  # <=1% difference passes automatically

# Purchases need a real identity: commerce rows reference auth.users through a
# foreign key, so the fabricated "demo-user" principal is rejected with a clean
# 401 instead of a database error. Real Supabase sessions always carry UUIDs.
def commerce_user_id(principal: Any) -> str:
    """Resolve a session principal to a user id the DB layer can use."""
    import re

    raw = getattr(principal, "user_id", "") or ""
    if re.fullmatch(r"[0-9a-fA-F-]{36}", raw):
        return raw
    raise CommerceError("sign in is required to shop (demo sessions cannot own orders)")

_repo: Optional[CommerceRepository] = None


def repo() -> CommerceRepository:
    """One repository instance per process (it holds no per-request state)."""
    global _repo
    if _repo is None:
        _repo = CommerceRepository()
    return _repo


def _vnd(value: Any) -> int:
    """PayOS takes integer VND amounts; the DB stores numeric(14,2)."""
    return int(Decimal(str(value)).quantize(Decimal("1")))


async def _create_link_for_order(order: dict, items: list[dict]) -> payos_client.PaymentLink:
    """Build a payment link for an order.

    Network call with NO database transaction held (invariant B1). The
    description is the bare order code: PayOS caps that field at 9 characters
    for bank accounts not linked through the gateway. The line-item names are
    the real bot names (An Toan / Trung Binh / Mao Hiem) so the buyer sees
    exactly which tier they are paying for on the checkout page.
    """
    from repositories.commerce_repository import _parse_ts

    lines = [
        {
            # Buyer-facing: show the bot tier name, trimmed to PayOS' item
            # name cap so multi-bot carts stay readable.
            "name": str(item.get("bot_name_snapshot")
                        or item.get("bot_name") or "Bot license")[:25],
            "quantity": int(item.get("qty") or 1),
            "price": _vnd(item.get("unit_price_snapshot")
                          or item.get("unit_price") or 0),
        }
        for item in items
    ]
    expires = _parse_ts(order.get("expires_at"))
    return await payos_client.create_payment_link(
        order_code=int(order["order_code"]),
        amount=_vnd(order["total_amount"]),
        description=str(order["order_code"]),
        expired_at=int(expires.timestamp()) if expires else None,
        items=lines,
    )


async def checkout(user_id: str, cart_id: str, idempotency_key: str) -> dict:
    """Two-phase checkout. Returns ``{"order", "items", "checkout_url", "reused"}``.

    Phase 1 (repository) creates the order and locks the cart in one DB unit.
    Phase 2 calls the gateway with no transaction held, then stores the link.
    A gateway failure releases the cart instead of stranding it.
    """
    repository = repo()

    # Idempotent replay handling BEFORE touching the cart: a second click, a
    # second tab, or a crash between the two phases must all land here.
    existing = await repository.get_order_by_idempotency_key(user_id, idempotency_key)
    if existing and existing.get("status") != "FAILED":
        if existing.get("payos_payment_link_id"):
            return {
                "order": existing,
                "items": await repository.get_order_items(existing["id"]),
                "checkout_url": existing.get("payos_payment_link_url"),
                "reused": True,
            }
        if existing.get("status") in ("PENDING", "EXPIRED"):
            # Crash between the phases (N3/P6): the order exists but has no link.
            # Recreate it instead of returning an order the buyer cannot pay.
            logger.warning(
                "recreating missing PayOS link for order %s", existing["order_code"]
            )
            items = await repository.get_order_items(existing["id"])
            try:
                link = await _create_link_for_order(existing, items)
            except PayOSError as exc:
                # Leave the order PENDING: a later retry recreates the link, and
                # the expire cron eventually frees the cart if the buyer leaves.
                logger.error(
                    "link recreation failed for order %s: %s", existing["order_code"], exc
                )
                raise CommerceError(f"payment gateway error: {exc}") from exc
            await repository.attach_payment_link(
                existing["id"],
                payment_link_id=link.payment_link_id,
                checkout_url=link.checkout_url,
            )
            return {
                "order": existing,
                "items": items,
                "checkout_url": link.checkout_url,
                "reused": True,
            }
        # COMPLETED / REFUND_* / REFUNDED: the purchase already concluded.
        return {
            "order": existing,
            "items": await repository.get_order_items(existing["id"]),
            "checkout_url": existing.get("payos_payment_link_url"),
            "reused": True,
        }

    # Phase 1 - order + locked cart, inside one database unit.
    result = await repository.run_checkout(
        user_id=user_id, cart_id=cart_id, idempotency_key=idempotency_key
    )
    order, items = result["order"], result["items"]

    # Phase 2 - gateway call outside any transaction (invariant B1).
    try:
        link = await _create_link_for_order(order, items)
    except PayOSError as exc:
        await repository.fail_order(order["id"], f"PayOS link failed: {exc}")
        raise CommerceError(f"payment gateway error: {exc}") from exc

    if not await repository.attach_payment_link(
        order["id"],
        payment_link_id=link.payment_link_id,
        checkout_url=link.checkout_url,
    ):
        # Somebody else attached a link first (concurrent retry on a new key).
        logger.warning("link attach raced for order %s", order["order_code"])

    return {
        "order": {**order, "payos_payment_link_id": link.payment_link_id},
        "items": items,
        "checkout_url": link.checkout_url,
        "reused": False,
    }


async def _complete_paid_order(order: dict, *, received_amount: Any) -> dict:
    """Everything that follows the atomic PAID transition.

    ``mark_order_paid`` already guaranteed exactly one caller reached here, so the
    licence grant cannot double-issue on concurrent webhooks. Delivery steps after
    that are best-effort: a failed toast must never roll back a paid order.
    """
    repository = repo()
    granted = await repository.grant_licenses_for_order(order["id"], order["user_id"])

    cart_id = order.get("cart_id")
    if cart_id:
        await repository.set_cart_status(cart_id, "ACTIVE")

    late = bool(order.get("late_payment"))
    await notification_service.notify_user(
        order["user_id"],
        "ORDER_PAID" if not late else "ORDER_PAID_LATE",
        "Mua bot thành công" if not late else "Thanh toán trễ đã được xác nhận",
        "Bot đã nằm trong Thư viện của bạn. Bạn có thể kích hoạt bất cứ lúc nào."
        if not late
        else "Đơn đã hết hạn khi bạn chuyển khoản nhưng tiền đã được xác nhận và bot "
        "vẫn được cộng vào Thư viện.",
        data={"order_id": order["id"], "order_code": order["order_code"], "granted": granted},
    )
    if late:
        await notification_service.notify_all_admins(
            "ORDER_LATE_PAYMENT",
            "Webhook đến sau khi đơn hết hạn",
            f"Order {order['order_code']} được thanh toán sau hạn - đã cấp bot, "
            "cần đối soát ngân hàng.",
            data={"order_id": order["id"], "received_amount": str(received_amount)},
            severity="warning",
        )
    return {"granted": granted, "late_payment": late}


async def handle_payos_webhook(body: Any) -> tuple[int, str]:
    """Process one PayOS callback. Returns the HTTP status and body text.

    Answers 200 for every recognised-but-uninteresting case (already processed,
    flagged for review) so PayOS does not retry pointlessly; only a bad signature
    or an unusable payload gets a 4xx.
    """
    if not payos_client.verify_webhook(body):
        # P9: an unverifiable callback never touches the database.
        logger.warning("PayOS webhook rejected: invalid signature")
        return 400, "Invalid signature"

    info = payos_client.extract_order_info(body)
    if not info:
        return 400, "Unusable payload"

    event_code = info.get("event_code")
    if event_code is not None and event_code != "00":
        # Non-success delivery (cancelled/expired link event): carries no money
        # action, so accept it without touching the database at all.
        logger.info("PayOS non-success event %s for order %s", event_code, info["order_code"])
        return 200, "Non-success event logged"

    order_code = info["order_code"]
    order = await repo().get_order_by_code(order_code)
    if not order:
        # Verified but unknown order. Answer 200, NOT 404: PayOS' confirm-webhook
        # verification ping sends a signed sample payload (orderCode 1234) and
        # treats any non-2xx as "Webhook url invalid", refusing to register the
        # URL at all (observed live 2026-09-18: `data: "Request failed with
        # status code 404"`). A genuinely raced checkout is not lost by accepting
        # here - `payos_polling_orders` re-checks every PENDING order with a link
        # every minute, which is exactly why the fallback exists (plan B.8).
        logger.warning(
            "PayOS webhook for unknown order_code=%s (verification ping or race); "
            "polling fallback will reconcile any real payment",
            order_code,
        )
        return 200, "Unknown order acknowledged"

    total = Decimal(str(order["total_amount"]))
    amount = Decimal(str(info["amount"]))
    if total > 0:
        mismatch = abs(total - amount) / total
    else:
        mismatch = Decimal("1")
    if mismatch > AMOUNT_MISMATCH_AUTO_ACCEPT:
        reason = f"AMOUNT_MISMATCH expected {total} got {amount}"
        await repo().flag_manual_review(order["id"], reason)
        await notification_service.notify_all_admins(
            "ORDER_AMOUNT_MISMATCH",
            "Đơn hàng lệch số tiền - cần đối soát",
            f"Order {order_code}: dự kiến {total} VND, nhận {amount} VND.",
            data={"order_id": order["id"], "expected": str(total), "received": str(amount)},
            severity="warning",
        )
        return 200, "Flagged for review"
    if mismatch > 0:
        logger.warning(
            "amount mismatch within tolerance for order %s (%s vs %s)",
            order_code, amount, total,
        )

    paid = await repo().mark_order_paid(order_code, received_amount=amount)
    if not paid:
        return 200, "Already processed"

    outcome = await _complete_paid_order(paid, received_amount=amount)
    logger.info(
        "order %s paid (granted=%d late=%s)",
        order_code, outcome["granted"], outcome["late_payment"],
    )
    return 200, "Success"


async def expire_pending_orders() -> int:
    """Cron job: expire unpaid orders past their deadline and free their carts."""
    return await repo().expire_stale_orders()


async def poll_pending_orders() -> int:
    """Cron fallback: ask the gateway directly for orders whose webhook never came.

    The checkout flow must not depend solely on a callback arriving, so anything
    still PENDING with a link gets its gateway state checked here (plan B.8).
    """
    repository = repo()
    completed = 0
    for order in await repository.list_orders_for_polling():
        try:
            state = await payos_client.get_payment_link(order["payos_payment_link_id"])
        except PayOSError as exc:
            logger.warning(
                "polling failed for order %s: %s", order["order_code"], exc
            )
            continue
        status = str(state.get("status") or "").upper()
        if status != "PAID":
            continue
        amount = state.get("amountPaid") or state.get("amount") or order["total_amount"]
        paid = await repository.mark_order_paid(
            int(order["order_code"]), received_amount=amount
        )
        if not paid:
            continue  # a webhook won the race
        await _complete_paid_order(paid, received_amount=amount)
        completed += 1
        logger.info("polling completed order %s", order["order_code"])
    return completed


async def request_refund(order_id: str, user_id: str) -> dict:
    """Customer-initiated refund request. Full-order only, 7-day window.

    Rejected when any bot in the order was ever activated: the service has been
    consumed, so the money-back guarantee no longer applies (plan part 11).
    """
    repository = repo()
    order = await repository.get_order(order_id)
    if not order:
        raise CommerceError("order not found")
    if order["user_id"] != user_id:
        raise CommerceError("not your order")

    paid_at = order.get("paid_at")
    if not paid_at:
        raise CommerceError("order has not been paid")
    from repositories.commerce_repository import _parse_ts

    paid_dt = _parse_ts(paid_at)
    from datetime import datetime, timedelta, timezone as tz

    age = datetime.now(tz.utc) - paid_dt
    if age > timedelta(days=REFUND_WINDOW_DAYS):
        raise CommerceError(f"refund window expired ({REFUND_WINDOW_DAYS} days)")

    licenses = await repository.licenses_for_order(order_id)
    activated = [lic for lic in licenses if lic.get("status") != "OWNED_INACTIVE"]
    if activated:
        raise CommerceError(
            "a bot in this order has been activated - refunds are only possible "
            "before activation"
        )

    if order.get("status") == "REFUND_PENDING":
        return {"status": "REFUND_PENDING", "reused": True}
    await repository.set_order_refund_status(order_id, "REFUND_PENDING")
    await notification_service.notify_all_admins(
        "ORDER_REFUND_REQUEST",
        "Yêu cầu hoàn tiền",
        f"Order {order['order_code']} ({_vnd(order['total_amount'])} VND) yêu cầu hoàn tiền.",
        data={"order_id": order_id},
        severity="warning",
    )
    return {"status": "REFUND_PENDING", "reused": False}


async def approve_refund(order_id: str, admin_id: str) -> dict:
    """Admin approves the pending refund: PayOS returns the money, licences die.

    Runs the gateway call OUTSIDE any database transaction (invariant B1): the
    refund endpoint is as capable of pool exhaustion as checkout is.
    """
    repository = repo()
    order = await repository.get_order(order_id)
    if not order:
        raise CommerceError("order not found")
    if order.get("status") != "REFUND_PENDING":
        raise CommerceError(f"order is not awaiting refund (status={order.get('status')})")

    try:
        result = await payos_client.refund(
            payment_link_id=order.get("payos_payment_link_id") or "",
            amount=_vnd(order["total_amount"]),
            description=str(order["order_code"]),
        )
    except PayOSError as exc:
        logger.error("PayOS refund failed for order %s: %s", order["order_code"], exc)
        await notification_service.notify_all_admins(
            "ORDER_REFUND_FAILED",
            "Hoàn tiền PayOS thất bại",
            f"Order {order['order_code']}: {exc}",
            data={"order_id": order_id},
            severity="critical",
        )
        raise CommerceError("refund gateway error") from exc

    if not result.success:
        await repository.set_order_refund_status(order_id, "REFUND_PENDING")
        raise CommerceError("gateway did not confirm the refund")

    await repository.set_order_refund_status(order_id, "REFUNDED", provider_txn_id=result.txn_id)
    refunded = await repository.refund_licenses_for_order(order_id)
    await notification_service.notify_user(
        order["user_id"],
        "ORDER_REFUNDED",
        "Hoàn tiền thành công",
        f"Đơn {order['order_code']} đã được hoàn {_vnd(order['total_amount'])} VND.",
        data={"order_id": order_id, "refunded_licenses": refunded},
    )
    logger.info(
        "admin %s refunded order %s (%d licences)", admin_id, order["order_code"], refunded
    )
    return {"status": "REFUNDED", "refunded_licenses": refunded}