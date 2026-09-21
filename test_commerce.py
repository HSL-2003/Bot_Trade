"""Unit tests for the Phase-A commerce orchestration (no network, no real DB).

FakeRepository stands in for CommerceRepository so the business rules - webhook
verification, the amount-mismatch policy, idempotent replay handling and the
atomic PAID transition - are exercised in isolation and deterministically.
"""

import asyncio
import os
import sys
import unittest
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services import commerce_service
from repositories import payos_client
from repositories.commerce_repository import CommerceError
from repositories.payos_client import PayOSError

CHECKSUM_KEY = "test-checksum-key"
ORDER_CODE = 200000001
ORDER_ID = "ord-1"
USER_ID = "user-1"
CART_ID = "cart-1"


def signed_webhook(order_code: int = ORDER_CODE, amount: int = 2_000_000,
                   code: str = "00", reference: str | None = None) -> dict:
    """Build a webhook body whose signature PayOS' own algorithm would accept."""
    data = {
        "orderCode": order_code,
        "amount": amount,
        "description": str(order_code),
        "code": code,
        "desc": "success" if code == "00" else "cancelled",
        "reference": reference or "TF23020400000001",
    }
    return {"code": code, "desc": "success", "success": True, "data": data,
            "signature": payos_client.compute_signature(data, CHECKSUM_KEY)}


class FakeRepository:
    """Records calls; reproduces the DB compare-and-swap semantics it needs."""

    def __init__(self, order: dict, items: list[dict] | None = None):
        self.order = order
        self.items = items or [{"bot_id": "bot-1", "qty": 1,
                                "unit_price_snapshot": order.get("total_amount", 0),
                                "bot_name_snapshot": "Bot An Toan"}]
        self.licenses = [{"id": "lic-1", "status": "OWNED_INACTIVE"}]
        self.calls: list[tuple] = []
        self.paid = False
        self.link_created = False

    # --- reads -------------------------------------------------------------
    async def get_order_by_idempotency_key(self, user_id, key):
        self.calls.append(("by_idem", user_id, key))
        return self.order if self.order.get("idempotency_key") == key else None

    async def get_order_by_code(self, code):
        self.calls.append(("by_code", code))
        return self.order if int(self.order["order_code"]) == int(code) else None

    async def get_order(self, order_id):
        self.calls.append(("by_id", order_id))
        return self.order if self.order["id"] == order_id else None

    async def get_order_items(self, order_id):
        self.calls.append(("items", order_id))
        return self.items

    async def licenses_for_order(self, order_id):
        self.calls.append(("licenses", order_id))
        return self.licenses

    # --- writes ------------------------------------------------------------
    async def mark_order_paid(self, order_code, *, received_amount):
        self.calls.append(("mark_paid", order_code, str(received_amount)))
        if self.paid:
            return None
        self.paid = True
        self.order = {**self.order, "status": "PAID",
                      "late_payment": self.order.get("status") == "EXPIRED",
                      "received_amount": received_amount}
        return self.order

    async def grant_licenses_for_order(self, order_id, user_id):
        self.calls.append(("grant", order_id, user_id))
        return len(self.items)

    async def set_cart_status(self, cart_id, status):
        self.calls.append(("cart", cart_id, status))

    async def attach_payment_link(self, order_id, *, payment_link_id, checkout_url):
        self.calls.append(("attach", order_id))
        self.link_created = True
        self.order = {**self.order, "payos_payment_link_id": payment_link_id,
                      "payos_payment_link_url": checkout_url}
        return True

    async def fail_order(self, order_id, reason):
        self.calls.append(("fail", order_id, reason))
        # Mirror the real implementation: a failed order releases its cart.
        order = self.order if self.order["id"] == order_id else None
        if order and order.get("cart_id"):
            await self.set_cart_status(order["cart_id"], "ACTIVE")

    async def flag_manual_review(self, order_id, reason):
        self.calls.append(("review", order_id, reason))

    async def set_order_refund_status(self, order_id, status, *, provider_txn_id=None):
        self.calls.append(("refund_status", order_id, status))
        self.order = {**self.order, "status": status}
        return True

    async def refund_licenses_for_order(self, order_id):
        self.calls.append(("refund_licences", order_id))
        return len(self.licenses)

    async def run_checkout(self, **kwargs):  # pragma: no cover - not under test here
        raise AssertionError("run_checkout must not be called in these tests")


def paid_order(**overrides) -> dict:
    order = {
        "id": ORDER_ID, "order_code": ORDER_CODE, "user_id": USER_ID,
        "cart_id": CART_ID, "total_amount": "2000000", "status": "PENDING",
        "idempotency_key": "idem-1", "late_payment": False,
        "payos_payment_link_id": None, "payos_payment_link_url": None,
        "paid_at": _utcnow_iso(),
    }
    order.update(overrides)
    return order


def _utcnow_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class CommerceServiceTestBase(unittest.TestCase):
    def setUp(self):
        self._old_key = os.environ.get("PAYOS_CHECKSUM_KEY")
        os.environ["PAYOS_CHECKSUM_KEY"] = CHECKSUM_KEY
        self._old_repo = commerce_service._repo

    def tearDown(self):
        if self._old_key is None:
            os.environ.pop("PAYOS_CHECKSUM_KEY", None)
        else:
            os.environ["PAYOS_CHECKSUM_KEY"] = self._old_key
        commerce_service._repo = self._old_repo

    def install(self, fake: FakeRepository):
        commerce_service._repo = fake
        return fake

    def run(self, result=None):
        # Every test body is written as an async inner function.
        return super().run(result)


class _StubNotifications:
    """Hermetic stand-in so tests never touch the real notification transport."""

    def __init__(self):
        self.calls: list[tuple] = []

    async def notify_user(self, user_id, ntype, title, message, data=None, severity="info"):
        self.calls.append(("user", user_id, ntype, severity))
        return "n-1"

    async def notify_all_admins(self, ntype, title, message, data=None, severity="info"):
        self.calls.append(("admins", ntype, severity))
        return 1


class WebhookTests(CommerceServiceTestBase):
    def test_bad_signature_is_rejected_before_any_db_access(self):
        fake = self.install(FakeRepository(paid_order()))
        body = signed_webhook()
        body["signature"] = "0" * 64
        status, text = asyncio.run(commerce_service.handle_payos_webhook(body))
        self.assertEqual((status, text), (400, "Invalid signature"))
        # The database must never be touched by an unverifiable callback (P9).
        self.assertEqual(fake.calls, [])

    def test_webhook_grants_licence_and_unlocks_cart(self):
        fake = self.install(FakeRepository(paid_order()))
        commerce_service.notification_service = _StubNotifications()
        status, text = asyncio.run(
            commerce_service.handle_payos_webhook(signed_webhook())
        )
        self.assertEqual((status, text), (200, "Success"))
        actions = [c[0] for c in fake.calls]
        self.assertIn("mark_paid", actions)
        self.assertIn("grant", actions)
        self.assertIn("cart", actions)
        self.assertEqual(fake.order["status"], "PAID")

    def test_amount_mismatch_beyond_threshold_flags_review(self):
        fake = self.install(FakeRepository(paid_order()))
        stub = _StubNotifications()
        commerce_service.notification_service = stub
        # 5% short of the order total: real money, needs a human (A.5 #5).
        status, text = asyncio.run(
            commerce_service.handle_payos_webhook(signed_webhook(amount=1_900_000))
        )
        self.assertEqual((status, text), (200, "Flagged for review"))
        reviews = [c for c in fake.calls if c[0] == "review"]
        self.assertEqual(len(reviews), 1)
        self.assertIn("AMOUNT_MISMATCH", reviews[0][2])
        self.assertFalse(fake.paid, "a flagged order must not grant licences")

    def test_amount_mismatch_within_tolerance_auto_accepts(self):
        fake = self.install(FakeRepository(paid_order()))
        commerce_service.notification_service = _StubNotifications()
        # 0.01 VND under: far inside the 1% auto-accept policy.
        status, text = asyncio.run(
            commerce_service.handle_payos_webhook(signed_webhook(amount=1_999_999))
        )
        self.assertEqual((status, text), (200, "Success"))
        self.assertTrue(fake.paid)

    def test_late_payment_completes_and_alerts_admins(self):
        fake = self.install(FakeRepository(paid_order(status="EXPIRED")))
        stub = _StubNotifications()
        commerce_service.notification_service = stub
        status, text = asyncio.run(
            commerce_service.handle_payos_webhook(signed_webhook())
        )
        self.assertEqual((status, text), (200, "Success"))
        self.assertTrue(fake.order["late_payment"])
        admin_alerts = [c for c in stub.calls if c[0] == "admins"]
        self.assertTrue(admin_alerts, "late payment must alert the ops team")

    def test_already_processed_is_idempotent(self):
        fake = self.install(FakeRepository(paid_order(status="PAID")))
        fake.paid = True
        commerce_service.notification_service = _StubNotifications()
        status, text = asyncio.run(
            commerce_service.handle_payos_webhook(signed_webhook())
        )
        self.assertEqual((status, text), (200, "Already processed"))
        # The winning webhook already granted; a duplicate must not re-grant.
        self.assertNotIn("grant", [c[0] for c in fake.calls])

    def test_unknown_order_is_acknowledged_not_rejected(self):
        # PayOS' confirm-webhook ping is signed but references a sample order, so
        # answering non-2xx makes PayOS refuse to register the URL entirely
        # (verified live). Unknown orders must be acknowledged; the polling
        # fallback reconciles any genuinely raced payment.
        fake = self.install(FakeRepository(paid_order(order_code=999)))
        commerce_service.notification_service = _StubNotifications()
        status, text = asyncio.run(commerce_service.handle_payos_webhook(signed_webhook()))
        self.assertEqual((status, text), (200, "Unknown order acknowledged"))
        self.assertEqual(fake.calls, [("by_code", ORDER_CODE)])

    def test_non_success_event_is_logged_without_db_writes(self):
        fake = self.install(FakeRepository(paid_order()))
        commerce_service.notification_service = _StubNotifications()
        status, text = asyncio.run(
            commerce_service.handle_payos_webhook(signed_webhook(code="01"))
        )
        self.assertEqual((status, text), (200, "Non-success event logged"))
        self.assertEqual(fake.calls, [])


class _CheckoutRepository(FakeRepository):
    """Adds the phase-1 hook so checkout tests can drive it without a database."""

    def __init__(self, order, items=None):
        super().__init__(order, items)
        self.phase1_result = {"order": order, "items": self.items, "reused": False}

    async def run_checkout(self, **kwargs):
        self.calls.append(("phase1", kwargs["cart_id"]))
        return self.phase1_result


class CheckoutTests(CommerceServiceTestBase):
    def _patch_link(self, fake):
        from repositories.payos_client import PaymentLink

        async def fake_link(order, items):
            fake.calls.append(("gateway", order["order_code"]))
            return PaymentLink(payment_link_id="pl-1",
                               checkout_url="https://pay.test/pl-1")

        commerce_service._create_link_for_order = fake_link
        return PaymentLink

    def _restore_link(self):
        import importlib

        importlib.reload(commerce_service)

    def setUp(self):
        super().setUp()
        commerce_service.notification_service = _StubNotifications()

    def tearDown(self):
        self._restore_link()
        super().tearDown()

    def test_replay_with_existing_link_returns_it_without_gateway_call(self):
        fake = self.install(_CheckoutRepository(paid_order(
            status="PENDING", payos_payment_link_id="pl-1",
            payos_payment_link_url="https://pay.test/pl-1",
        )))
        self._patch_link(fake)
        result = asyncio.run(commerce_service.checkout(USER_ID, CART_ID, "idem-1"))
        self.assertTrue(result["reused"])
        self.assertEqual(result["checkout_url"], "https://pay.test/pl-1")
        self.assertNotIn("gateway", [c[0] for c in fake.calls])

    def test_crash_between_phases_recreates_the_link(self):
        # N3/P6: an order left PENDING with no link must not strand the buyer.
        fake = self.install(_CheckoutRepository(paid_order(status="PENDING")))
        self._patch_link(fake)
        result = asyncio.run(commerce_service.checkout(USER_ID, CART_ID, "idem-1"))
        self.assertTrue(result["reused"])
        self.assertIn("gateway", [c[0] for c in fake.calls])
        self.assertIn("attach", [c[0] for c in fake.calls])
        self.assertEqual(result["checkout_url"], "https://pay.test/pl-1")

    def test_gateway_failure_fails_order_and_releases_cart(self):
        fake = self.install(_CheckoutRepository(paid_order()))
        commerce_service.notification_service = _StubNotifications()

        async def no_existing(user_id, key):
            return None  # first attempt: the order does not exist yet

        fake.get_order_by_idempotency_key = no_existing

        async def boom(order, items):
            raise PayOSError("gateway down")

        commerce_service._create_link_for_order = boom
        with self.assertRaises(CommerceError):
            asyncio.run(commerce_service.checkout(USER_ID, CART_ID, "idem-1"))
        failures = [c for c in fake.calls if c[0] == "fail"]
        self.assertEqual(len(failures), 1, "the order must be marked FAILED")
        self.assertIn(("cart", CART_ID, "ACTIVE"), fake.calls)


class RefundTests(CommerceServiceTestBase):
    def _stub_refund(self, success=True, txn="ref-1"):
        from repositories.payos_client import RefundResult

        async def fake_refund(**kwargs):
            return RefundResult(success=success, txn_id=txn)

        commerce_service.payos_client.refund = fake_refund

    def setUp(self):
        super().setUp()
        commerce_service.notification_service = _StubNotifications()

    def _licences(self, status="OWNED_INACTIVE"):
        return [{"id": "lic-1", "status": status}]

    def test_refund_requires_refund_pending_status(self):
        fake = self.install(FakeRepository(paid_order(status="PAID")))
        asyncio.run(commerce_service.request_refund(ORDER_ID, USER_ID))
        self.assertIn(("refund_status", ORDER_ID, "REFUND_PENDING"),
                      [c for c in fake.calls if c[0] == "refund_status"])

    def test_refund_rejected_after_activation(self):
        fake = FakeRepository(paid_order(status="PAID"))
        fake.licenses = self._licences("ACTIVE")
        self.install(fake)
        with self.assertRaises(CommerceError):
            asyncio.run(commerce_service.request_refund(ORDER_ID, USER_ID))

    def test_admin_refund_marks_order_and_licences(self):
        fake = self.install(FakeRepository(paid_order(status="REFUND_PENDING")))
        fake.licenses = self._licences()
        self._stub_refund(success=True)
        result = asyncio.run(commerce_service.approve_refund(ORDER_ID, "admin-1"))
        self.assertEqual(result["status"], "REFUNDED")
        self.assertIn(("refund_licences", ORDER_ID), fake.calls)
        self.assertEqual(fake.order["status"], "REFUNDED")

    def test_failed_gateway_refund_keeps_request_pending(self):
        fake = self.install(FakeRepository(paid_order(status="REFUND_PENDING")))
        fake.licenses = self._licences()
        self._stub_refund(success=False)
        with self.assertRaises(CommerceError):
            asyncio.run(commerce_service.approve_refund(ORDER_ID, "admin-1"))
        self.assertEqual(fake.order["status"], "REFUND_PENDING")


class NotificationSerializationTests(CommerceServiceTestBase):
    """Regression for the live incident of 2026-09-21: persistence of the paid
    order's notification died on ``TypeError: Object of type UUID is not JSON
    serializable`` because ``data`` carried ``UUID`` values straight from asyncpg.
    The broadcast sink (WebSocket frame) has the same requirement, so the normal
    shape is enforced in one place.
    """

    def test_database_types_survive_notification_payloads(self):
        from decimal import Decimal

        from services.notification_service import _json_safe

        safe = _json_safe({
            "order_id": UUID("12345678-1234-5678-1234-567812345678"),
            "order_code": 200000005,
            "granted": 1,
            "price": Decimal("10000.00"),
        })
        self.assertEqual(safe["order_id"], "12345678-1234-5678-1234-567812345678")
        self.assertEqual(safe["order_code"], 200000005)
        self.assertEqual(safe["granted"], 1)
        self.assertEqual(safe["price"], "10000.00")
        import json

        json.dumps(safe)  # must not raise