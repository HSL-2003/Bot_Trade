from typing import Optional, Literal
import os
import asyncio
import logging
from contextlib import asynccontextmanager
from urllib.parse import quote
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv(override=True)

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, JSONResponse, Response
from fastapi.encoders import jsonable_encoder
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from pydantic import BaseModel
import uvicorn
from datetime import datetime, timezone
from bot import MT5TradingBot, MT5_AVAILABLE
from config import SUPPORTED_SYMBOLS, agents_enabled, allowed_origins
from services.account_service import TradingAccountService
from services.auth_service import AuthenticationError, InMemorySessionService, SupabaseSessionService, normalize_email, validate_password_strength
from services.auth_dependencies import _authenticate as _session_authenticate
from repositories.persistence import InMemoryAccountRepository, LOCK_HARD, LOCK_SOFT, LOCK_UNLOCKED
from repositories.supabase_repository import SupabaseAccountRepository
from security import (
    CSRFProtectionMiddleware,
    CorrelationIdMiddleware,
    RequestSizeLimitMiddleware,
    log_security_event,
)

security_logger = logging.getLogger("security")
logger = logging.getLogger("app")

limiter = Limiter(key_func=get_remote_address, default_limits=["120/minute", "10/second"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle: initialize MT5, sync closed trades, feed prices on
    startup; tear down broker/session resources on shutdown.

    Note: ``bot``, ``repository`` and ``account_service`` are module-level globals
    assigned later in this file. They are resolved at runtime (when the lifespan
    coroutine actually runs at app startup) rather than at import time, so the
    function may appear here before those names are bound.
    """
    # --- startup ---
    await bot.initialize_mt5()
    await bot.fetch_news_feed()
    # Auto-heal & sync closed trades in Supabase using local history
    if hasattr(repository, "sync_closed_trades") and bot.history:
        try:
            await asyncio.to_thread(repository.sync_closed_trades, bot.history)
        except Exception:
            pass
    # Start the continuous price feed loop in the background
    asyncio.create_task(bot.start_price_feed_loop())

    # Phase 0: start the shared broker-mutation pipeline (serializes all MT5
    # trade ops through one queue behind a single IPC channel).
    from services.execution_pipeline import get_default_pipeline
    await get_default_pipeline().start()

    try:
        yield
    finally:
        # --- shutdown ---
        await account_service.shutdown()
        pipeline = get_default_pipeline()
        await pipeline.stop()
        from services.async_http import aclose_http_pool
        await aclose_http_pool()
        from services.direct_db import close_direct_db
        await close_direct_db()
        if not bot.simulation_mode and MT5_AVAILABLE:
            import MetaTrader5 as mt5  # type: ignore[import-not-found]
            mt5.shutdown()


app = FastAPI(title="MT5 Confluence Algo Bot", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # Content-Security-Policy supporting Three.js WebGL, Google Fonts, Supabase CDN, TradingView widgets & WebSockets
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' blob: https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://unpkg.com https://s3.tradingview.com https://*.tradingview.com https://www.gstatic.com; "
            "worker-src 'self' blob:; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://api.fontshare.com; "
            "font-src 'self' https://fonts.gstatic.com https://api.fontshare.com https://cdn.fontshare.com data:; "
            "media-src 'self' https://api.getlayers.ai data:; "
            "img-src 'self' data: https: https://*.tradingview.com; "
            "frame-src 'self' https://*.tradingview.com https://s.tradingview.com https://www.tradingview.com; "
            "child-src 'self' blob: https://*.tradingview.com https://s.tradingview.com https://www.tradingview.com; "
            "connect-src 'self' blob: data: ws: wss: http: https: https://*.supabase.co https://unpkg.com https://*.tradingview.com wss://*.tradingview.com wss://pushstream.tradingview.com https://www.gstatic.com;"
        )
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


# ---------------------------------------------------------------------------
# Middleware stack. In Starlette the LAST-added middleware is OUTERMOST, so we
# register in this order deliberately:
#   1. CSRF protection (innermost, just before routing).
#   2. Request body size cap (DoS hardening).
#   3. CORS.
#   4. Correlation id propagation (sets X-Correlation-Id).
#   5. Security headers (OUTERMOST, so every response "including rejections"
#      carries the headers and a correlation id).
# ---------------------------------------------------------------------------
app.add_middleware(CSRFProtectionMiddleware)
app.add_middleware(RequestSizeLimitMiddleware)

# Enable CORS with specific methods and headers for production hardening
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Account-Id", "X-CSRF-Token"],
    max_age=600,
)

# Correlation id propagation -- every response carries X-Correlation-Id.
app.add_middleware(CorrelationIdMiddleware)

# Security headers added last => outermost, so even rejected requests get them.
app.add_middleware(SecurityHeadersMiddleware)


# Mount local static files directory
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Instantiate the global Trading Bot
bot = MT5TradingBot()  # legacy compatibility; scoped routes use account_service
if os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY"):
    logger.info("⚡ [SUPABASE] Initializing SupabaseAccountRepository at %s", os.getenv("SUPABASE_URL"))
    repository = SupabaseAccountRepository(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
else:
    logger.warning("⚠️ [AUTH] SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set. Using InMemoryAccountRepository")
    repository = InMemoryAccountRepository()
account_service = TradingAccountService(repository)
default_acc_id = os.getenv("DEFAULT_ACCOUNT_ID", "demo-account")
account_service.register_bot(default_acc_id, bot)
if os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY"):
    logger.info("⚡ [SUPABASE] Initializing SupabaseSessionService")
    session_service = SupabaseSessionService(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
else:
    logger.warning("⚠️ [AUTH] Using InMemorySessionService (No persistent auth)")
    session_service = InMemorySessionService()
bot_task = None


async def account_id_from_request(request: Request) -> str:
    account_id = request.headers.get("X-Account-Id") or request.query_params.get("account_id")
    authorization = request.headers.get("Authorization", "")
    # Authenticate whenever a Bearer token is present OR when REQUIRE_AUTH is on.
    # Production always requires auth, even if REQUIRE_AUTH was left unset, so a
    # mis-configured deployment can never silently expose trading endpoints.
    require_auth = os.getenv("REQUIRE_AUTH", "false").lower() == "true" or os.getenv("ENVIRONMENT", "development").lower() == "production"
    if authorization.startswith("Bearer ") or require_auth:
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Bearer authentication is required")
        try:
            principal = await _session_authenticate(session_service, authorization[7:].strip())
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

        # Validation: Admin accounts cannot operate as personal bot traders
        if "admin" in principal.roles and "trader" not in principal.roles:
            raise HTTPException(
                status_code=403,
                detail="Tài khoản Quản trị viên (Admin) không được sử dụng để chạy bot giao dịch. Vui lòng sử dụng tài khoản Trader hoặc quản lý tại Admin Portal."
            )

        if account_id and account_id != principal.account_id:
            raise HTTPException(status_code=403, detail="Account scope mismatch")
        account_id = principal.account_id
    return account_id or os.getenv("DEFAULT_ACCOUNT_ID", "demo-account")


class RegisterModel(BaseModel):
    email: str
    password: str
    display_name: Optional[str] = None


class LoginModel(BaseModel):
    email: str
    password: str
    portal: Optional[str] = None

class MagicLinkModel(BaseModel):
    email: str

class SocialCallbackModel(BaseModel):
    token: str
    portal: Optional[str] = "trader"

class ProfileModel(BaseModel):
    display_name: Optional[str] = None
    full_name: Optional[str] = None
    phone: Optional[str] = None
    timezone: Optional[str] = None
    preferred_currency: Optional[str] = None
    language: Optional[str] = None
    risk_level: Optional[Literal["low", "medium", "high"]] = None
    trading_goal: Optional[str] = None


class AgentTaskModel(BaseModel):
    prompt: str
    max_retries: Optional[int] = 3


class BotTypeCreate(BaseModel):
    name: str
    description: Optional[str] = None
    risk_level: Literal["low", "medium", "high"] = "medium"
    default_symbol: str = "XAUUSD"
    risk_percent: float = 1.5
    max_spread: int = 200
    max_daily_loss_percent: float = 5.0
    auto_trading: bool = True
    trailing_stop_enabled: bool = True
    trailing_stop_distance: int = 100
    breakeven_enabled: bool = True
    breakeven_trigger: int = 200
    cooldown_minutes: int = 15
    max_open_trades: int = 5
    is_active: bool = True
    metadata: Optional[dict] = {}


class BotTypeUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    risk_level: Optional[Literal["low", "medium", "high"]] = None
    default_symbol: Optional[str] = None
    risk_percent: Optional[float] = None
    max_spread: Optional[int] = None
    max_daily_loss_percent: Optional[float] = None
    auto_trading: Optional[bool] = None
    trailing_stop_enabled: Optional[bool] = None
    trailing_stop_distance: Optional[int] = None
    breakeven_enabled: Optional[bool] = None
    breakeven_trigger: Optional[int] = None
    cooldown_minutes: Optional[int] = None
    max_open_trades: Optional[int] = None
    is_active: Optional[bool] = None
    metadata: Optional[dict] = None


def bearer_token(request: Request) -> str:
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        cookie = request.cookies.get("session_token")
        if cookie:
            return cookie
        require_auth = os.getenv("REQUIRE_AUTH", "false").lower() == "true" or os.getenv("ENVIRONMENT", "development").lower() == "production"
        if not require_auth:
            return "demo-token"
        raise HTTPException(status_code=401, detail="Bearer authentication is required")
    return authorization[7:].strip()


async def require_admin(request: Request) -> None:
    """Verify that the request comes from an admin user. Raises HTTPException if not."""
    require_auth = os.getenv("REQUIRE_AUTH", "false").lower() == "true" or os.getenv("ENVIRONMENT", "development").lower() == "production"
    token = bearer_token(request)
    if (token == "demo-token" or not token) and not require_auth:
        return
    if not token:
        raise HTTPException(status_code=401, detail="Bearer token is required")

    try:
        principal = await _session_authenticate(session_service, token)
        allowed_accounts = {a.strip() for a in os.getenv("ADMIN_ACCOUNT_IDS", "").split(",") if a.strip()}
        admin_emails = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "admin@ponytail.finance,operator@ponytail.finance").split(",") if e.strip()}
        user_email = (session_service.get_user_email(principal.user_id) if hasattr(session_service, "get_user_email") else "") or ""
        
        # Strict validation: If user is a trader (bot user), deny admin access
        if "trader" in principal.roles and "admin" not in principal.roles:
            log_security_event(
                event="admin_access_denied_trader_role",
                severity="warning",
                user_id=principal.user_id,
                details={"roles": list(principal.roles), "endpoint": str(request.url.path)},
                correlation_id=getattr(request.state, "correlation_id", None),
                client_ip=request.client.host if request.client else None,
            )
            raise HTTPException(status_code=403, detail="Tài khoản Trader dùng BOT không có quyền truy cập hệ thống Quản trị (Admin)")

        is_admin = (
            "admin" in principal.roles 
            or principal.account_id in allowed_accounts 
            or user_email.lower() in admin_emails
            or "admin" in user_email.lower()
            or not require_auth
        )
        if not is_admin:
            log_security_event(
                event="admin_access_denied",
                severity="warning",
                user_id=principal.user_id,
                details={"roles": list(principal.roles), "endpoint": str(request.url.path)},
                correlation_id=getattr(request.state, "correlation_id", None),
                client_ip=request.client.host if request.client else None,
            )
            raise HTTPException(status_code=403, detail="Admin access required")
    except AuthenticationError as exc:
        if not require_auth:
            return
        log_security_event(
            event="admin_auth_failed",
            severity="warning",
            details={"endpoint": str(request.url.path)},
            correlation_id=getattr(request.state, "correlation_id", None),
            client_ip=request.client.host if request.client else None,
        )
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def set_session_cookie(response: Response, session: dict, request: Request) -> None:
    """Persist the session token in an HttpOnly, SameSite=Strict cookie."""
    token = session.get("access_token")
    if not token:
        return
    response.set_cookie(
        key="session_token",
        value=token,
        max_age=3600,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/",
    )


@app.post("/api/auth/register", status_code=201)
@limiter.limit("5/minute")
async def register(request: Request, payload: RegisterModel):
    logger.info("📝 [AUTH] Register attempt for: %s", payload.email)
    if len(payload.password) < 8:
        logger.warning("❌ [AUTH] Password too short for: %s", payload.email)
        raise HTTPException(status_code=422, detail="Password must contain at least 8 characters")
    try:
        result = session_service.register(normalize_email(payload.email), payload.password, payload.display_name)
        logger.info("✅ [AUTH] Registered successfully: %s (user_id: %s)", payload.email, result.get("user_id"))
    except AuthenticationError as exc:
        logger.warning("❌ [AUTH] Registration failed for %s: %s", payload.email, exc)
        log_security_event(
            "registration_failed",
            correlation_id=getattr(request.state, "correlation_id", None),
            client_ip=request.client.host if request.client else None,
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("❌ [AUTH] Unexpected error during registration: %s", exc)
        raise HTTPException(status_code=500, detail="Internal server error during registration") from exc
    response = JSONResponse(content=jsonable_encoder(result), status_code=201)
    set_session_cookie(response, result, request)
    return response


@app.post("/api/auth/login")
@limiter.limit("10/minute")
async def login(request: Request, payload: LoginModel):
    logger.info("🔑 [AUTH] Login attempt for: %s (portal: %s)", payload.email, payload.portal)
    try:
        result = session_service.login(normalize_email(payload.email), payload.password, portal=payload.portal)
        logger.info("✅ [AUTH] Login succeeded for: %s (user_id: %s)", payload.email, result.get("user_id"))
    except AuthenticationError as exc:
        logger.warning("❌ [AUTH] Login failed for %s: %s", payload.email, exc)
        log_security_event(
            "login_failed",
            correlation_id=getattr(request.state, "correlation_id", None),
            client_ip=request.client.host if request.client else None,
            detail="invalid credentials or role conflict",
        )
        status = 403 if ("không có quyền" in str(exc).lower() or "không được phép" in str(exc).lower() or "không được sử dụng" in str(exc).lower()) else 401
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("❌ [AUTH] Unexpected error during login: %s", exc)
        raise HTTPException(status_code=500, detail="Internal server error during login") from exc
    response = JSONResponse(content=jsonable_encoder(result))
    set_session_cookie(response, result, request)
    return response


@app.post("/api/auth/magic-link")
@limiter.limit("10/minute")
async def magic_link(request: Request, payload: MagicLinkModel):
    """Passwordless sign-in: send a one-click link to the user's email."""
    try:
        session_service.magic_link(payload.email)
    except AuthenticationError as exc:
        log_security_event(
            "magic_link_failed",
            correlation_id=getattr(request.state, "correlation_id", None),
            client_ip=request.client.host if request.client else None,
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"message": "Check your email for a secure sign-in link."}


@app.get("/api/auth/github")
async def github_oauth(request: Request):
    """Start GitHub OAuth via Supabase Auth."""
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    site_url = os.getenv("SITE_URL", "http://127.0.0.1:8000").rstrip("/")
    if not supabase_url:
        raise HTTPException(status_code=503, detail="OAuth is not configured")
    portal = request.query_params.get("portal", "")
    redirect_uri = f"{site_url}/auth/callback"
    if portal:
        redirect_uri += f"?portal={portal}"
    authorize_url = f"{supabase_url}/auth/v1/authorize?provider=github&redirect_to={quote(redirect_uri, safe='')}"
    return RedirectResponse(authorize_url)


@app.get("/api/auth/google")
async def google_oauth(request: Request):
    """Start Google OAuth via Supabase Auth."""
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    site_url = os.getenv("SITE_URL", "http://127.0.0.1:8000").rstrip("/")
    if not supabase_url:
        raise HTTPException(status_code=503, detail="OAuth is not configured")
    portal = request.query_params.get("portal", "")
    redirect_uri = f"{site_url}/auth/callback"
    if portal:
        redirect_uri += f"?portal={portal}"
    authorize_url = f"{supabase_url}/auth/v1/authorize?provider=google&redirect_to={quote(redirect_uri, safe='')}"
    return RedirectResponse(authorize_url)


@app.post("/api/auth/social/callback")
@limiter.limit("10/minute")
async def social_callback(request: Request, payload: SocialCallbackModel):
    """Exchange a Supabase OAuth / magic-link token for an app session."""
    try:
        portal = (payload.portal or "trader").strip().lower()
        result = session_service.login_with_supabase_token(payload.token, portal=portal)
    except AuthenticationError as exc:
        log_security_event(
            "oauth_callback_failed",
            correlation_id=getattr(request.state, "correlation_id", None),
            client_ip=request.client.host if request.client else None,
        )
        status = 403 if ("không có quyền" in str(exc).lower() or "không được" in str(exc).lower() or "không thể" in str(exc).lower()) else 401
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    response = JSONResponse(content=jsonable_encoder(result))
    set_session_cookie(response, result, request)
    return response


@app.get("/api/auth/me")
async def current_user(request: Request):
    try:
        principal = await _session_authenticate(session_service, bearer_token(request))
        email = session_service.get_user_email(principal.user_id) if hasattr(session_service, "get_user_email") else None
        display_name = None
        if hasattr(repository, "profile"):
            try:
                prof = repository.profile(principal.user_id)
                display_name = prof.get("display_name")
            except Exception:
                pass
        return {
            "user_id": principal.user_id,
            "account_id": principal.account_id,
            "roles": sorted(principal.roles),
            "email": email,
            "display_name": display_name or (email.split("@")[0] if email else "Trader"),
        }
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

@app.get("/api/auth/verify")
async def verify_token(request: Request):
    """Verify if a bearer token is valid"""
    token = bearer_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Bearer token required")
    try:
        principal = await asyncio.to_thread(session_service.authenticate, token)
        return {"valid": True, "user_id": principal.user_id, "account_id": principal.account_id}
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from exc


@app.post("/api/auth/logout", status_code=204)
async def logout(request: Request):
    try:
        session_service.revoke(bearer_token(request))
    except AuthenticationError as exc:
        log_security_event(
            "logout_failed",
            correlation_id=getattr(request.state, "correlation_id", None),
            client_ip=request.client.host if request.client else None,
        )
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    response = Response(status_code=204)
    response.delete_cookie("session_token", path="/")
    return response

async def principal_optional_from_request(request: Request) -> Optional[Any]:
    try:
        token = bearer_token(request)
        return await _session_authenticate(session_service, token)
    except Exception:
        return None


async def scoped_bot(request: Request) -> MT5TradingBot:
    acc_id = await account_id_from_request(request)
    principal = await principal_optional_from_request(request)
    user_id = principal.user_id if principal else None
    return account_service.get_bot(acc_id, user_id)


async def principal_from_request(request: Request):
    require_auth = os.getenv("REQUIRE_AUTH", "false").lower() == "true" or os.getenv("ENVIRONMENT", "development").lower() == "production"
    try:
        token = bearer_token(request)
        if token == "demo-token" and not require_auth:
            from services.auth_service import Principal
            return Principal(os.getenv("DEFAULT_ACCOUNT_ID", "demo-account"), "demo-user", frozenset({"trader"}))
        return await _session_authenticate(session_service, token)
    except Exception as exc:
        if not require_auth:
            from services.auth_service import Principal
            return Principal(os.getenv("DEFAULT_ACCOUNT_ID", "demo-account"), "demo-user", frozenset({"trader"}))
        if isinstance(exc, HTTPException):
            raise exc
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    allowed = {a.strip() for a in os.getenv("ADMIN_ACCOUNT_IDS", "").split(",") if a.strip()}
    if "admin" in principal.roles or principal.account_id in allowed:
        return principal
    log_security_event(
        "admin_access_denied",
        account_id=principal.account_id,
        user_id=principal.user_id,
        correlation_id=getattr(request.state, "correlation_id", None),
        client_ip=request.client.host if request.client else None,
    )
    raise HTTPException(status_code=403, detail="Admin role is required")

@app.get("/dashboard")
async def get_user_dashboard():
    return render_template("dashboard.html")


@app.get("/profile")
async def get_profile_page():
    return render_template("profile.html")

@app.get("/api/profile")
async def get_profile(request: Request):
    principal = await asyncio.to_thread(principal_from_request, request)
    user_id = principal.user_id
    account_id = principal.account_id

    # Run blocking DB lookups in worker thread pool
    def _fetch_profile_details():
        p_data = {}
        if hasattr(repository, "profile"):
            try:
                p_data = repository.profile(user_id) or {}
            except Exception:
                p_data = {"user_id": user_id}
        else:
            p_data = {"user_id": user_id}

        email = session_service.get_user_email(user_id) if hasattr(session_service, "get_user_email") else None
        
        acc_detail = {}
        if hasattr(repository, "get_account_detail"):
            try:
                acc_detail = repository.get_account_detail(account_id) or {}
            except Exception:
                acc_detail = {}
        return p_data, email, acc_detail

    profile_data, email, account_detail = await asyncio.to_thread(_fetch_profile_details)
    display_name = profile_data.get("display_name") or (email.split("@")[0] if email else "Trader")

    # 4. Financial & Realtime Balance Info from scoped_bot
    bot_inst = await scoped_bot(request)
    acc_info = bot_inst.account_info if hasattr(bot_inst, "account_info") else {}
    balance = float(acc_info.get("balance") or 10000.0)
    equity = float(acc_info.get("equity") or balance)
    floating_profit = float(acc_info.get("profit") or 0.0)
    margin = float(acc_info.get("margin") or 0.0)
    free_margin = float(acc_info.get("free_margin") or (equity - margin))
    currency = str(acc_info.get("currency") or profile_data.get("preferred_currency") or "USD")
    daily_drawdown_percent = float(acc_info.get("daily_drawdown_percent") or 0.0)

    # 5. Composite payload (Option A)
    return {
        **profile_data,
        "user_id": user_id,
        "account_id": account_id,
        "email": email,
        "display_name": display_name,
        "roles": sorted(principal.roles),
        "status": account_detail.get("status") or "active",
        "preferred_currency": currency,
        "finance": {
            "balance": round(balance, 2),
            "equity": round(equity, 2),
            "floating_profit": round(floating_profit, 2),
            "margin": round(margin, 2),
            "free_margin": round(free_margin, 2),
            "currency": currency,
            "daily_drawdown_percent": round(daily_drawdown_percent, 2),
        },
        "broker_info": {
            "broker": account_detail.get("broker") or ("Simulation Mode" if bot_inst.simulation_mode else "MetaQuotes-Demo"),
            "account_number": account_detail.get("account_number") or account_id,
            "lock_state": account_detail.get("lock_state") or bot_inst.lock_state or "unlocked",
            "bot_type_id": account_detail.get("bot_type_id"),
            "is_running": bot_inst.is_running,
            "simulation_mode": bot_inst.simulation_mode,
        },
    }
@app.patch("/api/profile")
async def update_profile(payload: ProfileModel, request: Request):
    principal = await principal_from_request(request)
    data = {key: value for key, value in payload.dict().items() if value is not None}
    if not hasattr(repository, "update_profile"):
        return {"user_id": principal.user_id, **data}
    return await asyncio.to_thread(repository.update_profile, principal.user_id, data)

@app.get("/api/dashboard/data")
@limiter.limit("60/minute")
async def dashboard_data(request: Request, days: int = 30):
    principal = await principal_from_request(request)
    days = max(1, min(days, 365))
    
    # 1. Fetch user trades from repository asynchronously in thread pool
    trades = []
    if hasattr(repository, "get_user_trades"):
        trades = await asyncio.to_thread(repository.get_user_trades, principal.account_id, principal.user_id, limit=1000)
    elif hasattr(repository, "recent_trades"):
        trades = await asyncio.to_thread(repository.recent_trades, principal.account_id, principal.user_id, limit=1000)
    
    # 2. Filter closed trades
    raw_closed = [t for t in trades if t.get("status") == "closed" or (t.get("close_price") is not None and float(t.get("close_price") or 0) > 0)]
    
    normalized_trades = []
    for t in raw_closed:
        normalized_trades.append({
            "broker_ticket": t.get("broker_ticket") or t.get("ticket"),
            "ticket": t.get("broker_ticket") or t.get("ticket"),
            "symbol": t.get("symbol", "XAUUSD"),
            "side": (t.get("side") or t.get("type") or "BUY").upper(),
            "type": (t.get("side") or t.get("type") or "BUY").upper(),
            "quantity": float(t.get("quantity") or t.get("volume") or 0.01),
            "volume": float(t.get("quantity") or t.get("volume") or 0.01),
            "entry_price": float(t.get("entry_price") or t.get("open_price") or 0.0),
            "open_price": float(t.get("entry_price") or t.get("open_price") or 0.0),
            "close_price": float(t.get("close_price") or 0.0),
            "profit": float(t.get("profit") or 0.0),
            "status": t.get("status") or "closed",
            "closed_at": str(t.get("closed_at") or t.get("close_time") or t.get("submitted_at") or "")[:19].replace("T", " "),
            "close_time": str(t.get("closed_at") or t.get("close_time") or t.get("submitted_at") or "")[:19].replace("T", " ")
        })

    # 3. Calculate summary metrics
    total_profit = sum(t["profit"] for t in normalized_trades)
    wins = [t for t in normalized_trades if t["profit"] > 0]
    losses = [t for t in normalized_trades if t["profit"] <= 0]
    count = len(normalized_trades)
    win_rate = max(0.0, min(100.0, (len(wins) / count * 100.0))) if count > 0 else 0.0
    # 4. Build cumulative profit points
    points = []
    cumulative = 0.0
    # Sort chronological for chart
    sorted_for_chart = sorted(normalized_trades, key=lambda x: str(x.get("closed_at") or ""))
    for t in sorted_for_chart:
        p = t["profit"]
        cumulative += p
        dt_label = str(t.get("closed_at") or "")[:10]
        points.append({
            "date": dt_label,
            "profit": round(p, 2),
            "cumulative_profit": round(cumulative, 2),
            "trades": 1
        })
    
    return {
        "summary": {
            "total_profit": round(total_profit, 2),
            "total_trades": count,
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate": round(win_rate, 2)
        },
        "points": points,
        "trades": normalized_trades
    }

@app.get("/api/history/analytics")
@limiter.limit("60/minute")
async def get_history_analytics(request: Request, period: str = "all"):
    if period not in ["day", "week", "month", "all"]:
        period = "all"
    current_bot = await scoped_bot(request)
    principal = await principal_optional_from_request(request)
    acc_id = await account_id_from_request(request)
    user_id = principal.user_id if principal else None

    # Auto-heal/sync unclosed records in Supabase if local history is present
    if hasattr(repository, "sync_closed_trades") and current_bot.history:
        asyncio.create_task(asyncio.to_thread(repository.sync_closed_trades, current_bot.history))

    # Get user trades from repository if available
    user_trades = None
    if hasattr(repository, "get_user_trades"):
        user_trades = repository.get_user_trades(acc_id, user_id, limit=300, period=period)

    return current_bot.get_history_analytics(period=period, trades=user_trades)

@app.get("/api/user/trades")
async def get_user_trades_endpoint(request: Request, limit: int = 100, period: str = "all"):
    principal = await principal_from_request(request)
    if hasattr(repository, "get_user_trades"):
        trades = repository.get_user_trades(principal.account_id, principal.user_id, limit=limit, period=period)
    elif hasattr(repository, "recent_trades"):
        trades = repository.recent_trades(principal.account_id, principal.user_id, limit=limit)
    else:
        trades = []
    
    total = len(trades)
    wins = [t for t in trades if float(t.get("profit") or 0) > 0]
    losses = [t for t in trades if float(t.get("profit") or 0) <= 0]
    total_profit = sum(float(t.get("profit") or 0) for t in trades)
    win_rate = (len(wins) / total * 100) if total > 0 else 0.0

    return {
        "trades": trades,
        "total_trades": total,
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate": round(win_rate, 2),
        "total_profit": round(total_profit, 2)
    }

# Active WebSocket connections list
active_connections: list[WebSocket] = []

class SettingsModel(BaseModel):
    symbol: str
    risk_percent: float
    max_spread: int
    max_daily_loss_percent: float
    trailing_stop_points: int
    trailing_step_points: int
    trailing_stop_offset_points: int
    breakeven_trigger_points: int
    breakeven_buffer_points: int
    news_restriction_minutes: int
    auto_trading: bool
    max_open_trades: int
    cooldown_duration: int
    roi_enabled: bool
    roi_table: str

class ManualTradeModel(BaseModel):
    type: Literal["BUY", "SELL", "BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP"]
    lot_size: float
    exec_type: Optional[str] = "MARKET" # MARKET or LIMIT
    trigger_price: Optional[float] = 0.0
    sl_points: Optional[float] = 0.0
    tp_points: Optional[float] = 0.0
    sl_price: Optional[float] = 0.0
    tp_price: Optional[float] = 0.0
    symbol: Optional[str] = None

class ModifySLTPModel(BaseModel):
    sl: float
    tp: float

class SymbolToggleModel(BaseModel):
    symbol: Optional[str] = None
    enabled: Optional[bool] = None
    preset: Optional[str] = None

def render_template(name: str) -> HTMLResponse:
    html_path = os.path.join(os.path.dirname(__file__), "templates", name)
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content=f"<h1>Template {name} not found!</h1>", status_code=404)


def render_auth(title: str, heading: str, action: str, footer: str, password: bool = True) -> HTMLResponse:
    path = os.path.join(os.path.dirname(__file__), "templates", "auth.html")
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    values = {"title": title, "heading": heading, "action": action, "footer": footer,
              "password": "" if password else "<!--"}
    content = content.replace("{{ title }}", values["title"]).replace("{{ heading }}", values["heading"])
    content = content.replace("{{ action }}", values["action"]).replace("{{ footer|safe }}", values["footer"])
    if not password:
        content = content.replace("{% if password %}", "").replace("{% endif %}", "").replace("<!--", "").replace("-->", "")
    else:
        content = content.replace("{% if password %}", "").replace("{% endif %}", "")
    config = {"url": os.getenv("SUPABASE_URL", ""), "anonKey": os.getenv("SUPABASE_ANON_KEY", "")}
    content = content.replace("<script src=\"/static/auth.js\"></script>", f"<script>window.__SUPABASE_CONFIG__={config!r};</script><script src=\"/static/auth.js\"></script>")
    return HTMLResponse(content=content)


@app.get("/tunnel-transition")
async def get_tunnel_transition():
    return render_template("tunnel-transition.html")


@app.get("/")
async def get_landing():
    return render_template("index.html")


@app.get("/app")
async def get_dashboard():
    return render_template("index.html")


@app.get("/app_landing")
@app.get("/our-app")
@app.get("/our_app")
@app.get("/mobile")
@app.get("/mobile_showcase")
async def get_app_landing():
    return render_template("app_landing.html")


@app.get("/about")
@app.get("/about-us")
async def get_about():
    return render_template("about.html")


@app.get("/pricing")
async def get_pricing():
    return render_template("pricing.html")


@app.get("/faq")
@app.get("/qa")
@app.get("/q-and-a")
async def get_faq():
    return render_template("faq.html")


@app.get("/login")
async def get_login():
    return render_auth("Log in", "Welcome back", "Log in", 'New here? <a href="/register">Create an account</a>')


@app.get("/register")
async def get_register():
    return render_auth("Create account", "Build your workspace", "Create account", 'Already registered? <a href="/login">Log in</a>')


@app.get("/forgot-password")
async def get_forgot_password():
    return render_auth("Reset password", "Recover access", "Send reset link", 'Remembered it? <a href="/login">Log in</a>', False)


@app.get("/auth/callback")
async def get_auth_callback():
    """Landing page for GitHub OAuth / magic-link redirects (token in URL hash)."""
    return render_template("auth_callback.html")

# WebSocket Endpoint for streaming real-time metrics
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    requested_account_id = websocket.query_params.get("account_id")
    token = websocket.query_params.get("token", "") or websocket.cookies.get("session_token", "")
    account_id = requested_account_id
    require_auth = os.getenv("REQUIRE_AUTH", "false").lower() == "true" or os.getenv("ENVIRONMENT", "development").lower() == "production"
    if require_auth or token:
        if not token:
            security_logger.warning("Unauthenticated WebSocket connection attempt blocked.")
            await websocket.close(code=1008, reason="Authentication required")
            return
        try:
            principal = await _session_authenticate(session_service, token)
        except AuthenticationError:
            security_logger.warning("Invalid token provided for WebSocket connection.")
            await websocket.close(code=1008, reason="Invalid token")
            return
        # The session is authoritative when the client does not yet have the
        # account id (for example, a page loaded from an older cached bundle).
        account_id = principal.account_id
        if requested_account_id and principal.account_id != requested_account_id:
            await websocket.close(code=1008, reason="Account mismatch")
            return
    account_id = account_id or os.getenv("DEFAULT_ACCOUNT_ID", "demo-account")
    try:
        stream_bot = account_service.get_bot(account_id)
        if not hasattr(stream_bot, "_price_feed_task") or stream_bot._price_feed_task is None or stream_bot._price_feed_task.done():
            stream_bot._price_feed_task = asyncio.create_task(stream_bot.start_price_feed_loop())
    except Exception as exc:
        await websocket.close(code=4404, reason=str(exc)[:120])
        return
    await websocket.accept()
    active_connections.append(websocket)
    try:
        while True:
            # Keep the live payload bounded; history has a dedicated endpoint.
            live = getattr(stream_bot, "live_state", None) or {}
            state = {
                "account_id": account_id,
                "is_running": stream_bot.is_running,
                "simulation_mode": stream_bot.simulation_mode,
                "system_locked": stream_bot.system_locked,
                "lock_state": stream_bot.lock_state,
                "lock_reason": stream_bot.lock_reason,
                "is_pending_order": stream_bot.is_pending_order,
                "symbol": live.get("symbol", stream_bot.symbol),
                "risk_percent": stream_bot.risk_percent,
                "max_spread": stream_bot.max_spread,
                "max_daily_loss_percent": stream_bot.max_daily_loss_percent,
                "trailing_stop_points": stream_bot.trailing_stop_points,
                "trailing_step_points": stream_bot.trailing_step_points,
                "trailing_stop_offset_points": stream_bot.trailing_stop_offset_points,
                "breakeven_trigger_points": stream_bot.breakeven_trigger_points,
                "breakeven_buffer_points": stream_bot.breakeven_buffer_points,
                "news_restriction_minutes": stream_bot.news_restriction_minutes,
                "auto_trading": stream_bot.auto_trading,
                "enabled_symbols": stream_bot.enabled_symbols,
                "max_open_trades": stream_bot.max_open_trades,
                "cooldown_duration": stream_bot.cooldown_duration,
                "roi_enabled": stream_bot.roi_enabled,
                "roi_table": stream_bot.roi_table,
                "pair_locks": stream_bot.pair_locks,
                "current_price": live.get("current_price", stream_bot.current_price),
                "account_info": live.get("account_info", stream_bot.account_info),
                "positions": live.get("positions", stream_bot.positions),
                "pending_orders": live.get("pending_orders", stream_bot.pending_orders),
                "news_events": stream_bot.news_events,
                "recent_logs": stream_bot.recent_logs,
                "sr_levels": live.get("sr_levels", stream_bot.sr_levels),
                "fib_levels": live.get("fib_levels", stream_bot.fib_levels),
                "confluence_zones": live.get("confluence_zones", stream_bot.confluence_zones),
                "active_signals": live.get("active_signals", stream_bot.active_signals),
                "trade_history": stream_bot.history[-100:],
                "statistics": stream_bot.get_statistics(),
                "indicators": live.get("indicators", stream_bot.indicators),
                "watchlist": live.get("watchlist", stream_bot.watchlist_data)
            }
            await websocket.send_json(state)
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        active_connections.remove(websocket)
    except Exception as e:
        if websocket in active_connections:
            active_connections.remove(websocket)

# Start Bot API
@app.post("/api/start")
@limiter.limit("10/minute")
async def start_bot(request: Request):
    global bot_task
    current_bot = await scoped_bot(request)
    if current_bot.lock_state == LOCK_HARD:
        raise HTTPException(status_code=423, detail="Trading system is hard-locked; unlock before starting")
    if current_bot.is_running:
        return {"status": "already_running", "simulation_mode": current_bot.simulation_mode}
    
    # Try initialization of MT5
    await current_bot.initialize_mt5()
    await current_bot.fetch_news_feed()
    # Kick off the price-feed loop for this bot so quotes flow immediately
    asyncio.create_task(current_bot.start_price_feed_loop())
    
    # Run the bot in a background task
    bot_task = asyncio.create_task(current_bot.start())
    return {"status": "started", "simulation_mode": current_bot.simulation_mode}

# Stop Bot API
@app.post("/api/stop")
@limiter.limit("10/minute")
async def stop_bot(request: Request):
    current_bot = await scoped_bot(request)
    if not current_bot.is_running:
        return {"status": "already_stopped"}
    await current_bot.stop()
    return {"status": "stopped"}

# Toggle simulation mode
class SimulationToggleModel(BaseModel):
    enabled: Optional[bool] = None

@app.post("/api/simulation/toggle")
@limiter.limit("10/minute")
async def toggle_simulation(request: Request, payload: Optional[SimulationToggleModel] = None):
    current_bot = await scoped_bot(request)
    if current_bot.is_running:
        raise HTTPException(status_code=409, detail="Stop the bot before changing simulation mode")
    if payload is not None and payload.enabled is not None:
        current_bot.simulation_mode = payload.enabled
    else:
        current_bot.simulation_mode = not current_bot.simulation_mode
    return {"simulation_mode": current_bot.simulation_mode}

# Get Bot State API
@app.get("/api/state")
async def get_state(request: Request):
    current_bot = await scoped_bot(request)
    live = getattr(current_bot, "live_state", None) or {}
    return {
        "is_running": current_bot.is_running,
        "simulation_mode": current_bot.simulation_mode,
        "system_locked": current_bot.system_locked,
        "lock_state": current_bot.lock_state,
        "lock_reason": current_bot.lock_reason,
        "is_pending_order": current_bot.is_pending_order,
        "symbol": live.get("symbol", current_bot.symbol),
        "settings": {
            "risk_percent": current_bot.risk_percent,
            "max_spread": current_bot.max_spread,
            "max_daily_loss_percent": current_bot.max_daily_loss_percent,
            "trailing_stop_points": current_bot.trailing_stop_points,
            "trailing_step_points": current_bot.trailing_step_points,
            "trailing_stop_offset_points": current_bot.trailing_stop_offset_points,
            "breakeven_trigger_points": current_bot.breakeven_trigger_points,
            "breakeven_buffer_points": current_bot.breakeven_buffer_points,
            "news_restriction_minutes": current_bot.news_restriction_minutes,
            "auto_trading": current_bot.auto_trading,
            "enabled_symbols": current_bot.enabled_symbols,
            "max_open_trades": current_bot.max_open_trades,
            "cooldown_duration": current_bot.cooldown_duration,
            "roi_enabled": current_bot.roi_enabled,
            "roi_table": ",".join([f"{k}:{v}" for k, v in current_bot.roi_table.items()])
        },
        "pair_locks": current_bot.pair_locks,
        "current_price": live.get("current_price", current_bot.current_price),
        "account_info": live.get("account_info", current_bot.account_info),
        "positions": live.get("positions", current_bot.positions),
        "news_events": current_bot.news_events,
        "recent_logs": current_bot.recent_logs,
        "sr_levels": live.get("sr_levels", current_bot.sr_levels),
        "fib_levels": live.get("fib_levels", current_bot.fib_levels),
        "confluence_zones": live.get("confluence_zones", current_bot.confluence_zones),
        "active_signals": live.get("active_signals", current_bot.active_signals),
        "trade_history": current_bot.history[-100:] if current_bot.history else [],
        "statistics": current_bot.get_statistics(),
        "indicators": live.get("indicators", current_bot.indicators),
        "watchlist": live.get("watchlist", current_bot.watchlist_data)
    }

# Update Settings API
@app.post("/api/settings")
@limiter.limit("30/minute")
async def update_settings(settings: SettingsModel, request: Request):
    bot = await scoped_bot(request)
    settings.symbol = settings.symbol.upper().strip()
    if settings.symbol not in SUPPORTED_SYMBOLS:
        raise HTTPException(status_code=422, detail="Unsupported symbol")
    if not 0 < settings.risk_percent <= 100:
        raise HTTPException(status_code=422, detail="Risk percentage must be between 0 and 100")
    bot.symbol = settings.symbol
    bot.risk_percent = settings.risk_percent
    bot.max_spread = settings.max_spread
    bot.max_daily_loss_percent = settings.max_daily_loss_percent
    bot.trailing_stop_points = settings.trailing_stop_points
    bot.trailing_step_points = settings.trailing_step_points
    bot.trailing_stop_offset_points = settings.trailing_stop_offset_points
    bot.breakeven_trigger_points = settings.breakeven_trigger_points
    bot.breakeven_buffer_points = settings.breakeven_buffer_points
    bot.news_restriction_minutes = settings.news_restriction_minutes
    bot.auto_trading = settings.auto_trading
    bot.max_open_trades = settings.max_open_trades
    bot.cooldown_duration = settings.cooldown_duration
    bot.roi_enabled = settings.roi_enabled
    
    # Parse roi_table string to dict
    try:
        new_roi = {}
        for item in settings.roi_table.split(","):
            if not item.strip():
                continue
            k, v = item.split(":")
            new_roi[int(k)] = float(v)
        if new_roi:
            bot.roi_table = new_roi
    except Exception as e:
        await bot.log_event("WARNING", f"Invalid ROI table string '{settings.roi_table}' provided. Error: {e}")

    await bot.log_event("SETTINGS", f"Settings updated by User. Symbol: {bot.symbol}, Risk: {bot.risk_percent}%, Max Spread: {bot.max_spread}, Max Loss: {bot.max_daily_loss_percent}%, Trailing Stop: {bot.trailing_stop_points}, Trailing Step: {bot.trailing_step_points}, Trailing Offset: {bot.trailing_stop_offset_points}, Breakeven Trigger: {bot.breakeven_trigger_points}, Breakeven Buffer: {bot.breakeven_buffer_points}, News Restriction: {bot.news_restriction_minutes}m, Auto Trading: {bot.auto_trading}, Max Open Trades: {bot.max_open_trades}, Cooldown: {bot.cooldown_duration}s, ROI Enabled: {bot.roi_enabled}, ROI Table: {bot.roi_table}")
    return {"status": "success", "settings": settings}

# Symbol Toggle Auto-Trading Endpoint
@app.post("/api/symbol-toggle")
@limiter.limit("30/minute")
async def symbol_toggle_endpoint(data: SymbolToggleModel, request: Request):
    current_bot = await scoped_bot(request)
    if data.preset == "XAUUSD_ONLY":
        for sym in current_bot.enabled_symbols:
            current_bot.enabled_symbols[sym] = (sym == "XAUUSD")
        await current_bot.log_event("SETTINGS", "Target Auto-Trade Symbol preset set to ONLY XAUUSD.")
    elif data.preset == "ALL_ON":
        for sym in current_bot.enabled_symbols:
            current_bot.enabled_symbols[sym] = True
        await current_bot.log_event("SETTINGS", "Target Auto-Trade Symbol preset set to ALL ON.")
    elif data.symbol and data.enabled is not None:
        data.symbol = data.symbol.upper().strip()
        if data.symbol not in SUPPORTED_SYMBOLS:
            raise HTTPException(status_code=422, detail="Unsupported symbol")
        current_bot.enabled_symbols[data.symbol] = data.enabled
        status_str = "ENABLED" if data.enabled else "DISABLED"
        await current_bot.log_event("SETTINGS", f"Auto-Trading for {data.symbol} set to {status_str}.")
    return {"status": "success", "enabled_symbols": current_bot.enabled_symbols}

# Trigger Manual Trade
@app.post("/api/trade")
@limiter.limit("20/minute")
async def manual_trade(trade: ManualTradeModel, request: Request):
    bot = await scoped_bot(request)
    if bot.trades_blocked:
        raise HTTPException(status_code=423, detail=f"Trading system is locked ({bot.lock_state})")
    if trade.lot_size <= 0:
        raise HTTPException(status_code=422, detail="Lot size must be positive")
    
    # Run filter check for manual trade
    current_bot = await scoped_bot(request)
    passed = await current_bot.check_filters(trade.type, is_manual=True, symbol=trade.symbol)
    if not passed:
        raise HTTPException(status_code=400, detail="Trade rejected by risk filters (spread/drawdown/news).")

    sym = (trade.symbol or current_bot.symbol).upper().strip()
    if sym not in SUPPORTED_SYMBOLS:
        raise HTTPException(status_code=422, detail="Unsupported symbol")
    if trade.sl_points is not None and trade.sl_points < 0:
        raise HTTPException(status_code=422, detail="Stop-loss points must not be negative")
    if trade.tp_points is not None and trade.tp_points < 0:
        raise HTTPException(status_code=422, detail="Take-profit points must not be negative")
    point = current_bot.get_symbol_point(sym)
    sym_price = current_bot.get_current_price_for_symbol(sym)
    
    # Check if this is a Limit / Pending order
    is_limit = trade.exec_type == "LIMIT" or (trade.trigger_price and trade.trigger_price > 0) or ("LIMIT" in trade.type) or ("STOP" in trade.type)

    if is_limit:
        trig_price = trade.trigger_price or (sym_price["ask"] if "BUY" in trade.type else sym_price["bid"])
        if trade.sl_price and trade.sl_price > 0:
            valid_sl = trade.sl_price < trig_price if "BUY" in trade.type else trade.sl_price > trig_price
            if not valid_sl:
                raise HTTPException(status_code=422, detail="Stop-loss is on the wrong side of the trigger price")
        if trade.tp_price and trade.tp_price > 0:
            valid_tp = trade.tp_price > trig_price if "BUY" in trade.type else trade.tp_price < trig_price
            if not valid_tp:
                raise HTTPException(status_code=422, detail="Take-profit is on the wrong side of the trigger price")
        pending = await current_bot.add_pending_order(
            order_type=trade.type,
            lot_size=trade.lot_size,
            trigger_price=trig_price,
            sl_points=trade.sl_points or 0.0,
            tp_points=trade.tp_points or 0.0,
            sl_price=trade.sl_price or 0.0,
            tp_price=trade.tp_price or 0.0,
            symbol=sym
        )
        return {"status": "pending_order_submitted", "ticket": pending["ticket"]}

    open_price = sym_price["ask"] if trade.type == "BUY" else sym_price["bid"]

    if trade.sl_price and trade.sl_price > 0:
        valid_sl = trade.sl_price < open_price if trade.type == "BUY" else trade.sl_price > open_price
        if not valid_sl:
            raise HTTPException(status_code=422, detail="Stop-loss is on the wrong side of the entry price")

    if trade.tp_price and trade.tp_price > 0:
        valid_tp = trade.tp_price > open_price if trade.type == "BUY" else trade.tp_price < open_price
        if not valid_tp:
            raise HTTPException(status_code=422, detail="Take-profit is on the wrong side of the entry price")

    sl_pts = trade.sl_points or 0.0
    tp_pts = trade.tp_points or 0.0

    if trade.sl_price and trade.sl_price > 0:
        sl_pts = round(abs(open_price - trade.sl_price) / point, 1)

    if trade.tp_price and trade.tp_price > 0:
        tp_pts = round(abs(open_price - trade.tp_price) / point, 1)

    # Place order as background task (non-blocking)
    asyncio.create_task(current_bot.execute_market_trade(
        order_type=trade.type,
        lot_size=trade.lot_size,
        sl_points=sl_pts,
        tp_points=tp_pts,
        symbol=sym
    ))
    return {"status": "order_submitted", "sl_points": sl_pts, "tp_points": tp_pts}

# Cancel Pending Order
@app.post("/api/pending/cancel/{ticket}")
@limiter.limit("30/minute")
async def cancel_pending_order_endpoint(ticket: int, request: Request):
    current_bot = await scoped_bot(request)
    success = await current_bot.cancel_pending_order(ticket)
    if not success and bot != current_bot:
        success = await bot.cancel_pending_order(ticket)
    if not success:
        raise HTTPException(status_code=404, detail="Pending order not found.")
    return {"status": "success", "ticket": ticket}

# Force Close Position
@app.post("/api/close/{ticket}")
@limiter.limit("30/minute")
async def close_position_endpoint(ticket: int, request: Request):
    current_bot = await scoped_bot(request)
    await current_bot.close_position(ticket)
    if bot != current_bot:
        await bot.close_position(ticket)
    # Also ensure position is cleared from any active session bots
    if hasattr(account_service, "_sessions"):
        for session in list(account_service._sessions.values()):
            if session.bot != current_bot and session.bot != bot:
                await session.bot.close_position(ticket)
    return {"status": "close_submitted", "ticket": ticket}

# Force Close All Positions
@app.post("/api/close-all")
@limiter.limit("10/minute")
async def close_all_positions_endpoint(request: Request):
    current_bot = await scoped_bot(request)
    await current_bot.close_all_positions()
    if bot != current_bot:
        await bot.close_all_positions()
    if hasattr(account_service, "_sessions"):
        for session in list(account_service._sessions.values()):
            if session.bot != current_bot and session.bot != bot:
                await session.bot.close_all_positions()
    return {"status": "close_all_submitted"}

# Modify SL/TP of an open position
@app.post("/api/modify-sltp/{ticket}")
@limiter.limit("30/minute")
async def modify_sltp_endpoint(ticket: int, data: ModifySLTPModel, request: Request):
    current_bot = await scoped_bot(request)
    success = await current_bot.modify_position_sltp(ticket, data.sl, data.tp)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to modify SL/TP. Position not found or broker rejected.")
    return {"status": "modified", "ticket": ticket, "sl": data.sl, "tp": data.tp}

# Manual Trigger Circuit Breaker (Hard Lockdown)
@app.post("/api/circuit-breaker/trigger")
@limiter.limit("10/minute")
async def manual_trigger_circuit_breaker(request: Request):
    current_bot = await scoped_bot(request)
    await current_bot.hard_lock("Manual circuit breaker lockdown")
    return {"status": "locked", "lock_state": current_bot.lock_state}

# Reset Circuit Breaker (Unlock)
@app.post("/api/circuit-breaker/reset")
@limiter.limit("10/minute")
async def reset_circuit_breaker(request: Request):
    current_bot = await scoped_bot(request)
    current_bot.daily_start_equity = current_bot.account_info["balance"]
    current_bot.account_info["daily_start_equity"] = current_bot.daily_start_equity
    current_bot.account_info["daily_drawdown_percent"] = 0.0
    current_bot.unlock("Circuit breaker manual reset")
    return {"status": "unlocked", "lock_state": current_bot.lock_state}

# Unified persisted lock control (soft_lock | hard_lock | unlock).
class LockModel(BaseModel):
    state: Literal["soft_locked", "hard_locked", "unlocked"]
    reason: Optional[str] = None

@app.post("/api/lock")
@limiter.limit("10/minute")
async def set_lock_endpoint(payload: LockModel, request: Request):
    current_bot = await scoped_bot(request)
    if payload.state == LOCK_HARD:
        await current_bot.hard_lock(payload.reason)
    elif payload.state == LOCK_SOFT:
        current_bot.soft_lock(payload.reason)
    else:
        current_bot.unlock(payload.reason)
    return {"status": "ok", "lock_state": current_bot.lock_state, "lock_reason": current_bot.lock_reason}

# Multi-Agent Iterative SDLC Loop Endpoint
@app.post("/api/agents/sdlc-loop")
@limiter.limit("5/minute")
async def run_sdlc_loop_endpoint(task: AgentTaskModel, request: Request):
    if not agents_enabled():
        raise HTTPException(status_code=404, detail="SDLC agents are disabled")
    principal = await principal_from_request(request)
    if "admin" not in principal.roles and "trader" not in principal.roles:
        raise HTTPException(status_code=403, detail="Unauthorized role for SDLC agents")
    if not task.prompt.strip() or not 1 <= (task.max_retries or 0) <= 5:
        raise HTTPException(status_code=422, detail="Invalid agent task")
    from agents.manager import ManagerAgent
    agent_manager = ManagerAgent()
    try:
        with open(__file__, "r", encoding="utf-8") as f:
            current_code = f.read()
    except Exception:
        current_code = ""

    result = await agent_manager.run_sdlc_loop(
        user_request=task.prompt,
        current_code=current_code,
        max_retries=task.max_retries or 3
    )
    return {"status": "success", "result": result}


# ============================================================================
# Admin Dashboard
# ============================================================================
class AdminStatusModel(BaseModel):
    status: Literal["active", "blocked", "suspended", "archived"]
    reason: Optional[str] = None


class AdminLockModel(BaseModel):
    state: Literal["soft_locked", "hard_locked", "unlocked"]
    reason: Optional[str] = None


class AdminRolesModel(BaseModel):
    roles: list[str]


class AdminCreateUserModel(BaseModel):
    email: str
    password: str
    display_name: Optional[str] = None
    role: Literal["trader", "admin"] = "trader"
    bot_type_id: Optional[str] = None


class AdminUpdateUserModel(BaseModel):
    display_name: Optional[str] = None
    password: Optional[str] = None
    role: Optional[Literal["trader", "admin"]] = None
    status: Optional[Literal["active", "suspended", "blocked", "archived"]] = None


class EmergencyCloseAllModel(BaseModel):
    halt_bots: bool = True
    reason: str = "Market sweep protection emergency trigger"


@app.get("/admin/login")
async def get_admin_login_page():
    return render_template("admin_login.html")

@app.get("/admin")
async def get_admin_page():
    return render_template("admin_dashboard.html")


def _partner_rates() -> dict[str, float]:
    """Exness-like IB rates (USD / lot). Override via env without DB migration."""
    return {
        "commission_per_lot": float(os.getenv("PARTNER_COMMISSION_PER_LOT", "5")),
        "backcom_per_lot": float(os.getenv("PARTNER_BACKCOM_PER_LOT", "1.5")),
        "platform_fee_pct": float(os.getenv("PARTNER_PLATFORM_FEE_PCT", "10")),
    }


def _day_key(raw) -> str:
    s = str(raw or "")
    return s[:10] if len(s) >= 10 else ""


def _daterange_days(n: int) -> list[str]:
    from datetime import timedelta
    today = datetime.now(timezone.utc).date()
    return [(today - timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]


def _partner_analytics(users: list, trades: list) -> dict:
    """Partner / IB style metrics: registrations, volume, commission, backcom, bot P&L."""
    rates = _partner_rates()
    c_lot = rates["commission_per_lot"]
    b_lot = rates["backcom_per_lot"]
    fee_pct = rates["platform_fee_pct"] / 100.0

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    days_7 = set(_daterange_days(7))
    days_30 = _daterange_days(30)
    days_30_set = set(days_30)

    # --- Users ---
    joined_by_day: dict[str, int] = {}
    users_today = users_7d = 0
    for u in users:
        day = _day_key(u.get("created_at"))
        if not day:
            continue
        joined_by_day[day] = joined_by_day.get(day, 0) + 1
        if day == today:
            users_today += 1
        if day in days_7:
            users_7d += 1

    # --- Trades ---
    closed = [t for t in trades if t.get("status") == "closed"]
    daily: dict[str, dict[str, float]] = {}
    for d in days_30:
        daily[d] = {"volume": 0.0, "bot_profit": 0.0, "commission": 0.0, "backcom": 0.0, "platform_fee": 0.0, "trades": 0}

    total_volume = total_bot_profit = 0.0
    vol_today = vol_7d = 0.0
    profit_today = profit_7d = 0.0
    commission_total = backcom_total = platform_fee_total = 0.0
    commission_today = commission_7d = 0.0
    backcom_today = backcom_7d = 0.0

    for t in closed:
        day = _day_key(t.get("closed_at") or t.get("created_at"))
        vol = float(t.get("quantity") or 0)
        profit = float(t.get("profit") or 0)
        commission = round(vol * c_lot, 4)
        backcom = round(vol * b_lot, 4)
        platform_fee = round(max(profit, 0.0) * fee_pct, 4)

        total_volume += vol
        total_bot_profit += profit
        commission_total += commission
        backcom_total += backcom
        platform_fee_total += platform_fee

        if day == today:
            vol_today += vol
            profit_today += profit
            commission_today += commission
            backcom_today += backcom
        if day in days_7:
            vol_7d += vol
            profit_7d += profit
            commission_7d += commission
            backcom_7d += backcom

        if day in days_30_set:
            bucket = daily[day]
            bucket["volume"] += vol
            bucket["bot_profit"] += profit
            bucket["commission"] += commission
            bucket["backcom"] += backcom
            bucket["platform_fee"] += platform_fee
            bucket["trades"] += 1

    net_ib = commission_total - backcom_total
    net_platform = net_ib + platform_fee_total

    series = []
    cum_commission = cum_backcom = cum_bot = 0.0
    for d in days_30:
        row = daily[d]
        cum_commission += row["commission"]
        cum_backcom += row["backcom"]
        cum_bot += row["bot_profit"]
        series.append({
            "date": d,
            "registrations": joined_by_day.get(d, 0),
            "volume": round(row["volume"], 4),
            "bot_profit": round(row["bot_profit"], 2),
            "commission": round(row["commission"], 2),
            "backcom": round(row["backcom"], 2),
            "platform_fee": round(row["platform_fee"], 2),
            "trades": int(row["trades"]),
            "cum_commission": round(cum_commission, 2),
            "cum_backcom": round(cum_backcom, 2),
            "cum_bot_profit": round(cum_bot, 2),
        })

    return {
        "rates": rates,
        "summary": {
            "users_total": len(users),
            "users_today": users_today,
            "users_7d": users_7d,
            "volume_lots_total": round(total_volume, 4),
            "volume_lots_today": round(vol_today, 4),
            "volume_lots_7d": round(vol_7d, 4),
            "bot_profit_total": round(total_bot_profit, 2),
            "bot_profit_today": round(profit_today, 2),
            "bot_profit_7d": round(profit_7d, 2),
            "commission_total": round(commission_total, 2),
            "commission_today": round(commission_today, 2),
            "commission_7d": round(commission_7d, 2),
            "backcom_total": round(backcom_total, 2),
            "backcom_today": round(backcom_today, 2),
            "backcom_7d": round(backcom_7d, 2),
            "platform_fee_total": round(platform_fee_total, 2),
            "net_ib_income": round(net_ib, 2),
            "net_platform_income": round(net_platform, 2),
            "closed_trades": len(closed),
        },
        "daily_series": series,
    }


def _admin_aggregates(accounts, trades):
    """Compute dashboard KPIs + chart series from raw account/trade rows."""
    total_accounts = len(accounts)
    active = sum(1 for a in accounts if a.get("status") == "active")
    blocked = sum(1 for a in accounts if a.get("status") == "blocked")
    suspended = sum(1 for a in accounts if a.get("status") == "suspended")
    locked = sum(1 for a in accounts if a.get("lock_state") != "unlocked")
    users = {str(a.get("owner_user_id")) for a in accounts if a.get("owner_user_id")}

    closed = [t for t in trades if t.get("status") == "closed"]
    wins = [t for t in closed if (t.get("profit") or 0) > 0]
    losses = [t for t in closed if (t.get("profit") or 0) < 0]
    total_profit = sum(t.get("profit") or 0 for t in closed)
    total_volume = sum(t.get("quantity") or 0 for t in closed)
    total_trades = len(closed)
    win_rate = (len(wins) / total_trades * 100) if total_trades else 0.0

    symbol_stats: dict[str, dict] = {}
    for t in closed:
        s = t.get("symbol", "?")
        bucket = symbol_stats.setdefault(s, {"trades": 0, "volume": 0.0, "profit": 0.0, "wins": 0})
        bucket["trades"] += 1
        bucket["volume"] += t.get("quantity") or 0
        bucket["profit"] += t.get("profit") or 0
        if (t.get("profit") or 0) > 0:
            bucket["wins"] += 1
    symbols = [{"symbol": k, **v} for k, v in sorted(symbol_stats.items(), key=lambda kv: kv[1]["profit"], reverse=True)]

    per_account: dict[str, dict] = {}
    for a in accounts:
        if a.get("id"):
            per_account[a["id"]] = {"id": a["id"], "display_name": a.get("display_name"), "profit": 0.0, "trades": 0, "bot_type_name": a.get("bot_type_name")}
    for t in closed:
        acc = per_account.setdefault(t.get("account_id"), {"id": t.get("account_id"), "display_name": None, "profit": 0.0, "trades": 0})
        acc["profit"] += t.get("profit") or 0
        acc["trades"] += 1
    top_accounts = sorted(per_account.values(), key=lambda x: x["profit"], reverse=True)[:10]

    daily: dict[str, float] = {}
    for t in sorted(closed, key=lambda x: str(x.get("closed_at") or "")):
        day = str(t.get("closed_at") or "")[:10]
        if day:
            daily[day] = daily.get(day, 0.0) + (t.get("profit") or 0)

    status_dist = [
        {"label": "Active", "value": active},
        {"label": "Blocked", "value": blocked},
        {"label": "Suspended", "value": suspended},
    ]
    return {
        "summary": {
            "total_accounts": total_accounts,
            "total_users": len(users),
            "active": active,
            "blocked": blocked,
            "suspended": suspended,
            "locked": locked,
            "total_trades": total_trades,
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate": round(win_rate, 2),
            "total_profit": round(total_profit, 2),
            "total_volume": round(total_volume, 2),
            "today_profit": round(sum(t.get("profit") or 0 for t in closed if str(t.get("closed_at") or "")[:10] == datetime.now(timezone.utc).strftime("%Y-%m-%d")), 2),
        },
        "daily_profit": [{"date": d, "profit": round(v, 2)} for d, v in sorted(daily.items())],
        "top_accounts": [{**a, "profit": round(a["profit"], 2)} for a in top_accounts],
        "symbol_stats": [{**s, "profit": round(s["profit"], 2), "volume": round(s["volume"], 2)} for s in symbols],
        "status_distribution": status_dist,
    }


@app.get("/api/admin/overview")
@limiter.limit("120/minute")
async def admin_overview(request: Request, q: str = "", status: str = "", lock_state: str = "", page: int = 1, per_page: int = 50):
    """Combined admin payload: KPIs/charts + filtered accounts table in ONE request.

    Phase 2: prefers the SQL view ``admin_account_summary`` (pre-aggregated
    per-account stats, pre-joined with bot_types) — replacing the
    O(all-trades) Python-side aggregation with a single indexed SQL query.
    Falls back to the legacy path if the view doesn't exist (migration not run).
    """
    await require_admin(request)
    accounts_fut = asyncio.to_thread(repository.list_accounts) if hasattr(repository, "list_accounts") else None
    trades_fut = asyncio.to_thread(repository.get_all_trades) if hasattr(repository, "get_all_trades") else None
    users_fut = asyncio.to_thread(repository.list_user_profiles) if hasattr(repository, "list_user_profiles") else None

    accounts, trades, users = [], [], []
    futs = [f for f in (accounts_fut, trades_fut, users_fut) if f]
    if futs:
        results = await asyncio.gather(*futs)
        idx = 0
        if accounts_fut:
            accounts = results[idx]; idx += 1
        if trades_fut:
            trades = results[idx]; idx += 1
        if users_fut:
            users = results[idx]

    data = _admin_aggregates(accounts, trades)
    data["partner"] = _partner_analytics(users, trades)

    # Phase 2: try the SQL view for enriched per-account stats (pre-joined).
    enriched_accounts = None
    if hasattr(repository, "get_account_summary"):
        try:
            enriched_accounts = await asyncio.to_thread(repository.get_account_summary)
        except Exception:
            pass  # migration not run — legacy path below

    filtered = enriched_accounts if enriched_accounts else accounts
    if q:
        q_lower = q.lower()
        filtered = [a for a in filtered if q_lower in str(a.get("id", "")).lower() or q_lower in str(a.get("display_name", "")).lower()]
    if status:
        filtered = [a for a in filtered if a.get("status") == status or a.get("account_status") == status]
    if lock_state:
        filtered = [a for a in filtered if a.get("lock_state") == lock_state]
    filtered = sorted(filtered, key=lambda a: str(a.get("created_at") or ""), reverse=True)

    # Only enrich + join when NOT using the view (view already has bot_type)
    if not enriched_accounts:
        filtered = _enrich_account_stats(filtered, trades)
        if hasattr(repository, "get_bot_types"):
            bot_types_map = {bt["id"]: bt for bt in repository.get_bot_types()}
            for a in filtered:
                bt_id = a.get("bot_type_id")
                if bt_id and bt_id in bot_types_map:
                    bt = bot_types_map[bt_id]
                    a["bot_type_name"] = bt["name"]
                    a["bot_type_risk"] = bt["risk_level"]

    total = len(filtered)
    page = max(1, page)
    per_page = max(1, min(per_page, 200))
    start = (page - 1) * per_page
    data["accounts_section"] = {"total": total, "page": page, "per_page": per_page, "accounts": filtered[start:start + per_page]}
    return data


@app.get("/api/admin/partner-analytics")
@limiter.limit("60/minute")
async def admin_partner_analytics(request: Request):
    """Exness-style partner metrics: registrations, lot volume, commission, backcom, bot P&L."""
    await require_admin(request)
    users = await asyncio.to_thread(repository.list_user_profiles) if hasattr(repository, "list_user_profiles") else []
    trades = await asyncio.to_thread(repository.get_all_trades) if hasattr(repository, "get_all_trades") else []
    return _partner_analytics(users, trades)


def _enrich_account_stats(accounts, trades):
    """Attach per-account trade counts / win rate / PnL / volume / open positions to a list."""
    closed = [t for t in trades if t.get("status") == "closed"]
    agg: dict[str, dict] = {}
    for t in closed:
        acc = t.get("account_id")
        if not acc:
            continue
        a = agg.setdefault(acc, {"total": 0, "wins": 0, "profit": 0.0, "volume": 0.0})
        a["total"] += 1
        a["volume"] += t.get("quantity") or 0
        p = t.get("profit") or 0
        a["profit"] += p
        if p > 0:
            a["wins"] += 1
    for a in accounts:
        acc_id = a.get("id")
        if not acc_id:
            continue
        s = agg.get(acc_id, {})
        a["total_trades"] = s.get("total", 0)
        a["winning_trades"] = s.get("wins", 0)
        a["losing_trades"] = s.get("total", 0) - s.get("wins", 0)
        a["total_profit"] = round(s.get("profit", 0.0), 2)
        a["profit"] = a["total_profit"]
        a["total_volume"] = round(s.get("volume", 0.0), 2)
        a["volume"] = a["total_volume"]
        a["win_rate"] = round(s.get("wins", 0) / s["total"] * 100, 1) if s.get("total") else 0.0

        # Attach live open positions count and running state from account_service sessions
        try:
            session = account_service._sessions.get(acc_id)
            if session and session.bot:
                a["open_positions_count"] = len(session.bot.positions)
                a["bot_is_running"] = bool(session.bot.is_running)
            else:
                a["open_positions_count"] = 0
                a["bot_is_running"] = False
        except Exception:
            a["open_positions_count"] = 0
            a["bot_is_running"] = False
    return accounts


@app.get("/api/admin/accounts")
@limiter.limit("60/minute")
async def admin_list_accounts(request: Request, q: str = "", status: str = "", lock_state: str = "", page: int = 1, per_page: int = 50):
    await require_admin(request)
    accounts = await asyncio.to_thread(repository.list_accounts) if hasattr(repository, "list_accounts") else []
    if q:
        q = q.lower()
        accounts = [a for a in accounts if q in str(a.get("id", "")).lower() or q in str(a.get("display_name", "")).lower()]
    if status:
        accounts = [a for a in accounts if a.get("status") == status]
    if lock_state:
        accounts = [a for a in accounts if a.get("lock_state") == lock_state]
    accounts = sorted(accounts, key=lambda a: str(a.get("created_at") or ""), reverse=True)
    if hasattr(repository, "get_all_trades"):
        trades = await asyncio.to_thread(repository.get_all_trades)
        accounts = _enrich_account_stats(accounts, trades)
    total = len(accounts)
    page = max(1, page)
    per_page = max(1, min(per_page, 200))
    start = (page - 1) * per_page
    return {"total": total, "page": page, "per_page": per_page, "accounts": accounts[start:start + per_page]}


@app.get("/api/admin/accounts/{account_id}")
@limiter.limit("60/minute")
async def admin_account_detail(account_id: str, request: Request):
    await require_admin(request)
    if not hasattr(repository, "get_account_detail"):
        raise HTTPException(status_code=404, detail="Account detail unavailable")
    detail = repository.get_account_detail(account_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Trading account does not exist")
    # Join bot_type_name
    if hasattr(repository, "get_bot_types"):
        bt_id = detail.get("bot_type_id")
        if bt_id:
            bot_types = {bt["id"]: bt for bt in repository.get_bot_types()}
            bt = bot_types.get(bt_id)
            if bt:
                detail["bot_type_name"] = bt["name"]
                detail["bot_type_risk"] = bt["risk_level"]
    return detail


@app.post("/api/admin/accounts/{account_id}/bot-type")
@limiter.limit("20/minute")
async def admin_set_bot_type(account_id: str, payload: dict, request: Request):
    await require_admin(request)
    bot_type_id = payload.get("bot_type_id")
    if not bot_type_id:
        raise HTTPException(status_code=400, detail="bot_type_id is required")
    if hasattr(repository, "update_account"):
        repository.update_account(account_id, {"bot_type_id": bot_type_id})
    return {"status": "updated", "account_id": account_id, "bot_type_id": bot_type_id}


@app.post("/api/admin/accounts/{account_id}/status")
@limiter.limit("20/minute")
async def admin_set_status(account_id: str, payload: AdminStatusModel, request: Request):
    await require_admin(request)
    if not hasattr(repository, "set_account_status"):
        raise HTTPException(status_code=501, detail="Unsupported repository")
    repository.set_account_status(account_id, payload.status, payload.reason)
    if payload.status == "blocked" and hasattr(repository, "revoke_account_sessions"):
        repository.revoke_account_sessions(account_id)
    return {"status": "ok", "account_id": account_id, "account_status": payload.status}


@app.post("/api/admin/accounts/{account_id}/lock")
@limiter.limit("20/minute")
async def admin_set_lock(account_id: str, payload: AdminLockModel, request: Request):
    await require_admin(request)
    current_bot = account_service.get_bot(account_id)
    if payload.state == LOCK_HARD:
        await current_bot.hard_lock(payload.reason)
    elif payload.state == LOCK_SOFT:
        current_bot.soft_lock(payload.reason)
    else:
        current_bot.unlock(payload.reason)
    if hasattr(repository, "set_lock_state"):
        repository.set_lock_state(account_id, payload.state, payload.reason)
    logger.info(f"[ADMIN_LOCK] Account {account_id} lock state successfully set to: {payload.state} (reason: {payload.reason})")
    return {"status": "ok", "account_id": account_id, "lock_state": current_bot.lock_state}


@app.post("/api/admin/accounts/{account_id}/unlock")
@limiter.limit("10/minute")
async def admin_unlock_account(account_id: str, payload: AdminLockModel, request: Request):
    """Admin-only unlock of a hard/soft locked account.

    Phase 2: gates the LOCK_HARD→UNLOCKED transition through a dedicated
    endpoint that validates the admin role and logs the action. The DB trigger
    (enforce_hard_lock) is a defense-in-depth safety net.
    """
    principal = await require_admin(request)
    current_bot = account_service.get_bot(account_id)
    if current_bot.lock_state != LOCK_HARD:
        raise HTTPException(status_code=400, detail="Account is not hard-locked")
    current_bot.unlock(payload.reason)
    if hasattr(repository, "set_lock_state"):
        repository.set_lock_state(account_id, LOCK_UNLOCKED, payload.reason)
    log_security_event(
        event="admin_unlock_account",
        severity="warning",
        account_id=account_id,
        user_id=principal.user_id,
        correlation_id=getattr(request.state, "correlation_id", None),
        details={"reason": payload.reason, "previous_state": "hard_locked"},
    )
    logger.info(f"[ADMIN_UNLOCK] Account {account_id} unlocked by admin {principal.user_id} (reason: {payload.reason})")
    return {"status": "unlocked", "account_id": account_id, "lock_state": current_bot.lock_state}


@app.get("/api/admin/users")
@limiter.limit("60/minute")
async def admin_list_users(request: Request):
    """Lấy danh sách hồ sơ người dùng trong hệ thống kèm thông tin tài khoản và email."""
    await require_admin(request)
    users = []
    if hasattr(repository, "list_user_profiles"):
        users = await asyncio.to_thread(repository.list_user_profiles)

    # Enrich with account info if available
    if hasattr(repository, "list_accounts"):
        accounts = await asyncio.to_thread(repository.list_accounts)
        acc_by_user = {a.get("owner_user_id"): a for a in accounts if a.get("owner_user_id")}
        for u in users:
            uid = u.get("user_id")
            if uid in acc_by_user:
                acc = acc_by_user[uid]
                u["account_id"] = acc.get("id")
                u["bot_type_id"] = acc.get("bot_type_id")
                u["bot_type_name"] = acc.get("bot_type_name")
    return {"total": len(users), "users": users}


@app.post("/api/admin/users")
@limiter.limit("20/minute")
async def admin_create_user(payload: AdminCreateUserModel, request: Request):
    """Tạo người dùng mới (Admin hoặc Trader - loại trừ lẫn nhau)."""
    await require_admin(request)
    try:
        validate_password_strength(payload.password)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        norm_email = normalize_email(str(payload.email))
        res = await asyncio.to_thread(
            repository.create_user_admin,
            norm_email,
            payload.password,
            payload.display_name or norm_email,
            payload.role,
            payload.bot_type_id,
        )
        log_security_event(
            event="admin_create_user",
            severity="info",
            user_id=getattr(request.state, "user_id", None),
            details={"email": norm_email, "role": payload.role, "user_id": res.get("user_id")},
        )
        return {"status": "created", "user": res}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.patch("/api/admin/users/{user_id}")
@limiter.limit("20/minute")
async def admin_update_user(user_id: str, payload: AdminUpdateUserModel, request: Request):
    """Cập nhật thông tin/mật khẩu/vai trò/trạng thái người dùng."""
    await require_admin(request)
    if payload.password:
        try:
            validate_password_strength(payload.password)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    try:
        res = await asyncio.to_thread(
            repository.update_user_profile_admin,
            user_id,
            display_name=payload.display_name,
            password=payload.password,
            role=payload.role,
            status=payload.status,
        )
        if payload.role and hasattr(session_service, "set_user_roles"):
            session_service.set_user_roles(user_id, [payload.role])
        log_security_event(
            event="admin_update_user",
            severity="info",
            user_id=getattr(request.state, "user_id", None),
            details={"target_user_id": user_id, "updated_fields": [k for k, v in payload.model_dump().items() if v is not None]},
        )
        return {"status": "updated", "user": res}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/admin/users/{user_id}")
@limiter.limit("20/minute")
async def admin_delete_user(user_id: str, request: Request):
    """Xóa người dùng khỏi hệ thống."""
    await require_admin(request)
    current_user_id = getattr(request.state, "user_id", None)
    if current_user_id and current_user_id == user_id:
        raise HTTPException(status_code=400, detail="Không thể tự xóa tài khoản Admin đang đăng nhập.")

    try:
        success = await asyncio.to_thread(repository.delete_user_admin, user_id)
        log_security_event(
            event="admin_delete_user",
            severity="warning",
            user_id=current_user_id,
            details={"target_user_id": user_id, "success": success},
        )
        return {"status": "deleted", "user_id": user_id, "success": success}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/api/admin/users/{user_id}/roles")
@limiter.limit("30/minute")
async def admin_set_roles(user_id: str, payload: AdminRolesModel, request: Request):
    await require_admin(request)
    role_set = set(payload.roles) & {"trader", "admin"}
    if "admin" in role_set and "trader" in role_set:
        raise HTTPException(
            status_code=400,
            detail="Một tài khoản không thể đồng thời là Admin và Trader (dùng bot). Vai trò mang tính loại trừ lẫn nhau."
        )
    roles = ["admin"] if "admin" in role_set else ["trader"]
    result = {"user_id": user_id, "roles": roles}
    if hasattr(repository, "update_user_roles"):
        result = repository.update_user_roles(user_id, roles)
    if hasattr(session_service, "set_user_roles"):
        session_service.set_user_roles(user_id, result["roles"])
    return result


@app.get("/api/admin/accounts/{account_id}/positions")
@limiter.limit("60/minute")
async def admin_account_positions(account_id: str, request: Request):
    """Quan sát trực tiếp danh sách lệnh đang mở của một tài khoản."""
    await require_admin(request)
    session = account_service.get_session(account_id)
    bot = session.bot
    positions = list(bot.positions)
    floating_pnl = round(sum(float(p.get("profit") or 0.0) for p in positions), 2)
    return {
        "account_id": account_id,
        "is_running": bot.is_running,
        "positions_count": len(positions),
        "floating_pnl": floating_pnl,
        "positions": positions,
    }


@app.post("/api/admin/accounts/{account_id}/positions/{ticket}/close")
@limiter.limit("30/minute")
async def admin_close_position(account_id: str, ticket: int, request: Request):
    """Admin can thiệp đóng một vị thế mở cụ thể của tài khoản."""
    await require_admin(request)
    session = account_service.get_session(account_id)
    bot = session.bot
    success = await bot.close_position(ticket)
    log_security_event(
        event="admin_close_position",
        severity="warning",
        user_id=getattr(request.state, "user_id", None),
        details={"account_id": account_id, "ticket": ticket, "success": success},
    )
    return {"status": "closed", "ticket": ticket, "account_id": account_id, "success": success}


@app.post("/api/admin/accounts/{account_id}/positions/close-all")
@limiter.limit("15/minute")
async def admin_close_all_positions(account_id: str, request: Request):
    """Admin can thiệp đóng toàn bộ vị thế của một tài khoản."""
    await require_admin(request)
    session = account_service.get_session(account_id)
    bot = session.bot
    closed_count = len(bot.positions)
    await bot.close_all_positions()
    log_security_event(
        event="admin_close_all_account_positions",
        severity="warning",
        user_id=getattr(request.state, "user_id", None),
        details={"account_id": account_id, "closed_count": closed_count},
    )
    return {"status": "all_closed", "account_id": account_id, "closed_positions": closed_count}


@app.get("/api/admin/accounts/{account_id}/daily-performance")
@limiter.limit("60/minute")
async def admin_account_daily_performance(account_id: str, request: Request):
    """Phân tích tỷ lệ thắng và lợi nhuận theo từng ngày cho tài khoản."""
    await require_admin(request)
    days = []
    if hasattr(repository, "get_account_daily_performance"):
        days = await asyncio.to_thread(repository.get_account_daily_performance, account_id)
    total_profit = round(sum(d.get("net_profit", 0) for d in days), 2)
    total_trades = sum(d.get("total_trades", 0) for d in days)
    total_wins = sum(d.get("winning_trades", 0) for d in days)
    win_rate = round(total_wins / total_trades * 100, 1) if total_trades > 0 else 0.0
    return {
        "account_id": account_id,
        "days": days,
        "summary": {
            "total_days": len(days),
            "total_profit": total_profit,
            "total_trades": total_trades,
            "win_rate": win_rate,
        }
    }


@app.post("/api/admin/fleet/emergency-close-all")
@limiter.limit("5/minute")
async def admin_fleet_emergency_close_all(payload: EmergencyCloseAllModel, request: Request):
    """Lệnh đóng khẩn cấp toàn bộ vị thế trên mọi tài khoản khi thị trường quét mạnh."""
    await require_admin(request)
    total_closed = 0
    total_halted = 0
    affected_accounts = []

    for acc_id, session in list(account_service._sessions.items()):
        bot = session.bot
        if not bot:
            continue
        pos_count = len(bot.positions)
        if pos_count > 0:
            await bot.close_all_positions()
            total_closed += pos_count
            affected_accounts.append(acc_id)
        if payload.halt_bots and bot.is_running:
            await bot.stop()
            bot.soft_lock(payload.reason)
            total_halted += 1

    log_security_event(
        event="fleet_emergency_close_all",
        severity="critical",
        user_id=getattr(request.state, "user_id", None),
        details={
            "reason": payload.reason,
            "halt_bots": payload.halt_bots,
            "total_closed": total_closed,
            "total_halted": total_halted,
            "affected_accounts": affected_accounts,
        },
    )
    logger.warning(f"[EMERGENCY_SWEEP] Global emergency executed: {total_closed} positions closed across {len(affected_accounts)} accounts, {total_halted} bots halted.")
    return {
        "status": "emergency_executed",
        "total_positions_closed": total_closed,
        "total_bots_halted": total_halted,
        "affected_accounts_count": len(affected_accounts),
        "affected_accounts": affected_accounts,
        "reason": payload.reason,
    }


@app.get("/api/admin/bot-fleet/breakdown")
@limiter.limit("60/minute")
async def admin_bot_fleet_breakdown(request: Request):
    """Thống kê chi tiết từng loại bot cho từng tài khoản."""
    await require_admin(request)
    bot_types = await asyncio.to_thread(repository.get_bot_types, include_inactive=True) if hasattr(repository, "get_bot_types") else []
    accounts = await asyncio.to_thread(repository.list_accounts) if hasattr(repository, "list_accounts") else []
    trades = await asyncio.to_thread(repository.get_all_trades) if hasattr(repository, "get_all_trades") else []
    accounts = _enrich_account_stats(accounts, trades)

    breakdown = []
    for bt in bot_types:
        bt_id = bt["id"]
        assigned_accs = [a for a in accounts if a.get("bot_type_id") == bt_id]
        total_pnl = round(sum(a.get("total_profit", 0.0) for a in assigned_accs), 2)
        total_trades = sum(a.get("total_trades", 0) for a in assigned_accs)
        winning_trades = sum(a.get("winning_trades", 0) for a in assigned_accs)
        avg_win_rate = round((winning_trades / total_trades) * 100, 1) if total_trades > 0 else 0.0
        running_bots = sum(1 for a in assigned_accs if a.get("bot_is_running"))

        breakdown.append({
            "bot_type": bt,
            "assigned_accounts_count": len(assigned_accs),
            "running_bots_count": running_bots,
            "total_profit": total_pnl,
            "total_trades": total_trades,
            "win_rate": avg_win_rate,
            "accounts": [{
                "id": a.get("id"),
                "display_name": a.get("display_name"),
                "equity": a.get("equity", 0),
                "profit": a.get("total_profit", 0.0),
                "win_rate": a.get("win_rate", 0.0),
                "open_positions_count": a.get("open_positions_count", 0),
                "status": a.get("status"),
                "is_running": a.get("bot_is_running", False),
            } for a in assigned_accs]
        })

    return {
        "total_bot_types": len(bot_types),
        "total_accounts": len(accounts),
        "breakdown": breakdown,
    }


@app.post("/api/admin/accounts/{account_id}/revoke-sessions")
@limiter.limit("20/minute")
async def admin_revoke(account_id: str, request: Request):
    await require_admin(request)
    n = repository.revoke_account_sessions(account_id) if hasattr(repository, "revoke_account_sessions") else 0
    return {"status": "ok", "account_id": account_id, "revoked": n}


@app.post("/api/admin/accounts/{account_id}/deploy")
@limiter.limit("10/minute")
async def admin_deploy_bot(account_id: str, request: Request):
    """Nạp (khởi động) bot vào một tài khoản MT5 người dùng."""
    await require_admin(request)
    current_bot = account_service.get_bot(account_id)
    if current_bot.lock_state == LOCK_HARD:
        raise HTTPException(status_code=423, detail="Account is hard-locked; unlock before deploying bot")
    if current_bot.is_running:
        return {"status": "already_running", "account_id": account_id, "simulation_mode": current_bot.simulation_mode}
    await current_bot.initialize_mt5()
    await current_bot.fetch_news_feed()
    asyncio.create_task(current_bot.start_price_feed_loop())
    asyncio.create_task(current_bot.start())
    return {"status": "started", "account_id": account_id, "simulation_mode": current_bot.simulation_mode}


@app.post("/api/admin/accounts/{account_id}/halt")
@limiter.limit("10/minute")
async def admin_halt_bot(account_id: str, request: Request):
    """Dừng bot đang chạy trên một tài khoản."""
    await require_admin(request)
    current_bot = account_service.get_bot(account_id)
    if not current_bot.is_running:
        return {"status": "already_stopped", "account_id": account_id}
    await current_bot.stop()
    return {"status": "stopped", "account_id": account_id}


@app.get("/api/admin/trades")
@limiter.limit("60/minute")
async def admin_trades(request: Request, limit: int = 300):
    """Toàn bộ hoạt động đi lệnh của bot trên mọi tài khoản."""
    await require_admin(request)
    limit = max(1, min(limit, 2000))
    trades = await asyncio.to_thread(repository.get_all_trades, limit) if hasattr(repository, "get_all_trades") else []
    closed = [t for t in trades if t.get("status") == "closed"]
    open_trades = [t for t in trades if t.get("status") in ("filled", "partially_filled", "pending", "submitted")]
    status_dist: dict[str, int] = {}
    for t in trades:
        s = t.get("status") or "unknown"
        status_dist[s] = status_dist.get(s, 0) + 1
    return {
        "total": len(trades),
        "open_count": len(open_trades),
        "closed_count": len(closed),
        "total_profit": round(sum(t.get("profit") or 0 for t in closed), 2),
        "status_distribution": [{"label": k, "value": v} for k, v in sorted(status_dist.items(), key=lambda kv: kv[1], reverse=True)],
        "trades": trades[:limit],
    }


# ---------------------------------------------------------------------------
# Bot Types Management API (CRUD)
# ---------------------------------------------------------------------------

@app.get("/api/admin/bot-types")
@limiter.limit("60/minute")
async def list_bot_types(request: Request, include_inactive: bool = False):
    """Lấy danh sách tất cả các loại bot."""
    await require_admin(request)
    if not hasattr(repository, "get_bot_types"):
        raise HTTPException(status_code=501, detail="Bot types management not supported")
    bot_types = await asyncio.to_thread(repository.get_bot_types, include_inactive=include_inactive)
    return {"total": len(bot_types), "bot_types": bot_types}


@app.get("/api/admin/bot-types/{bot_type_id}")
@limiter.limit("60/minute")
async def get_bot_type(bot_type_id: str, request: Request):
    """Lấy thông tin chi tiết một loại bot."""
    await require_admin(request)
    if not hasattr(repository, "get_bot_type"):
        raise HTTPException(status_code=501, detail="Bot types management not supported")
    bot_type = await asyncio.to_thread(repository.get_bot_type, bot_type_id)
    if not bot_type:
        raise HTTPException(status_code=404, detail="Bot type not found")
    
    # Count accounts using this bot type
    accounts = []
    if hasattr(repository, "list_accounts"):
        all_accounts = await asyncio.to_thread(repository.list_accounts)
        accounts = [a for a in all_accounts if a.get("bot_type_id") == bot_type_id]
    
    bot_type["accounts_count"] = len(accounts)
    bot_type["accounts_using"] = accounts
    return bot_type


@app.post("/api/admin/bot-types")
@limiter.limit("20/minute")
async def create_bot_type(payload: BotTypeCreate, request: Request):
    """Tạo loại bot mới."""
    await require_admin(request)
    if not hasattr(repository, "create_bot_type"):
        raise HTTPException(status_code=501, detail="Bot types management not supported")
    
    try:
        bot_type = await asyncio.to_thread(repository.create_bot_type, payload.model_dump())
        log_security_event(
            event="bot_type_created",
            severity="info",
            user_id=getattr(request.state, "user_id", None),
            details={"bot_type_id": bot_type.get("id"), "name": bot_type.get("name")},
        )
        return {"status": "created", "bot_type": bot_type}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/api/admin/bot-types/{bot_type_id}")
@limiter.limit("20/minute")
async def update_bot_type(bot_type_id: str, payload: BotTypeUpdate, request: Request):
    """Cập nhật thông tin loại bot."""
    await require_admin(request)
    if not hasattr(repository, "update_bot_type"):
        raise HTTPException(status_code=501, detail="Bot types management not supported")
    
    # Only include fields that were actually set
    update_data = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update")
    
    try:
        bot_type = await asyncio.to_thread(repository.update_bot_type, bot_type_id, update_data)
        log_security_event(
            event="bot_type_updated",
            severity="info",
            user_id=getattr(request.state, "user_id", None),
            details={"bot_type_id": bot_type_id, "updated_fields": list(update_data.keys())},
        )
        return {"status": "updated", "bot_type": bot_type}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.patch("/api/admin/bot-types/{bot_type_id}")
@limiter.limit("20/minute")
async def patch_bot_type(bot_type_id: str, payload: BotTypeUpdate, request: Request):
    """Cập nhật một phần thông tin loại bot (giống PUT nhưng theo RESTful convention)."""
    return await update_bot_type(bot_type_id, payload, request)


@app.delete("/api/admin/bot-types/{bot_type_id}")
@limiter.limit("20/minute")
async def delete_bot_type(bot_type_id: str, request: Request, hard_delete: bool = False):
    """Xóa loại bot (soft delete mặc định, hard delete nếu hard_delete=true)."""
    await require_admin(request)
    if not hasattr(repository, "delete_bot_type"):
        raise HTTPException(status_code=501, detail="Bot types management not supported")
    
    # Check if any accounts are using this bot type
    if hasattr(repository, "list_accounts"):
        all_accounts = await asyncio.to_thread(repository.list_accounts)
        accounts_using = [a for a in all_accounts if a.get("bot_type_id") == bot_type_id]
        if accounts_using and hard_delete:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot hard delete bot type: {len(accounts_using)} account(s) are using it. Remove bot type from accounts first or use soft delete."
            )
    
    try:
        success = await asyncio.to_thread(repository.delete_bot_type, bot_type_id, soft_delete=not hard_delete)
        if not success:
            raise HTTPException(status_code=404, detail="Bot type not found")
        
        log_security_event(
            event="bot_type_deleted",
            severity="warning" if hard_delete else "info",
            user_id=getattr(request.state, "user_id", None),
            details={"bot_type_id": bot_type_id, "hard_delete": hard_delete},
        )
        
        delete_type = "hard deleted" if hard_delete else "deactivated"
        return {"status": "deleted", "bot_type_id": bot_type_id, "delete_type": delete_type}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


if __name__ == "__main__":
    reload_flag = os.getenv("UVICORN_RELOAD", "false").lower() == "true"
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=reload_flag)
