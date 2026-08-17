---
title: Bot Trade
emoji: 📈
colorFrom: indigo
colorTo: yellow
sdk: docker
pinned: false
license: mit
app_port: 8000
---

# Confluence Algo Bot & MT5 Trading Terminal

Ứng dụng giao dịch tự động và thủ công xây dựng trên **FastAPI**, hỗ trợ kết nối **MetaTrader 5** hoặc chạy ở chế độ mô phỏng. Ứng dụng cung cấp dashboard web, REST API, WebSocket realtime, quản lý rủi ro và các cơ chế bảo vệ khi giao dịch.

> ⚠️ Đây là phần mềm giao dịch tài chính. Hãy kiểm thử ở chế độ mô phỏng/demo trước khi sử dụng tài khoản thật.

## Tính năng chính

- Dashboard web theo dõi trạng thái bot, giá, lệnh và lịch sử giao dịch.
- Giao dịch thủ công: `BUY`, `SELL`, `BUY_LIMIT`, `SELL_LIMIT`, `BUY_STOP`, `SELL_STOP`.
- Chạy bot tự động hoặc chế độ mô phỏng khi chưa cài MetaTrader 5.
- Phân tích confluence sử dụng EMA, RSI, Fibonacci và vùng hỗ trợ/kháng cự.
- Quản lý rủi ro theo phần trăm vốn, giới hạn spread và giới hạn lỗ trong ngày.
- Trailing stop, breakeven, ROI table, cooldown và giới hạn số lệnh.
- Emergency lockdown/circuit breaker để khóa hệ thống và đóng vị thế.
- WebSocket `/ws` cung cấp dữ liệu trạng thái realtime.
- Hỗ trợ phạm vi tài khoản và xác thực session tùy chọn.

## Công nghệ

- Python 3.10+
- FastAPI, Uvicorn, Pydantic
- AsyncIO, WebSocket, HTTPX, python-dotenv
- MetaTrader5 Python API *(tùy chọn, cần cho giao dịch thật)*
- HTML, CSS, JavaScript và Three.js

## Cấu trúc project

```text
BOt/
├── app.py                 # FastAPI application và route
├── bot.py                 # Trading engine và simulation mode
├── config.py              # Cấu hình runtime
├── core/risk.py           # Tính volume và chính sách rủi ro
├── services/              # Business services
├── repositories/          # Persistence và repository adapters
├── connectors/            # Connector protocol/transport
├── templates/             # HTML templates
├── static/                # CSS, JavaScript và asset giao diện
├── test_safety.py         # Unit tests
├── requirements.txt
└── Dockerfile
```

## Cài đặt và chạy local

### Yêu cầu

- Python 3.10 trở lên
- Git
- MetaTrader 5 Terminal nếu muốn giao dịch thật

```bash
git clone <URL_REPOSITORY>
cd BOt
python -m venv .venv
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

Linux/macOS:

```bash
source .venv/bin/activate
```

Cài dependency và khởi động:

```bash
pip install -r requirements.txt
uvicorn app:app --host 127.0.0.1 --port 8000 --reload
```

Truy cập:

- Landing page: http://127.0.0.1:8000/
- Dashboard: http://127.0.0.1:8000/app
- Login: http://127.0.0.1:8000/login
- API docs: http://127.0.0.1:8000/docs

## Chạy test

```bash
python -m unittest -v test_safety.py
python -m compileall -q .
```

## Cấu hình môi trường

Tạo file `.env` ở thư mục gốc. Không commit file này lên Git.

```ini
HOST=127.0.0.1
PORT=8000

# MetaTrader 5 - để trống nếu chỉ chạy simulation mode
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

# Supabase tùy chọn
SUPABASE_URL=
SUPABASE_ANON_KEY=
SUPABASE_SERVICE_ROLE_KEY=
```

Symbol được hỗ trợ mặc định: `XAUUSD`, `USOIL`, `EURUSD`, `GBPUSD`.

Nếu không cài MetaTrader 5, bot sẽ tự chạy **Simulation Mode**. Khi cần connector MT5:

```bash
pip install MetaTrader5
```

## Asset 3D

Các model và texture trong `static/models/` có dung lượng lớn nên đã được thêm vào `.gitignore` và loại khỏi Git index. File vẫn cần tồn tại local để màn hình login hiển thị model 3D.

Khi clone project trên máy mới, hãy tải/copy asset vào:

```text
static/models/
```

## Docker

```bash
docker build -t confluence-algo-bot .
docker run --env-file .env -p 8000:8000 confluence-algo-bot
```

## API tiêu biểu

| Method | Endpoint | Mô tả |
|---|---|---|
| GET | `/` | Landing page |
| GET | `/app` | Dashboard |
| GET | `/login` | Trang đăng nhập |
| POST | `/api/start` | Bắt đầu bot |
| POST | `/api/stop` | Dừng bot |
| POST | `/api/settings` | Cập nhật cài đặt |
| POST | `/api/trade` | Gửi lệnh thủ công |
| GET | `/api/analytics` | Thống kê giao dịch |
| POST | `/api/close-all` | Đóng toàn bộ vị thế |
| WS | `/ws` | Stream trạng thái realtime |

Danh sách đầy đủ request/response có tại Swagger UI: http://127.0.0.1:8000/docs.

## License

Project được phát hành theo giấy phép MIT. Xem file `LICENSE` để biết thêm chi tiết.