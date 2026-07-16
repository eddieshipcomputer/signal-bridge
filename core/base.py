"""
signal-bridge — Abstract broker interface.

All adapters must implement this interface. The core engine interacts
with brokers exclusively through BrokerAdapter — no broker-specific
types or API calls leak into core.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import AsyncIterator, Optional

from .types import (
    Balance,
    Fill,
    Order,
    OrderStatus,
    Position,
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
    """

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """Return all open positions from the exchange."""
        ...

    @abstractmethod
    async def get_position(self, ticker: str) -> Optional[Position]:
        """Return position for a single ticker, or None if flat."""
        ...

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
    async def modify_order(self, order_id: str, modifications: dict) -> bool:
        """
        Modify an existing order or trigger.

        NOTE: Not guaranteed to be atomic. Some brokers (HL) support
        in-place modification; others (Alpaca) do cancel + replace.
        Callers should not assume atomicity.
        Returns True if the modification eventually succeeded.
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
        """
        Return current price for a ticker.
        For equities: real-time during market hours only.
        Check is_market_open() before relying on this for entry zone checks.
        """
        ...

    @abstractmethod
    async def is_market_open(self) -> bool:
        """
        Whether the market is currently open for trading.
        Crypto (HL): always True (24/7).
        Equities (Alpaca): session-aware (9:30-16:00 ET weekdays).
        """
        ...
