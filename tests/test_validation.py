"""PHASE 13 tests — paper-trading validation, and the seam defect it uncovered.

Two things are under test here, and the first is why the second exists.

**The seam.** `TradeRecord.from_paper` reads every field off the trade object with a
default, so a field the producer does not define becomes `0.0` in the database without an
exception anywhere. `PaperTrade` did not define `r_multiple`. Since
`monitoring.dashboard.compute` averages that column into `expectancy_r`, every paper run
stored zero expectancy — and `Performance.verdict()` requires `expectancy_r > 0` for a
positive verdict, so a profitable paper run long enough to be graded could only ever have
been reported as NEGATIVE. `margin` and `liquidation_price` were lost the same way. The
tests below pin the fix against reversion: each asserts a *nonzero* stored value derived
from a real run, which is exactly what the pre-fix code could not produce.

**The validation.** The verdict has to be able to say no, so most of these tests drive a
criterion to FAIL rather than to PASS. Two properties matter more than the individual
checks: `INSUFFICIENT` never counts as a pass, and a criterion that cannot be evaluated is
withheld rather than assumed — which is why reading stored history alone can never reach
VALIDATED.

Offline and deterministic, like every other module here: seeded series, `SimulatedGateway`,
`tmp_path` databases, and the autouse socket guard in `conftest.py` behind all of it.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from config import ConfigError, load_config
from database.models import EquityPoint, TradeRecord, TradeStore
from exchange.gate_client import Contract, RiskTier
from monitoring.dashboard import compute
from paper.loop import PaperTrader, ReplayMarketSource
from paper.validation import (
    CheckStatus,
    SessionEvidence,
    ValidationParams,
    record_session,
    run_session,
    validate,
)
from strategy.indicators import Candles

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
NOW = 1_754_784_000.0


def candles(close, *, interval=60.0, wick=0.0008):
    close = np.asarray(close, dtype=float)
    n = len(close)
    open_ = np.concatenate([[close[0]], close[:-1]])
    return Candles(
        time=np.arange(n, dtype=float) * interval,
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
    """Minimal stand-in for a Phase 5 Signal, as in `test_paper.py`."""

    def __init__(self, direction=1, score=88.0):
        self.direction = direction
        self.score = score
        self.accepted = True
        self.stage = "accepted"


def once(direction=1, at=1):
    state = {"n": 0}

    def decide(**kwargs):
        state["n"] += 1
        return Signal(direction) if state["n"] == at else None

    return decide


def trader(path=None, *, decide=None, config=None, start=WARMUP):
    series = candles(quiet() if path is None else path)
    source = ReplayMarketSource({"1m": series}, "1m", start=start)
    return PaperTrader(
        config or load_config(), source, "BTC_USDT", TIERS, BTC,
        decide=decide if decide is not None else once(),
    )


def one_trade(path=None, **kwargs):
    """Run a session that takes exactly one round trip. Returns (evidence, trade)."""
    bot = trader(path if path is not None else rally(), **kwargs)
    evidence = asyncio.run(run_session(bot))
    assert evidence.trades, "fixture produced no trade"
    return evidence, evidence.trades[0]


def record(**overrides):
    fields = dict(
        timestamp=NOW, symbol="BTC_USDT", side="long", leverage=100,
        entry_price=65_000.0, exit_price=65_650.0, size=100, margin=6.5,
        stop_loss=64_789.0, fees=1.2, pnl=48.8, pnl_percent=0.00488,
        r_multiple=1.9, signal_score=88.0, market_regime="TRENDING",
        exit_reason="tp2", equity_after=10_048.8, mode="paper",
    )
    fields.update(overrides)
    return TradeRecord(**fields)


def evidence(**overrides):
    """Clean observed evidence — every conduct check passes unless overridden."""
    fields = dict(
        symbol="BTC_USDT", steps=400, starting_equity=10_000.0, equity=10_048.8,
        simulated=True, live_gate_open=False, entries_attempted=3, entries_filled=1,
        entries_expired=2, open_at_end=False, observed=True,
    )
    fields.update(overrides)
    return SessionEvidence(**fields)


def statuses(report):
    return {check.name: check.status for check in report.checks}


# --- the seam defect, pinned ------------------------------------------------

def test_a_paper_trade_reports_its_own_r_multiple():
    """The defect: `PaperTrade` had no `r_multiple`, so the adapter stored 0.0.

    Asserted against the definition Phase 9 uses, including the quanto multiplier — the
    factor whose omission is why the adapter cannot derive this itself.
    """
    _, trade = one_trade()

    risk = abs(trade.entry_price - trade.stop_price) * abs(trade.size) \
        * BTC.quanto_multiplier
    assert risk > 0
    assert trade.r_multiple == pytest.approx(trade.net_pnl / risk, rel=1e-9)
    assert trade.r_multiple != 0.0


def test_the_stored_record_keeps_the_r_multiple_margin_and_liquidation_price():
    """All three were silently zeroed by the adapter's `getattr` defaults."""
    _, trade = one_trade()
    stored = TradeRecord.from_paper(trade, leverage=100)

    assert stored.r_multiple == pytest.approx(trade.r_multiple)
    assert stored.r_multiple != 0.0
    assert stored.margin == pytest.approx(trade.margin)
    assert stored.margin > 0.0
    assert stored.liquidation_price == pytest.approx(trade.liquidation_price)
    assert stored.liquidation_price > 0.0


def test_a_winning_paper_run_does_not_report_zero_expectancy():
    """The consequence that made the defect matter.

    `expectancy_r` is the mean of the stored `r_multiple` column, and
    `Performance.verdict()` needs it above zero to call an edge positive. With the field
    missing, a profitable run reported +0.000R and would have been graded NEGATIVE.
    """
    _, trade = one_trade()
    assert trade.net_pnl > 0

    performance = compute([TradeRecord.from_paper(trade, leverage=100)],
                          min_trades_for_verdict=1)
    assert performance.expectancy_r > 0
    assert "NEGATIVE" not in performance.verdict()


def test_the_adapter_does_not_invent_an_r_multiple_it_cannot_compute():
    """A trade object with no `r_multiple` still stores 0.0 — deliberately.

    The denominator needs the contract's quanto multiplier, which no trade object carries.
    Guessing from `size` alone would be wrong by that multiplier, so the adapter reports
    zero rather than a plausible fiction, and the producer owns the number.
    """
    class Bare:
        symbol = "BTC_USDT"
        direction = 1
        entry_time = NOW
        exit_time = NOW + 60
        entry_price = 65_000.0
        exit_price = 65_200.0
        size = 1_000
        stop_price = 64_789.0
        exit_reason = "tp1"
        fees = 1.0
        net_pnl = 19.0
        equity_after = 10_019.0

    assert TradeRecord.from_paper(Bare(), leverage=100).r_multiple == 0.0


def test_an_explicit_margin_argument_still_wins_over_the_trade():
    """The parameter predates the field; callers passing it must keep working."""
    _, trade = one_trade()
    assert TradeRecord.from_paper(trade, leverage=100, margin=99.5).margin == 99.5


# --- conduct: the checks that must be able to fail --------------------------

def test_a_clean_run_passes_every_conduct_check():
    report = validate(evidence(), [record()], [EquityPoint(NOW, 10_048.8)])
    assert report.conduct_ok
    for check in report.conduct:
        assert check.status is CheckStatus.PASS, f"{check.name}: {check.detail}"


def test_an_open_safety_gate_fails_validation_outright():
    """A paper result gathered with live trading enabled is not a paper result."""
    report = validate(evidence(live_gate_open=True), [record()])
    assert statuses(report)["simulation_only"] is CheckStatus.FAIL
    assert not report.conduct_ok
    assert not report.validated
    assert "did not behave correctly" in report.verdict()


def test_a_non_simulated_gateway_fails_validation():
    report = validate(evidence(simulated=False), [record()])
    assert statuses(report)["simulation_only"] is CheckStatus.FAIL
    assert not report.validated


def test_an_unprotected_position_fails_validation():
    """Invariant 1. One unprotected carry is enough."""
    report = validate(evidence(unprotected=1, protection_failures=1), [record()])
    assert statuses(report)["stop_on_every_position"] is CheckStatus.FAIL
    assert not report.validated


def test_a_flattened_entry_is_not_counted_as_unprotected():
    """The engine market-closing an unprotectable entry is the design working."""
    report = validate(evidence(protection_failures=1, flattened=1), [record()])
    assert statuses(report)["stop_on_every_position"] is CheckStatus.PASS


def test_a_liquidation_fails_validation():
    """Invariant 2: liquidation is never used as a stop."""
    report = validate(evidence(), [record(exit_reason="liquidation")])
    assert statuses(report)["no_liquidation"] is CheckStatus.FAIL
    assert not report.validated


def test_a_loss_beyond_the_budget_fails_validation():
    """Invariant 3: the loss that arrives must be the loss that was sized."""
    report = validate(evidence(), [record(pnl=-500.0, r_multiple=-4.0)])
    assert statuses(report)["loss_within_budget"] is CheckStatus.FAIL
    assert not report.validated


def test_a_normal_stop_out_is_within_the_budget():
    report = validate(evidence(), [record(pnl=-25.0, r_multiple=-1.0)])
    check = next(c for c in report.checks if c.name == "loss_within_budget")
    assert check.status is CheckStatus.PASS
    assert "lost 1.00R" in check.detail


def test_an_all_winning_run_is_not_described_as_having_lost():
    """`min()` over R returns the smallest *win* when nothing lost.

    Reporting that as "worst trade lost 1.62R" turns a profit into a loss in the one
    report whose whole purpose is not to misstate what happened.
    """
    report = validate(evidence(), [record(pnl=48.8, r_multiple=1.62)])
    check = next(c for c in report.checks if c.name == "loss_within_budget")
    assert check.status is CheckStatus.PASS
    assert "lost" not in check.detail.replace("no graded trade lost anything", "")
    assert "+1.62R" in check.detail


def test_the_reported_loss_magnitude_is_not_signed_twice():
    """A 4R loss is "lost 4.00R", never "lost -4.00R"."""
    report = validate(evidence(), [record(pnl=-500.0, r_multiple=-4.0)])
    check = next(c for c in report.checks if c.name == "loss_within_budget")
    assert check.status is CheckStatus.FAIL
    assert "lost 4.00R" in check.detail


def test_trades_with_no_r_multiple_fail_rather_than_pass_the_loss_check():
    """The seam defect's second consequence: an unmeasurable invariant is a failed one.

    Before the fix every stored paper trade had `r_multiple == 0.0`, which would have let
    the loss budget read as satisfied because nothing appeared to have lost anything.
    """
    report = validate(evidence(), [record(pnl=-500.0, r_multiple=0.0)])
    check = next(c for c in report.checks if c.name == "loss_within_budget")
    assert check.status is CheckStatus.FAIL
    assert "cannot be checked" in check.detail


def test_a_ledger_that_disagrees_with_the_account_fails_validation():
    """The audit trail has to describe the account it claims to describe."""
    report = validate(evidence(equity=10_500.0), [record(pnl=48.8)])
    assert statuses(report)["ledger_reconciles"] is CheckStatus.FAIL
    assert not report.validated


def test_a_stop_on_the_wrong_side_of_entry_fails_validation():
    """The Phase 12 defect class, checked against what was stored."""
    report = validate(evidence(), [record(stop_loss=65_500.0)])
    assert statuses(report)["stop_recorded"] is CheckStatus.FAIL
    assert not report.validated


def test_a_trade_stored_without_a_stop_fails_validation():
    report = validate(evidence(), [record(stop_loss=0.0)])
    assert statuses(report)["stop_recorded"] is CheckStatus.FAIL


def test_a_run_that_ends_holding_a_position_fails_validation():
    report = validate(evidence(open_at_end=True), [record()])
    assert statuses(report)["flat_at_end"] is CheckStatus.FAIL
    assert not report.validated


# --- evidence: withheld is not passed ---------------------------------------

def test_a_small_sample_is_withheld_rather_than_passed():
    """The same refusal Phase 9 and Phase 11 make, on the same threshold."""
    report = validate(evidence(), [record()], [EquityPoint(NOW, 10_048.8)])
    assert report.conduct_ok                       # the machine behaved
    assert not report.validated                    # but nothing is claimed
    assert statuses(report)["sample_size"] is CheckStatus.INSUFFICIENT
    assert statuses(report)["edge_not_from_leverage"] is CheckStatus.INSUFFICIENT
    assert "WITHHELD" in report.verdict()
    assert "not passed" in report.verdict()


def test_the_sample_threshold_is_read_from_the_backtest_key():
    """One threshold, three phases. They must not drift apart."""
    cfg = load_config()
    assert ValidationParams.from_config(cfg).min_trades == \
        cfg.get("backtest.min_trades_for_verdict")


def test_a_run_that_filled_nothing_validates_nothing():
    """Every conduct check passes vacuously on silence, so silence must not read as pass.

    A run that filled nothing also ends on the equity it started with — the point of the
    check is that clean conduct over an empty run still validates nothing.
    """
    report = validate(
        evidence(entries_filled=0, entries_attempted=4, equity=10_000.0), []
    )
    assert report.conduct_ok
    assert not report.validated
    assert statuses(report)["exercised"] is CheckStatus.INSUFFICIENT


def test_a_negative_expectancy_fails_once_the_sample_is_large_enough():
    """R is per unit of risk, so a negative expectancy is not fixable with leverage."""
    trades = [record(pnl=-25.0, r_multiple=-0.4) for _ in range(5)]
    report = validate(evidence(equity=10_000.0 - 125.0), trades,
                      [EquityPoint(NOW, 9_875.0)],
                      params=ValidationParams(min_trades=5))
    assert statuses(report)["edge_not_from_leverage"] is CheckStatus.FAIL
    assert not report.validated
    assert "argues against the strategy" in report.verdict()


def test_a_positive_expectancy_on_a_sufficient_sample_validates():
    """The one path that reaches VALIDATED — and it still is not authorisation."""
    trades = [record(pnl=48.8, r_multiple=1.9) for _ in range(3)]
    curve = [EquityPoint(NOW + i, 10_000.0 + 48.8 * i) for i in range(4)]
    report = validate(evidence(equity=10_000.0 + 48.8 * 3), trades, curve,
                      params=ValidationParams(min_trades=3))

    assert report.validated
    assert "VALIDATED" in report.verdict()
    assert "not authorisation" in report.verdict()


def test_a_drawdown_past_the_account_limit_fails_validation():
    curve = [EquityPoint(NOW, 10_000.0), EquityPoint(NOW + 1, 9_000.0),
             EquityPoint(NOW + 2, 10_048.8)]
    report = validate(evidence(), [record()], curve,
                      params=ValidationParams(min_trades=1))
    assert statuses(report)["drawdown_within_limit"] is CheckStatus.FAIL
    assert not report.validated


def test_a_missing_equity_curve_is_withheld_not_passed():
    """With no curve, drawdown computes as 0.00% — the most flattering possible lie."""
    report = validate(evidence(), [record()], [])
    assert statuses(report)["drawdown_within_limit"] is CheckStatus.INSUFFICIENT
    assert not report.validated


# --- fail-closed --------------------------------------------------------------

def test_an_empty_report_is_not_validated():
    """`all()` over nothing is True; the verdict must not inherit that."""
    from paper.validation import ValidationReport

    empty = ValidationReport(checks=(), performance=compute([]),
                             params=ValidationParams(), evidence=evidence())
    assert not empty.validated


def test_conduct_failures_are_reported_before_strategy_ones():
    """A misbehaving machine is a defect to fix, not a strategy result to interpret."""
    trades = [record(pnl=-25.0, r_multiple=-0.4) for _ in range(3)]
    report = validate(evidence(unprotected=1, equity=10_000.0 - 75.0), trades,
                      [EquityPoint(NOW, 9_925.0)],
                      params=ValidationParams(min_trades=3))
    assert "did not behave correctly" in report.verdict()
    assert "argues against" not in report.verdict()


def test_the_params_refuse_a_loss_tolerance_below_one_r():
    with pytest.raises(ValueError, match="max_loss_r"):
        ValidationParams(max_loss_r=0.5)


def test_the_config_refuses_a_loss_tolerance_below_one_r(monkeypatch, tmp_path):
    """Fail-closed at load, like every other threshold in this repo."""
    import copy

    import yaml

    import config as config_module

    with config_module.CONFIG_PATH.open(encoding="utf-8") as handle:
        raw = copy.deepcopy(yaml.safe_load(handle))
    raw["validation"]["max_loss_r"] = 0.5
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_PATH", path)

    with pytest.raises(ConfigError, match="max_loss_r"):
        load_config()


def test_the_shipped_config_carries_a_usable_loss_tolerance():
    assert ValidationParams.from_config(load_config()).max_loss_r >= 1.0


# --- recording ----------------------------------------------------------------

def test_a_session_persists_its_trades_and_its_equity_curve(tmp_path):
    """The Phase 10 -> 11 handoff, wired in production code rather than in a test."""
    store = TradeStore(tmp_path / "trades.db")
    bot = trader(rally())
    result = asyncio.run(run_session(bot, store=store, leverage=100))

    assert store.count() == len(result.trades) == 1
    stored = store.trades()[0]
    assert stored.mode == "paper"
    assert stored.r_multiple != 0.0
    assert store.equity_curve(), "no equity curve was recorded"


def test_the_recorded_curve_is_marked_to_market_not_realised_only():
    """At 100x the drawdown that matters arrives before any fill.

    A curve sampled from realised equity alone cannot show it, so the sampler uses
    mark-to-market and the two figures must actually differ while a position is open.
    """
    bot = trader(drop())

    async def scenario():
        marked = []
        while True:
            await bot.step()
            if bot.open_position is not None:
                mark = bot.source.mark_price(bot.symbol)
                marked.append((bot.report.equity, bot.mark_to_market(mark)))
            if not bot.source.advance():
                break
        return marked

    marked = asyncio.run(scenario())
    assert marked, "the fixture never opened a position"
    assert any(realised != marked_to_market for realised, marked_to_market in marked), (
        "mark_to_market never differed from realised equity while a position was open"
    )


def test_the_curve_is_flat_when_no_position_is_open():
    bot = trader(quiet(), decide=lambda **kwargs: None)
    asyncio.run(run_session(bot))
    assert bot.unrealised_pnl(ENTRY) == 0.0
    assert bot.mark_to_market(ENTRY) == bot.report.equity


def test_a_snapshot_stride_thins_the_curve_without_losing_the_trades(tmp_path):
    store = TradeStore(tmp_path / "trades.db")
    bot = trader(rally())
    result = asyncio.run(run_session(bot, store=store, snapshot_stride=10))

    assert len(result.curve) <= result.steps
    assert store.count() == len(result.trades)


def test_a_zero_snapshot_stride_is_refused():
    with pytest.raises(ValueError, match="snapshot_stride"):
        asyncio.run(run_session(trader(), snapshot_stride=0))


def test_record_session_is_the_only_writer_needed_for_stats(tmp_path):
    """`--stats` reads this table; validation must fill it the same way."""
    store = TradeStore(tmp_path / "trades.db")
    bot = trader(rally())
    result = asyncio.run(run_session(bot))
    assert store.count() == 0                       # nothing written without a store

    assert record_session(store, result, leverage=100) == len(result.trades)
    assert store.count() == len(result.trades)


# --- reading history back -----------------------------------------------------

def test_stored_history_withholds_the_checks_a_record_cannot_answer(tmp_path):
    """Outcomes are recorded; events are not. Reading history cannot prove conduct."""
    store = TradeStore(tmp_path / "trades.db")
    store.record_trade(record())
    store.record_equity(EquityPoint(NOW, 10_048.8))

    report = validate(SessionEvidence.from_store(store), store.trades(),
                      store.equity_curve(), params=ValidationParams(min_trades=1))
    found = statuses(report)
    for withheld in ("stop_on_every_position", "ledger_reconciles", "flat_at_end"):
        assert found[withheld] is CheckStatus.INSUFFICIENT, withheld
    assert not report.validated


def test_stored_history_still_checks_what_the_records_do_prove(tmp_path):
    """The durable half of invariant 1 outlives the run that established it."""
    store = TradeStore(tmp_path / "trades.db")
    store.record_trade(record(stop_loss=65_500.0))

    report = validate(SessionEvidence.from_store(store), store.trades())
    assert statuses(report)["stop_recorded"] is CheckStatus.FAIL


def test_a_live_mode_row_makes_stored_history_fail_simulation_only(tmp_path):
    """A live trade in a database being read as paper validation is itself the finding."""
    store = TradeStore(tmp_path / "trades.db")
    store.record_trade(record(mode="live"))
    store.record_trade(record(mode="paper"))

    report = validate(SessionEvidence.from_store(store), store.trades(mode="paper"))
    assert statuses(report)["simulation_only"] is CheckStatus.FAIL


def test_a_round_trip_through_the_database_preserves_the_r_multiple(tmp_path):
    """The column has to survive SQLite, not just the adapter."""
    store = TradeStore(tmp_path / "trades.db")
    bot = trader(rally())
    result = asyncio.run(run_session(bot, store=store, leverage=100))

    assert store.trades()[0].r_multiple == pytest.approx(result.trades[0].r_multiple)


# --- it still cannot trade ----------------------------------------------------

def test_validation_never_opens_the_safety_gate():
    """The report is an input to Phase 14, not a switch. It must own no way to flip one."""
    import paper.validation as module

    source = (module.__file__ and open(module.__file__, encoding="utf-8").read()) or ""
    for forbidden in ("live_enabled =", "DRY_RUN", "confirm_live =", "place_order",
                      "submit_entry", "os.environ"):
        assert forbidden not in source, f"paper/validation.py contains {forbidden!r}"


def test_the_report_renders_losses_and_refusals_visibly():
    report = validate(evidence(), [record(pnl=-500.0, r_multiple=-4.0)])
    rendered = report.render()
    assert "FAIL" in rendered
    assert "loss_within_budget" in rendered
    assert "verdict:" in rendered
