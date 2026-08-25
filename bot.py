import os
import sys
import json
import time
import asyncio
import logging
from datetime import datetime, timedelta, timezone
import random
from typing import Dict, List, Any, Optional, Callable
import httpx
from dotenv import load_dotenv
from core.risk import InstrumentSpec, RiskCalculationError, calculate_volume

# Load environment variables
load_dotenv(override=True)

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("MT5Bot")

# Try to import MetaTrader 5
try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False
    logger.warning("MetaTrader5 library is not installed. Will run in SIMULATION MODE.")

class MT5TradingBot:
    def __init__(self):
        # Configuration parameters
        self.magic_number = int(os.getenv("MAGIC_NUMBER", 20260715))
        self.symbol = os.getenv("DEFAULT_SYMBOL", "XAUUSD")
        self.risk_percent = float(os.getenv("RISK_PERCENT", 1.5))
        self.max_spread = int(os.getenv("MAX_SPREAD", 200))
        self.max_daily_loss_percent = float(os.getenv("MAX_DAILY_LOSS_PERCENT", 5.0))
        self.news_url = os.getenv("FOREX_FACTORY_NEWS_URL", "https://www.forexfactory.com/ffcal_week_this.xml")
        self.news_restriction_minutes = int(os.getenv("NEWS_RESTRICTION_MINUTES", 30))

        # Trailing Stop & Breakeven Parameters (Ponytail rules)
        self.trailing_stop_points = int(os.getenv("TRAILING_STOP_POINTS", 500))
        self.trailing_step_points = int(os.getenv("TRAILING_STEP_POINTS", 500))
        self.trailing_stop_offset_points = int(os.getenv("TRAILING_STOP_OFFSET_POINTS", 1000))
        self.breakeven_trigger_points = int(os.getenv("BREAKEVEN_TRIGGER_POINTS", 500))
        self.breakeven_buffer_points = int(os.getenv("BREAKEVEN_BUFFER_POINTS", 0))
        self.auto_trading = os.getenv("AUTO_TRADING", "true").lower() == "true"

        # Freqtrade-inspired parameters
        self.max_open_trades = int(os.getenv("MAX_OPEN_TRADES", 3000))
        self.cooldown_duration = int(os.getenv("COOLDOWN_DURATION", 300))
        self.daily_profit_target_percent = float(os.getenv("DAILY_PROFIT_TARGET_PERCENT", 7.0)) # ponytail: daily profit target 7%
        self.roi_enabled = os.getenv("ROI_ENABLED", "true").lower() == "true"
        roi_table_str = os.getenv("ROI_TABLE", "0:0.04,30:0.015,60:0.005,120:0.0")
        self.roi_table = {}
        try:
            for item in roi_table_str.split(","):
                k, v = item.split(":")
                self.roi_table[int(k)] = float(v)
        except Exception:
            self.roi_table = {0: 0.04, 30: 0.015, 60: 0.005, 120: 0.0}
        self.pair_locks = {}

        # User & Database Context Hooks
        self.account_id: Optional[str] = None
        self.user_id: Optional[str] = None
        self.on_trade_open: Optional[Callable] = None
        self.on_trade_close: Optional[Callable] = None

        # Bot Runtime States
        self.is_running = False
        self.simulation_mode = not MT5_AVAILABLE
        self.system_locked = False
        self.is_pending_order = False
        self.pending_orders = []
        self.account_info = {
            "balance": 10000.0,
            "equity": 10000.0,
            "margin": 0.0,
            "free_margin": 10000.0,
            "profit": 0.0,
            "daily_start_equity": 10000.0,
            "daily_drawdown_percent": 0.0
        }
        
        # Real-time state fields
        self.current_price = {"bid": 4493.20, "ask": 4493.35, "spread": 15}
        self.watchlist_symbols = ["XAUUSD", "EURUSD", "GBPUSD", "USOIL"]
        self.enabled_symbols = {sym: True for sym in self.watchlist_symbols}
        self.watchlist_data = {
            sym: {"bid": 4493.20 if sym == "XAUUSD" else (1.1685 if sym == "EURUSD" else (1.3613 if sym == "GBPUSD" else 85.00)), "ask": 4493.35 if sym == "XAUUSD" else (1.1687 if sym == "EURUSD" else (1.3615 if sym == "GBPUSD" else 85.04)), "spread": 15, "change": 0.0, "change_abs": 0.0}
            for sym in self.watchlist_symbols
        }
        self.positions: List[Dict[str, Any]] = []
        self.news_events: List[Dict[str, Any]] = []
        self.recent_logs: List[Dict[str, Any]] = []
        self.sr_levels: List[float] = []
        self.sr_levels_all: List[float] = []  # ponytail: full S/R set for confluence matching
        self.fib_levels: Dict[str, float] = {}
        self.confluence_zones: List[Dict[str, Any]] = []
        self.active_signals = []
        self.last_trade_time = 0.0
        self.indicators: Dict[str, Any] = {"rsi": 50.0, "ema_10": 0.0, "ema_34": 0.0, "ema_89": 0.0, "ema_144": 0.0, "ema_300": 0.0, "trend": "NEUTRAL"}
        
        # Simulation Mode state persistence
        self.raw_closes = []
        self.simulation_basis = 0.0
        self._last_tv_update = 0.0
        self.binance_basis = {"XAUUSD": 13.50, "EURUSD": 0.0, "GBPUSD": 0.0, "USOIL": 0.0}
        
        # Trade History and Stats
        self.history: List[Dict[str, Any]] = []
        self.load_history()

        # Threading/Async locks and queues
        self.log_queue = asyncio.Queue()
        self.loop = None

    def load_history(self):
        try:
            if os.path.exists("trade_history.json"):
                with open("trade_history.json", "r") as f:
                    self.history = json.load(f)
        except Exception:
            self.history = []

    def save_history(self):
        try:
            with open("trade_history.json", "w") as f:
                json.dump(self.history, f, indent=4)
        except Exception:
            pass

    def _trigger_trade_open(self, pos: Dict[str, Any], account_id: Optional[str] = None, user_id: Optional[str] = None):
        if callable(self.on_trade_open):
            try:
                acc = account_id or pos.get("account_id") or self.account_id or "demo-account"
                usr = user_id or pos.get("user_id") or self.user_id
                res = self.on_trade_open(acc, usr, pos)
                if asyncio.iscoroutine(res):
                    asyncio.create_task(res)
            except Exception as e:
                logger.error(f"Error in on_trade_open callback: {e}")

    def _trigger_trade_close(self, ticket: int, close_info: Dict[str, Any], account_id: Optional[str] = None, user_id: Optional[str] = None):
        if callable(self.on_trade_close):
            try:
                acc = account_id or close_info.get("account_id") or self.account_id or "demo-account"
                usr = user_id or close_info.get("user_id") or self.user_id
                res = self.on_trade_close(acc, usr, ticket, close_info)
                if asyncio.iscoroutine(res):
                    asyncio.create_task(res)
            except Exception as e:
                logger.error(f"Error in on_trade_close callback: {e}")

    def get_pip_size(self, symbol: str) -> float:
        s = symbol.upper()
        if "XAU" in s:
            return 0.10
        elif "OIL" in s or "USO" in s:
            return 0.01
        elif "JPY" in s:
            return 0.01
        else:
            return 0.0001

    def calculate_trade_pips(self, trade: Dict[str, Any]) -> float:
        symbol = trade.get("symbol", "XAUUSD")
        open_price = float(trade.get("open_price") or trade.get("entry_price") or 0.0)
        close_price = float(trade.get("close_price") or 0.0)
        t_type = (trade.get("type") or trade.get("side") or "BUY").upper()
        pip_size = self.get_pip_size(symbol)
        
        if pip_size <= 0:
            return 0.0
        if t_type == "BUY":
            pips = (close_price - open_price) / pip_size
        else:
            pips = (open_price - close_price) / pip_size
        return round(pips, 1)

    def get_statistics(self) -> Dict[str, Any]:
        total = len(self.history)
        if total == 0:
            return {
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0,
                "total_profit": 0.0,
                "gross_profit": 0.0,
                "gross_loss": 0.0,
                "profit_factor": 0.0,
                "total_pips": 0.0,
                "avg_pips": 0.0,
                "avg_win": 0.0,
                "avg_loss": 0.0,
                "risk_reward_ratio": 0.0,
                "expectancy": 0.0
            }
        wins = [t for t in self.history if float(t.get("profit") or 0) > 0]
        losses = [t for t in self.history if float(t.get("profit") or 0) <= 0]
        
        wins_count = len(wins)
        losses_count = len(losses)
        win_rate = round((wins_count / total) * 100, 2)
        
        gross_profit = sum(float(t.get("profit") or 0) for t in wins)
        gross_loss = sum(abs(float(t.get("profit") or 0)) for t in losses)
        net_profit = round(gross_profit - gross_loss, 2)
        
        profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (round(gross_profit, 2) if gross_profit > 0 else 0.0)
        
        total_pips = round(sum(self.calculate_trade_pips(t) for t in self.history), 1)
        avg_pips = round(total_pips / total, 1)
        
        avg_win = round(gross_profit / wins_count, 2) if wins_count > 0 else 0.0
        avg_loss = round(gross_loss / losses_count, 2) if losses_count > 0 else 0.0
        
        # Risk-Reward Ratio (Average Win / Average Loss)
        rr_ratio = round(avg_win / avg_loss, 2) if avg_loss > 0 else (round(avg_win, 2) if avg_win > 0 else 0.0)
        
        # Expectancy ($ per trade) = (Win Rate % * Avg Win) - (Loss Rate % * Avg Loss)
        win_prob = wins_count / total
        loss_prob = losses_count / total
        expectancy = round((win_prob * avg_win) - (loss_prob * avg_loss), 2)
        
        return {
            "total_trades": total,
            "wins": wins_count,
            "losses": losses_count,
            "win_rate": win_rate,
            "total_profit": net_profit,
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "profit_factor": profit_factor,
            "total_pips": total_pips,
            "avg_pips": avg_pips,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "risk_reward_ratio": rr_ratio,
            "expectancy": expectancy
        }

    def calculate_trade_pips(self, trade: Dict[str, Any]) -> float:
        """Calculate pips gained/lost for a trade"""
        if "pips" in trade and trade["pips"] is not None:
            try:
                return round(float(trade["pips"]), 1)
            except (ValueError, TypeError):
                pass
        sym = str(trade.get("symbol") or "XAUUSD").upper()
        if "XAU" in sym:
            pip_size = 0.10
        elif "OIL" in sym or "USO" in sym or "JPY" in sym:
            pip_size = 0.01
        else:
            pip_size = 0.0001
        open_p = float(trade.get("open_price") or trade.get("entry_price") or 0.0)
        close_p = float(trade.get("close_price") or 0.0)
        if open_p <= 0 or close_p <= 0:
            return 0.0
        side = str(trade.get("type") or trade.get("side") or "BUY").upper()
        if "BUY" in side:
            pips = (close_p - open_p) / pip_size
        else:
            pips = (open_p - close_p) / pip_size
        return round(pips, 1)

    def get_history_analytics(self, period: str = "all", trades: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        now = datetime.now()
        # Prefer the explicit trades source (Supabase). If it is empty or not
        # provided (e.g. Supabase returned no rows / errored), fall back to the
        # local in-memory history so the Closed Trades table never flashes and
        # then disappears purely because the remote query came back empty.
        source_trades = trades if trades else self.history
        filtered = []

        for t in source_trades:
            close_time_str = t.get("close_time") or t.get("closed_at") or t.get("submitted_at")
            if not close_time_str:
                if period == "all": filtered.append(t)
                continue
            try:
                clean_time = str(close_time_str).replace("Z", "+00:00")
                dt = datetime.fromisoformat(clean_time)
                if dt.tzinfo is not None:
                    dt = dt.astimezone().replace(tzinfo=None)
            except Exception:
                if period == "all": filtered.append(t)
                continue
                
            if period == "day" and dt.date() == now.date():
                filtered.append(t)
            elif period == "week" and dt >= (now - timedelta(days=7)):
                filtered.append(t)
            elif period == "month" and (dt.year == now.year and dt.month == now.month):
                filtered.append(t)
            elif period == "all":
                filtered.append(t)
                
        total_trades = len(filtered)
        wins = [t for t in filtered if float(t.get("profit") or 0) > 0]
        losses = [t for t in filtered if float(t.get("profit") or 0) <= 0]
        
        gross_profit = round(sum(float(t.get("profit") or 0) for t in wins), 2)
        gross_loss = round(sum(abs(float(t.get("profit") or 0)) for t in losses), 2)
        net_profit = round(gross_profit - gross_loss, 2)
        
        total_pips = round(sum(self.calculate_trade_pips(t) for t in filtered), 1)
        win_rate = round((len(wins) / total_trades * 100), 2) if total_trades > 0 else 0.0
        profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (round(gross_profit, 2) if gross_profit > 0 else 0.0)
        
        # Calculate estimated account balance
        initial_balance = 10000.0
        if self.account_info and "balance" in self.account_info and self.account_info["balance"] > 0:
            current_bal = self.account_info["balance"]
        else:
            current_bal = initial_balance + sum(float(t.get("profit") or 0) for t in source_trades)
            
        return {
            "period": period,
            "total_trades": total_trades,
            "wins": len(wins),
            "losses": len(losses),
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
            "net_profit": net_profit,
            "total_pips": total_pips,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "account_balance": round(current_bal, 2),
            "trades": self._build_history_trades(filtered)
        }

    def _build_history_trades(self, filtered: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Normalize closed-trade rows for the Closed Trades History table."""
        out = []
        for t in reversed(filtered):
            row = {
                "ticket": t.get("broker_ticket") or t.get("ticket"),
                "symbol": t.get("symbol", "XAUUSD"),
                "type": (t.get("side") or t.get("type") or "BUY").upper(),
                "volume": float(t.get("quantity") or t.get("volume") or 0.01),
                "open_price": float(t.get("entry_price") or t.get("open_price") or 0.0),
                "close_price": float(t.get("close_price") or 0.0),
                "profit": float(t.get("profit") or 0.0),
                "close_time": str(t.get("closed_at") or t.get("close_time") or t.get("submitted_at") or ""),
                "pips": self.calculate_trade_pips(t)
            }
            out.append(row)
        return out

    async def log_event(self, event_type: str, message: str, details: Optional[Dict[str, Any]] = None):
        """Structured logging (Auditing & Telemetry Layer)"""
        timestamp = datetime.now().isoformat()
        log_entry = {
            "timestamp": timestamp,
            "event_type": event_type,
            "message": message,
            "details": details or {}
        }
        # Add to local console and UI tracking
        self.recent_logs.append(log_entry)
        if len(self.recent_logs) > 100:
            self.recent_logs.pop(0)
        
        # Output to terminal
        log_msg = f"[{event_type}] {message}"
        if details:
            log_msg += f" | {json.dumps(details)}"
        logger.info(log_msg)

    async def initialize_mt5(self) -> bool:
        """Initialize connection to MetaTrader 5 terminal"""
        if self.simulation_mode:
            await self.log_event("SYSTEM", "Running in Simulation Mode. MT5 login skipped.")
            return True

        # MT5 initialization inside a thread executor to avoid blocking the main event loop
        def _connect():
            # If path is provided, use it
            path = os.getenv("MT5_PATH")
            login = os.getenv("MT5_LOGIN")
            password = os.getenv("MT5_PASSWORD")
            server = os.getenv("MT5_SERVER")

            if path:
                initialized = mt5.initialize(path=path)
            else:
                initialized = mt5.initialize()

            if not initialized:
                return False

            if login and password and server:
                authorized = mt5.login(login=int(login), password=password, server=server)
                if not authorized:
                    mt5.shutdown()
                    return False
            return True

        success = await asyncio.to_thread(_connect)
        if success:
            self.simulation_mode = False
            await self.log_event("SYSTEM", "MetaTrader 5 connected successfully.", {
                "login": os.getenv("MT5_LOGIN"),
                "server": os.getenv("MT5_SERVER")
            })
            return True
        else:
            self.simulation_mode = True
            await self.log_event("WARNING", "Failed to connect to MT5 terminal. Fallback to Simulation Mode.")
            return True

    async def update_account_state(self):
        """Reconciliation & State Layer: fetch positions and account parameters"""
        if self.simulation_mode:
            # Calculate floating profit on simulated positions
            floating_profit = 0.0
            for pos in self.positions:
                symbol = pos["symbol"]
                sym_price = self.get_current_price_for_symbol(symbol)
                bid = sym_price.get("bid", 0.0)
                ask = sym_price.get("ask", 0.0)
                if bid <= 0 or ask <= 0:
                    continue
                
                multiplier = self.get_symbol_multiplier(symbol)
                current_price = bid if pos["type"] == "BUY" else ask
                pos["current_price"] = round(current_price, 2 if "XAU" in symbol or "USO" in symbol or "OIL" in symbol else 5)
                
                if pos["type"] == "BUY":
                    pos["profit"] = round((bid - pos["open_price"]) * pos["volume"] * multiplier, 2)
                elif pos["type"] == "SELL":
                    pos["profit"] = round((pos["open_price"] - ask) * pos["volume"] * multiplier, 2)
                floating_profit += pos["profit"]
            
            self.account_info["profit"] = round(floating_profit, 2)
            self.account_info["equity"] = round(self.account_info["balance"] + floating_profit, 2)
            self.account_info["free_margin"] = round(self.account_info["equity"] - self.account_info["margin"], 2)
            
            # Daily drawdown calculation
            drawdown = self.account_info["daily_start_equity"] - self.account_info["equity"]
            self.account_info["daily_drawdown_percent"] = round(max(0.0, (drawdown / self.account_info["daily_start_equity"]) * 100), 2)
            
            return

        # MT5 mode
        def _get_account_details():
            acc = mt5.account_info()
            if acc is None:
                return None
            
            # Fetch active positions using Magic Number
            raw_positions = mt5.positions_get(magic=self.magic_number)
            return acc, raw_positions

        res = await asyncio.to_thread(_get_account_details)
        if res is None:
            await self.log_event("ERROR", "Failed to fetch account info from MT5")
            return
        
        acc, raw_positions = res
        self.account_info = {
            "balance": acc.balance,
            "equity": acc.equity,
            "margin": acc.margin,
            "free_margin": acc.margin_free,
            "profit": acc.profit,
            "daily_start_equity": getattr(self, "daily_start_equity", acc.balance), # Fallback to balance if not set
            "daily_drawdown_percent": round(max(0.0, ((self.daily_start_equity - acc.equity) / self.daily_start_equity) * 100), 2)
        }

        # Format open positions
        updated_positions = []
        for pos in raw_positions:
            p_type = "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL"
            pos_open_time = datetime.fromtimestamp(pos.time, timezone.utc).isoformat()
            updated_positions.append({
                "ticket": pos.ticket,
                "symbol": pos.symbol,
                "type": p_type,
                "volume": pos.volume,
                "open_price": pos.price_open,
                "current_price": pos.price_current,
                "sl": pos.sl,
                "tp": pos.tp,
                "profit": pos.profit,
                "magic": pos.magic,
                "open_time": pos_open_time
            })

        # Check for closed positions in MT5 mode (cooldown locks disabled as requested by user)
        # ponytail: disabled pair locks on trade close
        self.positions = updated_positions

        # Check pending limit/stop orders execution trigger
        await self.check_pending_orders()

    async def add_pending_order(self, order_type: str, lot_size: float, trigger_price: float, sl_points: float = 0.0, tp_points: float = 0.0, sl_price: float = 0.0, tp_price: float = 0.0, symbol: str = None) -> dict:
        """Add a limit/stop pending order to the queue"""
        if self.system_locked:
            await self.log_event("EXECUTION_BLOCKED", "Pending order rejected: trading system is emergency-locked.")
            raise RiskCalculationError("Trading system is emergency-locked")
        sym = symbol or self.symbol
        ticket = random.randint(1000000, 9999999)
        dec = 2 if ("XAU" in sym or "USO" in sym or "OIL" in sym) else 5
        
        pending = {
            "ticket": ticket,
            "symbol": sym,
            "type": order_type.upper(),
            "volume": lot_size,
            "trigger_price": round(trigger_price, dec),
            "sl": round(sl_price, dec) if sl_price else 0.0,
            "tp": round(tp_price, dec) if tp_price else 0.0,
            "sl_points": sl_points,
            "tp_points": tp_points,
            "created_at": datetime.now().isoformat()
        }
        self.pending_orders.append(pending)
        await self.log_event("PENDING_ORDER", f"Pending Order Created! Ticket #{ticket} - {pending['type']} {lot_size} Lots at {pending['trigger_price']} on {sym}", pending)
        return pending

    async def cancel_pending_order(self, ticket: int) -> bool:
        """Cancel a pending order by ticket ID"""
        initial_len = len(self.pending_orders)
        self.pending_orders = [p for p in self.pending_orders if p["ticket"] != ticket]
        if len(self.pending_orders) < initial_len:
            await self.log_event("PENDING_ORDER", f"Pending Order #{ticket} cancelled by User.")
            return True
        return False

    async def check_pending_orders(self):
        """Check if market price touched trigger level of any pending orders"""
        if not self.pending_orders:
            return

        to_remove = []
        for p in list(self.pending_orders):
            sym = p["symbol"]
            sym_price = self.get_current_price_for_symbol(sym)
            bid = sym_price.get("bid", 0)
            ask = sym_price.get("ask", 0)
            trig = p["trigger_price"]
            order_type = p["type"]
            
            should_trigger = False
            exec_type = "BUY"
            
            if "BUY" in order_type:
                exec_type = "BUY"
                if "STOP" in order_type:
                    if ask >= trig and ask > 0:
                        should_trigger = True
                else:
                    if ask <= trig and ask > 0:
                        should_trigger = True
            elif "SELL" in order_type:
                exec_type = "SELL"
                if "STOP" in order_type:
                    if bid <= trig and bid > 0:
                        should_trigger = True
                else:
                    if bid >= trig and bid > 0:
                        should_trigger = True

            if should_trigger:
                to_remove.append(p["ticket"])
                await self.log_event("PENDING_TRIGGERED", f"Pending Order #{p['ticket']} triggered at {trig}! Executing Market {exec_type}...")
                point = self.get_symbol_point(sym)
                open_price = ask if exec_type == "BUY" else bid
                
                sl_pts = p["sl_points"]
                tp_pts = p["tp_points"]
                if p.get("sl", 0) > 0:
                    sl_pts = round(abs(open_price - p["sl"]) / point, 1)
                if p.get("tp", 0) > 0:
                    tp_pts = round(abs(open_price - p["tp"]) / point, 1)

                asyncio.create_task(self.execute_market_trade(
                    order_type=exec_type,
                    lot_size=p["volume"],
                    sl_points=sl_pts,
                    tp_points=tp_pts,
                    symbol=sym
                ))

        if to_remove:
            self.pending_orders = [p for p in self.pending_orders if p["ticket"] not in to_remove]

    async def parse_news_data(self, data):
        now_utc = datetime.now(timezone.utc)
        parsed_events = []
        for item in data:
            try:
                dt = datetime.fromisoformat(item['date'])
                dt_utc = dt.astimezone(timezone.utc)
                seconds_remaining = (dt_utc - now_utc).total_seconds()
                
                if item['impact'] in ['High', 'Medium'] and seconds_remaining >= -1800:
                    local_dt = dt_utc.astimezone()
                    parsed_events.append({
                        "title": item['title'],
                        "currency": item['country'],
                        "impact": item['impact'],
                        "time": local_dt.strftime("%H:%M"),
                        "date": local_dt.strftime("%Y-%m-%d"),
                        "seconds_remaining": int(seconds_remaining)
                    })
            except Exception:
                continue
        parsed_events.sort(key=lambda x: x["seconds_remaining"])
        self.news_events = parsed_events

    async def generate_mock_news(self):
        now = datetime.now()
        self.news_events = [
            {
                "title": "US CPI m/m (Inflation)",
                "currency": "USD",
                "impact": "High",
                "time": (now + timedelta(minutes=15)).strftime("%H:%M"),
                "date": now.strftime("%Y-%m-%d"),
                "seconds_remaining": 900
            },
            {
                "title": "Fed Interest Rate Decision",
                "currency": "USD",
                "impact": "High",
                "time": (now + timedelta(hours=2)).strftime("%H:%M"),
                "date": now.strftime("%Y-%m-%d"),
                "seconds_remaining": 7200
            },
            {
                "title": "ECB Press Conference",
                "currency": "EUR",
                "impact": "Medium",
                "time": (now + timedelta(hours=4)).strftime("%H:%M"),
                "date": now.strftime("%Y-%m-%d"),
                "seconds_remaining": 14400
            }
        ]

    async def fetch_news_feed(self):
        """Risk & Filter Layer: Fetch high-impact news items with 1-hour local caching"""
        cache_file = "news_cache.json"
        
        # Check cache validity (1 hour = 3600 seconds)
        use_cache = False
        if os.path.exists(cache_file):
            mtime = os.path.getmtime(cache_file)
            if time.time() - mtime < 3600:
                use_cache = True
                
        if use_cache:
            try:
                with open(cache_file, "r") as f:
                    data = json.load(f)
                await self.parse_news_data(data)
                await self.log_event("SYSTEM", "Loaded economic calendar news from local cache.")
                return
            except Exception as e:
                await self.log_event("WARNING", f"Failed to load news from cache: {str(e)}")

        # Fetch from remote API if cache is invalid or missing
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, headers=headers, timeout=10.0)
                if response.status_code == 200:
                    data = response.json()
                    
                    # Write to cache
                    with open(cache_file, "w") as f:
                        json.dump(data, f)
                        
                    await self.parse_news_data(data)
                    await self.log_event("SYSTEM", "Fetched fresh economic calendar news from NFS and cached it.")
                    return
                else:
                    await self.log_event("WARNING", f"Failed to fetch news from NFS (Status {response.status_code}). Trying to use expired cache or fallback.")
        except Exception as e:
            await self.log_event("WARNING", f"Failed to fetch news feed: {str(e)}. Trying to use expired cache or fallback.")

        # Fallback to expired cache if available
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r") as f:
                    data = json.load(f)
                await self.parse_news_data(data)
                await self.log_event("SYSTEM", "Loaded news from expired cache as fallback.")
                return
            except Exception:
                pass

        # Ultimate fallback to simulated news
        await self.generate_mock_news()

    def run_market_analysis(self):
        """Calculate Support & Resistance and Fibonacci retracements (Strategy)"""
        # Support & Resistance levels from rolling history (simulated or real H1 bars)
        if self.simulation_mode:
            # Generate mock S/R and Fibonacci levels relative to current price if not populated by live history fetcher
            bid = self.current_price["bid"]
            dec = 2 if "XAU" in self.symbol else 5
            
            # If not populated by live API, generate dynamic levels centered around current price
            if not self.sr_levels or abs(self.sr_levels[2] - bid) > (100.0 if "XAU" in self.symbol else 0.05):
                step = 15.0 if "XAU" in self.symbol else 0.0050
                self.sr_levels = [
                    round(bid - 2*step, dec),
                    round(bid - step, dec),
                    round(bid - 0.2*step, dec), # close to bid to trigger signals
                    round(bid + step, dec),
                    round(bid + 2*step, dec)
                ]
                self.sr_levels_all = list(self.sr_levels)
                
                swing_high = bid + 25.0 if "XAU" in self.symbol else bid + 0.0100
                swing_low = bid - 20.0 if "XAU" in self.symbol else bid - 0.0080
                diff = swing_high - swing_low
                
                self.fib_levels = {
                    "0.0%": round(swing_high, dec),
                    "23.6%": round(swing_high - 0.236 * diff, dec),
                    "38.2%": round(swing_high - 0.382 * diff, dec),
                    "50.0%": round(swing_high - 0.500 * diff, dec),
                    "61.8%": round(swing_high - 0.618 * diff, dec),
                    "100.0%": round(swing_low, dec)
                }
        else:
            # Real MT5 M15 data calculation (minimum 350 bars to compute 300-period EMA)
            rates = mt5.copy_rates_from_pos(self.symbol, mt5.TIMEFRAME_M15, 0, 350)
            if rates is not None and len(rates) > 0:
                highs = [r['high'] for r in rates]
                lows = [r['low'] for r in rates]
                closes = [r['close'] for r in rates]
                
                # S/R through simple Peak and Trough detection
                # Let's take local max and min over a rolling window of 5 candles
                all_sr = []
                for i in range(2, len(rates) - 2):
                    if highs[i] == max(highs[i-2:i+3]):
                        all_sr.append(round(highs[i], 2))
                    if lows[i] == min(lows[i-2:i+3]):
                        all_sr.append(round(lows[i], 2))
                
                self.sr_levels_all = sorted(set(all_sr))
                # Keep top 5 unique levels closest to current price (for UI display)
                current = self.current_price["bid"]
                self.sr_levels = sorted(self.sr_levels_all, key=lambda x: abs(x - current))[:5]
                self.sr_levels.sort()

                # Fibonacci swing high & swing low over last 24 candles (6 hours of 15m data)
                recent_highs = highs[-24:]
                recent_lows = lows[-24:]
                swing_high = max(recent_highs)
                swing_low = min(recent_lows)
                diff = swing_high - swing_low
                
                self.fib_levels = {
                    "0.0%": round(swing_high, 2),
                    "23.6%": round(swing_high - 0.236 * diff, 2),
                    "38.2%": round(swing_high - 0.382 * diff, 2),
                    "50.0%": round(swing_high - 0.500 * diff, 2),
                    "61.8%": round(swing_high - 0.618 * diff, 2),
                    "100.0%": round(swing_low, 2)
                }

                # Calculate Indicators in Live MT5 mode (fallback if TV indicators not set)
                # ponytail: use TradingView indicators as primary, fallback to MT5 calculation
                if closes and (not self.indicators or self.indicators.get("rsi") == 50.0 or self.indicators.get("ema_10") == 0.0):
                    rsi_val = self.calculate_rsi(closes, 14)
                    ema_10_val = self.calculate_ema(closes, 10)
                    ema_34_val = self.calculate_ema(closes, 34)
                    ema_89_val = self.calculate_ema(closes, 89)
                    ema_144_val = self.calculate_ema(closes, 144)
                    ema_300_val = self.calculate_ema(closes, 300)
                    
                    last_price = closes[-1]
                    if last_price > ema_300_val and ema_10_val > ema_34_val:
                        trend_val = "BULLISH"
                    elif last_price < ema_300_val and ema_10_val < ema_34_val:
                        trend_val = "BEARISH"
                    else:
                        trend_val = "NEUTRAL"
                    
                    dec = 2 if "XAU" in self.symbol else 5
                    self.indicators = {
                        "rsi": round(rsi_val, 2),
                        "ema_10": round(ema_10_val, dec),
                        "ema_34": round(ema_34_val, dec),
                        "ema_89": round(ema_89_val, dec),
                        "ema_144": round(ema_144_val, dec),
                        "ema_300": round(ema_300_val, dec),
                        "trend": trend_val
                    }

        # Confluence zone detection: Fib 38.2%, 50% or 61.8% close to ANY Support/Resistance level
        self.confluence_zones = []
        point = self.get_symbol_point(self.symbol)
        tolerance = 300 * point  # ponytail: expanded tolerance to 300 points ($3.0 USD for Gold)
        dec = 2 if "XAU" in self.symbol else 5
        # ponytail: use sr_levels_all for matching, sr_levels (top-5) is only for UI display
        sr_pool = self.sr_levels_all if self.sr_levels_all else self.sr_levels
        for fib_name, fib_val in self.fib_levels.items():
            if fib_name in ["38.2%", "50.0%", "61.8%"]:
                for sr_val in sr_pool:
                    if abs(fib_val - sr_val) < tolerance:
                        self.confluence_zones.append({
                            "fib_level": fib_name,
                            "fib_price": fib_val,
                            "sr_price": sr_val,
                            "center_price": round((fib_val + sr_val) / 2, dec)
                        })

    async def execute_market_trade(self, order_type: str, lot_size: float, sl_points: float, tp_points: float, snapshot_price: Optional[Dict[str, float]] = None, symbol: Optional[str] = None):
        """Execution & Self-Healing Layer: Thread-safe order placement with exponential retry backoff.
        snapshot_price: Optional dict {"bid": ..., "ask": ...} captured at signal detection time to prevent slippage.
        """
        if self.system_locked:
            await self.log_event("EXECUTION_BLOCKED", "Market order rejected: trading system is emergency-locked.")
            return

        if symbol is None:
            symbol = self.symbol

        order_type = order_type.upper()
        if order_type not in {"BUY", "SELL"} or lot_size <= 0 or sl_points < 0 or tp_points < 0:
            await self.log_event("EXECUTION_BLOCKED", "Market order rejected: invalid execution parameters.")
            return

        if self.is_pending_order:
            await self.log_event("EXECUTION_BLOCKED", "Cannot place order: Another order is already pending.")
            return

        self.is_pending_order = True
        
        # Anti-slippage: use snapshot price if provided, otherwise fallback to current live price for this specific symbol
        exec_price = snapshot_price if snapshot_price else self.watchlist_data.get(symbol, self.current_price)
        
        # Slippage guard: reject if price drifted too far from snapshot
        if snapshot_price:
            max_slip = 50 * self.get_symbol_point(symbol)  # ponytail: 50 pts = 0.5 USD for Gold/Oil
            live_ref = self.watchlist_data.get(symbol, self.current_price)["ask"] if order_type == "BUY" else self.watchlist_data.get(symbol, self.current_price)["bid"]
            snap_ref = snapshot_price["ask"] if order_type == "BUY" else snapshot_price["bid"]
            if abs(live_ref - snap_ref) > max_slip:
                self.is_pending_order = False
                await self.log_event("SLIPPAGE_REJECT", f"Order rejected: price drifted {abs(live_ref - snap_ref):.2f} from snapshot (max {max_slip:.2f}). Snap={snap_ref}, Live={live_ref}")
                return
        
        await self.log_event("EXECUTION", f"Initiating order send: {order_type} {lot_size} Lots on {symbol}")

        # Exponential backoff retry parameters
        max_retries = 3
        backoff = 1.0

        for attempt in range(1, max_retries + 1):
            try:
                if self.simulation_mode:
                    # Simulation mode order execution
                    await asyncio.sleep(0.2) # Simulate network latency
                    ticket = random.randint(1000000, 9999999)
                    open_price = exec_price["ask"] if order_type == "BUY" else exec_price["bid"]
                    
                    point = self.get_symbol_point(symbol)
                    dec = 2 if "XAU" in symbol or "USO" in symbol or "OIL" in symbol else 5
                    sl_price = (open_price - (sl_points * point) if order_type == "BUY" else open_price + (sl_points * point)) if (sl_points and sl_points > 0) else 0.0
                    tp_price = (open_price + (tp_points * point) if order_type == "BUY" else open_price - (tp_points * point)) if (tp_points and tp_points > 0) else 0.0

                    new_pos = {
                        "ticket": ticket,
                        "symbol": symbol,
                        "type": order_type,
                        "volume": lot_size,
                        "open_price": round(open_price, dec),
                        "current_price": round(open_price, dec),
                        "sl": round(sl_price, dec),
                        "tp": round(tp_price, dec),
                        "profit": 0.0,
                        "magic": self.magic_number,
                        "open_time": datetime.now().isoformat()
                    }
                    self.positions.append(new_pos)
                    self._trigger_trade_open(new_pos)
                    await self.log_event("TRADE_SUCCESS", f"Simulated position opened successfully! Ticket: {ticket}", new_pos)
                    self.is_pending_order = False
                    return

                # Real MT5 Mode execution
                # Prepare MT5 order request structure
                def _place_order():
                    # Check Filling Mode automatically to prevent rejection
                    symbol_info = mt5.symbol_info(symbol)
                    if not symbol_info:
                        return {"success": False, "error": f"Symbol {symbol} not found in MT5"}
                    
                    # Filling mode mapping
                    filling_mode = mt5.ORDER_FILLING_FOK
                    if symbol_info.filling_mode & mt5.SYMBOL_FILLING_IOC:
                        filling_mode = mt5.ORDER_FILLING_IOC
                    elif symbol_info.filling_mode & mt5.SYMBOL_FILLING_FOK:
                        filling_mode = mt5.ORDER_FILLING_FOK
                    else:
                        filling_mode = mt5.ORDER_FILLING_RETURN

                    price = mt5.symbol_info_tick(symbol).ask if order_type == "BUY" else mt5.symbol_info_tick(symbol).bid
                    sl = (price - (sl_points * symbol_info.point) if order_type == "BUY" else price + (sl_points * symbol_info.point)) if (sl_points and sl_points > 0) else 0.0
                    tp = (price + (tp_points * symbol_info.point) if order_type == "BUY" else price - (tp_points * symbol_info.point)) if (tp_points and tp_points > 0) else 0.0

                    request = {
                        "action": mt5.TRADE_ACTION_DEAL,
                        "symbol": symbol,
                        "volume": lot_size,
                        "type": mt5.ORDER_TYPE_BUY if order_type == "BUY" else mt5.ORDER_TYPE_SELL,
                        "price": price,
                        "sl": sl,
                        "tp": tp,
                        "deviation": 20,
                        "magic": self.magic_number,
                        "comment": "Antigravity MT5 Bot",
                        "type_time": mt5.ORDER_TIME_GTC,
                        "type_filling": filling_mode,
                    }

                    result = mt5.order_send(request)
                    return {"success": result.retcode == mt5.TRADE_RETCODE_DONE, "retcode": result.retcode, "comment": result.comment, "result": result}

                # Run blocking order_send in executor thread
                trade_res = await asyncio.to_thread(_place_order)

                if trade_res["success"]:
                    ret_obj = trade_res["result"]
                    open_info = {
                        "ticket": ret_obj.order,
                        "symbol": symbol,
                        "type": order_type,
                        "volume": lot_size,
                        "open_price": ret_obj.price,
                        "sl": sl,
                        "tp": tp,
                        "open_time": datetime.now().isoformat()
                    }
                    self._trigger_trade_open(open_info)
                    await self.log_event("TRADE_SUCCESS", f"Order filled on MT5. Ticket: {ret_obj.order}", {
                        "ticket": ret_obj.order,
                        "price": ret_obj.price,
                        "volume": ret_obj.volume
                    })
                    self.is_pending_order = False
                    return
                else:
                    retcode = trade_res.get("retcode")
                    comment = trade_res.get("comment", "")
                    await self.log_event("TRADE_REJECTED", f"Broker rejected trade. Retcode: {retcode} ({comment})")
                    
                    # Self-Healing Retry logic for specific retryable errors
                    # Requotes, network errors, etc.
                    retryable_codes = [
                        mt5.TRADE_RETCODE_REQUOTE,
                        mt5.TRADE_RETCODE_CONNECTION,
                        mt5.TRADE_RETCODE_PRICE_CHANGED,
                        mt5.TRADE_RETCODE_TIMEOUT
                    ]
                    if retcode in retryable_codes and attempt < max_retries:
                        await self.log_event("RETRY", f"Attempt {attempt} failed with retryable error. Backing off for {backoff}s...")
                        await asyncio.sleep(backoff)
                        backoff *= 2.0 # Exponential multiplier
                    else:
                        break # Non-retryable error

            except Exception as e:
                await self.log_event("EXCEPTION", f"Order execution exception on attempt {attempt}: {str(e)}")
                if attempt < max_retries:
                    await asyncio.sleep(backoff)
                    backoff *= 2.0
                else:
                    break

        # If we broke out of loop or finished attempts without success, reset flag
        self.is_pending_order = False
        await self.log_event("TRADE_ERROR", "Order execution failed after maximum retries.")

    def get_symbol_multiplier(self, symbol: str) -> float:
        s = symbol.upper()
        if "XAU" in s:
            return 100.0
        elif "OIL" in s or "USO" in s:
            return 1000.0
        elif "JPY" in s:
            return 1000.0
        else:
            return 100000.0

    def get_current_price_for_symbol(self, symbol: str) -> Dict[str, float]:
        """Get symbol-specific bid/ask tick from watchlist_data or current_price fallback"""
        if hasattr(self, "watchlist_data") and symbol in self.watchlist_data:
            w_data = self.watchlist_data[symbol]
            if float(w_data.get("bid", 0.0)) > 0:
                return {
                    "bid": float(w_data["bid"]),
                    "ask": float(w_data["ask"]),
                    "spread": w_data.get("spread", 0)
                }
        if hasattr(self, "current_price") and float(self.current_price.get("bid", 0.0)) > 0 and (symbol == self.symbol or "XAU" in symbol):
            return self.current_price
        
        defaults = {
            "XAUUSD": {"bid": 4488.90, "ask": 4490.40, "spread": 15},
            "EURUSD": {"bid": 1.1685, "ask": 1.1687, "spread": 2},
            "GBPUSD": {"bid": 1.3613, "ask": 1.3616, "spread": 3},
            "USOIL": {"bid": 85.00, "ask": 85.03, "spread": 3}
        }
        return defaults.get(symbol, {"bid": 1.0, "ask": 1.0, "spread": 0})

    async def close_position(self, ticket: int):
        """Close an active position"""
        print(f"[LOG][bot.close_position] BẮT ĐẦU đóng ticket={ticket} | simulation_mode={self.simulation_mode} | vị thế đang giữ={len(self.positions)}", flush=True)
        if self.simulation_mode:
            pos_to_close = None
            for p in list(self.positions):
                if str(p.get("ticket")) == str(ticket):
                    pos_to_close = p
                    break
            if pos_to_close:
                sym = pos_to_close["symbol"]
                sym_price = self.get_current_price_for_symbol(sym)
                bid = float(sym_price.get("bid") or 0.0)
                ask = float(sym_price.get("ask") or 0.0)
                
                open_p = float(pos_to_close.get("open_price") or 0.0)
                curr_p = float(pos_to_close.get("current_price") or 0.0)
                dec = 2 if ("XAU" in sym or "USO" in sym or "OIL" in sym) else (3 if "JPY" in sym else 5)
                
                if pos_to_close["type"] == "BUY":
                    close_price = bid if bid > 0 else (curr_p if curr_p > 0 else open_p)
                else:
                    close_price = ask if ask > 0 else (curr_p if curr_p > 0 else open_p)
                
                if close_price <= 0:
                    defaults = {"XAUUSD": 4488.90, "EURUSD": 1.1685, "GBPUSD": 1.3613, "USOIL": 85.00}
                    close_price = defaults.get(sym, 1.0)
                
                multiplier = self.get_symbol_multiplier(sym)
                close_price = round(close_price, dec)
                
                if pos_to_close["type"] == "BUY":
                    profit = round((close_price - open_p) * float(pos_to_close["volume"]) * multiplier, 2)
                else:
                    profit = round((open_p - close_price) * float(pos_to_close["volume"]) * multiplier, 2)
                
                self.positions.remove(pos_to_close)
                
                self.account_info["balance"] = round(self.account_info["balance"] + profit, 2)
                close_record = {
                    "ticket": pos_to_close["ticket"],
                    "symbol": pos_to_close["symbol"],
                    "type": pos_to_close["type"],
                    "volume": float(pos_to_close["volume"]),
                    "open_price": open_p,
                    "close_price": close_price,
                    "profit": profit,
                    "close_time": datetime.now().isoformat()
                }
                self.history.append(close_record)
                self.save_history()
                self._trigger_trade_close(ticket, close_record)
                await self.log_event("TRADE_CLOSE", f"Simulated position closed: Ticket {ticket} ({sym}) at price {close_price} with profit {profit}")
                print(f"[LOG][bot.close_position] SIMULATION: đã đóng ticket={ticket} {sym} giá={close_price} profit={profit}", flush=True)
            else:
                print(f"[LOG][bot.close_position] SIMULATION: KHÔNG TÌM THẤY ticket={ticket} trong {[p.get('ticket') for p in self.positions]}", flush=True)
            return

        # MT5 mode close
        def _close():
            pos = None
            for p in mt5.positions_get(magic=self.magic_number) or []:
                if p.ticket == ticket:
                    pos = p
                    break
            if pos is None:
                for p in mt5.positions_get(ticket=ticket) or []:
                    pos = p
                    break
            if pos is None:
                return False, 0.0, 0.0, None

            symbol_info = mt5.symbol_info(pos.symbol)
            if not symbol_info:
                return False, 0.0, 0.0, None

            filling_mode = mt5.ORDER_FILLING_FOK
            if symbol_info.filling_mode & mt5.SYMBOL_FILLING_IOC:
                filling_mode = mt5.ORDER_FILLING_IOC
            elif symbol_info.filling_mode & mt5.SYMBOL_FILLING_FOK:
                filling_mode = mt5.ORDER_FILLING_FOK
            else:
                filling_mode = mt5.ORDER_FILLING_RETURN

            tick = mt5.symbol_info_tick(pos.symbol)
            tick_price = (tick.bid if pos.type == mt5.POSITION_TYPE_BUY else tick.ask) if tick else getattr(pos, "price_current", 0.0)
            if not tick_price or tick_price <= 0:
                tick_price = getattr(pos, "price_current", 0.0) or getattr(pos, "price_open", 0.0)

            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": pos.symbol,
                "volume": pos.volume,
                "type": mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY,
                "position": pos.ticket,
                "price": tick_price,
                "deviation": 20,
                "magic": self.magic_number,
                "comment": "Close position",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": filling_mode,
            }
            result = mt5.order_send(request)
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                # MT5 order_send result.price is often 0.0 for deal close
                # Fallback cascade: result.price -> tick_price -> pos.price_current -> pos.price_open
                close_p = 0.0
                if hasattr(result, "price") and result.price and float(result.price) > 0:
                    close_p = float(result.price)
                elif tick_price and float(tick_price) > 0:
                    close_p = float(tick_price)
                elif hasattr(pos, "price_current") and pos.price_current and float(pos.price_current) > 0:
                    close_p = float(pos.price_current)
                else:
                    close_p = float(getattr(pos, "price_open", 0.0))

                dec = 2 if ("XAU" in pos.symbol or "USO" in pos.symbol or "OIL" in pos.symbol) else (3 if "JPY" in pos.symbol else 5)
                close_p = round(close_p, dec)

                profit_val = getattr(result, "profit", None)
                if profit_val is None or float(profit_val) == 0.0:
                    profit_val = getattr(pos, "profit", 0.0)
                if float(profit_val) == 0.0 and close_p > 0 and getattr(pos, "price_open", 0.0) > 0:
                    mult = self.get_symbol_multiplier(pos.symbol)
                    if pos.type == mt5.POSITION_TYPE_BUY:
                        profit_val = round((close_p - pos.price_open) * pos.volume * mult, 2)
                    else:
                        profit_val = round((pos.price_open - close_p) * pos.volume * mult, 2)
                else:
                    profit_val = round(float(profit_val), 2)

                return True, close_p, profit_val, pos
            return False, 0.0, 0.0, None

        res_success, close_price, profit_val, pos_obj = await asyncio.to_thread(_close)
        print(f"[LOG][bot.close_position] MT5 result: success={res_success} | close_price={close_price} | profit={profit_val}", flush=True)
        if res_success:
            for p in list(self.positions):
                if int(p.get("ticket", 0)) == int(ticket):
                    self.positions.remove(p)
            close_record = {
                "ticket": ticket,
                "symbol": pos_obj.symbol if pos_obj else "XAUUSD",
                "type": "BUY" if (pos_obj and pos_obj.type == mt5.POSITION_TYPE_BUY) else "SELL",
                "volume": pos_obj.volume if pos_obj else 0.01,
                "open_price": pos_obj.price_open if pos_obj else close_price,
                "close_price": close_price,
                "profit": profit_val,
                "close_time": datetime.now().isoformat()
            }
            self.history.append(close_record)
            self.save_history()
            self._trigger_trade_close(ticket, close_record)
            await self.log_event("TRADE_CLOSE", f"Position {ticket} closed successfully at price {close_price} with profit {profit_val}.")
        else:
            await self.log_event("ERROR", f"Failed to close position {ticket} on MT5.")

    async def modify_position_sltp(self, ticket: int, sl: float, tp: float) -> bool:
        """Modify Stop Loss and Take Profit for an active open position"""
        try:
            # 1. Simulation mode or fallback
            if self.simulation_mode or not MT5_AVAILABLE:
                found = False
                for pos in self.positions:
                    if int(pos.get("ticket", 0)) == int(ticket):
                        dec = 2 if ("XAU" in pos.get("symbol", "") or "OIL" in pos.get("symbol", "") or "USO" in pos.get("symbol", "")) else (3 if "JPY" in pos.get("symbol", "") else 5)
                        pos["sl"] = round(float(sl), dec) if float(sl) > 0 else 0.0
                        pos["tp"] = round(float(tp), dec) if float(tp) > 0 else 0.0
                        found = True
                        await self.log_event("POSITION_MODIFY", f"Simulated Position #{ticket} SL/TP updated: SL={pos['sl']}, TP={pos['tp']}", pos)
                        break
                return found

            # 2. Live MT5 mode
            def _modify():
                positions = mt5.positions_get(ticket=ticket)
                if not positions:
                    return False
                pos = positions[0]
                symbol_info = mt5.symbol_info(pos.symbol)
                digits = symbol_info.digits if symbol_info else 2
                req_sl = round(float(sl), digits) if float(sl) > 0 else 0.0
                req_tp = round(float(tp), digits) if float(tp) > 0 else 0.0
                
                request = {
                    "action": mt5.TRADE_ACTION_SLTP,
                    "position": ticket,
                    "symbol": pos.symbol,
                    "sl": req_sl,
                    "tp": req_tp,
                }
                result = mt5.order_send(request)
                if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                    for local_pos in self.positions:
                        if int(local_pos.get("ticket", 0)) == int(ticket):
                            local_pos["sl"] = req_sl
                            local_pos["tp"] = req_tp
                    return True
                else:
                    err_msg = result.comment if result else "None"
                    logger.error(f"MT5 SL/TP modify failed for #{ticket}: {err_msg}")
                    return False

            res = await asyncio.to_thread(_modify)
            if res:
                await self.log_event("POSITION_MODIFY", f"Position #{ticket} SL/TP modified successfully on MT5: SL={sl}, TP={tp}")
            return res
        except Exception as e:
            logger.error(f"Error modifying SL/TP for #{ticket}: {e}")
            return False

    async def close_all_positions(self):
        """Close all active open positions"""
        await self.log_event("SYSTEM", "Closing all open positions by user request...")
        tickets = [pos["ticket"] for pos in list(self.positions)]
        for ticket in tickets:
            await self.close_position(ticket)

    def get_symbol_point(self, symbol: str) -> float:
        """Helper to get symbol point size"""
        if not self.simulation_mode and MT5_AVAILABLE:
            info = mt5.symbol_info(symbol)
            if info:
                return info.point
        
        # Fallback / Simulation Mode
        symbol_upper = symbol.upper()
        if "JPY" in symbol_upper:
            return 0.001
        elif "XAU" in symbol_upper:
            return 0.01
        elif "USO" in symbol_upper or "OIL" in symbol_upper:
            return 0.01
        else:
            return 0.00001

    def get_symbol_multiplier(self, symbol: str) -> float:
        """Helper to get contract size / profit multiplier for a symbol"""
        if not self.simulation_mode and MT5_AVAILABLE:
            info = mt5.symbol_info(symbol)
            if info:
                return float(info.trade_contract_size)
        
        # Fallback / Simulation Mode
        symbol_upper = symbol.upper()
        if "XAU" in symbol_upper or "USO" in symbol_upper or "OIL" in symbol_upper:
            return 100.0
        else:
            return 100000.0

    async def modify_position_sltp(self, ticket: int, new_sl: float, new_tp: float) -> bool:
        """Modify SL/TP of an active position (Execution Layer)"""
        if self.simulation_mode:
            for pos in self.positions:
                if pos["ticket"] == ticket:
                    pos["sl"] = round(new_sl, 2 if "XAU" in pos["symbol"] else 5)
                    pos["tp"] = round(new_tp, 2 if "XAU" in pos["symbol"] else 5)
                    await self.log_event("SYSTEM", f"Simulated position modified: Ticket {ticket}, SL: {new_sl}, TP: {new_tp}")
                    return True
            return False

        # MT5 mode modification
        def _modify():
            raw_positions = mt5.positions_get(ticket=ticket)
            if not raw_positions or len(raw_positions) == 0:
                return {"success": False, "error": "Position not found"}
            pos = raw_positions[0]
            
            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "position": ticket,
                "symbol": pos.symbol,
                "sl": new_sl,
                "tp": new_tp,
            }
            result = mt5.order_send(request)
            return {"success": result.retcode == mt5.TRADE_RETCODE_DONE, "retcode": result.retcode, "comment": result.comment}

        res = await asyncio.to_thread(_modify)
        if res["success"]:
            await self.log_event("TRADE_MODIFY", f"Position {ticket} SL/TP modified successfully. SL: {new_sl}, TP: {new_tp}")
            return True
        else:
            await self.log_event("ERROR", f"Failed to modify position {ticket}: {res.get('comment')} (code {res.get('retcode')})")
            return False

    async def manage_active_positions(self):
        """Advanced Execution Layer: Trailing Stop, Breakeven, and ROI modifications"""
        if not self.positions:
            return

        for pos in list(self.positions):
            symbol = pos["symbol"]
            ticket = pos["ticket"]
            p_type = pos["type"]
            open_price = pos["open_price"]
            current_sl = pos["sl"]
            current_tp = pos["tp"]
            
            sym_price = self.get_current_price_for_symbol(symbol)
            point = self.get_symbol_point(symbol)
            bid = sym_price["bid"]
            ask = sym_price["ask"]

            # 1. Time-based ROI Exit check
            if self.roi_enabled and pos.get("open_time"):
                open_time_str = pos["open_time"]
                try:
                    open_time = datetime.fromisoformat(open_time_str)
                    if open_time.tzinfo is not None:
                        duration_sec = (datetime.now(timezone.utc) - open_time).total_seconds()
                    else:
                        duration_sec = (datetime.now() - open_time).total_seconds()
                    minutes_open = duration_sec / 60.0
                    
                    # Calculate profit ratio
                    current_ref_price = bid if p_type == "BUY" else ask
                    if p_type == "BUY":
                        profit_ratio = (current_ref_price - open_price) / open_price
                    else:
                        profit_ratio = (open_price - current_ref_price) / open_price
                        
                    # Find matching ROI threshold from table (highest key <= minutes_open)
                    matching_key = None
                    for key in sorted(self.roi_table.keys()):
                        if minutes_open >= key:
                            matching_key = key
                            
                    if matching_key is not None:
                        threshold = self.roi_table[matching_key]
                        if profit_ratio >= threshold:
                            await self.log_event("TRADE_CLOSE", f"ROI Exit triggered for {p_type} #{ticket}. Held for {round(minutes_open, 1)}m (limit >= {matching_key}m), profit {round(profit_ratio * 100, 3)}% (threshold {round(threshold * 100, 2)}%)", {"ticket": ticket, "profit_ratio": profit_ratio, "duration_mins": minutes_open})
                            await self.close_position(ticket)
                            continue
                except Exception as e:
                    logger.error(f"Error checking ROI exit for ticket {ticket}: {e}")

            if p_type == "BUY":
                profit_points = (bid - open_price) / point
                
                # Breakeven
                if profit_points >= self.breakeven_trigger_points:
                    target_be_sl = open_price + (self.breakeven_buffer_points * point)
                    if current_sl == 0 or current_sl < target_be_sl - 1e-9:
                        await self.log_event("BREAKEVEN", f"Breakeven triggered for BUY #{ticket}. Moving SL from {current_sl} to {target_be_sl}", {"ticket": ticket, "open": open_price, "new_sl": target_be_sl})
                        await self.modify_position_sltp(ticket, target_be_sl, current_tp)
                        pos["sl"] = target_be_sl
                        current_sl = target_be_sl

                # Trailing Stop
                if self.trailing_stop_points > 0:
                    if self.trailing_stop_offset_points <= 0 or profit_points >= self.trailing_stop_offset_points:
                        target_trail_sl = bid - (self.trailing_stop_points * point)
                        if current_sl == 0 or target_trail_sl > current_sl + (self.trailing_step_points * point) + 1e-9:
                            await self.log_event("TRAILING_STOP", f"Trailing SL for BUY #{ticket}. Moving SL from {current_sl} to {target_trail_sl}", {"ticket": ticket, "bid": bid, "new_sl": target_trail_sl})
                            await self.modify_position_sltp(ticket, target_trail_sl, current_tp)
                            pos["sl"] = target_trail_sl

            elif p_type == "SELL":
                profit_points = (open_price - ask) / point
                
                # Breakeven
                if profit_points >= self.breakeven_trigger_points:
                    target_be_sl = open_price - (self.breakeven_buffer_points * point)
                    if current_sl == 0 or current_sl > target_be_sl + 1e-9:
                        await self.log_event("BREAKEVEN", f"Breakeven triggered for SELL #{ticket}. Moving SL from {current_sl} to {target_be_sl}", {"ticket": ticket, "open": open_price, "new_sl": target_be_sl})
                        await self.modify_position_sltp(ticket, target_be_sl, current_tp)
                        pos["sl"] = target_be_sl
                        current_sl = target_be_sl

                # Trailing Stop
                if self.trailing_stop_points > 0:
                    if self.trailing_stop_offset_points <= 0 or profit_points >= self.trailing_stop_offset_points:
                        target_trail_sl = ask + (self.trailing_stop_points * point)
                        if current_sl == 0 or target_trail_sl < current_sl - (self.trailing_step_points * point) - 1e-9:
                            await self.log_event("TRAILING_STOP", f"Trailing SL for SELL #{ticket}. Moving SL from {current_sl} to {target_trail_sl}", {"ticket": ticket, "ask": ask, "new_sl": target_trail_sl})
                            await self.modify_position_sltp(ticket, target_trail_sl, current_tp)
                            pos["sl"] = target_trail_sl

    async def check_filters(self, signal_type: str, is_manual: bool = False, symbol: str = None) -> bool:
        """Risk & Filter Layer: Validate spread and news restrictions (drawdown lock disabled)"""
        # Bypass spread and news checks for manual trades
        if is_manual:
            return True

        # Symbol-specific Spread Filter check
        sym = symbol or self.symbol

        # Weekend Market Filter check (Saturday=5, Sunday=6)
        if not is_manual and datetime.now().weekday() in [5, 6]:
            await self.log_event("FILTER_BLOCKED", f"Auto-trading signal ignored for {sym}. Forex market is closed on Weekends (Saturday & Sunday).")
            return False

        sym_price = self.get_current_price_for_symbol(sym)
        spread = sym_price.get("spread", 0)
        
        # ponytail: Higher spread tolerance (100 pts) for Commodities/Gold/Oil
        max_allowed = 100 if ("USO" in sym or "OIL" in sym or "XAU" in sym) else self.max_spread
        if spread > max_allowed:
            await self.log_event("FILTER_BLOCKED", f"Trade ignored for {sym}. High Spread: {spread} points (Max allowed: {max_allowed})")
            return False

        # News Filter check (No trading 30 mins before or after High Impact News)
        for news in self.news_events:
            if news["impact"] == "High":
                time_diff_sec = news["seconds_remaining"]
                # 30 mins = 1800 seconds. If within -1800 to +1800 seconds
                # Note: news countdown is simulated, let's check if remaining seconds is less than 1800
                if 0 <= time_diff_sec <= (self.news_restriction_minutes * 60):
                    await self.log_event("FILTER_BLOCKED", f"Trade ignored. High Impact News upcoming: {news['title']} in {round(time_diff_sec/60, 1)} minutes.")
                    return False
        
        return True

    async def emergency_lockdown(self):
        """Emergency shutdown: Close all trades, lock system"""
        self.system_locked = True
        await self.log_event("EMERGENCY", "LOCKDOWN ACTIVATED! Closing all open positions...")
        tickets = [pos["ticket"] for pos in self.positions]
        for ticket in tickets:
            await self.close_position(ticket)
        await self.log_event("EMERGENCY", "All positions closed. Trading system locked.")

    def calculate_lot_size(self, sl_points: float, risk_percent: float = None, stars_count: int = 1) -> float:
        """Calculate volume from configured money risk; fail closed on bad SL."""
        if sl_points <= 0:
            raise RiskCalculationError("Stop-loss distance is required for risk sizing")
        point = self.get_symbol_point(self.symbol)
        entry = self.current_price["ask"]
        stop = entry - (sl_points * point)
        spec = InstrumentSpec(
            tick_size=point,
            tick_value=point * self.get_symbol_multiplier(self.symbol),
        )
        return calculate_volume(
            equity=self.account_info["equity"],
            risk_percent=risk_percent if risk_percent is not None else self.risk_percent,
            entry_price=entry,
            stop_loss=stop,
            instrument=spec,
        )

    def calculate_ema(self, prices: List[float], period: int) -> float:
        if len(prices) < period:
            return prices[-1] if prices else 0.0
        multiplier = 2.0 / (period + 1.0)
        ema = sum(prices[:period]) / period
        for price in prices[period:]:
            ema = (price - ema) * multiplier + ema
        return ema

    def calculate_rsi(self, prices: List[float], period: int = 14) -> float:
        if len(prices) < period + 1:
            return 50.0
        gains = []
        losses = []
        for i in range(1, len(prices)):
            diff = prices[i] - prices[i-1]
            if diff >= 0:
                gains.append(diff)
                losses.append(0.0)
            else:
                gains.append(0.0)
                losses.append(abs(diff))
                
        # Calculate initial average gain/loss
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
            
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    async def update_live_price(self):
        """Fetch real-time live prices and indicators with 15s TradingView throttling and 1s realtime tick feed"""
        now = time.time()
        should_query_tv = (now - self._last_tv_update) >= 15.0
        
        cfd_res = {}
        forex_res = {}
        if should_query_tv:
            try:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    "Origin": "https://www.tradingview.com",
                    "Referer": "https://www.tradingview.com/"
                }
                async def fetch_cfd():
                    url = "https://scanner.tradingview.com/cfd/scan"
                    payload = {
                        "symbols": {
                            "tickers": ["OANDA:XAUUSD", "FX:USOIL"],
                            "query": { "types": [] }
                        },
                        "columns": [
                            "close", "bid", "ask", "change", "change_abs",
                            "RSI|15", "EMA10|15", "EMA34|15", "EMA89|15", "EMA144|15", "EMA300|15"
                        ]
                    }
                    async with httpx.AsyncClient(headers=headers, timeout=4.0) as client:
                        r = await client.post(url, json=payload)
                        if r.status_code == 200:
                            res = r.json()
                            results = {}
                            for d in res.get("data", []):
                                ticker = d["s"]
                                sym = "XAUUSD" if "XAUUSD" in ticker else "USOIL"
                                results[sym] = d["d"]
                            return results
                    return {}

                async def fetch_forex():
                    url = "https://scanner.tradingview.com/forex/scan"
                    payload = {
                        "symbols": {
                            "tickers": ["OANDA:EURUSD", "OANDA:GBPUSD"],
                            "query": { "types": [] }
                        },
                        "columns": [
                            "close", "bid", "ask", "change", "change_abs",
                            "RSI|15", "EMA10|15", "EMA34|15", "EMA89|15", "EMA144|15", "EMA300|15"
                        ]
                    }
                    async with httpx.AsyncClient(headers=headers, timeout=4.0) as client:
                        r = await client.post(url, json=payload)
                        if r.status_code == 200:
                            res = r.json()
                            results = {}
                            for d in res.get("data", []):
                                sym = d["s"].split(":")[-1]
                                results[sym] = d["d"]
                            return results
                    return {}

                cfd_res, forex_res = await asyncio.gather(fetch_cfd(), fetch_forex(), return_exceptions=True)
                if isinstance(cfd_res, Exception): cfd_res = {}
                if isinstance(forex_res, Exception): forex_res = {}
                self._last_tv_update = now
            except Exception as e:
                logger.debug(f"TradingView scanner throttle note: {e}")
                self._last_tv_update = now + 15.0

            # Process TradingView CFD (XAUUSD & USOIL) indicators & prices
            for sym in ["XAUUSD", "USOIL"]:
                quote = cfd_res.get(sym) if isinstance(cfd_res, dict) else None
                if quote and quote[0] is not None:
                    close = float(quote[0])
                    change = float(quote[3]) if quote[3] is not None else 0.0
                    change_abs = float(quote[4]) if quote[4] is not None else 0.0
                    point = self.get_symbol_point(sym)
                    spread = 15 if sym == "XAUUSD" else 4
                    bid = close
                    ask = round(close + (spread * point), 2)
                    rsi_val = float(quote[5]) if quote[5] is not None else 50.0
                    ema_10_val = float(quote[6]) if quote[6] is not None else close
                    ema_34_val = float(quote[7]) if quote[7] is not None else close
                    ema_89_val = float(quote[8]) if quote[8] is not None else close
                    ema_144_val = float(quote[9]) if quote[9] is not None else close
                    ema_300_val = float(quote[10]) if quote[10] is not None else close
                    trend_val = "BULLISH" if (close > ema_300_val and ema_10_val > ema_34_val) else ("BEARISH" if (close < ema_300_val and ema_10_val < ema_34_val) else "NEUTRAL")
                    
                    self.watchlist_data[sym] = {
                        "bid": bid, "ask": ask, "spread": spread, "change": round(change, 2), "change_abs": round(change_abs, 2),
                        "indicators": {
                            "rsi": round(rsi_val, 2), "ema_10": round(ema_10_val, 2), "ema_34": round(ema_34_val, 2),
                            "ema_89": round(ema_89_val, 2), "ema_144": round(ema_144_val, 2), "ema_300": round(ema_300_val, 2),
                            "trend": trend_val
                        }
                    }

            # Process TradingView Forex
            typical_spreads = {"EURUSD": 12, "GBPUSD": 15}
            for sym in ["EURUSD", "GBPUSD"]:
                quote = forex_res.get(sym) if isinstance(forex_res, dict) else None
                if quote and quote[0] is not None:
                    close = float(quote[0])
                    change = float(quote[3]) if quote[3] is not None else 0.0
                    change_abs = float(quote[4]) if quote[4] is not None else 0.0
                    point = self.get_symbol_point(sym)
                    spread = typical_spreads.get(sym, 15)
                    bid = close
                    ask = round(close + (spread * point), 5)
                    rsi_val = float(quote[5]) if quote[5] is not None else 50.0
                    ema_10_val = float(quote[6]) if quote[6] is not None else close
                    ema_34_val = float(quote[7]) if quote[7] is not None else close
                    ema_89_val = float(quote[8]) if quote[8] is not None else close
                    ema_144_val = float(quote[9]) if quote[9] is not None else close
                    ema_300_val = float(quote[10]) if quote[10] is not None else close
                    trend_val = "BULLISH" if (close > ema_300_val and ema_10_val > ema_34_val) else ("BEARISH" if (close < ema_300_val and ema_10_val < ema_34_val) else "NEUTRAL")
                    self.watchlist_data[sym] = {
                        "bid": bid, "ask": ask, "spread": spread, "change": round(change, 2), "change_abs": round(change_abs, 5),
                        "indicators": {
                            "rsi": round(rsi_val, 2), "ema_10": round(ema_10_val, 5), "ema_34": round(ema_34_val, 5),
                            "ema_89": round(ema_89_val, 5), "ema_144": round(ema_144_val, 5), "ema_300": round(ema_300_val, 5),
                            "trend": trend_val
                        }
                    }

        # Continuous Real-Time Tick Updates (Every 1s loop)
        binance_symbols = {"XAUUSD": "PAXGUSDT", "EURUSD": "EURUSDT", "GBPUSD": "GBPUSDT", "USOIL": "PAXGUSDT"}
        for sym in self.watchlist_symbols:
            current_sym = self.watchlist_data.get(sym, {})
            current_bid = current_sym.get("bid", 0.0)
            point = self.get_symbol_point(sym)
            spread = current_sym.get("spread", 15)
            dec = 2 if "XAU" in sym or "USO" in sym or "OIL" in sym else 5
            
            # Fetch latest price from Binance and apply basis offset to match TradingView OANDA quote
            b_sym = binance_symbols.get(sym)
            if b_sym:
                try:
                    async with httpx.AsyncClient(timeout=1.5) as client:
                        r = await client.get(f"https://api.binance.com/api/v3/ticker/price?symbol={b_sym}")
                        if r.status_code == 200:
                            raw_p = float(r.json()["price"])
                            # Dynamically calibrate basis offset if we have valid TV baseline
                            if should_query_tv and sym in ["XAUUSD", "USOIL"] and current_bid > 3000.0:
                                self.binance_basis[sym] = round(current_bid - raw_p, 2)
                            
                            basis = self.binance_basis.get(sym, 0.0)
                            jitter = random.uniform(-0.03, 0.03) if "XAU" in sym else random.uniform(-0.00003, 0.00003)
                            new_bid = round(raw_p + basis + jitter, dec)
                            current_bid = new_bid
                except Exception:
                    pass

            # Micro-fluctuation fallback if network temporarily delayed
            if current_bid <= 0:
                initial_bids = {"XAUUSD": 4488.90, "EURUSD": 1.1685, "GBPUSD": 1.3613, "USOIL": 85.00}
                current_bid = initial_bids.get(sym, 1.0)
            else:
                jitter = random.uniform(-0.02, 0.02) if "XAU" in sym else random.uniform(-0.00002, 0.00002)
                current_bid = round(current_bid + jitter, dec)

            new_ask = round(current_bid + (spread * point), dec)
            prev_indicators = current_sym.get("indicators", self.indicators)
            
            self.watchlist_data[sym] = {
                **current_sym,
                "bid": current_bid,
                "ask": new_ask,
                "spread": spread,
                "indicators": prev_indicators
            }

        # Sync active selected symbol
        if self.symbol in self.watchlist_data and self.watchlist_data[self.symbol]["bid"] > 0:
            self.current_price = {
                "bid": self.watchlist_data[self.symbol]["bid"],
                "ask": self.watchlist_data[self.symbol]["ask"],
                "spread": self.watchlist_data[self.symbol]["spread"]
            }
            if "indicators" in self.watchlist_data[self.symbol]:
                self.indicators = self.watchlist_data[self.symbol]["indicators"]

    async def update_simulation_history(self):
        """Fetch historical 15m rates from Binance to update S/R and Fib in Simulation Mode"""
        symbols_map = {
            "XAUUSD": "PAXGUSDT",
            "EURUSD": "EURUSDT",
            "GBPUSD": "GBPUSDT",
            "USOIL": "PAXGUSDT"
        }
        b_sym = symbols_map.get(self.symbol, "PAXGUSDT")
        url = f"https://api.binance.com/api/v3/klines?symbol={b_sym}&interval=15m&limit=350"
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(url, timeout=5.0)
                if r.status_code == 200:
                    data = r.json()
                    # index 2: high, 3: low, 4: close
                    highs = [float(k[2]) for k in data]
                    lows = [float(k[3]) for k in data]
                    closes = [float(k[4]) for k in data]
                    
                    if highs and lows:
                        dec = 2 if "XAU" in self.symbol or "USO" in self.symbol or "OIL" in self.symbol else 5
                        
                        # Calculate basis adjustment relative to live TradingView OANDA quote
                        current_live = self.current_price["bid"]
                        last_close = closes[-1]
                        basis = current_live - last_close
                        
                        all_sr = []
                        # peak / trough window 5
                        for i in range(2, len(highs) - 2):
                            if highs[i] == max(highs[i-2:i+3]):
                                all_sr.append(round(highs[i] + basis, dec))
                            if lows[i] == min(lows[i-2:i+3]):
                                all_sr.append(round(lows[i] + basis, dec))
                        
                        self.sr_levels_all = sorted(set(all_sr))
                        # Keep top 5 unique levels closest to current price (for UI display)
                        current = self.current_price["bid"]
                        self.sr_levels = sorted(self.sr_levels_all, key=lambda x: abs(x - current))[:5]
                        self.sr_levels.sort()

                        # Fibonacci swing high & swing low over last 24 15m bars
                        recent_highs = highs[-24:]
                        recent_lows = lows[-24:]
                        swing_high = max(recent_highs) + basis
                        swing_low = min(recent_lows) + basis
                        diff = swing_high - swing_low
                        
                        self.fib_levels = {
                            "0.0%": round(swing_high, dec),
                            "23.6%": round(swing_high - 0.236 * diff, dec),
                            "38.2%": round(swing_high - 0.382 * diff, dec),
                            "50.0%": round(swing_high - 0.500 * diff, dec),
                            "61.8%": round(swing_high - 0.618 * diff, dec),
                            "100.0%": round(swing_low, dec)
                        }
                        
                        # Calculate Indicators
                        if closes:
                            # Save raw closes and basis for S/R calculations
                            self.raw_closes = list(closes)
                            self.simulation_basis = basis
                            
                            adjusted_closes = [c + basis for c in closes]
                            rsi_val = self.calculate_rsi(adjusted_closes, 14)
                            ema_10_val = self.calculate_ema(adjusted_closes, 10)
                            ema_34_val = self.calculate_ema(adjusted_closes, 34)
                            ema_89_val = self.calculate_ema(adjusted_closes, 89)
                            ema_144_val = self.calculate_ema(adjusted_closes, 144)
                            ema_300_val = self.calculate_ema(adjusted_closes, 300)
                            
                            last_price = adjusted_closes[-1]
                            if last_price > ema_300_val and ema_10_val > ema_34_val:
                                trend_val = "BULLISH"
                            elif last_price < ema_300_val and ema_10_val < ema_34_val:
                                trend_val = "BEARISH"
                            else:
                                trend_val = "NEUTRAL"
                            
                            # Only set if TV indicators are not set/valid
                            # ponytail: TV indicators take precedence, Binance is fallback
                            if not self.indicators or self.indicators.get("rsi") == 50.0 or self.indicators.get("ema_10") == 0.0:
                                self.indicators = {
                                    "rsi": round(rsi_val, 2),
                                    "ema_10": round(ema_10_val, dec),
                                    "ema_34": round(ema_34_val, dec),
                                    "ema_89": round(ema_89_val, dec),
                                    "ema_144": round(ema_144_val, dec),
                                    "ema_300": round(ema_300_val, dec),
                                    "trend": trend_val
                                }
                            
                        await self.log_event("SYSTEM", f"Updated Support/Resistance, Fibonacci levels and Indicators (RSI: {self.indicators['rsi']}, Trend: {self.indicators['trend']}) for {self.symbol} (basis: {round(basis, dec)}).")
        except Exception as e:
            await self.log_event("WARNING", f"Failed to fetch simulation history: {str(e)}")

    async def generate_simulated_ticks(self):
        """Monitor simulated positions and SL/TP when the bot is running in Simulation Mode"""
        while self.simulation_mode:
            # Update simulated positions and monitor stop loss / take profit
            for pos in list(self.positions):
                symbol = pos["symbol"]
                sym_price = self.get_current_price_for_symbol(symbol)
                bid = sym_price.get("bid", 0.0)
                ask = sym_price.get("ask", 0.0)
                if bid <= 0 or ask <= 0:
                    continue
                
                # Check exits
                multiplier = self.get_symbol_multiplier(symbol)
                pos["current_price"] = round(bid if pos["type"] == "BUY" else ask, 2 if "XAU" in symbol or "USO" in symbol or "OIL" in symbol else 5)
                
                if pos["type"] == "BUY":
                    pos["profit"] = round((bid - pos["open_price"]) * pos["volume"] * multiplier, 2)
                    if pos["sl"] > 0 and bid <= pos["sl"]:
                        profit = round((pos["sl"] - pos["open_price"]) * pos["volume"] * multiplier, 2)
                        await self.log_event("POSITION_EXIT", f"Simulated SL Hit for Position {pos['ticket']} at {pos['sl']}", pos)
                        self.positions.remove(pos)
                        self.account_info["balance"] = round(self.account_info["balance"] + profit, 2)
                        close_record = {
                            "ticket": pos["ticket"],
                            "symbol": pos["symbol"],
                            "type": pos["type"],
                            "volume": pos["volume"],
                            "open_price": pos["open_price"],
                            "close_price": pos["sl"],
                            "profit": profit,
                            "close_time": datetime.now().isoformat()
                        }
                        self.history.append(close_record)
                        self.save_history()
                        self._trigger_trade_close(pos["ticket"], close_record)
                    elif pos["tp"] > 0 and bid >= pos["tp"]:
                        profit = round((pos["tp"] - pos["open_price"]) * pos["volume"] * multiplier, 2)
                        await self.log_event("POSITION_EXIT", f"Simulated TP Hit for Position {pos['ticket']} at {pos['tp']}", pos)
                        self.positions.remove(pos)
                        self.account_info["balance"] = round(self.account_info["balance"] + profit, 2)
                        close_record = {
                            "ticket": pos["ticket"],
                            "symbol": pos["symbol"],
                            "type": pos["type"],
                            "volume": pos["volume"],
                            "open_price": pos["open_price"],
                            "close_price": pos["tp"],
                            "profit": profit,
                            "close_time": datetime.now().isoformat()
                        }
                        self.history.append(close_record)
                        self.save_history()
                        self._trigger_trade_close(pos["ticket"], close_record)
                elif pos["type"] == "SELL":
                    pos["profit"] = round((pos["open_price"] - ask) * pos["volume"] * multiplier, 2)
                    if pos["sl"] > 0 and ask >= pos["sl"]:
                        profit = round((pos["open_price"] - pos["sl"]) * pos["volume"] * multiplier, 2)
                        await self.log_event("POSITION_EXIT", f"Simulated SL Hit for Position {pos['ticket']} at {pos['sl']}", pos)
                        self.positions.remove(pos)
                        self.account_info["balance"] = round(self.account_info["balance"] + profit, 2)
                        close_record = {
                            "ticket": pos["ticket"],
                            "symbol": pos["symbol"],
                            "type": pos["type"],
                            "volume": pos["volume"],
                            "open_price": pos["open_price"],
                            "close_price": pos["sl"],
                            "profit": profit,
                            "close_time": datetime.now().isoformat()
                        }
                        self.history.append(close_record)
                        self.save_history()
                        self._trigger_trade_close(pos["ticket"], close_record)
                    elif pos["tp"] > 0 and ask <= pos["tp"]:
                        profit = round((pos["open_price"] - pos["tp"]) * pos["volume"] * multiplier, 2)
                        await self.log_event("POSITION_EXIT", f"Simulated TP Hit for Position {pos['ticket']} at {pos['tp']}", pos)
                        self.positions.remove(pos)
                        self.account_info["balance"] = round(self.account_info["balance"] + profit, 2)
                        close_record = {
                            "ticket": pos["ticket"],
                            "symbol": pos["symbol"],
                            "type": pos["type"],
                            "volume": pos["volume"],
                            "open_price": pos["open_price"],
                            "close_price": pos["tp"],
                            "profit": profit,
                            "close_time": datetime.now().isoformat()
                        }
                        self.history.append(close_record)
                        self.save_history()
                        self._trigger_trade_close(pos["ticket"], close_record)
            
            await asyncio.sleep(1.0)

    async def start_price_feed_loop(self):
        """Continuous background loop to update quotes, S/R, Fib and Confluence zones realtime"""
        # Update live price first so self.current_price is accurate!
        try:
            await self.update_live_price()
        except Exception as e:
            logger.error(f"Failed initial live price update: {e}")

        # Initial simulation history load
        if self.simulation_mode:
            try:
                # Load simulation history for ALL symbols initially!
                backup_symbol = self.symbol
                for sym in self.watchlist_symbols:
                    self.symbol = sym
                    self.current_price = {
                        "bid": self.watchlist_data[sym]["bid"],
                        "ask": self.watchlist_data[sym]["ask"],
                        "spread": self.watchlist_data[sym]["spread"]
                    }
                    await self.update_simulation_history()
                    self.watchlist_data[sym]["sr_levels_all"] = self.sr_levels_all
                    self.watchlist_data[sym]["sr_levels"] = self.sr_levels
                    self.watchlist_data[sym]["fib_levels"] = self.fib_levels
                
                self.symbol = backup_symbol
                self.current_price = {
                    "bid": self.watchlist_data[self.symbol]["bid"],
                    "ask": self.watchlist_data[self.symbol]["ask"],
                    "spread": self.watchlist_data[self.symbol]["spread"]
                }
                self.sr_levels_all = self.watchlist_data[self.symbol].get("sr_levels_all", [])
                self.sr_levels = self.watchlist_data[self.symbol].get("sr_levels", [])
                self.fib_levels = self.watchlist_data[self.symbol].get("fib_levels", {})
            except Exception as e:
                logger.error(f"Failed initial simulation history load: {e}")
        
        last_history_update = time.time()
        
        while True:
            try:
                # 1. Update Quotes & Indicators
                # Always fetch indicators and prices from TradingView first
                await self.update_live_price()

                if self.simulation_mode:
                    # Update history every 5 minutes (300 seconds)
                    now = time.time()
                    if now - last_history_update >= 300.0:
                        backup_symbol = self.symbol
                        for sym in self.watchlist_symbols:
                            self.symbol = sym
                            self.current_price = {
                                "bid": self.watchlist_data[sym]["bid"],
                                "ask": self.watchlist_data[sym]["ask"],
                                "spread": self.watchlist_data[sym]["spread"]
                            }
                            await self.update_simulation_history()
                            self.watchlist_data[sym]["sr_levels_all"] = self.sr_levels_all
                            self.watchlist_data[sym]["sr_levels"] = self.sr_levels
                            self.watchlist_data[sym]["fib_levels"] = self.fib_levels
                        self.symbol = backup_symbol
                        self.current_price = {
                            "bid": self.watchlist_data[self.symbol]["bid"],
                            "ask": self.watchlist_data[self.symbol]["ask"],
                            "spread": self.watchlist_data[self.symbol]["spread"]
                        }
                        self.sr_levels_all = self.watchlist_data[self.symbol].get("sr_levels_all", [])
                        self.sr_levels = self.watchlist_data[self.symbol].get("sr_levels", [])
                        self.fib_levels = self.watchlist_data[self.symbol].get("fib_levels", {})
                        last_history_update = now
                else:
                    # MT5 mode: Overwrite prices with broker's execution price, but keep TradingView indicators!
                    if MT5_AVAILABLE:
                        def _get_mt5_watchlist_data():
                            data = {}
                            for sym in self.watchlist_symbols:
                                tick = mt5.symbol_info_tick(sym)
                                if tick:
                                    point = mt5.symbol_info(sym).point
                                    spread = round((tick.ask - tick.bid) / point) if point > 0 else 0
                                    rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_D1, 1, 1)
                                    if rates is not None and len(rates) > 0 and rates[0]['close'] > 0:
                                        prev_close = rates[0]['close']
                                        change_abs = tick.bid - prev_close
                                        change_percent = (change_abs / prev_close) * 100
                                    else:
                                        change_abs = 0.0
                                        change_percent = 0.0
                                    dec = 2 if "XAU" in sym else 5
                                    data[sym] = {
                                        "bid": round(tick.bid, dec),
                                        "ask": round(tick.ask, dec),
                                        "spread": int(spread),
                                        "change": round(change_percent, 2),
                                        "change_abs": round(change_abs, dec)
                                    }
                            return data
                        
                        mt5_data = await asyncio.to_thread(_get_mt5_watchlist_data)
                        for sym, info in mt5_data.items():
                            tv_indicators = self.watchlist_data.get(sym, {}).get("indicators", {
                                "rsi": 50.0, "ema_10": info["bid"], "ema_34": info["bid"],
                                "ema_89": info["bid"], "ema_144": info["bid"], "ema_300": info["bid"],
                                "trend": "NEUTRAL"
                            })
                            self.watchlist_data[sym] = {
                                **info,
                                "indicators": tv_indicators
                            }
                            if sym == self.symbol:
                                self.current_price = {
                                    "bid": info["bid"],
                                    "ask": info["ask"],
                                    "spread": info["spread"]
                                }
                                self.indicators = tv_indicators

                # 2. Run Market Analysis and Scan signals for ALL watchlist symbols
                backup_symbol = self.symbol
                self.active_signals = []
                for sym in self.watchlist_symbols:
                    self.symbol = sym
                    self.current_price = {
                        "bid": self.watchlist_data[sym]["bid"],
                        "ask": self.watchlist_data[sym]["ask"],
                        "spread": self.watchlist_data[sym]["spread"]
                    }
                    self.indicators = self.watchlist_data[sym]["indicators"]
                    self.sr_levels_all = self.watchlist_data[sym].get("sr_levels_all", [])
                    self.sr_levels = self.watchlist_data[sym].get("sr_levels", [])
                    self.fib_levels = self.watchlist_data[sym].get("fib_levels", {})
                    self.confluence_zones = self.watchlist_data[sym].get("confluence_zones", [])
                    
                    self.run_market_analysis()
                    
                    self.watchlist_data[sym]["sr_levels_all"] = self.sr_levels_all
                    self.watchlist_data[sym]["sr_levels"] = self.sr_levels
                    self.watchlist_data[sym]["fib_levels"] = self.fib_levels
                    self.watchlist_data[sym]["confluence_zones"] = self.confluence_zones
                    
                    if self.is_running:
                        await self.scan_market_signals()
                
                # Restore the active selected symbol for the web dashboard displays
                self.symbol = backup_symbol
                self.current_price = {
                    "bid": self.watchlist_data[self.symbol]["bid"],
                    "ask": self.watchlist_data[self.symbol]["ask"],
                    "spread": self.watchlist_data[self.symbol]["spread"]
                }
                self.indicators = self.watchlist_data[self.symbol]["indicators"]
                self.sr_levels_all = self.watchlist_data[self.symbol].get("sr_levels_all", [])
                self.sr_levels = self.watchlist_data[self.symbol].get("sr_levels", [])
                self.fib_levels = self.watchlist_data[self.symbol].get("fib_levels", {})
                self.confluence_zones = self.watchlist_data[self.symbol].get("confluence_zones", [])

                # Ensure simulated tick monitoring task is active for manual positions
                if self.simulation_mode and (not hasattr(self, "_sim_tick_task") or self._sim_tick_task.done()):
                    self._sim_tick_task = asyncio.create_task(self.generate_simulated_ticks())

                # Always update position PnL, current_price, and check pending order triggers 24/7
                await self.update_account_state()
                await self.check_pending_orders()

            except Exception as e:
                logger.error(f"Error in continuous price feed loop: {e}")
            
            # Sleep 1.5 seconds for extremely smooth real-time update
            await asyncio.sleep(1.5)

    async def scan_market_signals(self):
        """Analyze current price vs Confluence Zones and generate graded trade signals"""
        bid = self.current_price["bid"]
        ask = self.current_price["ask"]
        point = self.get_symbol_point(self.symbol)
        dec = 2 if "XAU" in self.symbol or "USO" in self.symbol or "OIL" in self.symbol else 5

        # Indicators helper
        rsi = self.indicators.get("rsi", 50.0)
        trend = self.indicators.get("trend", "NEUTRAL")

        # ponytail: Wave trading — 2-way trading enabled (Trend filter disabled per user request)
        allowed_direction = None

        # 1. Fibonacci Confluence zone scanning
        for zone in self.confluence_zones:
            diff = abs(bid - zone["center_price"])
            
            # ponytail: Expanded trigger window to 300 points ($3.0 USD) to catch entries easily
            if diff <= (300 * point):
                is_buy_signal = bid > zone["sr_price"] and zone["fib_level"] in ["50.0%", "61.8%"]
                is_sell_signal = bid < zone["sr_price"] and zone["fib_level"] in ["50.0%", "61.8%"]
                
                if not is_buy_signal and not is_sell_signal:
                    continue
                    
                sig_type = "BUY" if is_buy_signal else "SELL"

                # Wave filter: allowed_direction is None -> allow 2-way trading
                if allowed_direction is not None and sig_type != allowed_direction:
                    continue
                
                # Grade logic: base 2 stars
                stars_val = 2
                
                # Check trend alignment
                if (sig_type == "BUY" and trend == "BULLISH") or (sig_type == "SELL" and trend == "BEARISH"):
                    stars_val += 1
                    
                # Check RSI agreement (Relaxed to 45/55)
                if (sig_type == "BUY" and rsi <= 45) or (sig_type == "SELL" and rsi >= 55):
                    stars_val += 1
                
                # Cap at 3 stars
                stars_val = min(3, stars_val)
                
                signal = {
                    "symbol": self.symbol,
                    "type": sig_type,
                    "price": round(bid, dec),
                    "fib_level": zone["fib_level"],
                    "fib_price": zone["fib_price"],
                    "sr_price": zone["sr_price"],
                    "strength": "High" if stars_val == 3 else "Medium",
                    "strength_stars": "⭐" * stars_val,
                    "win_probability": 92 if stars_val >= 3 else (78 if stars_val == 2 else 65)
                }
                
                if not any(s["symbol"] == self.symbol and s["type"] == sig_type and s["price"] == round(bid, dec) for s in self.active_signals):
                    self.active_signals.append(signal)

        # 2. Multi-Indicator Setup (Fallback strategy)
        # Check if we already have a signal for THIS specific symbol
        has_symbol_signal = any(sig["symbol"] == self.symbol for sig in self.active_signals)
        if not has_symbol_signal and rsi is not None:
            ema_10 = self.indicators.get("ema_10", 0.0)
            ema_34 = self.indicators.get("ema_34", 0.0)
            
            # RSI Reversal Strategy (Relaxed: BUY when RSI < 42, SELL when RSI > 58)
            # ponytail: relaxed RSI thresholds to < 42 and > 58 as requested by user
            is_buy = rsi < 42
            is_sell = rsi > 58
            
            if is_buy or is_sell:
                sig_type = "BUY" if is_buy else "SELL"
                
                # Fallback setups are baseline 1 star or 2 star if rsi is deeply oversold/overbought
                stars_val = 1
                if (sig_type == "BUY" and rsi < 25) or (sig_type == "SELL" and rsi > 75):
                    stars_val = 2
                    
                signal = {
                    "symbol": self.symbol,
                    "type": sig_type,
                    "price": round(bid, dec),
                    "fib_level": "RSI Reversal",
                    "fib_price": ema_10,
                    "sr_price": ema_34,
                    "strength": "Medium" if stars_val == 2 else "Low",
                    "strength_stars": "⭐" * stars_val,
                    "win_probability": 78 if stars_val == 2 else 65
                }
                if not any(s["symbol"] == self.symbol and s["type"] == sig_type and s["price"] == round(bid, dec) for s in self.active_signals):
                    self.active_signals.append(signal)

        # 3. Process Active Signals for THIS symbol
        for sig in [s for s in self.active_signals if s["symbol"] == self.symbol]:
            sig_type = sig["type"]
            stars_str = sig.get("strength_stars", "⭐")
            stars_count = len(stars_str)
            
            # Skip signals below 1 star (Relaxed from 2 stars)
            # ponytail: relaxed to allow 1-star entries (RSI < 35 or > 65) as requested
            if stars_count < 1:
                continue
                
            now_ts = time.time()
            cooldown_passed = (now_ts - self.last_trade_time) >= 15.0
            
            # ponytail: nhồi lệnh allowed — no distance guard, stacking enabled
            
            if cooldown_passed and not self.is_pending_order:
                # 1. Max Open Trades Guard
                if len(self.positions) >= self.max_open_trades:
                    await self.log_event("EXECUTION_BLOCKED", f"Signal {sig_type} blocked: Max open trades limit reached ({len(self.positions)}/{self.max_open_trades})")
                    continue

                if not self.auto_trading:
                    await self.log_event("SIGNAL", f"{sig_type} ({stars_str}) setup detected at {sig['price']}. Auto Trading is OFF, skipping entry.")
                elif not self.enabled_symbols.get(self.symbol, True):
                    await self.log_event("SIGNAL", f"{sig_type} ({stars_str}) setup detected for {self.symbol} at {sig['price']}. Auto Trading for {self.symbol} is DISABLED, skipping entry.")
                else:
                    passed = await self.check_filters(sig_type, symbol=self.symbol)
                    if passed:
                        # ponytail: R:R = 1:3 (SL = 5 giá = 500 points, TP = 15 giá = 1500 points)
                        sl_points = 500
                        tp_points = 1500
                        
                        # Dynamically scale risk based on star count: 3 stars = 100%, 2 stars = 66%, 1 star = 33%
                        if stars_count >= 3:
                            scaled_risk = self.risk_percent
                        elif stars_count == 2:
                            scaled_risk = self.risk_percent * 0.66
                        else:
                            scaled_risk = self.risk_percent * 0.33
                        lot_size = self.calculate_lot_size(sl_points, scaled_risk, stars_count)
                        
                        # Update last trade time to block concurrent spam
                        self.last_trade_time = now_ts
                        
                        # Snapshot current price to prevent slippage during async execution
                        price_snapshot = {"bid": bid, "ask": ask, "spread": self.current_price.get("spread", 0)}
                        
                        # Place trade with frozen price
                        asyncio.create_task(self.execute_market_trade(
                            order_type=sig_type,
                            lot_size=lot_size,
                            sl_points=sl_points,
                            tp_points=tp_points,
                            snapshot_price=price_snapshot,
                            symbol=self.symbol
                        ))

    async def start(self):
        """Start the background event loop for the trading bot"""
        if self.is_running:
            return
        
        self.is_running = True
        self.daily_start_equity = self.account_info["balance"]
        self.account_info["daily_start_equity"] = self.daily_start_equity
        await self.log_event("SYSTEM", "Trading Bot Started successfully.")

        # Trigger simulated tick feed in background if in simulation mode
        if self.simulation_mode:
            asyncio.create_task(self.generate_simulated_ticks())

        # Main Loop: runs periodically
        while self.is_running:
            try:
                # Update ticks from MT5 if connected
                if not self.simulation_mode and MT5_AVAILABLE:
                    def _get_mt5_watchlist_data():
                        data = {}
                        for sym in self.watchlist_symbols:
                            tick = mt5.symbol_info_tick(sym)
                            if tick:
                                point = mt5.symbol_info(sym).point
                                spread = round((tick.ask - tick.bid) / point) if point > 0 else 0
                                
                                # Fetch daily bar to get previous day's close for change calculation
                                rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_D1, 1, 1)
                                if rates is not None and len(rates) > 0 and rates[0]['close'] > 0:
                                    prev_close = rates[0]['close']
                                    change_abs = tick.bid - prev_close
                                    change_percent = (change_abs / prev_close) * 100
                                else:
                                    change_abs = 0.0
                                    change_percent = 0.0
                                    
                                dec = 2 if "XAU" in sym else 5
                                data[sym] = {
                                    "bid": round(tick.bid, dec),
                                    "ask": round(tick.ask, dec),
                                    "spread": int(spread),
                                    "change": round(change_percent, 2),
                                    "change_abs": round(change_abs, dec)
                                }
                        return data
                    
                    mt5_data = await asyncio.to_thread(_get_mt5_watchlist_data)
                    for sym, info in mt5_data.items():
                        tv_indicators = self.watchlist_data.get(sym, {}).get("indicators", {
                            "rsi": 50.0, "ema_10": info["bid"], "ema_34": info["bid"],
                            "ema_89": info["bid"], "ema_144": info["bid"], "ema_300": info["bid"],
                            "trend": "NEUTRAL"
                        })
                        self.watchlist_data[sym] = {
                            **info,
                            "indicators": tv_indicators
                        }
                        if sym == self.symbol:
                            self.current_price = {
                                "bid": info["bid"],
                                "ask": info["ask"],
                                "spread": info["spread"]
                            }

                # Update news Remaining times
                for news in self.news_events:
                    if news["seconds_remaining"] > 0:
                        news["seconds_remaining"] -= 5

                # Perform analysis
                self.run_market_analysis()

                # ponytail: scan_market_signals moved to start_price_feed_loop (1.5s) for tighter timing

                # Reconcile position state
                await self.update_account_state()

                # Manage active positions (Trailing Stop & Breakeven)
                await self.manage_active_positions()

            except Exception as e:
                await self.log_event("ERROR", f"Error in core bot event loop: {str(e)}")

            await asyncio.sleep(5.0) # Core loop interval (5 seconds)

    async def stop(self):
        """Stop the trading bot execution"""
        if not self.is_running:
            return
        
        self.is_running = False
        await self.log_event("SYSTEM", "Trading Bot stopped by User.")
