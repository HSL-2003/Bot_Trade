# Bot Types UI - Quick User Guide

## Truy cập Bot Types Management

1. **Đăng nhập Admin Dashboard:**
   ```
   http://127.0.0.1:8000/admin
   ```
   
2. **Click "Bot Types" trong sidebar** (icon ⚙)

---

## Giao diện chính

### Table Columns

| Column | Mô tả |
|--------|-------|
| **Name** | Tên bot type + mô tả (nếu có) |
| **Risk Level** | LOW / MEDIUM / HIGH badge |
| **Risk %** | Phần trăm equity risk per trade |
| **Max Spread** | Spread tối đa cho phép (points) |
| **Max Daily Loss** | Circuit breaker % loss/ngày |
| **Max Trades** | Số lệnh mở tối đa cùng lúc |
| **Accounts** | Số accounts đang dùng (click để xem) |
| **Status** | ACTIVE / INACTIVE |
| **Actions** | Chi tiết / Sửa / Vô hiệu hoặc Xóa |

### Top Toolbar

- **[ + Tạo Bot Type ]** - Mở form tạo mới
- **☐ Hiện cả bot types đã vô hiệu hóa** - Checkbox filter

---

## Tạo Bot Type Mới

### Bước 1: Click "[ + Tạo Bot Type ]"

Modal mở ra với form có 2 cột.

### Bước 2: Điền thông tin

**Bắt buộc:**
- **Name:** Tên bot type (ví dụ: "Gold Scalper Pro")
- **Risk Level:** Chọn LOW / MEDIUM / HIGH

**Tùy chọn (có giá trị mặc định):**
- **Description:** Mô tả chi tiết
- **Default Symbol:** XAUUSD (hoặc EURUSD, USOIL, etc.)
- **Risk Percent:** 1.5% (phần trăm equity per trade)
- **Max Spread:** 200 points (từ chối trade nếu spread > limit)
- **Max Daily Loss:** 5.0% (circuit breaker)
- **Max Open Trades:** 5 (giới hạn lệnh đồng thời)
- **Trailing Stop Distance:** 100 points
- **Breakeven Trigger:** 200 points (khi nào move SL về breakeven)
- **Cooldown:** 15 minutes (thời gian chờ giữa các lệnh)

**Checkboxes:**
- ☑ Auto Trading
- ☑ Trailing Stop Enabled
- ☑ Breakeven Enabled

### Bước 3: Click "TẠO BOT TYPE"

✅ Thành công → Modal đóng, list refresh, hiện alert
❌ Lỗi → Hiện message màu đỏ trong modal

---

## Chỉnh sửa Bot Type

### Bước 1: Tìm bot type trong table

### Bước 2: Click button "Sửa"

Modal mở ra với tất cả fields đã điền sẵn.

### Bước 3: Thay đổi các fields cần thiết

Có thể sửa bất kỳ field nào, kể cả:
- ☑ **Active** checkbox (để enable/disable)

### Bước 4: Click "CẬP NHẬT"

✅ Thành công → Modal đóng, list refresh với data mới
❌ Lỗi → Hiện message trong modal

---

## Xem Chi tiết Bot Type

### Click button "Chi tiết" trên row

Modal hiển thị:

**Section 1: Description**
- Mô tả đầy đủ của bot type

**Section 2: Configuration Grid (2 cột)**
- Risk Level badge
- Default Symbol
- Risk Percent
- Max Spread
- Max Daily Loss
- Max Open Trades
- Trailing Stop Distance + status (✓/✗)
- Breakeven Trigger + status (✓/✗)
- Cooldown
- Auto Trading (✓ ON / ✗ OFF)
- Status badge (ACTIVE/INACTIVE)
- **Accounts Using** (số lớn)

**Section 3: Accounts List** (nếu có accounts đang dùng)
- Account ID - Display Name - Status badge

**Actions:**
- Button "SỬA" → Mở edit modal
- Button "ĐÓNG" → Đóng modal

---

## Vô hiệu hóa Bot Type (Soft Delete)

### Khi nào dùng:
- Không muốn xóa vĩnh viễn
- Có accounts đang sử dụng
- Muốn giữ lại để tham khảo sau

### Cách làm:

1. Tìm bot type **ACTIVE** trong table
2. Click button **"Vô hiệu"**
3. Confirm trong dialog
4. ✅ Bot type status → INACTIVE
5. Vẫn tồn tại trong database, chỉ bị ẩn khỏi list

### Để hiện lại:
- Check ☑ "Hiện cả bot types đã vô hiệu hóa"

### Để kích hoạt lại:
- Click "Sửa" → Check ☑ Active → "CẬP NHẬT"

---

## Xóa Bot Type Vĩnh viễn (Hard Delete)

### ⚠️ Cảnh báo:
- Xóa vĩnh viễn khỏi database
- Không thể khôi phục
- **KHÔNG được phép** nếu có accounts đang dùng

### Điều kiện:
1. Bot type phải **INACTIVE** trước
2. Không có account nào đang dùng

### Cách làm:

1. **Bước 1:** Vô hiệu hóa bot type (nếu chưa)
   - Click "Vô hiệu" → Confirm

2. **Bước 2:** Check ☑ "Hiện cả bot types đã vô hiệu hóa"

3. **Bước 3:** Tìm bot type INACTIVE

4. **Bước 4:** Click button **"Xóa"**

5. **Bước 5:** Confirm trong dialog

6. ✅ Bot type bị xóa vĩnh viễn

### Nếu có lỗi:
❌ "Cannot hard delete: X account(s) are using it"

→ Phải remove bot type từ tất cả accounts trước:
1. Click "Chi tiết" → Xem danh sách accounts
2. Vào từng account → Gán bot type khác
3. Quay lại xóa bot type

---

## Gán Bot Type cho Account

**Lưu ý:** Không làm trực tiếp từ Bot Types section.

### Cách làm:

1. Click sidebar **"Tài khoản"**
2. Tìm account cần gán
3. Click menu **"⋮"** → **"Đổi Bot Type"**
4. Chọn bot type từ dropdown
5. Confirm

**Hoặc:**

1. Click sidebar **"Tài khoản"**
2. Click vào account row để xem chi tiết
3. Section "Bot Type" → Click "Đổi"
4. Chọn bot type mới
5. Save

---

## Tips & Best Practices

### 1. Naming Convention
```
✅ Good:
- "Conservative Gold - Low Risk"
- "Aggressive Scalper - High Risk"
- "Forex Swing - EURUSD"

❌ Bad:
- "Bot 1"
- "Test"
- "asdfasdf"
```

### 2. Risk Level Guidelines

**LOW Risk:**
- Risk Percent: 0.5% - 1.5%
- Max Daily Loss: 2% - 4%
- Max Open Trades: 1 - 3
- Trailing Stop: 60-100 points
- Cooldown: 20-30 minutes

**MEDIUM Risk:**
- Risk Percent: 1.5% - 2.5%
- Max Daily Loss: 4% - 6%
- Max Open Trades: 3 - 7
- Trailing Stop: 100-150 points
- Cooldown: 10-20 minutes

**HIGH Risk:**
- Risk Percent: 2.5% - 5%
- Max Daily Loss: 6% - 15%
- Max Open Trades: 5 - 15
- Trailing Stop: 150-250 points
- Cooldown: 5-15 minutes

### 3. Testing New Bot Types

1. Tạo bot type mới với settings conservative
2. Test trên 1 account demo
3. Monitor performance 1-2 tuần
4. Adjust parameters dựa trên results
5. Deploy lên accounts production

### 4. Description Best Practices

Nên bao gồm:
- Target market (Gold, Oil, Forex)
- Trading style (Scalper, Swing, Day Trade)
- Risk profile (Conservative, Balanced, Aggressive)
- Optimal timeframes (M5, M15, H1, H4)
- Special conditions (trending markets, volatile sessions)

Example:
```
Gold Scalper - Medium Risk

Target: XAUUSD
Style: Scalping với quick exits
Timeframe: M5-M15
Session: London + NY overlap
Risk: Medium (1.5% per trade)
Max loss: 5% daily circuit breaker

Best for: Volatile market conditions
Avoid: Low volatility Asian session
```

### 5. Maintenance

**Hàng tuần:**
- Review performance của từng bot type
- Check accounts using each type
- Adjust parameters nếu cần

**Hàng tháng:**
- Archive inactive bot types (soft delete)
- Clean up unused templates
- Document changes in description

---

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| **ESC** | Đóng modal đang mở |
| **Tab** | Navigate giữa form fields |
| **Enter** | Submit form (khi focus trong input) |

---

## Common Issues & Solutions

### Issue 1: "Cannot create bot type"
**Causes:**
- Missing required fields (Name, Risk Level)
- Invalid number values (negative, zero)
- Network error

**Solutions:**
- Check all required fields
- Validate numbers > 0
- Check console for API errors

### Issue 2: "Cannot delete bot type"
**Error:** "X account(s) are using it"

**Solution:**
1. Click "Chi tiết" to see accounts
2. Remove bot type from all accounts first
3. Try delete again

### Issue 3: Bot type không hiện trong list
**Cause:** Bot type bị INACTIVE

**Solution:**
- Check ☑ "Hiện cả bot types đã vô hiệu hóa"

### Issue 4: Modal không mở
**Causes:**
- JavaScript error
- Network lag

**Solutions:**
1. Open browser console (F12)
2. Check for errors
3. Refresh page (Ctrl+R)
4. Clear cache if needed

### Issue 5: Changes không save
**Causes:**
- Network error
- Validation error
- Permission denied

**Solutions:**
1. Check error message trong modal
2. Verify admin permissions
3. Check network tab trong DevTools
4. Retry sau vài giây

---

## Support & Feedback

**Bugs?**
- Check browser console
- Note error message
- Document steps to reproduce

**Feature requests?**
- Bulk operations
- Export/import
- Version history
- Performance analytics
- Bot type templates gallery

**Questions?**
- Check docs/API_BOT_TYPES.md for API details
- Check docs/UI_BOT_TYPES.md for technical info
