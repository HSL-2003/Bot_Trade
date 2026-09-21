# PROJECT MASTER PLAN — Bot Trade (Confluence Algo Bot)

> **MỤC ĐÍCH CỦA FILE NÀY:** đây là "bộ nhớ ngoài" của dự án. Khi một phiên làm
> việc (window context) mới bắt đầu và không còn nhớ gì, **đọc file này trước
> tiên** — nó mô tả luồng đi, kiến trúc, dữ liệu, các quyết định đã chốt, các
> việc còn dang dở và **những bước phải làm thủ công** (migration, dashboard).
>
> Cập nhật file này mỗi khi: chốt một quyết định kiến trúc, thêm/bớt bảng, hoàn
> thành một phase, hoặc thêm một case validate mới.

**Cập nhật lần cuối:** Pass 6 review + triển khai Notification (Phase B mở màn).

---

## 0. ĐỌC NHANH — 30 giây


| Câu hỏi             | Trả lời                                                                                                                                    |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| Sản phẩm là gì?     | Hệ thống **bán license bot giao dịch** (MT4/MT5) + thuê VPS + vận hành bot hộ khách                                                        |
| Bot chạy ở đâu?     | EA (MQL4/MQL5) biên dịch từ MetaEditor, chạy trên **VPS của công ty** (không cấp RDP cho khách)                                            |
| Backend?            | FastAPI + Supabase (Postgres/Auth/Realtime) + asyncpg direct path                                                                          |
| Mobile?             | Flutter — `C:\Users\ITPC\orca\workspaces\bot_trade\mobile\`                                                                                |
| Web?                | FastAPI Jinja templates: `templates/index.html` (khách), `templates/admin_dashboard.html` (admin)                                          |
| Trạng thái hiện tại | Phase 0 + Phase 1 (async foundation, race fixes, risk engine) **xong**. Phase A–E (thương mại) **chưa code xong**; Notification vừa mở màn |


---

## 1. ⚠️ VIỆC PHẢI LÀM THỦ CÔNG (user phải tự vào chạy)

> Đây là danh sách những việc **không thể tự động hoá** — cần user đăng nhập
> Supabase Dashboard / PayOS / nhà cung cấp VPS. Khi một bước mới phát sinh,
> ghi vào bảng này và **báo user**.


| #   | Việc                                                                              | Ở đâu                             | Trạng thái                   |
| --- | --------------------------------------------------------------------------------- | --------------------------------- | ---------------------------- |
| M1  | Chạy `supabase/consolidated_pending_migration.sql`                                | Supabase → SQL Editor             | ✅ **ĐÃ CHẠY** (verify 2026-09-17) |
| M2  | Chạy `supabase/notification_migration.sql`                                        | Supabase → SQL Editor             | ✅ **ĐÃ CHẠY** (verify 2026-09-17) |
| M2b | Chạy `supabase/phase_a_commerce_migration.sql` | Supabase → SQL Editor | ✅ **ĐÃ CHẠY** (verify 2026-09-18: 13/14 check PASS) |
| M3  | Kiểm tra Realtime đã bật cho `public.notifications`                               | Supabase → Database → Replication | ✅ **ĐÃ BẬT** (verify 2026-09-17) |
| M4  | Set `DATABASE_POOL_URL` (Supabase pooler, port 6543, transaction mode)            | biến môi trường / `.env`          | ✅ **ĐÃ SET + VERIFY** (2026-09-17) |
| M5  | Tạo merchant account PayOS + lấy `PAYOS_CLIENT_ID` / `API_KEY` / `CHECKSUM_KEY`   | PayOS Dashboard                   | ✅ **ĐÃ SET + VERIFY LIVE** (2026-09-18) |
| M6  | Đăng ký webhook URL với PayOS (PayOS sẽ gửi ping xác minh)                        | PayOS Dashboard                   | ✅ **ĐÃ ĐĂNG KÝ** (2026-09-18) |
| M6b | **Test end-to-end: chuyển 10.000 VND cho order 200000005** | Ngân hàng / app PayOS | ✅ **ĐÃ CHUYỂN + HOÀN TẤT** (2026-09-21 08:45:45 UTC) — xem phân tích ở M6c |
| M6c | **PHÂN TÍCH LIVE ĐƠN ĐẦU TIÊN (2026-09-21)** — bằng chứng từ log + DB thật | — | Xem 3 điểm dưới — mục này là sổ tay cho window context sau |

**M6c-1 — Đơn đầu tiên hoàn tất ĐÚNG qua `poll_pending_orders`:**
- `08:45:45 UTC  [INFO] polling completed order 200000005`
- DB: `status=PAID`, `received_amount=10000.00`, `paid_at=08:45:46Z`, license
  `OWNED_INACTIVE` (1 cái), cart mở lại `ACTIVE`.
- Chuỗi chứng minh thiết kế B.8 (không bao giờ phụ thuộc 100% vào webhook).

**M6c-2 — Webhook thật bị TỪ CHỐI chữ ký (BUG MỞ, đã chống + chẩn đoán):**
- Log `08:52:00Z/08:52:35Z`: `PayOS webhook rejected: invalid signature` từ IP
  ngoài (`14.161.12.103`, `203.171.29.218`) — tức gateway CÓ gửi, nhưng chữ ký của
  payload thanh toán thật không khớp thuật toán hiện tại (trong khi ping
  `confirm-webhook` có chữ ký hợp lệ → sai lệch nằm ở nội dung payload thật:
  trường lồng hoặc bộ key khác).
- Chống tạm: `verify_webhook` giờ log `data_keys`, `nested`, tiền tố 2 chữ ký và
  `orderCode` mỗi lần mismatch — payload thật kế tiếp sẽ tự khai field lệch.
- An toàn tiền: không hạ kiểm tra chữ ký xuống (fail-closed giữ nguyên);
  polling cron là đường cứu hộ đã được chứng minh.

**M6c-3 — Notification của đơn đã trả BỊ MẤT do `UUID` không serialize được:**
- Log `08:45:45Z`: `TypeError: Object of type UUID is not JSON serializable`
  tại `notification_service._insert` (line 103, `json.dumps(payload["data"])`) —
  `order_id` mang kiểu UUID của asyncpg.
- Fix: `_json_safe()` chuẩn hoá 1 lần tại `notify_user` → cả JSONB, REST body và
  WS frame đều an toàn. Verify trên DB thật với payload mang UUID:
  `NOTIFICATION_PERSISTENCE_OK`. Thêm test hồi quy
  `NotificationSerializationTests` trong `test_commerce.py`. Hiện 60/60 test pass.
- Thông báo của đơn 200000005 đã mất theo lỗi cũ (license và tiền thì nguyên) —
  không gửi lại thủ công để giữ lịch sử đúng (thông báo probe đánh dấu đã đọc).
| M7  | Chọn nhà cung cấp VPS (cần hỗ trợ custom user-data cho Windows)                   | —                                 | ⏳ Phase D                    |
| M8  | Dựng **golden image** VPS (MT5 + whitelist domain license server + agent service) | VPS provider                      | ⏳ Phase D                    |


**Ghi chú cho M4:** nếu KHÔNG set `DATABASE_POOL_URL` thì hệ thống vẫn chạy
(PostgREST), nhưng: (a) checkout mất `SELECT ... FOR UPDATE` thật, (b)
notification/advisory-lock rơi vào chế độ degraded (in-process), (c) **không
được chạy nhiều hơn 1 uvicorn worker**.

**M4 đã kiểm chứng (2026-09-17):** Transaction pooler `aws-0-ap-northeast-2.pooler.supabase.com:6543`,
user `postgres.<ref>`, mật khẩu `@` URL-encode `%40`. Direct 5432 KHÔNG dùng được
từ máy dev (`db.<ref>.supabase.co` chỉ có IPv6 → `DNS_FAILED errno 11002`). Pin
đổi `asyncpg==0.30.0` → **`0.31.0`** (bản cũ không có wheel `cp314` cho Python 3.14).
**Fix bắt buộc:** `services/direct_db.py::_get_pool()` truyền `statement_cache_size=0`
— PgBouncer transaction mode không giữ server-side prepared statement.

**M2b/M5/M6 đã kiểm chứng (2026-09-18):**
- M2b: 7 bảng Commerce + index bất biến + `bot_magic_seq` + RLS 8/8 + seed 3 bot.
  13 bảng "thiếu" là Phase B/C/D, đúng thiết kế. **Fix kèm:** `bot_magic_seq`
  trước đó chưa từng có trong file SQL nào → đã bổ sung vào migration mục (9).
- M5: key thật → `code 101` ("Mã thanh toán không tồn tại" = đã xác thực);
  key sai → `code 214` (bị chặn). PayOS trả **HTTP 200** cho cả hai, lỗi nghiệp vụ
  nằm ở field `code` — client xử lý đúng.
- M6: `POST /confirm-webhook` trả `{webhookUrl, accountName: LAM HOANG SON,
  accountNumber: 0899901359, name: BOT_TRADE, shortName: MBBank}`.
- ⚠️ **Bug tích hợp thật đã phát hiện & sửa:** ping xác minh của PayOS **có chữ ký
  hợp lệ** nhưng mang `orderCode` mẫu (1234) không tồn tại → code cũ trả **404
  "Order not found"** → PayOS báo `Webhook url invalid` và **từ chối đăng ký URL**.
  Sửa: order không tồn tại trả **200 "Unknown order acknowledged"** (log warning);
  mất mát thật không xảy ra vì `poll_pending_orders` quét lại mọi order PENDING mỗi
  phút. Đây cũng là bằng chứng checksum key + thuật toán HMAC của ta đúng.
- Tunnel dùng để test: `cloudflared tunnel --url http://127.0.0.1:8000`
  → `https://science-higher-curves-buried.trycloudflare.com` (URL **tạm thời**,
  đổi mỗi lần chạy; production phải trỏ domain thật rồi `confirm_webhook` lại).

---

## 2. TRẠNG THÁI CODEBASE HIỆN TẠI

### 2.0 CẤU TRÚC FOLDER (đã sắp xếp lại 2026-09-21)

Nguyên tắc: **service đi với service, repository đi với repository**. File nào đọc/ghi
dữ liệu (DB hoặc HTTP gateway) nằm ở `repositories/`; file nào chứa logic nghiệp vụ
nằm ở `services/`.

```
repositories/                     # tầng truy cập dữ liệu (DB + HTTP adapter)
  persistence.py                  #   Protocol + InMemoryAccountRepository
  supabase_repository.py          #   PostgREST adapter (accounts, sessions, trades)
  commerce_repository.py          #   cart/order/license + atomic checkout (asyncpg|REST)
  direct_db.py                    #   asyncpg pool (critical path, fail-fast)
  db_locks.py                     #   Postgres advisory lock (thay Redis ở Phase A)
  payos_client.py                 #   PayOS gateway adapter (outbound HTTP)

services/                         # tầng nghiệp vụ
  account_service.py              #   vòng đời bot session theo account
  auth_service.py                 #   login/register/session/Principal
  auth_dependencies.py            #   FastAPI auth deps (require_admin, principal...)
  commerce_service.py             #   checkout 2 pha, webhook, cron, refund
  notification_service.py         #   thông báo in-app + realtime
  execution_pipeline.py           #   serialize lệnh MT5 (Phase 0)
  market_data.py                  #   shared price board
  risk_engine.py                  #   position sizing theo symbol_info live
  async_http.py                   #   pool httpx dùng chung (transport thuần)
```

**Ngoại lệ có chủ đích:** `services/async_http.py` là tiện ích transport thuần không
thuộc nghiệp vụ lẫn dữ liệu, được cả hai tầng dùng — giữ ở `services/` thay vì tạo
thêm package mới. `repositories/payos_client.py` nằm cùng `supabase_repository.py`
vì cả hai đều là adapter ra ngoài (HTTP), nhất quán một kiểu.

**Đã xoá:** mọi script tạm `_*.py` (`_probe_payos.py`, `_reorg_imports.py`,
`_smoke_after_reorg.py`, `_verify_*.py`, `_check_*.py`, `_restart_server.cmd`) sau khi
dùng xong. Test chính thức giữ lại: `test_safety.py`, `test_commerce.py`.


### 2.1 Đã xong (đã verify bằng test)


| Hạng mục                                         | File                                                                       | Ghi chú                                                   |
| ------------------------------------------------ | -------------------------------------------------------------------------- | --------------------------------------------------------- |
| Race fix: `is_pending_order` không kẹt vĩnh viễn | `bot.py`                                                                   | `try/finally`                                             |
| Race fix: double-close                           | `bot.py`                                                                   | `_closing_tickets` guard                                  |
| Serialize mọi lệnh MT5                           | `services/execution_pipeline.py`                                           | Queue → Worker → semaphore → MT5 IPC                      |
| Torn-state fix                                   | `bot.py` (`live_state` snapshot)                                           | 1 reference assignment                                    |
| Shared market data                               | `services/market_data.py`                                                  | board cho N consumers                                     |
| Async HTTP pool                                  | `services/async_http.py`                                                   | pool **keyed theo event loop**                            |
| Auth không block event loop                      | `auth_service.py` (`authenticate_async`), `auth_dependencies.py`           | mọi dependency đã `await`                                 |
| Direct DB critical path                          | `services/direct_db.py`                                                    | asyncpg, **fail-fast, không fallback**                    |
| Risk engine tách riêng                           | `services/risk_engine.py`                                                  | dùng live `symbol_info` metadata                          |
| **Notification (mới)**                           | `services/notification_service.py` + `supabase/notification_migration.sql` | in-app + realtime 2 kênh                                  |
| **Cross-process lock (mới)**                     | `services/db_locks.py`                                                     | **Postgres advisory lock** (quyết định: không dùng Redis) |
| Migration gộp                                    | `supabase/consolidated_pending_migration.sql`                              | index + view + trigger LOCK_HARD                          |


### 2.2 Kiểm thử

```
cd C:\Users\ITPC\orca\workspaces\bot_trade\Plan-2
python -m unittest test_safety        # 44 tests, tất cả OK
python -m py_compile app.py bot.py    # syntax check
```

### 2.3 Legacy (giữ làm reference, KHÔNG xoá)

- `bot.py` trading engine: engine Python sẽ **không còn điều khiển lệnh** khi EA
MQL5 lên sóng. Giữ file để tham chiếu logic, tắt dần khỏi `lifespan` ở Phase C.
- `services/market_data.py`: EA lấy giá trực tiếp từ terminal → backend không cần.
- Admin "gán bot theo account" → chuyển thành **admin override trên license**.

---

## 3. KIẾN TRÚC — 5 LỚP

```
┌─────────────────────────────────────────────────────────────────────────┐
│ LP 1 — CLIENT                                                          │
│  • Web khách: templates/index.html (dashboard, thư viện, giỏ hàng)      │
│  • Web admin: templates/admin_dashboard.html (duyệt, đối soát, công nợ) │
│  • Mobile: Flutter (mobile/) — dashboard, login/register, thư viện      │
│  → KHÔNG BAO GIỜ kết nối trực tiếp VPS/MT5. Chỉ gọi Backend API.        │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │ HTTPS / WSS
┌──────────────────────────────▼──────────────────────────────────────────┐
│ LỚP 2 — BACKEND API (FastAPI, app.py)                                   │
│  • Thương mại: cart, checkout, PayOS webhook, order                     │
│  • Thư viện & License: license, activate, switch request                │
│  • Admin review: hàng đợi switch/refund/công nợ                         │
│  • Provisioning: job queue cho VPS Agent (outbound-only)                │
│  • Billing VPS: chu kỳ, quá hạn, suspend/terminate                      │
│  • License Server: heartbeat từ EA (ACTIVE/PENDING_STOP/INVALID)        │
│  • Reporting/Ingestion: equity snapshot + trade report từ EA            │
│  • Notification: in-app + realtime (2 kênh, 1 row)                      │
│  • Audit & Consent: user_consents, bot_audit_logs, security events      │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────────────┐
│ LỚP 3 — DỮ LIỆU                                                         │
│  • Postgres (Supabase) — bảng nghiệp vụ + RLS                           │
│  • asyncpg direct pool (DATABASE_POOL_URL) — critical path, fail-fast   │
│  • Postgres advisory lock — cross-process lock (KHÔNG dùng Redis)       │
│  • KMS/Vault — Master password + credential hạ tầng (envelope encrypt)  │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────────────┐
│ LỚP 4 — TÍCH HỢP NGOÀI                                                   │
│  • PayOS (payment link + webhook + refund)                              │
│  • VPS Provider API (tạo/suspend/resume/terminate)                      │
│  • Supabase Realtime (fan-out notification cho mobile)                  │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │ outbound only (không mở port inbound)
┌──────────────────────────────▼──────────────────────────────────────────┐
│ LỚP 5 — THỰC THI (VPS khách — công ty đứng tên)                          │
│  • Windows Agent Service (poll job từ backend)                          │
│  • MetaTrader 4/5 — login bằng Master password của khách                │
│  • EA (4 module): LicenseGuard · RiskGuard · ReportingModule ·          │
│                   OrderExecution                                        │
└─────────────────────────────────────────────────────────────────────────┘
```

### 3.1 Nguyên tắc bất di bất dịch

1. **Backend KHÔNG bao giờ gọi trực tiếp MT5.** Mọi thông tin về vị thế/equity
 đi qua **đúng 1 kênh: heartbeat của EA** (~60s), lưu cache vào
 `bot_instance_status`. Không tn tại hàm kiểu `mt5_get_open_positions()`.
2. **Mọi kết nối từ VPS đều là outbound.** Không RDP cho khách, không mở port.
3. **Mọi guard tách theo `magic_number`.** Khách trade tay trên cùng tài khoản
 không được làm bot dừng oan.
4. **Mọi thứ nhạy cảm chỉ tồn tại 1 lần, dưới dạng hash.** Master password →
 encrypted; license key → SHA-256 hash trong DB.

---

## 4. MÔ HÌNH KINH DOANH — QUYẾT ĐỊNH ĐÃ CHỐT


| Chủ đề                      | Quyết định                                                                                                                            |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| Custody tiền giao dịch      | **Non-custodial** — công ty không giữ tiền của khách                                                                                  |
| VPS                         | Bên thứ 3, **công ty đứng tên API**, khách không có quyền hạ tầng                                                                     |
| RDP                         | **KHÔNG cấp cho khách** — bảo vệ bản quyền `.ex4/.ex5`                                                                                |
| Bot                         | Code trong **MetaEditor (MQL4/MQL5)**, không dùng Python để giao dịch                                                                 |
| Mua bot                     | ≠ Kích hoạt. Mua xong → `OWNED_INACTIVE` trong **Thư viện**                                                                           |
| Số bot chạy đồng thời       | **1 khách = 1 bot ACTIVE** tại 1 thời điểm                                                                                            |
| Kích hoạt bot khác          | Huỷ vĩnh viễn bot cũ (`PERMANENTLY_STOPPED`) — muốn dùng lại phải **mua lại**                                                         |
| Switch bot                  | **Không phát sinh thanh toán** (bot đích đã mua trước). Cần **admin duyệt**, hiệu lực **06:00 sáng hôm sau (giờ VN, tz-aware)**       |
| Admin từ chối switch        | **Không cần hoàn tiền** (bot đích vẫn nguyên trong thư viện)                                                                          |
| Hoàn tiền                   | PayOS refund tự động, **chỉ hoàn TOÀN BỘ order** (không hoàn 1 phần trong combo), trong **7 ngày** và license **chưa từng kích hoạt** |
| Amount mismatch             | Lệch **≤ 1% tự động chấp nhận + log**; vượt ngưỡng → `manual_review`                                                                  |
| Phí                         | Bán license theo gói + phí VPS định kỳ. **Không** chia sẻ lợi nhuận                                                                   |
| Suspend VPS khi còn lệnh mở | **Không chặn cng** — dựa SL server-side + cảnh báo trước 24h                                                                          |
| Notification                | Bảng `notifications` in-app **+ realtime** (WS cho web, Supabase Realtime cho mobile)                                                 |


### 4.1 Chính sách hiệu suất (chỉ để hiển thị, không dùng để thu phí)

- **Daily Dietz chaining:** `R_ngày = (EV − BV − CF) / (BV + CF × w)`, `w` = phần
thời gian còn lại của ngày kể từ lúc cashflow.
- Chốt sub-period **ngay khi có cashflow**, không đợi hết ngày.
- Hiển thị dùng **Equity**; lưu **High-Water Mark** để không hiển thị "lãi ảo".
- Tách theo `magic_number`.
- ️ **Equity snapshot là của CẢ tài khoản** → trộn P&amp;L lệnh tay của khách. Muốn
tách sạch cần cột `bot_equity` — **quyết định để Phase E**.

---

## 5. DATA MODEL

### 5.1 Bảng ĐÃ CÓ (đang chạy — KHÔNG tạo lại)


| Bảng                           | Nguồn                                                                      | Ghi chú                                                |
| ------------------------------ | -------------------------------------------------------------------------- | ------------------------------------------------------ |
| `auth.users`                   | Supabase                                                                   | **nguồn user duy nhất** — KHÔNG tạo bảng `users` mới   |
| `user_profiles`                | `schema.sql` + `dashboard_migration.sql` + `admin_dashboard_migration.sql` | `roles jsonb`, `phone`, `timezone`                     |
| `bot_types`                    | `bot_types_migration.sql`                                                  | safe/medium/risky + `min_capital` + `trade_frequency`  |
| `trading_accounts`             | `schema.sql`                                                               | `lock_state`, `bot_type_id`, `unlocked_by`             |
| `user_sessions`                | `schema.sql`                                                               | `token_hash` (không lưu raw token)                     |
| `trade_orders`                 | `schema.sql`                                                               | **legacy** (engine Python cũ) — giữ làm archive        |
| `account_equity_snapshots`     | `dashboard_migration.sql`                                                  | ⚠️ **dùng bảng này**, KHÔNG tạo `equity_snapshots` mới |
| `admin_account_summary` (view) | `consolidated_pending_migration.sql`                                       | superset, có cả `id`/`account_id`                      |
| `notifications`                | `notification_migration.sql` **(mới)**                                     | in-app + realtime                                      |


### 5.2 Bảng SẼ TẠO (Phase A–E, chưa tồn tại)

```sql
-- ---------- CATALOG ----------
bots (
    id uuid PK, name, description,
    tier_id uuid FK -> bot_types(id),        -- FK, KHÔNG enum inline (giữ min_capital)
    price numeric, is_active boolean, max_owned_per_user int default 1
)
bot_builds (                                  -- deploy đúng version EA, nền cho rollout batch
    id uuid PK, bot_id FK, file_version varchar, sha256 varchar,
    storage_path text, is_current boolean, created_at
)

-- ---------- TÀI KHOẢN MT5 & VPS ----------
mt5_accounts (
    id uuid PK, user_id uuid FK auth.users(id),
    broker_name, broker_server, account_login bigint,
    master_password_encrypted text,           -- envelope encryption qua KMS
    kms_key_id varchar, created_at,
    UNIQUE (account_login, broker_server)
)
vps_instances (
    id uuid PK, user_id FK auth.users(id),
    provider varchar, provider_instance_id varchar, provider_region varchar,
    status text check (status in ('PROVISIONING','RUNNING','SUSPENDED','TERMINATED')),
    created_at, suspended_at, terminated_at
)
vps_subscriptions (
    id uuid PK, user_id FK, vps_instance_id FK,
    monthly_fee numeric, billing_cycle text default 'MONTHLY',
    status text check (status in ('ACTIVE','OVERDUE','SUSPENDED','CANCELLED')),
    next_charge_date date, grace_period_ends_at timestamptz
)
vps_billing_charges (                         -- lịch sử hoá đơn để đối soát
    id uuid PK, vps_subscription_id FK,
    period_start date, period_end date, amount numeric,
    status text check (status in ('PENDING','PAID','FAILED')),
    paid_at, payos_payment_link_id
)
vps_jobs (                                    -- hàng đợi cho VPS Agent (outbound-only)
    id uuid PK, vps_instance_id FK,
    job_type text,                            -- DEPLOY_BOT | KILL_BOT | VERIFY_LOGIN | START_TERMINAL
    payload jsonb,                            -- ⚠️ KHÔNG chứa secret (không password, không license_key)
    status text check (status in ('PENDING','IN_PROGRESS','DONE','FAILED')),
    created_at, picked_up_at, completed_at, result jsonb, error_message
)
```

### 5.3 License &amp; instance

```sql
user_bot_licenses (
    id uuid PK, user_id FK, bot_id FK, order_id FK,
    status text check (status in ('OWNED_INACTIVE','ACTIVE','PERMANENTLY_STOPPED','REFUNDED')),
    purchased_at, activated_at, stopped_at,
    stop_reason text check (stop_reason in
        ('SWITCHED_TO_NEW_BOT','ADMIN_ACTION','VPS_TERMINATED_NONPAYMENT')) -- nullable
)
bot_instances (
    id uuid PK, user_bot_license_id FK, mt5_account_id FK, vps_instance_id FK,
    magic_number bigint UNIQUE,               -- sinh từ SEQUENCE bot_magic_seq
    license_key_hash varchar NOT NULL,        -- SHA-256, KHÔNG plaintext
    license_key_created_at timestamptz,
    pending_stop boolean default false,       -- cờ riêng (KHÔNG nhét vào license status)
    terminal_install_dir text,
    started_at, stopped_at,
    provisioning_status text check (provisioning_status in
        ('PENDING','PROVISIONING','RUNNING','STOPPED','FAILED'))
)
bot_instance_status (                          -- cache do EA tự báo qua heartbeat
    bot_instance_id uuid PK FK,
    last_heartbeat_at timestamptz, last_equity numeric, last_balance numeric,
    last_open_positions_count int,
    license_status_reported varchar,           -- ACTIVE | PENDING_STOP | INVALID (để debug)
    consecutive_mismatch_count int default 0   -- đếm để auto-revoke
)
```

### 5.4 Sequence

```sql
create sequence if not exists bot_magic_seq start 100000;
```

> **Vì sao tách file kiểu này:** các bảng trong 5.3 quyết định semantics của
> License Server; đọc kèm mục 7.2 để hiểu vòng đời `license_key_hash` và
> `pending_stop`.

### 5.5 Thương mại

```sql
carts (id uuid PK, user_id FK, status text check (status in ('ACTIVE','LOCKED')), updated_at)
-- Ràng buộc ở DB: UNIQUE (user_id) WHERE status = 'ACTIVE'  (1 giỏ active / user)
cart_items (id uuid PK, cart_id FK, bot_id FK, qty int default 1, added_at)

orders (
    id uuid PK, order_code bigint UNIQUE,      -- ⚠️ PayOS giới hạn ~9 chữ số
    user_id FK, cart_id FK,
    total_amount numeric, received_amount numeric,
    idempotency_key varchar,                   -- UNIQUE (user_id, idempotency_key)  [không unique toàn cục]
    status text check (status in
        ('PENDING','EXPIRED','PAID','REFUND_PENDING','REFUNDED','COMPLETED')),
    late_payment boolean default false,        -- webhook đến sau khi đã EXPIRED
    payos_payment_link_id, payos_payment_link_url, refund_provider_txn_id,
    created_at, expires_at, paid_at
)
order_items (id uuid PK, order_id FK, bot_id FK, qty,
             unit_price_snapshot numeric,   -- GIÁ CHỐT lúc checkout, không đổi dù giá bot sau này thay đổi
             bot_name_snapshot text)
```

> **Vì sao `orders.cart_id` mà không phải `carts.last_order_id`:** mở khoá giỏ
> trực tiếp từ order, tránh cột ngược gây mất đồng bộ.

### 5.6 Switch bot

```sql
bot_switch_requests (
    id uuid PK, user_id FK, current_license_id FK, requested_license_id FK,
    status text check (status in ('PENDING_ADMIN_REVIEW','APPROVED','REJECTED',
                                  'EXECUTED','DEFERRED','FAILED_NEEDS_MANUAL')),
    has_open_positions_at_request boolean,     -- đọc từ bot_instance_status, KHÔNG gọi MT5
    scheduled_execute_date date,               -- 06:00 VN ngày kế tiếp
    reviewed_by, reviewed_at, review_note, executed_at
)
```

> **KHÔNG có `order_id`** — switch không phát sinh giao dịch tiền.

### 5.7 Audit, Consent, Hiệu suất

```sql
user_consents (id uuid PK, user_id FK, consent_type, terms_version,
               terms_content_hash, ip_address, user_agent, checkbox_states jsonb, created_at)
bot_audit_logs (id uuid PK, user_id FK, bot_instance_id FK, action,
                old_params jsonb, new_params jsonb, ip_address, created_at)

cashflow_events (id, mt5_account_id FK, type check (type in ('DEPOSIT','WITHDRAW')),
                 amount, occurred_at)
daily_dietz_periods (id, mt5_account_id FK, period_date, sub_period_return,
                     closed_reason check (closed_reason in ('DAILY','CASHFLOW_TRIGGERED')))
high_water_marks (mt5_account_id PK, hwm_equity_adjusted, updated_at)

trade_history (
    id uuid PK, mt5_account_id FK, magic_number bigint,
    ticket bigint, position_id bigint, symbol,
    side text,                                 -- 'BUY' | 'SELL'
    volume numeric, open_price numeric, opened_at timestamptz,
    close_price numeric, closed_at timestamptz,
    profit numeric, swap numeric, commission numeric,
    UNIQUE (mt5_account_id, ticket)            -- ⚠️ BẮT BUỘC: EA resend không double-insert
)
-- index bắt buộc:
create index if not exists idx_trade_history_account_time
    on public.trade_history (mt5_account_id, closed_at desc);
```

### 5.8 Quy tắc RLS (áp cho MỌI bảng nghiệp vụ mới)

```sql
alter table <t> enable row level security;
create policy "<t> owner select" on <t> for select using (auth.uid() = user_id);
```

**Ngoại lệ bắt buộc:**

- `mt5_accounts.master_password_encrypted` — **KHÔNG** expose qua policy thường;
tách view riêng hoặc bảng phụ chỉ service-role đọc được.
- Backend dùng **service-role key** → bypass RLS (chỉ đúng 1 writer).

---

## 6. LUNG NGHIỆP VỤ

### 6.1 Add to Cart → Checkout → PayOS

```
[Khách] add_to_cart(bot_id)
   ├─ cart.status != ACTIVE            → 409 "giỏ đang xử lý"
   ├─ bot.is_active == false           → 409 "ngừng bán"
   └─ count_usable_licenses(user, bot) >= max_owned_per_user  → 409 "đã sở hữu, chưa dùng hết"
        (count_usable = OWNED_INACTIVE + ACTIVE; KHÔNG đếm REFUNDED / PERMANENTLY_STOPPED)

[Khách] checkout(cart_id, idempotency_key)
   async with try_advisory_lock(f"checkout:{cart_id}"):        # Postgres advisory lock
     │
     ├─ 1. Idempotency: get_order_by_idempotency_key(user_id, key)
     │     • existing PENDING + KHÔNG có link  → TÁI TẠO LINK (ngoài tx) rồi trả
     │     • existing PENDING + đã có link    → trả link cũ
     │     • existing FAILED                  → cho tạo order mới
     │     • existing khác                    → trả nguyên
     │
     ├─ 2. TX1 (CHỈ DB — TUYỆT ĐỐI KHÔNG gọi mạng ngoài)
     │     • pending order cùng cart còn hạn  → return pending (trả link cũ)
     │     • pending order đã hết hạn         → expire_order + MỞ LẠI CART ngay
     │     • SELECT ... FOR UPDATE trên cart
     │     • re-validate TẠI ĐÂY: bot.is_active + count_usable_licenses
     │     • snapshot giá vào order_items
     │     • order_code = nextval 9 chữ số (retry nếu trùng)
     │     • cart.status = 'LOCKED'
     │
     ├─ 3. PayOS create_payment_link()   ← NGOÀI transaction (timeout 5-10s)
     │     └─ lỗi → TX2a cancel_order (mở lại cart) → 502
     │
     └─ 4. TX2b attach_payment_link()    ← nhanh, chỉ UPDATE

[PayOS] POST /api/payos/webhook
   ├─ verify checksum (field `signature` trong BODY — xác nhận lại tên field với docs PayOS)
   ├─ lock: payos:webhook:{order_code}
   ├─ event_code != "00" → log non-success event, return 200
   ├─ amount mismatch:
   │     • |total − received| / total <= 1%  → chấp nhận + log
   │     • vượt ngưỡng                       → flag manual_review, return 200
   ├─ atomic: UPDATE ... SET status='PAID' WHERE order_code=? AND status IN ('PENDING','EXPIRED')
   │     ─ 0 row → "Already processed" (chống double-credit)
   ├─ late_payment = (order.status == 'EXPIRED')
   ├─ TẠO license cho từng order_item (status = OWNED_INACTIVE)
   │     ⚠️ KHÔNG re-validate is_active ở đây — đã thu tiền thì phải giao hàng
   ├─ order.status = 'COMPLETED'; cart.status = 'ACTIVE'
   └─ notify_user(TYPE_ORDER_PAID, "Mua bot thành công! Xem Thư viện Bot")
```

**Bất biến quan trọng:**


| #   | Bất biến                                              | Lý do                                            |
| --- | ----------------------------------------------------- | ------------------------------------------------ |
| B1  | PayOS **luôn ngoài transaction**                      | Giữ lock/pool connection 5-10s → pool exhaustion |
| B2  | Webhook chấp nhận **cả PENDING và EXPIRED**           | Khách chuyển trễ vẫn phải được giao hàng         |
| B3  | Giá snapshot lúc **checkout**, không lúc add-to-cart  | Giá đổi giữa 2 bước                              |
| B4  | `idempotency_key` unique theo **(user_id, key)**      | Chống đọc chéo order của user khác               |
| B5  | Mua = xong (`COMPLETED`), **không** cần bước nào thêm | Mua ≠ kích hoạt                                  |


### 6.2 Thư viện → Kích hoạt → Switch

```
[Thư viện] hiển thị theo license.status
   OWNED_INACTIVE      → nút "Kích hoạt"
   ACTIVE              → nhãn "Đang chạy"
   PERMANENTLY_STOPPED → nút "Mua lại" (quay về 6.1)
   REFUNDED            → không hiển thị nút

activate_from_library(user, license_id)
   ├─ license.user_id != user OR status != OWNED_INACTIVE → 403
   ├─ Có switch request đang mở (PENDING/APPROVED/DEFERRED) → 409
   └─ current_active = get_active_license(user)
        │  (loại trừ license có instance pending_stop = true — giữ bất biến "1 ACTIVE")
        ├─ NULL → NHÁNH 1: kích hoạt NGAY (không cần admin)
        │     ├─ chưa có mt5_account → yêu cầu nhập login/master password/broker + 2 checkbox
        │     ├─ đã có VPS + mt5_account từ lần trước → DÙNG LẠI (không thu thêm phí VPS)
        │     └─ provisioning (xem 6.3) → license.status = ACTIVE
        └─ KHÁC → NHÁNH 2: create_switch_request (cần admin duyệt)
              ⚠️ KHÔNG tạo Order — bot đích đã mua từ trước

Admin duyệt switch:
   status = APPROVED, scheduled_execute_date = 06:00 VN ngày kế tiếp
   set bot_instance.pending_stop = true      ← chặn lệnh MỚI ngay, vẫn quản lý lệnh cũ
   notify_user(TYPE_BOT_SWITCH_APPROVED)

Admin từ chối:
   Nếu request đang ở APPROVED/DEFERRED → CLEAR pending_stop = false   ← bắt buộc
   status = REJECTED, không cần hoàn tiền (bot đích vẫn nguyên trong thư viện)

Cron 06:00 VN (tz-aware Asia/Ho_Chi_Minh):
   • heartbeat phải MỚI (< 2 phút) VÀ open_positions_count == 0
        → không đạt: DEFERRED (dời 1 ngày) + notify admin
   • KILL_BOT job → agent stop terminal
   • DEPLOY_BOT job cho license mới (magic MỚI)
   • deploy lỗi → REDEPLOY BOT CŨ (upsert cùng instance+magic) → DEFERRED + alert
        → cả 2 đều lỗi: FAILED_NEEDS_MANUAL + alert critical
   • CHỈ KHI bot mới xác nhận heartbeat ACTIVE:
        current_license.status   = PERMANENTLY_STOPPED (SWITCHED_TO_NEW_BOT)
        requested_license.status = ACTIVE
        request.status = EXECUTED
```

**Bất biến:**


| #   | Bất biến                                                                                  | Lý do                                                                     |
| --- | ----------------------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| B6  | Chỉ mark `PERMANENTLY_STOPPED` **sau khi** bot mới chạy                                   | Tránh khách mất cả 2 bot                                                  |
| B7  | `magic_number` được TÁI DÙNG cho **cùng license** khi redeploy                            | Giữ liên tục lịch sử P&amp;L. Chỉ CẤM tái dùng cho license khác           |
| B8  | `deploy_bot_to_vps` phải **upsert/reactivate** instance theo `(license_id, magic_number)` | Nếu insert mù sẽ đụng `UNIQUE(magic_number)` → chính path rollback tự sập |
| B9  | Reject switch ở APPROVED/DEFERRED phải **clear `pending_stop`**                           | Không thì bot cũ bị khoá âm thầm mãi                                      |


### 6.3 Provisioning (VPS Agent — outbound only)

```
Backend tạo vps_jobs (payload KHÔNG chứa secret)
        │
        ▼
[VPS] Windows Agent Service (poll mỗi 5s)
    GET  /api/vps-agent/jobs/next?vps_instance_id=...   (Bearer AgentToken)
    POST /api/vps-agent/jobs/{id}/ack
    ── riêng DEPLOY_BOT / VERIFY_LOGIN ───
    POST /api/vps-agent/jobs/{id}/secrets
         • CHỈ khi job.status == 'IN_PROGRESS'  (410 nếu không)
         • CHỈ trong 15 phút kể từ picked_up_at (410 nếu quá)
         • MỖI LẦN LẤY = 1 DÒNG AUDIT (không log response)
         • trả: login + password + server + license_key
    $env:MT5_PASSWORD = ...        ← ENV VAR, KHÔNG command-line (args lộ qua WMI/tasklist)
    deploy_bot.ps1 ...             ← script tự xoá start_config.ini sau khi terminal chạy
    Remove-Item Env:\MT5_PASSWORD
    POST /api/vps-agent/jobs/{id}/complete  {status, output}

Backend xác nhận 2 TẦNG (job DONE ≠ EA đã chạy):
    1. wait_for_job_completion(job_id, timeout=120)
    2. wait_for_first_heartbeat_confirmed_active(bot_instance_id, timeout=180)
          • chờ đến khi bot_instance_status.license_status_reported == 'ACTIVE'
          • đây là mục đích duy nhất; KHÔNG "tối ưu" thành reject-ngay-nếu-chưa-ACTIVE
            (vài beat đầu có thể INVALID do terminal chưa kết nối broker xong)
    3. chỉ khi (2) đúng → provisioning_status = 'RUNNING'
```

**Activation lạc quan (kích hoạt trước, rollback nếu fail):**

```
license.status = 'ACTIVE'   ← set TRƯỚC khi deploy (EA cần thấy ACTIVE ngay beat đầu)
try:  deploy (upsert instance, jobs, 2-tầng verify)
except: license.status = previous_status   ← rollback
```

**Bảo vệ bản quyền `.ex4/.ex5`:** file gốc đặt NGOÀI `MQL5\Experts`, script deploy
copy vào đúng lúc; compile khoá theo account number nếu công cụ hỗ trợ; VPS chỉ
có 1 tài khoản Windows duy nhất của công ty; audit log mọi lần agent truy cập.

**Cấu hình `deploy_bot.ps1` (đã vá 5 lỗi):**


| Param                  | Sửa gì                                                                                                                                |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `-TerminalInstallDir`  | thống nhất 1 gốc (bỏ mâu thuẫn `/portable` vs data folder)                                                                            |
| `-MagicNumber`         | **bắt buộc** — thiếu thì `RiskGuard` nhận 0 → lẫn lịch sử P&amp;L toàn hệ thống                                                       |
| `.set`                 | ghi cả `LicenseKey` + `InpMagicNumber`, đặt ở `MQL5\Presets`, `ExpertParameters` = đường dẫn tuyệt đối (**verify trên golden image**) |
| `AllowDllImport`       | `0` (EA chỉ dùng `WebRequest`, không cần DLL)                                                                                         |
| xoá `start_config.ini` | sau khi terminal chạy → password không tồn tại lâu dài trên disk                                                                      |


### 6.4 Billing VPS

```
Cron enforce_vps_billing (hằng ngày):
  1. sub.next_charge_date <= today
       → trigger_recurring_charge (PayOS KHÔNG có recurring native → tạo link mới + notify)
       • thành công → next_charge_date += 1 tháng, status = ACTIVE, ghi vps_billing_charges PAID
       • thất bại  → status = OVERDUE, grace_period_ends_at = now() + 3 ngày
  2. OVERDUE & còn <= 24h tới grace end  → notify_user "VPS sẽ tạm ngưng sau 24h"
  3. OVERDUE & grace_period_ends_at <= now
       → provider.suspend() → vps_instance.status = 'SUSPENDED'
       ⚠️ KHÔNG chặn cng khi còn lệnh mở (dựa SL server-side)
  4. SUSPENDED > 30 ngày → provider.terminate()
       → vps_instance.status = 'TERMINATED'
       → mọi license trên VPS đó: PERMANENTLY_STOPPED (VPS_TERMINATED_NONPAYMENT)

Resume sau khi khách thanh toán:
  provider.resume() → vps_instance.status = 'RUNNING'
  → job START_TERMINAL (agent tự start terminal64.exe với profile đã lưu)
```

> ⚠️ `**vps_subscriptions` phải được tạo ở bước provisioning lần đầu** (kèm
> `next_charge_date = now() + 30 ngày`). Nếu thiếu, cron không có gì để quét →
> không ai thu phí VPS. Đây là lỗi M4 trong review pass 6.

### 6.5 License Server (heartbeat từ EA)

```
[EA] LicenseGuard_OnTimer (mỗi 60s)
    POST /api/heartbeat
    body: { license_key, account, magic_number, equity,
            open_positions_count, terminal_time }      ← terminal_time = TimeGMT() epoch

[Backend] @rate_limit_by_ip("60/minute")
    1. cache lookup license_key_hash (TTL 5s)   ← giảm tải khi bị spam
    2. không tìm thấy → sample-log 1/10 + trả INVALID (KHÔNG log mỗi request → chống log-flood)
    3. LỚP 1: account_login phải khớp mt5_accounts.account_login → lệch: INVALID + log security
    4. LỚP 2: magic_number phải khớp bot_instances.magic_number   → lệch: INVALID + alert provisioning
    5. LỚP 3: consecutive_mismatch_count >= MAX → tự động revoke + alert critical
    6. cập nhật bot_instance_status (equity, open_positions_count, last_heartbeat_at)
    7. trả signed_response:
         provisioning_status == 'FAILED' → INVALID          ← revoke phải ngừng hẳn, KHÔNG PENDING_STOP
         pending_stop == true            → PENDING_STOP
         còn lại                         → ACTIVE

signed_response(status): { status, ts, sig }  với sig = HMAC-SHA256(SHARED_SECRET, f"{status}|{ts}")
```

**EA phía MQL5 — 4 module:**


| Module            | Nhiệm vụ                                                                   | Điểm cần nhớ                                                                                                                                                                                                                                                                                     |
| ----------------- | -------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `LicenseGuard`    | heartbeat + verify chữ ký phản hồi, `IsNewOrderAllowed()`                  | `SHARED_SECRET` là **hằng số compile-time** (`#define`), KHÔNG `input` (không lọt vào `.set`). Replay window 300s. **Bắt buộc kiểm tra `res` trong 200–299**, không chỉ `res == -1`                                                                                                              |
| `RiskGuard`       | chặn lệnh vượt ngưỡng theo tier + daily drawdown **tính riêng theo magic** | `g_realizedTodayCache` seed 1 lần trong `Init()` bằng `HistorySelect` (restart-proof), sau đó incremental qua `OnTradeTransaction`. `OnTick` chỉ cộng floating (KHÔNG quét History mỗi tick). Phải gọi `HistoryDealSelect` trước khi đọc property. Đếm cả `DEAL_ENTRY_OUT` và `DEAL_ENTRY_INOUT` |
| `ReportingModule` | đẩy equity snapshot + trade report                                         | **KHÔNG gọi mạng trong `OnTradeTransaction`** — chỉ ghi vào queue nội bộ, gửi theo lô trong `OnTimer`. Snapshot thêm `realized_pnl_today_all` (KHÔNG lọc magic) cho cashflow detection                                                                                                           |
| `OrderExecution`  | logic chiến lược                                                           | **BẮT BUỘC set `request.magic = InpMagicNumber`** — nếu không, toàn bộ tách bạch theo magic ở các module kia vô nghĩa                                                                                                                                                                            |


**Timezone (quan trọng):**

```mql5
long NowUtcEpoch()                        { return (long)TimeGMT(); }            // mốc "hiện tại" gửi lên
long BrokerTimeToUtcEpoch(datetime t)     { return (long)t - ((long)TimeTradeServer() - (long)TimeGMT()); }
// Nhật ký nội bộ EA (ranh giới ngày RiskGuard / Dietz) VẪN dùng TimeTradeServer()
// — "1 ngày giao dịch" phải theo lịch broker, không phải nửa đêm UTC.
```

### 6.6 Reporting &amp; Hiệu suất

```
[EA] SendAccountSnapshot (mỗi 5 phút) → POST /api/accounts/snapshot
     { account_login, equity, balance, realized_pnl_today_all, snapshot_at }
     → account_equity_snapshots (dùng bảng đã có)

[EA] FlushQueue (trong OnTimer) → POST /api/trades/report
     { ticket, position_id, magic_number, symbol, side,
       volume, open_price, opened_at, close_price, closed_at,
       profit, swap, commission }
     → trade_history  ⚠️ UPSERT theo UNIQUE(mt5_account_id, ticket)

Cashflow detection (Phase E):
     cashflow_detected = balance_delta − realized_pnl_today_all
       • realized_pnl_today_all = tổng P&L THẬT của CẢ tài khoản (không lọc magic)
         → vì lệnh tay của khách cũng làm balance đổi mà không có trong trade_history của bot
       • chênh lệch != 0 → ghi cashflow_events (DEPOSIT/WITHDRAW)
       • có cashflow → CHỐT sub-period Dietz ngay

Cron daily_equity_snapshot_rollup (hằng ngày):
     chốt sub-period Dietz từ dữ liệu đã báo → daily_dietz_periods
     cập nhật high_water_marks
```

### 6.7 Notifications (in-app + realtime) — ĐÃ TRIỂN KHAI

**Nguyên tắc thiết kế:** **1 row → 2 kênh độc lập.**

```
notify_user(user_id, type, title, message, data, severity)
        │
        ├─► KÊNH 1 — PERSISTENT (nguồn sự thật)
        │     INSERT INTO public.notifications (...)
        │     → offline vẫn xem được đầy đủ lịch sử khi quay lại
        │
        └─► KÊNH 2 — REALTIME (best effort)
              ├─ Web dashboard: push vào asyncio.Queue của từng socket
              │    → ws loop drain → gắn vào payload state → send_json
              └─ Mobile Flutter: Supabase Realtime postgres_changes
                   subscribe('notifications', filter: user_id=eq.<uid>)
                   → KHÔNG tốn công backend, và chạy độc lập khi backend restart
```

**File &amp; API:**


| Thành phần                        | File                                                                                                 | Ghi chú                                                                                        |
| --------------------------------- | ---------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| Bảng + RLS + Realtime publication | `supabase/notification_migration.sql`                                                                | `notifications`, 2 index, 2 policy, DO block add vào `supabase_realtime`                       |
| Service                           | `services/notification_service.py`                                                                   | `notify_user`, `notify_all_admins`, `list_notifications`, `mark_read`, `register_push_handler` |
| Hub realtime (web)                | `app.py` — `notification_clients`, `_push_notification`, `_register/_unregister_notification_client` | registry `user_id → set[asyncio.Queue]`                                                        |
| REST API                          | `app.py`                                                                                             | `GET /api/notifications`, `POST /api/notifications/read`                                       |
| WS integration                    | `app.py` `websocket_endpoint`                                                                        | queue drain mỗi 0.5s, bỏ key `notifications` nếu rỗng                                          |


**Quyết định kỹ thuật quan trọng:**

1. **Vì sao dùng `asyncio.Queue` per-connection thay vì gửi trực tiếp từ push
 handler?** Starlette chỉ cho **1 writer mỗi WebSocket**; WS loop đã gửi state
 mỗi 0.5s. Gửi từ coroutine khác → interleave frame. Queue giữ đúng 1 writer,
 và queue có `maxsize=50` nên client chậm tự drop (không block ai).
2. **Vì sao cần cả Supabase Realtime nếu đã có WS?** Web dashboard dùng WS
 (cùng origin, đã có sẵn). Mobile KHÔNG nên mở WS tới backend (tốn pin, phải
 reconnect, và WS là per-process → không scale ngang). Supabase Realtime cho
 mobile subscribe trực tiếp vào Postgres → backend không phải fan-out N clients.
3. **Notification KHÔNG được làm hỏng business flow.** `notify_user` bắt mọi
 exception khi persist và **trả `None`** (không raise) — một toast lỗi không
 được rollback một order đã thanh toán.
4. **Write path 2 chế độ:** có `DATABASE_POOL_URL` → asyncpg (nhanh); không có →
 Supabase REST qua `async_http` pool. `direct_db.get_direct_pool()` trả `None`
 khi chưa cấu hình (khác `_get_pool` fail-fast vì đây là optional feature).

**Types đã định nghĩa (`services/notification_service.py`):**

```
Khách: ORDER_PAID · BOT_ACTIVATED · BOT_SWITCH_REQUESTED · BOT_SWITCH_APPROVED
       BOT_SWITCH_REJECTED · BOT_SWITCH_DEFERRED · BOT_SWITCH_EXECUTED
       REFUND_REQUESTED · REFUND_COMPLETED · VPS_PAYMENT_FAILED · VPS_SUSPENDED
       VPS_TERMINATED · PROVISIONING_FAILED
Admin: ADMIN_NEW_SWITCH_REQUEST · ADMIN_REFUND_REQUEST
       ADMIN_PROVISIONING_ALERT · ADMIN_SECURITY_ALERT
```

> Coi danh sách type là **public API**: thêm thì được, **đổi tên thì không** (app
> mobile map type → icon/deep link).

**Payload mobile nhận qua Realtime:**

```json
{ "id": "<uuid>", "user_id": "<uuid>", "type": "ORDER_PAID", "title": "...",
  "message": "...", "data": { "order_id": "...", "count": 1 },
  "severity": "success", "read_at": null, "created_at": "..." }
```

**Việc còn lại cho Notification (không chặn gì):**

- [ ] Flutter: màn hình inbox + badge unread (`get unread count`), subscribe Realtime
- [ ] Web dashboard: hiển thị toast khi `state.notifications` xuất hiện
- [ ] Gọi `notify_*` tại các điểm nghiệp vụ (Phase A/B: webhook paid, switch

  approve/reject/execute, refund, billing suspend) — **service đã sẵn sàng**
- [ ] (tuỳ chọn) Email cho sự kiện quan trọng — **cần user chốt nhà cung cấp SMTP**

---

## 7. QUYẾT ĐỊNH KỸ THUẬT ĐÃ CHỐT (không bàn lại)


| #   | Quyết định                                                                                                                           | Lý do / ghi chú                                                                                                                                                                                     |
| --- | ------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| D1  | **Cross-process lock = Postgres advisory lock**, KHÔNG dùng Redis (Phase A)                                                          | Redis chưa có trong môi trường; asyncpg đã có. `pg_try_advisory_xact_lock` tự release khi crash → an toàn hơn bảng lock tự chế. Chuyển sang Redis sau này = sửa **1 file** (`services/db_locks.py`) |
| D2  | **KHÔNG fallback** asyncpg → PostgREST cho critical path                                                                             | Timeout có thể đã ghi thành công → fallback ghi lần 2 = duplicate. Fail-fast để caller retry                                                                                                        |
| D3  | **Heartbeat response phải ký HMAC-SHA256** + timestamp chống replay                                                                  | Không ký thì fake DNS/MITM trả `{"status":"ACTIVE"}` = mở khoá cả fleet                                                                                                                             |
| D4  | `**SHARED_SECRET` là hằng số compile-time**, KHÔNG `input`                                                                           | `input` sẽ lọt vào `.set` → secret nằm trên disk VPS. Đổi secret = rebuild + rollout batch (tần suất rất thấp, chấp nhận)                                                                           |
| D5  | `**license_key` chỉ lưu SHA-256 hash** trong DB                                                                                      | DB leak không lộ key dùng được; giống mô hình master password                                                                                                                                       |
| D6  | **Secrets không bao giờ nằm trong `vps_jobs.payload`**                                                                               | Lấy qua `POST /jobs/{id}/secrets` (chỉ khi `IN_PROGRESS`, TTL 15 phút, audit mọi lần); truyền vào script qua **ENV VAR** (không command-line — args lộ qua WMI/tasklist)                            |
| D7  | **PayOS luôn ngoài transaction** (two-phase checkout)                                                                                | Giữ connection + row lock 5-10s → pool exhaustion                                                                                                                                                   |
| D8  | **Không có RDP cho khách**                                                                                                           | Bảo vệ bản quyền `.ex4/.ex5`; ranh giới trách nhiệm rõ                                                                                                                                              |
| D9  | **VPS Agent polling** (outbound-only), không WinRM/SSH inbound                                                                       | Không mở port; mọi thao tác đi qua job queue có log → phục vụ audit cho khách xem                                                                                                                   |
| D10 | `**TimeGMT()` cho mốc "hiện tại"**, `TimeTradeServer()` cho nhật ký nội bộ EA                                                        | Tránh lệch múi giờ broker khi lưu `timestamptz`                                                                                                                                                     |
| D11 | `**deploy_bot_to_vps` upsert/reactivate** instance theo `(license_id, magic_number)`                                                 | Chống `UNIQUE(magic_number)` violation phá chính path rollback                                                                                                                                      |
| D12 | **Activation lạc quan**: set license ACTIVE trước, rollback nếu fail                                                                 | EA cần thấy ACTIVE ngay heartbeat đầu                                                                                                                                                               |
| D13 | **Chỉ tick `PERMANENTLY_STOPPED` sau khi bot mới xác nhận ACTIVE**                                                                   | Tránh khách mất cả 2 bot                                                                                                                                                                            |
| D14 | **Notification: 1 row → 2 kênh** (persist + realtime), realtime qua **queue per-socket** cho web và **Supabase Realtime** cho mobile | Xem 6.7                                                                                                                                                                                             |
| D15 | **Equity tách P&amp;L lệnh tay của khách → Phase E**                                                                                 | Cần cột `bot_equity`; không chặn phase nào                                                                                                                                                          |
| D16 | **Refund chỉ full-order**, 7 ngày, license chưa từng kích hoạt                                                                       | PayOS hoàn theo order, không theo từng item                                                                                                                                                         |
| D17 | **Amount mismatch ≤ 1% auto-accept + log**                                                                                           | Tránh phải can thiệp tay cho sai số làm tròn                                                                                                                                                        |
| D18 | **Suspend VPS không chặn khi còn lệnh mở**                                                                                           | Nếu chặn, khách nợ mãi bằng cách giữ 1 lệnh. Bù: cảnh báo 24h trước                                                                                                                                 |


### 7.1 Bảng lock đã dùng (đặt tên thống nhất)


| Lock name                        | Bảo vệ                             |
| -------------------------------- | ---------------------------------- |
| `checkout:{cart_id}`             | 2 checkout đồng thời cùng giỏ      |
| `payos:webhook:{order_code}`     | 2 webhook đồng thời cùng order     |
| `bot_switch:{mt5_account_id}`    | 2 switch request chồng nhau        |
| `provisioning:{vps_instance_id}` | 2 tiến trình provisioning cùng VPS |


### 7.2 Bẫy đã biết (đừng vấp lại)


| Bẫy                                                                      | Sự thật                                                                |
| ------------------------------------------------------------------------ | ---------------------------------------------------------------------- |
| `comment` của MT5 tối đa **31 ký tự**                                    | Dùng `uuid4().hex[:16]`, KHÔNG dùng `str(uuid4())` (36 ký tự → bị cắt) |
| `ACCOUNT_LOGIN` có thể vượt 32-bit                                       | Dùng `long` + `%I64d` trong MQL5, không `(int)`                        |
| `DEAL_ENTRY_INOUT` chỉ có trên **netting** account                       | Trên hedging không bao giờ trigger — vô hại, đừng tưởng bug            |
| `PayOS` checksum nằm trong **body field `signature`**, không phải header | Xác nhận lại tên field với docs PayOS hiện hành                        |
| PayOS `orderCode` giới hạn **~9 chữ số**                                 | Clamp + retry khi sinh                                                 |
| `WebRequest` trả **HTTP status**, `-1` chỉ là lỗi mạng                   | Phải check `res` trong 200–299, không chỉ `res == -1`                  |
| `MQL5` struct chứa `string` là dynamic                                   | **Không** khai báo được fixed-size array kiểu đó — verify khi build    |


### 7.3 Degraded mode (khi thiếu cấu hình)


| Thiếu env                           | Hệ quả                                                                                        | Có chấp nhận không?                                    |
| ----------------------------------- | --------------------------------------------------------------------------------------------- | ------------------------------------------------------ |
| `DATABASE_POOL_URL`                 | checkout không có `SELECT ... FOR UPDATE` thật; lock rơi về in-process; notification qua REST | **Chỉ chấp nhận 1 uvicorn worker.** Nhiều worker = sai |
| `SUPABASE_URL` / `SERVICE_ROLE_KEY` | notification tắt (log warning, không raise)                                                   | Chỉ dev                                                |
| PayOS keys                          | checkout không tạo được link                                                                  | Phase A cần                                            |


---

## 8. CASE TABLE (các tình huống phải xử lý)

> Đây là danh sách case tích luỹ qua 6 vòng review. Nhóm theo chủ đề. "→" là cách
> xử lý đã chốt.

### 8.1 Thương mại (Cart / Checkout / PayOS)


| Case                                                         | Xử lý                                                                                     |
| ------------------------------------------------------------ | ----------------------------------------------------------------------------------------- |
| 2 webhook PayOS đến cùng lúc                                 | Lock `payos:webhook:{order_code}` + atomic UPDATE `WHERE status IN ('PENDING','EXPIRED')` |
| Khách bấm thanh toán nhiều lần / nhiều tab                   | Idempotency key `UNIQUE(user_id, key)`                                                    |
| Giá bot đổi giữa add-to-cart và checkout                     | Snapshot giá vào `order_items` **lúc checkout**                                           |
| Bot ngừng bán khi đang trong giỏ                             | Re-validate `is_active` lúc checkout                                                      |
| Chuyển thiếu/thừa tiền                                       | ≤1% auto-accept + log; vượt → `manual_review`, không auto-reject                          |
| Mua trùng bot đang sở hữu chưa dùng hết                      | Chặn 2 lớp: add-to-cart **và** checkout (`count_usable_licenses`)                         |
| Webhook đến sau khi order đã EXPIRED                         | Vẫn giao hàng + cờ `late_payment = true`                                                  |
| Order PENDING quá 15 phút                                    | Cron `expire_pending_orders` → EXPIRED + mở lại cart                                      |
| Chữ ký webhook giả mạo                                       | Reject 400 ngay, không chạm DB                                                            |
| `order_code` trùng do sinh cùng mili-giây                    | UNIQUE + retry sinh mã (clamp 9 chữ số)                                                   |
| **Crash giữa TX1 và TX2b** → order PENDING **không có link** | Idempotent-retry phải **tái tạo link**; order FAILED thì cho tạo order mới                |
| Pending order hết hạn lúc checkout                           | `expire_order` **phải mở lại cart ngay trong tx** (không chờ cron 5 phút)                 |
| PayOS báo duplicate orderCode khi retry                      | Bắt lỗi → fetch existing link, không trả 502                                              |
| Refund 1 bot trong combo                                     | **Không hỗ trợ** — chỉ hoàn toàn bộ order                                                 |
| **PayOS gọi trong transaction**                              | **Luôn ngoài transaction** (two-phase)                                                    |
| Webhook event huỷ/hết hạn link                               | Rẽ nhánh theo event code, log non-success, lưu `received_amount`                          |
| Order COMPLETED mà khách bấm thanh toán lại                  | Trả "Bạn đã sở hữu bot — xem Thư viện", không trả link chết                               |


### 8.2 Thư viện / Kích hoạt / Switch


| Case                                                      | Xử lý                                                                                              |
| --------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| 2 yêu cầu switch chồng nhau                               | Lock `bot_switch:{mt5_account_id}` + chặn tạo request thứ 2 khi đang có request mở                 |
| Lệnh mở phát sinh sau khi admin đã duyệt                  | Set `pending_stop = true` ngay khi duyệt (chặn lệnh MỚI, vẫn quản lý lệnh cũ)                      |
| Vẫn còn lệnh mở đúng 06:00                                | `DEFERRED`, dời 1 ngày + notify admin                                                              |
| **Heartbeat cũ tới 60s → kill EA khi đang có lệnh**       | Bắt buộc heartbeat **&lt; 2 phút** VÀ `open_positions_count == 0`, không đạt → DEFERRED            |
| **Deploy bot mới fail sau khi đã kill bot cũ**            | Chỉ mark `PERMANENTLY_STOPPED` **sau khi** bot mới chạy; fail → tự **redeploy bot cũ**             |
| Cả redeploy cũ lẫn mới đều fail                           | `FAILED_NEEDS_MANUAL` + alert critical (không tự thử nữa)                                          |
| Admin reject request đang DEFERRED                        | **Clear `pending_stop`**                                                                           |
| Magic number tái sử dụng                                  | Chỉ tái dùng cho **cùng license** (redeploy). Cấm dùng cho license khác                            |
| `**deploy_bot_to_vps` insert mù → đụng UNIQUE(magic)**    | Upsert/reactivate theo `(license_id, magic_number)`                                                |
| Race sinh magic number                                    | Postgres `SEQUENCE bot_magic_seq`                                                                  |
| Invariant "1 ACTIVE" bị phá tạm thời khi switch           | `get_active_license` **loại trừ** license có instance `pending_stop = true`                        |
| `pending_stop` treo vĩnh viễn                             | Clear khi reject ở APPROVED/DEFERRED **và** khi redeploy-rollback thành công                       |
| License đã REFUNDED bị đếm vào suất sở hữu                | `count_usable_licenses` chỉ đếm `OWNED_INACTIVE` + `ACTIVE`                                        |
| Khách tự đổi Master password → bot chết ở lần restart sau | Luồng **"Cập nhật Master password"** + xác nhận qua job `VERIFY_LOGIN`                             |
| Khách trade tay trên cùng tài khoản (terminal thứ 2)      | Bot không lây (mọi guard theo magic), nhưng equity/dashboard trộn P&amp;L → `bot_equity` ở Phase E |
| Admin REJECT từ DEFERRED (defer loop)                     | Reject phải clear `pending_stop` + escalate admin nếu defer quá N lần                              |


### 8.3 Provisioning / Agent / Bản quyền


| Case                                               | Xử lý                                                                                          |
| -------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| 2 tiến trình provisioning cùng VPS                 | Lock `provisioning:{vps_instance_id}`                                                          |
| Nhân viên vận hành nghỉ việc                       | Rotate credential trung tâm định kỳ + ngay khi có biến động nhân sự                            |
| Khách không có RDP nên không tự kiểm tra được      | Dashboard trạng thái (RUNNING/STOPPED, heartbeat cuối, số lệnh) — **không** show terminal thật |
| Khách nghi ngờ công ty tự thao tác                 | `bot_audit_logs` **hiển thị được cho khách xem**                                               |
| **Password lộ vào `vps_jobs.payload`/log**         | Secrets tách khỏi job; lấy qua `/secrets` (rate-limit + audit); truyền qua ENV VAR             |
| `**license_key` plaintext nằm trong job payload**  | Cũng đi qua `/secrets`; job chỉ giữ `license_key_hash` để agent đối chiếu                      |
| `/secrets` lấy được mãi khi job đã xong            | Chỉ khi `status == 'IN_PROGRESS'` + TTL 15 phút từ `picked_up_at`                              |
| Job kẹt `IN_PROGRESS` do agent crash               | Cron `vps_job_reaper` (10 phút → FAILED, retry vì idempotent)                                  |
| AgentToken bake vào golden image                   | **Inject qua user-data** lúc tạo VPS; fallback bootstrap token 1 lần                           |
| Provider không hỗ trợ custom user-data cho Windows | Đưa vào checklist chọn provider (Phase D)                                                      |
| Domain license server chưa whitelist               | Golden image đã whitelist sn → clone thay vì cấu hình lại                                      |
| Bot `.ex4/.ex5` cập nhật version mới               | Rollout theo batch + rollback (`bot_builds`)                                                   |
| Deploy không rõ version EA nào                     | Bảng `bot_builds` (version + sha256 + storage_path)                                            |
| `vps_jobs` tích luỹ vô hạn                         | Cron archive &gt; 30 ngày                                                                      |


### 8.4 License Server / EA


| Case                                                            | Xử lý                                                                                                              |
| --------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| **License key lộ → gắn vào terminal khác vẫn ACTIVE**           | 3 lớp: account_login khớp, magic_number khớp, auto-revoke sau N lần mismatch                                       |
| **Response heartbeat không ký → fake DNS mở khoá cả fleet**     | HMAC-SHA256 + timestamp replay window 300s; không verify được = coi như mất kết nối                                |
| EA mất kết nối license server do lỗi mạng                       | **Grace period 30 phút** trước khi fail-safe tắt lệnh mới                                                          |
| `TimeGMT()` lúc Init có thể lệch khi terminal chưa connect      | Không phải bug — `wait_for_first_heartbeat_confirmed_active` poll tới timeout. **Đừng "tối ưu" thành reject-ngay** |
| Heartbeat bị DoS bằng license_key ngẫu nhiên                    | Rate-limit theo IP + cache lookup TTL 5s + **sample log 1/10**                                                     |
| `**HistorySelect` mỗi tick → terminal trễ, miss entry**         | Cache `realized` qua `OnTradeTransaction`, `OnTick` chỉ cộng floating                                              |
| Cache mất khi EA/terminal restart                               | **Seed 1 lần** trong `Init()` bằng `HistorySelect` (restart-proof), sau đó incremental                             |
| Đọc deal property thiếu `HistoryDealSelect` → profit = 0        | Luôn `HistoryDealSelect(dealTicket)` trước khi đọc                                                                 |
| Bỏ sót `DEAL_ENTRY_INOUT` khi tính realized P&amp;L             | Đếm cả `DEAL_ENTRY_OUT` **và** `DEAL_ENTRY_INOUT`                                                                  |
| Daily drawdown tính trên **toàn bộ ACCOUNT_EQUITY**             | Khách trade tay → bot dừng oan. Sửa: tính P&amp;L riêng theo **đúng magic**                                        |
| Mẫu số `g_dayStartEquity` vẫn là equity toàn tài khoản          | **Chấp nhận có chủ đích** (đủ cho circuit-breaker), ghi rõ tài liệu                                                |
| `**WebRequest` gọi trực tiếp trong `OnTradeTransaction`**       | Block EA tới 5s giữa lúc giá chạy → đẩy vào queue, gửi theo lô trong `OnTimer`                                     |
| `**PostJson`/heartbeat coi HTTP 500 là thành công**             | Mất data im lặng → phải check `res` trong 200–299                                                                  |
| EA resend báo cáo do lỗi mạng tạm thời                          | `UNIQUE(mt5_account_id, ticket)` + **upsert**, không double-insert                                                 |
| Queue report đầy → drop im lặng                                 | Log cảnh báo + tăng `MAX_QUEUE` (200)                                                                              |
| Seed map `position_id → open info` mất khi restart              | Seed từ `PositionsTotal()` trong `Init()`                                                                          |
| Lệnh **không có Stop Loss**                                     | `RiskGuard` từ chối tuyệt đối ở **mọi tier**, không chỉ cảnh báo                                                   |
| Vị thế đang mở thiếu SL do lỗi khác                             | Khoá **toàn bộ** lệnh mới cho tới khi xử lý xong                                                                   |
| **Dashboard báo RUNNING khi EA thực tế INVALID (1-2 phút đầu)** | Kích hoạt license **trước** khi deploy (rollback nếu fail); chỉ coi thành công khi EA báo đúng `ACTIVE`            |
| `**magic_number` sinh ra nhưng không tới được EA**              | Pipeline phải xuyên suốt: deploy script → `.set` → input EA → `request.magic` khi đặt lệnh                         |
| Timestamp EA theo giờ broker bị lưu nhầm như UTC                | `TimeGMT()` cho mốc hiện tại, `BrokerTimeToUtcEpoch()` cho mốc lịch sử                                             |
| Clock skew server ↔ broker                                      | NTP sync trên golden image + dùng `TimeTradeServer()` khi so timestamp trong MQL5                                  |


### 8.5 Billing VPS


| Case                                                 | Xử lý                                                                              |
| ---------------------------------------------------- | ---------------------------------------------------------------------------------- |
| Khách không thanh toán VPS định kỳ                   | Grace 3 ngày → SUSPEND → 30 ngày → TERMINATE                                       |
| Suspend khi đang có lệnh mở                          | Không chặn cng, dựa SL server-side + cảnh báo 24h                                  |
| Không ai đăng nhập lại MT5 sau khi VPS hết suspend   | Job `START_TERMINAL` tự động sau `resume`                                          |
| Không đối soát được lịch sử thanh toán VPS           | Bảng `vps_billing_charges`                                                         |
| `vps_subscriptions` không được tạo → cron không quét | **Tạo ở provisioning lần đầu** (M4, review pass 6)                                 |
| PayOS không hỗ trợ recurring                         | Mỗi kỳ tạo link mới + notify. Muốn auto-charge → cổng có subscription (MoMo/VNPay) |


### 8.6 Bảo mật / Credential


| Case                                                          | Xử lý                                                                                               |
| ------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| Rò rỉ credential hạ tầng bên thứ 3                            | Rotate định kỳ, 2FA, tách prod/staging, audit log mọi lần gọi                                       |
| Rò rỉ Master password qua vận hành thủ công                   | Chỉ 1 service đọc qua KMS, audit log, **cấm lưu ngoài hệ thống**; script tự xoá config sau khi chạy |
| `master_password` plaintext trên disk VPS                     | `start_config.ini` bị **xoá** sau khi terminal chạy (tồn tại vài chục giây)                         |
| `AllowDllImport=1` không cần thiết                            | Đổi về `0` (EA chỉ dùng WebRequest)                                                                 |
| Session admin hết hạn làm admin endpoints trả 500 thay vì 401 | `log_security_event` phải nhận đúng signature (đã fix)                                              |


---

## 9. CRON JOBS


| Job                            | Tần suất                                          | Việc làm                                                                                    |
| ------------------------------ | ------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| `expire_pending_orders`        | 5 phút                                            | Order PENDING quá 15 phút → EXPIRED + mở lại cart                                           |
| `payos_polling_fallback`       | 1 phút                                            | Order PENDING sắp hết hạn → tra cứu trạng thái qua PayOS API (không phụ thuộc 100% webhook) |
| `execute_approved_switches`    | 1 lần/ngày **06:00 Asia/Ho_Chi_Minh** (tz-aware!) | Thực thi switch APPROVED tới hạn                                                            |
| `enforce_vps_billing`          | 1 lần/ngày                                        | Thu phí, OVERDUE, SUSPEND/TERMINATE                                                         |
| `retry_failed_refunds`         | 10 phút                                           | Retry refund PayOS lỗi                                                                      |
| `check_ea_heartbeat`           | 5 phút                                            | Alert nếu bot RUNNING không heartbeat &gt; 10 phút                                          |
| `provisioning_verify_timeout`  | 2 phút                                            | `provisioning_status='PROVISIONING'` quá X phút chưa heartbeat → FAILED + alert             |
| `vps_job_reaper`               | 5 phút                                            | `vps_jobs IN_PROGRESS` quá 10 phút → FAILED + retry                                         |
| `daily_equity_snapshot_rollup` | 1 lần/ngày                                        | Chốt sub-period Dietz + cập nhật HWM                                                        |
| `overdue_switch_review_alert`  | 1 giờ                                             | Alert nội bộ nếu switch request treo quá SLA                                                |
| `vps_jobs_archive`             | 1 lần/ngày                                        | Job DONE/FAILED &gt; 30 ngày → xoá/bảng lạnh                                                |


---

## 10. ROADMAP — PHASE &amp; TRẠNG THÁI


| Phase                              | Nội dung                                                                                                                                       | Phụ thuộc                       | Trạng thái                      |
| ---------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------- | ------------------------------- |
| **0 — Trading safety**             | `try/finally` pending-flag, `_closing_tickets`, serialize MT5 qua pipeline, torn-state fix, spread guard                                       | —                               | ✅ **XONG**                      |
| **1 — Async foundation**           | httpx pool, auth non-blocking, asyncpg direct path, risk_engine, market_data board                                                             | —                               | ✅ **XONG**                      |
| **2 — Data layer**                 | index + view `admin_account_summary`, LOCK_HARD trigger, `unlocked_by`                                                                         | migration                       | 🟡 **SQL xong**, chờ chạy (M1)  |
| **B-lite — Notification**          | `notifications` + service + WS hub + REST API                                                                                                  | migration (M2)                  | ✅ **CODE XONG**, chờ chạy M2/M3 |
| **A — Commerce**                   | `bots`/`carts`/`orders`, two-phase checkout qua asyncpg, PayOS client + webhook, cron expire + polling fallback, tab admin đối soát            | PayOS (M5/M6) + M4              | ✅ **CODE XONG + LIVE PASS ĐƠN ĐẦU TIÊN** (2026-09-21) — order 200000005/10.000 VND → `PAID` + license `OWNED_INACTIVE` + cart unlock, **qua `poll_pending_orders` khi webhook thật bị từ chối chữ ký.** Xem phân tích live ở mục M6c |
| **B — Library &amp; License**      | `user_bot_licenses`, `mt5_accounts`, activation nhánh 1/2, `bot_switch_requests` + tab admin duyệt, `user_consents`, refund flow               | Phase A                         | ⬜ CHƯA                          |
| **C — License Server + EA**        | endpoint heartbeat (HMAC), LicenseGuard/RiskGuard/ReportingModule/OrderExecution, pipeline magic end-to-end                                    | Phase B + dev EA song song      | ⬜ CHƯA                          |
| **D — Provisioning + VPS Billing** | VPS provider module, **Windows Agent Service**, `deploy_bot.ps1` (đã vá 5 lỗi), golden image, cron billing                                     | Phase C + chọn provider (M7/M8) | ⬜ CHƯA                          |
| **E — Reporting + Performance**    | `/api/trades/report` (upsert), `/api/accounts/snapshot`, `trade_history`, Dietz rollup + HWM, `bot_equity`, dashboard khách + màn hình Flutter | Phase C                         | ⬜ CHƯA                          |


**Bước tiếp theo ngay:** Phase A (không bị chặn bởi gì ngoài PayOS keys).
Notification đã sẵn sàng để Phase A/B gọi vào.

---

## 11. CÂU HỎI CÒN MỞ (cần user/công ty chốt)


| #   | Câu hỏi                                                                           | Trạng thái                 |
| --- | --------------------------------------------------------------------------------- | -------------------------- |
| Q1  | Kênh **email** cho sự kiện quan trọng — nhà cung cấp SMTP nào?                    | ⏳ chưa chốt (in-app đã có) |
| Q2  | Đổi `SHARED_SECRET` định kỳ bao lâu? (mỗi lần đổi = rebuild + rollout toàn fleet) | ⏳ chưa chốt                |
| Q3  | Nhà cung cấp VPS nào (cần custom user-data cho Windows)?                          | ⏳ Phase D                  |
| Q4  | Refund khi bot chưa kích hoạt: **7 ngày** (đã chốt) — có cần ngoại lệ nào không?  | ✅ chốt 7 ngày              |
| Q5  | Có cần hiển thị `bot_equity` tách riêng trên dashboard không?                     | ⏳ Phase E                  |


*(Đã chốt: advisory lock thay Redis · refund full-order 7 ngày · amount mismatch ≤1% ·
suspend không chặn khi còn lệnh · SharedSecret global bake vào EA · magic tái dùng cho cùng license.)*

---

## 12. LỊCH SỬ REVIEW (để không sửa lại cái đã sửa)

6 vòng review đã chạy. Mật độ lỗi nghiêm trọng giảm dần: **15 → 12 → 15 → 12 → 9 → 0**.


| Pass | Nội dung chính                                                                                                        |
| ---- | --------------------------------------------------------------------------------------------------------------------- |
| 1    | 15 lỗi đầu: schema trùng, `users` dư, `equity_snapshots` trùng, enum inline                                           |
| 2    | 4 lỗi mới: `HistorySelect` mỗi tick, `PostJson` coi 500 là OK, thiếu kênh remote execution, 2 lỗ hổng trình tự switch |
| 3    | 4 lỗi mới: password trong job payload, PayOS trong transaction, cache mất khi restart, license timing                 |
| 4    | 4 lỗi mới: license không bind account, UNIQUE violation phá rollback, khách đổi password, timezone                    |
| 5    | 3 lỗi mới: thiếu cột `license_key_hash`, mâu thuẫn SharedSecret, mất fix P6 khi refactor                              |
| 6    | **0 lỗi critical** — chỉ integration details (M1–M6) + quyết định D1                                                  |


### 12.1 Bài học đã rút ra (QUAN TRỌNG)

> **Mỗi lần viết lại một hàm đã từng bị flag lỗi, phải liệt kê lại toàn bộ case
> cũ gắn với hàm đó trước khi coi là "sửa xong".**
>
> Đã xảy ra 2 lần: (a) refactor `checkout` ở pass 3 làm mất fix P6 (order không
> link); (b) fix P2 ở pass 4 suýt làm hỏng chính path rollback. Fix mới vô tình
> xoá fix cũ là loại lỗi khó thấy nhất.

### 12.2 Checklist trước khi coi một hàm là xong

1. Liệt kê mọi case đã đóng liên quan tới hàm đó (grep mục 8).
2. Kiểm tra mọi **call site** (đổi chữ ký → cập nhật hết).
3. Kiểm tra mọi cột/bảng mà hàm tham chiếu **thực sự tồn tại** trong schema.
4. Chạy `python -m unittest test_safety` + `py_compile`.
5. Nếu là hàm async: xác nhận không có I/O blocking trong thân hàm.

---

## 13. BẢN ĐỒ FILE (tìm nhanh)


| Cần gì                               | Ở đâu                                                                                |
| ------------------------------------ | ------------------------------------------------------------------------------------ |
| Web app chính (routes, lifespan, WS) | `app.py`                                                                             |
| Bot engine cũ (reference)            | `bot.py`                                                                             |
| Auth / session / Principal           | `services/auth_service.py`, `services/auth_dependencies.py`                          |
| HTTP pool                            | `services/async_http.py`                                                             |
| DB direct path (asyncpg)             | `services/direct_db.py`                                                              |
| **Advisory lock**                    | `services/db_locks.py`                                                               |
| **Notification**                     | `services/notification_service.py`                                                   |
| Risk sizing                          | `services/risk_engine.py`, `core/risk.py`                                            |
| Broker IPC serialize                 | `services/execution_pipeline.py`                                                     |
| Repository Supabase                  | `repositories/supabase_repository.py`                                                |
| Migration đang chạy                  | `supabase/schema.sql`, `supabase/*.sql`                                              |
| **Migration chờ chạy**               | `supabase/consolidated_pending_migration.sql`, `supabase/notification_migration.sql` |
| Admin UI                             | `templates/admin_dashboard.html`                                                     |
| Khách UI                             | `templates/index.html`                                                               |
| Mobile Flutter                       | `mobile/` (43 files, screen: `mobile/lib/features/...`)                              |
| Blueprint thiết kế mới               | `_spec_planner_prompt.md`, các file plan trong repo                                  |
| Test                                 | `test_safety.py` (44 tests)                                                          |


---

## 14. LỆNH THƯỜNG DÙNG

```powershell
cd C:\Users\ITPC\orca\workspaces\bot_trade\Plan-2

# chạy app
python app.py

# test + syntax
python -m unittest test_safety
python -m py_compile app.py bot.py

# xem migration nào còn chờ
Get-ChildItem supabase\*.sql | Select-Object Name, LastWriteTime
```

---

**HẾT.** Nếu bạn là một phiên làm việc mới: đọc mục 0 → 1 → 5 → 6 là đủ để bắt
đầu code. Mục 8 và 12 là nơi tra cứu khi cần quyết định chi tiết.
