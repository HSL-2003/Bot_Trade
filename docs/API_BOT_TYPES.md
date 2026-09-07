# Bot Types Management API

API để quản lý các loại bot (bot templates) với cấu hình khác nhau.

## Endpoints

### 1. List Bot Types
**GET** `/api/admin/bot-types`

Lấy danh sách tất cả các loại bot.

**Query Parameters:**
- `include_inactive` (boolean, optional): Bao gồm cả bot types đã vô hiệu hóa. Mặc định: `false`

**Response:**
```json
{
  "total": 5,
  "bot_types": [
    {
      "id": "uuid",
      "name": "Conservative Gold",
      "description": "Low risk gold trading with tight controls",
      "risk_level": "low",
      "default_symbol": "XAUUSD",
      "risk_percent": 1.0,
      "max_spread": 150,
      "max_daily_loss_percent": 3.0,
      "auto_trading": true,
      "trailing_stop_enabled": true,
      "trailing_stop_distance": 80,
      "breakeven_enabled": true,
      "breakeven_trigger": 150,
      "cooldown_minutes": 20,
      "max_open_trades": 3,
      "is_active": true,
      "metadata": {},
      "created_at": "2026-09-04T14:00:00Z",
      "updated_at": "2026-09-04T14:00:00Z"
    }
  ]
}
```

### 2. Get Bot Type Details
**GET** `/api/admin/bot-types/{bot_type_id}`

Lấy thông tin chi tiết một loại bot và danh sách accounts đang sử dụng.

**Response:**
```json
{
  "id": "uuid",
  "name": "Standard Gold",
  "description": "Balanced gold trading with moderate risk",
  "risk_level": "medium",
  "risk_percent": 1.5,
  "max_spread": 200,
  "max_daily_loss_percent": 5.0,
  "auto_trading": true,
  "trailing_stop_enabled": true,
  "trailing_stop_distance": 100,
  "breakeven_enabled": true,
  "breakeven_trigger": 200,
  "cooldown_minutes": 15,
  "max_open_trades": 5,
  "is_active": true,
  "metadata": {},
  "accounts_count": 12,
  "accounts_using": [
    {
      "id": "account-1",
      "display_name": "User Account 1",
      "status": "active"
    }
  ],
  "created_at": "2026-09-04T14:00:00Z",
  "updated_at": "2026-09-04T14:00:00Z"
}
```

### 3. Create Bot Type
**POST** `/api/admin/bot-types`

Tạo loại bot mới.

**Request Body:**
```json
{
  "name": "Custom Strategy",
  "description": "Custom trading strategy for specific market conditions",
  "risk_level": "medium",
  "default_symbol": "XAUUSD",
  "risk_percent": 2.0,
  "max_spread": 200,
  "max_daily_loss_percent": 5.0,
  "auto_trading": true,
  "trailing_stop_enabled": true,
  "trailing_stop_distance": 100,
  "breakeven_enabled": true,
  "breakeven_trigger": 200,
  "cooldown_minutes": 15,
  "max_open_trades": 5,
  "is_active": true,
  "metadata": {
    "created_by": "admin",
    "tags": ["gold", "conservative"]
  }
}
```

**Response:**
```json
{
  "status": "created",
  "bot_type": {
    "id": "new-uuid",
    "name": "Custom Strategy",
    ...
  }
}
```

### 4. Update Bot Type
**PUT** `/api/admin/bot-types/{bot_type_id}`

Cập nhật thông tin loại bot. Chỉ cần gửi các fields muốn thay đổi.

**Request Body:**
```json
{
  "name": "Updated Strategy Name",
  "risk_percent": 2.5,
  "max_spread": 250,
  "is_active": true
}
```

**Response:**
```json
{
  "status": "updated",
  "bot_type": {
    "id": "uuid",
    "name": "Updated Strategy Name",
    ...
  }
}
```

### 5. Partial Update Bot Type
**PATCH** `/api/admin/bot-types/{bot_type_id}`

Giống như PUT, cập nhật một phần thông tin loại bot.

### 6. Delete Bot Type
**DELETE** `/api/admin/bot-types/{bot_type_id}`

Xóa loại bot (soft delete mặc định).

**Query Parameters:**
- `hard_delete` (boolean, optional): Hard delete (xóa vĩnh viễn). Mặc định: `false`

**Response:**
```json
{
  "status": "deleted",
  "bot_type_id": "uuid",
  "delete_type": "deactivated"
}
```

**Note:** Không thể hard delete bot type nếu có accounts đang sử dụng.

### 7. Assign Bot Type to Account
**POST** `/api/admin/accounts/{account_id}/bot-type`

Gán loại bot cho một account.

**Request Body:**
```json
{
  "bot_type_id": "uuid"
}
```

**Response:**
```json
{
  "status": "updated",
  "account_id": "account-1",
  "bot_type_id": "uuid"
}
```

## Risk Levels

Bot types có 3 mức độ rủi ro:

- **low**: Ít rủi ro, phù hợp với trader mới hoặc conservative
  - Risk percent: 0.5% - 1.5%
  - Max daily loss: 2% - 4%
  - Max open trades: 1 - 3

- **medium**: Cân bằng giữa rủi ro và lợi nhuận
  - Risk percent: 1.5% - 2.5%
  - Max daily loss: 4% - 6%
  - Max open trades: 3 - 7

- **high**: Rủi ro cao, cho trader có kinh nghiệm
  - Risk percent: 2.5% - 5%
  - Max daily loss: 6% - 15%
  - Max open trades: 5 - 15

## Safety Controls

Mỗi bot type có các control safety:

1. **Trailing Stop**: Tự động di chuyển stop loss theo lợi nhuận
   - `trailing_stop_enabled`: Bật/tắt
   - `trailing_stop_distance`: Khoảng cách (points)

2. **Breakeven**: Chuyển stop loss về điểm hòa vốn
   - `breakeven_enabled`: Bật/tắt
   - `breakeven_trigger`: Kích hoạt khi profit đạt (points)

3. **Cooldown**: Thời gian nghỉ giữa các lệnh
   - `cooldown_minutes`: Số phút chờ

4. **Max Open Trades**: Giới hạn số lệnh mở cùng lúc
   - `max_open_trades`: Số lệnh tối đa

5. **Max Daily Loss**: Circuit breaker tự động
   - `max_daily_loss_percent`: Tỷ lệ % loss/equity tối đa/ngày

## Authentication

Tất cả endpoints yêu cầu admin role:

```
Authorization: Bearer <admin_token>
```

## Rate Limiting

- List/Get: 60 requests/minute
- Create/Update/Delete: 20 requests/minute

## Error Codes

- `400` - Bad request (invalid parameters)
- `401` - Unauthorized (missing or invalid token)
- `403` - Forbidden (not admin)
- `404` - Bot type not found
- `501` - Repository không hỗ trợ bot types

## Examples

### Tạo bot type mới với curl:

```bash
curl -X POST http://127.0.0.1:8000/api/admin/bot-types \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
  -d '{
    "name": "My Custom Bot",
    "description": "Custom strategy",
    "risk_level": "medium",
    "risk_percent": 2.0,
    "max_spread": 200,
    "max_daily_loss_percent": 5.0
  }'
```

### Lấy danh sách bot types với Python:

```python
import requests

url = "http://127.0.0.1:8000/api/admin/bot-types"
headers = {"Authorization": "Bearer YOUR_ADMIN_TOKEN"}

response = requests.get(url, headers=headers)
bot_types = response.json()

print(f"Total bot types: {bot_types['total']}")
for bt in bot_types['bot_types']:
    print(f"- {bt['name']} ({bt['risk_level']})")
```

### Gán bot type cho account:

```bash
curl -X POST http://127.0.0.1:8000/api/admin/accounts/account-123/bot-type \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
  -d '{"bot_type_id": "bot-type-uuid"}'
```

### Cập nhật bot type:

```bash
curl -X PUT http://127.0.0.1:8000/api/admin/bot-types/bot-type-uuid \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
  -d '{
    "risk_percent": 2.5,
    "max_daily_loss_percent": 6.0
  }'
```

### Xóa bot type (soft delete):

```bash
curl -X DELETE http://127.0.0.1:8000/api/admin/bot-types/bot-type-uuid \
  -H "Authorization: Bearer YOUR_ADMIN_TOKEN"
```
