import os
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
from bot import MT5TradingBot

app = FastAPI(title="MT5 Confluence Algo Bot")

# Enable CORS for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Instantiate the global Trading Bot
bot = MT5TradingBot()
bot_task = None

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
    type: str # BUY or SELL
    lot_size: float
    sl_points: float
    tp_points: float

class ModifySLTPModel(BaseModel):
    sl: float
    tp: float

# Serve Dashboard frontend
@app.get("/")
async def get_dashboard():
    html_path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>Dashboard HTML template not found!</h1>")

# WebSocket Endpoint for streaming real-time metrics
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.append(websocket)
    try:
        while True:
            # Stream current bot state to the UI every 500ms
            state = {
                "is_running": bot.is_running,
                "simulation_mode": bot.simulation_mode,
                "system_locked": bot.system_locked,
                "is_pending_order": bot.is_pending_order,
                "symbol": bot.symbol,
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
                "max_open_trades": bot.max_open_trades,
                "cooldown_duration": bot.cooldown_duration,
                "roi_enabled": bot.roi_enabled,
                "roi_table": ",".join([f"{k}:{v}" for k, v in bot.roi_table.items()]),
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
async def update_settings(settings: SettingsModel):
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

# Trigger Manual Trade
@app.post("/api/trade")
async def manual_trade(trade: ManualTradeModel):
    if bot.system_locked:
        raise HTTPException(status_code=400, detail="System is locked due to daily risk limit breach.")
    
    # Run filter check
    passed = await bot.check_filters(trade.type)
    if not passed:
        raise HTTPException(status_code=400, detail="Trade rejected by risk filters (spread/drawdown/news).")

    # Place order as background task (non-blocking)
    asyncio.create_task(bot.execute_market_trade(
        order_type=trade.type,
        lot_size=trade.lot_size,
        sl_points=trade.sl_points,
        tp_points=trade.tp_points,
        symbol=bot.symbol
    ))
    return {"status": "order_submitted"}

# Force Close Position
@app.post("/api/close/{ticket}")
async def close_position_endpoint(ticket: int):
    # Close order asynchronously
    asyncio.create_task(bot.close_position(ticket))
    return {"status": "close_submitted", "ticket": ticket}

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
        import MetaTrader5 as mt5
        mt5.shutdown()

if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
