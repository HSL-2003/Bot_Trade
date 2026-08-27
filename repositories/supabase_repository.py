"""Supabase PostgREST repository adapter.

Uses the existing httpx dependency and keeps the domain independent of
Supabase. The service-role key must only be used server-side.
"""

from datetime import datetime, timezone
from typing import Any, Optional
import time
import httpx
from repositories.persistence import AccountScopeError, AccountState


class SupabaseAccountRepository:
    def __init__(self, url: str, service_role_key: str, *, timeout: float = 5.0):
        if not url or not service_role_key:
            raise ValueError("Supabase URL and server key are required")
        self.base_url = url.rstrip("/") + "/rest/v1"
        self.headers = {"apikey": service_role_key, "Authorization": f"Bearer {service_role_key}"}
        self.timeout = timeout
        # Short-TTL cache for admin aggregations: prevents the overview + accounts
        # endpoints from re-fetching the same rows from Supabase on every refresh.
        self._cache: dict[str, tuple[float, Any]] = {}

    def get(self, account_id: str) -> AccountState:
        if not account_id or not account_id.strip():
            raise AccountScopeError("Account scope is required")
        response = httpx.get(
            f"{self.base_url}/trading_accounts",
            params={"id": f"eq.{account_id}", "select": "id"},
            headers=self.headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        if not response.json():
            raise AccountScopeError("Trading account does not exist")
        return AccountState(account_id.strip())

    def save(self, state: AccountState) -> None:
        if not state.account_id.strip():
            raise AccountScopeError("Account scope is required")
        payload = {"id": state.account_id, "settings": state.settings}
        response = httpx.patch(
            f"{self.base_url}/trading_accounts",
            params={"id": f"eq.{state.account_id}"},
            json=payload,
            headers={**self.headers, "Content-Type": "application/json", "Prefer": "return=minimal"},
            timeout=self.timeout,
        )
        response.raise_for_status()

    def profile(self, user_id: str) -> dict[str, Any]:
        if not user_id or not user_id.strip():
            raise AccountScopeError("User scope is required")
        response = httpx.get(f"{self.base_url}/user_profiles", params={"user_id": f"eq.{user_id.strip()}", "select": "*", "limit": "1"}, headers=self.headers, timeout=self.timeout)
        response.raise_for_status()
        return response.json()[0] if response.json() else {"user_id": user_id.strip()}

    def update_profile(self, user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not user_id or not user_id.strip():
            raise AccountScopeError("User scope is required")
        user_id = user_id.strip()
        response = httpx.patch(
            f"{self.base_url}/user_profiles",
            params={"user_id": f"eq.{user_id}"},
            json=payload,
            headers={**self.headers, "Content-Type": "application/json", "Prefer": "return=representation"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        rows = response.json()
        if rows:
            return rows[0]

        # If no record was updated (profile does not exist yet), insert a new profile row
        insert_payload = {"user_id": user_id, **payload}
        insert_response = httpx.post(
            f"{self.base_url}/user_profiles",
            json=insert_payload,
            headers={**self.headers, "Content-Type": "application/json", "Prefer": "return=representation"},
            timeout=self.timeout,
        )
        insert_response.raise_for_status()
        insert_rows = insert_response.json()
        return insert_rows[0] if insert_rows else insert_payload

    def dashboard_rows(self, account_id: str, user_id: Optional[str] = None, days: int = 30) -> list[dict[str, Any]]:
        params = {"account_id": f"eq.{account_id}", "order": "trading_date.asc", "limit": str(max(days, 1))}
        if user_id and user_id.strip():
            params["user_id"] = f"eq.{user_id.strip()}"
        try:
            response = httpx.get(f"{self.base_url}/user_profit_daily", params=params, headers=self.headers, timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except Exception:
            return []

    def recent_trades(self, account_id: str, user_id: Optional[str] = None, limit: int = 20) -> list[dict[str, Any]]:
        params = {"account_id": f"eq.{account_id}", "order": "closed_at.desc.nullslast", "limit": str(limit), "select": "*"}
        if user_id and user_id.strip():
            params["user_id"] = f"eq.{user_id.strip()}"
        try:
            response = httpx.get(f"{self.base_url}/trade_orders", params=params, headers=self.headers, timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except Exception:
            return []

    def record_trade_open(self, account_id: str, user_id: Optional[str], trade: dict[str, Any]) -> dict[str, Any]:
        if not account_id or not account_id.strip():
            raise AccountScopeError("Account scope is required")
        now_iso = datetime.now(timezone.utc).isoformat()
        payload = {
            "account_id": account_id.strip(),
            "user_id": user_id.strip() if user_id and user_id.strip() else None,
            "broker_ticket": trade.get("ticket"),
            "symbol": trade.get("symbol", "XAUUSD"),
            "side": (trade.get("type") or trade.get("side") or "BUY").upper(),
            "order_type": trade.get("order_type", "MARKET"),
            "quantity": float(trade.get("volume") or trade.get("lot_size") or trade.get("quantity") or 0.01),
            "entry_price": float(trade.get("open_price") or trade.get("price") or trade.get("entry_price") or 0.0),
            "stop_loss": float(trade.get("sl") or trade.get("stop_loss") or 0.0),
            "take_profit": float(trade.get("tp") or trade.get("take_profit") or 0.0),
            "profit": 0.0,
            "status": "filled",
            "submitted_at": trade.get("open_time", now_iso),
            "filled_at": trade.get("open_time", now_iso),
            "metadata": trade.get("metadata", {})
        }
        try:
            response = httpx.post(
                f"{self.base_url}/trade_orders",
                json=payload,
                headers={**self.headers, "Content-Type": "application/json", "Prefer": "return=representation"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            rows = response.json()
            return rows[0] if rows else payload
        except Exception:
            return payload

    def record_trade_close(self, account_id: str, user_id: Optional[str], ticket: int, close_info: dict[str, Any]) -> Optional[dict[str, Any]]:
        now_iso = datetime.now(timezone.utc).isoformat()
        close_p = float(close_info.get("close_price") or 0.0)
        profit_v = float(close_info.get("profit") or 0.0)
        close_t = str(close_info.get("close_time") or close_info.get("closed_at") or now_iso)
        ticket_val = int(ticket) if str(ticket).isdigit() else ticket
        self._cache_clear("admin_trades")

        patch_payload = {
            "close_price": close_p,
            "profit": profit_v,
            "status": "closed",
            "closed_at": close_t,
        }
        try:
            # 1. Primary update: Match by broker_ticket (unique across orders)
            response = httpx.patch(
                f"{self.base_url}/trade_orders",
                params={"broker_ticket": f"eq.{ticket_val}"},
                json=patch_payload,
                headers={**self.headers, "Content-Type": "application/json", "Prefer": "return=representation"},
                timeout=self.timeout,
            )
            if response.is_success:
                rows = response.json()
                if rows:
                    return rows[0]
            
            # 2. Secondary update: Match by account_id and broker_ticket
            if account_id and account_id.strip():
                response2 = httpx.patch(
                    f"{self.base_url}/trade_orders",
                    params={"account_id": f"eq.{account_id.strip()}", "broker_ticket": f"eq.{ticket_val}"},
                    json=patch_payload,
                    headers={**self.headers, "Content-Type": "application/json", "Prefer": "return=representation"},
                    timeout=self.timeout,
                )
                if response2.is_success:
                    rows2 = response2.json()
                    if rows2:
                        return rows2[0]
            
            # 3. If no existing row was updated, insert a closed record
            full_payload = {
                "account_id": (account_id.strip() if account_id and account_id.strip() else "demo-account"),
                "user_id": user_id.strip() if user_id and user_id.strip() else None,
                "broker_ticket": ticket_val,
                "symbol": close_info.get("symbol", "XAUUSD"),
                "side": (close_info.get("type") or close_info.get("side") or "BUY").upper(),
                "order_type": "MARKET",
                "quantity": float(close_info.get("volume") or close_info.get("quantity") or 0.01),
                "entry_price": float(close_info.get("open_price") or close_info.get("entry_price") or 0.0),
                "close_price": close_p,
                "profit": profit_v,
                "status": "closed",
                "submitted_at": close_info.get("open_time") or now_iso,
                "filled_at": close_info.get("open_time") or now_iso,
                "closed_at": close_t,
                "metadata": {}
            }
            insert_res = httpx.post(
                f"{self.base_url}/trade_orders",
                json=full_payload,
                headers={**self.headers, "Content-Type": "application/json", "Prefer": "return=representation"},
                timeout=self.timeout,
            )
            if insert_res.is_success:
                insert_rows = insert_res.json()
                return insert_rows[0] if insert_rows else full_payload
            return patch_payload
        except Exception:
            return patch_payload

    def get_lock_state(self, account_id: str) -> dict[str, Any]:
        if not account_id or not account_id.strip():
            raise AccountScopeError("Account scope is required")
        try:
            response = httpx.get(
                f"{self.base_url}/trading_accounts",
                params={"id": f"eq.{account_id.strip()}", "select": "lock_state,lock_reason,locked_at,unlocked_at"},
                headers=self.headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            rows = response.json()
            if rows:
                return {
                    "lock_state": rows[0].get("lock_state") or "unlocked",
                    "lock_reason": rows[0].get("lock_reason"),
                    "locked_at": rows[0].get("locked_at"),
                    "unlocked_at": rows[0].get("unlocked_at"),
                }
        except Exception:
            pass
        return {"lock_state": "unlocked", "lock_reason": None, "locked_at": None, "unlocked_at": None}

    def set_lock_state(self, account_id: str, lock_state: str, reason: Optional[str] = None) -> None:
        if not account_id or not account_id.strip():
            raise AccountScopeError("Account scope is required")
        now_iso = datetime.now(timezone.utc).isoformat()
        payload: dict[str, Any] = {"lock_state": lock_state, "lock_reason": reason}
        if lock_state == "unlocked":
            payload["unlocked_at"] = now_iso
        else:
            payload["locked_at"] = now_iso
        try:
            response = httpx.patch(
                f"{self.base_url}/trading_accounts",
                params={"id": f"eq.{account_id.strip()}"},
                json=payload,
                headers={**self.headers, "Content-Type": "application/json", "Prefer": "return=minimal"},
                timeout=self.timeout,
            )
            response.raise_for_status()
        except Exception:
            # Never let a persistence hiccup crash the lock change in-memory.
            pass
        self._cache_clear("admin_accounts")

    def sync_closed_trades(self, closed_trades: list[dict[str, Any]]) -> int:
        """Auto-heal & synchronize any unclosed trade rows in Supabase"""
        self._cache_clear("admin_trades")
        synced_count = 0
        now_iso = datetime.now(timezone.utc).isoformat()
        for t in closed_trades:
            ticket = t.get("ticket") or t.get("broker_ticket")
            close_p = float(t.get("close_price") or 0.0)
            if not ticket or close_p <= 0:
                continue
            profit_v = float(t.get("profit") or 0.0)
            close_t = str(t.get("close_time") or t.get("closed_at") or now_iso)
            ticket_val = int(ticket) if str(ticket).isdigit() else ticket
            patch_payload = {
                "close_price": close_p,
                "profit": profit_v,
                "status": "closed",
                "closed_at": close_t,
            }
            try:
                res = httpx.patch(
                    f"{self.base_url}/trade_orders",
                    params={"broker_ticket": f"eq.{ticket_val}"},
                    json=patch_payload,
                    headers={**self.headers, "Content-Type": "application/json", "Prefer": "return=representation"},
                    timeout=self.timeout,
                )
                if res.is_success and res.json():
                    synced_count += 1
            except Exception:
                pass
        return synced_count

    def get_user_trades(self, account_id: str, user_id: Optional[str] = None, limit: int = 100, period: str = "all") -> list[dict[str, Any]]:
        if not account_id or not account_id.strip():
            raise AccountScopeError("Account scope is required")
        params = {
            "account_id": f"eq.{account_id.strip()}",
            "order": "submitted_at.desc",
            "limit": str(limit),
            "select": "*"
        }
        if user_id and user_id.strip():
            params["user_id"] = f"eq.{user_id.strip()}"
        try:
            response = httpx.get(f"{self.base_url}/trade_orders", params=params, headers=self.headers, timeout=self.timeout)
            response.raise_for_status()
            rows = response.json()
            return rows
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Admin / dashboard helpers (soft-delete, roles, aggregation)
    # ------------------------------------------------------------------
    def _cache_get(self, key: str, ttl: float):
        """Return cached value if fresh within `ttl` seconds, else None."""
        hit = self._cache.get(key)
        if hit and (time.monotonic() - hit[0]) < ttl:
            return hit[1]
        return None

    def _cache_set(self, key: str, value: Any, ttl: float) -> None:
        import time as _time
        self._cache[key] = (_time.monotonic(), value)

    def _cache_clear(self, key: str | None = None) -> None:
        if key:
            self._cache.pop(key, None)
        else:
            self._cache.clear()

    def _batch_locks(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch lock_state for many accounts in ONE PostgREST request."""
        if not ids:
            return {}
        try:
            response = httpx.get(
                f"{self.base_url}/trading_accounts",
                params={"id": f"in.({','.join(ids)})", "select": "id,lock_state,lock_reason,locked_at,unlocked_at"},
                headers=self.headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            default = {"lock_state": "unlocked", "lock_reason": None, "locked_at": None, "unlocked_at": None}
            return {
                row["id"]: {
                    "lock_state": row.get("lock_state") or "unlocked",
                    "lock_reason": row.get("lock_reason"),
                    "locked_at": row.get("locked_at"),
                    "unlocked_at": row.get("unlocked_at"),
                }
                for row in response.json()
            } or {i: default for i in ids}
        except Exception:
            return {i: {"lock_state": "unlocked", "lock_reason": None, "locked_at": None, "unlocked_at": None} for i in ids}

    def _batch_profiles(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch N profiles in ONE Supabase request instead of one HTTP call per profile."""
        if not ids:
            return {}
        try:
            response = httpx.get(
                f"{self.base_url}/user_profiles",
                params={"user_id": f"in.({','.join(ids)})", "select": "user_id,display_name,roles"},
                headers=self.headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            return {row["user_id"]: row for row in response.json()}
        except Exception:
            return {}

    def list_accounts(self, *, ttl: float = 20.0) -> list[dict[str, Any]]:
        cached = self._cache_get("admin_accounts", ttl)
        if cached is not None:
            return cached
        try:
            response = httpx.get(
                f"{self.base_url}/trading_accounts",
                params={"select": "*", "order": "created_at.asc", "limit": "1000"},
                headers=self.headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            rows = response.json()
        except Exception:
            return []

        owner_ids = [str(r.get("owner_user_id")) for r in rows if r.get("owner_user_id")]
        profiles = self._batch_profiles(owner_ids)

        out = []
        for row in rows:
            owner_id = row.get("owner_user_id")
            profile = profiles.get(str(owner_id)) if owner_id else None
            display_name = (profile or {}).get("display_name")
            roles = list((profile or {}).get("roles") or ["trader"])
            out.append({
                "id": row.get("id"),
                "owner_user_id": owner_id,
                "display_name": display_name or owner_id or row.get("id"),
                "roles": roles,
                "status": row.get("status", "active"),
                "is_active": row.get("is_active", True),
                "lock_state": row.get("lock_state") or "unlocked",
                "lock_reason": row.get("lock_reason"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
                "blocked_at": row.get("blocked_at"),
                "blocked_reason": row.get("blocked_reason"),
            })
        self._cache_set("admin_accounts", out, ttl)
        return out

    def get_all_trades(self, limit: int = 5000, *, ttl: float = 5.0) -> list[dict[str, Any]]:
        cached = self._cache_get("admin_trades", ttl)
        if cached is not None:
            return cached
        try:
            response = httpx.get(
                f"{self.base_url}/trade_orders",
                params={"select": "*", "order": "submitted_at.desc", "limit": str(limit)},
                headers=self.headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            rows = response.json()
            self._cache_set("admin_trades", rows, ttl)
            return rows
        except Exception:
            return []

    def get_account_detail(self, account_id: str) -> dict[str, Any]:
        if not account_id or not account_id.strip():
            raise AccountScopeError("Account scope is required")
        try:
            response = httpx.get(
                f"{self.base_url}/trading_accounts",
                params={"id": f"eq.{account_id.strip()}", "select": "*", "limit": "1"},
                headers=self.headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            rows = response.json()
            if not rows:
                raise AccountScopeError("Trading account does not exist")
            acc = rows[0]
        except Exception as exc:
            if isinstance(exc, AccountScopeError):
                raise
            return {}
        lock = self.get_lock_state(account_id)
        owner_id = acc.get("owner_user_id")
        owner = self.profile(owner_id) if owner_id else {}
        trades = self.get_user_trades(account_id, limit=1000)
        closed = [t for t in trades if t.get("status") == "closed"]
        wins = [t for t in closed if (t.get("profit") or 0) > 0]
        losses = [t for t in closed if (t.get("profit") or 0) < 0]
        return {
            **acc,
            "display_name": owner.get("display_name") or owner_id or account_id,
            "roles": list(owner.get("roles") or ["trader"]),
            "lock_state": lock["lock_state"],
            "lock_reason": lock["lock_reason"],
            "total_trades": len(closed),
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "total_profit": round(sum(t.get("profit") or 0 for t in closed), 2),
            "total_volume": round(sum(t.get("quantity") or 0 for t in closed), 2),
            "recent_trades": trades[:20],
        }

    def set_account_status(self, account_id: str, status: str, reason: Optional[str] = None) -> None:
        if not account_id or not account_id.strip():
            raise AccountScopeError("Account scope is required")
        if status not in ("active", "blocked", "suspended", "archived"):
            raise ValueError(f"Invalid account status: {status!r}")
        now_iso = datetime.now(timezone.utc).isoformat()
        payload: dict[str, Any] = {"status": status, "is_active": status == "active"}
        if status == "blocked":
            payload["blocked_at"] = now_iso
            payload["blocked_reason"] = reason
            payload["unblocked_at"] = None
        elif status == "active":
            payload["blocked_reason"] = None
            payload["unblocked_at"] = now_iso
        self._admin_patch(f"{self.base_url}/trading_accounts", {"id": f"eq.{account_id.strip()}"}, payload)
        self._cache_clear("admin_accounts")

    def update_user_roles(self, user_id: str, roles: list[str]) -> dict[str, Any]:
        clean = sorted({r.strip() for r in roles if r.strip()} or ["trader"])
        if not user_id or not user_id.strip():
            raise AccountScopeError("User scope is required")
        self._admin_patch(f"{self.base_url}/user_profiles", {"user_id": f"eq.{user_id.strip()}"}, {"roles": clean})
        self._cache_clear("admin_accounts")
        return {"user_id": user_id.strip(), "roles": clean}

    def revoke_account_sessions(self, account_id: str) -> int:
        if not account_id or not account_id.strip():
            raise AccountScopeError("Account scope is required")
        payload = {"is_active": False, "status": "revoked", "revoked_at": datetime.now(timezone.utc).isoformat()}
        try:
            response = httpx.patch(
                f"{self.base_url}/user_sessions",
                params={"account_id": f"eq.{account_id.strip()}"},
                json=payload,
                headers={**self.headers, "Content-Type": "application/json", "Prefer": "return=representation"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            return len(response.json()) if response.text else 0
        except Exception:
            return 0

    def _admin_patch(self, resource: str, params: dict[str, str], payload: dict[str, Any]) -> None:
        try:
            response = httpx.patch(
                resource,
                params=params,
                json=payload,
                headers={**self.headers, "Content-Type": "application/json", "Prefer": "return=minimal"},
                timeout=self.timeout,
            )
            response.raise_for_status()
        except Exception:
            pass