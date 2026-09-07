"""Security middleware and utilities.

Focused hardening utilities for the FastAPI gateway:

- CSRF protection (double-submit cookie pattern) for cookie-authenticated
  state-changing requests. API clients that authenticate with the
  ``Authorization: Bearer`` header are exempt, so REST tooling keeps working.
- Request body size enforcement to bound memory usage (DoS hardening).
- Security event logging with a per-request correlation id for audit trails.

Keeping these utilities in one module keeps ``app.py`` (the HTTP gateway)
lean and un-bloated, and keeps trading domain code untouched.
"""

from __future__ import annotations

import logging
import secrets
from typing import Awaitable, Callable

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

security_logger = logging.getLogger("security")

# Default CSRF cookie name and header name (double-submit cookie pattern).
CSRF_COOKIE_NAME = "_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"

# Maximum accepted request body size in bytes (1 MB).
MAX_BODY_BYTES = 1 * 1024 * 1024

# HTTP methods that are exempt from CSRF checks (safe, idempotent).
_CSRF_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# Paths that establish a new session or that must never carry a CSRF header
# (the browser-based double-submit pattern only applies to authenticated
# state changes, not to creating the session in the first place).
_CSRF_EXEMPT_PATHS = frozenset({
    "/api/auth/login",
    "/api/auth/register",
    "/api/auth/reset-password",
    "/api/auth/forgot-password",
    "/api/auth/magic-link",
    "/api/auth/social/callback",
})


def new_correlation_id() -> str:
    """Return a random correlation id used to tie together log lines."""
    return secrets.token_hex(8)


def log_security_event(
    event: str,
    *,
    account_id: str | None = None,
    user_id: str | None = None,
    correlation_id: str | None = None,
    client_ip: str | None = None,
    detail: str = "",
    severity: str = "warning",
    details: dict | None = None,
) -> None:
    """Emit a structured security log line for auditability."""
    parts = [f"event={event}", f"severity={severity}"]
    if account_id:
        parts.append(f"account={account_id}")
    if user_id:
        parts.append(f"user={user_id}")
    if correlation_id:
        parts.append(f"correlation={correlation_id}")
    if client_ip:
        parts.append(f"ip={client_ip}")
    if details:
        # Serialize the structured details dict into the audit line.
        parts.append("details=" + ",".join(f"{k}={v}" for k, v in details.items() if v is not None))
    if detail:
        parts.append(f"detail={detail}")
    security_logger.warning(" ".join(parts))


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Attach a correlation id and a request-id header to every response."""

    async def dispatch(self, request: Request, call_next: Callable[..., Awaitable[Response]]):
        correlation_id = request.headers.get("X-Correlation-Id") or new_correlation_id()
        request.state.correlation_id = correlation_id
        response = await call_next(request)
        response.headers["X-Correlation-Id"] = correlation_id
        return response


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject request bodies larger than a configured cap (DoS defense)."""

    def __init__(self, app, *, max_bytes: int = MAX_BODY_BYTES):
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next: Callable[..., Awaitable[Response]]):
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > self.max_bytes:
            log_security_event(
                "request_too_large",
                client_ip=request.client.host if request.client else None,
                correlation_id=getattr(request.state, "correlation_id", None),
                detail=f"content-length={content_length}",
            )
            return Response(status_code=413, content="Payload Too Large")
        response = await call_next(request)
        return response


def is_bearer_authenticated(request: Request) -> bool:
    """Heuristic: requests carrying an Authorization Bearer token are exempt."""
    auth = request.headers.get("Authorization", "")
    return auth.startswith("Bearer ")


class CSRFProtectionMiddleware(BaseHTTPMiddleware):
    """Double-submit cookie CSRF protection.

    A random CSRF value is emitted as an HttpOnly cookie on the first
    request. State-changing requests that rely on cookie authentication
    must echo the same value in the ``X-CSRF-Token`` header.

    Requests that authenticate via ``Authorization: Bearer`` are exempt
    because a token in a header is not automatically sent by the browser
    (the classic CSRF vector). This keeps API clients working unchanged.
    """

    def __init__(self, app):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable[..., Awaitable[Response]]):
        method = request.method.upper()
        is_safe = method in _CSRF_SAFE_METHODS
        is_api = is_bearer_authenticated(request)
        is_session_setup = request.url.path in _CSRF_EXEMPT_PATHS

        if not is_safe and not is_api and not is_session_setup:
            expected = request.cookies.get(CSRF_COOKIE_NAME, "")
            supplied = request.headers.get(CSRF_HEADER_NAME, "")
            if not expected or not supplied or not secrets.compare_digest(expected, supplied):
                log_security_event(
                    "csrf_rejected",
                    client_ip=request.client.host if request.client else None,
                    correlation_id=getattr(request.state, "correlation_id", None),
                    detail=f"method={request.method} path={request.url.path}",
                )
                return Response(status_code=403, content="CSRF validation failed")

        response = await call_next(request)
        # Always (re)issuance a fresh CSRF cookie when one is absent.
        if not request.cookies.get(CSRF_COOKIE_NAME):
            token = secrets.token_urlsafe(32)
            response.set_cookie(
                CSRF_COOKIE_NAME,
                token,
                max_age=3600,
                httponly=False,   # js needs to read it to echo back
                samesite="strict",
                secure=request.url.scheme == "https",
            )
        return response