# Bot Types API - Quick Test Guide

## Prerequisites

1. Server đang chạy:
```bash
uvicorn app:app --host 127.0.0.1 --port 8000 --reload
```

2. Có admin token (lấy từ login endpoint hoặc sử dụng demo token nếu REQUIRE_AUTH=false)

3. Đã chạy migration trong Supabase (nếu dùng Supabase)

## Quick Tests

### 1. List Bot Types

```bash
curl http://127.0.0.1:8000/api/admin/bot-types \
  -H "Authorization: Bearer YOUR_TOKEN"
```

**Expected Response:**
```json
{
  "total": 5,
  "bot_types": [
    {
      "id": "uuid...",
      "name": "Conservative Gold",
      "risk_level": "low",
      "risk_percent": 1.0,
      ...
    }
  ]
}
```

### 2. Get Single Bot Type

```bash
curl http://127.0.0.1:8000/api/admin/bot-types/UUID_HERE \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### 3. Create New Bot Type

```bash
curl -X POST http://127.0.0.1:8000/api/admin/bot-types \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -d '{
    "name": "Test Bot",
    "description": "Testing bot type",
    "risk_level": "medium",
    "risk_percent": 2.0,
    "max_spread": 200,
    "max_daily_loss_percent": 5.0,
    "trailing_stop_enabled": true,
    "trailing_stop_distance": 100,
    "breakeven_enabled": true,
    "breakeven_trigger": 200,
    "cooldown_minutes": 15,
    "max_open_trades": 5
  }'
```

**Expected Response:**
```json
{
  "status": "created",
  "bot_type": {
    "id": "new-uuid...",
    "name": "Test Bot",
    ...
  }
}
```

### 4. Update Bot Type

```bash
curl -X PUT http://127.0.0.1:8000/api/admin/bot-types/UUID_HERE \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -d '{
    "risk_percent": 2.5,
    "max_spread": 250
  }'
```

### 5. Assign Bot Type to Account

```bash
curl -X POST http://127.0.0.1:8000/api/admin/accounts/demo-account/bot-type \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -d '{
    "bot_type_id": "UUID_OF_BOT_TYPE"
  }'
```

### 6. Soft Delete Bot Type

```bash
curl -X DELETE http://127.0.0.1:8000/api/admin/bot-types/UUID_HERE \
  -H "Authorization: Bearer YOUR_TOKEN"
```

**Expected Response:**
```json
{
  "status": "deleted",
  "bot_type_id": "uuid...",
  "delete_type": "deactivated"
}
```

### 7. List Including Inactive

```bash
curl "http://127.0.0.1:8000/api/admin/bot-types?include_inactive=true" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

## Python Test Script

```python
import requests
import json

BASE_URL = "http://127.0.0.1:8000"
TOKEN = "YOUR_ADMIN_TOKEN"  # Replace with actual token

headers = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json"
}

def test_list_bot_types():
    """Test 1: List all bot types"""
    print("🧪 Test 1: List bot types")
    response = requests.get(f"{BASE_URL}/api/admin/bot-types", headers=headers)
    print(f"Status: {response.status_code}")
    data = response.json()
    print(f"Total: {data['total']}")
    for bt in data['bot_types']:
        print(f"  - {bt['name']} ({bt['risk_level']})")
    print()
    return data['bot_types']

def test_create_bot_type():
    """Test 2: Create new bot type"""
    print("🧪 Test 2: Create bot type")
    payload = {
        "name": "Python Test Bot",
        "description": "Created from Python test script",
        "risk_level": "medium",
        "risk_percent": 2.0,
        "max_spread": 200,
        "max_daily_loss_percent": 5.0
    }
    response = requests.post(
        f"{BASE_URL}/api/admin/bot-types",
        headers=headers,
        json=payload
    )
    print(f"Status: {response.status_code}")
    data = response.json()
    print(f"Created: {data['bot_type']['name']}")
    print(f"ID: {data['bot_type']['id']}")
    print()
    return data['bot_type']

def test_get_bot_type(bot_type_id):
    """Test 3: Get bot type details"""
    print("🧪 Test 3: Get bot type details")
    response = requests.get(
        f"{BASE_URL}/api/admin/bot-types/{bot_type_id}",
        headers=headers
    )
    print(f"Status: {response.status_code}")
    data = response.json()
    print(f"Name: {data['name']}")
    print(f"Accounts using: {data['accounts_count']}")
    print()
    return data

def test_update_bot_type(bot_type_id):
    """Test 4: Update bot type"""
    print("🧪 Test 4: Update bot type")
    payload = {
        "risk_percent": 2.5,
        "description": "Updated from Python test"
    }
    response = requests.put(
        f"{BASE_URL}/api/admin/bot-types/{bot_type_id}",
        headers=headers,
        json=payload
    )
    print(f"Status: {response.status_code}")
    data = response.json()
    print(f"Updated: {data['bot_type']['name']}")
    print(f"New risk_percent: {data['bot_type']['risk_percent']}")
    print()
    return data

def test_delete_bot_type(bot_type_id):
    """Test 5: Delete bot type"""
    print("🧪 Test 5: Soft delete bot type")
    response = requests.delete(
        f"{BASE_URL}/api/admin/bot-types/{bot_type_id}",
        headers=headers
    )
    print(f"Status: {response.status_code}")
    data = response.json()
    print(f"Delete type: {data['delete_type']}")
    print()

def run_all_tests():
    """Run all tests in sequence"""
    print("=" * 60)
    print("Bot Types API - Test Suite")
    print("=" * 60)
    print()
    
    try:
        # Test 1: List
        bot_types = test_list_bot_types()
        
        # Test 2: Create
        new_bot = test_create_bot_type()
        bot_type_id = new_bot['id']
        
        # Test 3: Get
        test_get_bot_type(bot_type_id)
        
        # Test 4: Update
        test_update_bot_type(bot_type_id)
        
        # Test 5: Delete
        test_delete_bot_type(bot_type_id)
        
        print("=" * 60)
        print("✅ All tests completed successfully!")
        print("=" * 60)
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    run_all_tests()
```

## Save & Run

```bash
# Save script as test_bot_types.py
python test_bot_types.py
```

## Expected Flow

1. ✅ List returns 5 default bot types
2. ✅ Create adds new bot type
3. ✅ Get shows details with accounts_count = 0
4. ✅ Update changes risk_percent to 2.5
5. ✅ Delete soft deletes (is_active = false)

## Common Errors

### 401 Unauthorized
- Check TOKEN is valid
- Check REQUIRE_AUTH setting in .env

### 403 Forbidden
- User is not admin
- Check roles in user_profiles table

### 501 Not Implemented
- Repository không có methods
- Check Supabase connection

### 400 Bad Request
- Invalid payload
- Check required fields

## Debug Tips

1. Check server logs:
```bash
# Server output will show request details
```

2. Check Supabase data:
```sql
SELECT * FROM bot_types;
SELECT * FROM trading_accounts WHERE bot_type_id IS NOT NULL;
```

3. Test without auth (development):
```bash
# In .env
REQUIRE_AUTH=false

# Then test without Authorization header
curl http://127.0.0.1:8000/api/admin/bot-types
```

## Browser Testing

Visit in browser (requires admin login):
- http://127.0.0.1:8000/docs - Swagger UI
- Test endpoints interactively

## Next Steps After Testing

1. ✅ Verify all endpoints work
2. ✅ Check data persists in Supabase
3. ✅ Test account assignment
4. ✅ Test delete validation (can't hard delete when in use)
5. ✅ Test rate limiting (should get 429 after limit)
