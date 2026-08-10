"""PHASE 6 tests — position sizing.

The invariant this file exists to protect: **size comes from risk, never from leverage,
and no rounding anywhere may increase exposure.** Everything else is a consequence.

Reference numbers are derived from the live-verified Phase 1 figures (taker 0.075%,
buffer 0.30%, mmr 0.30% on BTC/ETH and 0.50% on the other 29 pairs at >=100x) and
recomputed inside the tests, so a bug in ``position_sizer.py`` cannot make its own test
pass.
"""

from __future__ import annotations

import importlib
import math
from pathlib import Path

import numpy as np
import pytest

from config import load_config
from risk.position_sizer import (
    PositionPlan,
    SizingParams,
    liquidation_distance,
    plan_position,
    resolve_stop,
    select_tier,
)
from strategy.indicators import Candles, atr

# --- fixtures --------------------------------------------------------------
#
# Contract and tier payloads captured live on 2026-08-09; the BTC ladder is the same one
# tests/test_gate_client.py pins.

BTC_RAW = {
    "name": "BTC_USDT", "leverage_max": "200", "leverage_min": "1",
    "maintenance_rate": "0.003", "quanto_multiplier": "0.0001",
    "order_size_min": 1, "order_size_max": 12000000,
    "order_price_round": "0.1", "mark_price_round": "0.01",
    "taker_fee_rate": "0.00075", "maker_fee_rate": "-0.0001",
    "risk_limit_base": "500000", "in_delisting": False, "status": "trading",
}

BTC_TIERS_RAW = [
    {"tier": 1, "risk_limit": "500000", "initial_rate": "0.005",
     "maintenance_rate": "0.003", "leverage_max": "200", "deduction": "0"},
    {"tier": 2, "risk_limit": "1000000", "initial_rate": "0.006666",
     "maintenance_rate": "0.0035", "leverage_max": "150.01", "deduction": "250"},
    {"tier": 3, "risk_limit": "1500000", "initial_rate": "0.008",
     "maintenance_rate": "0.004", "leverage_max": "125", "deduction": "750"},
    {"tier": 5, "risk_limit": "3000000", "initial_rate": "0.01",
     "maintenance_rate": "0.005", "leverage_max": "100", "deduction": "2500"},
]


class FakeContract:
    """Minimal stand-in satisfying ``ContractSpec``. See also the real-Contract test."""

    def __init__(self, name="BTC_USDT", quanto_multiplier=0.0001, order_size_min=1,
                 order_size_max=12_000_000, order_price_round=0.0):
        self.name = name
        self.quanto_multiplier = quanto_multiplier
        self.order_size_min = order_size_min
        self.order_size_max = order_size_max
        self.order_price_round = order_price_round


class FakeTier:
    def __init__(self, tier, risk_limit, maintenance_rate, leverage_max=200.0):
        self.tier = tier
        self.risk_limit = risk_limit
        self.maintenance_rate = maintenance_rate
        self.leverage_max = leverage_max


#: One tier covering everything — the common case for a small account.
BTC_TIER1 = [FakeTier(1, 500_000, 0.003, 200.0)]
#: The mmr-0.50% contracts: SOL, XRP and the other 27 pairs at >=100x.
MMR50_TIER1 = [FakeTier(1, 500_000, 0.005, 100.0)]

BTC_TIERS = [
    FakeTier(int(t["tier"]), float(t["risk_limit"]), float(t["maintenance_rate"]),
             float(t["leverage_max"]))
    for t in BTC_TIERS_RAW
]


def candles(close, *, wick=0.001, interval=60.0):
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


def wobble(n=300, start=65_000.0, sigma=0.0005, seed=3):
    """A random walk with stationary volatility — ATR settles instead of drifting."""
    rng = np.random.default_rng(seed)
    return start * np.exp(np.cumsum(rng.normal(0, sigma, n)))


def flat(n=300, price=65_000.0):
    return np.full(n, price)


def plan(**overrides):
    """Size a default BTC long, with any argument overridable."""
    series = overrides.pop("candles", None)
    if series is None:
        series = candles(wobble())
    kwargs = dict(
        symbol="BTC_USDT", direction=1, candles=series, contract=FakeContract(),
        tiers=BTC_TIER1, equity=10_000.0, available=10_000.0, params=SizingParams(),
    )
    kwargs.setdefault(
        "entry_price", float(series.close[-1]) if len(series) else float("nan")
    )
    kwargs.update(overrides)
    return plan_position(**kwargs)


# --- the ceiling comes from the tiered maintenance rate --------------------

def test_stop_ceiling_matches_the_documented_phase_1_numbers():
    """0.325% on BTC/ETH and 0.125% on the mmr-0.50% pairs, at 100x and a 0.30% buffer."""
    p = SizingParams()
    assert liquidation_distance(100, 0.003, p.taker_fee) == pytest.approx(0.00625)
    assert liquidation_distance(100, 0.005, p.taker_fee) == pytest.approx(0.00425)

    btc = plan(candles=candles(wobble()), tiers=BTC_TIER1)
    mmr50 = plan(candles=candles(wobble()), tiers=MMR50_TIER1)
    assert btc.stop.ceiling == pytest.approx(0.00325, abs=1e-9)
    assert mmr50.stop.ceiling == pytest.approx(0.00125, abs=1e-9)


def test_ceiling_uses_the_tier_not_the_flat_contract_field():
    """A larger position must be sized against the tier's higher maintenance rate.

    The contract-level ``maintenance_rate`` is only tier 1. Sizing off it would set the
    stop ceiling too wide precisely as the position grows into a stricter tier.
    """
    # 0.25% of 400M equity against a 0.325% stop implies ~300M of notional: deep into the
    # ladder, where mmr has climbed from 0.30% to 0.50%.
    big = plan(equity=400_000_000.0, available=400_000_000.0, tiers=BTC_TIERS)
    assert big.tier.tier > 1
    assert big.tier.maintenance_rate > BTC_TIERS[0].maintenance_rate
    assert big.stop.ceiling < 0.00325


def test_no_tiers_is_a_refusal_not_a_fallback():
    result = plan(tiers=[])
    assert not result.ok
    assert result.stage == "risk_tiers"
    assert "flat maintenance_rate" in result.reason


def test_tier_used_is_never_weaker_than_the_final_notional_requires():
    """The fixed point may round the tier up (conservative) but never down."""
    for equity in (1_000.0, 100_000.0, 10_000_000.0, 400_000_000.0):
        result = plan(equity=equity, available=equity, tiers=BTC_TIERS)
        if not result.ok:
            continue
        required = select_tier(BTC_TIERS, result.notional)
        assert result.tier.maintenance_rate >= required.maintenance_rate, equity


def test_tier_leverage_cap_is_respected():
    """A tier whose leverage_max is below the configured leverage cannot hold the position."""
    tiers = [FakeTier(8, 500_000, 0.003, leverage_max=50.0)]
    result = plan(tiers=tiers)
    assert not result.ok
    assert result.stage == "risk_tiers"
    assert "caps leverage at 50x" in result.reason


def test_tier_selection_agrees_with_the_exchange_client():
    """``risk`` keeps its own copy of tier selection so it imports nothing from ``exchange``.

    Behaviour is pinned to the client's instead of the code being shared, which is the
    stronger guarantee: it would catch a divergence a shared import cannot have.
    """
    from exchange.gate_client import RiskTier, select_tier as client_select_tier

    tiers = [RiskTier.from_api(row) for row in BTC_TIERS_RAW]
    for notional in (0, 1, 499_999, 500_000, 500_001, 1_000_000, 1_200_000,
                     2_999_999, 3_000_000, 99_000_000):
        assert select_tier(tiers, notional).tier == client_select_tier(tiers, notional).tier


# --- the risk formula ------------------------------------------------------

def test_size_spends_the_risk_budget_without_exceeding_it():
    result = plan()
    assert result.ok
    assert result.risk_amount == pytest.approx(10_000.0 * 0.0025)
    assert result.max_loss <= result.risk_amount
    # Flooring one contract of granularity away is the only permitted shortfall.
    loss_per_contract = result.metrics["loss_per_contract"]
    assert result.risk_amount - result.max_loss < loss_per_contract


def test_size_is_the_risk_formula_from_the_spec():
    """size = (equity x risk.per_trade) / stop_distance, floored to whole contracts."""
    result = plan()
    expected = math.floor(
        result.risk_amount
        / (0.0001 * result.entry_price * result.stop.distance)
    )
    assert abs(result.size) == expected


def test_flooring_never_rounds_a_position_up():
    """Across many equities the realised risk stays at or below budget — never above."""
    series = candles(wobble())
    for equity in np.linspace(500.0, 250_000.0, 60):
        result = plan(candles=series, equity=float(equity), available=float(equity))
        if not result.ok:
            continue
        assert result.max_loss <= result.risk_amount + 1e-9, equity


def test_leverage_changes_margin_but_not_risk():
    """Leverage never increases risk; it only reduces locked margin.

    The tape is deliberately quiet so the ATR stop sits inside both ceilings — at 20x the
    liquidation ceiling is 4.3%, at 100x it is 0.325%, and a capped stop would change the
    size for a reason that has nothing to do with the invariant under test.
    """
    series = candles(wobble(sigma=0.00002), wick=0.00002)
    low = plan(candles=series, params=SizingParams(leverage=20))
    high = plan(candles=series, params=SizingParams(leverage=100))
    assert low.ok and high.ok
    assert not low.stop.capped and not high.stop.capped
    assert low.stop.distance == high.stop.distance      # same stop
    assert low.size == high.size                        # same size
    assert low.max_loss == pytest.approx(high.max_loss)  # same risk
    assert high.margin == pytest.approx(low.margin / 5)  # only the margin moved


def test_short_is_the_mirror_of_long():
    series = candles(flat())
    long = plan(candles=series, direction=1)
    short = plan(candles=series, direction=-1)
    assert long.ok and short.ok
    assert long.size > 0 > short.size
    assert abs(long.size) == abs(short.size)
    assert long.stop.price > long.entry_price * 0.99
    assert short.stop.price > short.entry_price       # stop above entry on a short
    assert long.stop.price < long.entry_price
    assert long.stop.distance == pytest.approx(short.stop.distance, abs=1e-9)


def test_reported_fee_cost_matches_the_architecture_table():
    """0.20 R on a 0.325% stop, 0.52 R on a 0.125% one — ARCHITECTURE §4."""
    btc = plan(tiers=BTC_TIER1)
    overhead = (btc.max_loss_after_fees - btc.max_loss) / btc.max_loss
    assert overhead == pytest.approx(0.20, abs=0.01)

    mmr50 = plan(contract=FakeContract(quanto_multiplier=1.0, name="SOL_USDT"),
                 tiers=MMR50_TIER1, candles=candles(wobble(start=150.0)))
    mmr50_entry = mmr50.entry_price
    assert mmr50.ok, mmr50.reason
    overhead = (mmr50.max_loss_after_fees - mmr50.max_loss) / mmr50.max_loss
    assert overhead == pytest.approx(0.52, abs=0.01)
    assert mmr50_entry > 0


def test_notional_and_margin_are_consistent():
    result = plan()
    assert result.notional == pytest.approx(result.coin_amount * result.entry_price)
    assert result.coin_amount == pytest.approx(abs(result.size) * 0.0001)
    assert result.margin == pytest.approx(result.notional / 100)


# --- exchange order limits -------------------------------------------------

def test_order_size_min_refuses_rather_than_rounding_up():
    """The minimum order risking more than the budget is a refusal, not a rounding.

    Rounding up here would silently exceed ``risk.per_trade``, the number every other
    guarantee in the bot is derived from.
    """
    result = plan(equity=50.0, available=50.0,
                  contract=FakeContract(order_size_min=1000))
    assert not result.ok
    assert result.stage == "order_size_min"
    assert result.size == 0
    assert "above the" in result.reason


def test_order_size_max_caps_the_position():
    result = plan(contract=FakeContract(order_size_max=10))
    assert result.ok
    assert abs(result.size) == 10
    assert "order_size_max" in result.capped_by
    assert result.max_loss < result.risk_amount   # a cap can only reduce risk


def test_risk_limit_caps_the_position():
    """Notional can never exceed the top tier's risk_limit."""
    tiers = [FakeTier(1, 5_000.0, 0.003, 200.0)]
    result = plan(equity=1_000_000.0, available=1_000_000.0, tiers=tiers)
    assert result.ok
    assert "risk_limit" in result.capped_by
    assert result.notional <= 5_000.0


def test_insufficient_margin_is_refused_and_carries_no_size():
    result = plan(available=1.0)
    assert not result.ok
    assert result.stage == "margin"
    assert result.size == 0
    assert result.margin > 1.0


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
def test_unusable_inputs_are_refused(bad):
    assert not plan(entry_price=bad).ok
    assert not plan(equity=bad).ok


def test_direction_zero_is_refused():
    result = plan(direction=0)
    assert not result.ok and result.stage == "direction"


def test_empty_candles_are_refused():
    empty = np.array([], dtype=float)
    result = plan(candles=Candles(empty, empty, empty, empty, empty, empty),
                  entry_price=65_000.0)
    assert not result.ok and result.stage == "data"


# --- the stop --------------------------------------------------------------

def test_atr_stop_is_used_when_it_fits_under_the_ceiling():
    """A quiet tape gives an ATR stop well inside the ceiling, so nothing is capped."""
    series = candles(wobble(sigma=0.00002), wick=0.00002)
    result = plan(candles=series)
    assert result.ok
    assert result.stop.method == "atr"
    assert not result.stop.capped
    assert result.stop.distance < result.stop.ceiling


def test_wide_atr_is_capped_at_the_ceiling():
    result = plan(candles=candles(wobble(sigma=0.002)))
    assert result.ok
    assert result.stop.capped
    assert result.stop.distance <= result.stop.ceiling + 1e-12


def test_capping_reserves_a_tick_for_the_liquidation_guard():
    """The stop is clamped a tick *inside* the ceiling, not onto it.

    Phase 7 re-checks the stop against a liquidation price it rounds toward entry, which
    shortens the gap by up to one tick. Capping onto the ceiling exactly left nothing for
    that, so a maximally-capped stop was vetoed intermittently depending on where the last
    decimals fell. The reserve costs a tick of stop room and moves the stop toward entry —
    the safe direction — and makes the two layers compose deterministically.
    """
    result = plan(candles=candles(wobble(sigma=0.002)),
                  contract=FakeContract(order_price_round=0.1))
    assert result.ok and result.stop.capped
    assert result.stop.ceiling == pytest.approx(0.00325)   # the true limit is reported
    assert result.stop.distance < result.stop.ceiling      # the stop used sits inside it
    reserved = result.stop.ceiling - result.stop.distance
    assert 0 < reserved <= 2 * (0.1 / result.entry_price)


def test_capping_without_a_price_grid_still_lands_on_the_ceiling():
    """With no tick there is nothing to round, so nothing needs reserving."""
    result = plan(candles=candles(wobble(sigma=0.002)),
                  contract=FakeContract(order_price_round=0.0))
    assert result.ok and result.stop.capped
    assert result.stop.distance == pytest.approx(result.stop.ceiling)


def test_the_reserve_never_pushes_the_stop_below_the_minimum_distance():
    """On a coarse grid the reserve could otherwise undercut stop_loss.min_distance."""
    result = plan(candles=candles(wobble(sigma=0.002)),
                  contract=FakeContract(order_price_round=25.0))
    if result.ok:
        assert result.stop.distance >= SizingParams().min_distance - 1e-12


def test_skip_refuses_where_cap_would_clamp():
    series = candles(wobble(sigma=0.002))
    capped = plan(candles=series, params=SizingParams(on_sl_exceeds_max="cap"))
    skipped = plan(candles=series, params=SizingParams(on_sl_exceeds_max="skip"))
    assert capped.ok and capped.stop.capped
    assert not skipped.ok
    assert skipped.stage == "sl_exceeds_max"


def test_min_distance_floors_a_tiny_atr_stop():
    """ARCHITECTURE §11: a 0.047% ATR implies a 0.071% stop, below the 0.10% floor."""
    series = candles(wobble(sigma=0.00002), wick=0.00002)
    p = SizingParams(min_sl_atr_ratio=0.0)
    result = plan(candles=series, params=p)
    assert result.ok
    assert result.stop.distance >= p.min_distance - 1e-9


def test_stop_inside_noise_is_refused():
    """A capped stop below min_sl_atr_ratio x ATR is inside noise, not protection."""
    series = candles(wobble(sigma=0.01), wick=0.01)   # violent tape, tiny ceiling
    result = plan(candles=series, tiers=MMR50_TIER1,
                  params=SizingParams(min_sl_atr_ratio=0.20))
    assert not result.ok
    assert result.stage == "noise"
    assert "inside noise" in result.reason


def test_structure_stop_wins_under_auto_when_it_is_wider():
    """`auto` takes the wider of ATR and structure."""
    # A quiet drift with one pronounced spike low, placed far enough back that its
    # confirmation bar has printed. Highs and lows are set explicitly: with the usual
    # open = previous close, the bar after a trough inherits the trough as its own low,
    # and two equal lows are a plateau, which Phase 4 deliberately refuses to call a pivot.
    n = 120
    close = np.full(n, 100.0)
    high = close + 0.02
    low = close - 0.02
    low[n - 20] = 98.0                      # the swing low, confirmed 2 bars later
    series = Candles(
        time=np.arange(n, dtype=float) * 60,
        open=close.copy(), high=high, low=low, close=close,
        volume=np.full(n, 1000.0),
    )
    p = SizingParams(min_distance=1e-6, min_sl_atr_ratio=0.0, atr_period=14,
                     structure_lookback=50)
    stop = resolve_stop(series, +1, 100.0, ceiling=0.5, params=p)
    assert stop.ok, stop.reason
    assert stop.method == "structure"
    assert stop.price == pytest.approx(98.0)
    assert stop.distance > p.atr_multiplier * stop.atr_distance   # wider than the ATR stop


def test_structure_without_a_confirmed_pivot_falls_back_to_atr():
    """No pivot is missing data, never open space — Phase 4's rule."""
    series = candles(np.linspace(100.0, 110.0, 200), wick=0.0)  # monotonic: no swing lows
    p = SizingParams(sl_method="structure", min_sl_atr_ratio=0.0)
    stop = resolve_stop(series, +1, float(series.close[-1]), ceiling=0.5, params=p)
    assert stop.ok
    assert stop.method == "atr"


def test_no_stop_fits_when_the_buffer_swallows_the_liquidation_distance():
    """At 200x the liquidation distance is inside the buffer: nothing can be placed."""
    p = SizingParams(leverage=200)
    result = plan(params=p, tiers=[FakeTier(1, 500_000, 0.003, 200.0)])
    assert not result.ok
    assert result.stage == "liquidation_ceiling"


# --- price-grid rounding ---------------------------------------------------

@pytest.mark.parametrize("tick", [0.0, 0.1, 1.0, 5.0, 25.0])
@pytest.mark.parametrize("direction", [1, -1])
def test_rounding_onto_the_price_grid_never_breaches_the_ceiling(tick, direction):
    """Rounding is toward entry, so the stop can only move away from liquidation."""
    series = candles(wobble(sigma=0.002))
    entry = float(series.close[-1])
    p = SizingParams(min_distance=1e-6, min_sl_atr_ratio=0.0)
    stop = resolve_stop(series, direction, entry, ceiling=0.00325, params=p,
                        price_tick=tick)
    assert stop.ok, stop.reason
    assert stop.distance <= stop.ceiling + 1e-12
    if tick > 0:
        assert stop.price / tick == pytest.approx(round(stop.price / tick), abs=1e-6)


def test_size_is_derived_from_the_rounded_stop_not_the_ideal_one():
    """Sizing on the unrounded stop would risk slightly more than the budget."""
    series = candles(flat())
    result = plan(candles=series, contract=FakeContract(order_price_round=25.0))
    assert result.ok
    realised = abs(result.size) * 0.0001 * abs(result.entry_price - result.stop.price)
    assert realised == pytest.approx(result.max_loss)
    assert result.max_loss <= result.risk_amount


def test_a_price_grid_too_coarse_for_a_stop_is_refused():
    series = candles(flat(price=100.0), wick=0.0005)
    stop = resolve_stop(series, +1, 100.0, ceiling=0.00325, params=SizingParams(),
                        price_tick=100.0)
    assert not stop.ok
    assert stop.stage == "price_grid"


# --- lookahead -------------------------------------------------------------

def test_as_of_equals_replaying_on_a_truncated_series():
    """What a backtest sees at bar i must equal what a live run saw at bar i."""
    series = candles(wobble(n=260))
    p = SizingParams()
    for i in range(200, len(series)):
        entry = float(series.close[i])
        live = plan_position(
            symbol="BTC_USDT", direction=1, entry_price=entry,
            candles=series.head(i + 1), contract=FakeContract(), tiers=BTC_TIER1,
            equity=10_000.0, available=10_000.0, params=p,
        )
        replay = plan_position(
            symbol="BTC_USDT", direction=1, entry_price=entry,
            candles=series, contract=FakeContract(), tiers=BTC_TIER1,
            equity=10_000.0, available=10_000.0, params=p, as_of=i,
        )
        assert live.ok == replay.ok, i
        assert live.size == replay.size, i
        assert live.reason == replay.reason, i


# --- config wiring ---------------------------------------------------------

def test_params_from_shipped_config():
    p = SizingParams.from_config(load_config())
    assert p.risk_per_trade == 0.0025
    assert p.leverage == 100
    assert p.liquidation_buffer == 0.003
    assert p.taker_fee == 0.00075
    assert p.maker_fee == -0.0001    # a rebate
    assert p.on_sl_exceeds_max == "cap"
    assert p.min_sl_atr_ratio == 0.20


@pytest.mark.parametrize("overrides", [
    {"risk_per_trade": 0.0},
    {"risk_per_trade": 0.5},
    {"leverage": 0},
    {"min_distance": 0.05, "max_distance": 0.02},
    {"sl_method": "vibes"},
    {"on_sl_exceeds_max": "maybe"},
    {"atr_multiplier": 0.0},
])
def test_unsafe_params_are_rejected_at_construction(overrides):
    with pytest.raises(ValueError):
        SizingParams(**overrides)


def test_the_real_exchange_contract_satisfies_the_protocol():
    """``exchange.gate_client.Contract`` is usable here without ``risk`` importing it."""
    from exchange.gate_client import Contract, RiskTier

    contract = Contract.from_api(BTC_RAW)
    tiers = [RiskTier.from_api(row) for row in BTC_TIERS_RAW]
    result = plan(contract=contract, tiers=tiers)
    assert result.ok
    assert result.stop.price % 0.1 == pytest.approx(0.0, abs=1e-6)   # on the price grid


# --- no order path ---------------------------------------------------------

def test_risk_package_has_no_exchange_import():
    """Sizing must have no path to the module that can place orders.

    Contracts and tiers arrive as structural protocols instead, so the property holds by
    construction rather than by review.
    """
    for name in ("risk.position_sizer", "risk.risk_manager"):
        module = importlib.import_module(name)
        source = Path(module.__file__).read_text(encoding="utf-8")
        code = "\n".join(
            line for line in source.splitlines() if line.startswith(("import ", "from "))
        )
        assert "exchange" not in code, f"{name} imports exchange"
        assert "execution" not in code, f"{name} imports execution"


def test_the_sizer_exposes_no_order_placing_method():
    forbidden = ("order", "execute", "place", "submit", "cancel")
    names = [n for n in dir(PositionPlan) if not n.startswith("_")]
    assert not [n for n in names if any(word in n.lower() for word in forbidden)]


def test_refusals_always_carry_a_stage_and_a_reason():
    refusals = [
        plan(tiers=[]),
        plan(direction=0),
        plan(available=0.0),
        plan(entry_price=float("nan")),
        plan(equity=50.0, available=50.0, contract=FakeContract(order_size_min=1000)),
    ]
    for result in refusals:
        assert not result.ok
        assert result.stage
        assert result.reason
        assert result.size == 0
        assert "no size" in result.summary()
