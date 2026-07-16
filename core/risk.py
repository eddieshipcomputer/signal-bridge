"""
signal-bridge — Risk management.

Conviction-tier sizing as the hard envelope. No linear scaling into trades.
Equity floor and daily drawdown circuit breaker.

Design principle (validated by junto's experience):
    Rules-based sizing is more auditable than LLM-based.
    An LLM layer can allocate WITHIN tiers, never replace them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from .types import Balance, Position, Side, Signal

logger = logging.getLogger(__name__)


class RiskDecision(str, Enum):
    APPROVE = "approve"
    SKIP_LOW_CONVICTON = "skip_low_conviction"
    SKIP_MAX_POSITIONS = "skip_max_positions"
    SKIP_SECTOR_CAP = "skip_sector_cap"
    SKIP_EQUITY_FLOOR = "skip_equity_floor"
    SKIP_DRAWDOWN_BREAKER = "skip_drawdown_breaker"
    SKIP_DUPLICATE = "skip_duplicate"


@dataclass
class RiskConfig:
    """Risk gate configuration."""
    equity_floor: float = 50.0           # stop all trading below this balance
    max_positions: int = 5               # portfolio-level cap
    daily_drawdown_limit: float = 0.10   # 10% daily loss = circuit breaker
    conviction_standard_min: int = 4     # 4-5 = 50% allocation
    conviction_full_min: int = 6         # 6-7 = 100% allocation
    standard_allocation_pct: float = 0.50   # 50% of max position size
    full_allocation_pct: float = 1.0       # 100% of max position size
    max_position_pct: float = 0.25         # max 25% of equity per position


@dataclass
class RiskCheckResult:
    decision: RiskDecision
    detail: str = ""
    size_fraction: float = 0.0          # 0.0 = no trade, 0.5 = standard, 1.0 = full
    notional_usd: float = 0.0           # computed notional after sizing


class RiskManager:
    """
    Hard risk gates. Every signal passes through here before execution.

    Sizing is binary within tiers:
        Conviction 1-3 → skip (hard gate, not smaller position)
        Conviction 4-5 → standard (50% allocation)
        Conviction 6-7 → full (100% allocation)

    No gradual scaling. This prevents slowly building into bad trades.
    """

    def __init__(self, config: RiskConfig | None = None):
        self.config = config or RiskConfig()
        self._daily_pnl: float = 0.0
        self._daily_pnl_reset: datetime = datetime.now(timezone.utc)
        self._peak_equity: float = 0.0

    def reset_daily_pnl(self) -> None:
        """Call at start of each trading day."""
        self._daily_pnl = 0.0
        self._daily_pnl_reset = datetime.now(timezone.utc)

    def update_pnl(self, realized_pnl: float) -> None:
        """Track realized PnL for drawdown circuit breaker."""
        # Reset if new day
        now = datetime.now(timezone.utc)
        if now - self._daily_pnl_reset > timedelta(hours=24):
            self.reset_daily_pnl()
        self._daily_pnl += realized_pnl

    def check(
        self,
        signal: Signal,
        balance: Balance,
        open_positions: list[Position],
    ) -> RiskCheckResult:
        """
        Run all risk gates on a signal. Returns approve/deny + sizing.

        This is the ONLY entry point for position sizing decisions.
        """
        # 1. Conviction gate (hard)
        if signal.conviction < self.config.conviction_standard_min:
            return RiskCheckResult(
                decision=RiskDecision.SKIP_LOW_CONVICTON,
                detail=f"Conviction {signal.conviction} below threshold {self.config.conviction_standard_min}",
            )

        # 2. Hold signals don't need sizing
        if signal.direction in (Side.HOLD, Side.EXIT):
            return RiskCheckResult(
                decision=RiskDecision.APPROVE,
                detail=f"{signal.direction.value} signal — no new position needed",
                size_fraction=0.0,
                notional_usd=0.0,
            )

        # 3. Equity floor
        if balance.equity < self.config.equity_floor:
            return RiskCheckResult(
                decision=RiskDecision.SKIP_EQUITY_FLOOR,
                detail=f"Equity ${balance.equity:.2f} below floor ${self.config.equity_floor:.2f}",
            )

        # 4. Daily drawdown circuit breaker
        if self._peak_equity > 0:
            drawdown = (self._peak_equity - balance.equity) / self._peak_equity
            if drawdown >= self.config.daily_drawdown_limit:
                return RiskCheckResult(
                    decision=RiskDecision.SKIP_DRAWDOWN_BREAKER,
                    detail=f"Daily drawdown {drawdown:.1%} >= limit {self.config.daily_drawdown_limit:.1%}",
                )

        # Update peak equity
        if balance.equity > self._peak_equity:
            self._peak_equity = balance.equity

        # 5. Max positions (ignore if this is an exit/hold)
        if signal.direction in (Side.LONG, Side.SHORT):
            if len(open_positions) >= signal.max_positions:
                # Check if we already have a position in this ticker (duplicate)
                existing = [p for p in open_positions if p.ticker == signal.ticker]
                if existing:
                    return RiskCheckResult(
                        decision=RiskDecision.SKIP_DUPLICATE,
                        detail=f"Already have position in {signal.ticker}",
                    )
                return RiskCheckResult(
                    decision=RiskDecision.SKIP_MAX_POSITIONS,
                    detail=f"{len(open_positions)} open positions, max {signal.max_positions}",
                )

        # 6. Conviction sizing (binary tiers)
        if signal.conviction >= self.config.conviction_full_min:
            size_fraction = self.config.full_allocation_pct
            tier = "full"
        else:
            size_fraction = self.config.standard_allocation_pct
            tier = "standard"

        # 7. Compute notional
        max_notional = balance.equity * self.config.max_position_pct
        notional = max_notional * size_fraction

        # 8. Available margin check
        if notional > balance.available_margin:
            notional = balance.available_margin
            logger.info(
                f"Risk: {signal.ticker} notional capped to available margin ${notional:.2f}"
            )

        return RiskCheckResult(
            decision=RiskDecision.APPROVE,
            detail=f"{tier} allocation ({signal.conviction}/7 conviction)",
            size_fraction=size_fraction,
            notional_usd=notional,
        )
