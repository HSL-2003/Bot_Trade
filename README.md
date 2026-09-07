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
├── supabase/schema.sql    # Supabase tables, indexes, triggers, and RLS
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

## Supabase Setup

The project includes `supabase/schema.sql`, which creates the tables required for persistent accounts, user sessions, and trade history:

- `user_profiles`: application profile and soft-delete status for each Supabase Auth user.
- `bot_types`: reusable bot configuration templates with risk levels and trading parameters.
- `trading_accounts`: account scope, settings, owner, bot type assignment, and active/archive status.
- `user_sessions`: persistent sessions with expiry and revoke status. Only a hash of the bearer token is stored.
- `trade_orders`: order and trade history. Rows are retained and transitioned through statuses instead of being deleted.
- `trade_order_events`: optional audit trail for broker events and status changes.

### Create the database schema

1. Create a project at https://supabase.com.
2. Open **SQL Editor** in the Supabase dashboard.
3. Open and run [`supabase/schema.sql`](supabase/schema.sql).
4. In **Project Settings > API**, copy the project URL and keys into `.env`.

```ini
SUPABASE_URL=https://your-project-ref.supabase.co
SUPABASE_ANON_KEY=your-anon-key
SUPABASE_SERVICE_ROLE_KEY=your-service-role-key
```

The service-role key bypasses Row Level Security and must only be used by the backend. Never expose it in frontend JavaScript, HTML, screenshots, or Git.

The current account adapter uses `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` to persist `trading_accounts`. The development session service is still in-memory; to persist login sessions, replace it with a Supabase-backed session service that stores `sha256(token)` in `user_sessions`, and updates `revoked_at`, `status`, and `is_active` instead of deleting rows. Order execution should insert/update `trade_orders` and append state changes to `trade_order_events`.

Recommended order status flow:

```text
submitted -> pending -> filled -> closed
submitted -> rejected
pending   -> cancelled
```

For statistics, query retained history by `account_id`, time range, and `status` rather than deleting users or orders. For example, closed trades can be selected with `status = 'closed'` and `is_active = true`.

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

### Admin Endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/admin/overview` | Admin dashboard with KPIs and account list |
| GET | `/api/admin/accounts` | List all trading accounts |
| GET | `/api/admin/accounts/{id}` | Get account details |
| POST | `/api/admin/accounts/{id}/status` | Update account status |
| POST | `/api/admin/accounts/{id}/lock` | Lock/unlock account |
| POST | `/api/admin/accounts/{id}/bot-type` | Assign bot type to account |
| GET | `/api/admin/trades` | List all trades across accounts |

### Bot Types Management (NEW)

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/admin/bot-types` | List all bot configuration templates |
| GET | `/api/admin/bot-types/{id}` | Get bot type details with usage stats |
| POST | `/api/admin/bot-types` | Create new bot type |
| PUT | `/api/admin/bot-types/{id}` | Update bot type configuration |
| PATCH | `/api/admin/bot-types/{id}` | Partial update bot type |
| DELETE | `/api/admin/bot-types/{id}` | Delete bot type (soft/hard) |

See [Bot Types API Documentation](docs/API_BOT_TYPES.md) for detailed usage and examples.

The complete request and response schemas are available through Swagger UI at http://127.0.0.1:8000/docs.

## Security

Hardening already implemented in the codebase:

- **Rate limiting** on authentication endpoints (SlowAPI).
- **Security headers** middleware (CSP, HSTS, X-Frame-Options, nosniff).
- **CSRF protection** (double-submit cookie) for cookie-authenticated changes;
  requests authenticated with `Authorization: Bearer` are exempt.
- **HttpOnly session cookie** (`session_token`, `SameSite=Strict`) set on
  login/register; API clients may still use the `Authorization` header.
- **WebSocket authentication** on `/ws` (token via query param or session cookie).
- **Request size limit** (1 MB) to harden against oversized-payload DoS.
- **Correlation ids** (`X-Correlation-Id`) across responses and security logs.
- **Pinned dependencies** in `requirements.txt`.

Operational requirements before production deployment:

1. **Rotate credentials.** If the keys in your local `.env` were ever exposed,
   regenerate `SUPABASE_ANON_KEY` and `SUPABASE_SERVICE_ROLE_KEY` in the
   Supabase dashboard immediately, then check Git history for accidental commits:
   ```bash
   git log --all --full-history -- "*/.env"
   git log --all --full-history -S "SUPABASE_SERVICE_ROLE_KEY"
   ```
2. **Use environment variables from the hosting platform** (Railway, Render,
   Fly, etc.) instead of committing a real `.env`. A template lives in `.env.example`.
3. **Serve over HTTPS.** In production put Uvicorn behind a TLS-terminating
   reverse proxy (example in `deploy/nginx.conf`) or set `force_https = true`
   on Fly. Never bind Uvicorn to `0.0.0.0` without a proxy.
4. **Set strict CORS origins** via `ALLOWED_ORIGINS` and keep
   `ENABLE_SDLC_AGENTS=false` unless it is intentionally required.
5. **Keep authentication enabled** (`REQUIRE_AUTH=true` / `ENVIRONMENT=production`)
   on any publicly reachable deployment.

## License

This project is released under the MIT License. See `LICENSE` for details.