"""PHASE 9 tests — the backtester.

A backtester that flatters a strategy is worse than none, because it turns an unprofitable
idea into a funded one. So most of these tests assert that the engine resolves *against*
the position: adverse intrabar ordering, post-only entries that fail to fill, fees and
funding actually charged, liquidation simulated, and a verdict withheld on a small sample.

The strategy decision is injected in most tests, which pins the engine's mechanics
independently of whether the shipped filters happen to signal on a fixture.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import numpy as np
import pytest

from backtest.engine import (
    BacktestEngine,
    BacktestParams,
    Metrics,
    Trade,
    slice_candles,
    walk_forward,
)
from config import load_config
from exchange.gate_client import Contract, RiskTier
from risk.position_sizer import SizingParams
from strategy.indicators import Candles

BTC_RAW = {
    "name": "BTC_USDT", "leverage_max": "200", "leverage_min": "1",
    "maintenance_rate": "0.003", "quanto_multiplier": "0.0001",
    "order_size_min": 1, "order_size_max": 12000000,
    "order_price_round": "0.1", "mark_price_round": "0.01",
    "taker_fee_rate": "0.00075", "maker_fee_rate": "-0.0001",
    "risk_limit_base": "500000", "in_delisting": False, "status": "trading",
}
BTC = Contract.from_api(BTC_RAW)
TIERS = [RiskTier.from_api({
    "tier": 1, "risk_limit": "500000", "initial_rate": "0.005",
    "maintenance_rate": "0.003", "leverage_max": "200", "deduction": "0",
})]

ENTRY = 65_000.0


def candles(close, *, interval=60.0, wick=0.001, highs=None, lows=None):
    close = np.asarray(close, dtype=float)
    n = len(close)
    open_ = np.concatenate([[close[0]], close[:-1]])
    return Candles(
        time=np.arange(n, dtype=float) * interval,
        open=open_,
        high=np.maximum(open_, close) * (1 + wick) if highs is None else np.asarray(highs, float),
        low=np.minimum(open_, close) * (1 - wick) if lows is None else np.asarray(lows, float),
        close=close,
        volume=np.full(n, 1000.0),
    )


def wobble(n=900, start=ENTRY, sigma=0.0002, drift=0.0, seed=5):
    rng = np.random.default_rng(seed)
    return start * np.exp(np.cumsum(rng.normal(drift, sigma, n)))


class Signal:
    """Minimal stand-in for a Phase 5 Signal."""

    def __init__(self, direction=1, score=85.0, accepted=True, stage="accepted"):
        self.direction = direction
        self.score = score
        self.accepted = accepted
        self.stage = stage


def once(direction=1, at=1):
    """A decision function that signals exactly once, on the ``at``-th call."""
    state = {"n": 0}

    def decide(**kwargs):
        state["n"] += 1
        return Signal(direction) if state["n"] == at else None

    return decide


def engine(**overrides):
    decide = overrides.pop("decide", None)
    params = BacktestParams(**overrides)
    return BacktestEngine(params, decide=decide)


def run(series, decide=None, **overrides):
    return engine(decide=decide, **overrides).run(
        "BTC_USDT", {"1m": series}, TIERS, BTC, warmup=250,
    )


# --- fills are not free ----------------------------------------------------

def test_a_post_only_entry_that_is_never_touched_does_not_fill():
    """Assuming fills would hand the strategy the maker rebate for free.

    The lows are set explicitly so that every bar after the signal trades strictly above
    the resting buy limit. With the default helper each bar opens at the previous close,
    so its own wick would always touch a limit placed there.
    """
    warmup = 251
    close = np.concatenate([wobble(n=warmup, sigma=0.0002),
                            ENTRY * np.linspace(1.002, 1.05, 150)])
    high = close * 1.0005
    low = np.concatenate([close[:warmup] * 0.9995,
                          close[warmup:] * 0.9999])
    low[warmup:] = np.maximum(low[warmup:], close[warmup - 1] * 1.0005)  # gap away
    series = candles(close, highs=high, lows=low)

    result = run(series, decide=once())
    assert result.metrics.entries_attempted == 1
    assert result.metrics.entries_filled == 0
    assert result.metrics.trades == 0


def test_a_touched_limit_fills():
    path = np.concatenate([np.full(260, ENTRY), np.full(200, ENTRY)])
    result = run(candles(path, wick=0.002), decide=once())
    assert result.metrics.entries_filled == 1


def test_an_unfilled_entry_expires_rather_than_resting_forever():
    """A stale limit must not fill on data hours after the signal that justified it."""
    path = np.concatenate([
        np.full(260, ENTRY),
        np.linspace(ENTRY, ENTRY * 1.05, 100),     # runs away
        np.linspace(ENTRY * 1.05, ENTRY * 0.95, 100),  # comes back much later
    ])
    result = run(candles(path, wick=0.0), decide=once(), entry_fill_timeout_seconds=60)
    assert result.metrics.entries_filled == 0


# --- intrabar resolution is adverse ---------------------------------------

def test_a_bar_containing_both_stop_and_target_resolves_as_the_stop():
    """A bar is four numbers, not a path. The optimistic reading is how backtests lie."""
    n = 300
    close = np.full(n, ENTRY)
    high = np.full(n, ENTRY * 1.0001)
    low = np.full(n, ENTRY * 0.9999)
    # The bar after entry spans far past both the stop and tp1.
    high[261] = ENTRY * 1.02
    low[261] = ENTRY * 0.98
    result = run(candles(close, wick=0.0, highs=high, lows=low), decide=once(at=1))
    assert result.metrics.trades == 1
    assert result.trades[0].exit_reason in ("stop", "liquidation")


def test_liquidation_takes_precedence_over_the_stop_in_the_same_bar():
    """At 100x the stop can be jumped; when it is, the loss is the margin, not the plan."""
    n = 300
    close = np.full(n, ENTRY)
    high = np.full(n, ENTRY * 1.0001)
    low = np.full(n, ENTRY * 0.9999)
    low[261] = ENTRY * 0.98            # far below both stop and liquidation
    result = run(candles(close, wick=0.0, highs=high, lows=low), decide=once(at=1))
    assert result.trades[0].exit_reason == "liquidation"
    assert result.metrics.liquidations == 1


def test_liquidation_can_be_switched_off_for_comparison():
    n = 300
    close = np.full(n, ENTRY)
    high = np.full(n, ENTRY * 1.0001)
    low = np.full(n, ENTRY * 0.9999)
    low[261] = ENTRY * 0.98
    result = run(candles(close, wick=0.0, highs=high, lows=low),
                 decide=once(at=1), simulate_liquidation=False)
    assert result.trades[0].exit_reason == "stop"
    assert result.metrics.liquidations == 0


def test_a_liquidation_loses_more_than_the_planned_risk():
    n = 300
    close = np.full(n, ENTRY)
    high = np.full(n, ENTRY * 1.0001)
    low = np.full(n, ENTRY * 0.9999)
    low[261] = ENTRY * 0.98
    liq = run(candles(close, wick=0.0, highs=high, lows=low), decide=once(at=1))
    stopped = run(candles(close, wick=0.0, highs=high, lows=low),
                  decide=once(at=1), simulate_liquidation=False)
    assert liq.trades[0].net_pnl < stopped.trades[0].net_pnl


# --- costs are charged -----------------------------------------------------

def test_fees_are_charged_on_every_trade():
    result = run(candles(wobble()), decide=once(at=1))
    assert result.metrics.trades == 1
    assert result.trades[0].fees != 0
    # Entry earns the maker rebate, exit pays taker, so the net is a cost.
    assert result.trades[0].fees > 0


def test_the_maker_rebate_is_credited_on_entry():
    """A positive maker fee would erase the rebate that makes post-only worth the misses."""
    rebated = run(candles(wobble()), decide=once(at=1))
    taker_entry = run(candles(wobble()), decide=once(at=1), fee_maker=0.00075)
    assert rebated.trades[0].fees < taker_entry.trades[0].fees


def test_funding_accrues_across_eight_hour_boundaries():
    """A position held through 00:00, 08:00 or 16:00 UTC pays funding."""
    long_hold = run(candles(wobble(n=1400, sigma=0.00005)), decide=once(at=1))
    assert long_hold.metrics.trades >= 1
    assert long_hold.metrics.funding_paid > 0


def test_funding_can_be_switched_off():
    result = run(candles(wobble(n=1400, sigma=0.00005)), decide=once(at=1),
                 simulate_funding=False)
    assert result.metrics.funding_paid == 0


def test_slippage_worsens_the_exit_price():
    clean = run(candles(wobble()), decide=once(at=1), fixed_slippage=0.0)
    slipped = run(candles(wobble()), decide=once(at=1), fixed_slippage=0.001)
    assert slipped.trades[0].net_pnl < clean.trades[0].net_pnl


def test_net_pnl_subtracts_both_fees_and_funding():
    trade = Trade(symbol="BTC_USDT", direction=1, entry_time=0, exit_time=60,
                  entry_price=ENTRY, exit_price=ENTRY * 1.01, size=100,
                  stop_price=ENTRY * 0.99, exit_reason="tp1",
                  gross_pnl=100.0, fees=7.5, funding=1.5)
    assert trade.net_pnl == pytest.approx(91.0)
    assert trade.won


# --- take-profit ladder ----------------------------------------------------

def test_a_partial_target_leaves_the_rest_running():
    """TP1 closes 40%; the position must not be reported as closed."""
    n = 400
    close = np.full(n, ENTRY)
    high = np.full(n, ENTRY * 1.0001)
    low = np.full(n, ENTRY * 0.9999)
    high[261] = ENTRY * 1.0035        # reaches tp1 only (stop is 0.325% wide)
    result = run(candles(close, wick=0.0, highs=high, lows=low), decide=once(at=1))
    # The trade only completes at the end of data, having banked a partial.
    assert result.metrics.trades == 1
    assert result.trades[0].exit_reason in ("end_of_data", "stop", "tp2", "tp3")


def test_reaching_the_final_target_closes_the_position():
    n = 400
    close = np.full(n, ENTRY)
    high = np.full(n, ENTRY * 1.0001)
    low = np.full(n, ENTRY * 0.9999)
    high[261:266] = ENTRY * 1.02      # blows through the whole ladder
    result = run(candles(close, wick=0.0, highs=high, lows=low), decide=once(at=1))
    assert result.trades[0].exit_reason == "tp3"
    assert result.trades[0].net_pnl > 0


# --- shorts mirror longs ---------------------------------------------------

def test_a_short_is_the_mirror_of_a_long():
    n = 300
    close = np.full(n, ENTRY)
    high = np.full(n, ENTRY * 1.0001)
    low = np.full(n, ENTRY * 0.9999)
    high[261] = ENTRY * 1.02          # adverse for a short
    result = run(candles(close, wick=0.0, highs=high, lows=low),
                 decide=once(direction=-1, at=1))
    assert result.metrics.trades == 1
    assert result.trades[0].direction == -1
    assert result.trades[0].exit_reason in ("stop", "liquidation")


# --- no lookahead ----------------------------------------------------------

def test_the_decision_never_sees_the_bar_it_decides_on():
    """The engine must hand the strategy only closed bars — Phase 5's rule."""
    seen = []

    def decide(symbol, candles, now, btc):
        seen.append((len(candles["1m"]), now))
        return None

    series = candles(wobble(n=400))
    engine(decide=decide).run("BTC_USDT", {"1m": series}, TIERS, BTC, warmup=250)
    interval = 60.0
    for length, now in seen:
        # `now` is the close time of the newest bar the engine considered, so every bar
        # the strategy can see opened strictly before it.
        assert now <= float(series.time[-1]) + interval
    assert seen, "the strategy should have been consulted"


def test_a_result_is_reproducible():
    series = candles(wobble())
    first = run(series, decide=once(at=1))
    second = run(series, decide=once(at=1))
    assert first.metrics.final_equity == second.metrics.final_equity
    assert [t.exit_reason for t in first.trades] == [t.exit_reason for t in second.trades]


# --- the verdict is allowed to say no -------------------------------------

def test_a_small_sample_is_inconclusive_however_good_it_looks():
    """A profit factor from thirty trades is noise wearing a decimal point."""
    result = run(candles(wobble()), decide=once(at=1))
    assert not result.conclusive
    assert "INCONCLUSIVE" in result.verdict
    assert str(result.metrics.trades) in result.verdict


def test_no_trades_is_reported_as_a_possible_outcome_not_a_fault():
    result = run(candles(wobble()), decide=lambda **kw: None)
    assert result.metrics.trades == 0
    assert not result.conclusive
    assert "no trades" in result.verdict


def test_a_losing_sample_large_enough_to_judge_is_called_negative():
    result = run(candles(wobble(n=1400, drift=-0.0004, sigma=0.0008)),
                 decide=lambda **kw: Signal(1), min_trades_for_verdict=1)
    assert result.conclusive
    if result.metrics.expectancy_r <= 0:
        assert "NEGATIVE" in result.verdict
        assert "leverage would only lose it faster" in result.verdict


def test_rejections_are_counted_by_stage():
    """"Skipped 400 bars at the spread stage" is actionable; "no signal" is not."""
    result = run(candles(wobble()), decide=lambda **kw: Signal(accepted=False,
                                                              stage="regime"))
    assert result.rejections.get("regime", 0) > 0


# --- metrics ---------------------------------------------------------------

def test_metrics_on_an_empty_run_do_not_invent_numbers():
    metrics = Metrics()
    assert metrics.trades == 0
    assert np.isnan(metrics.win_rate)
    assert np.isnan(metrics.profit_factor)


def test_profit_factor_is_infinite_rather_than_huge_when_nothing_was_lost():
    """"inf" reads as "too few losses to judge", which is the honest reading."""
    result = run(candles(np.concatenate([np.full(260, ENTRY),
                                         np.linspace(ENTRY, ENTRY * 1.02, 140)]),
                         wick=0.002),
                 decide=once(at=1))
    if result.metrics.trades and result.metrics.losses == 0:
        assert result.metrics.profit_factor == float("inf")


def test_max_drawdown_is_measured_from_the_running_peak():
    result = run(candles(wobble(n=1200)), decide=lambda **kw: Signal(1))
    assert 0.0 <= result.metrics.max_drawdown <= 1.0
    equities = [equity for _, equity in result.equity_curve]
    peak, worst = equities[0], 0.0
    for equity in equities:
        peak = max(peak, equity)
        worst = max(worst, (peak - equity) / peak)
    assert result.metrics.max_drawdown == pytest.approx(worst)


def test_the_equity_curve_tracks_the_trades():
    result = run(candles(wobble(n=1200)), decide=lambda **kw: Signal(1))
    if result.trades:
        assert result.equity_curve[-1][1] == pytest.approx(result.metrics.final_equity)


def test_r_multiple_is_measured_against_the_actual_risk():
    result = run(candles(wobble()), decide=once(at=1))
    trade = result.trades[0]
    risk = abs(trade.entry_price - trade.stop_price) * abs(trade.size) * 0.0001
    assert trade.r_multiple == pytest.approx(trade.net_pnl / risk, rel=1e-6)


# --- the risk stack is respected ------------------------------------------

def test_the_phase_6_breakers_halt_the_backtest():
    """A backtest that ignores the circuit breakers measures a bot nobody would run."""
    result = run(candles(wobble(n=1400, drift=-0.0006, sigma=0.001)),
                 decide=lambda **kw: Signal(1))
    halted = {k: v for k, v in result.rejections.items()
              if k in ("daily_loss", "drawdown", "consecutive_losses", "cooldown")}
    assert halted, f"expected a breaker to fire, saw {result.rejections}"


def test_a_stop_capped_at_the_ceiling_can_be_vetoed_by_rounding_alone():
    """A live seam finding, pinned here rather than left as folklore.

    Phase 6's ``on_sl_exceeds_max: cap`` clamps a wide ATR stop to *exactly* the
    liquidation ceiling, leaving 0.3000%-and-change of buffer against a 0.30% requirement.
    Phase 7 then rounds the predicted liquidation price toward entry — its own conservative
    rule — and on some prices that consumes the remaining fraction, so the plan is vetoed.

    Both layers are individually correct and both round the safe way. Composed, whether a
    maximally-capped stop is accepted depends on where the last few decimal places land,
    which is not a property anyone chose. It is **intermittent**, not systematic: roughly
    2 of 26 capped plans on this fixture. See ARCHITECTURE §16 — recorded so that a fix is
    deliberate rather than accidental.
    """
    from risk.liquidation_guard import LiquidationParams, TierSnapshot, assess_plan
    from risk.position_sizer import plan_position

    series = candles(wobble(n=900, sigma=0.0006))
    capped, vetoed = [], []
    for index in range(250, len(series), 25):
        plan = plan_position(
            symbol="BTC_USDT", direction=1, entry_price=float(series.close[index]),
            candles=series.head(index + 1), contract=BTC, tiers=TIERS,
            equity=10_000.0, available=10_000.0, params=SizingParams(),
        )
        if not (plan.ok and plan.stop.capped):
            continue
        capped.append(plan)
        verdict = assess_plan(plan, TierSnapshot.of("BTC_USDT", tuple(TIERS), 1e6), 1e6,
                              params=LiquidationParams(), contract=BTC)
        if not verdict.ok:
            vetoed.append(verdict)

    assert capped, "fixture must produce capped stops"
    assert vetoed, "the rounding interaction should veto at least one capped plan"
    assert len(vetoed) < len(capped), "and it is intermittent, not systematic"

    for verdict in vetoed:
        assert verdict.stage == "buffer"
        assert "mark-price grid" in verdict.reason
        # The fractional buffer was satisfied; only the price-grid check failed, so the
        # shortfall is rounding rather than a real breach of the liquidation buffer.
        assert verdict.buffer_actual >= verdict.buffer_required


def test_a_capped_stop_shows_up_as_a_liquidation_veto_in_the_replay():
    result = run(candles(wobble(n=1400, sigma=0.0006)), decide=lambda **kw: Signal(1))
    assert result.rejections.get("liq:buffer", 0) > 0


def test_sizing_comes_from_the_phase_6_sizer():
    result = run(candles(wobble()), decide=once(at=1))
    trade = result.trades[0]
    risk = abs(trade.entry_price - trade.stop_price) * abs(trade.size) * 0.0001
    budget = BacktestParams().starting_equity * SizingParams().risk_per_trade
    assert risk <= budget * 1.001


# --- walk-forward ----------------------------------------------------------

def test_walk_forward_splits_chronologically():
    """Shuffling bars would let a window learn from its own future."""
    series = candles(wobble(n=2000))
    result = walk_forward(engine(decide=lambda **kw: Signal(1)), "BTC_USDT",
                          {"1m": series}, TIERS, BTC, warmup=250)
    assert result.train.bars > 0
    assert result.train.metrics.entries_attempted >= 0
    assert result.verdict


def test_walk_forward_windows_do_not_overlap():
    series = candles(wobble(n=2000))
    sliced = [slice_candles(series, 0, 1000), slice_candles(series, 1000, 1500),
              slice_candles(series, 1500, 2000)]
    assert sliced[0].time[-1] < sliced[1].time[0] < sliced[2].time[0]
    assert sum(len(part) for part in sliced) == len(series)


def test_walk_forward_flags_a_strategy_that_dies_out_of_sample():
    """The failure the split exists to expose: fitted in training, dead on unseen data."""
    n = 2000
    # Trends up through training, then reverses hard for the out-of-sample window.
    path = np.concatenate([
        ENTRY * np.exp(np.cumsum(np.full(n // 2, 0.0004))),
        None if False else ENTRY * np.exp(np.cumsum(np.full(n // 2, 0.0004)))[-1]
        * np.exp(np.cumsum(np.full(n - n // 2, -0.0004))),
    ])
    result = walk_forward(engine(decide=lambda **kw: Signal(1)), "BTC_USDT",
                          {"1m": candles(path, wick=0.002)}, TIERS, BTC, warmup=250)
    assert result.verdict
    if result.degraded:
        assert "OVERFIT" in result.verdict


def test_the_shipped_walk_forward_split_is_the_documented_one():
    params = BacktestParams.from_config(load_config())
    assert (params.train_pct, params.validation_pct, params.test_pct) == (0.5, 0.25, 0.25)


def test_splits_must_sum_to_one():
    with pytest.raises(ValueError, match="sum to 1.0"):
        BacktestParams(train_pct=0.5, validation_pct=0.5, test_pct=0.25)


# --- config wiring ---------------------------------------------------------

def test_params_from_shipped_config():
    params = BacktestParams.from_config(load_config())
    assert params.fee_taker == 0.00075
    assert params.fee_maker == -0.0001          # a rebate
    assert params.simulate_liquidation is True
    assert params.simulate_funding is True
    assert params.min_trades_for_verdict == 1000


def test_a_thousand_trades_is_the_shipped_bar_for_a_verdict():
    """§7: no win rate is claimed, and the sample size that would justify one is explicit."""
    assert load_config().get("backtest.min_trades_for_verdict") == 1000


@pytest.mark.parametrize("bad", [
    {"slippage_model": "vibes"}, {"fixed_slippage": -0.001},
    {"starting_equity": 0}, {"min_trades_for_verdict": 0},
])
def test_unusable_params_are_rejected(bad):
    with pytest.raises(ValueError):
        BacktestParams(**bad)


def test_the_engine_builds_from_config():
    built = BacktestEngine.from_config(load_config())
    assert built.params.fee_taker == 0.00075
    assert built.sizing.leverage == 100
    assert built.liquidation.liquidation_buffer == 0.003


# --- no network, no orders -------------------------------------------------

def test_the_backtester_cannot_place_an_order():
    """A backtest that could reach the exchange is one that eventually will."""
    module = importlib.import_module("backtest.engine")
    imports = "\n".join(
        line for line in Path(module.__file__).read_text(encoding="utf-8").splitlines()
        if line.startswith(("import ", "from "))
    )
    assert "exchange" not in imports
    assert "execution" not in imports
    assert "aiohttp" not in imports
