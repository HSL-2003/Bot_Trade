"""Authentication/session boundary.

The prototype deliberately has no password or JWT implementation. This module
provides the account-scoped contract that a production identity provider can
implement without leaking authentication logic into trading code.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import secrets
import hashlib
import time
import httpx
import re
import bcrypt
from typing import Protocol, runtime_checkable



class AuthenticationError(ValueError):
    pass


EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def normalize_email(email: str) -> str:
    email = email.strip().lower()
    if not EMAIL_RE.fullmatch(email):
        raise AuthenticationError("A valid email address is required")
    return email


def validate_password_strength(password: str) -> None:
    if len(password) < 8:
        raise AuthenticationError("Password must be at least 8 characters long")
    if not any(c.isupper() for c in password):
        raise AuthenticationError("Password must contain at least one uppercase letter")
    if not any(c.islower() for c in password):
        raise AuthenticationError("Password must contain at least one lowercase letter")
    if not any(c.isdigit() for c in password):
        raise AuthenticationError("Password must contain at least one number")


def sanitize_display_name(name: str | None) -> str | None:
    if not name:
        return None
    # Strip HTML tags & non-printable characters, limit to 50 chars
    cleaned = re.sub(r"<[^>]*>", "", name).strip()
    return cleaned[:50] if cleaned else None


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


@runtime_checkable
class SessionService(Protocol):
    def register(self, email: str, password: str, display_name: str | None = None) -> dict: ...
    def login(self, email: str, password: str, portal: str | None = None) -> dict: ...
    def magic_link(self, email: str) -> None: ...
    def login_with_supabase_token(self, supabase_token: str, portal: str = "trader") -> dict: ...
    def create(self, principal: Principal) -> Session: ...
    def authenticate(self, token: str) -> Principal: ...
    def revoke(self, token: str) -> None: ...
    def set_user_roles(self, user_id: str, roles: list[str]) -> list[str]: ...


class InMemorySessionService:
    """Development-only session service; process-local and fail-closed."""

    def __init__(self, ttl_seconds: int = 3600):
        if ttl_seconds <= 0:
            raise ValueError("Session TTL must be positive")
        self._ttl = timedelta(seconds=ttl_seconds)
        self._sessions: dict[str, Session] = {}
        self._users: dict[str, dict] = {}  # email -> {password_hash, user_id, account_id}
        self._user_roles: dict[str, frozenset[str]] = {}  # user_id -> roles (editable via admin)

    @staticmethod
    def _hash_pw(password: str) -> str:
        return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    @staticmethod
    def _verify_pw(password: str, hashed: str) -> bool:
        if not hashed:
            return False
        if hashed.startswith("$2b$") or hashed.startswith("$2a$"):
            try:
                return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
            except Exception:
                return False
        # Fallback verification for legacy SHA256 hashes during migration
        legacy_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return secrets.compare_digest(legacy_hash, hashed)

    def register(self, email: str, password: str, display_name: str | None = None) -> dict:
        email = normalize_email(email)
        validate_password_strength(password)
        if email in self._users:
            raise AuthenticationError("This email is already registered")
        user_id = secrets.token_hex(16)
        account_id = f"acct-{user_id}"
        self._users[email] = {
            "password_hash": self._hash_pw(password),
            "user_id": user_id,
            "account_id": account_id,
            "display_name": sanitize_display_name(display_name) or email,
        }
        self._user_roles[user_id] = frozenset({"trader"})
        session = self.create(Principal(account_id, user_id, self._user_roles.get(user_id) or frozenset({"trader"})))
        return {
            "access_token": session.token,
            "token_type": "bearer",
            "expires_at": session.expires_at,
            "user_id": user_id,
            "account_id": account_id,
        }

    def login(self, email: str, password: str, portal: str | None = None) -> dict:
        email = normalize_email(email)
        user = self._users.get(email)
        if not user or not self._verify_pw(password, user["password_hash"]):
            raise AuthenticationError("Invalid email or password")
        existing_roles = list(self._user_roles.get(user["user_id"]) or ["trader"])
        
        # Enforce mutual exclusion: trader != admin
        if portal == "admin":
            if "trader" in existing_roles and "admin" not in existing_roles:
                raise AuthenticationError("Tài khoản của bạn là tài khoản dùng BOT (Trader), không được phép truy cập hệ thống Quản trị (Admin).")
            roles = ["admin"]
            self._user_roles[user["user_id"]] = frozenset(roles)
        elif portal == "trader":
            if "admin" in existing_roles and "trader" not in existing_roles:
                raise AuthenticationError("Tài khoản Quản trị viên (Admin) không được sử dụng để chạy bot giao dịch. Vui lòng đăng nhập tại /admin/login.")
            roles = ["trader"]
            self._user_roles[user["user_id"]] = frozenset(roles)
        else:
            roles = ["admin"] if "admin" in existing_roles else ["trader"]

        session = self.create(Principal(user["account_id"], user["user_id"], frozenset(roles)))
        return {
            "access_token": session.token,
            "token_type": "bearer",
            "expires_at": session.expires_at,
            "user_id": user["user_id"],
            "account_id": user["account_id"],
            "roles": roles,
        }

    def magic_link(self, email: str) -> None:
        raise AuthenticationError("Email sign-in is not supported in development mode")

    def login_with_supabase_token(self, supabase_token: str, portal: str = "trader") -> dict:
        raise AuthenticationError("OAuth sign-in is not supported in development mode")

    def _cleanup_expired(self) -> None:
        now = datetime.now(timezone.utc)
        expired = [token for token, session in self._sessions.items() if session.expires_at <= now]
        for token in expired:
            self._sessions.pop(token, None)
        # Cap max active sessions to 1000 to prevent DoS memory leak
        if len(self._sessions) > 1000:
            oldest_tokens = sorted(self._sessions.keys(), key=lambda t: self._sessions[t].expires_at)[: len(self._sessions) - 1000]
            for token in oldest_tokens:
                self._sessions.pop(token, None)

    def create(self, principal: Principal) -> Session:
        if not principal.account_id or not principal.user_id:
            raise AuthenticationError("A user and account scope are required")
        self._cleanup_expired()
        session = Session(secrets.token_urlsafe(32), principal, datetime.now(timezone.utc) + self._ttl)
        self._sessions[session.token] = session
        return session

    def authenticate(self, token: str) -> Principal:
        self._cleanup_expired()
        session = self._sessions.get(token)
        if session is None or session.expires_at <= datetime.now(timezone.utc):
            self._sessions.pop(token, None)
            raise AuthenticationError("Invalid or expired session")
        base = session.principal
        roles = self._user_roles.get(base.user_id) or base.roles
        return Principal(base.account_id, base.user_id, roles)

    def revoke(self, token: str) -> None:
        self._sessions.pop(token, None)

    def set_user_roles(self, user_id: str, roles: list[str]) -> list[str]:
        clean_set = {r.strip() for r in roles if r.strip()}
        if "admin" in clean_set and "trader" in clean_set:
            raise AuthenticationError("Một tài khoản không thể đồng thời là Admin và Trader (dùng bot). Vai trò mang tính loại trừ lẫn nhau.")
        clean = ["admin"] if "admin" in clean_set else ["trader"]
        self._user_roles[user_id] = frozenset(clean)
        return clean

    def get_user_email(self, user_id: str) -> Optional[str]:
        for email, u in self._users.items():
            if u.get("user_id") == user_id:
                return email
        return None

class SupabaseSessionService:
    """Supabase Auth client plus persistent application session storage."""

    def __init__(self, url: str, service_role_key: str, ttl_seconds: int = 3600):
        url = (url or "").strip()
        service_role_key = (service_role_key or "").strip()
        if not url or not service_role_key:
            raise ValueError("Supabase URL and service-role key are required")
        if ttl_seconds <= 0:
            raise ValueError("Session TTL must be positive")
        self.auth_url = url.rstrip("/") + "/auth/v1"
        self.rest_url = url.rstrip("/") + "/rest/v1"
        self.headers = {"apikey": service_role_key, "Authorization": f"Bearer {service_role_key}"}
        self.ttl = timedelta(seconds=ttl_seconds)
        # Principal cache: skips 2 Supabase round-trips (~0.5s) per API call.
        self._auth_cache: dict[str, tuple[float, Principal]] = {}
        self._auth_cache_ttl = 60.0

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

    async def _arequest(self, method: str, url: str, **kwargs):
        """Non-blocking request through the shared connection pool.

        Keeps the event loop free while the HTTP round-trip is in flight and
        reuses pooled connections (no per-call TLS handshake).
        """
        from services.async_http import get_http_pool

        kwargs.setdefault("headers", self.headers)
        pool = get_http_pool()
        if pool is not None:
            response = await pool.request(method, url, **kwargs)
        else:
            # No running loop (sync context) — fall back to a blocking call.
            response = httpx.request(method, url, **kwargs)
        if response.status_code >= 400:
            try:
                detail = response.json().get("msg") or response.json().get("message") or response.text
            except ValueError:
                detail = response.text
            raise AuthenticationError(detail or "Authentication request failed")
        return response

    async def authenticate_async(self, token: str) -> Principal:
        """Async equivalent of ``authenticate`` for request hot paths.

        Cache-check is served from memory; a cache miss performs the two DB
        lookups through the shared AsyncClient pool without blocking the
        event loop. Mirrors the sync implementation exactly.
        """
        if not token:
            raise AuthenticationError("Bearer token is required")
        token_hash = self._hash(token)
        now = time.monotonic()
        hit = self._auth_cache.get(token_hash)
        if hit and (now - hit[0]) < self._auth_cache_ttl:
            return hit[1]
        rows = (await self._arequest(
            "GET", f"{self.rest_url}/user_sessions",
            params={
                "token_hash": f"eq.{token_hash}", "is_active": "eq.true", "status": "eq.active",
                "expires_at": f"gt.{datetime.now(timezone.utc).isoformat()}",
                "select": "user_id,account_id,roles", "limit": "1",
            },
        )).json()
        if not rows:
            raise AuthenticationError("Invalid or expired session")
        row = rows[0]
        user_id = str(row["user_id"])
        # Source of truth for roles is user_profiles (admin-editable); fall back
        # to the session's stored roles when the profile lookup fails.
        roles = []
        try:
            prof = (await self._arequest(
                "GET", f"{self.rest_url}/user_profiles",
                params={"user_id": f"eq.{user_id}", "select": "roles", "limit": "1"},
            )).json()
            if prof and prof[0].get("roles"):
                roles = list(prof[0]["roles"])
        except Exception:
            roles = []
        if not roles:
            roles = list(row.get("roles") or ["trader"])
        if "admin" in roles and "trader" in roles:
            roles = ["admin"]
        principal = Principal(str(row["account_id"]), user_id, frozenset(roles))
        self._auth_cache[token_hash] = (now, principal)
        return principal

    def register(self, email: str, password: str, display_name: str | None = None) -> dict:
        email = normalize_email(email)
        validate_password_strength(password)
        sanitized_name = sanitize_display_name(display_name) or email
        response = self._request("POST", f"{self.auth_url}/admin/users", json={
            "email": email, "password": password, "email_confirm": True,
        })
        user = response.json()
        user_id = user["id"]
        account_id = f"acct-{user_id}"
        self._request("POST", f"{self.rest_url}/user_profiles", json={
            "user_id": user_id, "display_name": sanitized_name,
        }, headers={**self.headers, "Prefer": "return=minimal", "Content-Type": "application/json"})
        self._request("POST", f"{self.rest_url}/trading_accounts", json={
            "id": account_id, "owner_user_id": user_id, "name": "Primary account",
        }, headers={**self.headers, "Prefer": "return=minimal", "Content-Type": "application/json"})
        return self._create_session(user_id, account_id)

    def login(self, email: str, password: str, portal: str | None = None) -> dict:
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

        # Validate mutual exclusion between Admin and Trader
        existing_roles = self._get_user_roles(user_id)
        if portal == "admin":
            if "trader" in existing_roles and "admin" not in existing_roles:
                raise AuthenticationError("Tài khoản của bạn là tài khoản dùng BOT (Trader), không được phép truy cập hệ thống Quản trị (Admin).")
            roles = ["admin"]
            if existing_roles != ["admin"]:
                try:
                    self._request("PATCH", f"{self.rest_url}/user_profiles",
                                  params={"user_id": f"eq.{user_id}"},
                                  json={"roles": ["admin"]},
                                  headers={**self.headers, "Prefer": "return=minimal", "Content-Type": "application/json"})
                except Exception:
                    pass
        elif portal == "trader":
            if "admin" in existing_roles and "trader" not in existing_roles:
                raise AuthenticationError("Tài khoản Quản trị viên (Admin) không được sử dụng để chạy bot giao dịch. Vui lòng đăng nhập tại /admin/login.")
            roles = ["trader"]
            if existing_roles != ["trader"]:
                try:
                    self._request("PATCH", f"{self.rest_url}/user_profiles",
                                  params={"user_id": f"eq.{user_id}"},
                                  json={"roles": ["trader"]},
                                  headers={**self.headers, "Prefer": "return=minimal", "Content-Type": "application/json"})
                except Exception:
                    pass
        else:
            roles = list(existing_roles) if existing_roles else ["trader"]

        account_id = self._ensure_account(user_id)
        return self._create_session(user_id, account_id, roles=roles)

    def magic_link(self, email: str) -> None:
        """Send a passwordless sign-in (magic) link to the given email."""
        email = normalize_email(email)
        try:
            response = httpx.post(f"{self.auth_url}/otp", headers=self.headers,
                                  json={"email": email, "create_user": True}, timeout=10)
        except httpx.HTTPError as exc:
            raise AuthenticationError("Authentication service is unavailable") from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("msg") or response.json().get("message") or response.text
            except ValueError:
                detail = response.text
            raise AuthenticationError(detail or "Could not send a sign-in link")

    def login_with_supabase_token(self, supabase_token: str, portal: str = "trader") -> dict:
        """Exchange a Supabase OAuth or magic-link token into an app session.

        The user returns from GitHub/Google OAuth or email link with a short-lived
        Supabase access token in the URL fragment. We verify it against the
        Supabase Auth /user endpoint, enforce role separation, and mint our session.
        """
        if not supabase_token:
            raise AuthenticationError("Missing authentication token")
        try:
            response = httpx.get(
                f"{self.auth_url}/user",
                headers={**self.headers, "Authorization": f"Bearer {supabase_token}"},
                timeout=10,
            )
        except httpx.HTTPError as exc:
            raise AuthenticationError("Authentication service is unavailable") from exc
        if response.status_code >= 400:
            raise AuthenticationError("Invalid or expired sign-in token")
        user_data = response.json()
        user_id = (user_data.get("id") or "").strip()
        email = (user_data.get("email") or "").strip().lower()
        if not user_id:
            raise AuthenticationError("Invalid authentication response")

        # Check existing profile & roles
        prof = []
        try:
            prof = self._request("GET", f"{self.rest_url}/user_profiles", params={
                "user_id": f"eq.{user_id}", "select": "roles,user_id", "limit": "1",
            }).json()
        except Exception:
            pass

        existing_roles = list(prof[0].get("roles") or []) if prof and len(prof) > 0 and prof[0].get("roles") else []

        if portal == "admin":
            # Validation rule: If account is already a trader (bot user), deny admin access
            if "trader" in existing_roles and "admin" not in existing_roles:
                raise AuthenticationError("Tài khoản của bạn là tài khoản dùng BOT (Trader), không có quyền Quản trị (Admin).")
            
            roles = ["admin"]
            # Save / update profile to guarantee admin role and no trader role
            if prof and len(prof) > 0:
                if existing_roles != ["admin"]:
                    try:
                        self._request("PATCH", f"{self.rest_url}/user_profiles",
                                      params={"user_id": f"eq.{user_id}"},
                                      json={"roles": ["admin"]},
                                      headers={**self.headers, "Prefer": "return=minimal", "Content-Type": "application/json"})
                    except Exception:
                        pass
            else:
                display = (user_data.get("user_metadata") or {}).get("full_name") or (email.split("@")[0] if email else "Admin Operator")
                try:
                    self._request("POST", f"{self.rest_url}/user_profiles", json={
                        "user_id": user_id,
                        "display_name": display,
                        "roles": ["admin"],
                    }, headers={**self.headers, "Prefer": "return=minimal", "Content-Type": "application/json"})
                except Exception:
                    pass
        else: # portal == "trader"
            # Validation rule: If account is already an admin, deny trader / bot usage
            if "admin" in existing_roles and "trader" not in existing_roles:
                raise AuthenticationError("Tài khoản Quản trị viên (Admin) không được sử dụng để chạy bot giao dịch. Vui lòng đăng nhập tại /admin/login.")
            
            roles = list(existing_roles) if ("trader" in existing_roles) else ["trader"]
            if prof and len(prof) > 0:
                if "trader" not in existing_roles:
                    try:
                        updated_roles = list(set(existing_roles + ["trader"]))
                        self._request("PATCH", f"{self.rest_url}/user_profiles",
                                      params={"user_id": f"eq.{user_id}"},
                                      json={"roles": updated_roles},
                                      headers={**self.headers, "Prefer": "return=minimal", "Content-Type": "application/json"})
                        roles = updated_roles
                    except Exception:
                        pass
            else:
                display = (user_data.get("user_metadata") or {}).get("full_name") or (email.split("@")[0] if email else "Trader")
                try:
                    self._request("POST", f"{self.rest_url}/user_profiles", json={
                        "user_id": user_id,
                        "display_name": display,
                        "roles": ["trader"],
                    }, headers={**self.headers, "Prefer": "return=minimal", "Content-Type": "application/json"})
                except Exception:
                    pass

        account_id = self._ensure_account(user_id)
        return self._create_session(user_id, account_id, roles=roles)

    def get_user_email(self, user_id: str) -> Optional[str]:
        if not user_id:
            return None
        if not hasattr(self, "_email_cache"):
            self._email_cache = {}
        if user_id in self._email_cache:
            return self._email_cache[user_id]
        try:
            response = self._request("GET", f"{self.auth_url}/admin/users/{user_id.strip()}")
            email = response.json().get("email")
            if email:
                self._email_cache[user_id] = email
            return email
        except Exception:
            return None

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

    def _get_user_roles(self, user_id: str) -> list[str]:
        try:
            prof = self._request("GET", f"{self.rest_url}/user_profiles", params={
                "user_id": f"eq.{user_id}", "select": "roles", "limit": "1",
            }).json()
            if prof and prof[0].get("roles"):
                return list(prof[0]["roles"])
        except Exception:
            pass
        return ["trader"]

    def _create_session(self, user_id: str, account_id: str, roles: list[str] | None = None) -> dict:
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + self.ttl
        if roles is None:
            roles = self._get_user_roles(user_id)
        self._request("POST", f"{self.rest_url}/user_sessions", json={
            "user_id": user_id, "account_id": account_id, "token_hash": self._hash(token),
            "expires_at": expires_at.isoformat(),
            "roles": roles,
        }, headers={**self.headers, "Prefer": "return=minimal", "Content-Type": "application/json"})
        
        # Pre-warm cache so subsequent /api/auth/me or /api/admin/overview hit memory cache (0ms)
        principal = Principal(account_id, user_id, frozenset(roles))
        self._auth_cache[self._hash(token)] = (time.monotonic(), principal)
        
        return {"access_token": token, "token_type": "bearer", "expires_at": expires_at,
                "user_id": user_id, "account_id": account_id, "roles": roles}

    def authenticate(self, token: str) -> Principal:
        if not token:
            raise AuthenticationError("Bearer token is required")
        token_hash = self._hash(token)
        now = time.monotonic()
        hit = self._auth_cache.get(token_hash)
        if hit and (now - hit[0]) < self._auth_cache_ttl:
            return hit[1]
        rows = self._request("GET", f"{self.rest_url}/user_sessions", params={
            "token_hash": f"eq.{token_hash}", "is_active": "eq.true", "status": "eq.active",
            "expires_at": f"gt.{datetime.now(timezone.utc).isoformat()}", "select": "user_id,account_id,roles", "limit": "1",
        }).json()
        if not rows:
            raise AuthenticationError("Invalid or expired session")
        row = rows[0]
        user_id = str(row["user_id"])
        # Source of truth for roles is user_profiles (admin-editable); fall back
        # to the session's stored roles when the profile lookup fails.
        roles = []
        try:
            prof = self._request("GET", f"{self.rest_url}/user_profiles", params={
                "user_id": f"eq.{user_id}", "select": "roles", "limit": "1",
            }).json()
            if prof and prof[0].get("roles"):
                roles = list(prof[0]["roles"])
        except Exception:
            roles = []
        if not roles:
            roles = list(row.get("roles") or ["trader"])
        if "admin" in roles and "trader" in roles:
            roles = ["admin"]
        principal = Principal(str(row["account_id"]), user_id, frozenset(roles))
        self._auth_cache[token_hash] = (now, principal)
        return principal

    def revoke(self, token: str) -> None:
        self._auth_cache.pop(self._hash(token), None)
        self._request("PATCH", f"{self.rest_url}/user_sessions", params={"token_hash": f"eq.{self._hash(token)}"},
                      json={"status": "revoked", "is_active": False, "revoked_at": datetime.now(timezone.utc).isoformat()},
                      headers={**self.headers, "Prefer": "return=minimal", "Content-Type": "application/json"})

    def set_user_roles(self, user_id: str, roles: list[str]) -> list[str]:
        clean_set = {r.strip() for r in roles if r.strip()}
        if "admin" in clean_set and "trader" in clean_set:
            raise AuthenticationError("Một tài khoản không thể đồng thời là Admin và Trader (dùng bot). Vai trò mang tính loại trừ lẫn nhau.")
        clean = ["admin"] if "admin" in clean_set else ["trader"]
        try:
            self._request("PATCH", f"{self.rest_url}/user_profiles",
                          params={"user_id": f"eq.{user_id.strip()}"},
                          json={"roles": clean},
                          headers={**self.headers, "Prefer": "return=minimal", "Content-Type": "application/json"})
            for thash, (_, principal) in list(self._auth_cache.items()):
                if principal.user_id == user_id.strip():
                    self._auth_cache.pop(thash, None)
        except Exception:
            pass
        return clean