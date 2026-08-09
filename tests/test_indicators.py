"""PHASE 4 tests — technical indicators.

Reference values are computed independently inside the tests (hand-rolled recurrences,
closed-form cases) rather than copied from the implementation, so a bug in
strategy/indicators.py cannot make its own test pass.
"""

from __future__ import annotations

import numpy as np
import pytest

from strategy.indicators import (
    Candles,
    atr,
    ema,
    macd,
    nearest_resistance,
    nearest_support,
    relative_volume,
    rolling_vwap,
    rsi,
    sma,
    swing_highs,
    swing_lows,
    true_range,
    volume_ma,
    vwap,
)

ALL_SERIES_FUNCS = [
    (sma, {"period": 5}),
    (ema, {"period": 5}),
    (rsi, {"period": 14}),
    (volume_ma, {"period": 5}),
    (relative_volume, {"period": 5}),
]


@pytest.fixture
def series():
    """Deterministic pseudo-random OHLCV, long enough to warm a 200 EMA."""
    rng = np.random.default_rng(7)
    close = 100 + np.cumsum(rng.normal(0, 1, 300))
    high = close + rng.uniform(0.1, 1.5, 300)
    low = close - rng.uniform(0.1, 1.5, 300)
    volume = rng.uniform(100, 10_000, 300)
    return high, low, close, volume


# --- shared contracts ------------------------------------------------------

@pytest.mark.parametrize("func,kwargs", ALL_SERIES_FUNCS)
def test_output_length_matches_input(series, func, kwargs):
    _, _, close, _ = series
    assert len(func(close, **kwargs)) == len(close)


@pytest.mark.parametrize("func,kwargs", ALL_SERIES_FUNCS)
def test_insufficient_data_is_all_nan_not_partial(func, kwargs):
    """Fail-closed: too little history yields NaN, never a half-warmed number."""
    short = np.array([1.0, 2.0, 3.0])
    out = func(short, **kwargs)
    assert len(out) == 3
    assert np.all(np.isnan(out))


@pytest.mark.parametrize("func,kwargs", ALL_SERIES_FUNCS)
def test_empty_input_returns_empty(func, kwargs):
    assert len(func(np.array([]), **kwargs)) == 0


@pytest.mark.parametrize("func,kwargs", ALL_SERIES_FUNCS)
def test_inputs_are_not_mutated(series, func, kwargs):
    _, _, close, _ = series
    before = close.copy()
    func(close, **kwargs)
    assert np.array_equal(close, before)


@pytest.mark.parametrize("bad", [0, -1, -14])
def test_non_positive_period_rejected(bad):
    with pytest.raises(ValueError, match=">= 1"):
        ema(np.arange(50.0), bad)


@pytest.mark.parametrize("bad", [1.5, "14", None, True])
def test_non_integer_period_rejected(bad):
    with pytest.raises(ValueError, match="must be an int"):
        sma(np.arange(50.0), bad)


def test_two_dimensional_input_rejected():
    with pytest.raises(ValueError, match="1-dimensional"):
        sma(np.ones((10, 2)), 3)


# --- SMA -------------------------------------------------------------------

def test_sma_known_values():
    values = np.array([1.0, 2, 3, 4, 5, 6])
    out = sma(values, 3)
    assert np.all(np.isnan(out[:2]))
    np.testing.assert_allclose(out[2:], [2.0, 3.0, 4.0, 5.0])


def test_sma_of_constant_is_the_constant():
    out = sma(np.full(20, 7.5), 5)
    np.testing.assert_allclose(out[4:], 7.5)


def test_sma_first_valid_index():
    out = sma(np.arange(10.0), 4)
    assert np.isnan(out[2]) and not np.isnan(out[3])


# --- EMA -------------------------------------------------------------------

@pytest.mark.parametrize("period", [9, 21, 50, 200])
def test_ema_matches_independent_recurrence(series, period):
    """Recompute the SMA-seeded recurrence by hand and compare."""
    _, _, close, _ = series
    alpha = 2.0 / (period + 1.0)
    expected = np.full(len(close), np.nan)
    prev = close[:period].mean()
    expected[period - 1] = prev
    for i in range(period, len(close)):
        prev = alpha * close[i] + (1 - alpha) * prev
        expected[i] = prev
    np.testing.assert_allclose(ema(close, period), expected, equal_nan=True)


@pytest.mark.parametrize("period", [9, 21, 50, 200])
def test_ema_first_valid_index_is_period_minus_one(series, period):
    _, _, close, _ = series
    out = ema(close, period)
    assert np.all(np.isnan(out[: period - 1]))
    assert not np.isnan(out[period - 1])


def test_ema_seed_is_sma_not_first_value():
    """A first-value seed would make EMA200 drift for hundreds of bars."""
    values = np.concatenate([np.full(9, 10.0), np.full(41, 20.0)])
    out = ema(values, 9)
    assert out[8] == pytest.approx(10.0)  # SMA of the first 9


def test_ema_of_constant_is_the_constant():
    out = ema(np.full(60, 42.0), 21)
    np.testing.assert_allclose(out[20:], 42.0)


def test_ema_reacts_faster_than_sma(series):
    """Same period, step change: EMA must move toward the new level sooner."""
    values = np.concatenate([np.full(30, 100.0), np.full(30, 110.0)])
    e, s = ema(values, 10), sma(values, 10)
    assert e[33] > s[33]


def test_shorter_ema_is_more_responsive(series):
    _, _, close, _ = series
    fast, slow = ema(close, 9), ema(close, 50)
    assert np.nanstd(np.diff(fast[200:])) > np.nanstd(np.diff(slow[200:]))


# --- RSI -------------------------------------------------------------------

def test_rsi_range_is_bounded(series):
    _, _, close, _ = series
    out = rsi(close, 14)
    valid = out[~np.isnan(out)]
    assert valid.size > 0
    assert np.all((valid >= 0.0) & (valid <= 100.0))


def test_rsi_first_valid_index_is_period():
    out = rsi(np.arange(100.0), 14)
    assert np.all(np.isnan(out[:14]))
    assert not np.isnan(out[14])


def test_rsi_all_gains_is_100():
    out = rsi(np.arange(1.0, 60.0), 14)
    assert out[20] == pytest.approx(100.0)


def test_rsi_all_losses_is_zero():
    out = rsi(np.arange(60.0, 1.0, -1.0), 14)
    assert out[20] == pytest.approx(0.0)


def test_rsi_flat_series_is_neutral_50_not_nan():
    """A flat market is a known state, not missing data."""
    out = rsi(np.full(60, 100.0), 14)
    assert out[20] == pytest.approx(50.0)
    assert not np.isnan(out[20])


def test_rsi_matches_wilder_by_hand():
    """Independent Wilder recurrence on a small series."""
    close = np.array([44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
                      45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00])
    period = 14
    delta = np.diff(close)
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)
    avg_g = gains[:period].mean()
    avg_l = losses[:period].mean()
    expected_first = 100 - 100 / (1 + avg_g / avg_l)
    assert rsi(close, period)[period] == pytest.approx(expected_first, rel=1e-12)

    avg_g = (avg_g * (period - 1) + gains[period]) / period
    avg_l = (avg_l * (period - 1) + losses[period]) / period
    expected_second = 100 - 100 / (1 + avg_g / avg_l)
    assert rsi(close, period)[period + 1] == pytest.approx(expected_second, rel=1e-12)


def test_rsi_is_not_shifted_off_by_one():
    """A price series that only rises after bar 30 must not read >50 before it."""
    close = np.concatenate([np.full(30, 100.0), 100 + np.arange(1, 31.0)])
    out = rsi(close, 14)
    assert out[25] == pytest.approx(50.0)
    assert out[45] > 90.0


# --- MACD ------------------------------------------------------------------

def test_macd_line_is_fast_minus_slow_ema(series):
    _, _, close, _ = series
    result = macd(close, 12, 26, 9)
    np.testing.assert_allclose(
        result.macd, ema(close, 12) - ema(close, 26), equal_nan=True
    )


def test_macd_histogram_is_macd_minus_signal(series):
    _, _, close, _ = series
    result = macd(close, 12, 26, 9)
    np.testing.assert_allclose(
        result.histogram, result.macd - result.signal, equal_nan=True
    )


def test_macd_warmup_indices(series):
    """MACD valid at slow-1; signal at slow-1 + signal-1, not polluted by NaNs."""
    _, _, close, _ = series
    result = macd(close, 12, 26, 9)
    assert np.all(np.isnan(result.macd[:25])) and not np.isnan(result.macd[25])
    assert np.all(np.isnan(result.signal[:33])) and not np.isnan(result.signal[33])


def test_macd_signal_seeded_from_valid_macd_only(series):
    """Signal at its first valid bar equals the SMA of the first 9 valid MACD values."""
    _, _, close, _ = series
    result = macd(close, 12, 26, 9)
    assert result.signal[33] == pytest.approx(result.macd[25:34].mean(), rel=1e-12)


def test_macd_all_outputs_length_preserving(series):
    _, _, close, _ = series
    result = macd(close, 12, 26, 9)
    for array in (result.macd, result.signal, result.histogram):
        assert len(array) == len(close)


def test_macd_of_constant_is_zero():
    result = macd(np.full(80, 55.0), 12, 26, 9)
    assert result.macd[40] == pytest.approx(0.0, abs=1e-12)
    assert result.histogram[40] == pytest.approx(0.0, abs=1e-12)


def test_macd_positive_in_uptrend_negative_in_downtrend():
    up = np.arange(1.0, 121.0)
    assert macd(up).macd[100] > 0
    assert macd(up[::-1].copy()).macd[100] < 0


def test_macd_rejects_fast_not_less_than_slow():
    with pytest.raises(ValueError, match="must be <"):
        macd(np.arange(100.0), fast=26, slow=12)


def test_macd_insufficient_data_is_all_nan():
    result = macd(np.arange(10.0), 12, 26, 9)
    for array in (result.macd, result.signal, result.histogram):
        assert np.all(np.isnan(array))


# --- true range / ATR ------------------------------------------------------

def test_true_range_first_bar_is_high_minus_low():
    h = np.array([10.0, 11.0]); l = np.array([9.0, 10.0]); c = np.array([9.5, 10.5])
    assert true_range(h, l, c)[0] == pytest.approx(1.0)


def test_true_range_captures_gap_up():
    """Gap terms must dominate: a bar opening far above the prior close has TR > range."""
    h = np.array([10.0, 20.0]); l = np.array([9.0, 19.0]); c = np.array([9.5, 19.5])
    tr = true_range(h, l, c)
    assert tr[1] == pytest.approx(20.0 - 9.5)   # high - previous close
    assert tr[1] > (h[1] - l[1])


def test_true_range_captures_gap_down():
    h = np.array([20.0, 10.0]); l = np.array([19.0, 9.0]); c = np.array([19.5, 9.5])
    tr = true_range(h, l, c)
    assert tr[1] == pytest.approx(19.5 - 9.0)   # previous close - low


def test_true_range_never_negative(series):
    high, low, close, _ = series
    assert np.all(true_range(high, low, close) >= 0.0)


def test_atr_first_valid_index_and_positivity(series):
    high, low, close, _ = series
    out = atr(high, low, close, 14)
    assert np.all(np.isnan(out[:13])) and not np.isnan(out[13])
    assert np.all(out[13:] > 0.0)


def test_atr_seed_is_mean_of_first_true_ranges(series):
    high, low, close, _ = series
    tr = true_range(high, low, close)
    assert atr(high, low, close, 14)[13] == pytest.approx(tr[:14].mean(), rel=1e-12)


def test_atr_follows_wilder_recurrence(series):
    high, low, close, _ = series
    tr = true_range(high, low, close)
    out = atr(high, low, close, 14)
    expected = (out[13] * 13 + tr[14]) / 14
    assert out[14] == pytest.approx(expected, rel=1e-12)


def test_atr_of_constant_range_equals_that_range():
    h = np.full(40, 101.0); l = np.full(40, 100.0); c = np.full(40, 100.5)
    np.testing.assert_allclose(atr(h, l, c, 14)[13:], 1.0)


def test_atr_grows_when_volatility_expands():
    calm_h = np.full(40, 100.5); calm_l = np.full(40, 100.0); calm_c = np.full(40, 100.2)
    wild_h = np.concatenate([calm_h, np.full(40, 110.0)])
    wild_l = np.concatenate([calm_l, np.full(40, 100.0)])
    wild_c = np.concatenate([calm_c, np.full(40, 105.0)])
    out = atr(wild_h, wild_l, wild_c, 14)
    assert out[70] > out[35]


def test_atr_mismatched_lengths_rejected():
    with pytest.raises(ValueError, match="same length"):
        atr(np.ones(10), np.ones(9), np.ones(10), 5)


# --- VWAP ------------------------------------------------------------------

def test_vwap_first_bar_is_its_typical_price():
    h = np.array([11.0]); l = np.array([9.0]); c = np.array([10.0]); v = np.array([5.0])
    assert vwap(h, l, c, v)[0] == pytest.approx(10.0)


def test_vwap_known_two_bar_value():
    h = np.array([11.0, 21.0]); l = np.array([9.0, 19.0])
    c = np.array([10.0, 20.0]); v = np.array([1.0, 3.0])
    # typical prices 10 and 20, weights 1 and 3 -> (10 + 60) / 4
    np.testing.assert_allclose(vwap(h, l, c, v), [10.0, 17.5])


def test_vwap_stays_within_the_price_envelope(series):
    high, low, close, volume = series
    out = vwap(high, low, close, volume)
    assert np.nanmin(out) >= low.min() and np.nanmax(out) <= high.max()


def test_vwap_ignores_a_scaled_volume_unit():
    """The contract multiplier cancels, so scaling volume must not move VWAP."""
    high, low, close = np.array([11.0, 12, 13]), np.array([9.0, 10, 11]), np.array([10.0, 11, 12])
    volume = np.array([100.0, 200, 300])
    np.testing.assert_allclose(
        vwap(high, low, close, volume),
        vwap(high, low, close, volume * 0.0001),
    )


def test_vwap_zero_volume_prefix_is_nan_then_recovers():
    h = np.array([11.0, 11.0]); l = np.array([9.0, 9.0]); c = np.array([10.0, 10.0])
    out = vwap(h, l, c, np.array([0.0, 5.0]))
    assert np.isnan(out[0]) and out[1] == pytest.approx(10.0)


def test_vwap_of_flat_price_equals_that_price(series):
    _, _, _, volume = series
    n = len(volume)
    out = vwap(np.full(n, 101.0), np.full(n, 99.0), np.full(n, 100.0), volume)
    np.testing.assert_allclose(out, 100.0)


def test_vwap_negative_volume_rejected():
    with pytest.raises(ValueError, match="non-negative"):
        vwap(np.ones(3), np.ones(3), np.ones(3), np.array([1.0, -1.0, 1.0]))


def test_rolling_vwap_window_and_warmup(series):
    high, low, close, volume = series
    out = rolling_vwap(high, low, close, volume, 20)
    assert len(out) == len(close)
    assert np.all(np.isnan(out[:19])) and not np.isnan(out[19])


def test_rolling_vwap_matches_manual_window(series):
    high, low, close, volume = series
    out = rolling_vwap(high, low, close, volume, 20)
    typical = (high[30:50] + low[30:50] + close[30:50]) / 3.0
    expected = (typical * volume[30:50]).sum() / volume[30:50].sum()
    assert out[49] == pytest.approx(expected, rel=1e-12)


def test_rolling_vwap_forgets_old_bars_but_anchored_does_not():
    """The distinguishing property: a rolling window drops history, anchored keeps it."""
    h = np.concatenate([np.full(20, 11.0), np.full(20, 111.0)])
    l = np.concatenate([np.full(20, 9.0), np.full(20, 109.0)])
    c = np.concatenate([np.full(20, 10.0), np.full(20, 110.0)])
    v = np.full(40, 5.0)
    assert rolling_vwap(h, l, c, v, 20)[39] == pytest.approx(110.0)
    assert vwap(h, l, c, v)[39] == pytest.approx(60.0)


def test_rolling_vwap_all_zero_volume_window_is_nan():
    out = rolling_vwap(np.ones(10), np.ones(10), np.ones(10), np.zeros(10), 5)
    assert np.all(np.isnan(out))


def test_rolling_vwap_insufficient_data_is_all_nan():
    out = rolling_vwap(np.ones(3), np.ones(3), np.ones(3), np.ones(3), 20)
    assert len(out) == 3 and np.all(np.isnan(out))


# --- volume ---------------------------------------------------------------

def test_volume_ma_is_sma_of_volume(series):
    _, _, _, volume = series
    np.testing.assert_allclose(volume_ma(volume, 20), sma(volume, 20), equal_nan=True)


def test_volume_ma_known_values():
    out = volume_ma(np.array([10.0, 20, 30, 40]), 2)
    np.testing.assert_allclose(out[1:], [15.0, 25.0, 35.0])


def test_relative_volume_is_one_for_constant_volume():
    out = relative_volume(np.full(30, 500.0), 20)
    np.testing.assert_allclose(out[20:], 1.0)


def test_relative_volume_flags_a_spike():
    """A 5x bar reads exactly 5.0 — the baseline excludes the bar being measured."""
    volume = np.concatenate([np.full(20, 100.0), np.array([500.0])])
    assert relative_volume(volume, 20)[20] == pytest.approx(5.0)


def test_relative_volume_excludes_current_bar_from_its_own_baseline():
    """Including it would dilute the spike: 500/((20*100+500)/21) = 4.17, not 5.0.

    filters.btc_volume_spike_multiple is written in the intuitive unit ("4x normal"),
    so a diluted ratio would make that protective filter fire late.
    """
    volume = np.concatenate([np.full(20, 100.0), np.array([500.0])])
    inclusive = 500.0 / np.append(volume[1:21], 500.0)[:21].mean()
    out = relative_volume(volume, 20)[20]
    assert out == pytest.approx(5.0)
    assert out != pytest.approx(inclusive)


def test_relative_volume_first_valid_index_is_period():
    """Needs `period` prior bars, so index period-1 has no baseline yet."""
    out = relative_volume(np.full(30, 100.0), 20)
    assert np.all(np.isnan(out[:20]))
    assert not np.isnan(out[20])


def test_relative_volume_is_unbounded_above():
    """An inclusive baseline caps the ratio at `period`; this must not."""
    volume = np.concatenate([np.full(20, 1.0), np.array([1000.0])])
    assert relative_volume(volume, 20)[20] == pytest.approx(1000.0)


def test_relative_volume_dead_market_is_nan_not_infinity():
    """Zero average must not read as an infinite spike."""
    out = relative_volume(np.zeros(30), 20)
    assert np.all(np.isnan(out[20:]))
    assert not np.any(np.isinf(out))


# --- support / resistance -------------------------------------------------

# Pivot highs at index 2 (15.0) and index 6 (20.0); pivot lows at the mirrored indices.
PIVOT_HIGH = np.array([10.0, 11, 15, 11, 10, 11, 20, 11, 10])
PIVOT_LOW = np.array([10.0, 9, 5, 9, 10, 9, 2, 9, 10])


def test_swing_highs_finds_local_peaks():
    assert list(np.flatnonzero(swing_highs(PIVOT_HIGH, 2, 2))) == [2, 6]


def test_swing_lows_finds_local_troughs():
    assert list(np.flatnonzero(swing_lows(PIVOT_LOW, 2, 2))) == [2, 6]


def test_swing_mask_is_length_preserving():
    assert len(swing_highs(PIVOT_HIGH, 2, 2)) == len(PIVOT_HIGH)


def test_swing_edges_cannot_be_pivots():
    """The first `left` and last `right` bars lack the neighbours to confirm."""
    mask = swing_highs(PIVOT_HIGH, 2, 2)
    assert not mask[0] and not mask[1]
    assert not mask[-1] and not mask[-2]


def test_swing_wider_confirmation_finds_fewer_pivots():
    """A minor peak survives left=right=2 but is not significant at 4."""
    high = np.array([10.0, 11, 15, 11, 10, 11, 12, 11, 10, 11, 20, 11, 10, 11, 10])
    assert list(np.flatnonzero(swing_highs(high, 2, 2))) == [2, 6, 10]
    assert list(np.flatnonzero(swing_highs(high, 4, 4))) == [10]


def test_swing_plateau_is_not_a_pivot():
    """Equal adjacent highs are ambiguous; one flat top must not become two levels."""
    plateau = np.array([10.0, 11, 15, 15, 11, 10, 9])
    assert not swing_highs(plateau, 2, 2).any()


def test_swing_series_too_short_has_no_pivots():
    assert not swing_highs(np.array([1.0, 2, 3]), 2, 2).any()


def test_swing_nan_bars_are_never_pivots():
    values = np.array([10.0, 11, np.nan, 11, 10, 11, 20, 11, 10])
    assert not swing_highs(values, 2, 2)[2]


def test_nearest_resistance_picks_the_lowest_level_above_price():
    assert nearest_resistance(PIVOT_HIGH, 12.0, 2, 2) == pytest.approx(15.0)


def test_nearest_support_picks_the_highest_level_below_price():
    assert nearest_support(PIVOT_LOW, 8.0, 2, 2) == pytest.approx(5.0)


def test_nearest_resistance_skips_levels_below_price():
    """Above 15 the only resistance left is the 20 pivot."""
    assert nearest_resistance(PIVOT_HIGH, 17.0, 2, 2) == pytest.approx(20.0)


def test_level_must_be_strictly_beyond_price():
    """A level exactly at price is not resistance above it."""
    assert nearest_resistance(PIVOT_HIGH, 15.0, 2, 2) == pytest.approx(20.0)
    assert nearest_support(PIVOT_LOW, 5.0, 2, 2) == pytest.approx(2.0)


def test_no_level_returns_nan_not_a_sentinel_price():
    """NaN means 'not found' — a caller must not mistake it for open space."""
    assert np.isnan(nearest_resistance(PIVOT_HIGH, 999.0, 2, 2))
    assert np.isnan(nearest_support(PIVOT_LOW, 0.01, 2, 2))


def test_no_pivots_at_all_returns_nan():
    assert np.isnan(nearest_resistance(np.arange(50.0), 10.0, 2, 2))


def test_empty_series_returns_nan():
    assert np.isnan(nearest_resistance(np.array([]), 10.0, 2, 2))


def test_nan_price_returns_nan():
    assert np.isnan(nearest_resistance(PIVOT_HIGH, np.nan, 2, 2))


def test_pivot_is_invisible_until_its_confirmation_bar():
    """The lookahead guard: the 20 pivot at index 6 is only knowable at index 8.

    Reading it at bar 7 would let a backtest trade a level that had not yet printed.
    """
    assert np.isnan(nearest_resistance(PIVOT_HIGH, 17.0, 2, 2, as_of=7))
    assert nearest_resistance(PIVOT_HIGH, 17.0, 2, 2, as_of=8) == pytest.approx(20.0)


def test_confirmation_delay_scales_with_right():
    """right=3 pushes confirmation of the index-6 pivot from bar 8 to bar 9."""
    padded = np.concatenate([PIVOT_HIGH, [9.0, 8.0]])
    assert np.isnan(nearest_resistance(padded, 17.0, 2, 3, as_of=8))
    assert nearest_resistance(padded, 17.0, 2, 3, as_of=9) == pytest.approx(20.0)


def test_as_of_defaults_to_the_last_bar():
    assert (
        nearest_resistance(PIVOT_HIGH, 17.0, 2, 2)
        == nearest_resistance(PIVOT_HIGH, 17.0, 2, 2, as_of=len(PIVOT_HIGH) - 1)
    )


def test_as_of_out_of_range_rejected():
    with pytest.raises(ValueError, match="as_of"):
        nearest_resistance(PIVOT_HIGH, 12.0, 2, 2, as_of=99)


def test_lookback_window_forgets_distant_levels():
    """structure_lookback is 50 in config; a pivot older than that is not a level."""
    high = np.concatenate([PIVOT_HIGH[:5], np.full(60, 12.0)])
    assert np.isnan(nearest_resistance(high, 12.5, 2, 2, lookback=50))
    assert nearest_resistance(high, 12.5, 2, 2, lookback=100) == pytest.approx(15.0)


def test_as_of_equals_replaying_on_a_truncated_series(series):
    """Backtest/live parity, checked bar by bar.

    Live only ever holds bars 0..i. A backtest holds the whole array and passes as_of=i.
    If those two disagree anywhere, the backtest is reading bars that had not printed.
    """
    high, low, close, _ = series
    for i in range(60, len(close), 7):
        for func, values in ((nearest_resistance, high), (nearest_support, low)):
            live = func(values[: i + 1], close[i], lookback=50)
            replay = func(values, close[i], lookback=50, as_of=i)
            assert np.isclose(live, replay, equal_nan=True), f"{func.__name__} at bar {i}"


def test_support_and_resistance_bracket_the_price():
    high, low = PIVOT_HIGH, PIVOT_LOW
    price = 9.0
    resistance = nearest_resistance(high, price, 2, 2)
    support = nearest_support(low, price, 2, 2)
    assert support < price < resistance


def test_sr_inputs_are_not_mutated():
    before = PIVOT_HIGH.copy()
    nearest_resistance(PIVOT_HIGH, 12.0, 2, 2)
    swing_highs(PIVOT_HIGH, 2, 2)
    assert np.array_equal(PIVOT_HIGH, before)


@pytest.mark.parametrize("bad", [0, -1])
def test_sr_non_positive_parameters_rejected(bad):
    with pytest.raises(ValueError, match=">= 1"):
        swing_highs(PIVOT_HIGH, bad, 2)


# --- Candles --------------------------------------------------------------

GATE_ROWS = [
    {"o": "65294.5", "v": 231072, "t": 1786314060, "c": "65266.3",
     "l": "65266.3", "h": "65310.5", "sum": "1508687.91469"},
    {"o": "65266.4", "v": 61673, "t": 1786314120, "c": "65271",
     "l": "65266.3", "h": "65271.1", "sum": "402536.62997"},
]


def test_candles_from_gate_parses_string_prices():
    candles = Candles.from_gate(GATE_ROWS)
    assert len(candles) == 2
    np.testing.assert_allclose(candles.close, [65266.3, 65271.0])
    np.testing.assert_allclose(candles.high, [65310.5, 65271.1])
    np.testing.assert_allclose(candles.volume, [231072.0, 61673.0])
    np.testing.assert_allclose(candles.turnover, [1508687.91469, 402536.62997])
    assert candles.close.dtype == np.float64


def test_candles_from_gate_preserves_ascending_time():
    candles = Candles.from_gate(GATE_ROWS)
    assert candles.time[0] < candles.time[1]


def test_candles_from_gate_empty_payload():
    candles = Candles.from_gate([])
    assert len(candles) == 0


def test_candles_from_gate_without_turnover_field():
    rows = [{k: v for k, v in row.items() if k != "sum"} for row in GATE_ROWS]
    assert Candles.from_gate(rows).turnover is None


def test_candles_rejects_high_below_low():
    with pytest.raises(ValueError, match="high < low"):
        Candles(
            time=np.array([1.0]), open=np.array([10.0]), high=np.array([9.0]),
            low=np.array([11.0]), close=np.array([10.0]), volume=np.array([1.0]),
        )


def test_candles_rejects_mismatched_columns():
    with pytest.raises(ValueError, match="same length"):
        Candles(
            time=np.array([1.0, 2.0]), open=np.array([10.0]), high=np.array([11.0]),
            low=np.array([9.0]), close=np.array([10.0]), volume=np.array([1.0]),
        )


def test_candles_feed_all_indicators_end_to_end():
    """Real payload shape flows through every indicator without raising."""
    rng = np.random.default_rng(3)
    close = 65000 + np.cumsum(rng.normal(0, 20, 260))
    rows = [
        {"t": 1786314060 + i * 60, "o": str(close[i]), "c": str(close[i]),
         "h": str(close[i] + 15), "l": str(close[i] - 15),
         "v": int(rng.uniform(1000, 50_000)), "sum": "1.0"}
        for i in range(260)
    ]
    c = Candles.from_gate(rows)
    outputs = {
        "ema9": ema(c.close, 9), "ema21": ema(c.close, 21),
        "ema50": ema(c.close, 50), "ema200": ema(c.close, 200),
        "rsi14": rsi(c.close, 14), "atr14": atr(c.high, c.low, c.close, 14),
        "vwap": vwap(c.high, c.low, c.close, c.volume),
        "rvwap": rolling_vwap(c.high, c.low, c.close, c.volume, 20),
        "vol_ma": volume_ma(c.volume, 20),
        "rvol": relative_volume(c.volume, 20),
    }
    for name, array in outputs.items():
        assert len(array) == 260, name
        assert not np.isnan(array[-1]), f"{name} still NaN at the last bar"
    assert not np.isnan(macd(c.close).histogram[-1])
