"""Signed connector transport primitives.

The actual network server remains an adapter concern. These primitives provide
replay protection and deterministic signing for WebSocket or mTLS transports.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import time
from connectors.connector_protocol import ConnectorMessage, ProtocolError
from connectors.pending_reconciliation import ReconciliationResult, reconcile_pending_orders


@dataclass(frozen=True)
class SignedConnectorMessage:
    message: ConnectorMessage
    timestamp: int
    nonce: str
    signature: str

    def signing_bytes(self) -> bytes:
        return f"{self.timestamp}.{self.nonce}.{self.message.to_json()}".encode()


def sign_message(message: ConnectorMessage, secret: str, *, timestamp: int | None = None, nonce: str) -> SignedConnectorMessage:
    if not secret or not nonce:
        raise ProtocolError("Connector secret and nonce are required")
    timestamp = int(time.time()) if timestamp is None else timestamp
    unsigned = SignedConnectorMessage(message, timestamp, nonce, "")
    signature = hmac.new(secret.encode(), unsigned.signing_bytes(), hashlib.sha256).hexdigest()
    return SignedConnectorMessage(message, timestamp, nonce, signature)


class ReplayGuard:
    def __init__(self, max_age_seconds: int = 60):
        self.max_age_seconds = max_age_seconds
        self._nonces: set[tuple[str, str]] = set()

    def verify(self, signed: SignedConnectorMessage, secret: str) -> None:
        now = int(datetime.now(timezone.utc).timestamp())
        if abs(now - signed.timestamp) > self.max_age_seconds:
            raise ProtocolError("Connector message is outside the replay window")
        key = (signed.message.connector_id, signed.nonce)
        if key in self._nonces:
            raise ProtocolError("Connector nonce was already used")
        expected = sign_message(signed.message, secret, timestamp=signed.timestamp, nonce=signed.nonce).signature
        if not hmac.compare_digest(expected, signed.signature):
            raise ProtocolError("Invalid connector signature")
        self._nonces.add(key)


def reconcile_native_pending_snapshot(
    local: dict[int, dict], signed: SignedConnectorMessage, secret: str, guard: ReplayGuard
) -> ReconciliationResult:
    """Verify a native MT5 pending-order snapshot before reconciliation."""
    guard.verify(signed, secret)
    if signed.message.message_type != "pending_order_snapshot":
        raise ProtocolError("Expected a pending_order_snapshot message")
    orders = signed.message.payload.get("orders")
    if not isinstance(orders, list):
        raise ProtocolError("Snapshot orders must be a list")
    scoped = [dict(order, account_id=signed.message.account_id) for order in orders]
    return reconcile_pending_orders(local, scoped, account_id=signed.message.account_id)