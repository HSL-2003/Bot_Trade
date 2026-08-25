from typing import Optional, Literal
import os
import asyncio
import logging
from urllib.parse import quote
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
from bot import MT5TradingBot, MT5_AVAILABLE
from config import SUPPORTED_SYMBOLS, agents_enabled, allowed_origins
from services.account_service import TradingAccountService
from services.auth_service import AuthenticationError, InMemorySessionService, SupabaseSessionService, normalize_email
from repositories.persistence import InMemoryAccountRepository
from repositories.supabase_repository import SupabaseAccountRepository
from security import (
    CSRFProtectionMiddleware,
    CorrelationIdMiddleware,
    RequestSizeLimitMiddleware,
    log_security_event,
)

security_logger = logging.getLogger("security")

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="MT5 Confluence Algo Bot")
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
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net https://unpkg.com https://s3.tradingview.com https://*.tradingview.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com data:; "
            "img-src 'self' data: https: https://*.tradingview.com; "
            "frame-src 'self' https://*.tradingview.com https://s.tradingview.com https://www.tradingview.com; "
            "child-src 'self' https://*.tradingview.com https://s.tradingview.com https://www.tradingview.com; "
            "connect-src 'self' ws: wss: http: https: https://*.supabase.co https://unpkg.com https://*.tradingview.com wss://*.tradingview.com wss://pushstream.tradingview.com;"
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
    allow_headers=["Content-Type", "Authorization", "X-Account-Id"],
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
    repository = SupabaseAccountRepository(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
else:
    repository = InMemoryAccountRepository()
account_service = TradingAccountService(repository)
default_acc_id = os.getenv("DEFAULT_ACCOUNT_ID", "demo-account")
account_service.register_bot(default_acc_id, bot)
if os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY"):
    session_service = SupabaseSessionService(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
else:
    session_service = InMemorySessionService()
bot_task = None


def account_id_from_request(request: Request) -> str:
    account_id = request.headers.get("X-Account-Id") or request.query_params.get("account_id")
    authorization = request.headers.get("Authorization", "")
    # Authenticate whenever a Bearer token is present OR when REQUIRE_AUTH is on.
    if authorization.startswith("Bearer ") or os.getenv("REQUIRE_AUTH", "false").lower() == "true":
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Bearer authentication is required")
        try:
            principal = session_service.authenticate(authorization[7:].strip())
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
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

class MagicLinkModel(BaseModel):
    email: str

class SocialCallbackModel(BaseModel):
    token: str

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


def bearer_token(request: Request) -> str:
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        # Browser flow fallback: the session rides in an HttpOnly cookie set
        # at login/register. API clients keep using the Authorization header.
        cookie = request.cookies.get("session_token")
        if cookie:
            return cookie
        raise HTTPException(status_code=401, detail="Bearer authentication is required")
    return authorization[7:].strip()


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
    if len(payload.password) < 8:
        raise HTTPException(status_code=422, detail="Password must contain at least 8 characters")
    try:
        result = session_service.register(normalize_email(payload.email), payload.password, payload.display_name)
    except AuthenticationError as exc:
        log_security_event(
            "registration_failed",
            correlation_id=getattr(request.state, "correlation_id", None),
            client_ip=request.client.host if request.client else None,
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response = JSONResponse(content=jsonable_encoder(result), status_code=201)
    set_session_cookie(response, result, request)
    return response


@app.post("/api/auth/login")
@limiter.limit("10/minute")
async def login(request: Request, payload: LoginModel):
    try:
        result = session_service.login(normalize_email(payload.email), payload.password)
    except AuthenticationError as exc:
        log_security_event(
            "login_failed",
            correlation_id=getattr(request.state, "correlation_id", None),
            client_ip=request.client.host if request.client else None,
            detail="invalid credentials",
        )
        raise HTTPException(status_code=401, detail=str(exc)) from exc
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
    redirect_uri = f"{site_url}/auth/callback"
    authorize_url = f"{supabase_url}/auth/v1/authorize?provider=github&redirect_to={quote(redirect_uri, safe='')}"
    return RedirectResponse(authorize_url)


@app.get("/api/auth/google")
async def google_oauth(request: Request):
    """Start Google OAuth via Supabase Auth."""
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    site_url = os.getenv("SITE_URL", "http://127.0.0.1:8000").rstrip("/")
    if not supabase_url:
        raise HTTPException(status_code=503, detail="OAuth is not configured")
    redirect_uri = f"{site_url}/auth/callback"
    authorize_url = f"{supabase_url}/auth/v1/authorize?provider=google&redirect_to={quote(redirect_uri, safe='')}"
    return RedirectResponse(authorize_url)


@app.post("/api/auth/social/callback")
@limiter.limit("10/minute")
async def social_callback(request: Request, payload: SocialCallbackModel):
    """Exchange a Supabase OAuth / magic-link token for an app session."""
    try:
        result = session_service.login_with_supabase_token(payload.token)
    except AuthenticationError as exc:
        log_security_event(
            "oauth_callback_failed",
            correlation_id=getattr(request.state, "correlation_id", None),
            client_ip=request.client.host if request.client else None,
        )
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    response = JSONResponse(content=jsonable_encoder(result))
    set_session_cookie(response, result, request)
    return response


@app.get("/api/auth/me")
async def current_user(request: Request):
    try:
        principal = session_service.authenticate(bearer_token(request))
        return {"user_id": principal.user_id, "account_id": principal.account_id, "roles": sorted(principal.roles)}
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


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

def principal_optional_from_request(request: Request) -> Optional[Any]:
    try:
        token = bearer_token(request)
        return session_service.authenticate(token)
    except Exception:
        return None


def scoped_bot(request: Request) -> MT5TradingBot:
    acc_id = account_id_from_request(request)
    principal = principal_optional_from_request(request)
    user_id = principal.user_id if principal else None
    return account_service.get_bot(acc_id, user_id)


def principal_from_request(request: Request):
    try:
        return session_service.authenticate(bearer_token(request))
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

@app.get("/dashboard")
async def get_user_dashboard():
    return render_template("dashboard.html")


@app.get("/profile")
async def get_profile_page():
    return render_template("profile.html")

@app.get("/api/profile")
async def get_profile(request: Request):
    principal = principal_from_request(request)
    if not hasattr(repository, "profile"):
        return {"user_id": principal.user_id, "display_name": None}
    return repository.profile(principal.user_id)

@app.patch("/api/profile")
async def update_profile(payload: ProfileModel, request: Request):
    principal = principal_from_request(request)
    data = {key: value for key, value in payload.dict().items() if value is not None}
    if not hasattr(repository, "update_profile"):
        return {"user_id": principal.user_id, **data}
    return repository.update_profile(principal.user_id, data)

@app.get("/api/dashboard/data")
async def dashboard_data(request: Request, days: int = 30):
    principal = principal_from_request(request)
    days = max(1, min(days, 365))
    
    # 1. Fetch user trades from repository
    trades = []
    if hasattr(repository, "get_user_trades"):
        trades = repository.get_user_trades(principal.account_id, principal.user_id, limit=200)
    elif hasattr(repository, "recent_trades"):
        trades = repository.recent_trades(principal.account_id, principal.user_id, limit=200)
    
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
    win_rate = (len(wins) / count * 100) if count > 0 else 0.0
    
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
async def get_history_analytics(request: Request, period: str = "all"):
    if period not in ["day", "week", "month", "all"]:
        period = "all"
    current_bot = scoped_bot(request)
    principal = principal_optional_from_request(request)
    acc_id = account_id_from_request(request)
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
    principal = principal_from_request(request)
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
            principal = session_service.authenticate(token)
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
            state = {
                "account_id": account_id,
                "is_running": stream_bot.is_running,
                "simulation_mode": stream_bot.simulation_mode,
                "system_locked": stream_bot.system_locked,
                "is_pending_order": stream_bot.is_pending_order,
                "symbol": stream_bot.symbol,
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
                "current_price": stream_bot.current_price,
                "account_info": stream_bot.account_info,
                "positions": stream_bot.positions,
                "pending_orders": stream_bot.pending_orders,
                "news_events": stream_bot.news_events,
                "recent_logs": stream_bot.recent_logs,
                "sr_levels": stream_bot.sr_levels,
                "fib_levels": stream_bot.fib_levels,
                "confluence_zones": stream_bot.confluence_zones,
                "active_signals": stream_bot.active_signals,
                "trade_history": stream_bot.history[-100:],
                "statistics": stream_bot.get_statistics(),
                "indicators": stream_bot.indicators,
                "watchlist": stream_bot.watchlist_data
            }
            await websocket.send_json(state)
            await asyncio.sleep(0.05)
    except WebSocketDisconnect:
        active_connections.remove(websocket)
    except Exception as e:
        if websocket in active_connections:
            active_connections.remove(websocket)

# Start Bot API
@app.post("/api/start")
async def start_bot(request: Request):
    global bot_task
    current_bot = scoped_bot(request)
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
async def stop_bot(request: Request):
    current_bot = scoped_bot(request)
    if not current_bot.is_running:
        return {"status": "already_stopped"}
    await current_bot.stop()
    return {"status": "stopped"}

# Toggle simulation mode
class SimulationToggleModel(BaseModel):
    enabled: Optional[bool] = None

@app.post("/api/simulation/toggle")
async def toggle_simulation(request: Request, payload: Optional[SimulationToggleModel] = None):
    current_bot = scoped_bot(request)
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
    current_bot = scoped_bot(request)
    return {
        "is_running": current_bot.is_running,
        "simulation_mode": current_bot.simulation_mode,
        "system_locked": current_bot.system_locked,
        "is_pending_order": current_bot.is_pending_order,
        "symbol": current_bot.symbol,
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
        "current_price": current_bot.current_price,
        "account_info": current_bot.account_info,
        "positions": current_bot.positions,
        "news_events": current_bot.news_events,
        "recent_logs": current_bot.recent_logs,
        "sr_levels": current_bot.sr_levels,
        "fib_levels": current_bot.fib_levels,
        "confluence_zones": current_bot.confluence_zones,
        "active_signals": current_bot.active_signals,
        "trade_history": current_bot.history,
        "statistics": current_bot.get_statistics(),
        "indicators": current_bot.indicators,
        "watchlist": current_bot.watchlist_data
    }

# Update Settings API
@app.post("/api/settings")
async def update_settings(settings: SettingsModel, request: Request):
    bot = scoped_bot(request)
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
async def symbol_toggle_endpoint(data: SymbolToggleModel, request: Request):
    current_bot = scoped_bot(request)
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
async def manual_trade(trade: ManualTradeModel, request: Request):
    bot = scoped_bot(request)
    if bot.system_locked:
        raise HTTPException(status_code=423, detail="Trading system is emergency-locked")
    if trade.lot_size <= 0:
        raise HTTPException(status_code=422, detail="Lot size must be positive")
    
    # Run filter check for manual trade
    current_bot = scoped_bot(request)
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
async def cancel_pending_order_endpoint(ticket: int, request: Request):
    current_bot = scoped_bot(request)
    success = await current_bot.cancel_pending_order(ticket)
    if not success and bot != current_bot:
        success = await bot.cancel_pending_order(ticket)
    if not success:
        raise HTTPException(status_code=404, detail="Pending order not found.")
    return {"status": "success", "ticket": ticket}

# Force Close Position
@app.post("/api/close/{ticket}")
async def close_position_endpoint(ticket: int, request: Request):
    _auth = request.headers.get("authorization", "")
    _csrf_hdr = request.headers.get("x-csrf-token", "MISSING")
    _csrf_cookie = request.cookies.get("_csrf", "MISSING")
    print(f"[LOG] POST /api/close/{ticket} RECEIVED | auth_header={'Bearer...' if _auth.startswith('Bearer') else 'MISSING/NONE'} | csrf_header={'YES' if _csrf_hdr not in ('', 'MISSING') else 'NO'} | csrf_cookie={'YES' if _csrf_cookie not in ('', 'MISSING') else 'NO'} | origin={request.headers.get('origin', 'NONE')}", flush=True)
    current_bot = scoped_bot(request)
    await current_bot.close_position(ticket)
    if bot != current_bot:
        await bot.close_position(ticket)
    # Also ensure position is cleared from any active session bots
    if hasattr(account_service, "_sessions"):
        for session in list(account_service._sessions.values()):
            if session.bot != current_bot and session.bot != bot:
                await session.bot.close_position(ticket)
    print(f"[LOG] POST /api/close/{ticket} DONE -> return 200 ok", flush=True)
    return {"status": "close_submitted", "ticket": ticket}

# Force Close All Positions
@app.post("/api/close-all")
async def close_all_positions_endpoint(request: Request):
    _auth = request.headers.get("authorization", "")
    _csrf_hdr = request.headers.get("x-csrf-token", "MISSING")
    _csrf_cookie = request.cookies.get("_csrf", "MISSING")
    print(f"[LOG] POST /api/close-all RECEIVED | auth_header={'Bearer...' if _auth.startswith('Bearer') else 'MISSING/NONE'} | csrf_header={'YES' if _csrf_hdr not in ('', 'MISSING') else 'NO'} | csrf_cookie={'YES' if _csrf_cookie not in ('', 'MISSING') else 'NO'} | origin={request.headers.get('origin', 'NONE')}", flush=True)
    current_bot = scoped_bot(request)
    await current_bot.close_all_positions()
    if bot != current_bot:
        await bot.close_all_positions()
    if hasattr(account_service, "_sessions"):
        for session in list(account_service._sessions.values()):
            if session.bot != current_bot and session.bot != bot:
                await session.bot.close_all_positions()
    print("[LOG] POST /api/close-all DONE -> return 200 ok", flush=True)
    return {"status": "close_all_submitted"}

# Modify SL/TP of an open position
@app.post("/api/modify-sltp/{ticket}")
async def modify_sltp_endpoint(ticket: int, data: ModifySLTPModel, request: Request):
    current_bot = scoped_bot(request)
    success = await current_bot.modify_position_sltp(ticket, data.sl, data.tp)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to modify SL/TP. Position not found or broker rejected.")
    return {"status": "modified", "ticket": ticket, "sl": data.sl, "tp": data.tp}

# Manual Trigger Circuit Breaker (Lockdown)
@app.post("/api/circuit-breaker/trigger")
async def manual_trigger_circuit_breaker(request: Request):
    current_bot = scoped_bot(request)
    current_bot.system_locked = True
    await current_bot.log_event("CIRCUIT_BREAKER", "Manual Daily Drawdown Circuit Breaker Triggered by User. Locking system.")
    await current_bot.emergency_lockdown()
    return {"status": "locked"}

# Reset Circuit Breaker (Unlock)
@app.post("/api/circuit-breaker/reset")
async def reset_circuit_breaker(request: Request):
    current_bot = scoped_bot(request)
    current_bot.system_locked = False
    # Reset daily starting equity
    current_bot.daily_start_equity = current_bot.account_info["balance"]
    current_bot.account_info["daily_start_equity"] = current_bot.daily_start_equity
    current_bot.account_info["daily_drawdown_percent"] = 0.0
    await current_bot.log_event("CIRCUIT_BREAKER", "Circuit Breaker manually reset by User. System unlocked.")
    return {"status": "unlocked"}

# Multi-Agent Iterative SDLC Loop Endpoint
@app.post("/api/agents/sdlc-loop")
async def run_sdlc_loop_endpoint(task: AgentTaskModel):
    if not agents_enabled():
        raise HTTPException(status_code=404, detail="SDLC agents are disabled")
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

# Background initialization tasks

@app.on_event("startup")
async def startup_event():
    # Attempt initial connection
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

@app.on_event("shutdown")
async def shutdown_event():
    await account_service.shutdown()
    # Close MT5 connection
    if not bot.simulation_mode and MT5_AVAILABLE:
        import MetaTrader5 as mt5  # type: ignore[import-not-found]
        mt5.shutdown()

if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
