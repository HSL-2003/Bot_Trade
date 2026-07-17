import os
import sys
import json
import time
import asyncio
import logging
from datetime import datetime, timedelta, timezone
import random
from typing import Dict, List, Any, Optional
import httpx
from dotenv import load_dotenv

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

        # Trailing Stop & Breakeven Parameters
        self.trailing_stop_points = int(os.getenv("TRAILING_STOP_POINTS", 300))
        self.trailing_step_points = int(os.getenv("TRAILING_STEP_POINTS", 50))
        self.trailing_stop_offset_points = int(os.getenv("TRAILING_STOP_OFFSET_POINTS", 150))
        self.breakeven_trigger_points = int(os.getenv("BREAKEVEN_TRIGGER_POINTS", 300))
        self.breakeven_buffer_points = int(os.getenv("BREAKEVEN_BUFFER_POINTS", 10))
        self.auto_trading = os.getenv("AUTO_TRADING", "true").lower() == "true"

        # Freqtrade-inspired parameters
        self.max_open_trades = int(os.getenv("MAX_OPEN_TRADES", 3))
        self.cooldown_duration = int(os.getenv("COOLDOWN_DURATION", 300))
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

        # Bot Runtime States
        self.is_running = False
        self.simulation_mode = not MT5_AVAILABLE
        self.system_locked = False
        self.is_pending_order = False
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
        self.current_price = {"bid": 2350.00, "ask": 2350.15, "spread": 15}
        self.watchlist_symbols = ["XAUUSD", "EURUSD", "GBPUSD"]
        self.watchlist_data = {
            sym: {"bid": 0.0, "ask": 0.0, "spread": 0, "change": 0.0, "change_abs": 0.0}
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

    def get_statistics(self) -> Dict[str, Any]:
        total = len(self.history)
        if total == 0:
            return {
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0,
                "total_profit": 0.0,
                "profit_factor": 0.0
            }
        wins = sum(1 for t in self.history if t["profit"] > 0)
        losses = sum(1 for t in self.history if t["profit"] <= 0)
        win_rate = round((wins / total) * 100, 2)
        total_profit = round(sum(t["profit"] for t in self.history), 2)
        
        gross_profit = sum(t["profit"] for t in self.history if t["profit"] > 0)
        gross_loss = sum(abs(t["profit"]) for t in self.history if t["profit"] < 0)
        
        profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (round(gross_profit, 2) if gross_profit > 0 else 0.0)
        
        return {
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "total_profit": total_profit,
            "profit_factor": profit_factor
        }

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
            bid = self.current_price["bid"]
            ask = self.current_price["ask"]
            
            for pos in self.positions:
                symbol = pos["symbol"]
                multiplier = 100.0 if "XAU" in symbol else 100000.0
                current_price = bid if pos["type"] == "BUY" else ask
                pos["current_price"] = round(current_price, 2 if "XAU" in symbol else 5)
                
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

        # Check for closed positions in MT5 mode to trigger pair lock cooldown
        old_positions_dict = {p["ticket"]: p for p in self.positions}
        new_tickets = {p["ticket"] for p in updated_positions}
        for ticket, pos in old_positions_dict.items():
            if ticket not in new_tickets:
                symbol = pos["symbol"]
                self.pair_locks[symbol] = time.time() + self.cooldown_duration
                await self.log_event("COOLDOWN_LOCK", f"Symbol {symbol} closed (Ticket #{ticket}). Cooldown lock for {self.cooldown_duration}s.")

        self.positions = updated_positions

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
        tolerance = 150 * point
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

    async def execute_market_trade(self, order_type: str, lot_size: float, sl_points: float, tp_points: float, snapshot_price: Optional[Dict[str, float]] = None):
        """Execution & Self-Healing Layer: Thread-safe order placement with exponential retry backoff.
        snapshot_price: Optional dict {"bid": ..., "ask": ...} captured at signal detection time to prevent slippage.
        """
        if self.is_pending_order:
            await self.log_event("EXECUTION_BLOCKED", "Cannot place order: Another order is already pending.")
            return

        self.is_pending_order = True
        
        # Anti-slippage: use snapshot price if provided, otherwise fallback to current live price
        exec_price = snapshot_price if snapshot_price else self.current_price
        
        # Slippage guard: reject if price drifted too far from snapshot
        if snapshot_price:
            max_slip = 50 * self.get_symbol_point(self.symbol)  # ponytail: 50 pts = 0.5 USD for Gold
            live_ref = self.current_price["ask"] if order_type == "BUY" else self.current_price["bid"]
            snap_ref = snapshot_price["ask"] if order_type == "BUY" else snapshot_price["bid"]
            if abs(live_ref - snap_ref) > max_slip:
                self.is_pending_order = False
                await self.log_event("SLIPPAGE_REJECT", f"Order rejected: price drifted {abs(live_ref - snap_ref):.2f} from snapshot (max {max_slip:.2f}). Snap={snap_ref}, Live={live_ref}")
                return
        
        await self.log_event("EXECUTION", f"Initiating order send: {order_type} {lot_size} Lots on {self.symbol}")

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
                    
                    point = self.get_symbol_point(self.symbol)
                    dec = 2 if "XAU" in self.symbol else 5
                    sl_price = open_price - (sl_points * point) if order_type == "BUY" else open_price + (sl_points * point)
                    tp_price = open_price + (tp_points * point) if order_type == "BUY" else open_price - (tp_points * point)

                    new_pos = {
                        "ticket": ticket,
                        "symbol": self.symbol,
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
                    await self.log_event("TRADE_SUCCESS", f"Simulated position opened successfully! Ticket: {ticket}", new_pos)
                    self.is_pending_order = False
                    return

                # Real MT5 Mode execution
                # Prepare MT5 order request structure
                def _place_order():
                    # Check Filling Mode automatically to prevent rejection
                    symbol_info = mt5.symbol_info(self.symbol)
                    if not symbol_info:
                        return {"success": False, "error": "Symbol not found in MT5"}
                    
                    # Filling mode mapping
                    filling_mode = mt5.ORDER_FILLING_FOK
                    if symbol_info.filling_mode & mt5.SYMBOL_FILLING_IOC:
                        filling_mode = mt5.ORDER_FILLING_IOC
                    elif symbol_info.filling_mode & mt5.SYMBOL_FILLING_FOK:
                        filling_mode = mt5.ORDER_FILLING_FOK
                    else:
                        filling_mode = mt5.ORDER_FILLING_RETURN

                    price = mt5.symbol_info_tick(self.symbol).ask if order_type == "BUY" else mt5.symbol_info_tick(self.symbol).bid
                    sl = price - (sl_points * symbol_info.point) if order_type == "BUY" else price + (sl_points * symbol_info.point)
                    tp = price + (tp_points * symbol_info.point) if order_type == "BUY" else price - (tp_points * symbol_info.point)

                    request = {
                        "action": mt5.TRADE_ACTION_DEAL,
                        "symbol": self.symbol,
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

    async def close_position(self, ticket: int):
        """Close an active position"""
        if self.simulation_mode:
            pos_to_close = None
            for p in self.positions:
                if p["ticket"] == ticket:
                    pos_to_close = p
                    break
            if pos_to_close:
                bid = self.current_price["bid"]
                ask = self.current_price["ask"]
                close_price = bid if pos_to_close["type"] == "BUY" else ask
                multiplier = 100.0 if "XAU" in pos_to_close["symbol"] else 100000.0
                
                if pos_to_close["type"] == "BUY":
                    profit = round((close_price - pos_to_close["open_price"]) * pos_to_close["volume"] * multiplier, 2)
                else:
                    profit = round((pos_to_close["open_price"] - close_price) * pos_to_close["volume"] * multiplier, 2)
                
                self.positions.remove(pos_to_close)
                
                # Pair Lock cooldown
                symbol = pos_to_close["symbol"]
                self.pair_locks[symbol] = time.time() + self.cooldown_duration
                
                self.account_info["balance"] = round(self.account_info["balance"] + profit, 2)
                self.history.append({
                    "ticket": pos_to_close["ticket"],
                    "symbol": pos_to_close["symbol"],
                    "type": pos_to_close["type"],
                    "volume": pos_to_close["volume"],
                    "open_price": pos_to_close["open_price"],
                    "close_price": close_price,
                    "profit": profit,
                    "close_time": datetime.now().isoformat()
                })
                self.save_history()
                await self.log_event("TRADE_CLOSE", f"Simulated position closed: Ticket {ticket} with profit {profit}")
                await self.log_event("COOLDOWN_LOCK", f"Symbol {symbol} locked in cooldown for {self.cooldown_duration}s.")
            return

        # MT5 mode close
        def _close():
            pos = None
            for p in mt5.positions_get(magic=self.magic_number):
                if p.ticket == ticket:
                    pos = p
                    break
            if pos is None:
                return False

            symbol_info = mt5.symbol_info(pos.symbol)
            filling_mode = mt5.ORDER_FILLING_FOK
            if symbol_info.filling_mode & mt5.SYMBOL_FILLING_IOC:
                filling_mode = mt5.ORDER_FILLING_IOC
            elif symbol_info.filling_mode & mt5.SYMBOL_FILLING_FOK:
                filling_mode = mt5.ORDER_FILLING_FOK
            else:
                filling_mode = mt5.ORDER_FILLING_RETURN

            price = mt5.symbol_info_tick(pos.symbol).bid if pos.type == mt5.POSITION_TYPE_BUY else mt5.symbol_info_tick(pos.symbol).ask
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": pos.symbol,
                "volume": pos.volume,
                "type": mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY,
                "position": pos.ticket,
                "price": price,
                "deviation": 20,
                "magic": self.magic_number,
                "comment": "Close position",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": filling_mode,
            }
            result = mt5.order_send(request)
            return result.retcode == mt5.TRADE_RETCODE_DONE

        success = await asyncio.to_thread(_close)
        if success:
            symbol = self.symbol
            for p in self.positions:
                if p["ticket"] == ticket:
                    symbol = p["symbol"]
                    break
            self.pair_locks[symbol] = time.time() + self.cooldown_duration
            await self.log_event("TRADE_CLOSE", f"Position {ticket} closed successfully.")
            await self.log_event("COOLDOWN_LOCK", f"Symbol {symbol} locked in cooldown for {self.cooldown_duration}s.")
        else:
            await self.log_event("ERROR", f"Failed to close position {ticket} on MT5.")

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
        else:
            return 0.00001

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
            
            if symbol != self.symbol:
                continue

            point = self.get_symbol_point(symbol)
            bid = self.current_price["bid"]
            ask = self.current_price["ask"]

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

    async def check_filters(self, signal_type: str, is_manual: bool = False) -> bool:
        """Risk & Filter Layer: Validate spread and news restrictions (drawdown lock disabled)"""
        # Circuit Breaker check disabled (ponytail: skipped as requested by user)
        # if self.account_info["daily_drawdown_percent"] >= self.max_daily_loss_percent:
        #     self.system_locked = True
        #     await self.log_event("CIRCUIT_BREAKER", f"DAILY DRAWDOWN CIRCUIT BREAKER TRIGGERED! Drawdown: {self.account_info['daily_drawdown_percent']}%. System Locked.")
        #     # Emergency close all orders
        #     await self.emergency_lockdown()
        #     return False

        # if self.system_locked:
        #     await self.log_event("FILTER_BLOCKED", "Trade ignored. System is currently locked due to daily risk breach.")
        #     return False

        # Bypass spread and news checks for manual trades
        if is_manual:
            return True

        # Spread Filter check
        spread = self.current_price["spread"]
        if spread > self.max_spread:
            await self.log_event("FILTER_BLOCKED", f"Trade ignored. High Spread: {spread} points (Max allowed: {self.max_spread})")
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
        await self.log_event("EMERGENCY", "LOCKDOWN ACTIVATED! Closing all open positions...")
        tickets = [pos["ticket"] for pos in self.positions]
        for ticket in tickets:
            await self.close_position(ticket)
        self.is_running = False
        await self.log_event("EMERGENCY", "All positions closed. Bot execution halted.")

    def calculate_lot_size(self, sl_points: float, risk_percent: float = None, stars_count: int = 1) -> float:
        """Calculate position size dynamically based on account balance and risk parameters"""
        balance = self.account_info["balance"]
        
        # ponytail: Flexible recovery lot sizing for small accounts (e.g., around 50 USD)
        if balance <= 80.0:
            # Recovery mode: trade larger sizes to regain equity based on signal strength
            if stars_count >= 3:
                return 0.05
            elif stars_count == 2:
                return 0.03
            else:
                return 0.02
        elif balance <= 200.0:
            # Stable small account: consistent safe size
            return 0.01
        else:
            # Standard account size: scale lot dynamically with risk settings
            rp = risk_percent if risk_percent is not None else self.risk_percent
            risk_amount = balance * (rp / 100.0)
            lot_size = risk_amount / (sl_points * 1.0)
            return round(max(0.01, min(10.00, lot_size)), 2)

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
        """Fetch real-time live prices and indicators from TradingView APIs for all watchlist symbols in parallel"""
        try:
            async def fetch_cfd():
                url = "https://scanner.tradingview.com/cfd/scan"
                payload = {
                    "symbols": {
                        "tickers": ["OANDA:XAUUSD"],
                        "query": { "types": [] }
                    },
                    "columns": [
                        "close", "bid", "ask", "change", "change_abs",
                        "RSI|15", "EMA10|15", "EMA34|15", "EMA89|15", "EMA144|15", "EMA300|15"
                    ]
                }
                async with httpx.AsyncClient() as client:
                    r = await client.post(url, json=payload, timeout=5.0)
                    if r.status_code == 200:
                        res = r.json()
                        if res.get("data") and len(res["data"]) > 0:
                            return "XAUUSD", res["data"][0]["d"]
                return "XAUUSD", None

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
                async with httpx.AsyncClient() as client:
                    r = await client.post(url, json=payload, timeout=5.0)
                    if r.status_code == 200:
                        res = r.json()
                        results = {}
                        for d in res.get("data", []):
                            sym = d["s"].split(":")[-1]
                            results[sym] = d["d"]
                        return results
                return {}

            cfd_res, forex_res = await asyncio.gather(fetch_cfd(), fetch_forex())
            
            # Process XAUUSD
            sym_xau, quote_xau = cfd_res
            if quote_xau:
                close = float(quote_xau[0])
                change = float(quote_xau[3]) if quote_xau[3] is not None else 0.0
                change_abs = float(quote_xau[4]) if quote_xau[4] is not None else 0.0
                
                point = self.get_symbol_point("XAUUSD")
                spread = 15
                bid = close
                ask = round(close + (spread * point), 2)
                
                # Indicators
                rsi_val = float(quote_xau[5]) if quote_xau[5] is not None else 50.0
                ema_10_val = float(quote_xau[6]) if quote_xau[6] is not None else close
                ema_34_val = float(quote_xau[7]) if quote_xau[7] is not None else close
                ema_89_val = float(quote_xau[8]) if quote_xau[8] is not None else close
                ema_144_val = float(quote_xau[9]) if quote_xau[9] is not None else close
                ema_300_val = float(quote_xau[10]) if quote_xau[10] is not None else close
                
                # Calculate trend
                if close > ema_300_val and ema_10_val > ema_34_val:
                    trend_val = "BULLISH"
                elif close < ema_300_val and ema_10_val < ema_34_val:
                    trend_val = "BEARISH"
                else:
                    trend_val = "NEUTRAL"
                
                self.watchlist_data["XAUUSD"] = {
                    "bid": bid,
                    "ask": ask,
                    "spread": spread,
                    "change": round(change, 2),
                    "change_abs": round(change_abs, 2),
                    "indicators": {
                        "rsi": round(rsi_val, 2),
                        "ema_10": round(ema_10_val, 2),
                        "ema_34": round(ema_34_val, 2),
                        "ema_89": round(ema_89_val, 2),
                        "ema_144": round(ema_144_val, 2),
                        "ema_300": round(ema_300_val, 2),
                        "trend": trend_val
                    }
                }
                if self.symbol == "XAUUSD":
                    self.current_price = {"bid": bid, "ask": ask, "spread": spread}
                    self.indicators = self.watchlist_data["XAUUSD"]["indicators"]

            # Process Forex
            typical_spreads = {
                "EURUSD": 12,
                "GBPUSD": 15
            }
            for sym in ["EURUSD", "GBPUSD"]:
                quote = forex_res.get(sym)
                if quote:
                    close = float(quote[0])
                    change = float(quote[3]) if quote[3] is not None else 0.0
                    change_abs = float(quote[4]) if quote[4] is not None else 0.0
                    
                    point = self.get_symbol_point(sym)
                    spread = typical_spreads.get(sym, 15)
                    bid = close
                    ask = round(close + (spread * point), 5)
                    
                    # Indicators
                    rsi_val = float(quote[5]) if quote[5] is not None else 50.0
                    ema_10_val = float(quote[6]) if quote[6] is not None else close
                    ema_34_val = float(quote[7]) if quote[7] is not None else close
                    ema_89_val = float(quote[8]) if quote[8] is not None else close
                    ema_144_val = float(quote[9]) if quote[9] is not None else close
                    ema_300_val = float(quote[10]) if quote[10] is not None else close
                    
                    # Calculate trend
                    if close > ema_300_val and ema_10_val > ema_34_val:
                        trend_val = "BULLISH"
                    elif close < ema_300_val and ema_10_val < ema_34_val:
                        trend_val = "BEARISH"
                    else:
                        trend_val = "NEUTRAL"
                    
                    self.watchlist_data[sym] = {
                        "bid": bid,
                        "ask": ask,
                        "spread": spread,
                        "change": round(change, 2),
                        "change_abs": round(change_abs, 5),
                        "indicators": {
                            "rsi": round(rsi_val, 2),
                            "ema_10": round(ema_10_val, 5),
                            "ema_34": round(ema_34_val, 5),
                            "ema_89": round(ema_89_val, 5),
                            "ema_144": round(ema_144_val, 5),
                            "ema_300": round(ema_300_val, 5),
                            "trend": trend_val
                        }
                    }
                    if self.symbol == sym:
                        self.current_price = {"bid": bid, "ask": ask, "spread": spread}
                        self.indicators = self.watchlist_data[sym]["indicators"]
            return
        except Exception as e:
            logger.error(f"Error in update_live_price: {e}")

        # Fallback to minor fluctuations if API fails
        for symbol in self.watchlist_symbols:
            if self.watchlist_data[symbol]["bid"] == 0.0:
                initial_bids = {"XAUUSD": 4038.40, "EURUSD": 1.14660, "GBPUSD": 1.35280}
                self.watchlist_data[symbol]["bid"] = initial_bids[symbol]
                
            spread = random.randint(12, 18)
            fluctuation = random.uniform(-0.15, 0.15) if symbol == "XAUUSD" else random.uniform(-0.00015, 0.00015)
            dec = 2 if "XAU" in symbol else 5
            new_bid = round(self.watchlist_data[symbol]["bid"] + fluctuation, dec)
            point = self.get_symbol_point(symbol)
            
            prev_closes = {"XAUUSD": 4045.00, "EURUSD": 1.14600, "GBPUSD": 1.35300}
            change_abs = new_bid - prev_closes[symbol]
            change_percent = (change_abs / prev_closes[symbol]) * 100 if prev_closes[symbol] > 0 else 0.0
            
            # Keep previous indicators if they exist in state, else set default
            prev_indicators = self.watchlist_data[symbol].get("indicators", {
                "rsi": 50.0,
                "ema_10": new_bid,
                "ema_34": new_bid,
                "ema_89": new_bid,
                "ema_144": new_bid,
                "ema_300": new_bid,
                "trend": "NEUTRAL"
            })
            
            self.watchlist_data[symbol] = {
                "bid": new_bid,
                "ask": round(new_bid + (spread * point), dec),
                "spread": spread,
                "change": round(change_percent, 2),
                "change_abs": round(change_abs, dec),
                "indicators": prev_indicators
            }
            
            if symbol == self.symbol:
                self.current_price = {
                    "bid": self.watchlist_data[symbol]["bid"],
                    "ask": self.watchlist_data[symbol]["ask"],
                    "spread": self.watchlist_data[symbol]["spread"]
                }
                self.indicators = prev_indicators

    async def update_simulation_history(self):
        """Fetch historical 15m rates from Binance to update S/R and Fib in Simulation Mode"""
        symbols_map = {
            "XAUUSD": "PAXGUSDT",
            "EURUSD": "EURUSDT",
            "GBPUSD": "GBPUSDT"
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
                        dec = 2 if "XAU" in self.symbol else 5
                        
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
        while self.is_running and self.simulation_mode:
            # Update simulated positions and monitor stop loss / take profit
            for pos in list(self.positions):
                bid = self.current_price["bid"]
                ask = self.current_price["ask"]
                symbol = pos["symbol"]
                
                # Check exits
                multiplier = 100.0 if "XAU" in symbol else 100000.0
                pos["current_price"] = round(bid if pos["type"] == "BUY" else ask, 2 if "XAU" in symbol else 5)
                
                if pos["type"] == "BUY":
                    pos["profit"] = round((bid - pos["open_price"]) * pos["volume"] * multiplier, 2)
                    if bid <= pos["sl"]:
                        profit = round((pos["sl"] - pos["open_price"]) * pos["volume"] * multiplier, 2)
                        await self.log_event("POSITION_EXIT", f"Simulated SL Hit for Position {pos['ticket']} at {pos['sl']}", pos)
                        self.positions.remove(pos)
                        self.pair_locks[symbol] = time.time() + self.cooldown_duration
                        self.account_info["balance"] = round(self.account_info["balance"] + profit, 2)
                        self.history.append({
                            "ticket": pos["ticket"],
                            "symbol": pos["symbol"],
                            "type": pos["type"],
                            "volume": pos["volume"],
                            "open_price": pos["open_price"],
                            "close_price": pos["sl"],
                            "profit": profit,
                            "close_time": datetime.now().isoformat()
                        })
                        self.save_history()
                        await self.log_event("COOLDOWN_LOCK", f"Symbol {symbol} locked in cooldown for {self.cooldown_duration}s.")
                    elif bid >= pos["tp"]:
                        profit = round((pos["tp"] - pos["open_price"]) * pos["volume"] * multiplier, 2)
                        await self.log_event("POSITION_EXIT", f"Simulated TP Hit for Position {pos['ticket']} at {pos['tp']}", pos)
                        self.positions.remove(pos)
                        self.pair_locks[symbol] = time.time() + self.cooldown_duration
                        self.account_info["balance"] = round(self.account_info["balance"] + profit, 2)
                        self.history.append({
                            "ticket": pos["ticket"],
                            "symbol": pos["symbol"],
                            "type": pos["type"],
                            "volume": pos["volume"],
                            "open_price": pos["open_price"],
                            "close_price": pos["tp"],
                            "profit": profit,
                            "close_time": datetime.now().isoformat()
                        })
                        self.save_history()
                        await self.log_event("COOLDOWN_LOCK", f"Symbol {symbol} locked in cooldown for {self.cooldown_duration}s.")
                elif pos["type"] == "SELL":
                    pos["profit"] = round((pos["open_price"] - ask) * pos["volume"] * multiplier, 2)
                    if ask >= pos["sl"]:
                        profit = round((pos["open_price"] - pos["sl"]) * pos["volume"] * multiplier, 2)
                        await self.log_event("POSITION_EXIT", f"Simulated SL Hit for Position {pos['ticket']} at {pos['sl']}", pos)
                        self.positions.remove(pos)
                        self.pair_locks[symbol] = time.time() + self.cooldown_duration
                        self.account_info["balance"] = round(self.account_info["balance"] + profit, 2)
                        self.history.append({
                            "ticket": pos["ticket"],
                            "symbol": pos["symbol"],
                            "type": pos["type"],
                            "volume": pos["volume"],
                            "open_price": pos["open_price"],
                            "close_price": pos["sl"],
                            "profit": profit,
                            "close_time": datetime.now().isoformat()
                        })
                        self.save_history()
                        await self.log_event("COOLDOWN_LOCK", f"Symbol {symbol} locked in cooldown for {self.cooldown_duration}s.")
                    elif ask <= pos["tp"]:
                        profit = round((pos["open_price"] - pos["tp"]) * pos["volume"] * multiplier, 2)
                        await self.log_event("POSITION_EXIT", f"Simulated TP Hit for Position {pos['ticket']} at {pos['tp']}", pos)
                        self.positions.remove(pos)
                        self.pair_locks[symbol] = time.time() + self.cooldown_duration
                        self.account_info["balance"] = round(self.account_info["balance"] + profit, 2)
                        self.history.append({
                            "ticket": pos["ticket"],
                            "symbol": pos["symbol"],
                            "type": pos["type"],
                            "volume": pos["volume"],
                            "open_price": pos["open_price"],
                            "close_price": pos["tp"],
                            "profit": profit,
                            "close_time": datetime.now().isoformat()
                        })
                        self.save_history()
                        await self.log_event("COOLDOWN_LOCK", f"Symbol {symbol} locked in cooldown for {self.cooldown_duration}s.")
            
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
                await self.update_simulation_history()
            except Exception as e:
                logger.error(f"Failed initial simulation history load: {e}")
        
        last_history_update = time.time()
        current_loop_symbol = self.symbol
        
        while True:
            try:
                # Check if symbol changed dynamically
                if self.simulation_mode and self.symbol != current_loop_symbol:
                    logger.info(f"Symbol changed from {current_loop_symbol} to {self.symbol}. Forcing history update.")
                    current_loop_symbol = self.symbol
                    await self.update_live_price()
                    await self.update_simulation_history()
                    last_history_update = time.time()

                # 1. Update Quotes & Indicators
                # Always fetch indicators and prices from TradingView first
                await self.update_live_price()

                if self.simulation_mode:
                    # Update history every 5 minutes (300 seconds)
                    now = time.time()
                    if now - last_history_update >= 300.0:
                        await self.update_simulation_history()
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

                # 2. Run Market Analysis (Updates Fib/SR and Confluence zones)
                self.run_market_analysis()

                # 3. Scan signals immediately after fresh price + analysis (tighter than 5s core loop)
                if self.is_running:
                    await self.scan_market_signals()

            except Exception as e:
                logger.error(f"Error in continuous price feed loop: {e}")
            
            # Sleep 1.5 seconds for extremely smooth real-time update
            await asyncio.sleep(1.5)

    async def scan_market_signals(self):
        """Analyze current price vs Confluence Zones and generate graded trade signals"""
        bid = self.current_price["bid"]
        ask = self.current_price["ask"]
        point = self.get_symbol_point(self.symbol)
        dec = 2 if "XAU" in self.symbol else 5
        
        # Clear signals
        self.active_signals = []

        # Indicators helper
        rsi = self.indicators.get("rsi", 50.0)
        trend = self.indicators.get("trend", "NEUTRAL")

        # ponytail: Wave trading — only trade in trend direction, skip counter-trend signals
        allowed_direction = None
        if trend == "BULLISH":
            allowed_direction = "BUY"
        elif trend == "BEARISH":
            allowed_direction = "SELL"
        else:
            # NEUTRAL: no clear wave, skip signal generation entirely
            return

        # 1. Fibonacci Confluence zone scanning
        for zone in self.confluence_zones:
            diff = abs(bid - zone["center_price"])
            
            # If price hits Confluence Zone (within 100 points range)
            if diff <= (100 * point):
                is_buy_signal = bid > zone["sr_price"] and zone["fib_level"] in ["50.0%", "61.8%"]
                is_sell_signal = bid < zone["sr_price"] and zone["fib_level"] in ["50.0%", "61.8%"]
                
                if not is_buy_signal and not is_sell_signal:
                    continue
                    
                sig_type = "BUY" if is_buy_signal else "SELL"

                # Wave filter: skip counter-trend signals
                if sig_type != allowed_direction:
                    continue
                
                # Grade logic: base 2 stars
                stars_val = 2
                
                # Check trend alignment
                if (sig_type == "BUY" and trend == "BULLISH") or (sig_type == "SELL" and trend == "BEARISH"):
                    stars_val += 1
                    
                # Check RSI agreement
                if (sig_type == "BUY" and rsi <= 38) or (sig_type == "SELL" and rsi >= 62):
                    stars_val += 1
                
                # Cap at 3 stars
                stars_val = min(3, stars_val)
                
                signal = {
                    "type": sig_type,
                    "price": round(bid, dec),
                    "fib_level": zone["fib_level"],
                    "fib_price": zone["fib_price"],
                    "sr_price": zone["sr_price"],
                    "strength": "High" if stars_val == 3 else "Medium",
                    "strength_stars": "⭐" * stars_val
                }
                
                self.active_signals.append(signal)

        # 2. Multi-Indicator Setup (Fallback strategy)
        if len(self.active_signals) == 0 and rsi is not None:
            ema_10 = self.indicators.get("ema_10", 0.0)
            ema_34 = self.indicators.get("ema_34", 0.0)
            
            # RSI Reversal Strategy (Relaxed)
            # ponytail: relaxed RSI thresholds as requested by user
            # Wave filter: only trigger RSI reversal in trend direction
            is_buy = rsi < 35 and allowed_direction == "BUY"
            is_sell = rsi > 65 and allowed_direction == "SELL"
            
            if is_buy or is_sell:
                sig_type = "BUY" if is_buy else "SELL"
                
                # Fallback setups are baseline 1 star or 2 star if rsi is deeply oversold/overbought
                stars_val = 1
                if (sig_type == "BUY" and rsi < 25) or (sig_type == "SELL" and rsi > 75):
                    stars_val = 2
                    
                signal = {
                    "type": sig_type,
                    "price": round(bid, dec),
                    "fib_level": "RSI Reversal",
                    "fib_price": ema_10,
                    "sr_price": ema_34,
                    "strength": "Medium" if stars_val == 2 else "Low",
                    "strength_stars": "⭐" * stars_val
                }
                self.active_signals.append(signal)

        # 3. Process Active Signals
        for sig in self.active_signals:
            sig_type = sig["type"]
            stars_str = sig.get("strength_stars", "⭐")
            stars_count = len(stars_str)
            
            # Skip signals below 1 star (Relaxed from 2 stars)
            # ponytail: relaxed to allow 1-star entries (RSI < 35 or > 65) as requested
            if stars_count < 1:
                continue
                
            now_ts = time.time()
            cooldown_passed = (now_ts - self.last_trade_time) >= 15.0
            
            # Determine minimum distance between entries to avoid excessive spam
            min_dist = 2.0 if "XAU" in self.symbol else 0.0020
            
            # Check if we already have a position too close to this price
            position_too_close = False
            for pos in self.positions:
                if pos["type"] == sig_type and abs(bid - pos["open_price"]) < min_dist:
                    position_too_close = True
                    break
            
            if cooldown_passed and not position_too_close and not self.is_pending_order:
                # 1. Max Open Trades Guard
                if len(self.positions) >= self.max_open_trades:
                    await self.log_event("EXECUTION_BLOCKED", f"Signal {sig_type} blocked: Max open trades limit reached ({len(self.positions)}/{self.max_open_trades})")
                    continue

                # 2. Pair Lock / Symbol Cooldown Guard
                unlock_time = self.pair_locks.get(self.symbol, 0)
                if time.time() < unlock_time:
                    remaining = int(unlock_time - time.time())
                    await self.log_event("EXECUTION_BLOCKED", f"Signal {sig_type} blocked: Symbol {self.symbol} is in cooldown lock for another {remaining}s.")
                    continue

                if not self.auto_trading:
                    await self.log_event("SIGNAL", f"{sig_type} ({stars_str}) setup detected at {sig['price']}. Auto Trading is OFF, skipping entry.")
                else:
                    passed = await self.check_filters(sig_type)
                    if passed:
                        # 300 points Stop Loss, 600 points Take Profit (Risk:Reward = 1:2)
                        sl_points = 300
                        tp_points = 600
                        
                        # Dynamically scale risk: 3 stars = 100% risk, 2 stars = 50% risk
                        scaled_risk = self.risk_percent if stars_count == 3 else (self.risk_percent * 0.5)
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
                            snapshot_price=price_snapshot
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
                if not self.simulation_mode:
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
                        self.watchlist_data[sym] = info
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
