"""Multi-timeframe signal engine: the final say on whether a setup becomes a signal.

Combines regime and score across the configured timeframes and returns a single
``Signal``. Emitting one is where this module stops. It does not size, place, or manage
anything — there is no order execution and no live trading in this layer, and the tests
assert the class exposes no such method. The output is a decision; acting on it is
Phase 6+.

Everything is a veto. A signal is accepted only by passing every gate, and any single
failure returns a rejection carrying the reason. The default answer is no.

Two lookahead traps this module exists to close:

1. **The in-progress bar.** Gate stamps each candle with its *open* time, so the newest
   bar in any series is still forming — verified against the live API, where 1h stamps
   are exact multiples of 3600 and the newest opened minutes ago. A bar on interval *T*
   is complete only at ``t + T``. Scoring the forming bar means trading a candle whose
   close is not yet known; in replay it is reading the future outright.
2. **Higher-timeframe leakage.** The 1h bar covering *now* has not closed either. Asking
   "what is the 1h trend?" at 10:05 must answer from the 09:00 bar, not the 10:00 bar
   that will not finish until 11:00.

Both reduce to one rule, applied per timeframe by :func:`closed_bars`: use only bars
whose close time is at or before ``now``. It is the single place that convention lives.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

import numpy as np

from strategy.indicators import Candles, atr, relative_volume
from strategy.regime import Regime, RegimeParams, RegimeResult, classify
from strategy.scoring import ScoreResult, ScoringParams, score

__all__ = [
    "TIMEFRAME_SECONDS",
    "timeframe_seconds",
    "closed_bars",
    "EngineParams",
    "TimeframeRead",
    "Signal",
    "SignalEngine",
]

#: Seconds per Gate candlestick interval.
TIMEFRAME_SECONDS: Mapping[str, int] = {
    "10s": 10, "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "8h": 28800, "1d": 86400,
}


def timeframe_seconds(timeframe: str) -> int:
    try:
        return TIMEFRAME_SECONDS[str(timeframe)]
    except KeyError:
        raise ValueError(f"unknown timeframe {timeframe!r}") from None


def closed_bars(candles: Candles, interval_seconds: int, now: float) -> Candles:
    """The prefix of ``candles`` that had fully closed by ``now``.

    Gate's ``t`` is the bar's **open** time (verified live: 1h stamps are exact multiples
    of 3600), so bar *i* covers ``[t[i], t[i] + interval)`` and is usable only once
    ``t[i] + interval <= now``. The boundary is exclusive: at exactly ``t + interval - ε``
    the bar is still forming.

    Returns a truncated copy; the input is never modified.
    """
    if len(candles) == 0:
        return candles
    interval = int(interval_seconds)
    if interval <= 0:
        raise ValueError(f"interval_seconds must be positive, got {interval_seconds!r}")
    count = int(np.count_nonzero((candles.time + interval) <= float(now)))
    return candles.head(count)


@dataclass(frozen=True)
class EngineParams:
    """Engine-level thresholds. Defaults mirror the shipped ``config.yaml``."""

    timeframes: tuple[str, ...] = ("1m", "5m", "15m", "1h")
    #: Contribution of each timeframe to the blended score; renormalised over what is present.
    weights: Mapping[str, float] = field(
        default_factory=lambda: {"1m": 0.15, "5m": 0.25, "15m": 0.30, "1h": 0.30}
    )
    #: Timeframes whose direction may veto the trade. Higher timeframes rule.
    veto_timeframes: tuple[str, ...] = ("15m", "1h")
    min_score: float = 80.0
    mode: str = "scalp"
    allowed_regimes: tuple[str, ...] = ("TRENDING", "RANGING")
    require_clear_regime: bool = True
    btc_symbol: str = "BTC_USDT"
    btc_filter: bool = True
    btc_max_1m_move: float = 0.004
    btc_max_5m_move: float = 0.010
    btc_volume_spike_multiple: float = 4.0
    abnormal_candle_atr_multiple: float = 3.0
    max_spread: float = 0.0004
    min_24h_volume_usdt: float = 20_000_000.0

    @classmethod
    def from_config(cls, cfg: Any) -> "EngineParams":
        base = cls()
        mode = str(cfg.get("mode", base.mode))
        timeframes = tuple(str(t) for t in cfg.get("strategy.timeframes", base.timeframes))
        weights = cfg.get("strategy.timeframe_weights", None)
        allowed = cfg.get(f"strategy.regime.{mode}_allowed", list(base.allowed_regimes))
        return cls(
            timeframes=timeframes,
            weights=(
                {str(k): float(v) for k, v in dict(weights).items()}
                if isinstance(weights, Mapping) else dict(base.weights)
            ),
            veto_timeframes=tuple(
                str(t) for t in cfg.get("strategy.veto_timeframes", base.veto_timeframes)
            ),
            min_score=float(cfg.get("strategy.minimum_score", base.min_score)),
            mode=mode,
            allowed_regimes=tuple(str(r) for r in allowed),
            require_clear_regime=bool(
                cfg.get("strategy.regime.require_clear_regime", base.require_clear_regime)
            ),
            btc_symbol=str(cfg.get("filters.btc_symbol", base.btc_symbol)),
            btc_filter=bool(cfg.get("filters.btc_volatility_filter", base.btc_filter)),
            btc_max_1m_move=float(cfg.get("filters.btc_max_1m_move", base.btc_max_1m_move)),
            btc_max_5m_move=float(cfg.get("filters.btc_max_5m_move", base.btc_max_5m_move)),
            btc_volume_spike_multiple=float(
                cfg.get("filters.btc_volume_spike_multiple", base.btc_volume_spike_multiple)
            ),
            abnormal_candle_atr_multiple=float(
                cfg.get("filters.abnormal_candle_atr_multiple",
                        base.abnormal_candle_atr_multiple)
            ),
            max_spread=float(cfg.get("filters.max_spread", base.max_spread)),
            min_24h_volume_usdt=float(
                cfg.get("filters.min_24h_volume_usdt", base.min_24h_volume_usdt)
            ),
        )


@dataclass(frozen=True)
class TimeframeRead:
    """What one timeframe contributed."""

    timeframe: str
    regime: RegimeResult
    bars: int
    last_bar_time: float
    long_score: ScoreResult | None = None
    short_score: ScoreResult | None = None


@dataclass(frozen=True)
class Signal:
    """The engine's answer — accepted or not. Advisory only; nothing has been ordered."""

    symbol: str
    now: float
    accepted: bool = False
    direction: int = 0                       # +1 long, -1 short, 0 none
    score: float = 0.0
    reason: str = ""
    stage: str = ""
    price: float = float("nan")
    atr: float = float("nan")
    timeframes: Mapping[str, TimeframeRead] = field(default_factory=dict)

    @property
    def side(self) -> str:
        return {1: "long", -1: "short"}.get(self.direction, "none")

    def summary(self) -> str:
        verdict = f"{self.side.upper()} score {self.score:.1f}" if self.accepted else "no trade"
        return f"{self.symbol}: {verdict} — {self.reason}"


#: Regimes that support a directional trade, per side. HIGH/LOW_VOLATILITY support
#: neither, which is why they appear in no list.
_SUPPORTS = {
    +1: (Regime.TRENDING, Regime.BREAKOUT),
    -1: (Regime.TRENDING, Regime.BREAKDOWN),
}


class SignalEngine:
    """Evaluates one symbol at a time. Stateless across calls.

    Holds no cooldown or position state on purpose: those belong to the risk manager
    (Phase 6), and duplicating them here would create two sources of truth about whether
    trading is permitted.
    """

    def __init__(
        self,
        config: Any = None,
        params: EngineParams | None = None,
        regime_params: RegimeParams | None = None,
        scoring_params: ScoringParams | None = None,
    ) -> None:
        if params is not None:
            self.params = params
        elif config is not None:
            self.params = EngineParams.from_config(config)
        else:
            self.params = EngineParams()

        if config is not None:
            self.regime_params = regime_params or RegimeParams.from_config(config)
            self.scoring_params = scoring_params or ScoringParams.from_config(config)
        else:
            self.regime_params = regime_params or RegimeParams()
            self.scoring_params = scoring_params or ScoringParams()

        if not self.params.timeframes:
            raise ValueError("no timeframes configured; nothing to evaluate")
        self.entry_timeframe = self.params.timeframes[0]

    # --- helpers -----------------------------------------------------------

    def _no(self, symbol: str, now: float, stage: str, reason: str, **extra: Any) -> Signal:
        return Signal(symbol=symbol, now=now, accepted=False, reason=reason,
                      stage=stage, **extra)

    def _btc_veto(self, symbol: str, btc: Mapping[str, Candles] | None, now: float) -> str:
        """Abnormal BTC movement suspends alt entries. Fails closed when data is absent."""
        p = self.params
        if not p.btc_filter or symbol == p.btc_symbol:
            return ""
        series = None if btc is None else btc.get("1m")
        if series is None or len(series) == 0:
            return "BTC volatility filter is on but no BTC 1m candles were supplied"

        view = closed_bars(series, timeframe_seconds("1m"), now)
        if len(view) < 6:
            return "BTC volatility filter is on but too few closed BTC 1m bars"

        close = view.close
        move_1m = abs(close[-1] / close[-2] - 1.0) if close[-2] > 0 else float("inf")
        if move_1m > p.btc_max_1m_move:
            return (f"BTC moved {move_1m * 100:.2f}% in 1m "
                    f"(limit {p.btc_max_1m_move * 100:.2f}%)")

        move_5m = abs(close[-1] / close[-6] - 1.0) if close[-6] > 0 else float("inf")
        if move_5m > p.btc_max_5m_move:
            return (f"BTC moved {move_5m * 100:.2f}% in 5m "
                    f"(limit {p.btc_max_5m_move * 100:.2f}%)")

        spike = float(relative_volume(view.volume, self.scoring_params.rvol_period)[-1])
        if np.isfinite(spike) and spike > p.btc_volume_spike_multiple:
            return (f"BTC volume spike {spike:.1f}x "
                    f"(limit {p.btc_volume_spike_multiple:.1f}x)")
        return ""

    def _abnormal_candle(self, view: Candles) -> str:
        """Refuse to enter straight after an outsized bar.

        The move already happened; entering now buys the retrace at the worst price, and
        the bar itself signals conditions the stop was not sized for.
        """
        limit = self.params.abnormal_candle_atr_multiple
        if not np.isfinite(limit):
            return ""
        series = atr(view.high, view.low, view.close, self.regime_params.atr_period)
        if len(series) < 2:
            return ""
        # Compare against the ATR *before* this bar, so the outlier is not diluted by its
        # own contribution to the average.
        previous = float(series[-2])
        span = float(view.high[-1] - view.low[-1])
        if not np.isfinite(previous) or previous <= 0:
            return ""
        if span > limit * previous:
            return f"last bar spanned {span / previous:.1f} ATR (limit {limit:.1f})"
        return ""

    def _blend(self, reads: Mapping[str, TimeframeRead], direction: int) -> float:
        """Weighted mean of per-timeframe scores, renormalised over what is present."""
        total = weight_sum = 0.0
        for timeframe, read in reads.items():
            result = read.long_score if direction > 0 else read.short_score
            if result is None:
                continue
            weight = self.params.weights.get(timeframe, 1.0 / max(len(reads), 1))
            total += weight * result.total
            weight_sum += weight
        return total / weight_sum if weight_sum > 0 else 0.0

    # --- the evaluation ----------------------------------------------------

    def evaluate(
        self,
        symbol: str,
        candles: Mapping[str, Candles],
        now: float,
        btc: Mapping[str, Candles] | None = None,
        spread: float | None = None,
        volume_24h_usdt: float | None = None,
    ) -> Signal:
        """Run every gate in order and return the resulting :class:`Signal`."""
        p = self.params

        # --- align every timeframe to its closed bars ------------------------
        views: dict[str, Candles] = {}
        for timeframe in p.timeframes:
            series = candles.get(timeframe)
            if series is None or len(series) == 0:
                return self._no(symbol, now, "data", f"no {timeframe} candles supplied")
            view = closed_bars(series, timeframe_seconds(timeframe), now)
            if len(view) == 0:
                return self._no(
                    symbol, now, "data",
                    f"no closed {timeframe} bar at {now:.0f}",
                )
            views[timeframe] = view

        # --- market-condition filters ----------------------------------------
        if spread is not None and spread > p.max_spread:
            return self._no(
                symbol, now, "spread",
                f"spread {spread * 100:.4f}% exceeds {p.max_spread * 100:.4f}%",
            )
        if volume_24h_usdt is not None and volume_24h_usdt < p.min_24h_volume_usdt:
            return self._no(
                symbol, now, "liquidity",
                f"24h volume {volume_24h_usdt:,.0f} below {p.min_24h_volume_usdt:,.0f} USDT",
            )

        btc_reason = self._btc_veto(symbol, btc, now)
        if btc_reason:
            return self._no(symbol, now, "btc_filter", btc_reason)

        # --- regime per timeframe --------------------------------------------
        reads: dict[str, TimeframeRead] = {}
        for timeframe, view in views.items():
            reads[timeframe] = TimeframeRead(
                timeframe=timeframe,
                regime=classify(view, params=self.regime_params),
                bars=len(view),
                last_bar_time=float(view.time[-1]),
            )

        if p.require_clear_regime:
            unclear = [tf for tf, r in reads.items() if r.regime.regime is None]
            if unclear:
                return self._no(
                    symbol, now, "regime",
                    f"no clear regime on {', '.join(unclear)}: "
                    f"{reads[unclear[0]].regime.reason}",
                    timeframes=reads,
                )

        # A volatility extreme anywhere is disqualifying, not just on the entry
        # timeframe. A 5m ATR blowout is the same hazard to a 1m entry whether or not the
        # 1m has noticed it yet, and at 100x leverage the stop is too tight to absorb it.
        for timeframe in p.timeframes:
            found = reads[timeframe].regime.regime
            if found in (Regime.HIGH_VOLATILITY, Regime.LOW_VOLATILITY):
                return self._no(
                    symbol, now, "volatility",
                    f"{timeframe} is {found.value}: {reads[timeframe].regime.reason}",
                    timeframes=reads,
                )

        entry = reads[self.entry_timeframe]
        if not entry.regime.allowed_by(list(p.allowed_regimes)):
            name = entry.regime.regime.value if entry.regime.regime else "none"
            return self._no(
                symbol, now, "regime",
                f"{self.entry_timeframe} regime {name} not in {p.mode}_allowed "
                f"{list(p.allowed_regimes)}",
                timeframes=reads,
            )

        # --- direction and the higher-timeframe veto --------------------------
        direction, conflict = self._resolve_direction(reads)
        if direction == 0:
            return self._no(symbol, now, "direction", conflict, timeframes=reads)

        veto = self._veto_reason(reads, direction)
        if veto:
            return self._no(symbol, now, "mtf_veto", veto, timeframes=reads)

        entry_view = views[self.entry_timeframe]
        abnormal = self._abnormal_candle(entry_view)
        if abnormal:
            return self._no(symbol, now, "abnormal_candle", abnormal, timeframes=reads)

        # --- score, blend, threshold ------------------------------------------
        for timeframe, view in views.items():
            read = reads[timeframe]
            result = score(view, direction, params=self.scoring_params)
            reads[timeframe] = TimeframeRead(
                timeframe=read.timeframe,
                regime=read.regime,
                bars=read.bars,
                last_bar_time=read.last_bar_time,
                long_score=result if direction > 0 else None,
                short_score=result if direction < 0 else None,
            )

        blended = self._blend(reads, direction)
        price = float(entry_view.close[-1])
        atr_now = float(
            atr(entry_view.high, entry_view.low, entry_view.close,
                self.regime_params.atr_period)[-1]
        )

        if blended < p.min_score:
            return Signal(
                symbol=symbol, now=now, accepted=False, direction=0, score=blended,
                reason=f"score {blended:.1f} below minimum {p.min_score:g}",
                stage="score", price=price, atr=atr_now, timeframes=reads,
            )

        if not np.isfinite(atr_now) or atr_now <= 0:
            return self._no(
                symbol, now, "atr", "ATR unavailable on the entry timeframe",
                timeframes=reads,
            )

        return Signal(
            symbol=symbol, now=now, accepted=True, direction=direction, score=blended,
            reason=f"all gates passed on {', '.join(p.timeframes)}",
            stage="accepted", price=price, atr=atr_now, timeframes=reads,
        )

    def _resolve_direction(self, reads: Mapping[str, TimeframeRead]) -> tuple[int, str]:
        """Direction from the veto timeframes, falling back to the entry timeframe.

        Higher timeframes decide which way the trade may go; the entry timeframe only
        gets a say when they are silent. A split among them is no trade.
        """
        votes = {
            reads[tf].regime.direction
            for tf in self.params.veto_timeframes
            if tf in reads and reads[tf].regime.direction != 0
        }
        if len(votes) == 1:
            return votes.pop(), ""
        if votes:
            return 0, (
                "higher timeframes conflict: "
                + ", ".join(
                    f"{tf}={reads[tf].regime.direction:+d}"
                    for tf in self.params.veto_timeframes if tf in reads
                )
            )
        fallback = reads[self.entry_timeframe].regime.direction
        if fallback == 0:
            return 0, "no timeframe expresses a direction"
        return fallback, ""

    def _veto_reason(self, reads: Mapping[str, TimeframeRead], direction: int) -> str:
        """A higher timeframe pointing the other way kills the trade.

        RANGING does not veto — a range on the 1h is context a 5m trend can work inside.
        An opposing *directional* regime does.
        """
        for timeframe in self.params.veto_timeframes:
            read = reads.get(timeframe)
            if read is None:
                continue
            result = read.regime
            if result.regime is None:
                return f"{timeframe} has no clear regime, cannot align"
            if result.direction != 0 and result.direction != direction:
                return (
                    f"{timeframe} is {result.regime.value} "
                    f"{'up' if result.direction > 0 else 'down'}, conflicting with a "
                    f"{'long' if direction > 0 else 'short'}"
                )
            if (
                result.regime not in _SUPPORTS[direction]
                and result.regime is not Regime.RANGING
            ):
                return (
                    f"{timeframe} regime {result.regime.value} does not align with a "
                    f"{'long' if direction > 0 else 'short'}"
                )
        return ""
