"""Performance analytics, losses included.

PHASE 11.

The metrics that flatter a strategy are the easy ones. Total PnL says nothing about how it
was earned, and a win rate says nothing without the payoff. So this module computes the set
that can embarrass the bot — profit factor, expectancy in R, max drawdown, max consecutive
losses, fee drag, liquidation distance — and reports them as measured (ARCHITECTURE §7).

Three refusals worth stating outright:

* **No verdict on a small sample.** Below ``backtest.min_trades_for_verdict`` the summary
  says so. A profit factor from thirty trades is noise wearing a decimal point, and Phase 9
  already refuses on the same threshold; the dashboard must not quietly disagree.
* **Nothing is annualised or extrapolated.** A 3-day paper run does not imply a yearly
  return, and printing one would be an invention.
* **Drawdown comes from the equity curve, not from closed trades.** At 100x a drawdown
  arrives through an open position's mark price; a curve rebuilt from closed trades
  understates the worst moment, which is the number that decides whether the account
  survives.

Read-only over :class:`~database.models.TradeStore`. It computes and formats; it does not
trade, and it cannot.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from database.models import EquityPoint, TradeRecord, TradeStore

__all__ = [
    "Performance",
    "compute",
    "max_consecutive_losses",
    "drawdown_from_curve",
    "liquidation_distance_pct",
    "Dashboard",
]


def max_consecutive_losses(trades: Sequence[TradeRecord]) -> int:
    """The longest losing streak. A breakeven trade neither extends nor breaks it."""
    worst = streak = 0
    for trade in trades:
        if trade.pnl < 0:
            streak += 1
            worst = max(worst, streak)
        elif trade.pnl > 0:
            streak = 0
    return worst


def drawdown_from_curve(curve: Sequence[EquityPoint]) -> tuple[float, float]:
    """``(max_drawdown_fraction, peak_equity)`` measured from the running high-water mark."""
    peak = -math.inf
    worst = 0.0
    for point in curve:
        peak = max(peak, point.equity)
        if peak > 0:
            worst = max(worst, (peak - point.equity) / peak)
    return worst, (peak if peak > -math.inf else 0.0)


def liquidation_distance_pct(mark_price: float, liq_price: float) -> float:
    """How far the position is from liquidation, as a fraction of mark.

    Reported because it is the number that decides survival at 100x, and it is not derivable
    from PnL. NaN when either input is unusable — an unknown distance must not read as a
    comfortable one.
    """
    if not (math.isfinite(mark_price) and math.isfinite(liq_price)) or mark_price <= 0 \
            or liq_price <= 0:
        return float("nan")
    return abs(mark_price - liq_price) / mark_price


@dataclass(frozen=True)
class Performance:
    """Everything the §22 contract asks for, computed as measured."""

    trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = float("nan")
    loss_rate: float = float("nan")
    profit_factor: float = float("nan")
    expectancy: float = float("nan")
    expectancy_r: float = float("nan")
    avg_win: float = 0.0
    avg_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    max_consecutive_losses: int = 0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    net_pnl: float = 0.0
    fees_paid: float = 0.0
    funding_paid: float = 0.0
    realised_pnl: float = 0.0
    daily_pnl: float = 0.0
    trades_today: int = 0
    liquidations: int = 0
    max_drawdown: float = 0.0
    peak_equity: float = 0.0
    equity: float = 0.0
    starting_equity: float = 0.0
    total_return: float = 0.0
    conclusive: bool = False
    min_trades_for_verdict: int = 1000
    by_regime: Mapping[str, int] = field(default_factory=dict)
    by_exit_reason: Mapping[str, int] = field(default_factory=dict)

    @property
    def fee_drag_r(self) -> float:
        """Fees as a multiple of average risk — the §5 number, measured rather than assumed."""
        if not self.trades or not self.expectancy_r:
            return float("nan")
        return self.fees_paid / self.trades

    def verdict(self) -> str:
        if self.trades == 0:
            return ("no trades recorded. For a score-80 filter that is a possible outcome, "
                    "not necessarily a fault.")
        if not self.conclusive:
            return (f"INCONCLUSIVE: {self.trades} trades is below the "
                    f"{self.min_trades_for_verdict} needed to tell edge from noise. These "
                    f"figures describe this sample only.")
        if self.expectancy_r > 0 and self.profit_factor > 1.0:
            return (f"positive over {self.trades} trades: {self.expectancy_r:+.3f}R each, "
                    f"PF {self.profit_factor:.2f}.")
        return (f"NEGATIVE over {self.trades} trades: {self.expectancy_r:+.3f}R each, "
                f"PF {self.profit_factor:.2f}. Leverage would only lose it faster.")


def compute(trades: Sequence[TradeRecord], curve: Sequence[EquityPoint] = (),
            *, starting_equity: float = 0.0, now: float | None = None,
            min_trades_for_verdict: int = 1000) -> Performance:
    """Reduce a trade history to the metric set. Empty input yields NaN, never zero.

    NaN matters: a win rate of 0.0 means every trade lost, while "no trades" means nothing
    was learned. Collapsing the second into the first is how an untested strategy reads as a
    catastrophic one, or worse, the reverse.
    """
    drawdown, peak = drawdown_from_curve(curve)
    equity = (
        curve[-1].equity if curve
        else (trades[-1].equity_after if trades else starting_equity)
    )
    if not trades:
        return Performance(
            max_drawdown=drawdown, peak_equity=peak, equity=equity,
            starting_equity=starting_equity,
            min_trades_for_verdict=min_trades_for_verdict,
        )

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl < 0]
    gross_profit = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    net = sum(t.pnl for t in trades)

    moment = now if now is not None else max(t.timestamp for t in trades)
    day_start = datetime.fromtimestamp(moment, tz=timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()
    today = [t for t in trades if t.timestamp >= day_start]

    regimes: dict[str, int] = {}
    reasons: dict[str, int] = {}
    for trade in trades:
        if trade.market_regime:
            regimes[trade.market_regime] = regimes.get(trade.market_regime, 0) + 1
        if trade.exit_reason:
            reasons[trade.exit_reason] = reasons.get(trade.exit_reason, 0) + 1

    return Performance(
        trades=len(trades),
        wins=len(wins),
        losses=len(losses),
        win_rate=len(wins) / len(trades),
        loss_rate=len(losses) / len(trades),
        # inf, not a large number: "no losses yet" is a sample-size statement, and rounding
        # it into a score invites reading it as quality.
        profit_factor=(gross_profit / gross_loss) if gross_loss > 0 else float("inf"),
        expectancy=net / len(trades),
        expectancy_r=sum(t.r_multiple for t in trades) / len(trades),
        avg_win=gross_profit / len(wins) if wins else 0.0,
        avg_loss=-gross_loss / len(losses) if losses else 0.0,
        largest_win=max((t.pnl for t in wins), default=0.0),
        largest_loss=min((t.pnl for t in losses), default=0.0),
        max_consecutive_losses=max_consecutive_losses(trades),
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        net_pnl=net,
        fees_paid=sum(t.fees for t in trades),
        funding_paid=sum(t.funding for t in trades),
        realised_pnl=net,
        daily_pnl=sum(t.pnl for t in today),
        trades_today=len(today),
        liquidations=sum(1 for t in trades if t.exit_reason == "liquidation"),
        max_drawdown=drawdown,
        peak_equity=peak,
        equity=equity,
        starting_equity=starting_equity,
        total_return=((equity - starting_equity) / starting_equity)
        if starting_equity else 0.0,
        conclusive=len(trades) >= min_trades_for_verdict,
        min_trades_for_verdict=min_trades_for_verdict,
        by_regime=regimes,
        by_exit_reason=reasons,
    )


class Dashboard:
    """Reads the store and renders the numbers. Never writes, never trades."""

    def __init__(self, store: TradeStore, *, starting_equity: float = 0.0,
                 min_trades_for_verdict: int = 1000) -> None:
        self.store = store
        self.starting_equity = float(starting_equity)
        self.min_trades_for_verdict = int(min_trades_for_verdict)

    @classmethod
    def from_config(cls, cfg: Any, store: TradeStore | None = None,
                    *, starting_equity: float = 0.0) -> "Dashboard":
        return cls(
            store or TradeStore.from_config(cfg),
            starting_equity=starting_equity,
            min_trades_for_verdict=int(cfg.get("backtest.min_trades_for_verdict", 1000)),
        )

    def performance(self, symbol: str | None = None, since: float | None = None,
                    now: float | None = None) -> Performance:
        return compute(
            self.store.trades(symbol=symbol, since=since),
            self.store.equity_curve(since=since),
            starting_equity=self.starting_equity,
            now=now,
            min_trades_for_verdict=self.min_trades_for_verdict,
        )

    def render(self, symbol: str | None = None, since: float | None = None,
               now: float | None = None) -> str:
        """A plain-text report. Losses are shown with the same prominence as wins."""
        p = self.performance(symbol, since, now)
        width = 62
        lines = [
            "=" * width,
            f"  performance{f' — {symbol}' if symbol else ''}",
            "=" * width,
        ]

        def row(label: str, value: str) -> str:
            return f"  {label:<26}: {value}"

        if p.trades == 0:
            lines.append(row("trades", "0"))
        else:
            lines += [
                row("trades", f"{p.trades} ({p.wins} won, {p.losses} lost)"),
                row("win rate", f"{p.win_rate * 100:.1f}%  "
                                f"(loss rate {p.loss_rate * 100:.1f}%)"),
                row("profit factor", f"{p.profit_factor:.2f}"),
                row("expectancy", f"{p.expectancy:+.2f} ({p.expectancy_r:+.3f}R)"),
                row("avg win / avg loss", f"{p.avg_win:+.2f} / {p.avg_loss:+.2f}"),
                row("largest win / loss", f"{p.largest_win:+.2f} / {p.largest_loss:+.2f}"),
                row("max consecutive losses", str(p.max_consecutive_losses)),
                row("liquidations", str(p.liquidations)),
                "-" * width,
                row("net pnl", f"{p.net_pnl:+.2f}"),
                row("fees paid", f"{p.fees_paid:.2f}"),
                row("funding paid", f"{p.funding_paid:.2f}"),
                row("pnl today", f"{p.daily_pnl:+.2f} over {p.trades_today} trade(s)"),
            ]
        lines += [
            "-" * width,
            row("equity", f"{p.equity:.2f}"),
            row("peak equity", f"{p.peak_equity:.2f}"),
            row("max drawdown", f"{p.max_drawdown * 100:.2f}%"),
        ]
        if p.starting_equity:
            lines.append(row("total return", f"{p.total_return * 100:+.2f}%"))

        if p.by_exit_reason:
            lines += ["-" * width, "  exits"]
            for reason, count in sorted(p.by_exit_reason.items(),
                                        key=lambda item: -item[1]):
                lines.append(row(f"  {reason}", str(count)))
        if p.by_regime:
            lines += ["-" * width, "  regimes"]
            for regime, count in sorted(p.by_regime.items(), key=lambda item: -item[1]):
                lines.append(row(f"  {regime}", str(count)))

        tripped = self.store.kill_switches()
        if tripped:
            lines += ["-" * width, "  KILL SWITCHES TRIPPED"]
            for breaker, reason in sorted(tripped.items()):
                lines.append(row(f"  {breaker}", reason[:60]))

        lines += ["-" * width, f"  verdict: {p.verdict()}", "=" * width]
        return "\n".join(lines)
