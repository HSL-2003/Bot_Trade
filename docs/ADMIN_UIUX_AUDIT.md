# Admin Portal — Tài liệu hiện trạng & phương án UI/UX

> Phạm vi: `/admin` (dashboard). **Không đụng** `/admin/login`.  
> Nguồn guideline: [ui-ux-pro-max-skill](https://github.com/nextlevelbuilder/ui-ux-pro-max-skill.git) + [taste-skill](https://github.com/Leonxlnx/taste-skill.git)  
> Design system đã persist: `design-system/loopstack-admin/`

**Design read:** Operations admin dashboard cho operator trading bot; vibe tối giản / chuyên nghiệp / data-dense; tối ưu scannability hơn “luxury neon”.

**Dials (taste + pro-max):** Variance `4` · Motion `3` · Density `8`

---

## 1. Bản đồ hệ thống `/admin`

### 1.1 Routes trang

| Route | Template | Ghi chú |
|-------|----------|---------|
| `GET /admin` | `templates/admin_dashboard.html` | SPA-like 1 file (~3.8k dòng): CSS + HTML + JS |
| `GET /admin/login` | `templates/admin_login.html` | **Ngoài phạm vi redesign** |

Auth guard client: JWT admin trong `localStorage` / cookie; thiếu token → redirect `/admin/login`.

### 1.2 Cấu trúc layout hiện tại

```
┌─ top-nav (fixed 64px) ─────────────────────────────────────────┐
│ Loopstack · Emergency Close All · OPERATOR · Refresh · Sign Out │
├─ sidebar 240px ─┬─ workspace ──────────────────────────────────┤
│ Overview        │ tab-pane active                               │
│ Accounts & P&L  │  pane-header + KPI / tables / cards           │
│ User Directory  │                                               │
│ Bot Fleet Studio│                                               │
│ Trades Audit    │                                               │
│ Risk Controls   │                                               │
└─────────────────┴───────────────────────────────────────────────┘
+ Account Inspector drawer (slide-over)
+ Modals: Account / BotType / CreateUser / EditUser / Emergency
+ Toast
```

### 1.3 6 tab chức năng

| Tab `data-tab` | Pane ID | Việc chính | API chính |
|----------------|---------|------------|-----------|
| `overview` | `#pane-overview` | KPI fleet, equity chart SVG, recent trades | `GET /api/admin/overview` |
| `accounts` | `#pane-accounts` | Bảng TK, filter, tạo TK, mở drawer | `GET /api/admin/accounts` |
| `users` | `#pane-users` | CRUD user, bulk delete, pagination | `/api/admin/users` |
| `bot-types` | `#pane-bot-types` | 3 strategy cards, allocation matrix, blueprints CRUD | `/api/admin/bot-types`, `/bot-fleet/breakdown` |
| `trades` | `#pane-trades` | Ledger toàn fleet + filter + pagination | `GET /api/admin/trades` |
| `risk` | `#pane-risk` | Circuit breaker (phần lớn hardcode UI) + halt | emergency + halt endpoints |

### 1.4 Overlay / modal / drawer

| UI | ID | Hành vi |
|----|-----|---------|
| Account drawer | `#drawerBackdrop` / `#accountDrawer` | Positions · Daily · Closed trades; assign bot; Deploy/Halt/Lock/Revoke |
| Create account | `#accountModal` | ID, owner, bot type, status |
| Create bot type | `#botTypeModal` | name, strategy, risk, JSON config |
| Create user | `#createUserModal` | email, password, role trader\|admin, bot type |
| Edit user | `#editUserModal` | name, password, role, status |
| Emergency close all | `#emergencyModal` | close all positions ± halt bots + reason |
| Toast | `#adminToast` | feedback ngắn |

### 1.5 API admin (backend `app.py`)

**Đọc**

- `GET /api/admin/overview` — KPI + chart series + accounts section
- `GET /api/admin/accounts` · `GET .../{id}` · positions · daily-performance
- `GET /api/admin/users`
- `GET /api/admin/trades`
- `GET /api/admin/bot-types` · `GET .../{id}`
- `GET /api/admin/bot-fleet/breakdown`

**Ghi / điều khiển**

- Account: `bot-type`, `status`, `lock`, `deploy`, `halt`, `revoke-sessions`
- Positions: `close` ticket · `close-all`
- Users: `POST` · `PATCH` · `DELETE` · `PUT .../roles`
- Bot types: `POST` · `PUT` · `PATCH` · `DELETE`
- Fleet: `POST /api/admin/fleet/emergency-close-all`

Tất cả yêu cầu `require_admin`.

### 1.6 Design token hiện tại (trong file)

| Token | Giá trị | Vấn đề |
|-------|---------|--------|
| `--bg-black` | `#050508` | Gần pure black (taste: tránh `#000`) |
| `--accent-neon` | `#39FF14` | Neon bão hòa cao, glow mạnh — “AI dark tech slop” |
| `--accent-red` | `#FF334B` | OK semantic, nhưng glow quá nhiều |
| `--accent-indigo` | `#6366F1` | Accent thứ 3 (badge admin) — phá one-accent rule |
| Fonts | Playfair + Outfit + JetBrains | Serif display không hợp ops dashboard tối giản |
| Effect | glass + noise + ambient neon radial + pulse glow | Motion/độ phức tạp cao hơn dial Motion=3 |

---

## 2. Audit UI/UX (theo skill)

### 2.1 Chẩn đoán nhanh (redesign-existing + design-taste)

| # | Phát hiện | Mức | Skill rule |
|---|-----------|-----|------------|
| A1 | Neon `#39FF14` + glow/text-shadow khắp nơi | Cao | taste: saturation &lt; 80%; 1 accent; no neon default |
| A2 | Playfair Display cho mọi `h1` pane | Cao | taste: serif discouraged cho product UI; pro-max: dashboard → sans/mono |
| A3 | Copy tiếng Anh marketing dài (“Executive Command Center”, “Firm-wide…”) lẫn VI | Trung | redesign: plain specific language; i18n nhất quán |
| A4 | File monolit ~3800 dòng, nhiều `style=""` inline | Trung | redesign: move to system; maintainability |
| A5 | Emergency hành động xuất hiện 3 chỗ (top-nav, overview, risk) | Trung | navigation clarity; progressive disclosure |
| A6 | Tab Risk phần lớn static / hardcode | Trung | empty vs live data honesty (pro-max live telemetry) |
| A7 | Touch target nhỏ (btn 4–6px padding), font base 13.5px | Trung | pro-max: 44px touch, base ~16 where possible (desktop dense OK nhưng icon-btn cần lớn hơn) |
| A8 | Focus ring yếu / thiếu trên nhiều control | Cao | a11y priority 1 |
| A9 | Không `prefers-reduced-motion` | Trung | motion dial + a11y |
| A10 | Bảng rộng 8–10 cột, mobile chỉ scroll ngang | Trung | pro-max table handling OK; cần card fallback |
| A11 | Mixed VI/EN trong cùng panel (“Xoa hanh loat” / “Huy chon” thiếu dấu) | Thấp | polish copy |
| A12 | Pulse-dot + emergency glow = distraction cho ops | Trung | motion subtle only |
| A13 | Badge indigo cho admin = accent thứ 2–3 | Thấp | one accent lock |
| A14 | Không deep-link tab (`#accounts`) | Trung | pro-max navigation |
| A15 | Skeleton loading thiếu; chỉ text “Loading…” | Thấp | redesign loading states |

### 2.2 Điểm đang tốt (giữ)

- Sidebar + tab rõ; active state có phân biệt
- Drawer inspector đúng pattern (slide-over thay modal cho detail)
- Bulk bar + pagination cho Users / Bot types / Trades
- Mono tabular numbers cho P&amp;L
- Semantic status (green/amber/red) cho bot state
- Emergency confirm có reason + checkbox halt

---

## 3. Phương án đề xuất (thống nhất màu · chuyên nghiệp · tối giản)

### 3.1 Hướng thiết kế chốt

**Giữ:** dark ops dashboard, sidebar, drawer, density cao.  
**Bỏ / giảm:** neon glow, serif luxury, ambient green wash, pulse animation, glass blur nặng.  
**Thêm:** token slate trung tính + 1 accent xanh muted; typography sans+mono; focus/a11y; copy VI thống nhất.

### 3.2 Palette đề xuất (lock một hệ)

Lấy từ pro-max “Dark tech + status green”, desaturate theo taste:

```css
:root {
  /* Surfaces — cool slate family (một họ xám) */
  --bg:            #0F172A;
  --bg-elevated:   #1B2336;
  --bg-hover:      #222B3D;
  --border:        #334155;
  --border-subtle: rgba(148, 163, 184, 0.12);

  /* Text */
  --text:          #F8FAFC;
  --text-secondary:#94A3B8;
  --text-muted:    #64748B;

  /* ONE brand accent (CTA / active nav) — muted, không neon */
  --accent:        #22C55E;
  --accent-muted:  rgba(34, 197, 94, 0.12);
  --accent-on:     #0F172A;

  /* Semantic only — không dùng làm brand */
  --ok:            #22C55E;
  --warn:          #D97706;   /* amber desaturated */
  --danger:        #EF4444;
  --info:          #64748B;   /* thay indigo cho role badge */

  /* Type */
  --font-sans: 'Outfit', 'Segoe UI', sans-serif;
  --font-mono: 'JetBrains Mono', ui-monospace, monospace;
  /* BỎ --font-display / Playfair */
}
```

**Quy tắc màu**

1. Brand accent chỉ dùng: primary button, active nav, focus ring, positive P&amp;L.
2. Danger chỉ cho destructive / emergency.
3. Không `box-shadow` glow màu; shadow tối đa `0 1px 0 rgba(0,0,0,.4)` hoặc none.
4. Border `1px solid var(--border)` — flat, không glass nặng.

### 3.3 Typography

| Role | Font | Size / weight |
|------|------|----------------|
| Page title | Outfit 600 | 22–24px, tracking -0.02em |
| Section title | Outfit 500 | 16–18px |
| Body / table | Outfit 400 | 13–14px (desktop dense) |
| Labels / th | JetBrains Mono 500 | 10–11px uppercase tracking |
| Numbers | JetBrains Mono + `tabular-nums` | — |

Bỏ Playfair trên mọi heading admin.

### 3.4 Layout & IA cải tiến

| Hạng mục | Hiện tại | Đề xuất |
|----------|----------|---------|
| Nav labels | EN dài | VI ngắn: Tổng quan · Tài khoản · Người dùng · Loại bot · Lệnh · Rủi ro |
| Emergency | 3 chỗ | Chỉ **1** nút sticky trên top-nav; Risk tab mô tả + link “Mở xác nhận” |
| Overview KPI | 5 card ngang | Giữ 5; bỏ neon text-shadow; accent-top = 2px solid border-top muted |
| Bot Fleet | 3 card risk màu mạnh | Flat card + badge pastel muted (pale green/amber/red bg) |
| Risk tab | Static metrics | Phase 2: bind API thật hoặc đánh dấu “Cấu hình tĩnh” rõ ràng |
| Deep link | Không | `?tab=accounts` hoặc `#accounts` sync với `switchToTab` |
| Workspace | Full bleed | `max-width: 1440px` optional trên ultrawide |

### 3.5 Component rules (minimal ops)

- **Card / panel:** nền `--bg-elevated`, border 1px, radius 8–10px, **không** backdrop-blur.
- **Button primary:** bg `--accent`, text `--accent-on`, radius 6px, hover darken 6%.
- **Button ghost:** border + transparent.
- **Button danger:** outline danger hoặc solid danger chỉ trong confirm modal.
- **Badge:** radius 4–6px (không pill lớn), pastel bg + chữ semantic.
- **Table:** sticky header, zebra cực nhẹ, row hover `--bg-hover`; action column sticky phải nếu cần.
- **Modal:** solid elevated surface; ESC + focus trap.
- **Drawer:** width 480–560px; tabs underline thay pill glow.
- **Motion:** 150–220ms opacity/transform only; tắt pulse-dot; tôn trọng `prefers-reduced-motion`.

### 3.6 Copy & ngôn ngữ

- Một ngôn ngữ UI: **Tiếng Việt** (giữ thuật ngữ kỹ thuật: P&amp;L, Deploy, Halt nếu team quen).
- Sửa lỗi chính tả: “Hủy chọn”, “Xóa hàng loạt”.
- Bỏ từ marketing: Executive, Firm-wide, Luxury, Telemetry stream…
- Toast: câu ngắn, không `!`.

### 3.7 A11y / UX bắt buộc (pro-max P1–P2)

- [ ] Focus visible `outline: 2px solid var(--accent); outline-offset: 2px`
- [ ] `aria-label` cho icon-only / emergency
- [ ] Confirm destructive: focus vào Cancel mặc định
- [ ] Loading skeleton khớp shape bảng/KPI
- [ ] Empty state có CTA (“Tạo tài khoản đầu tiên”)
- [ ] `prefers-reduced-motion: reduce`
- [ ] Mobile: sidebar → bottom hoặc top horizontal scroll tabs

---

## 4. Roadmap triển khai (không đụng login)

### Phase 0 — Token & foundation (1 PR nhỏ, rủi ro thấp)

1. Thay `:root` tokens theo §3.2 trong `admin_dashboard.html` only.
2. Gỡ Playfair link; map headings → Outfit.
3. Tắt ambient neon radial, pulse animation, text-shadow neon.
4. Chuẩn hóa button/badge/table theo token mới.

### Phase 1 — Copy + IA

1. Đổi label nav + tiêu đề pane sang VI ngắn.
2. Gom Emergency về 1 điểm vào.
3. Hash/query tab deep-link.
4. Sửa chính tả bulk actions.

### Phase 2 — States & a11y

1. Focus ring, reduced-motion, aria.
2. Skeleton / empty states.
3. Mobile nav collapse.

### Phase 3 (optional) — Cấu trúc code

1. Tách `static/admin/admin.css` + `admin.js` khỏi HTML (giữ template render).
2. Risk metrics nối API thật nếu có.

**Không làm trong redesign UI:** đổi auth, đổi contract API, đụng `admin_login.html`.

---

## 5. Checklist trước khi ship UI

Từ ui-ux-pro-max + taste:

- [ ] Không emoji làm icon
- [ ] Một accent brand; semantic colors chỉ cho status
- [ ] Không neon glow / purple AI gradient
- [ ] Không serif display trên admin
- [ ] Hover 150–300ms; cursor-pointer
- [ ] Contrast text ≥ 4.5:1 trên dark
- [ ] Focus keyboard rõ
- [ ] `prefers-reduced-motion`
- [ ] Responsive 375 / 768 / 1024 / 1440
- [ ] Login admin không bị sửa

---

## 6. File liên quan

| File | Vai trò |
|------|---------|
| `templates/admin_dashboard.html` | UI + logic admin (đối tượng redesign) |
| `templates/admin_login.html` | **Không chỉnh** |
| `app.py` | Routes `/admin*` + `/api/admin/*` |
| `design-system/loopstack-admin/MASTER.md` | Source of truth màu/type từ skill |
| `design-system/loopstack-admin/pages/admin-dashboard.md` | Override trang dashboard |

---

## 7. Trạng thái triển khai

**Đã làm trên `templates/admin_dashboard.html` (không đụng login):**

- Phase 0: token slate + accent `#22C55E`, bỏ Playfair/neon/glow/ambient/noise
- Phase 1: nav & tiêu đề VI, Emergency 1 entry (top-nav + link từ Risk), deep-link `?tab=`
- Phase 2: `:focus-visible`, `prefers-reduced-motion`, aria-label cơ bản

Xem lại UI tại `/admin` sau khi đăng nhập.
