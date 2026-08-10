"""The paper-trading loop: every layer wired together, against a simulated exchange.

PHASE 10.

Phases 5-9 each answer one question and stop. This module is the first place they run as a
system, in the order a live run would use:

    market data -> risk breakers -> signal -> size -> liquidation guard -> entry -> protect

Its value is not another set of statistics — Phase 9 already measures the strategy. Its
value is that it exercises the parts a backtest deliberately never touches: the order state
machine, the SL-first sequence, protective orders resting on an exchange, and the way those
behave when an entry does not fill or a stop cannot be verified. A backtest computes what a
trade *would* have earned; this finds out whether the bot can actually carry one.

**It cannot trade for real.** Paper mode is not a flag on the live path, it is a different
gateway: :meth:`OrderManager.for_config` hands back a :class:`SimulatedGateway` whenever the
three safety switches disagree, which in ``--mode paper`` they always do. Beyond that,
:class:`PaperTrader` **refuses to construct** when ``Config.live_enabled`` is true, so
"accidentally ran the paper loop against the real exchange" is not a reachable state rather
than one guarded by a conditional.

Market data is a protocol. :class:`ReplayMarketSource` walks recorded candles, which is what
the tests and offline paper runs use; :class:`RestMarketSource` pulls live candles through
the Phase 2 client, whose reads stay available while the write-guard is shut. Either way the
decisions come from the same Phase 5 engine and the same ``closed_bars`` rule, so a paper run
and a live run differ only in where the fills come from.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

from execution.order_manager import (
    ExecutionParams,
    OrderManager,
    OrderState,
    SimulatedGateway,
)
from execution.protection import ProtectionEngine, ProtectionParams
from risk.liquidation_guard import LiquidationParams, TierSnapshot, assess_plan
from risk.position_sizer import SizingParams, plan_position
from risk.risk_manager import MemoryRiskStore, RiskManager, RiskParams, RiskStore
from strategy.indicators import Candles
from strategy.signal_engine import EngineParams, SignalEngine

log = logging.getLogger(__name__)

__all__ = [
    "MarketSource",
    "ReplayMarketSource",
    "RestMarketSource",
    "PaperFill",
    "PaperTrade",
    "PaperReport",
    "PaperTrader",
]


class LiveTradingRefused(Exception):
    """Raised when the paper loop is pointed at an open safety gate."""


class MarketSource(Protocol):
    """Where the loop gets its view of the market."""

    def now(self) -> float: ...

    def candles(self, symbol: str) -> Mapping[str, Candles]: ...

    def mark_price(self, symbol: str) -> float: ...


class ReplayMarketSource:
    """Walks recorded candles one bar at a time.

    Used by the tests and by offline paper runs. The window it exposes ends at the bar the
    cursor is on, so the loop sees exactly what a live run would have seen at that instant —
    the same ``head``-based truncation Phase 5 uses, applied at the source rather than
    trusted downstream.
    """

    def __init__(self, candles: Mapping[str, Candles], entry_timeframe: str = "1m",
                 start: int = 0) -> None:
        if entry_timeframe not in candles:
            raise ValueError(f"no {entry_timeframe} candles supplied")
        self._all = dict(candles)
        self.entry_timeframe = entry_timeframe
        self.cursor = int(start)
        self._interval = self._infer_interval(candles[entry_timeframe])

    @staticmethod
    def _infer_interval(series: Candles) -> float:
        if len(series) < 2:
            return 60.0
        return float(series.time[1] - series.time[0])

    @property
    def exhausted(self) -> bool:
        return self.cursor >= len(self._all[self.entry_timeframe])

    def advance(self) -> bool:
        self.cursor += 1
        return not self.exhausted

    def now(self) -> float:
        """The instant the current entry bar closed."""
        series = self._all[self.entry_timeframe]
        index = min(self.cursor, len(series) - 1)
        return float(series.time[index]) + self._interval

    def candles(self, symbol: str) -> Mapping[str, Candles]:
        # Every timeframe is truncated to the wall-clock instant, not to a bar count, so a
        # slower timeframe cannot leak a bar that has not closed yet.
        moment = self.now()
        view = {}
        for timeframe, series in self._all.items():
            usable = int((series.time < moment).sum())
            view[timeframe] = series.head(usable)
        return view

    def mark_price(self, symbol: str) -> float:
        series = self._all[self.entry_timeframe]
        index = min(self.cursor, len(series) - 1)
        return float(series.close[index])

    def bar(self, symbol: str) -> tuple[float, float]:
        """High and low of the current bar, for driving the simulated exchange."""
        series = self._all[self.entry_timeframe]
        index = min(self.cursor, len(series) - 1)
        return float(series.high[index]), float(series.low[index])


class RestMarketSource:
    """Live candles through the Phase 2 client. Reads only — no write is ever attempted.

    The write-guard makes this safe by construction: the client refuses every POST while the
    gate is shut, before a socket opens. Paper mode therefore watches the real market and
    trades an imaginary account, which is the combination worth validating.
    """

    def __init__(self, client: Any, timeframes: Sequence[str] = ("1m", "5m", "15m", "1h"),
                 limit: int = 300, clock: Any = None) -> None:
        self._client = client
        self._timeframes = tuple(timeframes)
        self._limit = int(limit)
        self._cache: dict[str, Mapping[str, Candles]] = {}
        self._clock = clock

    def now(self) -> float:
        if self._clock is not None:
            return float(self._clock())
        import time

        return time.time()

    async def refresh(self, symbol: str) -> None:
        frames = {}
        for timeframe in self._timeframes:
            rows = await self._client.get_candlesticks(symbol, timeframe, self._limit)
            frames[timeframe] = Candles.from_gate(rows)
        self._cache[symbol] = frames

    def candles(self, symbol: str) -> Mapping[str, Candles]:
        try:
            return self._cache[symbol]
        except KeyError:
            raise RuntimeError(f"call refresh({symbol!r}) before reading candles") from None

    def mark_price(self, symbol: str) -> float:
        series = self.candles(symbol)[self._timeframes[0]]
        return float(series.close[-1])


@dataclass(frozen=True)
class PaperFill:
    """One reduction of an open position, priced at whatever protective order fired."""

    reason: str            # stop | tp1 | tp2 | tp3 | manual
    price: float
    size: int              # contracts closed, always positive
    pnl: float
    fee: float


@dataclass(frozen=True)
class PaperTrade:
    """A completed round trip in the paper account.

    The field list is deliberately a superset of what the loop itself needs: ``r_multiple``,
    ``margin``, ``liquidation_price`` and ``funding`` exist because
    :meth:`~database.models.TradeRecord.from_paper` reads them off whatever trade object it
    is handed. A missing attribute there does not raise — it silently stores ``0.0`` — so a
    field absent here becomes a zero in the audit trail and, for ``r_multiple``, a zero in
    the expectancy that decides the verdict. Phase 13 found exactly that.
    """

    symbol: str
    direction: int
    entry_time: float
    exit_time: float
    entry_price: float
    exit_price: float
    size: int
    stop_price: float
    exit_reason: str
    gross_pnl: float
    fees: float
    net_pnl: float
    equity_after: float
    score: float = 0.0
    fills: tuple[PaperFill, ...] = ()
    #: Net PnL over the cash the stop put at risk — the same definition Phase 9 uses
    #: (`backtest/engine.py`), so paper and backtest R are one number and comparable.
    r_multiple: float = 0.0
    #: Margin actually locked by the position, and the liquidation price the guard sized
    #: against. Both are reported rather than recomputed downstream.
    margin: float = 0.0
    liquidation_price: float = 0.0
    #: Always 0.0 on paper: no position is held across a funding stamp by this loop.
    #: Present so the column exists and is honest rather than absent.
    funding: float = 0.0

    @property
    def won(self) -> bool:
        return self.net_pnl > 0


@dataclass
class PaperReport:
    """What the loop did. Counters are cumulative across the run."""

    symbol: str = ""
    steps: int = 0
    equity: float = 0.0
    starting_equity: float = 0.0
    trades: list[PaperTrade] = field(default_factory=list)
    rejections: dict[str, int] = field(default_factory=dict)
    entries_attempted: int = 0
    entries_filled: int = 0
    entries_expired: int = 0
    protection_failures: int = 0
    flattened: int = 0

    @property
    def net_pnl(self) -> float:
        return self.equity - self.starting_equity

    @property
    def wins(self) -> int:
        return sum(1 for trade in self.trades if trade.won)

    def count(self, stage: str) -> None:
        self.rejections[stage] = self.rejections.get(stage, 0) + 1

    def summary(self) -> str:
        won = self.wins
        total = len(self.trades)
        rate = f"{won / total * 100:.0f}%" if total else "n/a"
        return (
            f"{self.symbol}: {self.steps} steps, {self.entries_attempted} entries "
            f"({self.entries_filled} filled, {self.entries_expired} expired), "
            f"{total} trades won {rate}, equity {self.equity:.2f} "
            f"({self.net_pnl:+.2f})"
        )


class PaperTrader:
    """Runs the whole stack against a simulated exchange, one step per bar or poll.

    Construction refuses an open safety gate. Everything else is a veto that is counted and
    moved past: a refused breaker, an unsignalled bar, an unsizable account, a guard veto,
    an unfilled entry. The loop's normal state is doing nothing, and that is reported rather
    than hidden.
    """

    def __init__(
        self,
        config: Any,
        source: MarketSource,
        symbol: str,
        tiers: Sequence[Any],
        contract: Any,
        *,
        starting_equity: float = 10_000.0,
        risk_store: RiskStore | None = None,
        client: Any = None,
        engine: SignalEngine | None = None,
        decide: Any = None,
    ) -> None:
        if getattr(config, "live_enabled", False):
            # Not a conditional inside the trading path — a refusal to exist. The paper
            # loop must never be the thing that reaches the real exchange.
            raise LiveTradingRefused(
                "the safety gate is OPEN (DRY_RUN=false, --mode live, --confirm-live). "
                "PaperTrader simulates fills and must not run against a live gate; use the "
                "live runner deliberately instead."
            )

        self.config = config
        self.source = source
        self.symbol = symbol
        self.tiers = tuple(tiers)
        self.contract = contract
        self._decide = decide

        self.sizing = SizingParams.from_config(config)
        self.liquidation = LiquidationParams.from_config(config)
        self.protection_params = ProtectionParams.from_config(config)
        self.engine = engine or SignalEngine(
            params=EngineParams.from_config(config)
        )
        self.risk = RiskManager(
            RiskParams.from_config(config), risk_store or MemoryRiskStore()
        )
        self._sim_clock = 0.0
        self.orders = OrderManager.for_config(
            config, client=client, params=ExecutionParams.from_config(config),
            last_price=0.0, clock=lambda: self._sim_clock, sleep=self._wait_for_market,
        )
        if not isinstance(self.orders.gateway, SimulatedGateway):
            raise LiveTradingRefused(
                "the order manager resolved to a live gateway; paper trading requires the "
                "simulator"
            )
        self.protection = ProtectionEngine(self.orders, self.protection_params)

        self.report = PaperReport(
            symbol=symbol, equity=float(starting_equity),
            starting_equity=float(starting_equity),
        )
        self._position: dict[str, Any] | None = None
        self._nonce = 0

    # --- helpers -----------------------------------------------------------

    @property
    def gateway(self) -> SimulatedGateway:
        return self.orders.gateway  # type: ignore[return-value]

    async def _wait_for_market(self, seconds: float) -> None:
        """Advance the replay while an entry rests, instead of sleeping on the wall clock.

        This is what makes the post-only timeout mean something on paper: the order manager
        polls, and between polls the market actually moves, so the cancel-after-timeout path
        is exercised against real bars rather than against a clock nobody watched.
        """
        self._sim_clock += float(seconds)
        advance = getattr(self.source, "advance", None)
        if advance is None or not advance():
            return
        for price in self._prices_this_step(self.source.mark_price(self.symbol)):
            self.gateway.advance(price)

    def _next_nonce(self) -> int:
        self._nonce += 1
        return self._nonce

    def _fee(self, notional: float, maker: bool) -> float:
        rate = (
            float(self.config.get("backtest.fee_maker", -0.0001)) if maker
            else float(self.config.get("backtest.fee_taker", 0.00075))
        )
        return notional * rate

    # --- observation (read-only) -------------------------------------------

    @property
    def open_position(self) -> Mapping[str, Any] | None:
        """The position the loop currently carries, or None. Read-only."""
        return self._position

    def unrealised_pnl(self, mark: float) -> float:
        """Open-position PnL at ``mark``. Zero when flat."""
        position = self._position
        if position is None:
            return 0.0
        coins = abs(int(position["remaining"])) * position["per_contract"]
        return position["direction"] * (float(mark) - position["entry_price"]) * coins

    def mark_to_market(self, mark: float | None = None) -> float:
        """Equity including the open position's unrealised PnL.

        ``report.equity`` only moves when something fills, so a curve built from it alone
        is a curve of *realised* equity — and at 100x the drawdown that decides survival
        arrives through an open position's mark price, before any fill (ARCHITECTURE §18).
        Sampling this instead is what makes a recorded equity curve mean what the
        dashboard claims it means.
        """
        if mark is None:
            mark = self.source.mark_price(self.symbol)
        return self.report.equity + self.unrealised_pnl(mark)

    # --- the step ----------------------------------------------------------

    async def step(self) -> PaperReport:
        """One iteration: settle what happened, then decide whether to do anything new."""
        self.report.steps += 1
        now = self.source.now()
        mark = self.source.mark_price(self.symbol)

        # Drive the simulated exchange with the bar's real extremes where the source can
        # supply them, so a resting stop is tested against the wick that would have hit it
        # rather than only against the close.
        for price in self._prices_this_step(mark):
            self.gateway.advance(price)

        await self._settle(now, mark)

        if self._position is not None:
            return self.report

        decision = self.risk.can_trade(
            now=now, equity=self.report.equity, open_positions=0, symbol=self.symbol,
        )
        if not decision.allowed:
            self.report.count(decision.breaker.value if decision.breaker else "risk")
            return self.report

        candles = self.source.candles(self.symbol)
        signal = self._signal(candles, now)
        if signal is None or not getattr(signal, "accepted", False):
            self.report.count(getattr(signal, "stage", "no_signal") or "no_signal")
            return self.report

        plan = plan_position(
            symbol=self.symbol, direction=signal.direction, entry_price=mark,
            candles=candles[self.engine.entry_timeframe], contract=self.contract,
            tiers=self.tiers, equity=self.report.equity, available=self.report.equity,
            params=self.sizing,
        )
        if not plan.ok:
            self.report.count(f"size:{plan.stage}")
            return self.report

        verdict = assess_plan(
            plan, TierSnapshot.of(self.symbol, self.tiers, now), now,
            params=self.liquidation, contract=self.contract,
        )
        if not verdict.ok:
            self.report.count(f"liq:{verdict.stage}")
            return self.report

        await self._enter(plan, now, float(getattr(signal, "score", 0.0)), verdict)
        return self.report

    def _prices_this_step(self, mark: float) -> tuple[float, ...]:
        """The prices to walk the simulator through this step.

        When the source knows the bar's high and low, both are used — adverse extreme
        first, matching the backtester's rule that a bar is four numbers and not a path.
        """
        bar = getattr(self.source, "bar", None)
        if bar is None:
            return (mark,)
        high, low = bar(self.symbol)
        if self._position is None:
            return (low, high, mark)
        return (low, high, mark) if self._position["direction"] > 0 else (high, low, mark)

    def _signal(self, candles: Mapping[str, Candles], now: float) -> Any:
        if self._decide is not None:
            return self._decide(symbol=self.symbol, candles=candles, now=now, btc=None)
        return self.engine.evaluate(self.symbol, candles, now)

    # --- entry and protection ---------------------------------------------

    async def _enter(self, plan: Any, now: float, score: float,
                     verdict: Any = None) -> None:
        self.report.entries_attempted += 1
        nonce = self._next_nonce()

        # A post-only order rests at the touch, one tick better than the mark, and fills
        # only if the market comes to it. Submitting *at* the mark would fill on equality
        # the instant it is placed, which quietly hands every paper run the maker rebate
        # and erases the unfilled-entry outcome the design depends on being frequent.
        tick = float(getattr(self.contract, "order_price_round", 0.0) or 0.0)
        limit = plan.entry_price - plan.direction * tick

        record = await self.orders.submit_entry(
            self.symbol, plan.size, str(limit), nonce,
        )
        if record.state is OrderState.EXPIRED:
            # The normal outcome for post-only. Not an error, and not a reason to chase.
            self.report.entries_expired += 1
            self.report.count("entry:expired")
            return
        if not record.state.has_exposure:
            self.report.count(f"entry:{record.state.value}")
            return

        self.report.entries_filled += 1
        entry_price = record.average_price or plan.entry_price
        size = record.filled_size or plan.size
        per_contract = plan.coin_amount / abs(plan.size) if plan.size else 0.0
        notional = abs(size) * per_contract * entry_price
        # The entry fee hits the account the moment the entry fills, not when the trade
        # closes. It is negative on a post-only fill — the maker rebate — and leaving it
        # uncredited would understate every paper result by the exact amount the design
        # relies on (ARCHITECTURE §5).
        entry_fee = self._fee(notional, maker=True)
        self.report.equity -= entry_fee

        result = await self.protection.protect(
            self.symbol, plan.direction, entry_price, plan.stop.price, size, nonce,
        )
        if not result.ok:
            self.report.protection_failures += 1
            if result.flattened:
                self.report.flattened += 1
                self.report.count("protection:flattened")
                # The engine market-closed the position; the exit is a taker order. The
                # entry fee was already charged above. This is a loss, and it is meant to
                # be: an unprotected 100x position is not worth its expected value.
                exit_fee = self._fee(notional, maker=False)
                self.report.equity -= exit_fee
                self.risk.record_trade(now=now, pnl=-(entry_fee + exit_fee),
                                       equity=self.report.equity)
            else:
                self.report.count("protection:unprotected")
            return

        self._position = {
            "direction": plan.direction,
            "entry_price": entry_price,
            "entry_time": now,
            "size": size,
            "remaining": abs(size),
            "per_contract": per_contract,
            "stop_price": plan.stop.price,
            "stop_order_id": result.stop_order_id,
            "fees": entry_fee,
            "realised": 0.0,
            "score": score,
            "fills": [],
            # Carried so the closed trade can report what it actually risked. The margin
            # is the plan's; the liquidation price is the guard's own conservative figure,
            # not a second derivation that could disagree with the one that authorised
            # the trade.
            "margin": float(getattr(plan, "margin", 0.0) or 0.0),
            "liquidation_price": float(getattr(verdict, "liq_price", 0.0) or 0.0),
            "levels": {result.stop_order_id: ("stop", plan.stop.price)}
            | {leg.order_id: (leg.name, leg.price) for leg in result.take_profits
               if leg.order_id},
        }

    # --- settlement --------------------------------------------------------

    async def _settle(self, now: float, mark: float) -> None:
        """Find out from the exchange what the protective orders did."""
        if self._position is None:
            return

        held = abs(await self.orders.position_size(self.symbol))
        if held == abs(self._position["remaining"]):
            return

        open_ids = {
            str(order.get("id"))
            for order in await self.gateway.list_price_orders(self.symbol)
        }
        fired = [
            (order_id, level) for order_id, level in self._position["levels"].items()
            if order_id and order_id not in open_ids
        ]
        # Resolve in the order the exchange would have: whichever level is nearest the
        # entry is the one price reached first.
        fired.sort(key=lambda item: abs(item[1][1] - self._position["entry_price"]))

        closed = abs(self._position["remaining"]) - held
        for order_id, (name, price) in fired:
            if closed <= 0:
                break
            chunk = closed if name in ("stop", "tp3") else min(closed, self._chunk(name))
            self._book_fill(name, price, chunk)
            closed -= chunk
            self._position["levels"].pop(order_id, None)
        if closed > 0:
            self._book_fill("unknown", mark, closed)

        self._position["remaining"] = held
        if held == 0:
            self._close_trade(now)

    def _chunk(self, name: str) -> int:
        fractions = {
            "tp1": self.protection_params.tp1_close_pct,
            "tp2": self.protection_params.tp2_close_pct,
        }
        return int(abs(self._position["size"]) * fractions.get(name, 1.0))

    def _book_fill(self, reason: str, price: float, size: int) -> None:
        position = self._position
        assert position is not None
        coins = size * position["per_contract"]
        pnl = position["direction"] * (price - position["entry_price"]) * coins
        fee = self._fee(abs(coins) * price, maker=False)
        position["realised"] += pnl
        position["fees"] += fee
        position["fills"].append(PaperFill(reason, price, size, pnl, fee))
        self.report.equity += pnl - fee

    def _close_trade(self, now: float) -> None:
        position = self._position
        assert position is not None
        fills = tuple(position["fills"])
        gross = position["realised"]
        fees = position["fees"]
        net = gross - fees
        last = fills[-1] if fills else None

        # The cash the stop actually put at risk, by Phase 9's definition
        # (`backtest/engine.py`): the stop distance over the size held, in coin terms.
        # `per_contract` is what makes it currency rather than contracts — the quanto
        # multiplier is not optional, and omitting it is wrong by a factor of 10,000 on
        # BTC. Paper and backtest R are therefore the same measurement.
        risk_amount = (
            abs(position["entry_price"] - position["stop_price"])
            * abs(int(position["size"]))
            * position["per_contract"]
        )

        trade = PaperTrade(
            symbol=self.symbol,
            direction=position["direction"],
            entry_time=position["entry_time"],
            exit_time=now,
            entry_price=position["entry_price"],
            exit_price=last.price if last else position["entry_price"],
            size=position["size"],
            stop_price=position["stop_price"],
            exit_reason=last.reason if last else "unknown",
            gross_pnl=gross,
            fees=fees,
            net_pnl=net,
            equity_after=self.report.equity,
            score=position["score"],
            fills=fills,
            r_multiple=net / risk_amount if risk_amount > 0 else 0.0,
            margin=position["margin"],
            liquidation_price=position["liquidation_price"],
        )
        self.report.trades.append(trade)
        self.risk.record_trade(now=now, pnl=net, equity=self.report.equity,
                               symbol=self.symbol)
        self._position = None

    # --- driving -----------------------------------------------------------

    async def run(self, steps: int | None = None, poll_seconds: float = 0.0,
                  on_step: Any = None) -> PaperReport:
        """Step until the source runs out, or ``steps`` iterations have run.

        ``on_step`` is called with this trader after every step, before the source
        advances. It exists so an observer can sample state *during* the run — Phase 13
        records the equity curve through it — without a second driving loop that could
        advance the source differently from this one.
        """
        taken = 0
        while steps is None or taken < steps:
            await self.step()
            if on_step is not None:
                on_step(self)
            taken += 1
            advance = getattr(self.source, "advance", None)
            if advance is not None and not advance():
                break
            if poll_seconds:
                await asyncio.sleep(poll_seconds)
        return self.report
