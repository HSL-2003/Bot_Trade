"""PayOS payment-gateway client (Phase A).

PayOS is the Vietnamese gateway used for bot-license purchases and recurring VPS
billing. This module is the ONLY place that talks to PayOS, so the rest of the
app never sees gateway-specific JSON.

Contract notes that matter (learned from the review, section 7.2 of the master
plan):
- Recorded responses are signed with a checksum that arrives in the **response
  body** field, not an HTTP header. ``verify_webhook`` reads the body field.
- ``orderCode`` is capped at ~9 digits -> ``generate_order_code`` clamps.
- The gateway has no native subscription API: recurring billing creates a fresh
  payment link each cycle.
- Every call is retried on 5xx/timeout but NEVER on a 4xx (a 4xx means the
  request itself is wrong; retrying cannot fix it and may duplicate a side
  effect).

Uses the shared ``async_http`` pool so TLS is reused across calls and the event
loop is never blocked.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

from services.async_http import get_http_pool

logger = logging.getLogger("payos")

DEFAULT_BASE_URL = "https://api-merchant.payos.vn"
MAX_ORDER_CODE = 999_999_999  # PayOS rejects longer codes


class PayOSError(RuntimeError):
    """Any failure talking to PayOS that the caller must handle."""

    def __init__(self, message: str, *, status_code: Optional[int] = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


@dataclass(frozen=True)
class PaymentLink:
    payment_link_id: str
    checkout_url: str
    qr_code: Optional[str] = None
    raw: Optional[dict] = None


@dataclass(frozen=True)
class RefundResult:
    success: bool
    txn_id: Optional[str] = None
    raw: Optional[dict] = None


def _config() -> tuple[str, str, str, str]:
    return (
        os.getenv("PAYOS_CLIENT_ID", "").strip(),
        os.getenv("PAYOS_API_KEY", "").strip(),
        os.getenv("PAYOS_CHECKSUM_KEY", "").strip(),
        os.getenv("PAYOS_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
    )


def is_configured() -> bool:
    client_id, api_key, checksum_key, _ = _config()
    return bool(client_id and api_key and checksum_key)


def generate_order_code(sequence_value: int) -> int:
    """Clamp a sequence value into PayOS' orderCode range.

    Using a Postgres sequence (``order_code_seq``) makes generation race-free;
    wrapping keeps it inside the gateway's 9-digit limit instead of failing.
    """
    return 100_000_000 + (int(sequence_value) % 900_000_000)


def compute_signature(data: dict, checksum_key: str) -> str:
    """PayOS checksum: HMAC-SHA256 over the alphabetically sorted key=value pairs.

    ``None`` values are skipped and nested values are dropped, matching the
    gateway's documented algorithm. Kept as a pure function so it is unit
    testable without any network call.
    """
    parts = []
    for key in sorted(data.keys()):
        value = data[key]
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            continue
        parts.append(f"{key}={value}")
    payload = "&".join(parts)
    return hmac.new(
        checksum_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def verify_webhook(body: dict) -> bool:
    """Verify an incoming webhook body.

    Accepts either shape PayOS has used: a top-level ``signature`` field next to
    ``data``, or ``signature`` inside ``data``. Returns False when the key is not
    configured, because an unverifiable webhook must never be trusted.
    """
    _, _, checksum_key, _ = _config()
    if not checksum_key:
        logger.error("PAYOS_CHECKSUM_KEY not set - cannot verify webhooks")
        return False

    if not isinstance(body, dict):
        return False

    signature = body.get("signature")
    data = body.get("data")
    if signature is None and isinstance(data, dict):
        signature = data.get("signature")
    if not signature:
        return False

    if not isinstance(data, dict):
        return False
    candidate = {k: v for k, v in data.items() if k != "signature"}
    expected = compute_signature(candidate, checksum_key)
    ok = hmac.compare_digest(str(signature), expected)
    if not ok:
        # Safe diagnostics (no secrets: keys and signature prefixes only). The
        # live gateway once delivered a real payment whose signature our
        # algorithm rejected (2026-09-21); the polling fallback completed that
        # order, and this log is what lets the NEXT delivery reveal the field
        # that differs (nested content? different key set?).
        logger.warning(
            "PayOS signature mismatch: data_keys=%s nested=%s "
            "provided=%s expected=%s orderCode=%s",
            sorted(candidate.keys()),
            sorted(k for k, v in candidate.items()
                   if isinstance(v, (dict, list))),
            str(signature)[:12],
            expected[:12],
            candidate.get("orderCode"),
        )
    return ok


async def _request(
    method: str,
    path: str,
    payload: Optional[dict] = None,
    *,
    attempts: int = 3,
) -> dict:
    """Authenticated call to PayOS with bounded retry on transient failures.

    Retries on network errors, timeouts and 5xx only. A 4xx is a definitive
    answer and is surfaced immediately - retrying a 4xx risks duplicating a side
    effect (for example creating two payment links for one order).
    """
    import asyncio

    client_id, api_key, checksum_key, base_url = _config()
    if not (client_id and api_key and checksum_key):
        raise PayOSError("PayOS is not configured (PAYOS_CLIENT_ID/API_KEY/CHECKSUM_KEY)")

    client = get_http_pool()
    if client is None:
        raise PayOSError("PayOS call requires a running event loop")

    headers = {
        "x-client-id": client_id,
        "x-api-key": api_key,
        "Content-Type": "application/json",
    }
    url = f"{base_url}{path}"

    last_error: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            response = await client.request(method, url, headers=headers, json=payload)
        except Exception as exc:  # network error / timeout
            last_error = exc
            logger.warning(
                "PayOS %s %s network error (attempt %d): %s", method, path, attempt, exc
            )
            if attempt < attempts:
                await asyncio.sleep(0.5 * attempt)
            continue

        if response.status_code >= 500:
            last_error = PayOSError(
                f"PayOS {method} {path} HTTP {response.status_code}",
                status_code=response.status_code,
                payload=response.text[:500],
            )
            logger.warning("PayOS %s %s 5xx (attempt %d)", method, path, attempt)
            if attempt < attempts:
                await asyncio.sleep(0.5 * attempt)
            continue

        try:
            body = response.json()
        except Exception:
            body = {"raw": response.text[:500]}

        if response.status_code >= 400:
            raise PayOSError(
                f"PayOS {method} {path} HTTP {response.status_code}: {response.text[:300]}",
                status_code=response.status_code,
                payload=body,
            )

        # PayOS wraps results in {code, desc, data}; code "00" means success.
        if isinstance(body, dict) and body.get("code") not in (None, "00", 0):
            raise PayOSError(
                f"PayOS {method} {path} rejected: {body.get('desc') or body.get('code')}",
                payload=body,
            )
        return body

    raise PayOSError(f"PayOS {method} {path} failed after {attempts} attempts: {last_error}")


async def create_payment_link(
    *,
    order_code: int,
    amount: float,
    description: str,
    expired_at: Optional[int] = None,
    return_url: Optional[str] = None,
    cancel_url: Optional[str] = None,
    items: Optional[list[dict]] = None,
    checkout_url: Optional[str] = None,
) -> PaymentLink:
    """Create a payment link. ``expired_at`` is a Unix timestamp in seconds.

    MUST be called outside any database transaction: it is a 5-10s network call,
    and holding a pooled connection plus a row lock that long exhausts the pool
    under concurrent checkouts (master plan invariant B1).
    """
    if order_code > MAX_ORDER_CODE:
        raise PayOSError(f"order_code {order_code} exceeds PayOS' 9-digit limit")

    checksum_key = os.getenv("PAYOS_CHECKSUM_KEY", "").strip()
    sign_data = {
        "amount": int(amount),
        "cancelUrl": cancel_url or os.getenv("PAYOS_CANCEL_URL", ""),
        "description": description,
        "orderCode": int(order_code),
        "returnUrl": return_url or os.getenv("PAYOS_RETURN_URL", ""),
    }
    payload = {**sign_data, "signature": compute_signature(sign_data, checksum_key)}
    if checkout_url is not None:
        payload["checkoutUrl"] = checkout_url
    if expired_at is not None:
        payload["expiredAt"] = int(expired_at)
    if items:
        payload["items"] = items

    body = await _request("POST", "/v2/payment-requests", payload)
    data = body.get("data") or {}
    return PaymentLink(
        payment_link_id=str(data.get("paymentLinkId") or data.get("id") or ""),
        checkout_url=str(data.get("checkoutUrl") or ""),
        qr_code=data.get("qrCode"),
        raw=data,
    )


async def confirm_webhook(webhook_url: str) -> dict:
    """Register/verify the callback URL with PayOS (docs: POST /confirm-webhook).

    Verified 2026-09-18 against docs: response is ``{"code","desc","data"}`` and
    HTTP is 200 even for a bad ``webhookUrl`` (code "20": "webhook not existed").
    On success the response simply echoes the registered URL.
    """
    body = await _request("POST", "/confirm-webhook", {"webhookUrl": webhook_url})
    return body.get("data") or body


async def get_payment_link(payment_link_id: str) -> dict:
    """Fetch current gateway state for a link (used by the polling fallback).

    The checkout flow must not depend solely on the webhook arriving: this is the
    safety net for a dropped callback.
    """
    body = await _request("GET", f"/v2/payment-requests/{payment_link_id}")
    return body.get("data") or {}


async def refund(
    *,
    payment_link_id: str,
    amount: float,
    description: str = "Refund",
) -> RefundResult:
    """Refund an order.

    PayOS refunds per payment link, which is exactly why the business policy is
    full-order-only: a single bot inside a combo cannot be refunded alone.

    NOTE: the exact refund path/signature has changed between PayOS API versions.
    ``PAYOS_REFUND_PATH`` lets you point at the current endpoint without editing
    code; the default below matches the v2 cancel/refund shape. CONFIRM against
    the PayOS docs of the version in use before enabling automatic refunds
    (see master plan manual step M5/M6).
    """
    path_template = os.getenv(
        "PAYOS_REFUND_PATH", "/v2/payment-requests/{payment_link_id}/cancel"
    )
    path = path_template.format(payment_link_id=payment_link_id)
    payload = {"amount": int(amount), "description": description}

    try:
        body = await _request("POST", path, payload)
    except PayOSError as exc:
        logger.error("PayOS refund failed for %s: %s", payment_link_id, exc)
        return RefundResult(success=False, raw={"error": str(exc)})

    data = body.get("data") or {}
    return RefundResult(
        success=True,
        txn_id=str(data.get("transactionId") or data.get("id") or ""),
        raw=data,
    )


def extract_order_info(webhook_body: dict) -> dict:
    """Normalise a webhook (or polling) payload into what the service needs.

    Returns ``{}`` for an unusable payload so the caller can log and still answer
    the gateway with 200 instead of crashing on unexpected JSON.
    """
    if not isinstance(webhook_body, dict):
        return {}
    data = webhook_body.get("data")
    if not isinstance(data, dict):
        data = webhook_body

    code = webhook_body.get("code", data.get("code"))
    order_code = data.get("orderCode")
    amount = data.get("amount")
    if order_code is None or amount is None:
        return {}

    try:
        order_code_int = int(order_code)
        amount_float = float(amount)
    except (TypeError, ValueError):
        return {}

    return {
        "event_code": str(code) if code is not None else None,
        "order_code": order_code_int,
        "amount": amount_float,
        "payment_link_id": data.get("paymentLinkId"),
        "reference": data.get("reference"),
    }
