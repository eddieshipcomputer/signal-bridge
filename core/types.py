"""
signal-bridge — Shared dataclasses for the execution layer.

These types are the contract between the core engine and broker adapters.
No broker-specific fields — adapters map their native types to these.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(str, Enum):
    PENDING = "pending"
    FILLED = "filled"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class TriggerType(str, Enum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    STOP_LIMIT = "stop_limit"


class PositionStatus(str, Enum):
    FLAT = "flat"
    PENDING = "pending"       # signal accepted, waiting for entry trigger
    FILLED = "filled"         # position open
    MANAGING = "managing"     # SL/TP set, being monitored
    CLOSING = "closing"       # close order submitted
    CLOSED = "closed"         # flat again, PnL realized


@dataclass
class Position:
    """An open or historical position."""
    ticker: str
    side: Side
    size: float                  # quantity in base units
    entry_price: float
    leverage: float = 1.0        # 1.0 = spot, higher = margined
    liquidation_price: Optional[float] = None
    unrealized_pnl: float = 0.0
    margin_used: float = 0.0
    status: PositionStatus = PositionStatus.FILLED
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    closed_at: Optional[datetime] = None
    strategy_id: str = ""        # which strategy opened this
    trigger_ids: list[str] = field(default_factory=list)  # SL/TP order IDs

    @property
    def notional(self) -> float:
        return self.size * self.entry_price


@dataclass
class Fill:
    """A single fill from the exchange."""
    fill_id: str
    ticker: str
    side: Side
    size: float
    price: float
    fee: float                   # exact fee from exchange, never estimated
    timestamp: datetime
    order_id: str = ""


@dataclass
class Order:
    """An order to be placed."""
    ticker: str
    side: Side
    size: float
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[float] = None    # required if order_type == LIMIT
    reduce_only: bool = False
    client_order_id: str = ""               # optional tag for tracking
    strategy_id: str = ""


@dataclass
class TriggerOrder:
    """A conditional/trigger order (SL, TP, stop-limit)."""
    ticker: str
    side: Side                   # opposite of position side (SL on a long = sell)
    trigger_type: TriggerType
    trigger_price: float         # price that activates the trigger
    size: float
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[float] = None     # for stop-limit
    reduce_only: bool = True
    client_order_id: str = ""
    strategy_id: str = ""


@dataclass
class Balance:
    """Account balance snapshot."""
    equity: float                # total account equity
    available_margin: float      # margin available for new positions
    margin_used: float           # margin tied up in open positions
    currency: str = "USD"


@dataclass
class Signal:
    """A validated trading signal from the signal generation layer."""
    ticker: str
    direction: Side
    conviction: int              # 1-7 scale
    entry_zone_low: float
    entry_zone_high: float
    sl_pct: float                # stop loss as % from entry
    tp_pct: float                # take profit as % from entry
    entry_session: str = "any"   # "open", "power_hour", "any"
    time_in_force: Optional[int] = None   # bars or days until signal expires
    catalyst_date: Optional[str] = None   # ISO date string
    regime_flag: str = "neutral"
    max_positions: int = 5
    strategy_id: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def conviction_tier(self) -> str:
        """Binary sizing tiers — not linear scaling."""
        if self.conviction <= 3:
            return "skip"
        elif self.conviction <= 5:
            return "standard"   # 50% max allocation
        else:
            return "full"       # 100% max allocation
