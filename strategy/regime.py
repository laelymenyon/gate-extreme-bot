"""Market regime classification.

Answers one question about a symbol on one timeframe: what kind of market is this, and is
it one we are willing to trade at all? The allow-lists in ``config.yaml``
(``strategy.regime.scalp_allowed`` / ``breakout_allowed``) then decide whether a given
playbook may run — no breakout logic while ranging, no trend logic in chop.

``classify`` returns ``regime=None`` for "no clear regime", which is the SKIP case. That
is a first-class answer rather than an error: ``require_clear_regime`` is true, and a 100x
scalp taken in an unreadable market is the trade that ends an account. Long idle stretches
are the design working (ARCHITECTURE §7), not a malfunction.

Note that neither ``HIGH_VOLATILITY`` nor ``LOW_VOLATILITY`` appears in any allow-list in
the shipped config, so both are effectively a skip. They are still reported by name
because "we sat out because ATR was in its 95th percentile" and "we sat out because the
tape was dead" are different post-mortems.

**No lookahead.** A classification for bar *i* reads ``candles.head(i + 1)`` and only
structural levels already confirmed at bar *i* (see ``indicators.confirmed_swing_levels``).
Passing ``as_of=i`` on a long array must give the identical answer to truncating the array
at *i* — asserted bar-by-bar in ``tests/test_regime.py`` rather than taken on trust.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

import numpy as np

from strategy.indicators import (
    Candles,
    adx,
    atr,
    confirmed_swing_levels,
    ema,
    relative_volume,
)

__all__ = ["Regime", "RegimeParams", "RegimeResult", "atr_percentile", "classify"]


class Regime(str, Enum):
    """The market states the classifier can report.

    ``str``-valued so a regime compares equal to the plain strings in the config
    allow-lists and logs readably without a conversion layer.
    """

    TRENDING = "TRENDING"
    RANGING = "RANGING"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    BREAKOUT = "BREAKOUT"
    BREAKDOWN = "BREAKDOWN"


@dataclass(frozen=True)
class RegimeParams:
    """Thresholds from ``strategy.regime``. Defaults mirror the shipped config."""

    ema_separation_trend: float = 0.0008
    ema_separation_chop: float = 0.0003
    adx_period: int = 14
    adx_trending: float = 22.0
    adx_ranging: float = 18.0
    atr_period: int = 14
    atr_lookback: int = 100
    atr_high_percentile: float = 0.85
    atr_low_percentile: float = 0.15
    breakout_lookback: int = 50
    breakout_atr_buffer: float = 0.25
    breakout_min_rvol: float = 1.5
    rvol_period: int = 20
    swing_left: int = 2
    swing_right: int = 2
    min_bars: int = 210

    @classmethod
    def from_config(cls, cfg: Any) -> "RegimeParams":
        """Build from a ``Config``. Missing keys fall back to the dataclass defaults."""
        def get(name: str, default: Any) -> Any:
            return cfg.get(f"strategy.regime.{name}", default)

        base = cls()
        return cls(
            ema_separation_trend=float(get("ema_separation_trend", base.ema_separation_trend)),
            ema_separation_chop=float(get("ema_separation_chop", base.ema_separation_chop)),
            adx_period=int(get("adx_period", base.adx_period)),
            adx_trending=float(get("adx_trending", base.adx_trending)),
            adx_ranging=float(get("adx_ranging", base.adx_ranging)),
            atr_period=int(get("atr_period", base.atr_period)),
            atr_lookback=int(get("atr_lookback", base.atr_lookback)),
            atr_high_percentile=float(get("atr_high_percentile", base.atr_high_percentile)),
            atr_low_percentile=float(get("atr_low_percentile", base.atr_low_percentile)),
            breakout_lookback=int(get("breakout_lookback", base.breakout_lookback)),
            breakout_atr_buffer=float(get("breakout_atr_buffer", base.breakout_atr_buffer)),
            breakout_min_rvol=float(get("breakout_min_rvol", base.breakout_min_rvol)),
            rvol_period=int(cfg.get("strategy.scoring.rvol_period", base.rvol_period)),
            min_bars=int(get("min_bars", base.min_bars)),
        )


@dataclass(frozen=True)
class RegimeResult:
    """A classification plus the measurements behind it.

    ``regime is None`` means SKIP. ``reason`` is always populated, so a bar the bot passed
    on can be explained afterwards instead of vanishing silently.
    """

    regime: Regime | None
    reason: str
    direction: int = 0                      # +1 up, -1 down, 0 none or not directional
    metrics: Mapping[str, float] = field(default_factory=dict)

    @property
    def is_clear(self) -> bool:
        return self.regime is not None

    def allowed_by(self, allow_list: Any) -> bool:
        """Whether this regime appears in a config allow-list."""
        if self.regime is None or not isinstance(allow_list, (list, tuple, set)):
            return False
        return self.regime.value in {str(item) for item in allow_list}


def _skip(reason: str, metrics: Mapping[str, float] | None = None) -> RegimeResult:
    return RegimeResult(None, reason, 0, dict(metrics or {}))


def atr_percentile(history: Sequence[float] | np.ndarray, current: float) -> float:
    """Midrank percentile of ``current`` within ``history``, in 0.0-1.0.

    Ties count as half, which is the whole point. Under a plain ``(history <=
    current).mean()`` a market with perfectly steady volatility scores 1.0 — every
    sample equals the current one — so a calm, regular tape would be reported as a
    volatility extreme and vetoed exactly when conditions are best. Midrank sends a
    constant series to 0.5, the neutral reading it deserves.

    Returns NaN for empty history: no basis for comparison is not a neutral reading.
    """
    values = np.asarray(history, dtype=np.float64)
    values = values[np.isfinite(values)]
    if values.size == 0 or not np.isfinite(current):
        return float("nan")
    below = float((values < current).sum())
    equal = float((values == current).sum())
    return (below + 0.5 * equal) / values.size


def classify(
    candles: Candles,
    as_of: int | None = None,
    params: RegimeParams | None = None,
) -> RegimeResult:
    """Classify the market at bar ``as_of`` (default: the last bar).

    Order of tests is deliberate and not interchangeable:

    1. **Volatility first.** An ATR in the top or bottom percentile of its own recent
       history disqualifies the symbol whatever the trend looks like. A textbook EMA stack
       during a volatility spike is precisely the setup that gaps through a 0.125% stop,
       so it must not be reachable as TRENDING.
    2. **Breakout / breakdown**, which are more specific than trend and would otherwise be
       swallowed by it.
    3. **Trend, then range.**
    4. Anything left is ambiguous -> SKIP.
    """
    params = params or RegimeParams()

    if len(candles) == 0:
        return _skip("no candles")

    if as_of is None:
        as_of = len(candles) - 1
    as_of = int(as_of)
    if not 0 <= as_of < len(candles):
        raise ValueError(f"as_of must be in [0, {len(candles) - 1}], got {as_of}")

    # Physically truncate rather than merely promising not to peek. Everything below is
    # computed on the bars a live run would have held at `as_of`, so lookahead is
    # impossible by construction rather than by review.
    view = candles.head(as_of + 1)
    if len(view) < params.min_bars:
        return _skip(f"insufficient history: {len(view)} bars < {params.min_bars}")

    close = view.close
    price = float(close[-1])
    if not np.isfinite(price) or price <= 0:
        return _skip("last close is not a usable price")

    atr_series = atr(view.high, view.low, close, params.atr_period)
    atr_now = float(atr_series[-1])
    if not np.isfinite(atr_now) or atr_now <= 0:
        return _skip("ATR unavailable")
    atr_pct = atr_now / price

    adx_now = float(adx(view.high, view.low, close, params.adx_period)[-1])
    ema_fast = float(ema(close, 9)[-1])
    ema_mid = float(ema(close, 21)[-1])
    ema_slow = float(ema(close, 50)[-1])
    ema_anchor = float(ema(close, 200)[-1])
    if not np.isfinite([ema_fast, ema_mid, ema_slow, ema_anchor]).all():
        return _skip("EMA stack unavailable")

    separation = abs(ema_fast - ema_mid) / price

    # ATR percentile against its own recent history: "volatile" only means anything
    # relative to what this symbol has been doing. A fixed ATR% threshold would call
    # every synthetic quiet and every alt violent. See atr_percentile for the tie rule.
    history = atr_series[-params.atr_lookback:] / close[-params.atr_lookback:]
    percentile = atr_percentile(history, atr_pct)

    metrics = {
        "price": price,
        "atr": atr_now,
        "atr_pct": atr_pct,
        "atr_percentile": percentile,
        "adx": adx_now,
        "ema_separation": separation,
        "ema9": ema_fast,
        "ema21": ema_mid,
        "ema50": ema_slow,
        "ema200": ema_anchor,
    }

    if not np.isfinite(adx_now):
        return _skip("ADX unavailable", metrics)
    if not np.isfinite(percentile):
        return _skip("ATR history unavailable", metrics)

    # --- 1. volatility gates ------------------------------------------------
    if percentile >= params.atr_high_percentile:
        return RegimeResult(
            Regime.HIGH_VOLATILITY,
            f"ATR {atr_pct * 100:.3f}% is at the {percentile * 100:.0f}th percentile "
            f"of its last {params.atr_lookback} bars",
            0,
            metrics,
        )
    if percentile <= params.atr_low_percentile:
        return RegimeResult(
            Regime.LOW_VOLATILITY,
            f"ATR {atr_pct * 100:.3f}% is at the {percentile * 100:.0f}th percentile; "
            "too little movement to clear fees",
            0,
            metrics,
        )

    # --- 2. breakout / breakdown -------------------------------------------
    rvol_series = relative_volume(view.volume, params.rvol_period)
    rvol = float(rvol_series[-1])
    metrics["rvol"] = rvol

    highs = confirmed_swing_levels(
        view.high, as_of=len(view) - 1, left=params.swing_left,
        right=params.swing_right, lookback=params.breakout_lookback, high=True,
    )
    lows = confirmed_swing_levels(
        view.low, as_of=len(view) - 1, left=params.swing_left,
        right=params.swing_right, lookback=params.breakout_lookback, high=False,
    )
    buffer = params.breakout_atr_buffer * atr_now

    if highs.size:
        ceiling = float(highs.max())
        metrics["structure_high"] = ceiling
        if price > ceiling + buffer and rvol >= params.breakout_min_rvol:
            return RegimeResult(
                Regime.BREAKOUT,
                f"close {price:.6g} cleared structure {ceiling:.6g} by "
                f"{params.breakout_atr_buffer:.2f} ATR on {rvol:.2f}x volume",
                +1,
                metrics,
            )
    if lows.size:
        floor = float(lows.min())
        metrics["structure_low"] = floor
        if price < floor - buffer and rvol >= params.breakout_min_rvol:
            return RegimeResult(
                Regime.BREAKDOWN,
                f"close {price:.6g} broke structure {floor:.6g} by "
                f"{params.breakout_atr_buffer:.2f} ATR on {rvol:.2f}x volume",
                -1,
                metrics,
            )

    # --- 3. trend -----------------------------------------------------------
    stacked_up = ema_fast > ema_mid > ema_slow and price > ema_anchor
    stacked_down = ema_fast < ema_mid < ema_slow and price < ema_anchor

    if adx_now >= params.adx_trending and separation >= params.ema_separation_trend:
        if stacked_up:
            return RegimeResult(
                Regime.TRENDING,
                f"EMAs stacked up, ADX {adx_now:.1f}, separation {separation * 100:.3f}%",
                +1,
                metrics,
            )
        if stacked_down:
            return RegimeResult(
                Regime.TRENDING,
                f"EMAs stacked down, ADX {adx_now:.1f}, separation {separation * 100:.3f}%",
                -1,
                metrics,
            )
        # Strong directional movement the EMAs do not corroborate — a turn in progress.
        return _skip(
            f"ADX {adx_now:.1f} is trending but the EMA stack disagrees", metrics
        )

    # --- 4. range -----------------------------------------------------------
    if adx_now <= params.adx_ranging and separation <= params.ema_separation_chop:
        return RegimeResult(
            Regime.RANGING,
            f"ADX {adx_now:.1f} with EMAs entangled ({separation * 100:.3f}% apart)",
            0,
            metrics,
        )

    # Between the two — trending too weakly to trade, ranging too loosely to fade.
    # Say which half failed: "ADX is between the thresholds" is often not the reason.
    if adx_now >= params.adx_trending:
        detail = (
            f"ADX {adx_now:.1f} is trending but the EMAs are only "
            f"{separation * 100:.3f}% apart (need {params.ema_separation_trend * 100:.3f}%)"
        )
    elif adx_now <= params.adx_ranging:
        detail = (
            f"ADX {adx_now:.1f} is rangebound but the EMAs are "
            f"{separation * 100:.3f}% apart (need <= {params.ema_separation_chop * 100:.3f}%)"
        )
    else:
        detail = (
            f"ADX {adx_now:.1f} sits between {params.adx_ranging} and "
            f"{params.adx_trending} — neither trending nor ranging"
        )
    return _skip(f"ambiguous: {detail}", metrics)
