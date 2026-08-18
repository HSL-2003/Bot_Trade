"""Supabase PostgREST repository adapter.

Uses the existing httpx dependency and keeps the domain independent of
Supabase. The service-role key must only be used server-side.
"""

from typing import Any
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

    def dashboard_rows(self, account_id: str, user_id: str, days: int) -> list[dict[str, Any]]:
        response = httpx.get(f"{self.base_url}/user_profit_daily", params={"account_id": f"eq.{account_id}", "user_id": f"eq.{user_id}", "order": "trading_date.asc", "limit": str(max(days, 1))}, headers=self.headers, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def recent_trades(self, account_id: str, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        response = httpx.get(f"{self.base_url}/trade_orders", params={"account_id": f"eq.{account_id}", "user_id": f"eq.{user_id}", "order": "closed_at.desc", "limit": str(limit), "select": "symbol,side,quantity,entry_price,close_price,profit,status,closed_at"}, headers=self.headers, timeout=self.timeout)
        response.raise_for_status()
        return response.json()