"""Pure position-sizing policy, isolated for testing and maintenance."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN


class RiskCalculationError(ValueError):
    """Raised when a position cannot be sized safely."""


@dataclass(frozen=True)
class InstrumentSpec:
    tick_size: float
    tick_value: float
    volume_min: float = 0.01
    volume_max: float = 100.0
    volume_step: float = 0.01


def calculate_volume(*, equity: float, risk_percent: float,
                     entry_price: float, stop_loss: float,
                     instrument: InstrumentSpec) -> float:
    """Calculate volume from money risk; no recovery or martingale policy."""
    if equity <= 0 or not 0 < risk_percent <= 100:
        raise RiskCalculationError("Equity and risk percentage are invalid")
    distance = abs(entry_price - stop_loss)
    if distance <= 0 or instrument.tick_size <= 0 or instrument.tick_value <= 0:
        raise RiskCalculationError("A valid stop and tick metadata are required")
    if instrument.volume_step <= 0:
        raise RiskCalculationError("Volume step must be positive")
    risk_money = equity * risk_percent / 100.0
    loss_per_lot = distance / instrument.tick_size * instrument.tick_value
    raw_volume = risk_money / loss_per_lot
    units = (Decimal(str(raw_volume)) / Decimal(str(instrument.volume_step))).quantize(
        Decimal("1"), rounding=ROUND_DOWN
    )
    volume = min(instrument.volume_max, float(units * Decimal(str(instrument.volume_step))))
    if volume < instrument.volume_min:
        raise RiskCalculationError("Calculated volume is below broker minimum")
    return volume