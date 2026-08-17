# Confluence Algo Bot & MT5 Trading Terminal

An automated and manual trading application built with **FastAPI**, with support for **MetaTrader 5** or simulation mode. The application provides a web dashboard, REST API, real-time WebSocket streaming, risk management, and trading safety controls.

> ⚠️ This software is intended for financial trading. Always test in simulation or demo mode before using a real trading account.

## Features

- Web dashboard for monitoring bot status, prices, orders, and trade history.
- Manual trading orders: `BUY`, `SELL`, `BUY_LIMIT`, `SELL_LIMIT`, `BUY_STOP`, and `SELL_STOP`.
- Automated trading and simulation mode when MetaTrader 5 is not installed.
- Confluence analysis using EMA, RSI, Fibonacci, and support/resistance levels.
- Risk management based on account equity, spread limits, and maximum daily loss.
- Trailing stop, breakeven, ROI table, cooldown, and maximum open trade controls.
- Emergency lockdown and circuit breaker to lock the system and close positions.
- Real-time system state streaming through WebSocket `/ws`.
- Optional account scoping and session authentication.

## Technology Stack

- Python 3.10+
- FastAPI, Uvicorn, and Pydantic
- AsyncIO, WebSocket, HTTPX, and python-dotenv
- MetaTrader5 Python API *(optional and required for live trading)*
- HTML, CSS, JavaScript, and Three.js

## Project Structure

```text
BOt/
├── app.py                 # FastAPI application and routes
├── bot.py                 # Trading engine and simulation mode
├── config.py              # Runtime configuration
├── core/risk.py           # Volume calculation and risk policies
├── services/              # Application services
├── repositories/          # Persistence and repository adapters
├── connectors/            # Connector protocol and transport modules
├── templates/             # HTML templates
├── static/                # CSS, JavaScript, and frontend assets
├── test_safety.py         # Unit tests
├── requirements.txt
└── Dockerfile
```

## Local Installation

### Requirements

- Python 3.10 or newer
- Git
- MetaTrader 5 Terminal for live trading

Clone the repository and create a virtual environment:

```bash
git clone <REPOSITORY_URL>
cd BOt
python -m venv .venv
```

Activate the virtual environment on Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

On Linux/macOS:

```bash
source .venv/bin/activate
```

Install dependencies and start the server:

```bash
pip install -r requirements.txt
uvicorn app:app --host 127.0.0.1 --port 8000 --reload
```

Open the following URLs in your browser:

- Landing page: http://127.0.0.1:8000/
- Dashboard: http://127.0.0.1:8000/app
- Login: http://127.0.0.1:8000/login
- API documentation: http://127.0.0.1:8000/docs

## Running Tests

```bash
python -m unittest -v test_safety.py
python -m compileall -q .
```

## Environment Configuration

Create a `.env` file in the project root. Do not commit this file to Git.

```ini
HOST=127.0.0.1
PORT=8000

# MetaTrader 5 - leave empty when using simulation mode
MT5_PATH=C:\\Program Files\\MetaTrader 5\\terminal64.exe
MT5_LOGIN=
MT5_PASSWORD=
MT5_SERVER=

DEFAULT_SYMBOL=XAUUSD
RISK_PERCENT=1.5
MAX_SPREAD=200
MAX_DAILY_LOSS_PERCENT=5.0
AUTO_TRADING=true

ALLOWED_ORIGINS=http://127.0.0.1:8000
REQUIRE_AUTH=false
DEFAULT_ACCOUNT_ID=demo-account

# Optional Supabase configuration
SUPABASE_URL=
SUPABASE_ANON_KEY=
SUPABASE_SERVICE_ROLE_KEY=
```

The default supported symbols are `XAUUSD`, `USOIL`, `EURUSD`, and `GBPUSD`.

If MetaTrader 5 is not installed, the bot automatically runs in **Simulation Mode**. To install the MetaTrader 5 connector when live trading is required:

```bash
pip install MetaTrader5
```

## 3D Assets

The models and textures in `static/models/` are large and are therefore excluded through `.gitignore` and removed from the Git index. The files must still exist locally for the 3D login scene to render correctly.

When cloning the project on a new machine, download or copy the 3D assets into:

```text
static/models/
```

## Docker

```bash
docker build -t confluence-algo-bot .
docker run --env-file .env -p 8000:8000 confluence-algo-bot
```

## API Overview

| Method | Endpoint | Description |
|---|---|---|
| GET | `/` | Landing page |
| GET | `/app` | Trading dashboard |
| GET | `/login` | Login page |
| POST | `/api/start` | Start the bot |
| POST | `/api/stop` | Stop the bot |
| POST | `/api/settings` | Update trading settings |
| POST | `/api/trade` | Submit a manual order |
| GET | `/api/analytics` | Retrieve trading statistics |
| POST | `/api/close-all` | Close all open positions |
| WS | `/ws` | Stream real-time system state |

The complete request and response schemas are available through Swagger UI at http://127.0.0.1:8000/docs.

## License

This project is released under the MIT License. See `LICENSE` for details.