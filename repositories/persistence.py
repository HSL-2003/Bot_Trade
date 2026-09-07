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

    def list_user_profiles(self) -> list[dict[str, Any]]:
        return list(self._profiles.values())

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
        wins = [t for t in closed if (t.get("profit") or 0) > 0]
        losses = [t for t in closed if (t.get("profit") or 0) < 0]
        gross_profit = sum(float(t.get("profit") or 0) for t in wins)
        gross_loss = sum(abs(float(t.get("profit") or 0)) for t in losses)
        total_p = round(sum(float(t.get("profit") or 0) for t in closed), 2)
        total_v = round(sum(float(t.get("quantity") or 0) for t in closed), 2)
        win_rate = round((len(wins) / len(closed)) * 100, 1) if closed else 0.0
        profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
        avg_trade = round(total_p / len(closed), 2) if closed else 0.0

        return {
            **acc,
            "display_name": owner.get("display_name") or owner_id or account_id,
            "roles": list(owner.get("roles") or ["trader"]),
            "lock_state": lock["lock_state"],
            "lock_reason": lock["lock_reason"],
            "total_trades": len(closed),
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "break_even_trades": len(closed) - len(wins) - len(losses),
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "average_trade": avg_trade,
            "total_profit": total_p,
            "total_volume": total_v,
            "recent_trades": sorted(trades, key=lambda x: x.get("submitted_at", ""), reverse=True)[:50],
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
        clean_set = {r.strip() for r in roles if r.strip()}
        if "admin" in clean_set and "trader" in clean_set:
            raise AccountScopeError("Một tài khoản không thể đồng thời là Admin và Trader (dùng bot). Vai trò mang tính loại trừ lẫn nhau.")
        clean = ["admin"] if "admin" in clean_set else ["trader"]
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

    def get_account_daily_performance(self, account_id: str) -> list[dict[str, Any]]:
        account_id = self._check(account_id)
        trades = [t for t in self._trades if t.get("account_id") == account_id]
        closed = [t for t in trades if t.get("status") == "closed"]
        daily_map: dict[str, dict[str, Any]] = {}

        for t in closed:
            raw_time = str(t.get("closed_at") or t.get("submitted_at") or t.get("created_at") or "")
            day_str = raw_time[:10] if len(raw_time) >= 10 else "N/A"
            if day_str not in daily_map:
                daily_map[day_str] = {
                    "date": day_str,
                    "total_trades": 0,
                    "winning_trades": 0,
                    "losing_trades": 0,
                    "break_even_trades": 0,
                    "net_profit": 0.0,
                    "gross_profit": 0.0,
                    "gross_loss": 0.0,
                    "total_volume": 0.0,
                    "win_rate": 0.0,
                    "profit_factor": 0.0,
                }
            d = daily_map[day_str]
            d["total_trades"] += 1
            p = float(t.get("profit") or 0.0)
            v = float(t.get("quantity") or 0.0)
            d["net_profit"] += p
            d["total_volume"] += v
            if p > 0:
                d["winning_trades"] += 1
                d["gross_profit"] += p
            elif p < 0:
                d["losing_trades"] += 1
                d["gross_loss"] += abs(p)
            else:
                d["break_even_trades"] += 1

        result = []
        for day_str in sorted(daily_map.keys(), reverse=True):
            d = daily_map[day_str]
            d["net_profit"] = round(d["net_profit"], 2)
            d["gross_profit"] = round(d["gross_profit"], 2)
            d["gross_loss"] = round(d["gross_loss"], 2)
            d["total_volume"] = round(d["total_volume"], 2)
            if d["total_trades"] > 0:
                d["win_rate"] = round((d["winning_trades"] / d["total_trades"]) * 100, 1)
            d["profit_factor"] = round(d["gross_profit"] / d["gross_loss"], 2) if d["gross_loss"] > 0 else (999.0 if d["gross_profit"] > 0 else 0.0)
            result.append(d)
        return result

    def create_user_admin(self, email: str, password: str, display_name: str, role: str = "trader", bot_type_id: Optional[str] = None) -> dict[str, Any]:
        import uuid
        email = (email or "").strip().lower()
        display_name = (display_name or "").strip() or email
        role_clean = role.strip().lower()
        if role_clean not in ("admin", "trader"):
            raise AccountScopeError("Vai trò không hợp lệ. Chỉ chấp nhận 'admin' hoặc 'trader'.")

        user_id = str(uuid.uuid4())
        profile = {
            "user_id": user_id,
            "email": email,
            "display_name": display_name,
            "roles": [role_clean],
            "status": "active",
            "is_active": True,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._profiles[user_id] = profile

        account_id = None
        if role_clean == "trader":
            account_id = f"acct-{user_id}"
            self._accounts[account_id] = {
                "id": account_id,
                "owner_user_id": user_id,
                "name": f"Account {display_name}",
                "bot_type_id": bot_type_id or "308b6e56-d024-4831-950c-e2cefd241c1b",
                "status": "active",
                "is_active": True,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

        return {
            "user_id": user_id,
            "email": email,
            "display_name": display_name,
            "roles": [role_clean],
            "status": "active",
            "account_id": account_id,
        }

    def update_user_profile_admin(self, user_id: str, *, display_name: Optional[str] = None, password: Optional[str] = None, role: Optional[str] = None, status: Optional[str] = None) -> dict[str, Any]:
        user_id = self._check(user_id)
        profile = self._profiles.get(user_id)
        if not profile:
            raise AccountScopeError("User not found")

        if display_name is not None:
            profile["display_name"] = display_name.strip()
        if role is not None:
            role_clean = role.strip().lower()
            if role_clean not in ("admin", "trader"):
                raise AccountScopeError("Một tài khoản không thể đồng thời là Admin và Trader (dùng bot). Vai trò mang tính loại trừ lẫn nhau.")
            profile["roles"] = [role_clean]
        if status is not None:
            status_clean = status.strip().lower()
            profile["status"] = status_clean
            profile["is_active"] = (status_clean == "active")

        if role == "trader":
            acc_id = f"acct-{user_id}"
            if acc_id not in self._accounts:
                self._accounts[acc_id] = {
                    "id": acc_id,
                    "owner_user_id": user_id,
                    "name": f"Account {profile.get('display_name') or user_id[:8]}",
                    "bot_type_id": "308b6e56-d024-4831-950c-e2cefd241c1b",
                    "status": "active",
                }

        return {"user_id": user_id, **profile}

    def delete_user_admin(self, user_id: str) -> bool:
        user_id = self._check(user_id)
        if user_id in self._profiles:
            del self._profiles[user_id]
        acc_id = f"acct-{user_id}"
        if acc_id in self._accounts:
            del self._accounts[acc_id]
        return True