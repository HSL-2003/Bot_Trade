from typing import Optional, Literal
import os
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
from bot import MT5TradingBot, MT5_AVAILABLE
from config import SUPPORTED_SYMBOLS, agents_enabled, allowed_origins
from account_service import TradingAccountService
from auth_service import AuthenticationError, InMemorySessionService
from persistence import InMemoryAccountRepository
from supabase_repository import SupabaseAccountRepository

app = FastAPI(title="MT5 Confluence Algo Bot")
agent_manager = None

class AgentTaskModel(BaseModel):
    prompt: str
    max_retries: Optional[int] = 3


# Enable CORS for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
session_service = InMemorySessionService()
bot_task = None


def account_id_from_request(request: Request) -> str:
    account_id = request.headers.get("X-Account-Id") or request.query_params.get("account_id")
    if os.getenv("REQUIRE_AUTH", "false").lower() == "true":
        authorization = request.headers.get("Authorization", "")
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


def scoped_bot(request: Request) -> MT5TradingBot:
    return account_service.get_bot(account_id_from_request(request))

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


@app.get("/")
async def get_landing():
    # Keep the original Three.js / 3D helmet landing experience at the root.
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

# WebSocket Endpoint for streaming real-time metrics
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    account_id = websocket.query_params.get("account_id") or os.getenv("DEFAULT_ACCOUNT_ID", "demo-account")
    if os.getenv("REQUIRE_AUTH", "false").lower() == "true":
        token = websocket.query_params.get("token", "")
        try:
            principal = session_service.authenticate(token)
        except AuthenticationError:
            await websocket.close(code=4401)
            return
        if principal.account_id != account_id:
            await websocket.close(code=4403)
            return
    stream_bot = account_service.get_bot(account_id)
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
                "max_spread": bot.max_spread,
                "max_daily_loss_percent": bot.max_daily_loss_percent,
                "trailing_stop_points": bot.trailing_stop_points,
                "trailing_step_points": bot.trailing_step_points,
                "trailing_stop_offset_points": bot.trailing_stop_offset_points,
                "breakeven_trigger_points": bot.breakeven_trigger_points,
                "breakeven_buffer_points": bot.breakeven_buffer_points,
                "news_restriction_minutes": bot.news_restriction_minutes,
                "auto_trading": bot.auto_trading,
                "enabled_symbols": bot.enabled_symbols,
                "max_open_trades": bot.max_open_trades,
                "cooldown_duration": bot.cooldown_duration,
                "roi_enabled": bot.roi_enabled,
                "roi_table": bot.roi_table,
                "pair_locks": bot.pair_locks,
                "current_price": bot.current_price,
                "account_info": bot.account_info,
                "positions": bot.positions,
                "pending_orders": bot.pending_orders,
                "news_events": bot.news_events,
                "recent_logs": bot.recent_logs,
                "sr_levels": bot.sr_levels,
                "fib_levels": bot.fib_levels,
                "confluence_zones": bot.confluence_zones,
                "active_signals": bot.active_signals,
                "trade_history": bot.history[-100:],
                "statistics": bot.get_statistics(),
                "indicators": bot.indicators,
                "watchlist": bot.watchlist_data
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
async def start_bot():
    global bot_task
    if bot.is_running:
        return {"status": "already_running"}
    
    # Try initialization of MT5
    await bot.initialize_mt5()
    await bot.fetch_news_feed()
    
    # Run the bot in a background task
    bot_task = asyncio.create_task(bot.start())
    return {"status": "started", "simulation_mode": bot.simulation_mode}

# Stop Bot API
@app.post("/api/stop")
async def stop_bot():
    if not bot.is_running:
        return {"status": "already_stopped"}
    await bot.stop()
    return {"status": "stopped"}

# Get Bot State API
@app.get("/api/state")
async def get_state():
    return {
        "is_running": bot.is_running,
        "simulation_mode": bot.simulation_mode,
        "system_locked": bot.system_locked,
        "is_pending_order": bot.is_pending_order,
        "symbol": bot.symbol,
        "settings": {
            "risk_percent": bot.risk_percent,
            "max_spread": bot.max_spread,
            "max_daily_loss_percent": bot.max_daily_loss_percent,
            "trailing_stop_points": bot.trailing_stop_points,
            "trailing_step_points": bot.trailing_step_points,
            "trailing_stop_offset_points": bot.trailing_stop_offset_points,
            "breakeven_trigger_points": bot.breakeven_trigger_points,
            "breakeven_buffer_points": bot.breakeven_buffer_points,
            "news_restriction_minutes": bot.news_restriction_minutes,
            "auto_trading": bot.auto_trading,
            "enabled_symbols": bot.enabled_symbols,
            "max_open_trades": bot.max_open_trades,
            "cooldown_duration": bot.cooldown_duration,
            "roi_enabled": bot.roi_enabled,
            "roi_table": ",".join([f"{k}:{v}" for k, v in bot.roi_table.items()])
        },
        "pair_locks": bot.pair_locks,
        "current_price": bot.current_price,
        "account_info": bot.account_info,
        "positions": bot.positions,
        "news_events": bot.news_events,
        "recent_logs": bot.recent_logs,
        "sr_levels": bot.sr_levels,
        "fib_levels": bot.fib_levels,
        "confluence_zones": bot.confluence_zones,
        "active_signals": bot.active_signals,
        "trade_history": bot.history,
        "statistics": bot.get_statistics(),
        "indicators": bot.indicators,
        "watchlist": bot.watchlist_data
    }

# History Analytics API with period filter (day, week, month, all)
@app.get("/api/history/analytics")
async def get_history_analytics(period: str = "all"):
    if period not in ["day", "week", "month", "all"]:
        period = "all"
    return bot.get_history_analytics(period)

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
async def symbol_toggle_endpoint(data: SymbolToggleModel):
    if data.preset == "XAUUSD_ONLY":
        for sym in bot.enabled_symbols:
            bot.enabled_symbols[sym] = (sym == "XAUUSD")
        await bot.log_event("SETTINGS", "Target Auto-Trade Symbol preset set to ONLY XAUUSD.")
    elif data.preset == "ALL_ON":
        for sym in bot.enabled_symbols:
            bot.enabled_symbols[sym] = True
        await bot.log_event("SETTINGS", "Target Auto-Trade Symbol preset set to ALL ON.")
    elif data.symbol and data.enabled is not None:
        data.symbol = data.symbol.upper().strip()
        if data.symbol not in SUPPORTED_SYMBOLS:
            raise HTTPException(status_code=422, detail="Unsupported symbol")
        bot.enabled_symbols[data.symbol] = data.enabled
        status_str = "ENABLED" if data.enabled else "DISABLED"
        await bot.log_event("SETTINGS", f"Auto-Trading for {data.symbol} set to {status_str}.")
    return {"status": "success", "enabled_symbols": bot.enabled_symbols}

# Trigger Manual Trade
@app.post("/api/trade")
async def manual_trade(trade: ManualTradeModel, request: Request):
    bot = scoped_bot(request)
    if bot.system_locked:
        raise HTTPException(status_code=423, detail="Trading system is emergency-locked")
    if trade.lot_size <= 0:
        raise HTTPException(status_code=422, detail="Lot size must be positive")
    
    # Run filter check for manual trade
    passed = await bot.check_filters(trade.type, is_manual=True)
    if not passed:
        raise HTTPException(status_code=400, detail="Trade rejected by risk filters (spread/drawdown/news).")

    sym = (trade.symbol or bot.symbol).upper().strip()
    if sym not in SUPPORTED_SYMBOLS:
        raise HTTPException(status_code=422, detail="Unsupported symbol")
    if trade.sl_points is not None and trade.sl_points < 0:
        raise HTTPException(status_code=422, detail="Stop-loss points must not be negative")
    if trade.tp_points is not None and trade.tp_points < 0:
        raise HTTPException(status_code=422, detail="Take-profit points must not be negative")
    point = bot.get_symbol_point(sym)
    sym_price = bot.get_current_price_for_symbol(sym)
    
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
        pending = await bot.add_pending_order(
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
    asyncio.create_task(bot.execute_market_trade(
        order_type=trade.type,
        lot_size=trade.lot_size,
        sl_points=sl_pts,
        tp_points=tp_pts,
        symbol=sym
    ))
    return {"status": "order_submitted", "sl_points": sl_pts, "tp_points": tp_pts}

# Cancel Pending Order
@app.post("/api/pending/cancel/{ticket}")
async def cancel_pending_order_endpoint(ticket: int):
    success = await bot.cancel_pending_order(ticket)
    if not success:
        raise HTTPException(status_code=404, detail="Pending order not found.")
    return {"status": "success", "ticket": ticket}

# Force Close Position
@app.post("/api/close/{ticket}")
async def close_position_endpoint(ticket: int):
    # Close order asynchronously
    asyncio.create_task(bot.close_position(ticket))
    return {"status": "close_submitted", "ticket": ticket}

# Force Close All Positions
@app.post("/api/close-all")
async def close_all_positions_endpoint():
    asyncio.create_task(bot.close_all_positions())
    return {"status": "close_all_submitted"}

# Modify SL/TP of an open position
@app.post("/api/modify-sltp/{ticket}")
async def modify_sltp_endpoint(ticket: int, data: ModifySLTPModel):
    success = await bot.modify_position_sltp(ticket, data.sl, data.tp)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to modify SL/TP. Position not found or broker rejected.")
    return {"status": "modified", "ticket": ticket, "sl": data.sl, "tp": data.tp}

# Manual Trigger Circuit Breaker (Lockdown)
@app.post("/api/circuit-breaker/trigger")
async def manual_trigger_circuit_breaker():
    bot.system_locked = True
    await bot.log_event("CIRCUIT_BREAKER", "Manual Daily Drawdown Circuit Breaker Triggered by User. Locking system.")
    await bot.emergency_lockdown()
    return {"status": "locked"}

# Reset Circuit Breaker (Unlock)
@app.post("/api/circuit-breaker/reset")
async def reset_circuit_breaker():
    bot.system_locked = False
    # Reset daily starting equity
    bot.daily_start_equity = bot.account_info["balance"]
    bot.account_info["daily_start_equity"] = bot.daily_start_equity
    bot.account_info["daily_drawdown_percent"] = 0.0
    await bot.log_event("CIRCUIT_BREAKER", "Circuit Breaker manually reset by User. System unlocked.")
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
    # Start the continuous price feed loop in the background
    asyncio.create_task(bot.start_price_feed_loop())

@app.on_event("shutdown")
async def shutdown_event():
    if bot.is_running:
        await bot.stop()
    # Close MT5 connection
    if not bot.simulation_mode and MT5_AVAILABLE:
        import MetaTrader5 as mt5  # type: ignore[import-not-found]
        mt5.shutdown()

if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
