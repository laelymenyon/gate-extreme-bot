"""Weighted 0-100 setup score.

Six categories, weighted per ``strategy.scoring_weights``: trend 25, momentum 20,
volume 15, price_action 20, volatility 10, support_resistance 10. Each earns a 0.0-1.0
fraction of its weight, so the total is bounded by 0-100 by construction rather than by
clamping. Below ``strategy.minimum_score`` the engine does nothing.

Every category reports its own fraction and a human-readable detail line. That breakdown
is the point: "scored 71" is not reviewable, whereas "trend 25/25, volume 3/15" says the
setup was structurally fine but nobody was participating.

**A NaN input earns zero, never a midpoint.** An indicator that has not warmed up is
missing information, and missing information must not accumulate into a passing score.
This is the same fail-closed rule Phase 4 applied to ``nearest_support`` returning NaN:
absence of a known level is not evidence of clear space ahead.

Scores are direction-aware — a setup is scored *as a long* or *as a short*, never in the
abstract, since RSI 68 is strength for one and exhaustion for the other.

**No lookahead.** Scoring reads ``candles.head(as_of + 1)`` and only structural levels
already confirmed at that bar.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

import numpy as np

from strategy.indicators import (
    Candles,
    adx,
    atr,
    confirmed_swing_levels,
    ema,
    macd,
    relative_volume,
    rsi,
)

__all__ = ["CategoryScore", "ScoreResult", "ScoringParams", "score"]

#: Fallback weights when the config omits them. Mirrors ``strategy.scoring_weights``.
DEFAULT_WEIGHTS: Mapping[str, float] = {
    "trend": 25.0,
    "momentum": 20.0,
    "volume": 15.0,
    "price_action": 20.0,
    "volatility": 10.0,
    "support_resistance": 10.0,
}


@dataclass(frozen=True)
class ScoringParams:
    """Thresholds from ``strategy.scoring``. Defaults mirror the shipped config."""

    rsi_period: int = 14
    rsi_long_floor: float = 50.0
    rsi_long_ceiling: float = 72.0
    rsi_short_ceiling: float = 50.0
    rsi_short_floor: float = 28.0
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    rvol_period: int = 20
    rvol_full_credit: float = 2.0
    rvol_no_credit: float = 0.8
    body_full_credit: float = 0.65
    atr_period: int = 14
    atr_pct_floor: float = 0.0006
    atr_pct_ideal: float = 0.0020
    atr_pct_ceiling: float = 0.0060
    sr_room_atr_full: float = 3.0
    sr_room_atr_none: float = 0.5
    adx_period: int = 14
    adx_full_credit: float = 30.0
    swing_left: int = 2
    swing_right: int = 2
    sr_lookback: int = 50
    weights: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))

    @classmethod
    def from_config(cls, cfg: Any) -> "ScoringParams":
        base = cls()

        def get(name: str, default: Any) -> Any:
            return cfg.get(f"strategy.scoring.{name}", default)

        weights = cfg.get("strategy.scoring_weights", None)
        weights = (
            {str(k): float(v) for k, v in dict(weights).items()}
            if isinstance(weights, Mapping)
            else dict(DEFAULT_WEIGHTS)
        )
        return cls(
            rsi_period=int(get("rsi_period", base.rsi_period)),
            rsi_long_floor=float(get("rsi_long_floor", base.rsi_long_floor)),
            rsi_long_ceiling=float(get("rsi_long_ceiling", base.rsi_long_ceiling)),
            rsi_short_ceiling=float(get("rsi_short_ceiling", base.rsi_short_ceiling)),
            rsi_short_floor=float(get("rsi_short_floor", base.rsi_short_floor)),
            macd_fast=int(get("macd_fast", base.macd_fast)),
            macd_slow=int(get("macd_slow", base.macd_slow)),
            macd_signal=int(get("macd_signal", base.macd_signal)),
            rvol_period=int(get("rvol_period", base.rvol_period)),
            rvol_full_credit=float(get("rvol_full_credit", base.rvol_full_credit)),
            rvol_no_credit=float(get("rvol_no_credit", base.rvol_no_credit)),
            body_full_credit=float(get("body_full_credit", base.body_full_credit)),
            atr_period=int(cfg.get("strategy.regime.atr_period", base.atr_period)),
            atr_pct_floor=float(get("atr_pct_floor", base.atr_pct_floor)),
            atr_pct_ideal=float(get("atr_pct_ideal", base.atr_pct_ideal)),
            atr_pct_ceiling=float(get("atr_pct_ceiling", base.atr_pct_ceiling)),
            sr_room_atr_full=float(get("sr_room_atr_full", base.sr_room_atr_full)),
            sr_room_atr_none=float(get("sr_room_atr_none", base.sr_room_atr_none)),
            weights=weights,
        )

    def weight(self, category: str) -> float:
        return float(self.weights.get(category, DEFAULT_WEIGHTS.get(category, 0.0)))


@dataclass(frozen=True)
class CategoryScore:
    name: str
    fraction: float          # 0.0-1.0 of this category's weight
    weight: float
    detail: str

    @property
    def points(self) -> float:
        return self.fraction * self.weight


@dataclass(frozen=True)
class ScoreResult:
    total: float
    direction: int
    categories: Mapping[str, CategoryScore]
    metrics: Mapping[str, float] = field(default_factory=dict)

    def meets(self, minimum: float) -> bool:
        return self.total >= float(minimum)

    def breakdown(self) -> str:
        """One-line summary for logs: ``trend 25.0/25 momentum 8.4/20 ...``."""
        return " ".join(
            f"{c.name} {c.points:.1f}/{c.weight:g}" for c in self.categories.values()
        )


def _ramp(value: float, zero_at: float, full_at: float) -> float:
    """Linear 0->1 between ``zero_at`` and ``full_at``. NaN earns 0."""
    if not np.isfinite(value):
        return 0.0
    if full_at == zero_at:
        return 1.0 if value >= full_at else 0.0
    fraction = (value - zero_at) / (full_at - zero_at)
    return float(min(1.0, max(0.0, fraction)))


def _band(value: float, floor: float, ideal: float, ceiling: float) -> float:
    """Triangular credit: 0 at ``floor``, 1 at ``ideal``, back to 0 at ``ceiling``.

    Used where more is better only up to a point — ATR wide enough to clear fees but not
    so wide it runs the stop.
    """
    if not np.isfinite(value):
        return 0.0
    if value <= floor or value >= ceiling:
        return 0.0
    if value <= ideal:
        return _ramp(value, floor, ideal)
    return 1.0 - _ramp(value, ideal, ceiling)


def _last(series: np.ndarray) -> float:
    return float(series[-1]) if len(series) else float("nan")


# --- categories ------------------------------------------------------------
#
# Each returns a 0.0-1.0 fraction and a detail string. `direction` is +1 for a long or
# -1 for a short; every category is scored from that side's point of view.

def _score_trend(view: Candles, direction: int, p: ScoringParams) -> CategoryScore:
    """EMA stack alignment, price above/below the 200, and ADX as strength."""
    close = view.close
    price = _last(close)
    e9, e21, e50, e200 = (_last(ema(close, n)) for n in (9, 21, 50, 200))
    strength = _last(adx(view.high, view.low, close, p.adx_period))

    if not np.isfinite([price, e9, e21, e50, e200]).all():
        return CategoryScore("trend", 0.0, p.weight("trend"), "EMA stack not warmed up")

    if direction > 0:
        ordered = [e9 > e21, e21 > e50, e50 > e200, price > e200]
    else:
        ordered = [e9 < e21, e21 < e50, e50 < e200, price < e200]

    # Three quarters for structure, one quarter for confirmed strength.
    alignment = sum(ordered) / len(ordered)
    fraction = 0.75 * alignment + 0.25 * _ramp(strength, 0.0, p.adx_full_credit)
    return CategoryScore(
        "trend", fraction, p.weight("trend"),
        f"{sum(ordered)}/4 EMA conditions aligned, ADX {strength:.1f}",
    )


def _score_momentum(view: Candles, direction: int, p: ScoringParams) -> CategoryScore:
    """RSI inside a directional band, plus MACD histogram agreement.

    The RSI band has a ceiling on purpose. Buying RSI 85 is chasing something already
    extended; at 100x with a 0.125% stop there is no room to survive the snap-back.
    """
    close = view.close
    strength = _last(rsi(close, p.rsi_period))
    result = macd(close, p.macd_fast, p.macd_slow, p.macd_signal)
    histogram = _last(result.histogram)
    line = _last(result.macd)

    if direction > 0:
        in_band = _ramp(strength, p.rsi_long_floor - 10.0, p.rsi_long_floor)
        if np.isfinite(strength) and strength > p.rsi_long_ceiling:
            in_band = 0.0
        agrees = np.isfinite(histogram) and histogram > 0
        line_ok = np.isfinite(line) and line > 0
    else:
        in_band = _ramp(-strength, -(p.rsi_short_ceiling + 10.0), -p.rsi_short_ceiling)
        if np.isfinite(strength) and strength < p.rsi_short_floor:
            in_band = 0.0
        agrees = np.isfinite(histogram) and histogram < 0
        line_ok = np.isfinite(line) and line < 0

    fraction = 0.5 * in_band + 0.3 * float(agrees) + 0.2 * float(line_ok)
    return CategoryScore(
        "momentum", fraction, p.weight("momentum"),
        f"RSI {strength:.1f}, MACD hist {histogram:+.6g}",
    )


def _score_volume(view: Candles, direction: int, p: ScoringParams) -> CategoryScore:
    """Relative volume: is anyone actually participating in this move?"""
    rvol = _last(relative_volume(view.volume, p.rvol_period))
    fraction = _ramp(rvol, p.rvol_no_credit, p.rvol_full_credit)
    return CategoryScore(
        "volume", fraction, p.weight("volume"), f"relative volume {rvol:.2f}x"
    )


def _score_price_action(view: Candles, direction: int, p: ScoringParams) -> CategoryScore:
    """Body conviction and close position within the last bar's range.

    A wide range that closes mid-bar is indecision; the same range closing on its high is
    a bar someone was leaning on.
    """
    high, low, close, open_ = view.high[-1], view.low[-1], view.close[-1], view.open[-1]
    span = high - low
    if not np.isfinite(span) or span <= 0:
        return CategoryScore(
            "price_action", 0.0, p.weight("price_action"), "zero-range bar"
        )

    body = abs(close - open_) / span
    close_position = (close - low) / span          # 1.0 = closed on the high
    directional = close_position if direction > 0 else 1.0 - close_position
    right_way = (close > open_) if direction > 0 else (close < open_)

    fraction = (
        0.4 * _ramp(body, 0.0, p.body_full_credit)
        + 0.4 * _ramp(directional, 0.4, 0.9)
        + 0.2 * float(right_way)
    )
    return CategoryScore(
        "price_action", fraction, p.weight("price_action"),
        f"body {body:.0%} of range, closed {close_position:.0%} up the bar",
    )


def _score_volatility(view: Candles, direction: int, p: ScoringParams) -> CategoryScore:
    """ATR% in the workable band — enough movement to pay fees, not enough to run stops."""
    price = _last(view.close)
    atr_now = _last(atr(view.high, view.low, view.close, p.atr_period))
    atr_pct = atr_now / price if np.isfinite(atr_now) and price > 0 else float("nan")
    fraction = _band(atr_pct, p.atr_pct_floor, p.atr_pct_ideal, p.atr_pct_ceiling)
    return CategoryScore(
        "volatility", fraction, p.weight("volatility"),
        f"ATR {atr_pct * 100:.3f}% of price",
    )


def _score_support_resistance(
    view: Candles, direction: int, p: ScoringParams
) -> CategoryScore:
    """Room to the opposing level, measured in ATRs.

    Only levels confirmed by the current bar count, so this cannot see a swing that has
    not printed yet.

    Pivot levels are the preferred yardstick, but the absence of a pivot is not the
    absence of an obstacle, so it cannot be the only one. A monotonic rise has no pivot
    highs (no bar has lower bars on both sides) and neither does a monotonic decline —
    yet overhead supply is nil in the first case and everywhere in the second. Scoring
    both as "unknown, zero" would zero this category for every trade into new highs, the
    entire breakout playbook; scoring both as "clear, full" would wave a long through in
    a crash.

    So when no confirmed pivot blocks the way, fall back to the plainest fact available:
    the highest high (or lowest low) in the lookback window. Price at the window extreme
    genuinely has nothing in front of it; price far below the window high genuinely does,
    pivot or not. Both readings use only bars at or before the decision bar.
    """
    price = _last(view.close)
    atr_now = _last(atr(view.high, view.low, view.close, p.atr_period))
    if not np.isfinite(price) or not np.isfinite(atr_now) or atr_now <= 0:
        return CategoryScore(
            "support_resistance", 0.0, p.weight("support_resistance"), "ATR unavailable"
        )

    values, label = (
        (view.high, "resistance") if direction > 0 else (view.low, "support")
    )
    levels = confirmed_swing_levels(
        values, as_of=len(view) - 1, left=p.swing_left, right=p.swing_right,
        lookback=p.sr_lookback, high=direction > 0,
    )
    blocking = levels[levels > price] if direction > 0 else levels[levels < price]

    if blocking.size:
        obstacle = float(blocking.min() if direction > 0 else blocking.max())
        source = f"{label} at {obstacle:.6g}"
    else:
        # No pivot in the way — measure against the window extreme instead. The current
        # bar is excluded: its own high is by definition >= its close, so including it
        # would report a breakout close as still having resistance a fraction of an ATR
        # overhead, and no new high could ever read as clear. Resistance is prior
        # structure, not the bar being scored.
        window = values[-(p.sr_lookback + 1):-1]
        window = window[np.isfinite(window)]
        if window.size == 0:
            return CategoryScore(
                "support_resistance", 0.0, p.weight("support_resistance"),
                "no usable price history",
            )
        extreme = float(window.max() if direction > 0 else window.min())
        if (direction > 0 and price >= extreme) or (direction < 0 and price <= extreme):
            # "Nothing in the way" is a claim about the whole lookback horizon, so it
            # needs the whole horizon behind it. With a short window the statement is
            # unsupported — and granting full credit would hand every thinly-backed
            # symbol 10 free points precisely where the least is known.
            if window.size < p.sr_lookback:
                return CategoryScore(
                    "support_resistance", 0.0, p.weight("support_resistance"),
                    f"only {window.size} prior bars, need {p.sr_lookback} to judge "
                    "clear space — unknown, not clear",
                )
            return CategoryScore(
                "support_resistance", 1.0, p.weight("support_resistance"),
                f"price has cleared the prior {window.size}-bar "
                f"{'high' if direction > 0 else 'low'} — nothing in the way",
            )
        obstacle = extreme
        source = (
            f"prior {window.size}-bar {'high' if direction > 0 else 'low'} {obstacle:.6g}"
        )

    room = abs(obstacle - price) / atr_now
    fraction = _ramp(room, p.sr_room_atr_none, p.sr_room_atr_full)
    return CategoryScore(
        "support_resistance", fraction, p.weight("support_resistance"),
        f"{room:.1f} ATR to {source}",
    )


_CATEGORIES = (
    _score_trend,
    _score_momentum,
    _score_volume,
    _score_price_action,
    _score_volatility,
    _score_support_resistance,
)


def score(
    candles: Candles,
    direction: int,
    as_of: int | None = None,
    params: ScoringParams | None = None,
) -> ScoreResult:
    """Score the setup at bar ``as_of`` as a long (``direction=+1``) or short (``-1``).

    Returns a total in 0-100 with the per-category breakdown attached. A direction of 0
    is rejected: there is no such thing as a directionless setup score, and silently
    picking a side would produce a number nobody could interpret.
    """
    if direction not in (1, -1):
        raise ValueError(f"direction must be +1 (long) or -1 (short), got {direction!r}")

    params = params or ScoringParams()

    if len(candles) == 0:
        raise ValueError("cannot score an empty candle series")

    if as_of is None:
        as_of = len(candles) - 1
    as_of = int(as_of)
    if not 0 <= as_of < len(candles):
        raise ValueError(f"as_of must be in [0, {len(candles) - 1}], got {as_of}")

    # Truncate rather than promise not to peek — the same guard regime.classify uses.
    view = candles.head(as_of + 1)

    categories = {}
    for scorer in _CATEGORIES:
        result = scorer(view, direction, params)
        categories[result.name] = result

    total = sum(c.points for c in categories.values())
    return ScoreResult(
        total=total,
        direction=direction,
        categories=categories,
        metrics={"price": _last(view.close), "bars": float(len(view))},
    )
