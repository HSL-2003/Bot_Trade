# PROJECT RULES & MEMORY (Spec-Kit, Understand-Anything, Ponytail)

## 1. Spec-Kit (Spec-Driven Development Workflow)
- **Spec-First approach**: Always analyze specifications, plan code structure, verify imports and references before modifying files.
- **Systematic Verification**: Always perform static analysis and AST/import verification for modified files.
- **Architectural Scoping**: Keep core backend (`bot.py`), gateway (`app.py`), and agents (`agents/`) clean, modular, and un-bloated.

## 2. Understand-Anything Plugin Rules
- **Repository Isolation**: Do NOT alter, overwrite, or corrupt code inside `.understand-anything-repo/`.
- **Knowledge Graph Path**: Knowledge graph JSON data resides in analyzed project's `.ua/` (or `.understand-anything/`) directory.
- **Browser-Safe Exports**: Maintain core browser-safe subpath exports (`./search`, `./types`, `./schema`).

## 3. Ponytail Custom Trading Rules & Overrides (DO NOT ALTER OR BREAK)
- **Risk-to-Reward Ratio**: R:R = 1:3 (`SL = 500 points` / $5.0 USD on Gold, `TP = 1500 points` / $15.0 USD on Gold).
- **Trailing Stop**: `trailing_stop_points = 500`, `trailing_step_points = 500`, `trailing_stop_offset_points = 1000`.
- **Breakeven**: `breakeven_trigger_points = 500`, `breakeven_buffer_points = 0`.
- **Wave & Trend Trading**: 2-way wave trading enabled (Trend filter relaxed/disabled).
- **Trigger Window**: Confluence zone trigger window expanded to `300 points` ($3.0 USD on Gold).
- **RSI Thresholds**: Relaxed RSI thresholds (`BUY` < 42, `SELL` > 58). Entry allowed from 1-star setups.
- **Order Stacking (Nhồi lệnh)**: Stacking enabled without minimum distance restriction.
- **Circuit Breaker**: Automatic lockdown disabled by default per user request (`system_locked = False`).
- **Indicator Hierarchy**: TradingView indicators have primary precedence; Binance & MT5 calculations are fallbacks.
- **Spread Tolerance**: Higher spread tolerance (`100 points`) for Gold, Oil, and Commodities.
- **UI Refresh Rate**: 50ms WebSocket stream interval (20 FPS UI refresh rate) in `app.py`.
- **Flexible Lot Sizing**: Dynamic recovery lot sizing for small accounts (~$50 - $200 USD).
