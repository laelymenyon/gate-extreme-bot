"""PHASE 5 tests — signal scoring.

Bounds, direction-awareness, and the fail-closed NaN rule are asserted against
hand-built paths, so a bug in strategy/scoring.py cannot make its own test pass.
"""

from __future__ import annotations

import numpy as np
import pytest

from strategy.indicators import Candles
from strategy.scoring import (
    DEFAULT_WEIGHTS,
    CategoryScore,
    ScoreResult,
    ScoringParams,
    score,
)

CATEGORIES = ("trend", "momentum", "volume", "price_action", "volatility",
              "support_resistance")


def candles(close, *, wick=0.0015, volume=None, seed=0):
    close = np.asarray(close, dtype=float)
    n = len(close)
    open_ = np.concatenate([[close[0]], close[:-1]])
    vol = np.full(n, 1000.0) if volume is None else np.asarray(volume, dtype=float)
    return Candles(
        time=np.arange(n, dtype=float) * 60,
        open=open_,
        high=np.maximum(open_, close) * (1 + wick),
        low=np.minimum(open_, close) * (1 - wick),
        close=close,
        volume=vol,
    )


def uptrend(n=300, slope=0.0015):
    return 100 * np.exp(np.arange(n) * slope)


def downtrend(n=300, slope=0.0015):
    return uptrend(n, slope)[::-1].copy()


PARAMS = ScoringParams()


# --- contracts -------------------------------------------------------------

@pytest.mark.parametrize("direction", [1, -1])
def test_total_is_within_zero_and_one_hundred(direction):
    result = score(candles(uptrend()), direction)
    assert 0.0 <= result.total <= 100.0


@pytest.mark.parametrize("direction", [1, -1])
def test_all_six_categories_are_reported(direction):
    result = score(candles(uptrend()), direction)
    assert set(result.categories) == set(CATEGORIES)


def test_total_equals_sum_of_category_points():
    result = score(candles(uptrend()), 1)
    expected = sum(c.points for c in result.categories.values())
    assert result.total == pytest.approx(expected)


def test_category_points_are_fraction_times_weight():
    result = score(candles(uptrend()), 1)
    for category in result.categories.values():
        assert category.points == pytest.approx(category.fraction * category.weight)


def test_every_fraction_is_a_unit_interval():
    for direction in (1, -1):
        result = score(candles(uptrend()), direction)
        for category in result.categories.values():
            assert 0.0 <= category.fraction <= 1.0, category.name


def test_weights_match_the_configured_totals():
    result = score(candles(uptrend()), 1)
    total = sum(c.weight for c in result.categories.values())
    assert total == pytest.approx(100.0)
    for name, weight in DEFAULT_WEIGHTS.items():
        assert result.categories[name].weight == pytest.approx(weight)


def test_max_possible_score_is_one_hundred():
    """Every category at full credit must total exactly the configured 100."""
    assert sum(DEFAULT_WEIGHTS.values()) == pytest.approx(100.0)


@pytest.mark.parametrize("bad", [0, 2, -2, None, "long"])
def test_directionless_scoring_is_rejected(bad):
    with pytest.raises(ValueError, match="direction"):
        score(candles(uptrend()), bad)


def test_empty_series_is_rejected():
    empty = Candles(*(np.array([], dtype=float) for _ in range(6)))
    with pytest.raises(ValueError):
        score(empty, 1)


def test_scoring_does_not_mutate_input():
    c = candles(uptrend())
    before = [a.copy() for a in (c.open, c.high, c.low, c.close, c.volume)]
    score(c, 1)
    for arr, orig in zip((c.open, c.high, c.low, c.close, c.volume), before):
        np.testing.assert_array_equal(arr, orig)


def test_every_category_carries_a_detail_string():
    result = score(candles(uptrend()), 1)
    for category in result.categories.values():
        assert isinstance(category.detail, str) and category.detail


def test_breakdown_names_every_category():
    text = score(candles(uptrend()), 1).breakdown()
    for name in CATEGORIES:
        assert name in text


def test_meets_compares_against_the_threshold():
    result = score(candles(uptrend()), 1)
    assert result.meets(result.total)
    assert result.meets(result.total - 1)
    assert not result.meets(result.total + 1)


# --- direction awareness ---------------------------------------------------

def test_uptrend_scores_higher_long_than_short():
    c = candles(uptrend())
    assert score(c, 1).total > score(c, -1).total


def test_downtrend_scores_higher_short_than_long():
    c = candles(downtrend())
    assert score(c, -1).total > score(c, 1).total


def test_trend_category_is_maximal_in_a_clean_uptrend():
    result = score(candles(uptrend()), 1)
    assert result.categories["trend"].fraction == pytest.approx(1.0, abs=0.05)


def test_trend_category_is_near_zero_when_fighting_the_stack():
    result = score(candles(uptrend()), -1)
    assert result.categories["trend"].fraction < 0.3


def test_direction_is_echoed_in_the_result():
    assert score(candles(uptrend()), 1).direction == 1
    assert score(candles(uptrend()), -1).direction == -1


# --- fail-closed on missing data -------------------------------------------

def test_short_history_scores_low_rather_than_defaulting_high():
    """Thin history must not score its way past the threshold."""
    result = score(candles(uptrend(n=30)), 1)
    assert result.total < 50


def test_unwarmed_trend_category_scores_zero_not_a_midpoint():
    result = score(candles(uptrend(n=30)), 1)
    assert result.categories["trend"].fraction == 0.0


def test_zero_range_bar_scores_no_price_action():
    close = np.full(300, 100.0)
    c = Candles(
        time=np.arange(300, dtype=float) * 60,
        open=close.copy(), high=close.copy(), low=close.copy(),
        close=close.copy(), volume=np.full(300, 1000.0),
    )
    assert score(c, 1).categories["price_action"].fraction == 0.0


def test_no_structure_at_all_scores_zero_support_resistance():
    """Too little history to confirm any level is unknown, not clear."""
    result = score(candles(uptrend(n=30)), 1)
    assert result.categories["support_resistance"].fraction == 0.0


# --- individual categories -------------------------------------------------

def test_volume_spike_earns_more_than_flat_volume():
    flat = candles(uptrend(), volume=np.full(300, 1000.0))
    spike = candles(
        uptrend(), volume=np.concatenate([np.full(299, 1000.0), [5000.0]])
    )
    assert (
        score(spike, 1).categories["volume"].fraction
        > score(flat, 1).categories["volume"].fraction
    )


def test_dead_volume_earns_nothing():
    volume = np.concatenate([np.full(299, 1000.0), [100.0]])
    result = score(candles(uptrend(), volume=volume), 1)
    assert result.categories["volume"].fraction == 0.0


def test_overbought_rsi_is_not_rewarded_for_a_long():
    """Chasing an extended market must not earn full momentum credit.

    At 100x with a 0.125% stop there is no room to survive the snap-back.
    """
    result = score(candles(uptrend(slope=0.01)), 1)
    rsi_value = result.categories["momentum"]
    assert rsi_value.fraction < 1.0


def test_momentum_rewards_agreement_with_direction():
    up = candles(uptrend())
    assert (
        score(up, 1).categories["momentum"].fraction
        > score(up, -1).categories["momentum"].fraction
    )


def test_volatility_band_rejects_a_dead_market():
    close = 100 + np.zeros(300)
    c = candles(close, wick=1e-7)
    assert score(c, 1).categories["volatility"].fraction == 0.0


def test_volatility_band_rejects_an_explosive_market():
    c = candles(uptrend(), wick=0.05)
    assert score(c, 1).categories["volatility"].fraction == 0.0


def test_volatility_band_rewards_the_middle():
    params = ScoringParams()
    c = candles(uptrend(slope=0.0002), wick=params.atr_pct_ideal / 2)
    assert score(c, 1).categories["volatility"].fraction > 0.5


def test_new_high_close_has_clear_room_above():
    """Price through all prior structure scores full, not zero.

    A missing level and a cleared level are different facts; conflating them would zero
    this category for every breakout trade.
    """
    result = score(candles(uptrend()), 1)
    assert result.categories["support_resistance"].fraction == pytest.approx(1.0)


def test_long_into_a_decline_sees_overhead_resistance():
    """The mirror case: no pivot high does not mean nothing overhead."""
    result = score(candles(downtrend()), 1)
    assert result.categories["support_resistance"].fraction > 0.0
    assert "high" in result.categories["support_resistance"].detail


def test_support_resistance_excludes_the_current_bar():
    """Resistance is prior structure; a bar cannot resist its own close."""
    result = score(candles(uptrend()), 1)
    detail = result.categories["support_resistance"].detail
    assert "prior" in detail or "cleared" in detail


# --- lookahead -------------------------------------------------------------

def test_appending_future_bars_does_not_change_a_past_score():
    close = uptrend(n=280)
    base = score(candles(close), 1)
    extended = np.concatenate([close, close[-1] * np.array([2.0, 0.5, 1.8])])
    later = score(candles(extended), 1, as_of=len(close) - 1)
    assert later.total == pytest.approx(base.total)


@pytest.mark.parametrize("direction", [1, -1])
def test_truncated_series_matches_as_of_at_every_bar(direction):
    path = uptrend(n=300, slope=0.0008)
    full = candles(path)
    for i in range(250, len(path)):
        live = score(full.head(i + 1), direction)
        replay = score(full, direction, as_of=i)
        assert live.total == pytest.approx(replay.total), f"bar {i}"


def test_as_of_out_of_range_is_rejected():
    c = candles(uptrend())
    with pytest.raises(ValueError, match="as_of"):
        score(c, 1, as_of=len(c))
