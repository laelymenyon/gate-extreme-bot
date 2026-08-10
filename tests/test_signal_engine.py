"""PHASE 5 tests — signal engine.

The engine composes regime + scoring across timeframes. These tests pin the gates that
keep it honest: closed bars only, no lookahead, fail-closed on missing data, and no
order execution anywhere in this layer.
"""

from __future__ import annotations

import numpy as np
import pytest

from strategy.indicators import Candles
from strategy.regime import Regime, RegimeParams
from strategy.scoring import ScoringParams
from strategy.signal_engine import (
    EngineParams,
    Signal,
    SignalEngine,
    closed_bars,
)

MINUTE = 60.0
TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}

#: Shared evaluation instant. Every fixture timeframe is built to end exactly here, so at
#: `NOW` the newest bar of each has just closed.
NOW = 260 * 3600.0


def candles(close, *, interval=60.0, wick=0.0015, volume=None, t0=0.0):
    close = np.asarray(close, dtype=float)
    n = len(close)
    open_ = np.concatenate([[close[0]], close[:-1]])
    vol = np.full(n, 1000.0) if volume is None else np.asarray(volume, dtype=float)
    return Candles(
        time=t0 + np.arange(n, dtype=float) * interval,
        open=open_,
        high=np.maximum(open_, close) * (1 + wick),
        low=np.minimum(open_, close) * (1 - wick),
        close=close,
        volume=vol,
    )


def uptrend(n=260, slope=0.0015, seed=1, noise=0.0004):
    """A trend with realistic jitter.

    The noise is load-bearing, not decoration. A perfectly smooth exponential has an ATR%
    that climbs monotonically toward its asymptote, so every bar is the highest of its own
    last 100 and the regime classifier — correctly — reports HIGH_VOLATILITY forever.
    Jitter gives ATR a stationary distribution, so the volatility gate can stay armed
    while the fixture still reads as the clean trend it is meant to be.
    """
    rng = np.random.default_rng(seed)
    return 100 * np.exp(np.cumsum(np.full(n, slope) + rng.normal(0, noise, n)))


def downtrend(n=260, slope=0.0015, seed=1, noise=0.0004):
    """A genuine downtrend, not a reversed uptrend.

    Reversing a path reverses its volatility profile too, which can park a fixture's
    final bars on an ATR spike and trip the volatility veto for reasons unrelated to
    direction. Generating with a negative drift keeps the two independent.
    """
    return uptrend(n, -slope, seed, noise)


def frames(path_fn=uptrend, n=260):
    """One aligned Candles per timeframe, all ending at the same wall-clock instant.

    Anchoring on a shared end time is the whole point: give each timeframe the same bar
    *count* from a common t0 instead and the 1h series runs 15 hours past the 1m one, so
    almost none of its bars have closed at the moment being evaluated.
    """
    out = {}
    for tf, secs in TF_SECONDS.items():
        out[tf] = candles(path_fn(n), interval=float(secs), t0=NOW - n * secs)
    return out


# --- closed-bar rule -------------------------------------------------------

def test_closed_bars_drops_the_forming_bar():
    """Gate stamps a bar with its OPEN time, so the last bar is still forming."""
    c = candles(uptrend(n=10), interval=60.0)
    closed = closed_bars(c, interval_seconds=60, now=c.time[-1] + 30)
    assert len(closed) == len(c) - 1


def test_closed_bars_keeps_the_bar_once_its_interval_elapses():
    c = candles(uptrend(n=10), interval=60.0)
    closed = closed_bars(c, interval_seconds=60, now=c.time[-1] + 60)
    assert len(closed) == len(c)


def test_closed_bars_is_exclusive_at_the_boundary():
    """A bar closes at open + interval, not a tick before."""
    c = candles(uptrend(n=10), interval=60.0)
    just_before = closed_bars(c, interval_seconds=60, now=c.time[-1] + 59.999)
    assert len(just_before) == len(c) - 1


def test_closed_bars_drops_every_unfinished_bar_not_only_the_last():
    c = candles(uptrend(n=10), interval=60.0)
    closed = closed_bars(c, interval_seconds=60, now=c.time[-3] + 1)
    assert len(closed) == len(c) - 3


def test_closed_bars_on_empty_is_empty():
    empty = Candles(*(np.array([], dtype=float) for _ in range(6)))
    assert len(closed_bars(empty, interval_seconds=60, now=0.0)) == 0


def test_closed_bars_does_not_mutate_input():
    c = candles(uptrend(n=10), interval=60.0)
    before = c.close.copy()
    closed_bars(c, interval_seconds=60, now=c.time[-1])
    np.testing.assert_array_equal(c.close, before)


def test_engine_evaluates_only_closed_bars():
    """The forming bar must not reach scoring, however tempting its price is."""
    engine = SignalEngine()
    data = frames()
    # One second before NOW, so the newest bar of every timeframe is still forming.
    forming = NOW - 1
    baseline = engine.evaluate("BTC_USDT", data, now=forming)

    spiked = {}
    for tf, secs in TF_SECONDS.items():
        c = data[tf]
        close = c.close.copy()
        close[-1] *= 3.0          # the still-forming bar goes vertical
        spiked[tf] = candles(close, interval=float(secs), t0=float(c.time[0]))
    after = engine.evaluate("BTC_USDT", spiked, now=forming)
    assert after.score == pytest.approx(baseline.score)
    assert after.accepted == baseline.accepted


# --- contracts -------------------------------------------------------------

def test_evaluate_returns_a_signal():
    engine = SignalEngine()
    data = frames()
    out = engine.evaluate("BTC_USDT", data, now=NOW)
    assert isinstance(out, Signal)
    assert out.symbol == "BTC_USDT"
    assert isinstance(out.reason, str) and out.reason


def test_signal_carries_no_order_fields():
    """Phase 5 decides; it must not smuggle in execution."""
    engine = SignalEngine()
    data = frames()
    out = engine.evaluate("BTC_USDT", data, now=NOW)
    forbidden = {"order_id", "client_order_id", "size", "quantity", "leverage",
                 "place", "submit", "execute"}
    assert not (forbidden & set(vars(out)))


def test_engine_exposes_no_execution_methods():
    for name in dir(SignalEngine):
        assert "order" not in name.lower()
        assert "execute" not in name.lower()
        assert "place" not in name.lower()


def test_rejected_signal_has_no_direction():
    engine = SignalEngine(params=EngineParams(min_score=101.0))
    data = frames()
    out = engine.evaluate("BTC_USDT", data, now=NOW)
    assert not out.accepted
    assert out.direction == 0


def test_missing_timeframe_is_refused_not_guessed():
    engine = SignalEngine()
    data = frames()
    del data["15m"]
    out = engine.evaluate("BTC_USDT", data, now=NOW)
    assert not out.accepted
    assert "15m" in out.reason


def test_thin_history_is_refused():
    engine = SignalEngine()
    data = frames(n=40)
    out = engine.evaluate("BTC_USDT", data, now=NOW)
    assert not out.accepted


def test_evaluate_does_not_mutate_inputs():
    engine = SignalEngine()
    data = frames()
    before = {tf: c.close.copy() for tf, c in data.items()}
    engine.evaluate("BTC_USDT", data, now=NOW)
    for tf, original in before.items():
        np.testing.assert_array_equal(data[tf].close, original)


def test_timeframe_reads_are_reported_for_diagnosis():
    engine = SignalEngine()
    data = frames()
    out = engine.evaluate("BTC_USDT", data, now=NOW)
    assert set(out.timeframes) == set(TF_SECONDS)


# --- multi-timeframe alignment ---------------------------------------------

def test_agreeing_uptrend_yields_a_long_bias():
    engine = SignalEngine(params=EngineParams(min_score=0.0))
    data = frames(uptrend)
    out = engine.evaluate("BTC_USDT", data, now=NOW)
    assert out.direction == 1


def test_agreeing_downtrend_yields_a_short_bias():
    engine = SignalEngine(params=EngineParams(min_score=0.0))
    data = frames(downtrend)
    out = engine.evaluate("BTC_USDT", data, now=NOW)
    assert out.direction == -1


def test_conflicting_timeframes_are_refused():
    """1m up against a 1h down is not a trade, whatever the score says."""
    engine = SignalEngine(params=EngineParams(min_score=0.0))
    data = frames(uptrend)
    data["1h"] = candles(downtrend(), interval=3600.0,
                         t0=float(data["1h"].time[0]))
    out = engine.evaluate("BTC_USDT", data, now=NOW)
    assert not out.accepted
    assert "align" in out.reason.lower() or "conflict" in out.reason.lower()


def test_score_below_threshold_is_refused_with_the_number():
    engine = SignalEngine(params=EngineParams(min_score=99.0))
    data = frames(uptrend)
    out = engine.evaluate("BTC_USDT", data, now=NOW)
    assert not out.accepted
    assert "99" in out.reason


def test_threshold_is_inclusive():
    engine = SignalEngine(params=EngineParams(min_score=0.0))
    data = frames(uptrend)
    out = engine.evaluate("BTC_USDT", data, now=NOW)
    assert out.accepted


# --- volatility veto -------------------------------------------------------

def test_high_volatility_timeframe_blocks_the_signal():
    """A blowout on any timeframe disqualifies, not only the entry one."""
    engine = SignalEngine(params=EngineParams(min_score=0.0))
    clean = engine.evaluate("BTC_USDT", frames(uptrend), now=NOW)
    assert clean.accepted, "baseline must be tradeable for this test to mean anything"

    data = frames(uptrend)
    blown = data["5m"]
    blown.high[-40:] = blown.close[-40:] * 1.15
    blown.low[-40:] = blown.close[-40:] * 0.85
    out = engine.evaluate("BTC_USDT", data, now=NOW)
    assert not out.accepted
    assert out.stage == "volatility"
    assert "5m" in out.reason


# --- BTC guard -------------------------------------------------------------

def test_alt_requires_btc_context_and_fails_closed():
    engine = SignalEngine(params=EngineParams(min_score=0.0))
    data = frames(uptrend)
    out = engine.evaluate("ETH_USDT", data, now=NOW, btc=None)
    assert not out.accepted
    assert "BTC" in out.reason


def test_btc_itself_needs_no_btc_context():
    engine = SignalEngine(params=EngineParams(min_score=0.0))
    data = frames(uptrend)
    out = engine.evaluate("BTC_USDT", data, now=NOW, btc=None)
    assert out.accepted


def test_btc_volume_spike_suspends_alt_entries():
    engine = SignalEngine(params=EngineParams(min_score=0.0))
    data = frames(uptrend)
    btc = frames(uptrend)
    spike = btc["1m"]
    # The bar being measured, not one behind it: relative_volume excludes the current bar
    # from its own baseline, so spiking [-2] would inflate the denominator instead.
    spike.volume[-1] = spike.volume[:-1].mean() * 50
    out = engine.evaluate(
        "ETH_USDT", data, now=NOW, btc=btc
    )
    assert not out.accepted
    assert "BTC" in out.reason


def test_calm_btc_permits_alt_entries():
    engine = SignalEngine(params=EngineParams(min_score=0.0))
    data = frames(uptrend)
    out = engine.evaluate("ETH_USDT", data, now=NOW,
                          btc=frames(uptrend))
    assert out.accepted


# --- lookahead -------------------------------------------------------------

def test_appending_future_bars_does_not_change_a_past_decision():
    """The core anti-lookahead guarantee, at the engine level."""
    engine = SignalEngine(params=EngineParams(min_score=0.0))
    data = frames(uptrend)
    now = data["1m"].time[-1] + 1
    base = engine.evaluate("BTC_USDT", data, now=now)

    extended = {}
    for tf, secs in TF_SECONDS.items():
        c = data[tf]
        future = c.close[-1] * np.array([2.0, 0.4, 1.9, 0.3])
        extended[tf] = candles(
            np.concatenate([c.close, future]), interval=float(secs),
            t0=float(c.time[0]),
        )
    later = engine.evaluate("BTC_USDT", extended, now=now)
    assert later.direction == base.direction
    assert later.score == pytest.approx(base.score)
    assert later.accepted == base.accepted


def test_replaying_bar_by_bar_is_stable():
    """Each evaluation must depend only on bars closed at its own `now`."""
    engine = SignalEngine(params=EngineParams(min_score=0.0))
    data = frames(uptrend, n=300)
    seen = []
    for extra in range(3):
        now = data["1h"].time[260 + extra] + 3600
        seen.append(engine.evaluate("BTC_USDT", data, now=now).score)
    repeat = [
        engine.evaluate("BTC_USDT", data, now=data["1h"].time[260 + e] + 3600).score
        for e in range(3)
    ]
    assert seen == pytest.approx(repeat)


def test_future_now_does_not_reveal_unclosed_bars():
    engine = SignalEngine(params=EngineParams(min_score=0.0))
    data = frames(uptrend)
    at_last = engine.evaluate("BTC_USDT", data, now=data["1h"].time[-1] + 3600)
    way_later = engine.evaluate("BTC_USDT", data, now=data["1h"].time[-1] + 10 * 3600)
    assert at_last.score == pytest.approx(way_later.score)
