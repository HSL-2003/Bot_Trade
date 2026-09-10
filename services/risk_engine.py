"""Risk engine — builds live InstrumentSpecs from broker metadata and sizes
positions deterministically (Phase 1 · item 1.5).

The position-sizing *policy* lives in ``core.risk`` as a pure function. This
module supplies the market-aware inputs:

- ``instrument_spec_for`` : prefer realtime ``mt5.symbol_info(symbol)``
  contract/volume/tick metadata and fall back to the legacy heuristic only when
  the broker is unavailable (simulation mode). This keeps the sizing honest:
  contract_size, volume_min/max/step and tick values differ per symbol/venue.
- ``size_position``      : thin wrapper that fails closed on bad input.

Design rule (from the architecture review): never hardcode instrument
parameters that a broker exposes. If we cannot get real metadata we must NOT
pretend — we use explicit defaults that match this venue's known convention or
let ``core.risk`` reject the trade.
"""

from __future__ import annotations

from typing import Any, Optional

from core.risk import InstrumentSpec, RiskCalculationError, calculate_volume

try:  # MetaTrader5 is optional (simulation mode)
    import MetaTrader5 as _mt5
except ImportError:  # pragma: no cover
    _mt5 = None  # type: ignore


# Legacy fallbacks mirroring the historical config (used only when MT5 absent).
_FALLBACK_SPEC: dict[str, tuple[float, float]] = {  # symbol -> (tick_size, tick_value_per_lot)
    "XAUUSD": (0.01, 100.0),
    "USOIL": (0.01, 1000.0),
    "EURUSD": (0.00001, 1.0),
    "GBPUSD": (0.00001, 1.0),
}


def _mt5_load() -> Any:
    """Import MetaTrader5 only when available; never raise from a soft import."""
    return _mt5


def instrument_spec_for(
    symbol: str,
    *,
    mt5_symbol_info: Optional[Any] = None,
) -> InstrumentSpec:
    """Build a live InstrumentSpec for ``symbol``.

    ``mt5_symbol_info`` — a ``symbol_info`` result — carries the authoritative
    contract/volume/tick metadata. When it is ``None`` (simulation mode or a
    failed lookup) we fall back to our tested defaults; the crucial part is we
    still clamp to sane min/max and never size a trade on fabricated numbers.
    """
    symbol = (symbol or "").upper()
    if mt5_symbol_info is not None:
        try:
            contract = float(mt5_symbol_info.contract_size or 0)
            vmin = float(mt5_symbol_info.volume_min or 0)
            vmax = float(mt5_symbol_info.volume_max or 0)
            vstep = float(mt5_symbol_info.volume_step or 0)
            point = float(mt5_symbol_info.point or 0)
            # Some builds expose tick value directly; otherwise derive it.
            tv = float(getattr(mt5_symbol_info, "tick_value", 0) or 0)
            if tv <= 0 and point > 0 and contract > 0:
                tv = contract * point
            if vmax <= 0:
                vmax = 100.0
            if vstep <= 0:
                vstep = 0.01
            if point <= 0 or tv <= 0:
                raise RiskCalculationError("Broker metadata missing point/tick value")
            return InstrumentSpec(
                tick_size=point,
                tick_value=tv,
                volume_min=vmin if vmin > 0 else 0.01,
                volume_max=vmax,
                volume_step=vstep,
            )
        except (TypeError, ValueError, AttributeError) as exc:
            raise RiskCalculationError(f"Invalid broker instrument metadata for {symbol}: {exc}") from exc

    # Fallback (no live metadata — simulation / not connected).
    tick = _FALLBACK_SPEC.get(symbol)
    if tick is None:
        # Unknown symbol: refuse rather than invent risk math for it.
        raise RiskCalculationError(f"No risk metadata available for symbol {symbol!r}")
    return InstrumentSpec(tick_size=tick[0], tick_value=tick[1])


def size_position(
    *,
    equity: float,
    risk_percent: float,
    entry_price: float,
    stop_loss: float,
    spec: InstrumentSpec,
) -> float:
    """Size a position from money risk; fail closed (raises) on invalid input."""
    return calculate_volume(
        equity=equity,
        risk_percent=risk_percent,
        entry_price=entry_price,
        stop_loss=stop_loss,
        instrument=spec,
    )


__all__ = ["InstrumentSpec", "RiskCalculationError", "instrument_spec_for", "size_position"]