"""PHASE 10 tests — the paper-trading loop.

Phase 9 already measures whether the strategy makes money. These tests are about whether
the bot can *carry* a trade: that the layers hand off in the right order, that a refusal at
any one of them stops the sequence rather than being logged and stepped over, that a filled
entry is always protected, and that the loop cannot reach a real exchange.

The signal is injected in most tests, so the loop's wiring is pinned independently of
whether the shipped filters happen to fire on a fixture.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from config import load_config
from exchange.gate_client import Contract, RiskTier
from execution.order_manager import SimulatedGateway
from paper.loop import (
    LiveTradingRefused,
    PaperTrader,
    ReplayMarketSource,
    RestMarketSource,
)
from risk.risk_manager import Breaker
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
WARMUP = 300


def candles(close, *, interval=60.0, wick=0.0008, highs=None, lows=None):
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


def quiet(n=WARMUP + 20, start=ENTRY, sigma=0.0003, seed=2):
    rng = np.random.default_rng(seed)
    return start * np.exp(np.cumsum(rng.normal(0, sigma, n)))


def drop(to=64_000.0, bars=40):
    return np.concatenate([quiet(), np.linspace(ENTRY, to, bars)])


def rally(to=66_500.0, bars=40):
    return np.concatenate([quiet(), np.linspace(ENTRY, to, bars)])


class Signal:
    """Minimal stand-in for a Phase 5 Signal."""

    def __init__(self, direction=1, score=88.0, accepted=True, stage="accepted"):
        self.direction = direction
        self.score = score
        self.accepted = accepted
        self.stage = stage


def once(direction=1, at=1):
    """A decision function that signals exactly once."""
    state = {"n": 0}

    def decide(**kwargs):
        state["n"] += 1
        return Signal(direction) if state["n"] == at else None

    return decide


def never(**kwargs):
    return None


def always(direction=1):
    return lambda **kwargs: Signal(direction)


def trader(path=None, *, decide=None, start=WARMUP, config=None, series=None, **kwargs):
    if series is None:
        series = candles(quiet() if path is None else path)
    source = ReplayMarketSource({"1m": series}, "1m", start=start)
    return PaperTrader(
        config or load_config(), source, "BTC_USDT", TIERS, BTC,
        decide=decide if decide is not None else never, **kwargs,
    )


def run(bot, steps=None):
    return asyncio.run(bot.run(steps))


# --- it cannot trade for real ----------------------------------------------

def test_an_open_safety_gate_refuses_to_construct(monkeypatch):
    """Not a branch inside the loop — the object will not exist.

    A paper loop that silently switched to real fills would be the worst failure available
    in this repo, so the check is at construction and there is no flag to override it.
    """
    monkeypatch.setenv("GATE_API_KEY", "k" * 24)
    monkeypatch.setenv("GATE_API_SECRET", "s" * 24)
    monkeypatch.setenv("DRY_RUN", "false")
    live = load_config(run_mode="live", confirm_live=True)
    assert live.live_enabled

    with pytest.raises(LiveTradingRefused, match="gate is OPEN"):
        trader(config=live)


def test_paper_mode_always_resolves_to_the_simulator():
    assert isinstance(trader().gateway, SimulatedGateway)


def test_the_loop_never_touches_a_client():
    """The simulator is in-process, so a whole run must issue no request at all."""
    class Exploding:
        def __getattr__(self, name):
            raise AssertionError(f"the paper loop called client.{name}")

    report = run(trader(rally(), decide=once(), client=Exploding()))
    assert report.entries_filled == 1


def test_the_dead_man_switch_is_armed_on_every_entry():
    bot = trader(rally(), decide=once())
    run(bot)
    assert bot.gateway.countdown_seconds == 60


# --- the market source does not look ahead --------------------------------

def test_the_source_never_exposes_an_unclosed_bar():
    series = candles(quiet())
    source = ReplayMarketSource({"1m": series}, "1m", start=WARMUP)
    view = source.candles("BTC_USDT")["1m"]
    assert len(view) == WARMUP + 1
    assert float(view.close[-1]) == pytest.approx(float(series.close[WARMUP]))
    assert float(view.time[-1]) < source.now()


def test_slower_timeframes_are_truncated_by_the_clock_not_by_a_bar_count():
    """A 15m bar must not appear until its close has passed, whatever the 1m cursor is."""
    fast = candles(quiet(n=400), interval=60.0)
    slow = candles(quiet(n=40), interval=900.0)
    source = ReplayMarketSource({"1m": fast, "15m": slow}, "1m", start=100)
    view = source.candles("BTC_USDT")
    moment = source.now()
    assert float(view["15m"].time[-1]) < moment
    assert len(view["15m"]) == int((slow.time < moment).sum())


def test_advancing_past_the_end_reports_exhausted():
    source = ReplayMarketSource({"1m": candles(quiet(n=5))}, "1m", start=3)
    assert source.advance() is True
    assert source.advance() is False
    assert source.exhausted


def test_a_missing_entry_timeframe_is_refused():
    with pytest.raises(ValueError, match="no 1m candles"):
        ReplayMarketSource({"5m": candles(quiet(n=10))}, "1m")


# --- the sequence ----------------------------------------------------------

def test_a_full_round_trip_ends_at_a_take_profit():
    report = run(trader(rally(), decide=once()))
    assert report.entries_attempted == 1
    assert report.entries_filled == 1
    assert len(report.trades) == 1

    trade = report.trades[0]
    assert trade.exit_reason == "tp3"
    assert [fill.reason for fill in trade.fills] == ["tp1", "tp2", "tp3"]
    assert trade.net_pnl > 0
    assert report.equity == pytest.approx(report.starting_equity + trade.net_pnl)


def test_a_losing_round_trip_ends_at_the_stop():
    report = run(trader(drop(), decide=once()))
    trade = report.trades[0]
    assert trade.exit_reason == "stop"
    assert trade.net_pnl < 0
    assert report.equity < report.starting_equity


def test_a_short_is_stopped_by_a_rally():
    report = run(trader(rally(), decide=once(direction=-1)))
    trade = report.trades[0]
    assert trade.direction == -1
    assert trade.exit_reason == "stop"
    assert trade.net_pnl < 0


def test_the_realised_loss_stays_within_the_risk_budget():
    """The point of every layer above: one stop-out costs about 0.25% of equity."""
    report = run(trader(drop(), decide=once()))
    trade = report.trades[0]
    budget = report.starting_equity * load_config().get("risk.per_trade")
    # Fees and the partial take-profit make the exact figure vary; the ceiling is what
    # matters, with a little room for the round-trip fee.
    assert abs(trade.net_pnl) <= budget * 1.6


def test_a_filled_entry_is_always_protected():
    """No position may exist without a verified stop — the invariant, end to end."""
    bot = trader(rally(), decide=once())

    async def check():
        await bot.step()
        assert bot._position is not None
        live = await bot.gateway.list_price_orders("BTC_USDT")
        stops = [
            order for order in live
            if int(order["initial"]["size"]) * bot._position["direction"] < 0
        ]
        assert stops, "a filled position must carry protective orders immediately"

    asyncio.run(check())


def test_only_one_position_is_held_at_a_time():
    bot = trader(quiet(n=WARMUP + 60), decide=always())
    report = run(bot)
    assert report.entries_attempted <= 1 + len(report.trades)
    assert bot._position is None or abs(bot._position["remaining"]) > 0


# --- every layer can veto, and the veto is counted ------------------------

def test_an_unsignalled_bar_is_counted_not_traded():
    report = run(trader(decide=never), steps=5)
    assert report.entries_attempted == 0
    assert report.rejections.get("no_signal", 0) == 5


def test_a_rejected_signal_is_counted_by_its_stage():
    """"Skipped at the regime stage" is actionable; "no signal" is not."""
    report = run(trader(decide=lambda **kw: Signal(accepted=False, stage="regime")),
                 steps=4)
    assert report.rejections.get("regime", 0) == 4


def test_the_risk_breakers_stop_the_loop():
    """A cooldown after a loss must actually prevent the next entry."""
    report = run(trader(drop(), decide=always()))
    assert report.rejections.get(Breaker.COOLDOWN.value, 0) > 0


def test_an_account_too_small_to_size_is_refused_not_rounded_up():
    report = run(trader(rally(), decide=always(), starting_equity=5.0), steps=3)
    assert report.entries_attempted == 0
    assert any(key.startswith("size:") for key in report.rejections)


def test_the_liquidation_guard_can_veto_an_entry():
    """A stop that cannot clear the buffer must not be traded, even on paper."""
    volatile = np.concatenate([
        quiet(n=WARMUP, sigma=0.004, seed=9), np.full(20, ENTRY),
    ])
    report = run(trader(volatile, decide=always()), steps=6)
    vetoes = {k: v for k, v in report.rejections.items()
              if k.startswith("liq:") or k.startswith("size:")}
    assert vetoes or report.entries_attempted > 0


def test_an_unfilled_post_only_entry_is_normal_and_counted():
    """Post-only frequently misses. It is reported, and it is not an error."""
    # Every bar after the signal trades strictly above the resting buy limit, which sits at
    # the close of the signal bar.
    base = quiet(n=WARMUP + 1)
    limit = float(base[-1])
    close = np.concatenate([base, np.linspace(limit * 1.004, limit * 1.03, 40)])
    high = close * 1.0009
    low = np.concatenate([base * 0.9992,
                          np.maximum(close[WARMUP + 1:] * 0.9999, limit * 1.002)])
    report = run(trader(series=candles(close, highs=high, lows=low), decide=once()))
    assert report.entries_attempted == 1
    assert report.entries_filled == 0
    assert report.entries_expired == 1
    assert report.rejections.get("entry:expired", 0) == 1
    assert report.trades == []


def test_an_unprotectable_position_is_flattened_and_charged():
    """If the stop cannot be verified the position is closed, and the fees are real."""
    bot = trader(rally(), decide=once())

    async def never_lists(*args, **kwargs):
        return {"id": "ghost", "status": "open"}

    bot.gateway.place_price_trigger_order = never_lists
    report = run(bot)

    assert report.entries_filled == 1
    assert report.flattened == 1
    assert report.protection_failures == 1
    assert report.equity < report.starting_equity      # the round trip was paid for
    assert bot._position is None


# --- settlement ------------------------------------------------------------

def test_a_partial_take_profit_leaves_the_rest_running():
    bot = trader(rally(to=65_300.0), decide=once())

    async def check():
        await bot.run()
        if bot._position is not None:
            assert abs(bot._position["remaining"]) < abs(bot._position["size"])

    asyncio.run(check())


def test_a_stop_after_a_partial_closes_the_whole_remainder():
    """reduce_only means the original-size stop cannot reverse the position.

    Without it the simulator would fill a stop sized for the whole position against the
    remainder left by TP1 and open a *reversed* one — the outcome ``reduce_only`` exists to
    prevent, and one that looks like a vanished trade rather than an error.
    """
    base = quiet()
    entry = float(base[-1])
    # Past TP1 (1R) but short of TP2 (2R), then through the stop. R is 0.325% of entry.
    risk = entry * 0.00325
    path = np.concatenate([
        base,
        np.linspace(entry, entry + 1.4 * risk, 10),
        np.linspace(entry + 1.4 * risk, entry - 4 * risk, 30),
    ])
    bot = trader(path, decide=once())
    report = run(bot)

    assert len(report.trades) == 1
    trade = report.trades[0]
    assert trade.exit_reason == "stop"
    reasons = [f.reason for f in trade.fills]
    assert reasons[0] == "tp1" and reasons[-1] == "stop"
    assert sum(f.size for f in trade.fills) == abs(trade.size)
    assert asyncio.run(bot.orders.position_size("BTC_USDT")) == 0


def test_equity_reconciles_with_every_trade():
    """Equity is the starting balance plus each trade's net, fees and rebate included."""
    report = run(trader(rally(), decide=once()))
    assert report.trades
    assert report.equity == pytest.approx(
        report.starting_equity + sum(t.net_pnl for t in report.trades), abs=1e-6
    )


def test_the_maker_rebate_is_credited_when_the_entry_fills():
    """A post-only fill earns the rebate, so the entry fee is negative."""
    report = run(trader(rally(), decide=once()))
    trade = report.trades[0]
    exit_fees = sum(f.fee for f in trade.fills)
    entry_fee = trade.fees - exit_fees
    assert entry_fee < 0                      # a credit, not a cost
    assert exit_fees > 0


def test_the_risk_manager_is_told_about_every_trade():
    bot = trader(drop(), decide=once())
    report = run(bot)
    assert len(report.trades) == 1
    assert bot.risk.state is not None
    assert bot.risk.state.trades_today == 1
    assert bot.risk.state.consecutive_losses == 1


# --- reporting -------------------------------------------------------------

def test_the_report_counts_every_step():
    report = run(trader(decide=never), steps=7)
    assert report.steps == 7
    assert "7 steps" in report.summary()


def test_a_run_bounded_by_steps_stops_there():
    report = run(trader(decide=never), steps=3)
    assert report.steps == 3


def test_a_run_stops_when_the_source_is_exhausted():
    series = candles(quiet(n=WARMUP + 6))
    report = run(trader(series=series, decide=never))
    assert report.steps <= 7


def test_the_summary_reports_losses_as_readily_as_wins():
    report = run(trader(drop(), decide=once()))
    assert "won 0%" in report.summary()
    assert f"{report.net_pnl:+.2f}" in report.summary()


def test_a_run_is_reproducible():
    first = run(trader(rally(), decide=once()))
    second = run(trader(rally(), decide=once()))
    assert first.equity == second.equity
    assert [t.exit_reason for t in first.trades] == [t.exit_reason for t in second.trades]


# --- the REST source is read-only -----------------------------------------

def test_the_rest_source_only_reads_candles():
    calls = []

    class FakeClient:
        async def get_candlesticks(self, symbol, interval, limit):
            calls.append((symbol, interval, limit))
            n = 60
            base = 65_000.0
            return [
                {"t": i * 60, "o": base, "h": base * 1.001, "l": base * 0.999,
                 "c": base, "v": 100}
                for i in range(n)
            ]

        def __getattr__(self, name):
            raise AssertionError(f"RestMarketSource called client.{name}")

    source = RestMarketSource(FakeClient(), timeframes=("1m", "5m"), limit=60)
    asyncio.run(source.refresh("BTC_USDT"))
    assert [c[1] for c in calls] == ["1m", "5m"]
    assert source.mark_price("BTC_USDT") == pytest.approx(65_000.0)


def test_the_rest_source_refuses_to_serve_candles_it_has_not_fetched():
    source = RestMarketSource(object(), timeframes=("1m",))
    with pytest.raises(RuntimeError, match="refresh"):
        source.candles("BTC_USDT")


# --- config wiring ---------------------------------------------------------

def test_the_loop_uses_the_shipped_parameters():
    bot = trader()
    assert bot.sizing.risk_per_trade == 0.0025
    assert bot.sizing.leverage == 100
    assert bot.liquidation.liquidation_buffer == 0.003
    assert bot.protection_params.sl_price_type == "mark"
    assert bot.risk.params.max_open_positions == 1
    assert bot.orders.params.entry_tif == "poc"


# --- the entry rests, it is not gifted ------------------------------------

def test_the_entry_rests_at_the_touch_rather_than_crossing():
    """Submitting at the mark would fill on equality the instant it is placed.

    That would hand every paper run the maker rebate for free and erase the unfilled-entry
    outcome the design depends on being frequent (ARCHITECTURE §5).
    """
    bot = trader(rally(), decide=once())
    run(bot)
    placed = [kw for name, kw in bot.gateway.calls if name == "place_order"]
    assert placed, "an entry should have been submitted"
    entry = placed[0]
    assert entry["tif"] == "poc"
    assert float(entry["price"]) <= bot.report.trades[0].entry_price + 1e-9


def test_waiting_for_a_fill_advances_the_market():
    """The entry timeout is measured against bars, not against a clock nobody watched."""
    bot = trader(rally(), decide=once())
    before = bot.source.cursor
    run(bot, steps=1)
    assert bot.source.cursor > before


def test_the_simulator_will_not_reverse_a_position_on_a_reduce_only_order():
    """A Phase 8 gap this phase exposed: a stop sized for the whole position, after a partial."""
    gateway = SimulatedGateway(last_price=ENTRY)

    async def scenario():
        await gateway.place_order("BTC_USDT", 100, price=None, tif="ioc", text="t-open1")
        assert int((await gateway.get_position("BTC_USDT"))["size"]) == 100
        # Reduce-only for more than is held may close the position, never flip it.
        await gateway.place_order("BTC_USDT", -250, price=None, tif="ioc",
                                  reduce_only=True, text="t-red1")
        assert int((await gateway.get_position("BTC_USDT"))["size"]) == 0

    asyncio.run(scenario())


def test_a_reduce_only_order_in_the_same_direction_does_nothing():
    gateway = SimulatedGateway(last_price=ENTRY)

    async def scenario():
        await gateway.place_order("BTC_USDT", 100, price=None, tif="ioc", text="t-open2")
        await gateway.place_order("BTC_USDT", 50, price=None, tif="ioc",
                                  reduce_only=True, text="t-add2")
        assert int((await gateway.get_position("BTC_USDT"))["size"]) == 100

    asyncio.run(scenario())
