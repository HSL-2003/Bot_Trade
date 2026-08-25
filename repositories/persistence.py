"""Account-scoped persistence contracts and a development implementation."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Protocol


class AccountScopeError(ValueError):
    pass


@dataclass
class AccountState:
    account_id: str
    settings: dict[str, Any] = field(default_factory=dict)
    pending_orders: dict[int, dict[str, Any]] = field(default_factory=dict)
    positions: dict[int, dict[str, Any]] = field(default_factory=dict)


class AccountRepository(Protocol):
    def get(self, account_id: str) -> AccountState: ...
    def save(self, state: AccountState) -> None: ...
    def record_trade_open(self, account_id: str, user_id: Optional[str], trade: dict[str, Any]) -> dict[str, Any]: ...
    def record_trade_close(self, account_id: str, user_id: Optional[str], ticket: int, close_info: dict[str, Any]) -> Optional[dict[str, Any]]: ...
    def get_user_trades(self, account_id: str, user_id: Optional[str] = None, limit: int = 100, period: str = "all") -> list[dict[str, Any]]: ...


class InMemoryAccountRepository:
    def __init__(self):
        self._states: dict[str, AccountState] = {}
        self._profiles: dict[str, dict[str, Any]] = {}
        self._trades: list[dict[str, Any]] = []

    @staticmethod
    def _check(account_id: str) -> str:
        account_id = account_id.strip()
        if not account_id:
            raise AccountScopeError("Account scope is required")
        return account_id

    def get(self, account_id: str) -> AccountState:
        account_id = self._check(account_id)
        return self._states.setdefault(account_id, AccountState(account_id))

    def save(self, state: AccountState) -> None:
        account_id = self._check(state.account_id)
        if account_id != state.account_id:
            raise AccountScopeError("State account scope mismatch")
        self._states[account_id] = state

    def profile(self, user_id: str) -> dict[str, Any]:
        return dict(self._profiles.get(user_id, {"user_id": user_id}))

    def update_profile(self, user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        profile = self._profiles.setdefault(user_id, {"user_id": user_id})
        profile.update(payload)
        return dict(profile)

    def record_trade_open(self, account_id: str, user_id: Optional[str], trade: dict[str, Any]) -> dict[str, Any]:
        account_id = self._check(account_id)
        now_iso = datetime.now(timezone.utc).isoformat()
        row = {
            "account_id": account_id,
            "user_id": user_id,
            "broker_ticket": trade.get("ticket"),
            "symbol": trade.get("symbol", "XAUUSD"),
            "side": trade.get("type", "BUY"),
            "order_type": trade.get("order_type", "MARKET"),
            "quantity": float(trade.get("volume", trade.get("lot_size", 0.01))),
            "entry_price": float(trade.get("open_price", trade.get("price", 0.0))),
            "stop_loss": float(trade.get("sl", trade.get("stop_loss", 0.0))),
            "take_profit": float(trade.get("tp", trade.get("take_profit", 0.0))),
            "close_price": None,
            "profit": 0.0,
            "status": "filled",
            "submitted_at": trade.get("open_time", now_iso),
            "filled_at": trade.get("open_time", now_iso),
            "closed_at": None,
            "metadata": trade.get("metadata", {})
        }
        self._trades.append(row)
        return row

    def record_trade_close(self, account_id: str, user_id: Optional[str], ticket: int, close_info: dict[str, Any]) -> Optional[dict[str, Any]]:
        account_id = self._check(account_id)
        now_iso = datetime.now(timezone.utc).isoformat()
        for row in reversed(self._trades):
            if row.get("account_id") == account_id and row.get("broker_ticket") == ticket:
                row["close_price"] = float(close_info.get("close_price", 0.0))
                row["profit"] = float(close_info.get("profit", 0.0))
                row["status"] = "closed"
                row["closed_at"] = close_info.get("close_time", now_iso)
                return row
        # If not found in existing open records, create a closed record directly
        row = {
            "account_id": account_id,
            "user_id": user_id,
            "broker_ticket": ticket,
            "symbol": close_info.get("symbol", "XAUUSD"),
            "side": close_info.get("type", "BUY"),
            "order_type": "MARKET",
            "quantity": float(close_info.get("volume", 0.01)),
            "entry_price": float(close_info.get("open_price", 0.0)),
            "close_price": float(close_info.get("close_price", 0.0)),
            "profit": float(close_info.get("profit", 0.0)),
            "status": "closed",
            "submitted_at": close_info.get("open_time", now_iso),
            "filled_at": close_info.get("open_time", now_iso),
            "closed_at": close_info.get("close_time", now_iso),
            "metadata": {}
        }
        self._trades.append(row)
        return row

    def get_user_trades(self, account_id: str, user_id: Optional[str] = None, limit: int = 100, period: str = "all") -> list[dict[str, Any]]:
        account_id = self._check(account_id)
        matches = [
            t for t in self._trades
            if t.get("account_id") == account_id and (user_id is None or t.get("user_id") == user_id or t.get("user_id") is None)
        ]
        return sorted(matches, key=lambda x: x.get("submitted_at", ""), reverse=True)[:limit]