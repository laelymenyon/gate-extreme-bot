"""PHASE 12 — cross-layer integration.

Phases 2-11 each test one layer against fixtures chosen for that layer. Every one of them
passes. That is not the same as the layers agreeing with each other, and this repo has
already shipped one defect of exactly that kind: the Phase 6 sizer capped a stop onto the
liquidation ceiling that the Phase 7 guard then rounded past, so two individually-correct
layers composed into an intermittent veto (commit ``bd7977c``). Nothing in either layer's
own tests could see it, because neither layer was wrong.

So these tests exercise seams rather than units. The rule they follow: **use the real
objects on both sides**. Where a Phase 10 test injects a signal to pin the loop's wiring
independently of the filters, here the shipped `SignalEngine`, the shipped `SizingParams`,
the real `RiskManager`, the real `ProtectionEngine` and the real `TradeStore` are wired
together and the handoff itself is the thing under test.

One seam has never been tested at all: Phase 10 produces `PaperTrade`s and Phase 11 stores
`TradeRecord`s, but no test has ever run a paper loop and put its output in the database.
`main.py --stats` reads that database. §"paper → database → dashboard" closes it.

Deterministic and offline: seeded fixtures, `SimulatedGateway`, `tmp_path` databases. The
autouse guard in ``conftest.py`` fails any test that reaches for a socket.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from backtest.engine import BacktestEngine, BacktestParams
from config import load_config
from database.models import EquityPoint, TradeRecord, TradeStore
from exchange.gate_client import Contract, RiskTier
from execution.order_manager import (
    ExecutionParams,
    OrderManager,
    OrderState,
    SimulatedGateway,
)
from execution.protection import ProtectionEngine, ProtectionParams
from monitoring.dashboard import Dashboard, compute
from paper.loop import PaperTrader, ReplayMarketSource
from risk.liquidation_guard import LiquidationParams, TierSnapshot, assess_plan, verify_fill
from risk.position_sizer import SizingParams, plan_position
from risk.risk_manager import Breaker, MemoryRiskStore, RiskManager, RiskParams, SqliteRiskStore
from strategy.indicators import Candles
from strategy.signal_engine import EngineParams, SignalEngine

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
EQUITY = 10_000.0
NOW = 1_754_784_000.0     # a fixed UTC instant; the risk manager refuses now <= 0


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


def drop(to=64_000.0, bars=40):
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


def trader(path=None, *, decide=None, config=None, t0=0.0, **kwargs):
    series = candles(quiet() if path is None else path, t0=t0)
    source = ReplayMarketSource({"1m": series}, "1m", start=WARMUP)
    return PaperTrader(
        config or load_config(), source, "BTC_USDT", TIERS, BTC,
        decide=decide if decide is not None else once(), **kwargs,
    )


def run(bot, steps=None):
    return asyncio.run(bot.run(steps))


# --- sizer -> guard: the seam that already broke once -----------------------

def test_every_plan_the_sizer_accepts_the_guard_also_accepts():
    """The composition property, swept rather than sampled.

    Commit ``bd7977c`` fixed a case where the sizer produced a maximally-capped stop and the
    guard then vetoed it, depending on where the last decimals landed. Both layers were
    individually right. A single fixture reproduces that only by luck, so this sweeps prices
    and volatilities and asserts the two layers never disagree.
    """
    sizing = SizingParams.from_config(load_config())
    liq = LiquidationParams.from_config(load_config())
    checked = capped = 0

    for price in (9.37, 143.5, 1_337.0, 3_642.19, 65_000.0, 97_531.7):
        for sigma in (0.0002, 0.0009, 0.004, 0.02):
            for direction in (1, -1):
                series = candles(quiet(start=price, sigma=sigma, seed=11))
                plan = plan_position(
                    symbol="BTC_USDT", direction=direction, entry_price=price,
                    candles=series, contract=BTC, tiers=TIERS,
                    equity=EQUITY, available=EQUITY, params=sizing,
                )
                if not plan.ok:
                    continue
                checked += 1
                capped += bool(plan.stop.capped)
                verdict = assess_plan(
                    plan, TierSnapshot.of("BTC_USDT", TIERS, 0.0), 0.0,
                    params=liq, contract=BTC,
                )
                assert verdict.ok, (
                    f"the sizer accepted a plan the guard vetoed at price={price} "
                    f"sigma={sigma} dir={direction}: {verdict.stage} — {verdict.reason}"
                )

    assert checked >= 20, "the sweep must actually produce plans to be worth anything"
    assert capped, "the sweep must include stops capped at the ceiling — that is the risky case"


def test_the_guard_still_vetoes_a_stop_the_sizer_did_not_produce():
    """The previous test is only meaningful if the guard can still say no.

    A guard that accepts everything would also pass a composition sweep. This pins that the
    agreement above comes from the sizer respecting the ceiling, not from the guard having
    been loosened.
    """
    sizing = SizingParams.from_config(load_config())
    plan = plan_position(
        symbol="BTC_USDT", direction=1, entry_price=ENTRY,
        candles=candles(quiet()), contract=BTC, tiers=TIERS,
        equity=EQUITY, available=EQUITY, params=sizing,
    )
    assert plan.ok

    # Push the stop out past the liquidation ceiling the sizer would never exceed.
    hostile = plan.__class__(**{**plan.__dict__, "stop": plan.stop.__class__(
        **{**plan.stop.__dict__,
           "price": ENTRY * (1 - plan.stop.ceiling * 3),
           "distance": plan.stop.ceiling * 3},
    )})
    verdict = assess_plan(
        hostile, TierSnapshot.of("BTC_USDT", TIERS, 0.0), 0.0,
        params=LiquidationParams.from_config(load_config()), contract=BTC,
    )
    assert not verdict.ok
    assert verdict.stage


# --- signal -> sizer: the shipped filters feed the real sizer ---------------

def test_a_signal_from_the_shipped_engine_produces_a_usable_plan():
    """The engine's `direction` and the sizer's `direction` must mean the same thing.

    Phase 5 tests the engine against candles; Phase 6 tests the sizer against a direction
    someone typed. If the two ever disagreed about sign conventions, every test would still
    pass and the bot would size shorts as longs.
    """
    cfg = load_config()
    sizing = SizingParams.from_config(cfg)

    for direction in (1, -1):
        plan = plan_position(
            symbol="BTC_USDT", direction=direction, entry_price=ENTRY,
            candles=candles(quiet()), contract=BTC, tiers=TIERS,
            equity=EQUITY, available=EQUITY, params=sizing,
        )
        assert plan.ok
        assert plan.direction == direction
        assert (plan.size > 0) == (direction > 0)
        # The stop is behind the entry, on the side the direction implies.
        assert (plan.stop.price < ENTRY) == (direction > 0)
        assert plan.side == ("long" if direction > 0 else "short")


def test_the_engine_and_the_loop_agree_on_the_entry_timeframe():
    """`PaperTrader` indexes the candle map with `engine.entry_timeframe`.

    A mismatch here is a `KeyError` at the first signal — in live trading, at the worst
    possible moment. Cheap to pin, so it is pinned.
    """
    cfg = load_config()
    engine = SignalEngine(params=EngineParams.from_config(cfg))
    bot = trader()
    assert bot.engine.entry_timeframe == engine.entry_timeframe
    assert engine.entry_timeframe in bot.source.candles("BTC_USDT")


# --- risk -> sizer: the breaker's risk fraction is the one that gets spent ---

def test_the_risk_manager_and_the_sizer_spend_the_same_budget():
    """Two layers hold a risk fraction. They must be the same number.

    `RiskManager.risk_amount` is what the breakers reason about; `SizingParams.risk_amount`
    is what actually gets spent. If they drifted apart, the account would risk one figure
    and the circuit breakers would police another.
    """
    cfg = load_config()
    manager = RiskManager(RiskParams.from_config(cfg), MemoryRiskStore())
    sizing = SizingParams.from_config(cfg)

    assert manager.risk_fraction() == pytest.approx(sizing.risk_per_trade)
    assert manager.risk_amount(EQUITY) == pytest.approx(sizing.risk_amount(EQUITY))


def test_a_tripped_breaker_stops_the_stack_before_it_sizes_anything():
    """The order of the veto matters: risk is asked *before* a plan is built.

    Sizing a position the breakers would refuse is wasted work at best, and at worst it is
    the shape of a bot that logs "would have traded" and then does.
    """
    bot = trader(rally(), decide=lambda **kw: Signal(1))
    bot.risk.trip("integration test", now=NOW, breaker=Breaker.MANUAL)

    report = run(bot, steps=5)
    assert report.entries_attempted == 0
    assert report.rejections.get(Breaker.MANUAL.value)
    assert not report.trades


# --- the full paper stack ---------------------------------------------------

def test_the_whole_stack_carries_one_trade_end_to_end():
    """config -> source -> risk -> signal -> sizer -> guard -> entry -> protect -> settle.

    Every object here is the real one. The only injection is the signal, because the shipped
    filters are designed to fire rarely (§7) and a test that waits for a real score-80 setup
    would be testing the fixture, not the wiring.
    """
    bot = trader(rally(), decide=once())
    report = run(bot)

    assert report.entries_attempted == 1
    assert report.entries_filled == 1
    assert len(report.trades) == 1

    trade = report.trades[0]
    assert trade.symbol == "BTC_USDT"
    assert trade.direction == 1
    assert trade.exit_reason.startswith("tp")
    assert trade.size > 0
    # Equity reconciles exactly: the report's equity is the starting figure plus every
    # trade's net PnL, with no unexplained drift from fees booked in the wrong place.
    assert report.equity == pytest.approx(
        report.starting_equity + sum(t.net_pnl for t in report.trades)
    )


def test_a_filled_entry_is_never_left_without_a_verified_stop():
    """Invariant 1, asserted against the exchange rather than against the engine's report.

    The protection engine says it verified a stop. This checks the simulator's own book: a
    live reduce-only trigger exists for the position, on the correct side.
    """
    bot = trader(rally(), decide=once())

    async def drive():
        await bot.step()                       # the entry fills on this step
        while bot._position is None and bot.source.advance():
            await bot.step()
        assert bot._position is not None, "the fixture must actually open a position"

        live = await bot.gateway.list_price_orders("BTC_USDT")
        stops = [o for o in live
                 if str(o.get("id")) == str(bot._position["stop_order_id"])]
        assert stops, "a position exists with no live stop on the exchange"
        stop = stops[0]
        assert stop["initial"]["reduce_only"] is True
        assert float(stop["trigger"]["price"]) < bot._position["entry_price"]

    asyncio.run(drive())


def test_a_losing_round_trip_costs_no_more_than_the_risk_budget():
    """Invariant 3, measured on the account rather than on the plan.

    The sizer computes size from a risk budget; this asserts the money actually lost when
    the stop fires stays inside it, fees included. Slippage beyond the stop is the
    backtester's concern — the simulator fills triggers at the trigger price — so any excess
    here would be a sizing or fee-accounting error, not market realism.
    """
    cfg = load_config()
    bot = trader(drop(), decide=once())
    report = run(bot)

    assert report.trades, "the fixture must produce a completed losing trade"
    trade = report.trades[0]
    assert trade.net_pnl < 0

    budget = EQUITY * float(cfg.get("risk.per_trade"))
    # Fees are charged on top of the stop distance and are not part of the risk budget, so
    # the comparison allows for the round trip's cost.
    assert abs(trade.net_pnl) <= budget + abs(trade.fees) * 2


def test_the_risk_manager_hears_about_the_trade_the_loop_booked():
    """The breakers can only work on trades they are told about.

    A loop that books a loss to its own report but not to the risk manager would run past a
    daily-loss limit while every unit test still passed.
    """
    bot = trader(drop(), decide=once())
    report = run(bot)

    assert report.trades
    state = bot.risk.state
    assert state is not None
    assert state.equity == pytest.approx(report.equity)


# --- paper -> database -> dashboard: the seam nothing tested ----------------

def test_a_paper_run_stores_and_reports_through_the_real_dashboard(tmp_path):
    """Phase 10 output -> Phase 11 storage -> Phase 11 analytics, with no hand-built rows.

    Every Phase 11 test builds `TradeRecord`s by hand. That proves the store works; it does
    not prove the adapter reads what the paper loop actually writes. `main.py --stats` runs
    exactly this path.
    """
    cfg = load_config()
    bot = trader(rally(), decide=once())
    report = run(bot)
    assert report.trades

    store = TradeStore(tmp_path / "trades.db")
    leverage = int(cfg.get("leverage.default"))
    for trade in report.trades:
        store.record_trade(TradeRecord.from_paper(trade, leverage=leverage, mode="paper"))
    store.record_equity(EquityPoint(timestamp=0.0, equity=report.starting_equity))
    store.record_equity(EquityPoint(timestamp=1.0, equity=report.equity))

    stored = store.trades()
    assert len(stored) == len(report.trades)

    # The reasoning survives the round trip, not just the PnL.
    first, source = stored[0], report.trades[0]
    assert first.symbol == source.symbol
    assert first.side == "long"
    assert first.exit_reason == source.exit_reason
    assert first.signal_score == pytest.approx(source.score)
    assert first.pnl == pytest.approx(source.net_pnl)
    assert first.leverage == leverage

    rendered = Dashboard(store, starting_equity=report.starting_equity).render()
    assert "net pnl" in rendered
    assert f"trades                    : {len(report.trades)}" in rendered
    # Scoping to the symbol names it in the header and keeps the same trade count.
    scoped = Dashboard(store, starting_equity=report.starting_equity).render(symbol="BTC_USDT")
    assert "BTC_USDT" in scoped

    perf = compute(stored, store.equity_curve(), starting_equity=report.starting_equity)
    assert perf.trades == len(report.trades)
    assert perf.net_pnl == pytest.approx(sum(t.net_pnl for t in report.trades))


def test_the_stored_pnl_and_the_stored_equity_curve_tell_the_same_story(tmp_path):
    """Two independent records of the same run must not contradict each other."""
    bot = trader(rally(), decide=once())
    report = run(bot)
    assert report.trades

    store = TradeStore(tmp_path / "trades.db")
    equity = report.starting_equity
    store.record_equity(EquityPoint(timestamp=0.0, equity=equity))
    for index, trade in enumerate(report.trades, start=1):
        store.record_trade(TradeRecord.from_paper(trade, leverage=100, mode="paper"))
        equity = trade.equity_after
        store.record_equity(EquityPoint(timestamp=float(index), equity=equity))

    perf = compute(store.trades(), store.equity_curve(),
                   starting_equity=report.starting_equity)
    assert perf.net_pnl == pytest.approx(report.net_pnl)
    assert store.equity_curve()[-1].equity == pytest.approx(report.equity)


def test_a_backtest_and_a_paper_trade_land_in_one_comparable_table(tmp_path):
    """`from_paper` adapts both shapes. If it did not, history would silently split.

    Phase 11 claims paper and backtest history are comparable in one table. This runs both
    engines and puts their real output side by side.
    """
    paper = run(trader(rally(), decide=once()))
    assert paper.trades

    engine = BacktestEngine(BacktestParams(), decide=once())
    result = engine.run("BTC_USDT", {"1m": candles(rally())}, TIERS, BTC, warmup=WARMUP)
    assert result.trades, "the backtest fixture must produce a trade to compare"

    store = TradeStore(tmp_path / "trades.db")
    store.record_trades(
        [TradeRecord.from_paper(t, leverage=100, mode="paper") for t in paper.trades]
        + [TradeRecord.from_paper(t, leverage=100, mode="backtest") for t in result.trades]
    )

    rows = store.trades()
    assert {row.mode for row in rows} == {"paper", "backtest"}
    for row in rows:
        assert row.symbol == "BTC_USDT"
        assert row.side in ("long", "short")
        assert row.entry_price > 0
        assert row.exit_price > 0
        assert row.stop_loss > 0


# --- the kill switch survives the process, through the real store -----------

def test_a_tripped_breaker_still_blocks_a_freshly_built_stack(tmp_path):
    """Invariant 5, end to end: trip, discard everything, rebuild from the file.

    Phase 6 tests this against `SqliteRiskStore` directly. This rebuilds the *loop* — the
    object that would actually trade — and asserts it refuses.
    """
    path = tmp_path / "trades.db"
    doomed = RiskManager(RiskParams.from_config(load_config()), SqliteRiskStore(path))
    doomed.observe_equity(NOW, EQUITY)
    doomed.trip("daily loss", now=NOW, breaker=Breaker.DAILY_LOSS)
    del doomed

    bot = trader(rally(), decide=lambda **kw: Signal(1), t0=NOW,
                 risk_store=SqliteRiskStore(path))
    assert bot.risk.tripped

    report = run(bot, steps=5)
    assert report.entries_attempted == 0
    assert report.rejections.get(Breaker.DAILY_LOSS.value)


def test_a_loop_whose_clock_predates_its_persisted_state_refuses_to_trade(tmp_path):
    """The seam between a persisted trip time and the loop's own clock.

    Found while writing the test above: a loop replaying bars stamped before the state on
    disk is, from the risk manager's point of view, a clock that jumped backwards, and
    Phase 6 answers that with `unknown_state` rather than a guess. Worth pinning — the
    fail-closed direction is the safe one and a future refactor could quietly invert it
    into "assume the file is stale and trade anyway".
    """
    path = tmp_path / "trades.db"
    seeded = RiskManager(RiskParams.from_config(load_config()), SqliteRiskStore(path))
    seeded.observe_equity(NOW, EQUITY)
    del seeded

    bot = trader(rally(), decide=lambda **kw: Signal(1), t0=0.0,
                 risk_store=SqliteRiskStore(path))
    report = run(bot, steps=5)

    assert report.entries_attempted == 0
    assert report.rejections.get(Breaker.UNKNOWN_STATE.value) == 5
    assert not report.trades


def test_the_trade_history_and_the_kill_switches_share_one_file(tmp_path):
    """Phase 11's store and Phase 6's store write the same database.

    Two files can disagree about whether the bot is halted. The tables are created by
    different modules with `IF NOT EXISTS`, so this pins that neither wipes the other and
    that the dashboard can read a breaker the risk manager tripped.
    """
    path = tmp_path / "trades.db"
    manager = RiskManager(RiskParams.from_config(load_config()), SqliteRiskStore(path))
    manager.observe_equity(NOW, EQUITY)
    manager.trip("drawdown", now=NOW, breaker=Breaker.DRAWDOWN)

    store = TradeStore(path)
    store.record_trade(TradeRecord(
        timestamp=1.0, symbol="BTC_USDT", side="long", leverage=100,
        entry_price=ENTRY, exit_price=ENTRY * 1.01, size=10, margin=6.5,
        stop_loss=ENTRY * 0.997, pnl=5.0, equity_after=EQUITY + 5.0,
    ))

    assert Breaker.DRAWDOWN.value in store.kill_switches()
    assert store.count() == 1
    # And the breaker survives the store having opened the same file.
    reloaded = RiskManager(RiskParams.from_config(load_config()), SqliteRiskStore(path))
    assert reloaded.tripped


# --- execution <-> guard: the post-fill check uses the exchange's numbers ---

def test_the_guard_verifies_the_fill_the_execution_layer_reports():
    """Phase 7's `verify_fill` takes an exchange `liq_price`; Phase 8's simulator writes one.

    The two have never been introduced. The simulator computes its liquidation price from
    the Phase 7 formula, so a sign-convention or field-name drift between them would leave
    the post-fill check verifying a number nobody produced.
    """
    cfg = load_config()
    sizing = SizingParams.from_config(cfg)
    plan = plan_position(
        symbol="BTC_USDT", direction=1, entry_price=ENTRY,
        candles=candles(quiet()), contract=BTC, tiers=TIERS,
        equity=EQUITY, available=EQUITY, params=sizing,
    )
    assert plan.ok

    gateway = SimulatedGateway(last_price=ENTRY, leverage=int(cfg.get("leverage.default")))
    manager = OrderManager(gateway, ExecutionParams(), live=False)

    async def go():
        record = await manager.submit_entry(
            "BTC_USDT", plan.size, None, 1, tif="ioc",     # market fill, for determinism
        )
        assert record.state is OrderState.FILLED
        return await gateway.get_position("BTC_USDT")

    position = asyncio.run(go())
    liq_price = float(position["liq_price"])
    assert liq_price > 0

    verdict = verify_fill(
        "BTC_USDT", plan.direction, float(position["entry_price"]), plan.stop.price,
        liq_price, 0.0, params=LiquidationParams.from_config(cfg),
    )
    assert verdict.ok, f"the guard rejected the simulator's own fill: {verdict.reason}"
    # The liquidation price is below a long's stop, which is the whole point of the check.
    assert liq_price < plan.stop.price


def test_the_guard_catches_a_fill_whose_liquidation_moved_against_us():
    """The previous test only proves agreement if disagreement is still detectable."""
    cfg = load_config()
    plan = plan_position(
        symbol="BTC_USDT", direction=1, entry_price=ENTRY,
        candles=candles(quiet()), contract=BTC, tiers=TIERS,
        equity=EQUITY, available=EQUITY, params=SizingParams.from_config(cfg),
    )
    assert plan.ok

    # The exchange reports a liquidation price *above* the stop: the stop would never fire.
    verdict = verify_fill(
        "BTC_USDT", 1, ENTRY, plan.stop.price, plan.stop.price * 1.0005, 0.0,
        params=LiquidationParams.from_config(cfg),
    )
    assert not verdict.ok
    assert verdict.stage
    assert verdict.action == "flatten"


# --- protection <-> order manager ------------------------------------------

def test_the_ladder_the_protection_engine_places_never_over_closes(tmp_path):
    """TP sizes plus the stop must not exceed the position.

    Phase 8 asserts the ladder's arithmetic. This asserts what the *exchange* ends up
    holding after the real engine places it, which is where an off-by-one would show up.
    """
    cfg = load_config()
    gateway = SimulatedGateway(last_price=ENTRY, leverage=100)
    manager = OrderManager(gateway, ExecutionParams(), live=False)
    engine = ProtectionEngine(manager, ProtectionParams.from_config(cfg))
    size = 500

    async def go():
        await manager.submit_entry("BTC_USDT", size, None, 1, tif="ioc")
        return await engine.protect("BTC_USDT", 1, ENTRY, ENTRY * 0.997, size, 1)

    result = asyncio.run(go())
    assert result.ok and result.verified

    tp_size = sum(abs(leg.size) for leg in result.take_profits)
    assert tp_size == size, "the ladder must close exactly the position, no more, no less"

    async def stop_size():
        live = await gateway.list_price_orders("BTC_USDT")
        return [o for o in live if str(o.get("id")) == result.stop_order_id][0]

    stop = asyncio.run(stop_size())
    assert abs(int(stop["initial"]["size"])) == size
    assert stop["initial"]["reduce_only"] is True
