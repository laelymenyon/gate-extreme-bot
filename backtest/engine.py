"""Bar-replay backtester sharing the live decision code.

PHASE 9.

The job of this module is to produce a number that is **allowed to say no**. A backtester
that flatters a strategy is worse than none, because it converts an unprofitable idea into
a funded one. So every modelling choice here resolves against the strategy:

* **Intrabar order is assumed adverse.** A bar is four numbers, not a path. When both the
  stop and a take-profit lie inside one bar's range, the stop is taken. When the
  liquidation price also lies inside it, liquidation is taken first of all. The optimistic
  reading of the same bar is what turns a losing system into a winning backtest.
* **Post-only entries do not always fill.** The entry rests at the signal bar's close and
  fills only if a later bar trades through it, within ``entry_fill_timeout_seconds``.
  Assuming fills would hand the strategy the maker rebate for free — the single largest
  lever on profitability at these stop widths (ARCHITECTURE §5).
* **Fees and funding are charged, not estimated away.** Maker rebate on entry, taker on
  every exit, funding every 8 hours on the open notional.
* **Liquidation is simulated.** At 100x the gap between stop and liquidation is 0.30 %, so
  a stop that is jumped is not a hypothetical; when it happens the loss is the margin, not
  the planned R.
* **A verdict is withheld below ``backtest.min_trades_for_verdict``.** Thirty trades cannot
  distinguish a 40 % win rate from a 55 % one, and reporting a profit factor from that
  sample is how a backtest lies without a single wrong number in it.

**No lookahead by construction.** Decisions at bar *i* are taken from ``candles.head(i+1)``,
the same call the live path makes, so replay and live are one code path rather than two
kept in agreement by hand (Phase 5's rule). The engine never reads ``high``/``low`` of the
bar it is deciding on — only of bars strictly after the decision.

The engine drives the Phase 5-8 stack: :class:`~strategy.signal_engine.SignalEngine` decides
direction, :func:`~risk.position_sizer.plan_position` sizes, the Phase 7 guard vetoes on the
liquidation buffer, and the Phase 6 breakers halt trading. It places no orders and opens no
socket — ``execution/`` is not imported, because a backtest that could reach the exchange is
a backtest that eventually will.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from risk.liquidation_guard import (
    LiquidationParams,
    TierSnapshot,
    assess_plan,
)
from risk.position_sizer import PositionPlan, SizingParams, plan_position
from risk.risk_manager import MemoryRiskStore, RiskManager, RiskParams
from strategy.indicators import Candles, atr
from strategy.signal_engine import EngineParams, SignalEngine, closed_bars, timeframe_seconds

__all__ = [
    "BacktestParams",
    "Trade",
    "Metrics",
    "BacktestResult",
    "WalkForwardResult",
    "slice_candles",
    "BacktestEngine",
    "walk_forward",
]

#: Gate.io charges funding every 8 hours, at 00:00, 08:00 and 16:00 UTC.
FUNDING_INTERVAL_SECONDS = 8 * 3600


@dataclass(frozen=True)
class BacktestParams:
    """Modelling choices from ``backtest``. Defaults mirror ``config.yaml``."""

    fee_taker: float = 0.00075
    fee_maker: float = -0.0001          # a rebate, live-verified
    slippage_model: str = "fixed"       # book | fixed
    fixed_slippage: float = 0.0003
    simulate_liquidation: bool = True
    simulate_funding: bool = True
    funding_rate: float = 0.0001        # per 8h interval, the venue default
    min_trades_for_verdict: int = 1000
    starting_equity: float = 10_000.0
    entry_fill_timeout_seconds: float = 20.0
    train_pct: float = 0.5
    validation_pct: float = 0.25
    test_pct: float = 0.25

    def __post_init__(self) -> None:
        if self.slippage_model not in ("book", "fixed"):
            raise ValueError(f"slippage_model must be 'book' or 'fixed', got {self.slippage_model!r}")
        if self.fixed_slippage < 0:
            raise ValueError("fixed_slippage must be >= 0")
        if self.starting_equity <= 0:
            raise ValueError("starting_equity must be > 0")
        if self.min_trades_for_verdict < 1:
            raise ValueError("min_trades_for_verdict must be >= 1")
        total = self.train_pct + self.validation_pct + self.test_pct
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"walk-forward splits must sum to 1.0, got {total}")
        for name in ("train_pct", "validation_pct", "test_pct"):
            if not 0.0 < getattr(self, name) < 1.0:
                raise ValueError(f"{name} must be in (0, 1)")

    @classmethod
    def from_config(cls, cfg: Any) -> "BacktestParams":
        base = cls()

        def get(name: str, default: Any) -> Any:
            return cfg.get(f"backtest.{name}", default)

        return cls(
            fee_taker=float(get("fee_taker", base.fee_taker)),
            fee_maker=float(get("fee_maker", base.fee_maker)),
            slippage_model=str(get("slippage_model", base.slippage_model)),
            fixed_slippage=float(get("fixed_slippage", base.fixed_slippage)),
            simulate_liquidation=bool(get("simulate_liquidation", base.simulate_liquidation)),
            simulate_funding=bool(get("simulate_funding", base.simulate_funding)),
            min_trades_for_verdict=int(
                get("min_trades_for_verdict", base.min_trades_for_verdict)
            ),
            entry_fill_timeout_seconds=float(
                cfg.get("take_profit.entry_fill_timeout_seconds",
                        base.entry_fill_timeout_seconds)
            ),
            train_pct=float(get("walk_forward.train_pct", base.train_pct)),
            validation_pct=float(get("walk_forward.validation_pct", base.validation_pct)),
            test_pct=float(get("walk_forward.test_pct", base.test_pct)),
        )


@dataclass(frozen=True)
class Trade:
    """One completed round trip, with every cost that was actually charged."""

    symbol: str
    direction: int
    entry_time: float
    exit_time: float
    entry_price: float
    exit_price: float
    size: int                     # signed contract count at entry
    stop_price: float
    exit_reason: str              # stop | liquidation | tp1 | tp2 | tp3 | end_of_data
    gross_pnl: float = 0.0
    fees: float = 0.0
    funding: float = 0.0
    r_multiple: float = 0.0
    equity_after: float = 0.0
    bars_held: int = 0
    score: float = 0.0

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.fees - self.funding

    @property
    def won(self) -> bool:
        return self.net_pnl > 0


@dataclass(frozen=True)
class Metrics:
    """Performance, reported as measured — including the losses."""

    trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = float("nan")
    profit_factor: float = float("nan")
    expectancy: float = float("nan")
    expectancy_r: float = float("nan")
    total_return: float = 0.0
    max_drawdown: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    fees_paid: float = 0.0
    funding_paid: float = 0.0
    liquidations: int = 0
    entries_attempted: int = 0
    entries_filled: int = 0
    final_equity: float = 0.0

    @property
    def fill_rate(self) -> float:
        return self.entries_filled / self.entries_attempted if self.entries_attempted else float("nan")

    def summary(self) -> str:
        return (
            f"{self.trades} trades, win rate {self.win_rate * 100:.1f}%, "
            f"PF {self.profit_factor:.2f}, expectancy {self.expectancy_r:+.3f}R, "
            f"return {self.total_return * 100:+.2f}%, max DD {self.max_drawdown * 100:.2f}%"
        )


@dataclass(frozen=True)
class BacktestResult:
    """What happened, and whether the sample is large enough to mean anything."""

    symbol: str
    metrics: Metrics
    trades: tuple[Trade, ...] = ()
    equity_curve: tuple[tuple[float, float], ...] = ()
    rejections: Mapping[str, int] = field(default_factory=dict)
    bars: int = 0
    verdict: str = ""
    conclusive: bool = False

    def summary(self) -> str:
        return f"{self.symbol}: {self.metrics.summary()}\n  verdict: {self.verdict}"


@dataclass(frozen=True)
class WalkForwardResult:
    """Train / validation / out-of-sample, in that chronological order.

    The out-of-sample window is the only one that carries information about the future.
    Reporting all three together makes the usual failure visible: a strategy that shines in
    training and dies out of sample was fitted, not discovered.
    """

    train: BacktestResult
    validation: BacktestResult
    test: BacktestResult
    verdict: str = ""
    degraded: bool = False

    def summary(self) -> str:
        return (
            f"train      {self.train.metrics.summary()}\n"
            f"validation {self.validation.metrics.summary()}\n"
            f"test (OOS) {self.test.metrics.summary()}\n"
            f"verdict: {self.verdict}"
        )


def slice_candles(candles: Candles, start: int, end: int) -> Candles:
    """The half-open window ``[start, end)``. ``Candles.head`` only gives prefixes."""
    start = max(0, int(start))
    end = min(len(candles), int(end))
    if end < start:
        raise ValueError(f"end {end} precedes start {start}")
    return Candles(
        time=candles.time[start:end],
        open=candles.open[start:end],
        high=candles.high[start:end],
        low=candles.low[start:end],
        close=candles.close[start:end],
        volume=candles.volume[start:end],
        turnover=None if candles.turnover is None else candles.turnover[start:end],
    )


def _metrics(trades: Sequence[Trade], curve: Sequence[tuple[float, float]],
             starting_equity: float, attempted: int, filled: int) -> Metrics:
    if not trades:
        return Metrics(
            entries_attempted=attempted, entries_filled=filled,
            final_equity=curve[-1][1] if curve else starting_equity,
            max_drawdown=_max_drawdown(curve),
        )

    wins = [t for t in trades if t.won]
    losses = [t for t in trades if not t.won]
    gross_win = sum(t.net_pnl for t in wins)
    gross_loss = -sum(t.net_pnl for t in losses)
    final = trades[-1].equity_after

    return Metrics(
        trades=len(trades),
        wins=len(wins),
        losses=len(losses),
        win_rate=len(wins) / len(trades),
        # Infinite rather than a large number when nothing was lost: a profit factor of
        # "inf" reads as "too few losses to judge", which is the honest reading.
        profit_factor=(gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        expectancy=sum(t.net_pnl for t in trades) / len(trades),
        expectancy_r=sum(t.r_multiple for t in trades) / len(trades),
        total_return=(final - starting_equity) / starting_equity,
        max_drawdown=_max_drawdown(curve),
        avg_win=gross_win / len(wins) if wins else 0.0,
        avg_loss=-gross_loss / len(losses) if losses else 0.0,
        fees_paid=sum(t.fees for t in trades),
        funding_paid=sum(t.funding for t in trades),
        liquidations=sum(1 for t in trades if t.exit_reason == "liquidation"),
        entries_attempted=attempted,
        entries_filled=filled,
        final_equity=final,
    )


def _max_drawdown(curve: Sequence[tuple[float, float]]) -> float:
    peak = -float("inf")
    worst = 0.0
    for _, equity in curve:
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, (peak - equity) / peak)
    return worst


class BacktestEngine:
    """Replays bars through the live decision stack.

    ``decide`` defaults to the Phase 5 :class:`SignalEngine` but can be injected, which is
    how the tests pin the engine's *mechanics* — fills, fees, intrabar ordering,
    liquidation — independently of whether the strategy happens to signal on a fixture.
    """

    def __init__(
        self,
        params: BacktestParams | None = None,
        sizing: SizingParams | None = None,
        liquidation: LiquidationParams | None = None,
        risk: RiskParams | None = None,
        engine_params: EngineParams | None = None,
        decide: Callable[..., Any] | None = None,
    ) -> None:
        self.params = params or BacktestParams()
        self.sizing = sizing or SizingParams()
        self.liquidation = liquidation or LiquidationParams()
        self.risk_params = risk or RiskParams()
        self.engine_params = engine_params
        self._decide = decide
        self._signal_engine: SignalEngine | None = None

    @classmethod
    def from_config(cls, cfg: Any, **kwargs: Any) -> "BacktestEngine":
        return cls(
            params=BacktestParams.from_config(cfg),
            sizing=SizingParams.from_config(cfg),
            liquidation=LiquidationParams.from_config(cfg),
            risk=RiskParams.from_config(cfg),
            engine_params=EngineParams.from_config(cfg),
            **kwargs,
        )

    # --- costs -------------------------------------------------------------

    def _slippage(self, price: float) -> float:
        """Adverse price movement on a taker exit, as an absolute price offset."""
        if self.params.slippage_model == "fixed":
            return price * self.params.fixed_slippage
        # The book model needs a live order book, which a bar series does not carry. Rather
        # than invent depth, fall back to the fixed figure and stay explicit about it.
        return price * self.params.fixed_slippage

    def _entry_fee(self, notional: float) -> float:
        """Post-only entry earns the maker rate, which is negative — a rebate."""
        return notional * self.params.fee_maker

    def _exit_fee(self, notional: float) -> float:
        return notional * self.params.fee_taker

    def _funding(self, notional: float, intervals: int) -> float:
        if not self.params.simulate_funding or intervals <= 0:
            return 0.0
        return notional * self.params.funding_rate * intervals

    # --- the replay --------------------------------------------------------

    def run(
        self,
        symbol: str,
        candles: Mapping[str, Candles],
        tiers: Sequence[Any],
        contract: Any,
        btc: Mapping[str, Candles] | None = None,
        entry_timeframe: str | None = None,
        warmup: int | None = None,
    ) -> BacktestResult:
        """Replay ``candles`` bar by bar and report what the strategy actually did."""
        entry_timeframe = entry_timeframe or (
            self.engine_params.timeframes[0] if self.engine_params else "1m"
        )
        series = candles.get(entry_timeframe)
        if series is None or len(series) == 0:
            raise ValueError(f"no {entry_timeframe} candles supplied for {symbol}")

        interval = timeframe_seconds(entry_timeframe)
        warmup = int(warmup if warmup is not None else min(len(series) - 1, 210))

        equity = self.params.starting_equity
        curve: list[tuple[float, float]] = [(float(series.time[0]), equity)]
        trades: list[Trade] = []
        rejections: dict[str, int] = {}
        attempted = filled = 0

        risk_manager = RiskManager(self.risk_params, MemoryRiskStore())
        position: dict[str, Any] | None = None
        pending: dict[str, Any] | None = None

        for index in range(warmup, len(series)):
            bar_time = float(series.time[index])
            now = bar_time + interval          # the bar has just closed

            # --- an open position is resolved before anything new is considered ---
            if position is not None:
                closed, equity = self._resolve_bar(
                    position, series, index, equity, trades, curve
                )
                if closed:
                    risk_manager.record_trade(
                        now=now, pnl=trades[-1].net_pnl, equity=equity
                    )
                    position = None
                else:
                    continue

            # --- a resting entry either fills, expires, or keeps waiting ---------
            if pending is not None:
                outcome = self._resolve_pending(pending, series, index)
                if outcome == "filled":
                    filled += 1
                    position = self._open_position(pending, series, index)
                    pending = None
                    continue
                if outcome == "expired":
                    pending = None
                else:
                    continue

            if position is not None or pending is not None:
                continue

            # --- may we trade at all? -------------------------------------------
            decision = risk_manager.can_trade(
                now=now, equity=equity, open_positions=0, symbol=symbol,
            )
            if not decision.allowed:
                key = decision.breaker.value if decision.breaker else "risk"
                rejections[key] = rejections.get(key, 0) + 1
                continue

            signal = self._signal(symbol, candles, now, btc)
            if signal is None or not getattr(signal, "accepted", False):
                stage = getattr(signal, "stage", "no_signal") or "no_signal"
                rejections[stage] = rejections.get(stage, 0) + 1
                continue

            plan = plan_position(
                symbol=symbol, direction=signal.direction,
                entry_price=float(series.close[index]),
                candles=series.head(index + 1), contract=contract, tiers=tiers,
                equity=equity, available=equity, params=self.sizing,
            )
            if not plan.ok:
                rejections[f"size:{plan.stage}"] = rejections.get(f"size:{plan.stage}", 0) + 1
                continue

            verdict = assess_plan(
                plan, TierSnapshot.of(symbol, tuple(tiers), now), now,
                params=self.liquidation, contract=contract,
            )
            if not verdict.ok:
                key = f"liq:{verdict.stage}"
                rejections[key] = rejections.get(key, 0) + 1
                continue

            attempted += 1
            pending = {
                "plan": plan,
                "limit": float(series.close[index]),
                "direction": plan.direction,
                "placed_index": index,
                "placed_time": bar_time,
                "score": float(getattr(signal, "score", 0.0)),
                "expires_after": max(
                    1, int(self.params.entry_fill_timeout_seconds // interval)
                ),
            }

        # A position still open at the end of the data is closed at the last close, marked
        # so it can be excluded from any statistic that assumes a completed round trip.
        if position is not None:
            equity = self._force_close(position, series, len(series) - 1, equity, trades, curve)

        metrics = _metrics(trades, curve, self.params.starting_equity, attempted, filled)
        verdict, conclusive = self._verdict(metrics)
        return BacktestResult(
            symbol=symbol, metrics=metrics, trades=tuple(trades),
            equity_curve=tuple(curve), rejections=dict(rejections),
            bars=len(series) - warmup, verdict=verdict, conclusive=conclusive,
        )

    # --- decision ----------------------------------------------------------

    def _signal(self, symbol: str, candles: Mapping[str, Candles], now: float,
                btc: Mapping[str, Candles] | None) -> Any:
        if self._decide is not None:
            return self._decide(symbol=symbol, candles=candles, now=now, btc=btc)
        if self._signal_engine is None:
            self._signal_engine = SignalEngine(params=self.engine_params)
        return self._signal_engine.evaluate(symbol, candles, now, btc)

    # --- fills -------------------------------------------------------------

    def _resolve_pending(self, pending: dict[str, Any], series: Candles,
                         index: int) -> str:
        """Did the resting post-only entry fill on this bar?

        A buy limit fills only if the bar traded at or below it. Assuming otherwise would
        hand the strategy the maker rebate for free, which is the single biggest lever on
        whether any of this is profitable.
        """
        limit = pending["limit"]
        if pending["direction"] > 0:
            if float(series.low[index]) <= limit:
                return "filled"
        elif float(series.high[index]) >= limit:
            return "filled"
        if index - pending["placed_index"] >= pending["expires_after"]:
            return "expired"
        return "waiting"

    def _open_position(self, pending: dict[str, Any], series: Candles,
                       index: int) -> dict[str, Any]:
        plan: PositionPlan = pending["plan"]
        entry = pending["limit"]                # post-only fills at its own limit
        # Coin amount per contract comes from the plan rather than the contract spec, so
        # the fill is priced with exactly the multiplier the sizer used.
        per_contract = plan.coin_amount / abs(plan.size) if plan.size else 0.0
        notional = per_contract * abs(plan.size) * entry
        return {
            "plan": plan,
            "direction": plan.direction,
            "size": plan.size,
            "entry_price": entry,
            "entry_time": float(series.time[index]),
            "entry_index": index,
            "stop_price": plan.stop.price if plan.stop else float("nan"),
            "liq_price": self._liquidation_price(plan, entry),
            "notional": notional,
            "per_contract": per_contract,
            "remaining": abs(plan.size),
            "realised": 0.0,
            "fees": self._entry_fee(notional),
            "funding": 0.0,
            "score": pending["score"],
            "targets": self._targets(plan, entry),
        }

    def _liquidation_price(self, plan: PositionPlan, entry: float) -> float:
        distance = float(plan.metrics.get("liquidation_distance", float("nan")))
        if not math.isfinite(distance):
            return float("nan")
        return entry * (1 - distance) if plan.direction > 0 else entry * (1 + distance)

    def _targets(self, plan: PositionPlan, entry: float) -> list[dict[str, Any]]:
        """TP rungs in R of the actual stop, sized to floor with the runner taking the rest."""
        if plan.stop is None:
            return []
        risk = abs(entry - plan.stop.price)
        if not math.isfinite(risk) or risk <= 0:
            return []
        held = abs(plan.size)
        first = int(held * 0.40)
        second = int(held * 0.35)
        runner = held - first - second
        rungs = []
        for name, r_multiple, size in (("tp1", 1.0, first), ("tp2", 2.0, second),
                                       ("tp3", 3.0, runner)):
            if size <= 0:
                continue
            rungs.append({
                "name": name, "r": r_multiple, "size": size,
                "price": entry + plan.direction * r_multiple * risk,
            })
        return rungs

    # --- resolving an open position ---------------------------------------

    def _resolve_bar(self, position: dict[str, Any], series: Candles, index: int,
                     equity: float, trades: list[Trade],
                     curve: list[tuple[float, float]]) -> tuple[bool, float]:
        """Walk one bar against an open position, adversely.

        A bar is four numbers, not a path, so the order in which price visited them is
        unknown. This resolves in the order that is worst for the position — liquidation,
        then stop, then targets — because the opposite assumption is exactly the one that
        makes a losing system backtest profitably.
        """
        direction = position["direction"]
        high = float(series.high[index])
        low = float(series.low[index])
        bar_time = float(series.time[index])

        position["funding"] += self._funding_for_bar(position, series, index)

        liq = position["liq_price"]
        if self.params.simulate_liquidation and math.isfinite(liq):
            hit = low <= liq if direction > 0 else high >= liq
            if hit:
                equity = self._close(position, liq, bar_time, index, "liquidation",
                                     equity, trades, curve, slipped=False)
                return True, equity

        stop = position["stop_price"]
        if math.isfinite(stop):
            hit = low <= stop if direction > 0 else high >= stop
            if hit:
                equity = self._close(position, stop, bar_time, index, "stop",
                                     equity, trades, curve, slipped=True)
                return True, equity

        for rung in list(position["targets"]):
            reached = high >= rung["price"] if direction > 0 else low <= rung["price"]
            if not reached:
                continue
            position["targets"].remove(rung)
            if rung["name"] == "tp3" or rung["size"] >= position["remaining"]:
                equity = self._close(position, rung["price"], bar_time, index,
                                     rung["name"], equity, trades, curve, slipped=True)
                return True, equity
            equity = self._partial(position, rung, bar_time, equity, curve)

        return False, equity

    def _funding_for_bar(self, position: dict[str, Any], series: Candles,
                         index: int) -> float:
        """Funding accrues on every 8h boundary the position was open across."""
        if not self.params.simulate_funding:
            return 0.0
        previous = float(series.time[index - 1]) if index else float(series.time[index])
        current = float(series.time[index])
        crossings = int(current // FUNDING_INTERVAL_SECONDS) - int(
            previous // FUNDING_INTERVAL_SECONDS
        )
        return self._funding(position["notional"], max(0, crossings))

    def _partial(self, position: dict[str, Any], rung: Mapping[str, Any], bar_time: float,
                 equity: float, curve: list[tuple[float, float]]) -> float:
        """Take part of the position off at a target. The rest keeps running."""
        size = int(rung["size"])
        price = float(rung["price"]) - position["direction"] * self._slippage(
            float(rung["price"])
        )
        pnl = position["direction"] * (price - position["entry_price"]) * (
            size * position["per_contract"]
        )
        fee = self._exit_fee(size * position["per_contract"] * price)
        position["realised"] += pnl
        position["fees"] += fee
        position["remaining"] -= size
        equity += pnl - fee
        curve.append((bar_time, equity))
        return equity

    def _close(self, position: dict[str, Any], price: float, bar_time: float, index: int,
               reason: str, equity: float, trades: list[Trade],
               curve: list[tuple[float, float]], *, slipped: bool) -> float:
        direction = position["direction"]
        # Liquidation is not a fill we get to slip on; the venue takes the position at its
        # own price. Every other exit is a taker order and pays the spread.
        fill = price - direction * self._slippage(price) if slipped else price
        size = int(position["remaining"])
        coins = size * position["per_contract"]
        pnl = direction * (fill - position["entry_price"]) * coins
        fee = self._exit_fee(abs(coins) * fill)

        gross = position["realised"] + pnl
        fees = position["fees"] + fee
        funding = position["funding"]
        equity += pnl - fee

        risk_amount = abs(
            position["entry_price"] - position["stop_price"]
        ) * abs(position["plan"].size) * position["per_contract"]
        net = gross - fees - funding
        trades.append(Trade(
            symbol=position["plan"].symbol,
            direction=direction,
            entry_time=position["entry_time"],
            exit_time=bar_time,
            entry_price=position["entry_price"],
            exit_price=fill,
            size=position["size"],
            stop_price=position["stop_price"],
            exit_reason=reason,
            gross_pnl=gross,
            fees=fees,
            funding=funding,
            r_multiple=net / risk_amount if risk_amount > 0 else 0.0,
            equity_after=equity,
            bars_held=index - position["entry_index"],
            score=position["score"],
        ))
        curve.append((bar_time, equity))
        return equity

    def _force_close(self, position: dict[str, Any], series: Candles, index: int,
                     equity: float, trades: list[Trade],
                     curve: list[tuple[float, float]]) -> float:
        return self._close(position, float(series.close[index]), float(series.time[index]),
                           index, "end_of_data", equity, trades, curve, slipped=True)

    # --- the verdict -------------------------------------------------------

    def _verdict(self, metrics: Metrics) -> tuple[str, bool]:
        """State plainly whether the sample supports any claim at all.

        ``backtest.min_trades_for_verdict`` exists because a profit factor computed from
        thirty trades is noise wearing a decimal point. Below it the engine refuses to call
        a result, however good the number looks.
        """
        if metrics.trades == 0:
            return (
                "no trades: the filters never admitted one. That is a possible outcome of "
                "a score-80 threshold, not necessarily a fault.",
                False,
            )
        if metrics.trades < self.params.min_trades_for_verdict:
            return (
                f"INCONCLUSIVE: {metrics.trades} trades is below the "
                f"{self.params.min_trades_for_verdict} needed to distinguish edge from "
                f"noise. Reported figures describe this sample only and must not be "
                f"treated as an expectancy.",
                False,
            )
        if metrics.expectancy_r > 0 and metrics.profit_factor > 1.0:
            return (
                f"positive over {metrics.trades} trades: {metrics.expectancy_r:+.3f}R per "
                f"trade, PF {metrics.profit_factor:.2f}. Out-of-sample confirmation is "
                f"still required before this means anything live.",
                True,
            )
        return (
            f"NEGATIVE over {metrics.trades} trades: {metrics.expectancy_r:+.3f}R per "
            f"trade, PF {metrics.profit_factor:.2f}. The strategy loses money on this "
            f"data; leverage would only lose it faster.",
            True,
        )


def walk_forward(engine: BacktestEngine, symbol: str, candles: Mapping[str, Candles],
                 tiers: Sequence[Any], contract: Any,
                 btc: Mapping[str, Candles] | None = None,
                 entry_timeframe: str = "1m", warmup: int | None = None) -> WalkForwardResult:
    """Split chronologically into train / validation / out-of-sample and run each.

    The split is by time, never randomly: shuffling bars would let a window learn from its
    own future, which is the most flattering bug a backtest can have. Only the final
    window carries information about performance on unseen data, and a strategy that is
    strong in training and weak out of sample was fitted rather than discovered — which is
    what ``degraded`` reports.
    """
    series = candles.get(entry_timeframe)
    if series is None or len(series) == 0:
        raise ValueError(f"no {entry_timeframe} candles supplied for {symbol}")

    total = len(series)
    train_end = int(total * engine.params.train_pct)
    validation_end = train_end + int(total * engine.params.validation_pct)

    def window(start: int, end: int) -> BacktestResult:
        sliced = {
            timeframe: slice_candles(
                data,
                int(len(data) * start / total),
                int(len(data) * end / total),
            )
            for timeframe, data in candles.items()
        }
        sliced_btc = None if btc is None else {
            timeframe: slice_candles(
                data, int(len(data) * start / total), int(len(data) * end / total)
            )
            for timeframe, data in btc.items()
        }
        return engine.run(symbol, sliced, tiers, contract, sliced_btc,
                          entry_timeframe=entry_timeframe, warmup=warmup)

    train = window(0, train_end)
    validation = window(train_end, validation_end)
    test = window(validation_end, total)

    degraded = (
        train.metrics.trades > 0 and test.metrics.trades > 0
        and train.metrics.expectancy_r > 0 >= test.metrics.expectancy_r
    )
    if degraded:
        verdict = (
            "OVERFIT: positive in training, non-positive out of sample. The in-sample "
            "result described this data, not the market."
        )
    elif test.metrics.trades == 0:
        verdict = "no out-of-sample trades; nothing can be concluded about unseen data."
    elif not test.conclusive:
        verdict = f"out-of-sample {test.verdict}"
    else:
        verdict = f"out-of-sample {test.verdict}"
    return WalkForwardResult(train=train, validation=validation, test=test,
                             verdict=verdict, degraded=degraded)
