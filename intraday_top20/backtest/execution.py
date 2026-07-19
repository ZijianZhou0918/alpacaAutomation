from __future__ import annotations

import math
from dataclasses import dataclass

from .config import ExecutionConfig


@dataclass(frozen=True)
class FillEstimate:
    reference_price: float
    fill_price: float
    slippage_bps: float
    slippage_cost: float
    commission: float


class CostModel:
    def __init__(self, config: ExecutionConfig):
        self.config = config

    def slippage_bps(self, reference_price: float, high: float, low: float) -> float:
        bps = self.config.base_slippage_bps + self.config.assumed_spread_bps / 2.0
        if reference_price < self.config.low_price_threshold:
            bps += self.config.low_price_extra_slippage_bps
        range_pct = (high - low) / reference_price if reference_price > 0 else 0.0
        if range_pct >= self.config.high_volatility_range_pct:
            bps += self.config.high_volatility_extra_slippage_bps
        return bps

    def commission(self, quantity: float) -> float:
        return max(self.config.minimum_commission, quantity * self.config.commission_per_share)

    def buy(self, reference_price: float, quantity: float, high: float, low: float) -> FillEstimate:
        bps = self.slippage_bps(reference_price, high, low)
        fill = reference_price * (1.0 + bps / 10_000.0)
        return FillEstimate(reference_price, fill, bps, (fill - reference_price) * quantity, self.commission(quantity))

    def sell(self, reference_price: float, quantity: float, high: float, low: float) -> FillEstimate:
        bps = self.slippage_bps(reference_price, high, low)
        fill = reference_price * (1.0 - bps / 10_000.0)
        return FillEstimate(reference_price, fill, bps, (reference_price - fill) * quantity, self.commission(quantity))


def executable_quantity(
    target_notional: float,
    estimated_fill_price: float,
    bar_volume: float,
    participation_rate: float,
    *,
    fractional_shares: bool,
) -> tuple[float, float]:
    desired = target_notional / estimated_fill_price if estimated_fill_price > 0 else 0.0
    volume_cap = max(0.0, bar_volume * participation_rate)
    quantity = min(desired, volume_cap)
    if fractional_shares:
        quantity = math.floor(quantity * 1_000) / 1_000
    else:
        quantity = math.floor(quantity)
    fill_ratio = quantity / desired if desired > 0 else 0.0
    return quantity, fill_ratio
