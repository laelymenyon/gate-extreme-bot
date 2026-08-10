"""Technical indicators in pure numpy (no TA-Lib).

PHASE 4.

Design rules, chosen so backtest and live share one code path:

* **Length-preserving.** Every function returns an array the same length as its input.
  Warm-up positions hold ``NaN``. Index *i* of the output always corresponds to bar *i*
  of the input, so nothing has to be re-aligned downstream.
* **NaN means "not enough data", never a number.** Insufficient history yields all-NaN
  rather than a partially-warmed value, keeping the fail-closed convention used by the
  market-data feed: callers must check ``isnan`` before acting on a value.
* **Pure functions.** No I/O, no config, no state.
* **Wilder smoothing** for RSI and ATR, **SMA-seeded EMA** for EMA/MACD — the
  conventions charting platforms use, so an EMA 200 here matches a 200 EMA on the chart
  instead of drifting for hundreds of bars.

Nothing here decides anything: no signals, no scoring, no thresholds. That is Phase 5.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

__all__ = [
    "Candles",
    "MACDResult",
    "sma",
    "ema",
    "rsi",
    "macd",
    "true_range",
    "atr",
    "adx",
    "vwap",
    "rolling_vwap",
    "volume_ma",
    "relative_volume",
    "swing_highs",
    "swing_lows",
    "nearest_support",
    "nearest_resistance",
    "confirmed_swing_levels",
]


def _as_float_array(values: Sequence[float] | np.ndarray, name: str) -> np.ndarray:
    """Coerce to a 1-D float64 array, rejecting shapes that would silently misbehave."""
    array = np.asarray(values, dtype=np.float64)
    if array.ndim != 1:
        raise ValueError(f"{name} must be 1-dimensional, got shape {array.shape}")
    return array


def _check_period(period: int, name: str = "period") -> int:
    if isinstance(period, bool) or not isinstance(period, (int, np.integer)):
        raise ValueError(f"{name} must be an int, got {period!r}")
    period = int(period)
    if period < 1:
        raise ValueError(f"{name} must be >= 1, got {period}")
    return period


def _nan_like(array: np.ndarray) -> np.ndarray:
    return np.full(array.shape, np.nan, dtype=np.float64)


def _require_same_length(**arrays: np.ndarray) -> int:
    lengths = {name: len(a) for name, a in arrays.items()}
    if len(set(lengths.values())) > 1:
        raise ValueError(f"inputs must be the same length, got {lengths}")
    return next(iter(lengths.values()), 0)


# --- OHLCV container -------------------------------------------------------

@dataclass(frozen=True)
class Candles:
    """OHLCV series, oldest bar first.

    Gate.io returns candlesticks ascending by time with string prices, integer contract
    volume in ``v``, and settle-currency turnover in ``sum``. ``from_gate`` converts that
    payload as-is; it does not drop the in-progress final bar, because whether to use a
    forming bar is a strategy decision, not an indicator one.
    """

    time: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    turnover: np.ndarray | None = None

    def __len__(self) -> int:
        return len(self.close)

    def __post_init__(self) -> None:
        _require_same_length(
            time=self.time, open=self.open, high=self.high,
            low=self.low, close=self.close, volume=self.volume,
        )
        if self.turnover is not None and len(self.turnover) != len(self.close):
            raise ValueError("turnover must match the length of close")
        if len(self.close) and np.any(self.high + 1e-9 < self.low):
            raise ValueError("found a bar with high < low")

    @classmethod
    def from_gate(cls, rows: Iterable[Mapping[str, Any]]) -> "Candles":
        """Build from ``GET /futures/{settle}/candlesticks`` rows."""
        rows = list(rows)
        if not rows:
            empty = np.array([], dtype=np.float64)
            return cls(empty, empty, empty, empty, empty, empty, empty)

        def column(key: str) -> np.ndarray:
            return np.array([float(row[key]) for row in rows], dtype=np.float64)

        return cls(
            time=column("t"),
            open=column("o"),
            high=column("h"),
            low=column("l"),
            close=column("c"),
            volume=column("v"),
            turnover=column("sum") if "sum" in rows[0] else None,
        )

    def head(self, count: int) -> "Candles":
        """The first ``count`` bars — exactly what a live run would have held then.

        Every Phase 5 module takes its history through this, so "replay bar *i*" and
        "live at bar *i*" are the same object rather than two code paths that have to be
        kept in agreement by hand. Tests use it to assert that a decision made on the
        full array with ``as_of=i`` matches one made on a truncated array.
        """
        count = int(count)
        if count < 0:
            raise ValueError(f"count must be >= 0, got {count}")
        count = min(count, len(self))
        return Candles(
            time=self.time[:count],
            open=self.open[:count],
            high=self.high[:count],
            low=self.low[:count],
            close=self.close[:count],
            volume=self.volume[:count],
            turnover=None if self.turnover is None else self.turnover[:count],
        )


@dataclass(frozen=True)
class MACDResult:
    macd: np.ndarray
    signal: np.ndarray
    histogram: np.ndarray

# --- moving averages -------------------------------------------------------

def sma(values: Sequence[float] | np.ndarray, period: int) -> np.ndarray:
    """Simple moving average. First valid index is ``period - 1``."""
    array = _as_float_array(values, "values")
    period = _check_period(period)
    out = _nan_like(array)
    if len(array) < period:
        return out

    cumulative = np.cumsum(np.insert(array, 0, 0.0))
    out[period - 1:] = (cumulative[period:] - cumulative[:-period]) / period
    return out


def ema(values: Sequence[float] | np.ndarray, period: int) -> np.ndarray:
    """Exponential moving average, seeded with the SMA of the first ``period`` values.

    alpha = 2 / (period + 1). Single-pass O(n); the recurrence is sequential by
    definition, so this is a loop rather than a vector op.
    """
    array = _as_float_array(values, "values")
    period = _check_period(period)
    out = _nan_like(array)
    if len(array) < period:
        return out

    alpha = 2.0 / (period + 1.0)
    previous = float(np.mean(array[:period]))
    out[period - 1] = previous
    for i in range(period, len(array)):
        previous = alpha * array[i] + (1.0 - alpha) * previous
        out[i] = previous
    return out


def _wilder_rma(values: np.ndarray, period: int) -> np.ndarray:
    """Wilder's smoothing: avg = (prev * (period - 1) + current) / period.

    Equivalent to an EMA with alpha = 1/period, seeded with the mean of the first
    ``period`` samples. RSI and ATR use this, as Wilder originally defined them.
    """
    out = _nan_like(values)
    if len(values) < period:
        return out

    previous = float(np.mean(values[:period]))
    out[period - 1] = previous
    for i in range(period, len(values)):
        previous = (previous * (period - 1) + values[i]) / period
        out[i] = previous
    return out


# --- oscillators -----------------------------------------------------------

def rsi(close: Sequence[float] | np.ndarray, period: int = 14) -> np.ndarray:
    """Wilder's RSI. First valid index is ``period`` (it needs ``period`` deltas).

    Boundary conventions:
      * average loss zero, average gain positive -> 100.0
      * average gain zero, average loss positive -> 0.0
      * a perfectly flat window (neither) -> 50.0, the neutral reading, rather than
        NaN — a flat market is a known state, not missing data.
    """
    array = _as_float_array(close, "close")
    period = _check_period(period)
    out = _nan_like(array)
    if len(array) <= period:
        return out

    delta = np.diff(array)
    gains = np.where(delta > 0.0, delta, 0.0)
    losses = np.where(delta < 0.0, -delta, 0.0)

    avg_gain = _wilder_rma(gains, period)
    avg_loss = _wilder_rma(losses, period)

    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.divide(avg_gain, avg_loss)
        values = 100.0 - (100.0 / (1.0 + rs))

    flat = (avg_gain == 0.0) & (avg_loss == 0.0)
    only_gains = (avg_loss == 0.0) & (avg_gain > 0.0)
    only_losses = (avg_gain == 0.0) & (avg_loss > 0.0)
    values = np.where(flat, 50.0, values)
    values = np.where(only_gains, 100.0, values)
    values = np.where(only_losses, 0.0, values)

    # delta[i] is the move from bar i into bar i+1, so shift by one to re-align.
    out[1:] = values
    return out


def macd(
    close: Sequence[float] | np.ndarray,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> MACDResult:
    """MACD line, signal line, and histogram.

    The signal EMA is seeded from the first ``signal`` *valid* MACD values rather than
    from the raw array, so the MACD warm-up NaNs cannot leak into it.
    """
    array = _as_float_array(close, "close")
    fast = _check_period(fast, "fast")
    slow = _check_period(slow, "slow")
    signal = _check_period(signal, "signal")
    if fast >= slow:
        raise ValueError(f"fast ({fast}) must be < slow ({slow})")

    macd_line = ema(array, fast) - ema(array, slow)

    signal_line = _nan_like(array)
    valid = ~np.isnan(macd_line)
    if valid.any():
        start = int(np.argmax(valid))
        signal_line[start:] = ema(macd_line[start:], signal)

    return MACDResult(
        macd=macd_line,
        signal=signal_line,
        histogram=macd_line - signal_line,
    )


# --- volatility ------------------------------------------------------------

def true_range(
    high: Sequence[float] | np.ndarray,
    low: Sequence[float] | np.ndarray,
    close: Sequence[float] | np.ndarray,
) -> np.ndarray:
    """True range per bar. Bar 0 is ``high - low`` (no previous close exists).

    Includes the gap terms, so an overnight or weekend gap widens TR instead of being
    invisible — which matters for anything sized off ATR.
    """
    h = _as_float_array(high, "high")
    l = _as_float_array(low, "low")
    c = _as_float_array(close, "close")
    _require_same_length(high=h, low=l, close=c)

    out = np.empty(len(h), dtype=np.float64)
    if len(h) == 0:
        return out
    out[0] = h[0] - l[0]
    if len(h) > 1:
        previous_close = c[:-1]
        out[1:] = np.maximum.reduce([
            h[1:] - l[1:],
            np.abs(h[1:] - previous_close),
            np.abs(l[1:] - previous_close),
        ])
    return out


def atr(
    high: Sequence[float] | np.ndarray,
    low: Sequence[float] | np.ndarray,
    close: Sequence[float] | np.ndarray,
    period: int = 14,
) -> np.ndarray:
    """Wilder's ATR. First valid index is ``period - 1``."""
    period = _check_period(period)
    return _wilder_rma(true_range(high, low, close), period)


def adx(
    high: Sequence[float] | np.ndarray,
    low: Sequence[float] | np.ndarray,
    close: Sequence[float] | np.ndarray,
    period: int = 14,
) -> np.ndarray:
    """Wilder's ADX — trend *strength*, with no opinion on direction.

    The regime classifier needs to tell a real trend from an EMA stack that merely
    happens to be in order during chop, which is what ADX measures and a moving-average
    comparison cannot. Rising and falling markets both score high; only the presence of
    directional movement matters.

    Wilder's construction: directional movement is the excess of one side's move over the
    other (ties and inside bars count as zero), both legs and the true range are
    RMA-smoothed over ``period``, DX is their normalised difference, and ADX is a second
    RMA of DX. That double smoothing puts the first valid index at ``2 * period - 1``.
    A period of flat or zero-range bars yields DX 0, not NaN — no movement is a reading,
    not missing data.
    """
    h = _as_float_array(high, "high")
    l = _as_float_array(low, "low")
    c = _as_float_array(close, "close")
    _require_same_length(high=h, low=l, close=c)
    period = _check_period(period)

    out = _nan_like(h)
    if len(h) < 2 * period:
        return out

    up_move = np.diff(h)
    down_move = -np.diff(l)
    # A bar only counts toward one side: the larger, strictly-positive excess wins.
    plus_dm = np.where((up_move > down_move) & (up_move > 0.0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0.0), down_move, 0.0)

    tr = true_range(h, l, c)[1:]
    smooth_tr = _wilder_rma(tr, period)
    smooth_plus = _wilder_rma(plus_dm, period)
    smooth_minus = _wilder_rma(minus_dm, period)

    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * np.divide(smooth_plus, smooth_tr)
        minus_di = 100.0 * np.divide(smooth_minus, smooth_tr)
        total = plus_di + minus_di
        dx = 100.0 * np.divide(np.abs(plus_di - minus_di), total)

    # Zero range over the whole window is a legitimately trendless market.
    dx = np.where((smooth_tr == 0.0) | (total == 0.0), 0.0, dx)

    # Seed the second smoothing from the first *valid* DX; averaging in the warm-up NaNs
    # would poison the seed and leave the whole series NaN.
    smoothed = _nan_like(dx)
    valid = ~np.isnan(dx)
    if valid.any():
        start = int(np.argmax(valid))
        smoothed[start:] = _wilder_rma(dx[start:], period)

    # diff() dropped bar 0, so shift back to align index i with bar i.
    out[1:] = smoothed
    return out


# --- volume-weighted -------------------------------------------------------

def _typical_price(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    return (high + low + close) / 3.0


def vwap(
    high: Sequence[float] | np.ndarray,
    low: Sequence[float] | np.ndarray,
    close: Sequence[float] | np.ndarray,
    volume: Sequence[float] | np.ndarray,
) -> np.ndarray:
    """Anchored (cumulative) VWAP from bar 0, using typical price.

    The contract multiplier cancels in a weighted average, so this is correct for
    Gate.io's contract-denominated ``v`` without needing ``quanto_multiplier``.

    The caller chooses the anchor by slicing the input (e.g. to a session open). Bars
    with zero cumulative volume stay NaN.
    """
    h = _as_float_array(high, "high")
    l = _as_float_array(low, "low")
    c = _as_float_array(close, "close")
    v = _as_float_array(volume, "volume")
    _require_same_length(high=h, low=l, close=c, volume=v)
    if np.any(v < 0):
        raise ValueError("volume must be non-negative")

    cumulative_volume = np.cumsum(v)
    cumulative_pv = np.cumsum(_typical_price(h, l, c) * v)
    out = _nan_like(h)
    nonzero = cumulative_volume > 0
    out[nonzero] = cumulative_pv[nonzero] / cumulative_volume[nonzero]
    return out


def rolling_vwap(
    high: Sequence[float] | np.ndarray,
    low: Sequence[float] | np.ndarray,
    close: Sequence[float] | np.ndarray,
    volume: Sequence[float] | np.ndarray,
    period: int = 20,
) -> np.ndarray:
    """VWAP over a trailing window. First valid index is ``period - 1``."""
    h = _as_float_array(high, "high")
    l = _as_float_array(low, "low")
    c = _as_float_array(close, "close")
    v = _as_float_array(volume, "volume")
    _require_same_length(high=h, low=l, close=c, volume=v)
    period = _check_period(period)
    if np.any(v < 0):
        raise ValueError("volume must be non-negative")

    out = _nan_like(h)
    if len(h) < period:
        return out

    pv = _typical_price(h, l, c) * v
    cumulative_pv = np.cumsum(np.insert(pv, 0, 0.0))
    cumulative_v = np.cumsum(np.insert(v, 0, 0.0))
    window_pv = cumulative_pv[period:] - cumulative_pv[:-period]
    window_v = cumulative_v[period:] - cumulative_v[:-period]

    with np.errstate(divide="ignore", invalid="ignore"):
        out[period - 1:] = np.where(window_v > 0, window_pv / window_v, np.nan)
    return out


def volume_ma(volume: Sequence[float] | np.ndarray, period: int = 20) -> np.ndarray:
    """Simple moving average of volume."""
    return sma(volume, period)


def relative_volume(
    volume: Sequence[float] | np.ndarray, period: int = 20
) -> np.ndarray:
    """This bar's volume over the average of the ``period`` bars **before** it.

    1.0 is an average bar, 3.0 a volume spike. First valid index is ``period``: the
    baseline needs ``period`` prior bars, and the current bar is deliberately not one of
    them.

    **The current bar is excluded from its own baseline.** Including it dilutes the very
    spike it is meant to measure. At ``period=20`` an inclusive average reports a true 4x
    bar as 3.48x, needs a 4.75x bar to read 4.0, and can never exceed 20.0 however
    extreme the bar gets. The thresholds this feeds — ``filters.btc_volume_spike_multiple:
    4.0``, the volume scoring category — are written in the intuitive unit ("this bar is
    4x normal"), so an inclusive baseline would make a protective filter fire late or not
    at all. Excluded, a 4x bar reads exactly 4.0 and the ratio is unbounded above.

    NaN where the baseline is zero, so a dead market cannot read as an infinite spike.
    """
    v = _as_float_array(volume, "volume")
    period = _check_period(period)
    out = _nan_like(v)
    if len(v) <= period:
        return out

    # sma index i averages bars i-period+1..i, so the baseline that excludes bar i is
    # the SMA at i-1.
    baseline = volume_ma(v, period)
    with np.errstate(divide="ignore", invalid="ignore"):
        out[1:] = np.where(baseline[:-1] > 0, v[1:] / baseline[:-1], np.nan)
    return out


# --- support / resistance --------------------------------------------------
#
# A swing pivot is only *knowable* ``right`` bars after it prints. Every function here
# therefore separates the bar a pivot sits on from the bar it becomes visible on, and
# the level lookups refuse to use a pivot before its confirmation bar. Getting this
# wrong is the classic backtest lookahead bug: levels that were not yet discoverable
# make a strategy look prescient in replay and fall apart live. Since backtest and live
# share this code path, the guard has to live here rather than in the caller.

def _pivots(values: np.ndarray, left: int, right: int, high: bool) -> np.ndarray:
    """Boolean mask, True on bars that are strict local extremes.

    Strict on both sides: a plateau of equal highs yields no pivot at all. Equality is
    ambiguous — treating it as a pivot invents two levels one tick apart out of a single
    flat top — so the conservative reading wins. NaN inputs compare False and are
    likewise never pivots.
    """
    n = len(values)
    mask = np.zeros(n, dtype=bool)
    if n < left + right + 1:
        return mask

    index = np.arange(left, n - right)
    center = values[index]
    keep = np.ones(len(index), dtype=bool)
    with np.errstate(invalid="ignore"):
        for k in range(1, left + 1):
            keep &= center > values[index - k] if high else center < values[index - k]
        for k in range(1, right + 1):
            keep &= center > values[index + k] if high else center < values[index + k]
    mask[index[keep]] = True
    return mask


def swing_highs(
    high: Sequence[float] | np.ndarray, left: int = 2, right: int = 2
) -> np.ndarray:
    """Mask of confirmed swing highs, True at the pivot bar itself.

    The pivot at index *i* only becomes visible at bar ``i + right``. The mask marks *i*,
    not the confirmation bar — use :func:`nearest_resistance`, which applies that delay,
    rather than reading this mask at the live edge.
    """
    array = _as_float_array(high, "high")
    return _pivots(array, _check_period(left, "left"), _check_period(right, "right"), True)


def swing_lows(
    low: Sequence[float] | np.ndarray, left: int = 2, right: int = 2
) -> np.ndarray:
    """Mask of confirmed swing lows, True at the pivot bar itself. See :func:`swing_highs`."""
    array = _as_float_array(low, "low")
    return _pivots(array, _check_period(left, "left"), _check_period(right, "right"), False)


def confirmed_swing_levels(
    values: Sequence[float] | np.ndarray,
    as_of: int | None = None,
    left: int = 2,
    right: int = 2,
    lookback: int = 50,
    high: bool = True,
) -> np.ndarray:
    """Prices of the swing pivots that were **discoverable at** ``as_of``.

    A pivot at bar *j* is included only when ``j + right <= as_of`` (its confirmation bar
    has printed) and ``j > as_of - lookback`` (it is still recent structure). Order
    follows the bars, oldest first.

    This is the one place the confirmation rule is written down. ``nearest_support``,
    ``nearest_resistance``, and the regime classifier all route through it, so there is a
    single definition of "a level that existed yet" to audit rather than three copies
    that could drift apart.
    """
    array = _as_float_array(values, "values")
    left = _check_period(left, "left")
    right = _check_period(right, "right")
    lookback = _check_period(lookback, "lookback")

    n = len(array)
    if n == 0:
        return np.array([], dtype=np.float64)

    if as_of is None:
        as_of = n - 1
    else:
        as_of = int(as_of)
        if not 0 <= as_of < n:
            raise ValueError(f"as_of must be in [0, {n - 1}], got {as_of}")

    index = np.flatnonzero(_pivots(array, left, right, high))
    index = index[(index + right <= as_of) & (index > as_of - lookback)]
    return array[index]


def _level(
    values: np.ndarray,
    price: float,
    left: int,
    right: int,
    lookback: int,
    as_of: int | None,
    above: bool,
) -> float:
    if len(values) == 0:
        return float("nan")

    price = float(price)
    if not np.isfinite(price):
        return float("nan")

    levels = confirmed_swing_levels(values, as_of, left, right, lookback, high=above)
    beyond = levels[levels > price] if above else levels[levels < price]
    if beyond.size == 0:
        return float("nan")
    return float(beyond.min() if above else beyond.max())


def nearest_resistance(
    high: Sequence[float] | np.ndarray,
    price: float,
    left: int = 2,
    right: int = 2,
    lookback: int = 50,
    as_of: int | None = None,
) -> float:
    """Lowest confirmed swing high strictly above ``price``, or NaN if there is none.

    Only pivots confirmed at or before ``as_of`` (default: the last bar) and within
    ``lookback`` bars of it are considered — ``lookback`` defaults to the config's
    ``stop_loss.structure_lookback``. NaN means "no level found", not "no resistance":
    a caller sizing a stop off structure must treat it as missing data and fall back,
    never as open space.
    """
    array = _as_float_array(high, "high")
    return _level(
        array, price, _check_period(left, "left"), _check_period(right, "right"),
        _check_period(lookback, "lookback"), as_of, above=True,
    )


def nearest_support(
    low: Sequence[float] | np.ndarray,
    price: float,
    left: int = 2,
    right: int = 2,
    lookback: int = 50,
    as_of: int | None = None,
) -> float:
    """Highest confirmed swing low strictly below ``price``, or NaN. See :func:`nearest_resistance`."""
    array = _as_float_array(low, "low")
    return _level(
        array, price, _check_period(left, "left"), _check_period(right, "right"),
        _check_period(lookback, "lookback"), as_of, above=False,
    )

