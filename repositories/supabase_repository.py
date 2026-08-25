"""Supabase PostgREST repository adapter.

Uses the existing httpx dependency and keeps the domain independent of
Supabase. The service-role key must only be used server-side.
"""

from datetime import datetime, timezone
from typing import Any, Optional
import httpx
from repositories.persistence import AccountScopeError, AccountState


class SupabaseAccountRepository:
    def __init__(self, url: str, service_role_key: str, *, timeout: float = 5.0):
        if not url or not service_role_key:
            raise ValueError("Supabase URL and server key are required")
        self.base_url = url.rstrip("/") + "/rest/v1"
        self.headers = {"apikey": service_role_key, "Authorization": f"Bearer {service_role_key}"}
        self.timeout = timeout

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

    def sync_closed_trades(self, closed_trades: list[dict[str, Any]]) -> int:
        """Auto-heal & synchronize any unclosed trade rows in Supabase"""
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