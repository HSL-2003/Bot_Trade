# AUTH_REFACTOR_SPEC.md

## 1. Hiện trạng & Pain Points

### 1.1 Cấu trúc hiện tại

| File | Vai trò | Ghi chú |
|------|---------|---------|
| `services/auth_service.py` | Domain boundary | Định nghĩa `Principal`, `Session`, `AuthenticationError`, `InMemorySessionService`, `SupabaseSessionService`, và các helper (`normalize_email`, `validate_password_strength`, `sanitize_display_name`). |
| `app.py` | HTTP layer + auth helpers lẫn lộn | Chứa endpoint `/api/auth/*`, đồng thời chứa 6 helper/bộ phận auth: `bearer_token`, `set_session_cookie`, `principal_from_request`, `principal_optional_from_request`, `require_admin`, `account_id_from_request`, `render_auth`. File này dài ~1350 dòng, trong đó ~200 dòng là auth logic. |
| `templates/auth.html` | Template đăng nhập/đăng ký | Template phức tạp có 3D showcase, tunnel transition, social login. Được render bằng `render_auth()` với string replacement thủ công. |
| `static/auth.js` | Client-side auth | Gọi `/api/auth/register`, `/api/auth/login`, `/api/auth/magic-link`, xử lý OAuth redirect, lưu token vào `localStorage`. |
| `test_safety.py` | Test auth | Bao gồm `AuthDisplayNameTests`, `AuthSocialLoginTests`, `AuthOAuthEndpointTests`, và một số integration test dùng `TestClient` gọi trực tiếp endpoint. |

### 1.2 Pain points

1. **Auth helpers lẫn lộn trong `app.py`**
   - `bearer_token`, `set_session_cookie`, `principal_from_request`, `principal_optional_from_request`, `require_admin`, `account_id_from_request` nằm trộn với route handler của trading bot (`/api/start`, `/api/trade`, v.v.).
   - `app.py` đóng vai trò vừa routing, vừa auth middleware, vừa template renderer, vừa business logic → vi phạm SRP, khó maintain.

2. **Không có interface `SessionService` chuẩn**
   - `InMemorySessionService` và `SupabaseSessionService` cùng expose `register`, `login`, `magic_link`, `login_with_supabase_token`, `authenticate`, `revoke`, `create`, `set_user_roles`.
   - Không có ABC/protocol enforce contract; thiếu instance-level dependency injection (module-level global `session_service` được gán theo env var).

3. **`require_admin` không phải FastAPI dependency**
   - Được implement như plain function gọi `principal_from_request(request)` → không dùng được với `Depends()`.
   - Các admin endpoint (`/api/admin/*`) hiện gọi `require_admin(request)` trực tiếp thay vì inject vào route signature.

4. **Trùng lặp logic trích xuất token**
   - `bearer_token()` trong `app.py` (dùng cho API).
   - `account_id_from_request()` tự đọc `Authorization` header.
   - WebSocket endpoint (`/ws`) tự đọc `token` từ query params hoặc cookie.
   - Ba nơi khác nhau làm cùng một việc: lấy token từ request.

5. **`render_auth()` lạm dụng string replacement**
   - Dùng `content.replace()` thay v template engine; dễ hỏng nếu template thay đổi. Tuy nhiên đây là vấn đề phụ, không ảnh hưởng contract.

6. **Test hiện tại thiếu coverage cho auth dependency layer**
   - Chỉ test `InMemorySessionService` trực tiếp và endpoint qua `TestClient`.
   - Không test helper như `bearer_token`, `set_session_cookie`, `principal_from_request` độc lập.

---

## 2. Phạm vi đề xuất

### 2.1 Files sẽ đổi

| File | Hành động | Mô tả |
|------|-----------|-------|
| `services/auth_service.py` | **Sửa** | Thêm `SessionService` protocol/ABC; đảm bảo cả 2 class implement rõ ràng. Có thể thêm `__all__`. |
| `services/auth_dependencies.py` | **Tạo mới** | Chứa FastAPI dependencies: `get_bearer_token`, `get_current_principal`, `get_current_principal_optional`, `require_admin_dep`, `get_account_id`, và `set_session_cookie`. Dependency sẽ inject `session_service` qua `app.state` hoặc `Depends()` chain. |
| `app.py` | **Sửa** | Xóa các helper auth ra khỏi file; import từ `auth_dependencies`. Cập nhật endpoint `/api/auth/*` và admin/trading routes để dùng `Depends()`. Giữ nguyên `render_auth`, route definitions, và toàn bộ JSON response. |
| `test_safety.py` | **Sửa** | Thêm test cho `auth_dependencies` (unit + integration với `TestClient`). Giữ nguyên test cũ. |

### 2.2 Cách đổi chi tiết

#### 2.2.1 `services/auth_service.py`

```python
from typing import Protocol, runtime_checkable

@runtime_checkable
class SessionService(Protocol):
    def register(self, email: str, password: str, display_name: str | None = None) -> dict: ...
    def login(self, email: str, password: str) -> dict: ...
    def magic_link(self, email: str) -> None: ...
    def login_with_supabase_token(self, supabase_token: str) -> dict: ...
    def create(self, principal: Principal) -> Session: ...
    def authenticate(self, token: str) -> Principal: ...
    def revoke(self, token: str) -> None: ...
    def set_user_roles(self, user_id: str, roles: list[str]) -> list[str]: ...
```

- `InMemorySessionService` và `SupabaseSessionService` hiện đã satisfy protocol này; chỉ cần thêm annotation `SessionService` vào `__init__` của chúng để rõ ràng.
- Không thay đổi method signature, không thay đổi return type.

#### 2.2.2 `services/auth_dependencies.py` (mới)

```python
from typing import Optional, Callable
from fastapi import Request, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from services.auth_service import AuthenticationError, Principal, SessionService

security = HTTPBearer(auto_error=False)

async def get_bearer_token(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> str:
    """
    Trích xuất bearer token từ:
    1. Authorization: Bearer <token>
    2. Cookie: session_token (browser flow fallback)
    """
    token = credentials.credentials if credentials else None
    if not token:
        token = request.cookies.get("session_token")
    if not token:
        raise HTTPException(status_code=401, detail="Bearer authentication is required")
    return token

async def get_current_principal(
    token: str = Depends(get_bearer_token),
    session_service: SessionService = Depends(_get_session_service),
) -> Principal:
    try:
        return session_service.authenticate(token)
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

async def get_current_principal_optional(
    request: Request,
    session_service: SessionService = Depends(_get_session_service),
) -> Optional[Principal]:
    try:
        token = request.cookies.get("session_token")
        if not token:
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                token = auth[7:].strip()
        if not token:
            return None
        return session_service.authenticate(token)
    except Exception:
        return None

async def require_admin_dep(
    principal: Principal = Depends(get_current_principal),
    request: Request = None,
) -> Principal:
    allowed = {a.strip() for a in os.getenv("ADMIN_ACCOUNT_IDS", "").split(",") if a.strip()}
    if "admin" in principal.roles or principal.account_id in allowed:
        return principal
    log_security_event(
        "admin_access_denied",
        account_id=principal.account_id,
        user_id=principal.user_id,
        correlation_id=getattr(request.state, "correlation_id", None) if request else None,
        client_ip=request.client.host if request and request.client else None,
    )
    raise HTTPException(status_code=403, detail="Admin role is required")

async def get_account_id(
    request: Request,
    principal: Optional[Principal] = Depends(get_current_principal_optional),
    session_service: SessionService = Depends(_get_session_service),
) -> str:
    """
    Xác định account_id cho request:
    1. Từ header/query X-Account-Id / account_id.
    2. Nếu có Bearer token, cross-check với principal.account_id.
    3. Fallback DEFAULT_ACCOUNT_ID.
    """
    account_id = request.headers.get("X-Account-Id") or request.query_params.get("account_id")
    require_auth = os.getenv("REQUIRE_AUTH", "false").lower() == "true" or os.getenv("ENVIRONMENT", "development").lower() == "production"
    if principal is not None:
        if account_id and account_id != principal.account_id:
            raise HTTPException(status_code=403, detail="Account scope mismatch")
        account_id = principal.account_id
    elif require_auth:
        raise HTTPException(status_code=401, detail="Bearer authentication is required")
    return account_id or os.getenv("DEFAULT_ACCOUNT_ID", "demo-account")

def set_session_cookie(response: Response, session: dict, request: Request) -> None:
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

def _get_session_service(request: Request) -> SessionService:
    return request.app.state.session_service
```

**Lưu ý quan trọng:**
- `_get_session_service` đọc từ `app.state.session_service`. Điều này yêu cầu `app.py` gán `app.state.session_service = session_service` tại startup.
- `require_admin_dep` nhận `request` thông qua `Depends` theo cách của FastAPI (`request: Request` tự động inject).
- `get_account_id` giữ nguyên logic `REQUIRE_AUTH` và `ENVIRONMENT=production` như cũ.

#### 2.2.3 `app.py` thay đổi

**Xóa:**
- `bearer_token` (dòng 200-209)
- `set_session_cookie` (dòng 212-225)
- `principal_from_request` (dòng 362-366)
- `principal_optional_from_request` (dòng 347-352)
- `require_admin` (dòng 369-382)
- `account_id_from_request` (dòng 148-165)

**Thêm:**
```python
from services.auth_dependencies import (
    get_bearer_token,
    get_current_principal,
    get_current_principal_optional,
    require_admin_dep,
    get_account_id,
    set_session_cookie,
)
```

**Cập nhật endpoint auth:**
```python
@app.post("/api/auth/register", status_code=201)
@limiter.limit("5/minute")
async def register(request: Request, payload: RegisterModel, principal: Principal = Depends(get_current_principal_optional)):
    # ... giữ nguyên body, chỉ thay principal_from_request bằng principal (nếu cần)
    ...

@app.get("/api/auth/me")
async def current_user(principal: Principal = Depends(get_current_principal)):
    return {"user_id": principal.user_id, "account_id": principal.account_id, "roles": sorted(principal.roles)}

@app.post("/api/auth/logout", status_code=204)
async def logout(token: str = Depends(get_bearer_token)):
    try:
        session_service.revoke(token)
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    response = Response(status_code=204)
    response.delete_cookie("session_token", path="/")
    return response
```

**Cập nhật admin routes:**
```python
@app.get("/api/admin/overview")
async def admin_overview(principal: Principal = Depends(require_admin_dep), ...):
    ...
```

**Cập nhật trading routes cần principal:**
```python
@app.post("/api/start")
async def start_bot(account_id: str = Depends(get_account_id), principal: Optional[Principal] = Depends(get_current_principal_optional)):
    current_bot = account_service.get_bot(account_id, principal.user_id if principal else None)
    ...
```

**WebSocket `/ws`:**
- WebSocket không hỗ trợ `Depends()` trực tiếp.
- Giữ nguyên logic đọc token từ query/cookie, nhưng gọi `session_service.authenticate(token)` trực tiếp.
- Có thể extract thành helper `authenticate_websocket(token: str) -> Principal` trong `auth_dependencies.py` để tái sử dụng.

**Startup:**
```python
app.state.session_service = session_service
```

#### 2.2.4 `templates/auth.html` & `static/auth.js`

- **Không đổi**. Template và JS client giữ nguyên.
- `render_auth()` có thể giữ trong `app.py` hoặc di chuyển sang `utils/templates.py`; không ảnh hưởng contract.

### 2.3 Giữ nguyên JSON contract

Tất cả endpoint `/api/auth/*` giữ nguyên request/response schema:

| Endpoint | Method | Request | Response |
|----------|--------|---------|----------|
| `/api/auth/register` | POST | `{email, password, display_name?}` | `{access_token, token_type, expires_at, user_id, account_id}` (201) |
| `/api/auth/login` | POST | `{email, password}` | `{access_token, token_type, expires_at, user_id, account_id, roles?}` (200) |
| `/api/auth/magic-link` | POST | `{email}` | `{message}` (200) |
| `/api/auth/social/callback` | POST | `{token}` | `{access_token, token_type, expires_at, user_id, account_id, roles}` (200) |
| `/api/auth/me` | GET | Bearer header | `{user_id, account_id, roles}` (200) |
| `/api/auth/logout` | POST | Bearer header | 204 No Content |
| `/api/auth/github` | GET | — | 307 Redirect |
| `/api/auth/google` | GET | — | 307 Redirect |

**Không thay đổi field name, status code, hay header.**

---

## 3. Rủi ro & Kế hoạch test

### 3.1 Rủi ro

| Rủi ro | Mức độ | Diễn giải | Giảm thiểu |
|---------|--------|-----------|------------|
| **WebSocket không hỗ trợ Depends** | Trung bình | FastAPI WebSocket endpoint không nhận `Depends()` như HTTP route. | Giữ nguyên logic trong endpoint, extract helper nếu cần tái sử dụng. |
| **Module-level `session_service`** | Thấp | `app.py` hiện gán `session_service` ở module level. Refactor sẽ inject qua `app.state` nhưng cần đảm bảo lifespan/startup không phụ thuộc vào global. | Gán `app.state.session_service = session_service` ngay sau khi instantiate. |
| **Breaking `require_admin` signature** | Thấp | `require_admin(request)` hiện được gọi trực tiếp trong nhiều route. Chuyển sang `Depends(require_admin_dep)` là non-breaking vì FastAPI tự inject `Request`. | Cập nhật từng route một, chạy test sau mỗi batch. |
| **`account_id_from_request` logic phức tạp** | Trung bình | Logic combine auth + account scope + env flag. Nếu refactor sai có thể expose endpoint khi `REQUIRE_AUTH` tắt. | Giữ nguyên logic 100%, chỉ di chuyển vào dependency. Viết unit test cho từng branch (auth required, auth optional, scope mismatch). |
| **Template render `render_auth`** | Thấp | String replacement dễ lỗi. | Không đổi trong scope này; có thể làm sau. |

### 3.2 Kế hoạch test

**Unit tests (mới trong `test_safety.py` hoặc `tests/test_auth_dependencies.py`):**

1. `test_get_bearer_token_from_header` — request có `Authorization: Bearer abc` → trả về `abc`.
2. `test_get_bearer_token_from_cookie_fallback` — không có header, có cookie `session_token=xyz` → trả về `xyz`.
3. `test_get_bearer_token_raises_401_when_missing` — không có header, không có cookie → 401.
4. `test_get_current_principal_valid_token` — token hợp lệ → trả `Principal`.
5. `test_get_current_principal_raises_401_on_invalid_token` — token không hợp lệ → 401.
6. `test_get_current_principal_optional_returns_none_when_no_token` — không có token → `None`.
7. `test_get_current_principal_optional_returns_principal_when_valid` — có token hợp lệ → `Principal`.
8. `test_require_admin_dep_allows_admin_role` — principal có role `admin` → pass.
9. `test_require_admin_dep_allows_allowlisted_account` — principal không có `admin` nhưng `account_id` trong `ADMIN_ACCOUNT_IDS` → pass.
10. `test_require_admin_dep_blocks_non_admin` — principal chỉ có `trader` → 403.
11. `test_get_account_id_from_header` — `X-Account-Id: acc-1` → trả về `acc-1`.
12. `test_get_account_id_from_principal_when_no_header` — token của `acc-2`, không có header → trả về `acc-2`.
13. `test_get_account_id_scope_mismatch` — header `X-Account-Id: acc-1` nhưng token là `acc-2` → 403.
14. `test_get_account_id_require_auth_enforcement` — `ENVIRONMENT=production`, không có token → 401.
15. `test_set_session_cookie_sets_httponly` — response có cookie `session_token`, `httponly=True`, `samesite=strict`.

**Integration tests (giữ nguyên, có thể bổ sung):**

- `test_register_and_login_flow` — đăng ký → login → gọi `/api/auth/me` với bearer token → 200.
- `test_logout_revokes_session` — login → logout → gọi `/api/auth/me` với token cũ → 401.
- `test_admin_endpoints_require_admin` — login với non-admin → gọi `/api/admin/overview` → 403.
- `test_oauth_endpoints_redirect` — giữ nguyên `test_google_endpoint_redirects_to_supabase`.

**Chạy test:**

```bash
python -m pytest test_safety.py -v
# Hoặc
python -m unittest test_safety -v
```

### 3.3 Thứ tự rollout

1. **Bước 1:** Thêm `SessionService` protocol vào `services/auth_service.py` (non-breaking).
2. **Bước 2:** Tạo `services/auth_dependencies.py` với các dependencies.
3. **Bước 3:** Cập nhật `app.py`:
   - Thêm `app.state.session_service`.
   - Thay thế helper cũ bằng `Depends()`.
   - Bắt đầu với endpoint `/api/auth/me` và `/api/auth/logout` (đơn giản nhất).
   - Tiếp đến `/api/admin/*` routes.
   - Cuối cùng các trading routes (`/api/start`, `/api/state`, v.v.).
4. **Bước 4:** Chạy toàn bộ `test_safety.py` + test mới. Đảm bảo 100% pass.
5. **Bước 5:** Manual smoke test: register → login → access dashboard → admin page → logout.

---

## 4. Tổng kết

Refactor này không đổi bất kỳ JSON contract nào, chỉ tái cấu trúc auth logic từ `app.py` (1350 dòng) vào module chuyên biệt `auth_dependencies.py` và chuẩn hoá interface `SessionService`. Kết quả:

- `app.py` giảm ~150-200 dòng, tập trung vào routing và trading logic.
- Auth helpers trở thành FastAPI dependencies, tái sử dụng được, dễ test.
- Cả 2 session service đều conform rõ ràng với `SessionService` protocol.
- Rủi ro thấp, có thể rollout từng endpoint.
