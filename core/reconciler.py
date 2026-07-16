"""
signal-bridge — Position-diff reconciler.

Philosophy (from junto's production pattern):
    Broker positions are the source of truth. Local state syncs to them.
    Fills are for the ledger (fee capture, execution journals) — NOT for
    deriving position state.

Every cycle:
    1. Pull broker positions (list_positions)
    2. Diff against local state
    3. Reconcile: update local to match broker
    4. Detect externally-closed positions (broker has none, local has one)
    5. For closes, pull actual closing fill for accurate price/fees
    6. Check protection coverage (reconcile_protection)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .types import (
    Fill,
    Position,
    PositionStatus,
    ProtectionOutcome,
    ProtectionStatus,
    Side,
)

if TYPE_CHECKING:
    from .base import BrokerAdapter

logger = logging.getLogger(__name__)


@dataclass
class ReconcileResult:
    """Result of a single reconciliation cycle."""
    updated: list[Position] = field(default_factory=list)       # local state was updated
    closed: list[Position] = field(default_factory=list)        # detected externally closed
    opened: list[Position] = field(default_factory=list)        # detected externally opened
    protection_issues: list[ProtectionOutcome] = field(default_factory=list)
    closing_fills: dict[str, Fill] = field(default_factory=dict)  # ticker → actual closing fill


class Reconciler:
    """
    Position-diff reconciler.

    Treats the broker's reported positions as ground truth. Every cycle,
    pulls broker state and diffs against local engine state. Local state
    always converges to broker reality.

    Usage:
        reconciler = Reconciler(adapter, local_state)
        result = await reconciler.cycle()
    """

    def __init__(self, adapter: BrokerAdapter, local_positions: dict[str, Position]):
        """
        Args:
            adapter: Broker adapter (source of truth)
            local_positions: Mutable dict of ticker → Position (engine state)
        """
        self.adapter = adapter
        self.local = local_positions

    async def cycle(self) -> ReconcileResult:
        """Run one reconciliation cycle. Returns what changed."""
        result = ReconcileResult()

        # 1. Pull broker positions (ground truth)
        try:
            broker_positions = await self.adapter.list_positions()
        except Exception as e:
            logger.error(f"Reconciler: failed to list positions: {e}")
            return result

        broker_map: dict[str, Position] = {p.ticker: p for p in broker_positions}
        broker_tickers = set(broker_map.keys())
        local_tickers = set(self.local.keys())

        # 2. Detect externally closed positions (in local, not in broker)
        closed_tickers = local_tickers - broker_tickers
        for ticker in closed_tickers:
            local_pos = self.local[ticker]
            if local_pos.status in (PositionStatus.CLOSED, PositionStatus.CLOSING):
                continue  # already being handled

            logger.info(f"Reconciler: {ticker} disappeared from broker — externally closed")
            local_pos.status = PositionStatus.CLOSED
            local_pos.closed_at = datetime.now(timezone.utc)

            # Pull actual closing fill for accurate price + fees
            fill = await self._get_closing_fill(ticker, local_pos)
            if fill:
                result.closing_fills[ticker] = fill
                # Update entry with actual fill data
                local_pos.current_price = fill.price
            else:
                # Fallback: use current ticker price (approximation)
                try:
                    local_pos.current_price = await self.adapter.get_ticker_price(ticker)
                except Exception:
                    logger.warning(f"Reconciler: could not get closing price for {ticker}")

            result.closed.append(local_pos)

        # 3. Detect externally opened positions (in broker, not in local)
        opened_tickers = broker_tickers - local_tickers
        for ticker in opened_tickers:
            broker_pos = broker_map[ticker]
            logger.info(f"Reconciler: {ticker} appeared on broker — externally opened")
            self.local[ticker] = broker_pos
            result.opened.append(broker_pos)

        # 4. Update existing positions with broker truth
        shared_tickers = local_tickers & broker_tickers
        for ticker in shared_tickers:
            broker_pos = broker_map[ticker]
            local_pos = self.local[ticker]

            changed = False

            # Quantity drift (partial fills, manual adjustments)
            if abs(local_pos.size - broker_pos.size) > 1e-9:
                logger.info(
                    f"Reconciler: {ticker} size drift "
                    f"local={local_pos.size} broker={broker_pos.size}"
                )
                local_pos.size = broker_pos.size
                changed = True

            # Entry price correction
            if abs(local_pos.entry_price - broker_pos.entry_price) > 1e-9:
                local_pos.entry_price = broker_pos.entry_price
                changed = True

            # Current price update
            local_pos.current_price = broker_pos.current_price
            local_pos.unrealized_pnl = broker_pos.unrealized_pnl

            if changed:
                result.updated.append(local_pos)

        # 5. Check protection coverage
        try:
            protection = await self.adapter.reconcile_protection()
            for outcome in protection:
                if outcome.status in (ProtectionStatus.NO_LEVELS, ProtectionStatus.ERROR):
                    result.protection_issues.append(outcome)
                    logger.warning(
                        f"Reconciler: protection issue on {outcome.ticker}: "
                        f"{outcome.status} {outcome.detail}"
                    )
        except Exception as e:
            logger.error(f"Reconciler: protection check failed: {e}")

        return result

    async def _get_closing_fill(self, ticker: str, position: Position) -> Fill | None:
        """
        Pull the actual closing fill for an externally-closed position.

        This is where get_fills() earns its keep — fee-accurate exit prices,
        not approximations.
        """
        try:
            async for fill in self.adapter.get_fills(position.opened_at):
                if fill.ticker == ticker and fill.side != position.side:
                    return fill
        except Exception as e:
            logger.warning(f"Reconciler: could not pull closing fill for {ticker}: {e}")

        return None
