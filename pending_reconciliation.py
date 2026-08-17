"""Pure reconciliation of local pending orders with broker snapshots."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ReconciliationResult:
    active: dict[int, dict[str, Any]]
    disappeared: tuple[int, ...]
    broker_only: tuple[int, ...]


def reconcile_pending_orders(
    local: dict[int, dict[str, Any]], broker: list[dict[str, Any]], *, account_id: str
) -> ReconciliationResult:
    if not account_id:
        raise ValueError("Account scope is required")
    broker_by_ticket = {}
    for order in broker:
        if order.get("account_id") != account_id or "ticket" not in order:
            continue
        broker_by_ticket[int(order["ticket"])] = dict(order)
    local_ids = set(local)
    broker_ids = set(broker_by_ticket)
    return ReconciliationResult(
        active=broker_by_ticket,
        disappeared=tuple(sorted(local_ids - broker_ids)),
        broker_only=tuple(sorted(broker_ids - local_ids)),
    )