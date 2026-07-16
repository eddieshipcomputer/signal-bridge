"""signal-bridge core package."""

from .types import (
    AssetClass,
    Balance,
    Fill,
    Order,
    OrderStatus,
    OrderType,
    Position,
    PositionStatus,
    ProtectionOutcome,
    ProtectionStatus,
    Side,
    Signal,
    SyntheticClose,
    TriggerOrder,
    TriggerType,
)
from .base import BrokerAdapter

__all__ = [
    "AssetClass",
    "BrokerAdapter",
    "Balance",
    "Fill",
    "Order",
    "OrderStatus",
    "OrderType",
    "Position",
    "PositionStatus",
    "ProtectionOutcome",
    "ProtectionStatus",
    "Side",
    "Signal",
    "SyntheticClose",
    "TriggerOrder",
    "TriggerType",
]
