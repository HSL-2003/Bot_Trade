"""Supabase PostgREST repository adapter.

Uses the existing httpx dependency and keeps the domain independent of
Supabase. The service-role key must only be used server-side.
"""

from typing import Any
import httpx
from persistence import AccountScopeError, AccountState


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