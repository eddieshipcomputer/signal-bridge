# signal-bridge

Generic signal-to-execution framework for algorithmic trading.

Takes validated trading signals and handles the full execution lifecycle: entry triggers, order placement, SL/TP management, position reconciliation, and risk gates. Broker-agnostic — plug in adapters for different exchanges.

## Quick Start

```bash
pip install -e .
python -m signal_bridge --config config.yaml
```

## Architecture

```
signal-bridge/
  core/
    types.py        # Shared dataclasses (Position, Fill, Order, Signal, etc.)
    base.py         # Abstract BrokerAdapter interface
    engine.py       # Main async execution loop
    state_machine.py  # Position lifecycle: flat → pending → filled → managing → closed
    reconciler.py   # Exchange-truth reconciliation loop
    risk.py         # Equity floor, max positions, daily drawdown breaker
    position.py     # Position tracking, PnL calc, fee handling
  adapters/
    base.py         # Re-exports core.base.BrokerAdapter
    hyperliquid.py  # Hyperliquid SDK adapter (reference implementation)
    alpaca.py       # Alpaca API adapter (equities)
    ibkr.py         # IBKR adapter (stub)
  schemas/
    base.json       # Required fields for all signals
    crypto.json     # Crypto perp signals (HL)
    equities.json   # Equity signals (Alpaca)
```

## Design Principles

1. **Exchange API is always ground truth.** Never estimate fees, prices, or positions. The reconciler syncs engine state to exchange state, never the reverse.
2. **Separate signal generation from execution.** The engine consumes validated signals — it doesn't know or care how they were generated.
3. **Broker-agnostic core.** All broker-specific logic lives in adapters. Core engine interacts exclusively through `BrokerAdapter`.
4. **Tiered conviction sizing.** Binary buckets (skip / standard / full), not linear scaling.
5. **Idempotent operations.** Cancelling an already-cancelled order succeeds. Placing a duplicate signal is a no-op.

## BrokerAdapter Interface

```python
class BrokerAdapter(ABC):
    async def get_positions() -> list[Position]
    async def get_position(ticker) -> Position | None
    async def get_account_balance() -> Balance
    async def place_order(order: Order) -> Fill
    async def place_trigger_order(trigger: TriggerOrder) -> str
    async def cancel_order(order_id: str) -> bool
    async def modify_order(order_id: str, mods: dict) -> bool
    async def get_fills(since: datetime) -> AsyncIterator[Fill]
    async def get_order_status(order_id: str) -> OrderStatus
    async def get_ticker_price(ticker: str) -> float
    async def is_market_open() -> bool
```

## Status

Early scaffold. Core types and broker interface defined. Engine loop, reconciler, and HL adapter being extracted from the FFT trading daemon.

## License

MIT
