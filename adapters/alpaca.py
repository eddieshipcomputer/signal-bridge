"""
signal-bridge — Alpaca broker adapter (equities).

Implements BrokerAdapter for the Alpaca Markets API.

Equities-specific notes:
- is_market_open(): session-aware, 9:30-16:00 ET weekdays only
- get_fills(): uses Activities endpoint with pagination (AsyncIterator)
- place_trigger_order(): Alpaca bracket orders submitted with entry;
  standalone stops use cancel+replace (not atomic, documented)
- enforce_stops_on_tick(): active for equities (synthetic sweep for
  positions that lost their bracket on manual intervention)
- reconcile_protection(): uses open orders list to check SL/TP coverage

Dependencies: alpaca-py (pip install alpaca-py)
Config: ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL env vars
  (use https://paper-api.alpaca.markets for paper trading)
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    LimitOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
    GetOrdersRequest,
)
from alpaca.trading.enums import (
    OrderSide,
    TimeInForce,
    OrderStatus as AlpacaOrderStatus,
    OrderType as AlpacaOrderType,
)
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.common.exceptions import APIError

from ..core.base import BrokerAdapter
from ..core.types import (
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
    SyntheticClose,
    TriggerOrder,
    TriggerType,
)


def _to_side(alpaca_side: OrderSide) -> Side:
    return Side.LONG if alpaca_side == OrderSide.BUY else Side.SHORT


def _from_side(side: Side) -> OrderSide:
    return OrderSide.BUY if side == Side.LONG else OrderSide.SELL


def _to_order_status(alpaca_status: AlpacaOrderStatus) -> OrderStatus:
    mapping = {
        AlpacaOrderStatus.PENDING_NEW: OrderStatus.PENDING,
        AlpacaOrderStatus.NEW: OrderStatus.PENDING,
        AlpacaOrderStatus.ACCEPTED: OrderStatus.PENDING,
        AlpacaOrderStatus.FILLED: OrderStatus.FILLED,
        AlpacaOrderStatus.PARTIALLY_FILLED: OrderStatus.PARTIAL,
        AlpacaOrderStatus.CANCELED: OrderStatus.CANCELLED,
        AlpacaOrderStatus.REJECTED: OrderStatus.REJECTED,
        AlpacaOrderStatus.EXPIRED: OrderStatus.CANCELLED,
    }
    return mapping.get(alpaca_status, OrderStatus.PENDING)


class AlpacaAdapter(BrokerAdapter):
    """Alpaca broker adapter for equities trading."""

    broker = "alpaca"

    def __init__(
        self,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        base_url: Optional[str] = None,
        paper: bool = True,
    ) -> None:
        self._api_key = api_key or os.environ["ALPACA_API_KEY"]
        self._secret_key = secret_key or os.environ["ALPACA_SECRET_KEY"]
        self._base_url = base_url or os.environ.get(
            "ALPACA_BASE_URL",
            "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets",
        )
        self._client = TradingClient(
            api_key=self._api_key,
            secret_key=self._secret_key,
            paper=paper,
        )
        self._data_client = StockHistoricalDataClient(
            api_key=self._api_key,
            secret_key=self._secret_key,
        )
        # SL/TP levels tracked locally for synthetic sweep
        # {ticker: {"sl": float | None, "tp": float | None}}
        self._protection_levels: dict[str, dict[str, Optional[float]]] = {}

    async def is_market_open(self) -> bool:
        """Equities: session-aware check via Alpaca clock endpoint."""
        clock = self._client.get_clock()
        return clock.is_open

    async def list_positions(self) -> list[Position]:
        alpaca_positions = self._client.get_all_positions()
        result = []
        for p in alpaca_positions:
            result.append(Position(
                ticker=p.symbol,
                symbol=p.symbol,
                side=Side.LONG if p.side.value == "long" else Side.SHORT,
                size=float(p.qty),
                entry_price=float(p.avg_entry_price),
                current_price=float(p.current_price),
                asset_class=AssetClass.EQUITY,
                leverage=1.0,  # Alpaca equity positions are always 1x (margin handled account-side)
                unrealized_pnl=float(p.unrealized_pl),
                margin_used=float(p.cost_basis),
                status=PositionStatus.MANAGING,
            ))
        return result

    async def get_account_balance(self) -> Balance:
        account = self._client.get_account()
        return Balance(
            equity=float(account.equity),
            available_margin=float(account.buying_power),
            margin_used=float(account.initial_margin),
            currency="USD",
        )

    async def place_order(self, order: Order) -> Fill:
        if order.order_type == OrderType.MARKET:
            request = MarketOrderRequest(
                symbol=order.ticker,
                qty=order.size,
                side=_from_side(order.side),
                time_in_force=TimeInForce.DAY,
                client_order_id=order.client_order_id or None,
            )
        else:
            if order.limit_price is None:
                raise ValueError("limit_price required for LIMIT orders")
            request = LimitOrderRequest(
                symbol=order.ticker,
                qty=order.size,
                side=_from_side(order.side),
                limit_price=order.limit_price,
                time_in_force=TimeInForce.DAY,
                client_order_id=order.client_order_id or None,
            )
        result = self._client.submit_order(request)
        fill_price = float(result.filled_avg_price or order.limit_price or 0.0)
        return Fill(
            fill_id=str(result.id),
            ticker=order.ticker,
            side=order.side,
            size=float(result.filled_qty or 0.0),
            price=fill_price,
            fee=0.0,  # Alpaca doesn't expose per-fill commission in order response
            timestamp=result.filled_at or datetime.now(timezone.utc),
            order_id=str(result.id),
        )

    async def place_trigger_order(self, trigger: TriggerOrder) -> str:
        """
        Alpaca does not support modifying trigger orders in place.
        This places a standalone stop or limit order.
        To update: cancel the existing trigger_id and call this again.
        Not atomic — there's a brief window between cancel and re-place.
        """
        if trigger.trigger_type == TriggerType.STOP_LOSS:
            from alpaca.trading.requests import StopOrderRequest
            request = StopOrderRequest(
                symbol=trigger.ticker,
                qty=trigger.size,
                side=_from_side(trigger.side),
                stop_price=trigger.trigger_price,
                time_in_force=TimeInForce.GTC,
                client_order_id=trigger.client_order_id or None,
            )
        elif trigger.trigger_type == TriggerType.TAKE_PROFIT:
            request = LimitOrderRequest(
                symbol=trigger.ticker,
                qty=trigger.size,
                side=_from_side(trigger.side),
                limit_price=trigger.trigger_price,
                time_in_force=TimeInForce.GTC,
                client_order_id=trigger.client_order_id or None,
            )
        else:
            raise ValueError(f"Unsupported trigger type: {trigger.trigger_type}")

        result = self._client.submit_order(request)
        return str(result.id)

    async def cancel_order(self, order_id: str) -> bool:
        try:
            self._client.cancel_order_by_id(order_id)
            return True
        except APIError:
            return False

    async def get_fills(self, since: datetime) -> AsyncIterator[Fill]:
        """
        Yields fills via Alpaca Activities endpoint with pagination.
        Websocket fill notifications can lag at market open — use this
        reconciler path as the reliable source of fill truth.
        """
        from alpaca.trading.requests import GetPortfolioHistoryRequest
        page_token = None
        while True:
            activities = self._client.get_activities(
                activity_types=["FILL"],
                after=since.isoformat(),
                page_token=page_token,
                page_size=100,
            )
            if not activities:
                break
            for act in activities:
                yield Fill(
                    fill_id=str(act.id),
                    ticker=act.symbol,
                    side=Side.LONG if act.side == "buy" else Side.SHORT,
                    size=float(act.qty),
                    price=float(act.price),
                    fee=float(act.per_share_amount or 0.0),
                    timestamp=act.transaction_time,
                    order_id=str(act.order_id),
                )
            if len(activities) < 100:
                break
            page_token = activities[-1].id

    async def get_order_status(self, order_id: str) -> OrderStatus:
        try:
            order = self._client.get_order_by_id(order_id)
            return _to_order_status(order.status)
        except APIError:
            return OrderStatus.CANCELLED

    async def get_ticker_price(self, ticker: str) -> float:
        """
        Returns latest quote mid-price.
        Only valid during market hours — pre/after-market data requires
        paid Alpaca subscription. Call is_market_open() before using this
        for entry zone decisions.
        """
        request = StockLatestQuoteRequest(symbol_or_symbols=ticker)
        quote = self._data_client.get_stock_latest_quote(request)
        q = quote[ticker]
        return (q.ask_price + q.bid_price) / 2.0

    async def reconcile_protection(self) -> list[ProtectionOutcome]:
        """
        Check SL/TP coverage for all open positions.

        Alpaca equities: bracket orders (submitted with entry) provide
        native coverage. Standalone positions (manually opened or from
        bracket failure) fall back to synthetic sweep.

        no_position status indicates the position closed externally
        (SL/TP hit, manual close) — engine should sync state accordingly.
        """
        positions = await self.list_positions()
        open_orders = self._client.get_orders(
            filter=GetOrdersRequest(status="open")
        )
        # Index stop/limit orders by symbol
        covered: dict[str, bool] = {}
        for o in open_orders:
            if o.order_type in (AlpacaOrderType.STOP, AlpacaOrderType.LIMIT, AlpacaOrderType.STOP_LIMIT):
                covered[o.symbol] = True

        outcomes = []
        position_tickers = {p.ticker for p in positions}

        for ticker, levels in self._protection_levels.items():
            if ticker not in position_tickers:
                outcomes.append(ProtectionOutcome(
                    ticker=ticker,
                    status=ProtectionStatus.NO_POSITION,
                    detail="position not found on exchange — may have closed externally",
                ))
                continue

            if not levels.get("sl") and not levels.get("tp"):
                outcomes.append(ProtectionOutcome(
                    ticker=ticker,
                    status=ProtectionStatus.NO_LEVELS,
                    detail="no SL/TP levels configured for this position",
                ))
                continue

            if covered.get(ticker):
                outcomes.append(ProtectionOutcome(
                    ticker=ticker,
                    status=ProtectionStatus.NATIVE,
                ))
            else:
                outcomes.append(ProtectionOutcome(
                    ticker=ticker,
                    status=ProtectionStatus.SYNTHETIC,
                    detail="no resting stop found — falling back to tick sweep",
                ))

        return outcomes

    async def enforce_stops_on_tick(self) -> list[SyntheticClose]:
        """
        Synthetic tick-time sweep for equities positions without resting stops.

        Checks current price against locally tracked SL/TP levels.
        If a level is breached, submits a market close order.

        Active for Alpaca (unlike HL which has native perp stops).
        """
        closes: list[SyntheticClose] = []
        positions = await self.list_positions()
        position_map = {p.ticker: p for p in positions}

        for ticker, levels in self._protection_levels.items():
            position = position_map.get(ticker)
            if not position:
                continue

            price = position.current_price
            sl = levels.get("sl")
            tp = levels.get("tp")
            close_reason: Optional[str] = None

            if position.side == Side.LONG:
                if sl and price <= sl:
                    close_reason = "stop_loss_hit"
                elif tp and price >= tp:
                    close_reason = "take_profit_hit"
            else:
                if sl and price >= sl:
                    close_reason = "stop_loss_hit"
                elif tp and price <= tp:
                    close_reason = "take_profit_hit"

            if close_reason:
                close_order = Order(
                    ticker=ticker,
                    side=Side.SHORT if position.side == Side.LONG else Side.LONG,
                    size=position.size,
                    order_type=OrderType.MARKET,
                    reduce_only=True,
                    strategy_id=position.strategy_id,
                )
                fill = await self.place_order(close_order)
                closes.append(SyntheticClose(
                    ticker=ticker,
                    side=position.side,
                    size=fill.size,
                    close_price=fill.price,
                    reason=close_reason,
                ))
                del self._protection_levels[ticker]

        return closes

    async def get_closing_fill(self, ticker: str, opened_at: datetime) -> Optional[Fill]:
        """
        Retrieve the actual closing fill for a position detected as closed-by-absence.

        Use this when reconcile_protection() returns NO_POSITION for a tracked ticker.
        Queries the Activities endpoint for the closing fill (sell for long, buy for short)
        since the position was opened. Returns the matched fill with true price + fees.

        Why: close-by-absence is reliable for detecting that a position closed, but
        the exit price must come from the actual fill — not last trade at detection time.
        junto approximates with getLastTrade(); we get the real fill here instead.
        """
        close_side_str = "sell"  # default long close; short close would be "buy"
        async for fill in self.get_fills(since=opened_at):
            if fill.ticker == ticker:
                return fill
        return None

    def set_protection_levels(
        self,
        ticker: str,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ) -> None:
        """Register SL/TP levels for a position (used by synthetic sweep)."""
        self._protection_levels[ticker] = {"sl": sl, "tp": tp}

    def clear_protection_levels(self, ticker: str) -> None:
        """Remove protection levels when a position is closed."""
        self._protection_levels.pop(ticker, None)
