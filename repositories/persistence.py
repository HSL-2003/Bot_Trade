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
    lock_state: str = "unlocked"          # unlocked | soft_locked | hard_locked
    lock_reason: Optional[str] = None


# Lock states (persisted, source of truth for trading-lock semantics).
LOCK_UNLOCKED = "unlocked"
LOCK_SOFT = "soft_locked"
LOCK_HARD = "hard_locked"
LOCK_STATES = frozenset({LOCK_UNLOCKED, LOCK_SOFT, LOCK_HARD})


class AccountRepository(Protocol):
    def get(self, account_id: str) -> AccountState: ...
    def save(self, state: AccountState) -> None: ...
    def record_trade_open(self, account_id: str, user_id: Optional[str], trade: dict[str, Any]) -> dict[str, Any]: ...
    def record_trade_close(self, account_id: str, user_id: Optional[str], ticket: int, close_info: dict[str, Any]) -> Optional[dict[str, Any]]: ...
    def get_user_trades(self, account_id: str, user_id: Optional[str] = None, limit: int = 100, period: str = "all") -> list[dict[str, Any]]: ...
    def get_lock_state(self, account_id: str) -> dict[str, Any]: ...
    def set_lock_state(self, account_id: str, lock_state: str, reason: Optional[str] = None) -> None: ...
    def list_accounts(self) -> list[dict[str, Any]]: ...
    def get_all_trades(self, limit: int = 5000) -> list[dict[str, Any]]: ...
    def get_account_detail(self, account_id: str) -> dict[str, Any]: ...
    def set_account_status(self, account_id: str, status: str, reason: Optional[str] = None) -> None: ...
    def update_user_roles(self, user_id: str, roles: list[str]) -> dict[str, Any]: ...
    def revoke_account_sessions(self, account_id: str) -> int: ...


class InMemoryAccountRepository:
    def __init__(self):
        self._states: dict[str, AccountState] = {}
        self._profiles: dict[str, dict[str, Any]] = {}
        self._trades: list[dict[str, Any]] = []
        self._accounts: dict[str, dict[str, Any]] = {}
        self._sessions: dict[str, dict[str, Any]] = {}  # account_id -> list-ish by token key

    @staticmethod
    def _check(account_id: str) -> str:
        account_id = account_id.strip()
        if not account_id:
            raise AccountScopeError("Account scope is required")
        return account_id

    def get(self, account_id: str) -> AccountState:
        account_id = self._check(account_id)
        self._accounts.setdefault(account_id, {
            "id": account_id,
            "owner_user_id": None,
            "name": "Primary account",
            "broker": None,
            "account_number": None,
            "status": "active",
            "is_active": True,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        return self._states.setdefault(account_id, AccountState(account_id))

    def save(self, state: AccountState) -> None:
        account_id = self._check(state.account_id)
        if account_id != state.account_id:
            raise AccountScopeError("State account scope mismatch")
        self._states[account_id] = state

    def profile(self, user_id: str) -> dict[str, Any]:
        p = self._profiles.get(user_id, {"user_id": user_id})
        p.setdefault("roles", ["trader"])
        return dict(p)

    def update_profile(self, user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        profile = self._profiles.setdefault(user_id, {"user_id": user_id, "roles": ["trader"]})
        profile.setdefault("roles", ["trader"])
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

    def get_lock_state(self, account_id: str) -> dict[str, Any]:
        account_id = self._check(account_id)
        state = self.get(account_id)
        return {
            "lock_state": state.lock_state or LOCK_UNLOCKED,
            "lock_reason": state.lock_reason,
            "locked_at": None,
            "unlocked_at": None,
        }

    def set_lock_state(self, account_id: str, lock_state: str, reason: Optional[str] = None) -> None:
        account_id = self._check(account_id)
        if lock_state not in LOCK_STATES:
            raise ValueError(f"Invalid lock_state: {lock_state!r}")
        state = self.get(account_id)
        state.lock_state = lock_state
        state.lock_reason = reason

    # ------------------------------------------------------------------
    # Admin / dashboard helpers (soft-delete, roles, aggregation)
    # ------------------------------------------------------------------
    def list_accounts(self) -> list[dict[str, Any]]:
        accounts = []
        for account_id, acc in self._accounts.items():
            lock = self.get_lock_state(account_id)
            owner_id = acc.get("owner_user_id")
            prof = self.profile(owner_id) if owner_id else {}
            accounts.append({
                "id": account_id,
                "owner_user_id": owner_id,
                "display_name": prof.get("display_name") or (owner_id or ""),
                "roles": list(prof.get("roles") or ["trader"]),
                "status": acc.get("status", "active"),
                "is_active": acc.get("is_active", True),
                "lock_state": lock["lock_state"],
                "lock_reason": lock["lock_reason"],
                "created_at": acc.get("created_at"),
                "updated_at": acc.get("updated_at"),
                "blocked_at": acc.get("blocked_at"),
            })
        return accounts

    def get_all_trades(self, limit: int = 5000) -> list[dict[str, Any]]:
        return list(self._trades)[:limit]

    def get_account_detail(self, account_id: str) -> dict[str, Any]:
        account_id = self._check(account_id)
        acc = self._accounts.get(account_id)
        if acc is None:
            raise AccountScopeError("Trading account does not exist")
        lock = self.get_lock_state(account_id)
        owner_id = acc.get("owner_user_id")
        owner = self.profile(owner_id) if owner_id else {}
        trades = [t for t in self._trades if t.get("account_id") == account_id]
        closed = [t for t in trades if t.get("status") == "closed"]
        wins = [t for t in closed if t.get("profit", 0) > 0]
        losses = [t for t in closed if t.get("profit", 0) < 0]
        return {
            **acc,
            "display_name": owner.get("display_name") or owner_id or account_id,
            "roles": list(owner.get("roles") or ["trader"]),
            "lock_state": lock["lock_state"],
            "lock_reason": lock["lock_reason"],
            "total_trades": len(closed),
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "total_profit": round(sum(t.get("profit", 0) for t in closed), 2),
            "total_volume": round(sum(t.get("quantity", 0) for t in closed), 2),
            "recent_trades": sorted(trades, key=lambda x: x.get("submitted_at", ""), reverse=True)[:20],
        }

    def set_account_status(self, account_id: str, status: str, reason: Optional[str] = None) -> None:
        account_id = self._check(account_id)
        if status not in ("active", "blocked", "suspended", "archived"):
            raise ValueError(f"Invalid account status: {status!r}")
        acc = self._accounts.setdefault(account_id, {"id": account_id, "status": "active", "is_active": True})
        acc["status"] = status
        acc["is_active"] = status == "active"
        acc["updated_at"] = datetime.now(timezone.utc).isoformat()
        if status == "blocked":
            acc["blocked_at"] = datetime.now(timezone.utc).isoformat()
            acc["blocked_reason"] = reason
            acc["unblocked_at"] = None
        elif status == "active":
            acc["blocked_reason"] = None
            acc["unblocked_at"] = datetime.now(timezone.utc).isoformat()

    def update_user_roles(self, user_id: str, roles: list[str]) -> dict[str, Any]:
        clean = sorted({r.strip() for r in roles if r.strip()} or ["trader"])
        profile = self._profiles.setdefault(user_id, {"user_id": user_id, "roles": ["trader"]})
        profile["roles"] = clean
        return {"user_id": user_id, "roles": clean}

    def revoke_account_sessions(self, account_id: str) -> int:
        account_id = self._check(account_id)
        removed = 0
        for token, sess in list(self._sessions.items()):
            if sess.get("account_id") == account_id:
                self._sessions.pop(token, None)
                removed += 1
        return removed