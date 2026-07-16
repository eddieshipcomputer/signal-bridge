"""
signal-bridge — Abstract broker interface.

Uses junto's ProtectionOutcome pattern: model protection intent, let each
adapter satisfy it natively (resting broker order) or synthetically
(tick-time sweep). Eliminates the modify_order atomicity problem.

All broker-specific logic lives in adapters. Core engine interacts
exclusively through BrokerAdapter — no broker types leak into core.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import AsyncIterator, Optional

from .types import (
    AssetClass,
    Balance,
    Fill,
    Order,
    OrderStatus,
    Position,
    ProtectionOutcome,
    SyntheticClose,
    TriggerOrder,
)


class BrokerAdapter(ABC):
    """
    Abstract broker interface.

    Implementations: hyperliquid.py (HL SDK), alpaca.py (Alpaca API),
    ibkr.py (stub).

    Contract:
    - All methods are async.
    - Exchange API is always ground truth — never estimate.
    - Methods map native broker types to shared dataclasses (core.types).
    - Errors raise exceptions; the engine handles retries/fallbacks.

    Reference: junto's lib/trading/adapter.ts (TypeScript, production).
    """

    broker: str = "base"

    @abstractmethod
    async def is_market_open(self) -> bool:
        """
        Whether the market is currently open for trading.
        Crypto (HL): always True (24/7).
        Equities (Alpaca): session-aware (9:30-16:00 ET weekdays).
        """
        ...

    @abstractmethod
    async def list_positions(self) -> list[Position]:
        """Return all open positions from the exchange (normalized)."""
        ...

    async def get_position(self, ticker: str) -> Optional[Position]:
        """Return position for a single ticker, or None if flat.
        Default impl scans list_positions — adapters may override for efficiency."""
        positions = await self.list_positions()
        for p in positions:
            if p.ticker == ticker:
                return p
        return None

    @abstractmethod
    async def get_account_balance(self) -> Balance:
        """Return current account balance snapshot."""
        ...

    @abstractmethod
    async def place_order(self, order: Order) -> Fill:
        """
        Place a market or limit order.
        Returns the Fill on success (price, size, fees from exchange).
        Raises on rejection or failure.
        """
        ...

    @abstractmethod
    async def place_trigger_order(self, trigger: TriggerOrder) -> str:
        """
        Place a trigger/conditional order (SL, TP, stop-limit).
        Returns the trigger order ID from the exchange.
        """
        ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        """
        Cancel a pending order or trigger.
        Returns True if cancelled (or already cancelled), False on failure.
        Idempotent — cancelling an already-cancelled order returns True.
        """
        ...

    @abstractmethod
    async def get_fills(self, since: datetime) -> AsyncIterator[Fill]:
        """
        Yield fills since the given timestamp.
        Returns an async iterator to handle pagination efficiently.
        The reconciler iterates through without loading all into memory.
        """
        ...

    @abstractmethod
    async def get_order_status(self, order_id: str) -> OrderStatus:
        """Return current status of an order."""
        ...

    @abstractmethod
    async def get_ticker_price(self, ticker: str) -> float:
        """Return current price for a ticker."""
        ...

    @abstractmethod
    async def reconcile_protection(self) -> list[ProtectionOutcome]:
        """
        Ensure every open trade has stop/target coverage.

        Returns one ProtectionOutcome per known position:
        - native: resting broker order covers it
        - synthetic: no resting stop, needs tick-time sweep
        - no_position: position doesn't exist (may have closed externally)
        - no_levels: position exists but no SL/TP configured
        - error: check failed

        This is the CORRECT abstraction for SL/TP management — not modify_order.
        Borrowed from junto's production pattern.
        """
        ...

    @abstractmethod
    async def enforce_stops_on_tick(self) -> list[SyntheticClose]:
        """
        Synthetic tick-time sweep — evaluates levels and closes positions
        where the venue has no resting stop.

        No-op for venues with full native stop support (e.g. HL perps).
        Active for venues like Alpaca that may not have resting stops on
        certain order types.

        Returns list of SyntheticClose for any positions closed this tick.
        """
        ...
