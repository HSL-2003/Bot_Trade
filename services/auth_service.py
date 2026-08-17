"""Authentication/session boundary.

The prototype deliberately has no password or JWT implementation. This module
provides the account-scoped contract that a production identity provider can
implement without leaking authentication logic into trading code.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import secrets


class AuthenticationError(ValueError):
    pass


@dataclass(frozen=True)
class Principal:
    account_id: str
    user_id: str
    roles: frozenset[str] = frozenset({"trader"})


@dataclass(frozen=True)
class Session:
    token: str
    principal: Principal
    expires_at: datetime


class InMemorySessionService:
    """Development-only session service; process-local and fail-closed."""

    def __init__(self, ttl_seconds: int = 3600):
        if ttl_seconds <= 0:
            raise ValueError("Session TTL must be positive")
        self._ttl = timedelta(seconds=ttl_seconds)
        self._sessions: dict[str, Session] = {}

    def create(self, principal: Principal) -> Session:
        if not principal.account_id or not principal.user_id:
            raise AuthenticationError("A user and account scope are required")
        session = Session(secrets.token_urlsafe(32), principal, datetime.now(timezone.utc) + self._ttl)
        self._sessions[session.token] = session
        return session

    def authenticate(self, token: str) -> Principal:
        session = self._sessions.get(token)
        if session is None or session.expires_at <= datetime.now(timezone.utc):
            self._sessions.pop(token, None)
            raise AuthenticationError("Invalid or expired session")
        return session.principal

    def revoke(self, token: str) -> None:
        self._sessions.pop(token, None)