"""FastAPI authentication dependencies shared by HTTP and application code.

Every concept lives in exactly one place:

- ``_extract_token`` is the single source of truth for reading a session token
  (Authorization header first, session cookie as the browser-flow fallback).
- ``auth_enforced`` is the single source of truth for deciding whether a
  request without a session may proceed.
- ``_admin_allowlist`` is the single source of truth for admin-by-account-id.
- ``AuthenticationError`` is raised by the domain layer and translated into
  ``HTTPException`` only here, at the HTTP boundary.
"""

import os
import asyncio
from typing import Optional

from fastapi import Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from security import log_security_event
from services.auth_service import AuthenticationError, Principal, SessionService

__all__ = [
    "SESSION_COOKIE_NAME",
    "authenticate_websocket",
    "auth_enforced",
    "get_account_id",
    "get_bearer_token",
    "get_current_principal",
    "get_current_principal_optional",
    "get_session_service",
    "require_admin",
    "set_session_cookie",
]

security = HTTPBearer(auto_error=False)
SESSION_COOKIE_NAME = "session_token"


def get_session_service(request: Request) -> SessionService:
    """Provide the session service configured at application startup."""
    return request.app.state.session_service


def _extract_token(request: Request) -> Optional[str]:
    """Read the session token from the Authorization header, then the cookie."""
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        return authorization[7:].strip()
    return request.cookies.get(SESSION_COOKIE_NAME)


def auth_enforced() -> bool:
    """Auth is mandatory when REQUIRE_AUTH is on or the deployment is production."""
    return (
        os.getenv("REQUIRE_AUTH", "false").lower() == "true"
        or os.getenv("ENVIRONMENT", "development").lower() == "production"
    )


def _admin_allowlist() -> frozenset:
    """Accounts that may act as admins without holding the admin role."""
    entries = {
        entry.strip()
        for entry in os.getenv("ADMIN_ACCOUNT_IDS", "").split(",")
        if entry.strip()
    }
    return frozenset(entries)


def _requested_account_id(request: Request) -> Optional[str]:
    """Read the account scope the client claims to operate on."""
    return request.headers.get("X-Account-Id") or request.query_params.get("account_id")


def _default_account_id() -> str:
    return os.getenv("DEFAULT_ACCOUNT_ID", "demo-account")


def _scoped_account_id(requested: Optional[str], principal: Principal) -> str:
    """Keep the session authoritative; reject any attempt to cross accounts."""
    if requested and requested != principal.account_id:
        raise HTTPException(status_code=403, detail="Account scope mismatch")
    return principal.account_id


async def get_bearer_token(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> str:
    token = credentials.credentials if credentials else _extract_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Bearer authentication is required")
    return token


async def _authenticate(session_service, token: str):
    """Non-blocking session authentication.

    Prefers the async (pooled, non-blocking) implementation when the session
    service exposes it; otherwise runs the sync implementation in a worker
    thread so the event loop is never blocked by the HTTP round-trips.
    """
    auth = getattr(session_service, "authenticate_async", None)
    if callable(auth):
        return await auth(token)
    return await asyncio.to_thread(session_service.authenticate, token)


async def get_current_principal(
    token: str = Depends(get_bearer_token),
    session_service: SessionService = Depends(get_session_service),
) -> Principal:
    try:
        return await _authenticate(session_service, token)
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


async def get_current_principal_optional(
    request: Request,
    session_service: SessionService = Depends(get_session_service),
) -> Optional[Principal]:
    token = _extract_token(request)
    if not token:
        return None
    try:
        return await _authenticate(session_service, token)
    except AuthenticationError as exc:
        # A supplied token that fails validation must surface as 401, not as an
        # anonymous request (legacy behavior: only "no token" means anonymous).
        raise HTTPException(status_code=401, detail=str(exc)) from exc


async def require_admin(
    request: Request,
    principal: Principal = Depends(get_current_principal),
) -> Principal:
    if "admin" in principal.roles or principal.account_id in _admin_allowlist():
        return principal
    log_security_event(
        "admin_access_denied",
        account_id=principal.account_id,
        user_id=principal.user_id,
        correlation_id=getattr(request.state, "correlation_id", None),
        client_ip=request.client.host if request.client else None,
    )
    raise HTTPException(status_code=403, detail="Admin role is required")


async def get_account_id(
    request: Request,
    principal: Optional[Principal] = Depends(get_current_principal_optional),
) -> str:
    requested = _requested_account_id(request)
    if principal is not None:
        return _scoped_account_id(requested, principal)
    if auth_enforced():
        raise HTTPException(status_code=401, detail="Bearer authentication is required")
    return requested or _default_account_id()


def set_session_cookie(response: Response, session: dict, request: Request) -> None:
    """Persist the session token in an HttpOnly, SameSite=Strict cookie."""
    token = session.get("access_token")
    if token:
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=token,
            max_age=3600,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            path="/",
        )


async def authenticate_websocket(token: str, session_service: SessionService) -> Principal:
    """Authenticate a WebSocket token using the same session boundary as HTTP."""
    return await _authenticate(session_service, token)
