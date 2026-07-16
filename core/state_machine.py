"""
signal-bridge — Position state machine.

Manages lifecycle transitions for positions:
    flat → pending → filled → managing → closed

States:
    FLAT      — no position, no pending orders
    PENDING   — signal accepted, entry order submitted, waiting for fill
    FILLED    — position open, no SL/TP yet
    MANAGING  — position open with SL/TP protection active
    CLOSING   — close order submitted, waiting for fill
    CLOSED    — flat again, PnL realized

Transitions are driven by:
    - Signal acceptance (flat → pending)
    - Order fills (pending → filled)
    - Protection provisioning (filled → managing)
    - Exit signals or SL/TP triggers (managing → closing → closed)
    - Reconciler detection of external closes (any → closed)

Invalid transitions raise — never silently skip a state change.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from .types import Position, PositionStatus, Signal, Side

logger = logging.getLogger(__name__)


# Valid state transitions (from → {allowed to})
VALID_TRANSITIONS: dict[PositionStatus, set[PositionStatus]] = {
    PositionStatus.FLAT: {
        PositionStatus.PENDING,      # new signal accepted
    },
    PositionStatus.PENDING: {
        PositionStatus.FILLED,       # entry order filled
        PositionStatus.FLAT,         # order cancelled/rejected/expired
        PositionStatus.CLOSED,       # reconciler detected it never opened
    },
    PositionStatus.FILLED: {
        PositionStatus.MANAGING,     # SL/TP placed
        PositionStatus.CLOSING,      # direct close signal
        PositionStatus.CLOSED,       # reconciler detected external close
    },
    PositionStatus.MANAGING: {
        PositionStatus.CLOSING,      # exit signal or SL/TP triggered
        PositionStatus.CLOSED,       # reconciler detected external close
        PositionStatus.FILLED,       # protection order cancelled, re-managing needed
    },
    PositionStatus.CLOSING: {
        PositionStatus.CLOSED,       # close order filled
        PositionStatus.MANAGING,     # close order failed, back to managing
    },
    PositionStatus.CLOSED: {
        PositionStatus.FLAT,         # cleanup complete, ready for new signal
    },
}


class StateMachineError(Exception):
    """Raised when an invalid state transition is attempted."""
    pass


class PositionStateMachine:
    """
    Manages position lifecycle transitions.

    Usage:
        sm = PositionStateMachine()
        sm.transition(position, PositionStatus.PENDING)  # flat → pending
        sm.transition(position, PositionStatus.FILLED)   # pending → filled
    """

    def can_transition(self, current: PositionStatus, target: PositionStatus) -> bool:
        """Check if a transition is valid without performing it."""
        return target in VALID_TRANSITIONS.get(current, set())

    def transition(
        self,
        position: Position,
        target: PositionStatus,
        detail: str = "",
    ) -> None:
        """
        Transition a position to a new state.

        Args:
            position: The position to transition
            target: Target state
            detail: Optional context for logging

        Raises:
            StateMachineError: if the transition is invalid
        """
        current = position.status

        if not self.can_transition(current, target):
            raise StateMachineError(
                f"Invalid transition for {position.ticker}: "
                f"{current.value} → {target.value}"
                f"{' (' + detail + ')' if detail else ''}"
            )

        old = position.status
        position.status = target

        # Side effects on specific transitions
        if target == PositionStatus.FILLED:
            if not position.opened_at:
                position.opened_at = datetime.now(timezone.utc)

        elif target == PositionStatus.CLOSED:
            position.closed_at = datetime.now(timezone.utc)
            if position.status != PositionStatus.CLOSING:
                # Direct close from filled/managing (external close detected)
                logger.info(
                    f"StateMachine: {position.ticker} closed externally "
                    f"(was {old.value})"
                )

        elif target == PositionStatus.FLAT:
            # Reset for reuse
            position.closed_at = position.closed_at or datetime.now(timezone.utc)

        logger.debug(
            f"StateMachine: {position.ticker} {old.value} → {target.value}"
            f"{' (' + detail + ')' if detail else ''}"
        )

    def accept_signal(self, position: Optional[Position], signal: Signal) -> Position:
        """
        Accept a new trading signal and create/transition a position.

        Returns the position object (new or existing).
        """
        if signal.direction == Side.HOLD:
            # Hold = re-qualify, don't change state
            if position:
                logger.info(f"StateMachine: HOLD {position.ticker} — no state change")
                return position
            logger.info(f"StateMachine: HOLD {signal.ticker} — no position to hold")
            # No position to hold — no-op
            return position or Position(
                ticker=signal.ticker,
                status=PositionStatus.FLAT,
            )

        if signal.direction == Side.EXIT:
            # Exit = close existing position
            if position and position.status in (
                PositionStatus.FILLED,
                PositionStatus.MANAGING,
            ):
                self.transition(
                    position, PositionStatus.CLOSING,
                    detail=f"EXIT signal (conviction {signal.conviction})"
                )
            return position or Position(
                ticker=signal.ticker,
                status=PositionStatus.FLAT,
            )

        # Entry signal (LONG or SHORT)
        if position and position.status != PositionStatus.FLAT:
            if position.status == PositionStatus.CLOSED:
                # Reset to flat first
                self.transition(position, PositionStatus.FLAT)
            else:
                raise StateMachineError(
                    f"Cannot accept entry for {signal.ticker}: "
                    f"position is {position.status.value}"
                )

        # Create new position or transition existing
        if position:
            position.side = signal.direction
            self.transition(
                position, PositionStatus.PENDING,
                detail=f"Entry signal (conviction {signal.conviction}/7)"
            )
            return position
        else:
            return Position(
                ticker=signal.ticker,
                side=signal.direction,
                status=PositionStatus.PENDING,
                strategy_id=signal.strategy_id,
            )

    def on_fill(self, position: Position) -> None:
        """Called when entry order fills."""
        self.transition(position, PositionStatus.FILLED, detail="Entry fill")

    def on_protected(self, position: Position) -> None:
        """Called when SL/TP orders are placed."""
        self.transition(position, PositionStatus.MANAGING, detail="SL/TP placed")

    def on_protection_lost(self, position: Position) -> None:
        """Called when SL/TP orders are cancelled or filled (protection gone)."""
        if position.status == PositionStatus.MANAGING:
            self.transition(position, PositionStatus.FILLED, detail="Protection lost")

    def on_close_signal(self, position: Position) -> None:
        """Called when an exit signal or SL/TP trigger fires."""
        if position.status in (PositionStatus.FILLED, PositionStatus.MANAGING):
            self.transition(position, PositionStatus.CLOSING, detail="Close signal")

    def on_closed(self, position: Position) -> None:
        """Called when close is confirmed (fill or reconciler detection)."""
        if position.status != PositionStatus.CLOSED:
            self.transition(
                position, PositionStatus.CLOSED,
                detail="Position closed"
            )

    def on_external_close(self, position: Position) -> None:
        """
        Called by reconciler when position disappears from broker.
        Can transition from any active state to CLOSED.

        Broker truth wins — bypass transition validation since
        the position is already gone from the exchange.
        """
        if position.status == PositionStatus.CLOSED:
            return  # already closed

        old = position.status
        if old == PositionStatus.FLAT:
            return  # nothing to close

        # Force the transition — broker says it's closed, it's closed.
        # Don't mutate VALID_TRANSITIONS (that would leak across positions).
        position.status = PositionStatus.CLOSED
        position.closed_at = datetime.now(timezone.utc)
        logger.info(
            f"StateMachine: {position.ticker} external close "
            f"(was {old.value}) — broker truth"
        )
