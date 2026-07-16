"""
signal-bridge — Main async execution engine.

Single entry point: `Engine(adapter, config).run()`

Responsibilities:
    - Consume signals from the signal source (Supabase poll or queue)
    - Gate signals through risk checks before acting
    - Place entry orders and set SL/TP on fill
    - Run the reconciler loop on each cycle
    - Run the synthetic stop sweep (enforce_stops_on_tick)
    - Surface events via callbacks (on_fill, on_close, on_risk_reject)

Architecture (decoupled from Benji + Stella whiteboard):
    Signal source → RiskGate → BrokerAdapter → Reconciler → State
    Each layer is replaceable; the engine is the wiring.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from .base import BrokerAdapter
from .reconciler import Reconciler, ReconcileResult
from .risk import RiskConfig, RiskGate, RiskDecision
from .types import (
    Fill,
    Order,
    OrderType,
    Position,
    PositionStatus,
    Side,
    Signal,
    TriggerOrder,
    TriggerType,
)

logger = logging.getLogger(__name__)


@dataclass
class EngineConfig:
    reconcile_interval_s: float = 30.0      # seconds between reconciler cycles
    stop_sweep_interval_s: float = 30.0     # seconds between synthetic stop sweeps
    signal_poll_interval_s: float = 60.0    # seconds between signal source polls
    dry_run: bool = False                   # log intended actions, don't execute


OnFillCallback = Callable[[Signal, Fill], Awaitable[None]]
OnCloseCallback = Callable[[Position, Optional[Fill]], Awaitable[None]]
OnRiskRejectCallback = Callable[[Signal, RiskDecision, str], Awaitable[None]]


class Engine:
    """
    Main async execution engine.

    Manages the full position lifecycle:
        flat → pending → filled → managing → closed

    Usage:
        adapter = AlpacaAdapter(paper=True)
        engine = Engine(adapter=adapter, risk_config=RiskConfig(), engine_config=EngineConfig())
        await engine.run(signal_source)
    """

    def __init__(
        self,
        adapter: BrokerAdapter,
        risk_config: Optional[RiskConfig] = None,
        engine_config: Optional[EngineConfig] = None,
        on_fill: Optional[OnFillCallback] = None,
        on_close: Optional[OnCloseCallback] = None,
        on_risk_reject: Optional[OnRiskRejectCallback] = None,
    ) -> None:
        self.adapter = adapter
        self.risk_config = risk_config or RiskConfig()
        self.config = engine_config or EngineConfig()
        self.on_fill = on_fill
        self.on_close = on_close
        self.on_risk_reject = on_risk_reject

        # Mutable position state — broker is always truth, this is local cache
        self._positions: dict[str, Position] = {}
        self._reconciler = Reconciler(adapter, self._positions)
        self._risk = RiskGate(self.risk_config)

        # Track daily starting equity for drawdown circuit breaker
        self._day_start_equity: Optional[float] = None
        self._day_start_date: Optional[str] = None

        self._running = False

    async def run(self, signal_source: "SignalSource") -> None:
        """
        Start the engine. Runs until stop() is called.

        Args:
            signal_source: Any object with `async def next_signal() -> Signal | None`
                           Returns None when no signal is available this cycle.
        """
        self._running = True
        logger.info(f"Engine starting (dry_run={self.config.dry_run})")

        await asyncio.gather(
            self._reconcile_loop(),
            self._stop_sweep_loop(),
            self._signal_loop(signal_source),
        )

    def stop(self) -> None:
        self._running = False

    # ── Main loops ──────────────────────────────────────────────────────────

    async def _reconcile_loop(self) -> None:
        """Position-diff loop — runs every reconcile_interval_s."""
        while self._running:
            try:
                result = await self._reconciler.cycle()
                await self._handle_reconcile_result(result)
            except Exception as e:
                logger.error(f"Reconciler loop error: {e}")
            await asyncio.sleep(self.config.reconcile_interval_s)

    async def _stop_sweep_loop(self) -> None:
        """Synthetic stop sweep — active for brokers without native stops."""
        while self._running:
            try:
                if not await self.adapter.is_market_open():
                    await asyncio.sleep(self.config.stop_sweep_interval_s)
                    continue
                closes = await self.adapter.enforce_stops_on_tick()
                for synthetic_close in closes:
                    pos = self._positions.get(synthetic_close.ticker)
                    if pos:
                        pos.status = PositionStatus.CLOSED
                        pos.closed_at = datetime.now(timezone.utc)
                        logger.info(
                            f"Synthetic close: {synthetic_close.ticker} "
                            f"reason={synthetic_close.reason} price={synthetic_close.close_price}"
                        )
                        if self.on_close:
                            await self.on_close(pos, None)
            except Exception as e:
                logger.error(f"Stop sweep error: {e}")
            await asyncio.sleep(self.config.stop_sweep_interval_s)

    async def _signal_loop(self, signal_source: "SignalSource") -> None:
        """Poll signal source and attempt entries on qualifying signals."""
        while self._running:
            try:
                signal = await signal_source.next_signal()
                if signal is not None:
                    await self._process_signal(signal)
            except Exception as e:
                logger.error(f"Signal loop error: {e}")
            await asyncio.sleep(self.config.signal_poll_interval_s)

    # ── Signal processing ────────────────────────────────────────────────────

    async def _process_signal(self, signal: Signal) -> None:
        """Gate signal through risk, then execute entry."""
        if not await self.adapter.is_market_open():
            logger.info(f"Signal {signal.ticker}: market closed, skipping")
            return

        balance = await self.adapter.get_account_balance()
        await self._update_day_start(balance)

        risk_result = self._risk.check(
            signal=signal,
            balance=balance,
            current_positions=self._positions,
        )

        if risk_result.decision != RiskDecision.APPROVE:
            logger.info(
                f"Signal {signal.ticker} rejected: {risk_result.decision} — {risk_result.detail}"
            )
            if self.on_risk_reject:
                await self.on_risk_reject(signal, risk_result.decision, risk_result.detail)
            return

        # handle exit/hold directions without placing entry orders
        if signal.direction == Side.EXIT:
            await self._close_position(signal.ticker, reason="signal_exit")
            return
        if signal.direction == Side.HOLD:
            logger.info(f"Signal {signal.ticker}: direction=hold, no action")
            return

        notional = self._risk.size_notional(signal, balance)
        price = await self.adapter.get_ticker_price(signal.ticker)
        if price <= 0:
            logger.warning(f"Signal {signal.ticker}: bad price {price}, skipping")
            return

        # Check entry zone
        if not (signal.entry_zone_low <= price <= signal.entry_zone_high):
            logger.info(
                f"Signal {signal.ticker}: price {price:.2f} outside entry zone "
                f"[{signal.entry_zone_low:.2f}, {signal.entry_zone_high:.2f}]"
            )
            return

        qty = notional / price
        order = Order(
            ticker=signal.ticker,
            side=signal.direction,
            size=qty,
            order_type=OrderType.MARKET,
            strategy_id=signal.strategy_id,
        )

        if self.config.dry_run:
            logger.info(
                f"DRY RUN — would place: {signal.ticker} {signal.direction} "
                f"qty={qty:.4f} notional={notional:.2f}"
            )
            return

        try:
            fill = await self.adapter.place_order(order)
            logger.info(
                f"Filled: {signal.ticker} {signal.direction} "
                f"qty={fill.size} @ {fill.price} fee={fill.fee}"
            )
            await self._on_entry_fill(signal, fill)
            if self.on_fill:
                await self.on_fill(signal, fill)
        except Exception as e:
            logger.error(f"Order placement failed for {signal.ticker}: {e}")

    async def _on_entry_fill(self, signal: Signal, fill: Fill) -> None:
        """Set up position state and place SL/TP triggers after entry fill."""
        entry_price = fill.price
        sl_price = entry_price * (1 - signal.sl_pct / 100)
        tp_price = entry_price * (1 + signal.tp_pct / 100)
        close_side = Side.SHORT if signal.direction == Side.LONG else Side.LONG

        sl_id: Optional[str] = None
        tp_id: Optional[str] = None

        try:
            sl_trigger = TriggerOrder(
                ticker=signal.ticker,
                side=close_side,
                trigger_type=TriggerType.STOP_LOSS,
                trigger_price=sl_price,
                size=fill.size,
                strategy_id=signal.strategy_id,
            )
            sl_id = await self.adapter.place_trigger_order(sl_trigger)
        except Exception as e:
            logger.error(f"SL placement failed for {signal.ticker}: {e}")

        try:
            tp_trigger = TriggerOrder(
                ticker=signal.ticker,
                side=close_side,
                trigger_type=TriggerType.TAKE_PROFIT,
                trigger_price=tp_price,
                size=fill.size,
                strategy_id=signal.strategy_id,
            )
            tp_id = await self.adapter.place_trigger_order(tp_trigger)
        except Exception as e:
            logger.error(f"TP placement failed for {signal.ticker}: {e}")

        # Register protection levels for synthetic sweep (Alpaca equities)
        if hasattr(self.adapter, "set_protection_levels"):
            self.adapter.set_protection_levels(signal.ticker, sl=sl_price, tp=tp_price)

        pos = Position(
            ticker=signal.ticker,
            symbol=signal.ticker,
            side=signal.direction,
            size=fill.size,
            entry_price=entry_price,
            current_price=entry_price,
            status=PositionStatus.MANAGING,
            strategy_id=signal.strategy_id,
            trigger_ids=[tid for tid in [sl_id, tp_id] if tid],
        )
        self._positions[signal.ticker] = pos

    # ── Reconciler event handling ────────────────────────────────────────────

    async def _handle_reconcile_result(self, result: ReconcileResult) -> None:
        for pos in result.closed:
            closing_fill = result.closing_fills.get(pos.ticker)
            logger.info(
                f"Position closed: {pos.ticker} "
                f"exit_price={closing_fill.price if closing_fill else 'unknown'}"
            )
            if hasattr(self.adapter, "clear_protection_levels"):
                self.adapter.clear_protection_levels(pos.ticker)
            del self._positions[pos.ticker]
            if self.on_close:
                await self.on_close(pos, closing_fill)

        for pos in result.opened:
            logger.info(f"External position detected: {pos.ticker} — tracking")

        for outcome in result.protection_issues:
            logger.warning(f"Protection issue: {outcome.ticker} {outcome.status} {outcome.detail}")

    # ── Helpers ──────────────────────────────────────────────────────────────

    async def _close_position(self, ticker: str, reason: str = "") -> None:
        pos = self._positions.get(ticker)
        if not pos:
            return
        close_order = Order(
            ticker=ticker,
            side=Side.SHORT if pos.side == Side.LONG else Side.LONG,
            size=pos.size,
            order_type=OrderType.MARKET,
            reduce_only=True,
            strategy_id=pos.strategy_id,
        )
        if self.config.dry_run:
            logger.info(f"DRY RUN — would close {ticker} reason={reason}")
            return
        try:
            fill = await self.adapter.place_order(close_order)
            # cancel any open triggers
            for trigger_id in pos.trigger_ids:
                await self.adapter.cancel_order(trigger_id)
            if hasattr(self.adapter, "clear_protection_levels"):
                self.adapter.clear_protection_levels(ticker)
            pos.status = PositionStatus.CLOSED
            pos.closed_at = datetime.now(timezone.utc)
            del self._positions[ticker]
            if self.on_close:
                await self.on_close(pos, fill)
        except Exception as e:
            logger.error(f"Close failed for {ticker}: {e}")

    async def _update_day_start(self, balance: "Balance") -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._day_start_date != today:
            self._day_start_date = today
            self._day_start_equity = balance.equity
            self._risk.reset_day(balance.equity)


class SignalSource:
    """Abstract signal source interface. Implement for Supabase, file, queue, etc."""

    async def next_signal(self) -> Optional[Signal]:
        raise NotImplementedError
