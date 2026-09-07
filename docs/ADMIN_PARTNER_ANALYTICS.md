# Admin Partner / Doanh thu (Exness-style)

Tab **Doanh thu** trên `/admin` hiển thị KPI + biểu đồ cột/đường cho quản lý user & tiền, mô phỏng báo cáo Partner IB của Exness.

## Chỉ số

| KPI | Nguồn |
|-----|--------|
| User hôm nay / 7 ngày / tổng | `user_profiles.created_at` |
| P&L bot | Tổng `profit` lệnh `closed` |
| Volume lot | Tổng `quantity` lệnh closed |
| Hoa hồng (commission) | `lot × PARTNER_COMMISSION_PER_LOT` |
| Backcom (rebate) | `lot × PARTNER_BACKCOM_PER_LOT` |
| Phí platform | `% PARTNER_PLATFORM_FEE_PCT` trên P&L bot **dương** mỗi lệnh |
| Thu nhập ròng IB | Commission − Backcom |
| Thu nhập ròng platform | IB net + phí platform |

## Biểu đồ (30 ngày)

1. Cột — user đăng ký / ngày  
2. Cột ghép — commission vs backcom  
3. Cột — volume lot / ngày  
4. Đường lũy kế — commission, backcom, bot P&L  

## API

- `GET /api/admin/overview` → thêm field `partner`
- `GET /api/admin/partner-analytics` → payload partner riêng

## Cấu hình `.env`

```
PARTNER_COMMISSION_PER_LOT=5
PARTNER_BACKCOM_PER_LOT=1.5
PARTNER_PLATFORM_FEE_PCT=10
```

Đây là **ước tính revenue-share theo lot** (giống tinh thần Exness IB), chưa gắn ledger thanh toán thật. Khi có bảng commission thực tế có thể thay công thức trong `_partner_analytics`.
