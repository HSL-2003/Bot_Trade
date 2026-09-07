# Hotfix: Missing require_admin Function

## Date: 2026-09-04 14:31

### 🐛 Problem

Server crashed with `NameError: name 'require_admin' is not defined` when accessing `/api/admin/overview` endpoint.

**Error trace:**
```
File "C:\Users\ITPC\orca\workspaces\bot_trade\Plan-2\app.py", line 1201, in admin_overview
    require_admin(request)
    ^^^^^^^^^^^^^
NameError: name 'require_admin' is not defined
```

### ✅ Solution

Added the missing `require_admin()` helper function in `app.py` after `bearer_token()` function.

**Function added:**
```python
def require_admin(request: Request) -> None:
    """Verify that the request comes from an admin user. Raises HTTPException if not."""
    try:
        principal = session_service.authenticate(bearer_token(request))
        if "admin" not in principal.roles:
            log_security_event(
                event="admin_access_denied",
                severity="warning",
                user_id=principal.user_id,
                details={"roles": list(principal.roles), "endpoint": str(request.url.path)},
                correlation_id=getattr(request.state, "correlation_id", None),
                client_ip=request.client.host if request.client else None,
            )
            raise HTTPException(status_code=403, detail="Admin access required")
    except AuthenticationError as exc:
        log_security_event(
            event="admin_auth_failed",
            severity="warning",
            details={"endpoint": str(request.url.path)},
            correlation_id=getattr(request.state, "correlation_id", None),
            client_ip=request.client.host if request.client else None,
        )
        raise HTTPException(status_code=401, detail=str(exc)) from exc
```

### 📋 Function Behavior

1. **Authentication Check:**
   - Uses `session_service.authenticate()` with bearer token
   - Raises 401 if authentication fails

2. **Authorization Check:**
   - Verifies `"admin"` is in `principal.roles`
   - Raises 403 if user is not admin

3. **Security Logging:**
   - Logs `admin_access_denied` when non-admin tries to access
   - Logs `admin_auth_failed` when authentication fails
   - Includes correlation ID, client IP, user ID, and endpoint details

### 🎯 Affected Endpoints (16 total)

All admin endpoints now have proper authentication/authorization:

1. `GET /api/admin/overview`
2. `GET /api/admin/accounts`
3. `GET /api/admin/accounts/{account_id}`
4. `POST /api/admin/accounts/{account_id}/bot-type`
5. `POST /api/admin/accounts/{account_id}/status`
6. `POST /api/admin/accounts/{account_id}/lock`
7. `PUT /api/admin/users/{user_id}/roles`
8. `POST /api/admin/accounts/{account_id}/revoke-sessions`
9. `POST /api/admin/accounts/{account_id}/deploy`
10. `POST /api/admin/accounts/{account_id}/halt`
11. `GET /api/admin/trades`
12. `GET /api/admin/bot-types` *(NEW)*
13. `GET /api/admin/bot-types/{id}` *(NEW)*
14. `POST /api/admin/bot-types` *(NEW)*
15. `PUT /api/admin/bot-types/{id}` *(NEW)*
16. `DELETE /api/admin/bot-types/{id}` *(NEW)*

### ✅ Testing

- [x] Python compilation successful
- [x] No syntax errors
- [x] Function uses existing imports (`log_security_event` already imported)
- [x] Function follows same pattern as other auth helpers

### 🔐 Security Features

1. **Double-layer protection:**
   - Authentication (valid token)
   - Authorization (admin role)

2. **Audit trail:**
   - Security events logged for monitoring
   - Includes user context and request details

3. **Proper HTTP status codes:**
   - 401 for authentication failure
   - 403 for authorization failure (authenticated but not admin)

### 📝 Files Changed

**Modified:**
- `app.py` (+37 lines) - Added `require_admin()` function

**Created:**
- `docs/HOTFIX_REQUIRE_ADMIN.md` (this file)

### 🚀 Deployment

No restart required if using `--reload` mode. Otherwise:

```bash
# Restart server
uvicorn app:app --host 127.0.0.1 --port 8000 --reload
```

### 🧪 Quick Test

```bash
# Should return 401 without token
curl http://127.0.0.1:8000/api/admin/overview

# Should return admin dashboard with valid admin token
curl http://127.0.0.1:8000/api/admin/overview \
  -H "Authorization: Bearer YOUR_ADMIN_TOKEN"

# Should return 403 with non-admin token
curl http://127.0.0.1:8000/api/admin/overview \
  -H "Authorization: Bearer YOUR_TRADER_TOKEN"
```

### 🎓 How to Get Admin Role

If using Supabase:

```sql
-- Set admin role in user_profiles
UPDATE user_profiles
SET roles = '["admin", "trader"]'::jsonb
WHERE user_id = 'your-user-uuid';
```

If using InMemorySessionService (development):

Check `services/auth_service.py` for hardcoded demo users or registration logic.

### 📊 Impact

- **Status:** ✅ Fixed
- **Severity:** High (blocking all admin endpoints)
- **Lines changed:** 37 lines added
- **Breaking changes:** None
- **Backwards compatible:** Yes
