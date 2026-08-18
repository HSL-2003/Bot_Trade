"""Account-scoped persistence contracts and a development implementation."""

from dataclasses import dataclass, field
from typing import Any, Protocol


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


class InMemoryAccountRepository:
    def __init__(self):
        self._states: dict[str, AccountState] = {}
        self._profiles: dict[str, dict[str, Any]] = {}

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