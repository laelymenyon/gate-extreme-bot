"""PHASE 5 tests — regime detection.

Regimes are asserted from hand-built price paths whose character is obvious by
construction (a monotonic ramp *is* a trend; a tight oscillation *is* chop), so a bug in
strategy/regime.py cannot make its own test pass.
"""

from __future__ import annotations

import numpy as np
import pytest

from strategy.indicators import Candles, adx
from strategy.regime import (
    Regime,
    RegimeParams,
    RegimeResult,
    atr_percentile,
    classify,
)


def candles(close, *, wick=0.0006, volume=1000.0, start=0.0, step=60.0):
    """OHLC around a close path, with proportional wicks so ATR tracks price."""
    close = np.asarray(close, dtype=float)
    n = len(close)
    open_ = np.concatenate([[close[0]], close[:-1]])
    return Candles(
        time=start + np.arange(n, dtype=float) * step,
        open=open_,
        high=np.maximum(open_, close) * (1 + wick),
        low=np.minimum(open_, close) * (1 - wick),
        close=close,
        volume=np.full(n, volume),
    )


def ramp(n=300, slope=0.002, start=100.0):
    return start * np.exp(np.arange(n) * slope)


def chop(n=300, amplitude=0.004, period=8, start=100.0):
    return start * (1 + amplitude * np.sin(2 * np.pi * np.arange(n) / period))


PARAMS = RegimeParams()

# The volatility gate deliberately outranks everything else, so trend/chop tests widen it
# out of the way and assert one thing at a time. A perfectly smooth exponential ramp has
# an ATR% that climbs monotonically toward its asymptote, which makes every bar the
# highest of its last 100 — real enough, but it is a fact about the fixture, not about
# trend detection. `test_volatility_veto_outranks_trend` covers the interaction directly.
TREND_PARAMS = RegimeParams(atr_high_percentile=1.01, atr_low_percentile=-0.01)


# --- contracts -------------------------------------------------------------

def test_classify_returns_regime_read():
    read = classify(candles(ramp()))
    assert isinstance(read, RegimeResult)
    assert isinstance(read.reason, str) and read.reason


def test_every_regime_member_is_distinct():
    values = [r.value for r in Regime]
    assert len(values) == len(set(values))


def test_insufficient_history_is_skip_not_guess():
    read = classify(candles(ramp(n=50)))
    assert read.regime is None
    assert "insufficient history" in read.reason


def test_empty_candles_is_skip():
    empty = Candles(*(np.array([], dtype=float) for _ in range(6)))
    assert classify(empty).regime is None


def test_classify_does_not_mutate_input():
    c = candles(ramp())
    before = [a.copy() for a in (c.open, c.high, c.low, c.close, c.volume)]
    classify(c)
    for arr, orig in zip((c.open, c.high, c.low, c.close, c.volume), before):
        np.testing.assert_array_equal(arr, orig)


def test_metrics_are_reported_for_diagnosis():
    read = classify(candles(ramp()))
    assert {"adx", "ema_separation", "atr_pct"} <= set(read.metrics)


# --- trend vs chop ---------------------------------------------------------

def test_monotonic_rise_is_trending_up():
    read = classify(candles(ramp()), params=TREND_PARAMS)
    assert read.regime is Regime.TRENDING
    assert read.direction == 1


def test_monotonic_fall_is_trending_down():
    read = classify(candles(ramp()[::-1].copy()), params=TREND_PARAMS)
    assert read.regime is Regime.TRENDING
    assert read.direction == -1


def test_trend_direction_matches_ema_stack():
    read = classify(candles(ramp()), params=TREND_PARAMS)
    assert read.metrics["ema_separation"] > 0
    assert read.direction == 1


def test_tight_oscillation_is_not_trending():
    read = classify(candles(chop()), params=TREND_PARAMS)
    assert read.regime is not Regime.TRENDING


def test_ranging_has_no_direction():
    read = classify(candles(chop()), params=TREND_PARAMS)
    if read.regime is Regime.RANGING:
        assert read.direction == 0


def test_high_adx_without_ema_separation_is_not_trending():
    """ADX alone must not be enough — the message should say which half failed."""
    params = RegimeParams(ema_separation_trend=0.5,  # unreachable separation
                          atr_high_percentile=1.01, atr_low_percentile=-0.01)
    read = classify(candles(ramp()), params=params)
    assert read.regime is not Regime.TRENDING
    assert "EMAs" in read.reason


def test_ambiguous_reason_does_not_misreport_adx_position():
    """A trending ADX blocked by separation must not be described as 'between'."""
    params = RegimeParams(ema_separation_trend=0.5,
                          atr_high_percentile=1.01, atr_low_percentile=-0.01)
    read = classify(candles(ramp()), params=params)
    adx_now = read.metrics["adx"]
    if adx_now >= params.adx_trending:
        assert "between" not in read.reason


# --- volatility ------------------------------------------------------------

def test_constant_volatility_is_not_flagged_high():
    """A perfectly steady market must not read as a volatility extreme.

    With naive `(history <= now).mean()` every value ties at percentile 1.0 and steady
    markets get flagged HIGH_VOLATILITY, suspending trading exactly when it is safest.
    """
    steady = candles(ramp(slope=0.0))
    read = classify(steady)
    assert read.regime is not Regime.HIGH_VOLATILITY


def test_atr_percentile_of_constant_series_is_midrank():
    values = np.full(100, 3.0)
    assert atr_percentile(values, 3.0) == pytest.approx(0.5)


def test_atr_percentile_is_bounded():
    rng = np.random.default_rng(0)
    history = rng.uniform(0, 1, 200)
    for probe in (-1.0, 0.0, 0.5, 1.0, 2.0):
        assert 0.0 <= atr_percentile(history, probe) <= 1.0


def test_atr_percentile_ranks_extremes_correctly():
    history = np.arange(1.0, 101.0)
    assert atr_percentile(history, 0.0) < 0.05
    assert atr_percentile(history, 1000.0) > 0.95


def test_volatility_spike_is_flagged_high():
    close = ramp(n=300, slope=0.0005)
    c = candles(close)
    c.high[-30:] = c.close[-30:] * 1.05
    c.low[-30:] = c.close[-30:] * 0.95
    read = classify(c)
    assert read.regime is Regime.HIGH_VOLATILITY


def test_volatility_veto_outranks_trend():
    """A screaming trend inside a volatility blowout is still refused."""
    c = candles(ramp())
    c.high[-30:] = c.close[-30:] * 1.10
    c.low[-30:] = c.close[-30:] * 0.90
    assert classify(c).regime is Regime.HIGH_VOLATILITY


def test_dead_volatility_is_flagged_low():
    close = ramp(n=300, slope=0.0008)
    c = candles(close)
    c.high[-40:] = c.close[-40:] * 1.000001
    c.low[-40:] = c.close[-40:] * 0.999999
    read = classify(c)
    assert read.regime is Regime.LOW_VOLATILITY


def test_widened_percentile_bounds_disable_the_volatility_gate():
    params = RegimeParams(atr_high_percentile=1.01, atr_low_percentile=-0.01)
    c = candles(ramp())
    c.high[-30:] = c.close[-30:] * 1.10
    c.low[-30:] = c.close[-30:] * 0.90
    assert classify(c, params=params).regime is not Regime.HIGH_VOLATILITY


# --- lookahead -------------------------------------------------------------

def test_classification_ignores_bars_after_as_of():
    """Appending future bars must not change a past classification."""
    close = ramp(n=280)
    base = classify(candles(close))
    extended = np.concatenate([close, close[-1] * np.array([1.5, 0.5, 1.4, 0.6])])
    later = classify(candles(extended), as_of=len(close) - 1)
    assert later.regime is base.regime
    assert later.direction == base.direction
    assert later.metrics["adx"] == pytest.approx(base.metrics["adx"])


@pytest.mark.parametrize("path", [ramp(n=300), chop(n=300), ramp(n=300)[::-1].copy()])
def test_truncated_series_matches_as_of_at_every_bar(path):
    """What live sees at bar i must equal what a backtest sees with as_of=i."""
    full = candles(path)
    for i in range(240, len(path)):
        live = classify(full.head(i + 1))
        replay = classify(full, as_of=i)
        assert live.regime is replay.regime, f"bar {i}"
        assert live.direction == replay.direction, f"bar {i}"


def test_as_of_beyond_series_is_rejected():
    c = candles(ramp(n=300))
    with pytest.raises((ValueError, IndexError)):
        classify(c, as_of=len(c))


def test_adx_used_by_regime_has_no_lookahead():
    """Guard the indicator the classifier leans on hardest."""
    path = ramp(n=300, slope=0.001)
    c = candles(path)
    full = adx(c.high, c.low, c.close, 14)
    for i in (250, 275, 299):
        truncated = adx(c.high[: i + 1], c.low[: i + 1], c.close[: i + 1], 14)
        assert truncated[i] == pytest.approx(full[i], rel=1e-12)
