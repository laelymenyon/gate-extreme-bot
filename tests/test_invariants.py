"""PHASE 12 — the six core invariants, swept rather than sampled.

README §"Core invariants" lists six properties this bot is built around. Each is already
tested somewhere, once, on the fixture that made it convenient to test. That is enough to
show the property *can* hold. It is not enough to show it *does* hold across the range of
prices, volatilities, directions and account sizes the bot will actually meet — and this
repo has already shipped a defect that only appeared at certain decimal places
(``bd7977c``), plus a second one found while writing these tests, where a stop rounded onto
the far side of its own entry at a price that no existing fixture used.

So each invariant here is driven over a deterministic grid. No randomness beyond seeded
generators, no wall clock, no network: the same inputs on every run, so a failure is a
reproducible fact rather than a flake to be re-run.

The grid is deliberately hostile — prices spanning five orders of magnitude, volatility
from dead to violent, both directions, accounts from nearly-too-small to large — because
the interesting failures live at the edges, and the edges are exactly what a single
hand-written fixture never visits.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from config import ConfigError, load_config
from exchange.gate_client import Contract, RiskTier
from execution.protection import (
    ProtectionParams,
    breakeven_price,
    ratchet,
    take_profit_ladder,
)
from risk.liquidation_guard import LiquidationParams, TierSnapshot, assess_plan
from risk.position_sizer import SizingParams, plan_position
from risk.risk_manager import Breaker, MemoryRiskStore, RiskManager, RiskParams
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

NOW = 1_754_784_000.0

#: The hostile grid. Prices span five orders of magnitude; sigma spans dead to violent.
PRICES = (0.4137, 9.37, 143.5, 3_642.19, 65_000.0, 97_531.7)
SIGMAS = (0.00005, 0.0003, 0.002, 0.015)
EQUITIES = (250.0, 1_000.0, 10_000.0, 250_000.0)


def candles(close, *, wick=0.0008):
    close = np.asarray(close, dtype=float)
    n = len(close)
    open_ = np.concatenate([[close[0]], close[:-1]])
    return Candles(
        time=np.arange(n, dtype=float) * 60.0,
        open=open_,
        high=np.maximum(open_, close) * (1 + wick),
        low=np.minimum(open_, close) * (1 - wick),
        close=close,
        volume=np.full(n, 1000.0),
    )


def series(price, sigma, seed=7, n=320):
    rng = np.random.default_rng(seed)
    return candles(price * np.exp(np.cumsum(rng.normal(0, sigma, n))))


def plans(params=None):
    """Every accepted plan on the grid, with the inputs that produced it."""
    params = params or SizingParams.from_config(load_config())
    for price in PRICES:
        for sigma in SIGMAS:
            for direction in (1, -1):
                for equity in EQUITIES:
                    plan = plan_position(
                        symbol="BTC_USDT", direction=direction, entry_price=price,
                        candles=series(price, sigma), contract=BTC, tiers=TIERS,
                        equity=equity, available=equity, params=params,
                    )
                    if plan.ok:
                        yield plan, price, sigma, direction, equity


def test_the_sweep_is_not_empty():
    """Guards every sweep below: a grid that produces no plans proves nothing."""
    produced = list(plans())
    assert len(produced) >= 40, f"only {len(produced)} plans on the grid"
    assert {p.direction for p, *_ in produced} == {1, -1}
    assert len({price for _, price, *_ in produced}) >= 3


# --- invariant 3: size comes from risk, never from leverage ----------------

def test_no_accepted_plan_ever_risks_more_than_the_budget():
    """`size = (equity × risk) / stop_distance`, floored — never rounded up.

    The single most important arithmetic property in the repo: it is what makes 100x
    leverage a margin choice rather than a risk choice.
    """
    for plan, price, sigma, direction, equity in plans():
        assert plan.max_loss <= plan.risk_amount + 1e-9, (
            f"plan risks {plan.max_loss} of a {plan.risk_amount} budget "
            f"at price={price} sigma={sigma} dir={direction} equity={equity}"
        )
        # And the budget is the configured fraction of *this* equity, not a stale one.
        assert plan.risk_amount == pytest.approx(equity * 0.0025)


def test_the_realised_loss_matches_the_stop_that_will_actually_be_placed():
    """Sizing on the unrounded ideal stop would risk slightly more than the budget."""
    for plan, price, sigma, direction, _ in plans():
        realised = abs(plan.size) * plan.coin_amount / abs(plan.size) * abs(
            plan.entry_price - plan.stop.price
        )
        assert realised == pytest.approx(plan.max_loss, rel=1e-9), (
            f"at price={price} sigma={sigma} dir={direction}"
        )


def test_leverage_moves_margin_and_leaves_risk_alone():
    """The claim that justifies 100x. If it ever failed, the whole design is unsound."""
    base = SizingParams.from_config(load_config())
    for price in PRICES:
        for direction in (1, -1):
            sized = {}
            for leverage in (50, 100):
                params = SizingParams(**{**base.__dict__, "leverage": leverage})
                plan = plan_position(
                    symbol="BTC_USDT", direction=direction, entry_price=price,
                    candles=series(price, 0.0003), contract=BTC, tiers=TIERS,
                    equity=10_000.0, available=10_000.0, params=params,
                )
                if plan.ok:
                    sized[leverage] = plan
            if len(sized) < 2:
                continue
            low, high = sized[50], sized[100]
            assert low.max_loss == pytest.approx(high.max_loss, rel=1e-6), (
                f"leverage changed the risk at price={price} dir={direction}"
            )
            assert high.margin < low.margin, "more leverage must lock less margin"


# --- invariant 2: liquidation is never used as a stop ----------------------

def test_no_accepted_plan_puts_liquidation_between_entry_and_the_stop():
    """Every plan the sizer accepts must clear the guard's buffer.

    This is the sizer→guard seam from `test_integration.py`, widened across the account
    sizes too: a small account changes the size, the size changes the tier, and the tier
    changes the maintenance rate the ceiling is derived from.
    """
    liq = LiquidationParams.from_config(load_config())
    checked = 0
    for plan, price, sigma, direction, equity in plans():
        verdict = assess_plan(
            plan, TierSnapshot.of("BTC_USDT", TIERS, NOW), NOW,
            params=liq, contract=BTC,
        )
        assert verdict.ok, (
            f"the guard vetoed an accepted plan at price={price} sigma={sigma} "
            f"dir={direction} equity={equity}: {verdict.stage} — {verdict.reason}"
        )
        checked += 1
    assert checked >= 40


def test_the_stop_always_sits_between_entry_and_liquidation():
    """Stated as prices rather than as fractions, which is how it fails in practice."""
    for plan, price, sigma, direction, _ in plans():
        stop = plan.stop.price
        if direction > 0:
            assert stop < plan.entry_price, f"long stop {stop} >= entry at price={price}"
        else:
            assert stop > plan.entry_price, f"short stop {stop} <= entry at price={price}"
        assert plan.stop.distance <= plan.stop.ceiling + 1e-12, (
            f"stop {plan.stop.distance} breached ceiling {plan.stop.ceiling} "
            f"at price={price} sigma={sigma}"
        )


def test_a_stop_is_never_zero_width_or_inverted():
    """A stop that rounds onto its own entry is not protection, it is an instant exit."""
    for plan, price, sigma, direction, _ in plans():
        assert plan.stop.distance > 0, f"zero-width stop at price={price} sigma={sigma}"
        assert np.isfinite(plan.stop.price)
        assert plan.stop.price > 0


# --- invariant 4: no martingale, no averaging down -------------------------

def test_the_risk_fraction_cannot_be_moved_by_history():
    """Anti-martingale is unrepresentable, not merely switched off.

    `risk_fraction()` takes no arguments, so there is nothing to scale by. Losing streaks,
    winning streaks and drawdown must all leave it identical.
    """
    manager = RiskManager(RiskParams.from_config(load_config()), MemoryRiskStore())
    manager.observe_equity(NOW, 10_000.0)
    baseline = manager.risk_fraction()

    equity = 10_000.0
    for index, pnl in enumerate([-25.0, -25.0, 40.0, -25.0, 80.0, -25.0], start=1):
        equity += pnl
        manager.record_trade(now=NOW + index * 3600, pnl=pnl, equity=equity)
        assert manager.risk_fraction() == baseline, "history moved the risk fraction"


@pytest.mark.parametrize("forbidden", ["risk.martingale", "risk.averaging_down"])
def test_the_forbidden_strategies_cannot_be_switched_on(forbidden, tmp_path, monkeypatch):
    """Config validation rejects `true`; it is not a default that could be overridden."""
    import config as config_module

    section, key = forbidden.split(".")
    original = config_module.CONFIG_PATH.read_text(encoding="utf-8")
    mutated = tmp_path / "config.yaml"
    mutated.write_text(
        original.replace(f"  {key}: false", f"  {key}: true"), encoding="utf-8"
    )
    monkeypatch.setattr(config_module, "CONFIG_PATH", mutated)

    with pytest.raises(ConfigError, match=key):
        load_config()


def test_a_second_position_in_a_held_symbol_is_refused_as_averaging_down():
    """The behavioural half of invariant 4."""
    manager = RiskManager(RiskParams.from_config(load_config()), MemoryRiskStore())
    manager.observe_equity(NOW, 10_000.0)

    assert manager.can_trade(now=NOW, equity=10_000.0, open_positions=0,
                             symbol="BTC_USDT").allowed
    decision = manager.can_trade(now=NOW, equity=10_000.0, open_positions=1,
                                 symbol="BTC_USDT", open_symbols=("BTC_USDT",))
    assert not decision.allowed


# --- invariant 1 (the ladder half): protection never over-closes -----------

def test_a_take_profit_ladder_never_closes_more_than_the_position():
    """Over-closing reverses the position — the failure `reduce_only` exists to stop."""
    params = ProtectionParams.from_config(load_config())
    for price in PRICES:
        for direction in (1, -1):
            for size in (1, 7, 100, 1_174, 999_999):
                stop = price * (1 - 0.00325) if direction > 0 else price * (1 + 0.00325)
                legs = take_profit_ladder(price, stop, direction, size * direction, params)
                total = sum(abs(leg.size) for leg in legs)
                assert total == size, (
                    f"ladder closes {total} of {size} at price={price} dir={direction}"
                )
                for leg in legs:
                    # Every leg is in profit, on the correct side of entry.
                    assert (leg.price > price) if direction > 0 else (leg.price < price)


def test_the_ladder_always_leaves_a_runner():
    """A ladder that closed everything at TP1 would defeat the RR the design assumes."""
    params = ProtectionParams.from_config(load_config())
    for price in PRICES:
        for direction in (1, -1):
            stop = price * (1 - 0.00325) if direction > 0 else price * (1 + 0.00325)
            legs = take_profit_ladder(price, stop, direction, 1_000 * direction, params)
            assert len(legs) >= 2, "the ladder collapsed to a single exit"
            assert abs(legs[-1].size) > 0


def test_the_ratchet_never_loosens_a_stop():
    """A stop may only ever move toward profit, whatever the market does."""
    for price in PRICES:
        for direction in (1, -1):
            current = price * (1 - 0.003) if direction > 0 else price * (1 + 0.003)
            for step in range(12):
                candidate = (
                    price * (1 - 0.003 + step * 0.0004) if direction > 0
                    else price * (1 + 0.003 - step * 0.0004)
                )
                nxt = ratchet(current, candidate, direction)
                if direction > 0:
                    assert nxt >= current - 1e-12
                else:
                    assert nxt <= current + 1e-12
                current = nxt


def test_breakeven_is_always_past_entry_by_the_fee_buffer():
    """A break-even stop *at* entry still loses the round-trip fee."""
    params = ProtectionParams.from_config(load_config())
    for price in PRICES:
        for direction in (1, -1):
            be = breakeven_price(price, direction, params.breakeven_fee_buffer)
            if direction > 0:
                assert be > price
            else:
                assert be < price
            assert abs(be - price) / price == pytest.approx(
                params.breakeven_fee_buffer, rel=1e-9
            )


# --- invariant 6: an API response is not proof -----------------------------

def test_no_accepted_plan_trusts_a_number_it_was_not_given():
    """Every plan carries the tier it was sized against, so the check is re-derivable.

    A plan that reported a size without the tier behind it could not be verified after the
    fill, and the post-fill verification is the whole of invariant 6.
    """
    for plan, price, sigma, direction, equity in plans():
        assert plan.tier is not None, f"no tier recorded at price={price}"
        assert plan.leverage > 0
        assert plan.notional > 0
        assert plan.margin > 0
        # The tier must be strong enough for the notional actually taken.
        assert plan.notional <= plan.tier.risk_limit, (
            f"notional {plan.notional} exceeds tier limit {plan.tier.risk_limit} "
            f"at price={price} equity={equity}"
        )


# --- invariant 5: kill switches are not advisory ---------------------------

@pytest.mark.parametrize("equity_path,expected", [
    ([10_000.0, 9_899.0], Breaker.DAILY_LOSS),        # > 1% down on the day
    ([10_000.0, 10_500.0, 10_150.0], Breaker.DRAWDOWN),  # > 3% off the peak
])
def test_the_equity_breakers_trip_on_the_move_that_should_trip_them(equity_path, expected):
    """Driven by equity observations alone — no trade has to close for these to fire."""
    manager = RiskManager(RiskParams.from_config(load_config()), MemoryRiskStore())
    for index, equity in enumerate(equity_path):
        manager.observe_equity(NOW + index * 60, equity)

    decision = manager.can_trade(now=NOW + 600, equity=equity_path[-1], open_positions=0)
    assert not decision.allowed
    assert decision.breaker is expected


def test_a_breaker_that_has_tripped_stays_tripped_for_the_rest_of_the_day():
    """Recovering equity must not silently un-trip a latched breaker."""
    manager = RiskManager(RiskParams.from_config(load_config()), MemoryRiskStore())
    manager.observe_equity(NOW, 10_000.0)
    manager.observe_equity(NOW + 60, 9_800.0)          # trips the daily loss
    assert manager.tripped

    manager.observe_equity(NOW + 120, 10_400.0)        # a full recovery, same day
    assert manager.tripped, "a recovery cleared a latched breaker"
    assert not manager.can_trade(now=NOW + 180, equity=10_400.0,
                                 open_positions=0).allowed
