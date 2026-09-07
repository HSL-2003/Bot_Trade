# Admin Dashboard Page Overrides

> **PROJECT:** Loopstack Admin  
> Overrides `MASTER.md` cho `templates/admin_dashboard.html` only.  
> **Không áp dụng** cho `admin_login.html`.

---

## Scope

- In: `/admin` dashboard UI/UX
- Out: `/admin/login`, API contracts, auth

## Layout

- Keep: top-nav + left sidebar + workspace tabs + account drawer
- Max content width optional: `1440px` on ultrawide
- Emergency Close All: **một** entry point (top-nav only)
- Deep-link tabs: `?tab=` hoặc `#` sync với `switchToTab`

## Color (override)

- Accent brand: `#22C55E` only (no `#39FF14` neon)
- Surfaces: `#0F172A` / `#1B2336` / border `#334155`
- Semantic: warn `#D97706`, danger `#EF4444`, role badge = muted gray (no indigo accent)
- No colored glow shadows; flat borders

## Typography (override)

- All headings → Outfit; numbers/labels → JetBrains Mono
- Remove Playfair Display from this page

## Components

- Panels: solid elevated fill, 1px border, radius ≤10px, **no heavy glass blur**
- Tables: sticky header, hover row, horizontal scroll + card fallback ≤768px
- Motion: ≤220ms; disable pulse-dot; honor `prefers-reduced-motion`
- Copy UI: tiếng Việt thống nhất; sửa “Hủy chọn” / “Xóa hàng loạt”

## Checklist

Xem `docs/ADMIN_UIUX_AUDIT.md` §5.
