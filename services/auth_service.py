"""Authentication/session boundary.

The prototype deliberately has no password or JWT implementation. This module
provides the account-scoped contract that a production identity provider can
implement without leaking authentication logic into trading code.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import secrets
import hashlib
import httpx
import re


class AuthenticationError(ValueError):
    pass


EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def normalize_email(email: str) -> str:
    email = email.strip().lower()
    if not EMAIL_RE.fullmatch(email):
        raise AuthenticationError("A valid email address is required")
    return email


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
        self._users: dict[str, dict] = {}  # email -> {password_hash, user_id, account_id}

    @staticmethod
    def _hash_pw(password: str) -> str:
        return hashlib.sha256(password.encode("utf-8")).hexdigest()

    def register(self, email: str, password: str, display_name: str | None = None) -> dict:
        email = normalize_email(email)
        if email in self._users:
            raise AuthenticationError("This email is already registered")
        user_id = secrets.token_hex(16)
        account_id = f"acct-{user_id}"
        self._users[email] = {
            "password_hash": self._hash_pw(password),
            "user_id": user_id,
            "account_id": account_id,
            "display_name": display_name,
        }
        session = self.create(Principal(account_id, user_id))
        return {
            "access_token": session.token,
            "token_type": "bearer",
            "expires_at": session.expires_at,
            "user_id": user_id,
            "account_id": account_id,
        }

    def login(self, email: str, password: str) -> dict:
        email = normalize_email(email)
        user = self._users.get(email)
        if not user or user["password_hash"] != self._hash_pw(password):
            raise AuthenticationError("Invalid email or password")
        session = self.create(Principal(user["account_id"], user["user_id"]))
        return {
            "access_token": session.token,
            "token_type": "bearer",
            "expires_at": session.expires_at,
            "user_id": user["user_id"],
            "account_id": user["account_id"],
        }

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


class SupabaseSessionService:
    """Supabase Auth client plus persistent application session storage."""

    def __init__(self, url: str, service_role_key: str, ttl_seconds: int = 3600):
        if not url or not service_role_key:
            raise ValueError("Supabase URL and service-role key are required")
        if ttl_seconds <= 0:
            raise ValueError("Session TTL must be positive")
        self.auth_url = url.rstrip("/") + "/auth/v1"
        self.rest_url = url.rstrip("/") + "/rest/v1"
        self.headers = {"apikey": service_role_key, "Authorization": f"Bearer {service_role_key}"}
        self.ttl = timedelta(seconds=ttl_seconds)

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _request(self, method: str, url: str, **kwargs):
        kwargs.setdefault("headers", self.headers)
        kwargs.setdefault("timeout", 10)
        response = httpx.request(method, url, **kwargs)
        if response.status_code >= 400:
            try:
                detail = response.json().get("msg") or response.json().get("message") or response.text
            except ValueError:
                detail = response.text
            raise AuthenticationError(detail or "Authentication request failed")
        return response

    def register(self, email: str, password: str, display_name: str | None = None) -> dict:
        email = normalize_email(email)
        response = self._request("POST", f"{self.auth_url}/admin/users", json={
            "email": email, "password": password, "email_confirm": False,
        })
        user = response.json()
        user_id = user["id"]
        account_id = f"acct-{user_id}"
        self._request("POST", f"{self.rest_url}/user_profiles", json={
            "user_id": user_id, "display_name": display_name or email.split("@", 1)[0],
        }, headers={**self.headers, "Prefer": "return=minimal", "Content-Type": "application/json"})
        self._request("POST", f"{self.rest_url}/trading_accounts", json={
            "id": account_id, "owner_user_id": user_id, "name": "Primary account",
        }, headers={**self.headers, "Prefer": "return=minimal", "Content-Type": "application/json"})
        return self._create_session(user_id, account_id)

    def login(self, email: str, password: str) -> dict:
        email = normalize_email(email)
        try:
            response = httpx.post(f"{self.auth_url}/token", params={"grant_type": "password"},
                                  headers=self.headers, json={"email": email, "password": password}, timeout=10)
        except httpx.HTTPError as exc:
            raise AuthenticationError("Authentication service is unavailable") from exc
        if response.status_code >= 400:
            try:
                error = response.json()
            except ValueError:
                error = {}
            message = str(error.get("error_description") or error.get("msg") or "").lower()
            if "confirm" in message or "not verified" in message:
                raise AuthenticationError("Please confirm your email before signing in")
            raise AuthenticationError("Invalid email or password")
        user = response.json().get("user") or {}
        user_id = user.get("id")
        if not user_id:
            raise AuthenticationError("Invalid authentication response")
        account_id = self._ensure_account(user_id)
        return self._create_session(user_id, account_id)

    def _ensure_account(self, user_id: str) -> str:
        """Return the user's active account, repairing legacy/missing rows."""
        accounts = self._request("GET", f"{self.rest_url}/trading_accounts", params={
            "owner_user_id": f"eq.{user_id}", "select": "id,is_active,status",
            "order": "created_at.asc", "limit": "1",
        }).json()
        if accounts:
            account_id = accounts[0]["id"]
            if not accounts[0].get("is_active", True) or accounts[0].get("status") != "active":
                self._request("PATCH", f"{self.rest_url}/trading_accounts",
                              params={"id": f"eq.{account_id}"},
                              json={"is_active": True, "status": "active", "deleted_at": None},
                              headers={**self.headers, "Prefer": "return=minimal", "Content-Type": "application/json"})
            return account_id

        account_id = f"acct-{user_id}"
        self._request("POST", f"{self.rest_url}/trading_accounts", json={
            "id": account_id, "owner_user_id": user_id, "name": "Primary account",
            "status": "active", "is_active": True,
        }, headers={**self.headers, "Prefer": "return=minimal", "Content-Type": "application/json"})
        return account_id

    def _create_session(self, user_id: str, account_id: str) -> dict:
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + self.ttl
        self._request("POST", f"{self.rest_url}/user_sessions", json={
            "user_id": user_id, "account_id": account_id, "token_hash": self._hash(token),
            "expires_at": expires_at.isoformat(),
        }, headers={**self.headers, "Prefer": "return=minimal", "Content-Type": "application/json"})
        return {"access_token": token, "token_type": "bearer", "expires_at": expires_at,
                "user_id": user_id, "account_id": account_id}

    def authenticate(self, token: str) -> Principal:
        if not token:
            raise AuthenticationError("Bearer token is required")
        rows = self._request("GET", f"{self.rest_url}/user_sessions", params={
            "token_hash": f"eq.{self._hash(token)}", "is_active": "eq.true", "status": "eq.active",
            "expires_at": f"gt.{datetime.now(timezone.utc).isoformat()}", "select": "user_id,account_id,roles", "limit": "1",
        }).json()
        if not rows:
            raise AuthenticationError("Invalid or expired session")
        row = rows[0]
        return Principal(str(row["account_id"]), str(row["user_id"]), frozenset(row.get("roles") or ["trader"]))

    def revoke(self, token: str) -> None:
        self._request("PATCH", f"{self.rest_url}/user_sessions", params={"token_hash": f"eq.{self._hash(token)}"},
                      json={"status": "revoked", "is_active": False, "revoked_at": datetime.now(timezone.utc).isoformat()},
                      headers={**self.headers, "Prefer": "return=minimal", "Content-Type": "application/json"})