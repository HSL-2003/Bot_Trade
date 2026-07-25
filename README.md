---
title: Bot Trade
emoji: 🐢
colorFrom: indigo
colorTo: yellow
sdk: docker
pinned: false
license: mit
app_port: 8000
---

# Confluence Algo Bot & MT5 Trading Terminal

Confluence Algo Bot is an institutional-grade, automated and manual algorithmic trading system designed for financial markets including Gold (XAUUSD), Major Forex Pairs (EURUSD, GBPUSD), and Commodities (USOIL). Built on FastAPI, Asyncio, and MetaTrader 5 (MT5), it provides high-frequency WebSocket data streaming, multi-indicator strategy analysis, dynamic risk management, and a responsive web-based execution dashboard.

---

## Technical Architecture

The application is structured into three primary layers: Frontend UI, Gateway / API Layer, and Core Trading Engine.

```
+-----------------------------------------------------------------------+
|                            Web Interface                              |
|         (Single Page App / WebSockets / Chart.js / TradingView)       |
+-----------------------------------------------------------------------+
                                   |
                   WebSocket (50ms) / REST APIs
                                   v
+-----------------------------------------------------------------------+
|                           FastAPI Gateway                             |
|          (Authentication, Validation, Routing, Endpoints)             |
+-----------------------------------------------------------------------+
                                   |
                                   v
+-----------------------------------------------------------------------+
|                          MT5 Trading Engine                           |
|  - Strategy & Confluence Analyzer (EMA, RSI, Fibonacci, S/R)          |
|  - Execution Layer (MT5 Native Driver & Paper Trading Simulator)      |
|  - Risk Management (Trailing Stop, Breakeven, ROI Exit Table)         |
|  - Safety Controls (News Filter, Spread Filter, Circuit Breaker)      |
+-----------------------------------------------------------------------+
```

### Technology Stack
- Backend Framework: FastAPI (Python 3.10+) with Uvicorn ASGI Server
- Concurrency & Async: Python Asyncio for non-blocking order execution and price streaming
- Broker Connectivity: MetaTrader 5 Python API (MetaTrader5)
- Frontend Interface: Vanilla HTML5, CSS3 (Custom Dark Theme), JavaScript (ES6+)
- Data Visualization: TradingView Advanced Charting Widget, Chart.js Analytics
- Real-Time Communication: WebSockets (/ws) streaming at 20 FPS (50ms refresh rate)
- Containerization: Docker (Non-root user execution, Hugging Face Space compatible)

---

## Key Features

### 1. Algorithmic Strategy & Signal Generation
- Confluence Analysis: Combines 50.0% and 61.8% Fibonacci Retracement levels with key Support and Resistance (S/R) zones.
- Trend & Momentum Alignment: Uses multi-period Exponential Moving Averages (EMA 10, 34, 89, 144, 300) and RSI (14) to confirm directional bias.
- Signal Grading System: Evaluates setups dynamically and assigns quality ratings (1 to 3 stars) based on trend agreement and momentum confluence.

### 2. Manual Order Injection & Control Panel
- Flexible Execution Types: Supports direct Market Execution (BUY, SELL) and Pending Orders (BUY_LIMIT, SELL_LIMIT, BUY_STOP, SELL_STOP).
- Symbol Selection: Allows manual order execution on multiple symbols (XAUUSD, EURUSD, GBPUSD, USOIL) with automatic point size and decimal precision adjustment.
- Dual SL/TP Calculation Modes: Allows defining Stop Loss and Take Profit levels by exact Price or by Points distance.
- Bulk Order Closure: Features a "Close All Positions" emergency button to close 100% of open positions instantaneously.
- Order Modification: Provides an interactive modal dialog to adjust SL and TP levels of active orders dynamically.

### 3. Advanced Risk & Capital Management
- Dynamic Position Sizing: Automatically calculates Lot size based on account balance, target risk percentage, and signal grade.
- Trailing Stop & Breakeven Management: Dynamically locks in profits by moving Stop Loss to Breakeven or trailing price action at specified point steps.
- Time-Based ROI Exit Table: Evaluates open position duration and closes stagnant trades based on configurable ROI threshold matrices.
- Circuit Breaker & Emergency Lockdown: Locks the system and liquidates positions if daily drawdown limits are exceeded.
- Filter Safeguards: Automatically blocks auto-trading entries during high-impact economic news events or when broker spread exceeds allowed limits.

### 4. Dual Execution Engine
- MT5 Real Mode: Executes live orders directly via MetaTrader 5 with automatic order filling mode detection (FOK, IOC, RETURN).
- Paper Trading Simulator: Full simulation mode featuring realistic tick generation, spread accounting, and independent PnL tracking for strategy backtesting and live testing without broker credentials.

---

## API Documentation

### REST API Endpoints

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| GET | / | Serves the web dashboard interface |
| POST | /api/start | Initializes MT5 connection and starts auto-trading loop |
| POST | /api/stop | Pauses auto-trading signal scanning |
| POST | /api/settings | Updates global risk parameters and execution settings |
| POST | /api/trade | Submits a manual market or pending order |
| POST | /api/close/{ticket} | Closes a specific active position by ticket number |
| POST | /api/close-all | Closes all open positions simultaneously |
| POST | /api/cancel-pending/{ticket} | Cancels a pending order |
| POST | /api/modify-sltp/{ticket} | Modifies SL and TP levels for an active position |
| GET | /api/analytics | Returns historical trade metrics and expectancy reports |
| POST | /api/circuit-breaker/trigger | Triggers manual emergency lockdown |
| POST | /api/circuit-breaker/reset | Unlocks system and resets daily drawdown tracking |

### WebSocket Endpoint

- WS /ws: Broadcasts complete system state, live ticks, active positions, pending orders, telemetry logs, and performance metrics every 50ms.

---

## Environment Variables & Configuration

Create a .env file in the root directory to configure broker credentials and application parameters:

```ini
# Application Configuration
PORT=8000
HOST=0.0.0.0

# MetaTrader 5 Credentials (Optional for Simulation Mode)
MT5_PATH=C:\Program Files\MetaTrader 5\terminal64.exe
MT5_LOGIN=12345678
MT5_PASSWORD=YourPasswordHere
MT5_SERVER=YourBroker-Server

# Default Strategy Parameters
SYMBOL=XAUUSD
RISK_PERCENT=1.0
MAX_SPREAD=50
MAX_DAILY_LOSS_PERCENT=5.0
AUTO_TRADING=false
```

---

## Installation & Setup

### Prerequisites
- Python 3.10 or higher
- MetaTrader 5 Terminal (for live execution)
- Git

### Local Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/your-username/bot-trade.git
   cd bot-trade
   ```

2. Create and activate a virtual environment:
   ```bash
   python -m venv venv
   # On Windows:
   venv\Scripts\activate
   # On Linux/macOS:
   source venv/bin/activate
   ```

3. Install required dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Run the application:
   ```bash
   python app.py
   ```
   Access the dashboard at http://127.0.0.1:8000.

### Running with Docker

1. Build the Docker image:
   ```bash
   docker build -t confluence-algo-bot .
   ```

2. Run the container:
   ```bash
   docker run -d -p 8000:8000 --name algo-bot confluence-algo-bot
   ```

---

## License

This project is licensed under the MIT License. See the LICENSE file for details.
