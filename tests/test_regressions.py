"""PHASE 12 — regressions for defects this repo has actually shipped.

Every bug below was real, was found, and was fixed. Each already has a test in the phase
that fixed it. This file exists because those tests were written to describe the *fix*, and
a regression test's job is to fail when the fix is *reverted* — which is not the same
property, and is the one that decays as code moves underneath it.

The clearest example is the post-only entry. `test_the_entry_rests_at_the_touch_rather_than_crossing`
in `tests/test_paper.py` asserts the submitted limit is `<= entry_price + 1e-9`. An entry
submitted *at* the mark satisfies that comfortably — so the assertion passes in exactly the
state the defect describes. §"the post-only entry" below pins the strict inequality and the
consequence (the unfilled outcome must remain reachable) instead.

Each test names the defect, why it was expensive, and what the assertion would catch. Where
a phase test already covers a case properly, it is referenced rather than duplicated.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from config import load_config
from exchange.gate_client import Contract, RiskTier
from execution.order_manager import SimulatedGateway
from paper.loop import PaperTrader, ReplayMarketSource
from risk.liquidation_guard import LiquidationParams, TierSnapshot, assess_plan
from risk.position_sizer import SizingParams, plan_position, resolve_stop
from strategy.indicators import Candles, relative_volume
from strategy.regime import Regime, classify
from strategy.scoring import score
from strategy.signal_engine import EngineParams, SignalEngine

BTC = Contract.from_api({
    "name": "BTC_USDT", "leverage_max": "200", "leverage_min": "1",
    "maintenance_rate": "0.003", "quanto_multiplier": "0.0001",
    "order_size_min": 1, "order_size_max": 12000000,
    "order_price_round": "0.1", "mark_price_round": "0.01",
    "taker_fee_rate": "0.00075", "maker_fee_rate": "-0.0001",
    "risk_limit_base": "500000", "in_delisting": False, "status": "trading",
})
TIERS = [RiskTier.from_api({
    "tier": 1, "risk_limit": "500000", "initial_rate": "0.005",
    "maintenance_rate": "0.003", "leverage_max": "200", "deduction": "0",
})]

ENTRY = 65_000.0
WARMUP = 300


def candles(close, *, interval=60.0, wick=0.0008, t0=0.0):
    close = np.asarray(close, dtype=float)
    n = len(close)
    open_ = np.concatenate([[close[0]], close[:-1]])
    return Candles(
        time=t0 + np.arange(n, dtype=float) * interval,
        open=open_,
        high=np.maximum(open_, close) * (1 + wick),
        low=np.minimum(open_, close) * (1 - wick),
        close=close,
        volume=np.full(n, 1000.0),
    )


def quiet(n=WARMUP + 20, start=ENTRY, sigma=0.0003, seed=2):
    rng = np.random.default_rng(seed)
    return start * np.exp(np.cumsum(rng.normal(0, sigma, n)))


def rally(to=66_500.0, bars=40):
    return np.concatenate([quiet(), np.linspace(ENTRY, to, bars)])


class Signal:
    def __init__(self, direction=1, score=88.0, accepted=True, stage="accepted"):
        self.direction = direction
        self.score = score
        self.accepted = accepted
        self.stage = stage


def once(direction=1, at=1):
    state = {"n": 0}

    def decide(**kwargs):
        state["n"] += 1
        return Signal(direction) if state["n"] == at else None

    return decide


def trader(path=None, *, decide=None, **kwargs):
    source = ReplayMarketSource(
        {"1m": candles(quiet() if path is None else path)}, "1m", start=WARMUP,
    )
    return PaperTrader(
        load_config(), source, "BTC_USDT", TIERS, BTC,
        decide=decide if decide is not None else once(), **kwargs,
    )


# --- the post-only entry that was gifted the maker rebate ------------------
#
# Phase 10. The simulator fills a resting limit on equality, so an entry submitted *at* the
# mark filled the instant it was placed. Every paper run collected the maker rebate for
# free and the unfilled-entry outcome — which §5 says should be frequent — never occurred.
# "Would have made every paper run look better than the live venue ever will."

def test_the_entry_rests_strictly_inside_the_mark_not_on_it():
    """The strict inequality is the whole fix; `<=` would pass in the defective state.

    `tests/test_paper.py` asserts `limit <= entry + 1e-9`, which an at-the-mark entry
    satisfies. This asserts the limit is strictly better than the mark by a whole tick, in
    both directions, which is what makes the order actually passive.

    The mark is read *before* stepping: the loop advances the replay while the entry rests,
    so reading it afterwards compares the limit against a later bar.
    """
    for direction in (1, -1):
        bot = trader(decide=once(direction))
        mark = bot.source.mark_price("BTC_USDT")

        async def capture():
            await bot.step()
            return [kw for name, kw in bot.gateway.calls if name == "place_order"]

        placed = asyncio.run(capture())
        assert placed, "an entry should have been submitted"

        entry = placed[0]
        assert entry["tif"] == "poc", "the entry must be post-only"
        limit = float(entry["price"])
        tick = float(BTC.order_price_round)

        if direction > 0:
            assert limit < mark, f"a long entry rested at or above the mark ({limit} vs {mark})"
            assert mark - limit == pytest.approx(tick, rel=1e-6)
        else:
            assert limit > mark, f"a short entry rested at or below the mark ({limit} vs {mark})"
            assert limit - mark == pytest.approx(tick, rel=1e-6)


def test_a_market_that_never_comes_back_leaves_the_entry_unfilled():
    """The outcome the defect erased. It must stay reachable.

    A long resting one tick below the mark, in a market that only rises from the decision
    bar onward, must expire. The fixture needs care in two ways:

    * the bars after the decision carry no *lower* wick, because a bar that dips to its own
      low would legitimately touch the resting limit and the fill would be correct;
    * the warm-up keeps its wicks, because a zero-range series has no ATR and the plan is
      refused at `size:price_grid` before an entry is ever attempted.

    If this ever fills again, post-only has silently become a taker entry — and at these
    stop widths a taker entry needs a ~73 % win rate to break even (ARCHITECTURE §5).
    """
    base = quiet(n=WARMUP + 1)
    start = float(base[-1])
    close = np.concatenate([base, start * np.exp(np.arange(1, 80) * 0.001)])
    n = len(close)
    open_ = np.concatenate([[close[0]], close[:-1]])
    low = np.minimum(open_, close) * (1 - 0.0008)
    low[WARMUP + 1:] = np.minimum(open_, close)[WARMUP + 1:]     # no dip below the open

    series = Candles(
        time=np.arange(n, dtype=float) * 60.0,
        open=open_,
        high=np.maximum(open_, close) * 1.0008,
        low=low,
        close=close,
        volume=np.full(n, 1000.0),
    )
    bot = PaperTrader(
        load_config(), ReplayMarketSource({"1m": series}, "1m", start=WARMUP),
        "BTC_USDT", TIERS, BTC, decide=once(),
    )
    report = asyncio.run(bot.run())

    assert report.entries_attempted >= 1
    assert report.entries_filled == 0, "a post-only entry that was never touched filled"
    assert report.entries_expired >= 1
    assert report.rejections.get("entry:expired")


def test_the_simulator_still_refuses_to_fill_a_resting_order_price_never_reached():
    """The root cause, at the layer it lived in.

    The paper loop compensates by resting a tick passive; that compensation is only sound
    if the simulator itself does not fill orders the market never traded through.
    """
    gateway = SimulatedGateway(last_price=ENTRY)

    async def scenario():
        # A buy one tick below the last price must not fill on placement.
        raw = await gateway.place_order(
            "BTC_USDT", 100, price=str(ENTRY - 0.1), tif="poc", text="t-reg1",
        )
        assert raw["status"] == "open", "a resting buy filled without the market reaching it"

        gateway.advance(ENTRY - 0.05)          # closer, but still above the limit
        assert (await gateway.get_order(raw["id"]))["status"] == "open"

        gateway.advance(ENTRY - 0.1)           # trades through it: now it fills
        assert (await gateway.get_order(raw["id"]))["status"] == "finished"

    asyncio.run(scenario())


def test_the_maker_rebate_is_only_earned_on_an_entry_that_actually_rested():
    """The rebate is a consequence of resting, not a constant added to every run."""
    report = asyncio.run(trader(rally(), decide=once()).run())
    assert report.trades

    trade = report.trades[0]
    exit_fees = sum(fill.fee for fill in trade.fills)
    entry_fee = trade.fees - exit_fees
    assert entry_fee < 0, "the entry fee was not a maker rebate"
    # And it is the configured maker rate, not an invented number.
    notional = abs(trade.size) * BTC.quanto_multiplier * trade.entry_price
    expected = notional * float(load_config().get("backtest.fee_maker"))
    assert entry_fee == pytest.approx(expected, rel=1e-6)


# --- reduce_only, and the position that reversed itself --------------------
#
# Phase 10. After TP1 trimmed a position, the stop — still sized for the whole original
# position — filled in full and opened a *reversed* one. It looked like a vanished trade
# rather than an error.

@pytest.mark.parametrize("direction", [1, -1])
def test_a_reduce_only_order_can_never_flip_a_position(direction):
    """Both directions. `tests/test_paper.py` pins the long case only."""
    gateway = SimulatedGateway(last_price=ENTRY)
    size = 100 * direction

    async def scenario():
        await gateway.place_order("BTC_USDT", size, price=None, tif="ioc", text="t-reg2")
        assert int((await gateway.get_position("BTC_USDT"))["size"]) == size

        # Reduce-only for more than is held: it may close, never reverse.
        await gateway.place_order(
            "BTC_USDT", -size * 3, price=None, tif="ioc", reduce_only=True, text="t-reg3",
        )
        held = int((await gateway.get_position("BTC_USDT"))["size"])
        assert held == 0, f"a reduce-only order left {held} contracts — it reversed"

    asyncio.run(scenario())


def test_a_reduce_only_fill_after_a_partial_closes_only_what_remains():
    """The exact shape of the defect: an original-size stop against a trimmed position."""
    gateway = SimulatedGateway(last_price=ENTRY)

    async def scenario():
        await gateway.place_order("BTC_USDT", 1_000, price=None, tif="ioc", text="t-reg4")
        # TP1 trims 40%.
        await gateway.place_order("BTC_USDT", -400, price=None, tif="ioc",
                                  reduce_only=True, text="t-reg5")
        assert int((await gateway.get_position("BTC_USDT"))["size"]) == 600

        # The stop is still sized for the original 1,000.
        await gateway.place_order("BTC_USDT", -1_000, price=None, tif="ioc",
                                  reduce_only=True, text="t-reg6")
        held = int((await gateway.get_position("BTC_USDT"))["size"])
        assert held == 0, f"the oversized stop left {held} contracts instead of closing flat"

    asyncio.run(scenario())


def test_a_close_order_is_exempt_from_the_clamp_but_still_cannot_reverse():
    """`close=True` sizes itself from the position, so the clamp must not fight it."""
    gateway = SimulatedGateway(last_price=ENTRY)

    async def scenario():
        await gateway.place_order("BTC_USDT", 250, price=None, tif="ioc", text="t-reg7")
        await gateway.place_order("BTC_USDT", 0, price=None, tif="ioc",
                                  reduce_only=True, close=True, text="t-reg8")
        assert int((await gateway.get_position("BTC_USDT"))["size"]) == 0

    asyncio.run(scenario())


# --- the entry fee that never reached equity -------------------------------
#
# Phase 10, third defect: the entry fee was recorded on the trade but never applied to the
# account, so equity and the trade ledger disagreed.

def test_equity_equals_the_starting_balance_plus_every_trade():
    """If a fee is booked to a trade but not to equity, this is where it shows."""
    report = asyncio.run(trader(rally(), decide=once()).run())
    assert report.trades

    assert report.equity == pytest.approx(
        report.starting_equity + sum(t.net_pnl for t in report.trades), abs=1e-9
    )
    # And each trade's net is its gross minus its own fees — no fee booked twice or lost.
    for trade in report.trades:
        assert trade.net_pnl == pytest.approx(trade.gross_pnl - trade.fees, abs=1e-9)


# --- the Phase 6/7 seam: a capped stop vetoed by rounding alone ------------
#
# Commit bd7977c. The sizer clamped a wide ATR stop onto the liquidation ceiling exactly;
# the guard then rounded liquidation toward entry, consuming the last fraction of buffer
# and vetoing intermittently — roughly 2 of 26 capped plans, depending on decimals.

def test_a_maximally_capped_stop_reserves_room_for_the_guards_rounding():
    """The reserve is the fix. Without it the distance would sit *on* the ceiling.

    `tests/test_backtest.py` sweeps for vetoes; this asserts the mechanism directly, so a
    refactor that removed the reserve fails here with a clear cause rather than as an
    intermittent veto somewhere downstream.
    """
    # A volatile series guarantees the ATR stop is wider than the ceiling, forcing the cap.
    series = candles(quiet(sigma=0.01, seed=3))
    entry = float(series.close[-1])
    stop = resolve_stop(series, +1, entry, ceiling=0.00325,
                        params=SizingParams(), price_tick=float(BTC.order_price_round))

    assert stop.ok, stop.reason
    assert stop.capped, "the fixture must actually hit the ceiling"
    assert stop.distance < stop.ceiling, (
        "the capped stop landed on the ceiling exactly, leaving nothing for the guard's "
        "rounding — this is the bd7977c defect"
    )
    # The reserve is about one tick, not an arbitrary haircut.
    assert stop.ceiling - stop.distance <= 2 * float(BTC.order_price_round) / entry


def test_the_ceiling_is_still_reported_truthfully_after_the_reserve():
    """Only the distance used is pulled inside; `ceiling` must remain the real limit.

    If the reserve were folded into the reported ceiling, each layer that re-derived a
    ceiling from it would shave another tick, and the stop would creep in over time.
    """
    series = candles(quiet(sigma=0.01, seed=3))
    entry = float(series.close[-1])
    stop = resolve_stop(series, +1, entry, ceiling=0.00325,
                        params=SizingParams(), price_tick=float(BTC.order_price_round))
    assert stop.ceiling == pytest.approx(0.00325)


def test_a_capped_plan_survives_the_guard_at_awkward_prices():
    """The composition, at prices chosen to land badly on the grid."""
    liq = LiquidationParams.from_config(load_config())
    capped = 0
    for price in (137.77, 3_642.19, 65_000.07, 97_531.7):
        series = candles(quiet(start=price, sigma=0.01, seed=3))
        plan = plan_position(
            symbol="BTC_USDT", direction=1, entry_price=price, candles=series,
            contract=BTC, tiers=TIERS, equity=10_000.0, available=10_000.0,
            params=SizingParams.from_config(load_config()),
        )
        if not plan.ok:
            continue
        capped += bool(plan.stop.capped)
        verdict = assess_plan(
            plan, TierSnapshot.of("BTC_USDT", TIERS, 1e6), 1e6, params=liq, contract=BTC,
        )
        assert verdict.ok, f"capped plan vetoed at price={price}: {verdict.reason}"
    assert capped, "no plan was capped; the fixture no longer exercises the defect"


# --- the stop that rounded past its own entry ------------------------------
#
# Found by the Phase 12 sizer→guard sweep. When the entry price is off the order grid and
# the tick is wide relative to the stop, rounding "toward entry" overshot to the far side
# of it: a long with a stop *above* its entry, reported as healthy because the distance
# calculation takes an absolute value. Fixed in risk/position_sizer.py.
#
# The unit-level pins live in tests/test_position_sizer.py; these two pin the consequences.

def test_no_plan_ever_reaches_the_guard_with_an_inverted_stop():
    """The guard would catch it (`stop_side`) — but the sizer must not produce it.

    Relying on the downstream check means the invariant holds only where that check runs,
    and `PositionPlan.max_loss` would already have been computed from a nonsense distance.
    """
    params = SizingParams.from_config(load_config())
    for price in (0.4137, 9.37, 137.0, 3_642.19, 65_000.0):
        for direction in (1, -1):
            plan = plan_position(
                symbol="BTC_USDT", direction=direction, entry_price=price,
                candles=candles(quiet(start=price, sigma=0.0004, seed=5)),
                contract=BTC, tiers=TIERS, equity=10_000.0, available=10_000.0,
                params=params,
            )
            if not plan.ok:
                continue
            if direction > 0:
                assert plan.stop.price < plan.entry_price, (
                    f"long plan at {price} carries a stop above entry"
                )
            else:
                assert plan.stop.price > plan.entry_price, (
                    f"short plan at {price} carries a stop below entry"
                )


def test_the_refusal_names_the_price_grid_so_the_cause_is_findable():
    """A refusal an operator cannot act on gets worked around instead of fixed."""
    stop = resolve_stop(
        candles(np.full(320, 9.37)), +1, 9.37, ceiling=0.00325,
        params=SizingParams(), price_tick=0.1,
    )
    assert not stop.ok
    assert stop.stage == "price_grid"
    assert "9.37" in stop.reason and "tick" in stop.reason


# --- Phase 5: a calm market that read as violent ---------------------------
#
# The obvious percentile — (history <= now).mean() — scores a perfectly constant series at
# 1.0, because every sample ties. A calm tape reported HIGH_VOLATILITY and was vetoed
# exactly when conditions were best. Fixed with a midrank counting ties as half.

def test_a_dead_flat_market_is_not_reported_as_maximally_volatile():
    """The regression is the *tie* case; noisy fixtures never reach it.

    A constant price with a constant range is the exact input that makes every ATR sample
    equal, so `(history <= now).mean()` returns 1.0 — the top of the scale — for the calmest
    possible tape. Note the wick must be non-zero: a zero-range series has no ATR at all and
    `classify` refuses it earlier, never reaching the percentile.
    """
    read = classify(candles(np.full(400, ENTRY), wick=0.0015))

    assert read.metrics["atr_percentile"] == pytest.approx(0.5, abs=0.05), (
        "a constant series scored at an extreme — ties are not being counted as half"
    )
    assert read.regime is not Regime.HIGH_VOLATILITY


def test_the_percentile_stays_inside_its_bounds_on_pathological_input():
    """Whatever the input, a percentile outside [0, 1] would corrupt every band check."""
    for series in (
        candles(np.full(400, ENTRY), wick=0.0015),                 # every sample ties
        candles(np.linspace(ENTRY, ENTRY * 2, 400)),               # monotone
        candles(quiet(n=400, sigma=0.03, seed=8)),                 # violent
    ):
        percentile = classify(series).metrics["atr_percentile"]
        assert 0.0 <= percentile <= 1.0


def test_a_zero_range_series_is_refused_rather_than_scored():
    """The neighbouring fail-closed path, so the fixture above cannot drift into it."""
    read = classify(candles(np.full(400, ENTRY), wick=0.0))
    assert read.regime is None
    assert "ATR" in read.reason


# --- Phase 5: support/resistance zeroed every breakout ---------------------
#
# A NaN from nearest_resistance scored as "unknown -> 0", killing the breakout playbook,
# because a rally contains no pivot highs. Falls back to the lookback extreme, excluding
# the current bar — a bar's own high is >= its close, so otherwise no new high reads clear.

def test_a_clean_breakout_is_not_scored_as_having_no_room():
    """A monotone advance contains no pivot highs at all — that is the defective input.

    `nearest_resistance` returns NaN here, which used to score as "unknown -> 0" and zeroed
    the breakout playbook. The fallback to the lookback extreme is what makes it non-zero,
    so this fails if that fallback is removed.
    """
    smooth = candles(ENTRY * np.exp(np.arange(300) * 0.0015))
    result = score(smooth, direction=1)

    assert result.categories["support_resistance"].fraction > 0.0, (
        "a breakout with no pivot highs scored zero — the fallback is gone"
    )


def test_a_bar_does_not_count_as_its_own_overhead_resistance():
    """A bar's own high is >= its close, so including it means no close is ever clear.

    On a series making a new high every bar, an inclusive window puts resistance *at* the
    current bar and the score collapses to zero for every long, forever.
    """
    smooth = candles(ENTRY * np.exp(np.arange(300) * 0.0015))
    assert score(smooth, direction=1).categories["support_resistance"].fraction > 0.0


# --- Phase 5: the volatility veto that only checked one timeframe ---------
#
# A 5m ATR blowout slipped through to a 1m entry. `tests/test_signal_engine.py` pins the 5m
# case; this pins that *every* configured veto timeframe disqualifies, so adding one to the
# config cannot silently leave it unenforced.

@pytest.mark.parametrize("timeframe", ["1m", "5m", "15m", "1h"])
def test_a_blowout_on_any_timeframe_disqualifies(timeframe):
    engine = SignalEngine(params=EngineParams(min_score=0.0))
    intervals = {"1m": 60.0, "5m": 300.0, "15m": 900.0, "1h": 3600.0}
    bars = 260
    end = bars * 3600.0

    def frames():
        # Anchored on a shared end instant, so every timeframe's last bar has closed at
        # `now`. Equal bar counts from a common t0 would run the 1h series hours past the
        # 1m one. The jitter is load-bearing too: a perfectly smooth exponential has a
        # monotonically climbing ATR%, which reads as HIGH_VOLATILITY on its own.
        rng = np.random.default_rng(1)
        path = ENTRY * np.exp(np.cumsum(np.full(bars, 0.0015) + rng.normal(0, 0.0004, bars)))
        return {
            name: candles(path, interval=seconds, t0=end - bars * seconds)
            for name, seconds in intervals.items()
        }

    baseline = engine.evaluate("BTC_USDT", frames(), now=end)
    assert baseline.accepted, "baseline must be tradeable for this test to mean anything"

    data = frames()
    blown = data[timeframe]
    blown.high[-40:] = blown.close[-40:] * 1.15
    blown.low[-40:] = blown.close[-40:] * 0.85

    out = engine.evaluate("BTC_USDT", data, now=end)
    assert not out.accepted, f"a volatility blowout on {timeframe} was not vetoed"
    assert out.stage == "volatility"
    assert timeframe in out.reason


# --- Phase 4: relative volume that diluted its own spike -------------------
#
# An inclusive average puts the spike in its own baseline, so a 4x bar reads 3.25x and the
# BTC-spike guard would need a 4.75x bar before suspending alt entries.

def test_a_volume_spike_is_measured_against_a_baseline_that_excludes_it():
    volume = np.full(30, 100.0)
    volume[-1] = 400.0
    out = relative_volume(volume, 20)

    assert out[-1] == pytest.approx(4.0), (
        f"a 4x bar read as {out[-1]:.2f}x — the current bar is in its own baseline"
    )
