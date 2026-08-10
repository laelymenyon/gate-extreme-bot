"""PHASE 7 tests — the liquidation guard.

The property under test is one line: **liquidation is never a stop-loss.** A position may
exist only when its stop sits at least the configured buffer clear of the liquidation
price, computed from the tier matching actual notional.

The numbers are the live-verified Phase 1 figures (taker 0.075%, buffer 0.30%, mmr 0.30%
on BTC/ETH and 0.50% on the other 29 pairs at >=100x) and are recomputed inside the tests,
so a bug in ``liquidation_guard.py`` cannot make its own test pass.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import numpy as np
import pytest

from config import ConfigError, load_config
from exchange.gate_client import Contract, RiskTier
from risk.liquidation_guard import (
    CLOCK_SKEW_TOLERANCE_SECONDS,
    LiquidationParams,
    LiquidationVerdict,
    TierSnapshot,
    assess,
    assess_plan,
    liquidation_distance,
    liquidation_price,
    required_effective_leverage,
    verify_fill,
)
from risk.position_sizer import SizingParams, plan_position
from strategy.indicators import Candles

NOW = 1_754_784_000.0
ENTRY = 65_000.0

#: The BTC_USDT ladder captured live on 2026-08-09 — the same rows test_gate_client pins.
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

BTC_RAW = {
    "name": "BTC_USDT", "leverage_max": "200", "leverage_min": "1",
    "maintenance_rate": "0.003", "quanto_multiplier": "0.0001",
    "order_size_min": 1, "order_size_max": 12000000,
    "order_price_round": "0.1", "mark_price_round": "0.01",
    "taker_fee_rate": "0.00075", "maker_fee_rate": "-0.0001",
    "risk_limit_base": "500000", "in_delisting": False, "status": "trading",
}

BTC_TIERS = [RiskTier.from_api(row) for row in BTC_TIERS_RAW]
BTC = Contract.from_api(BTC_RAW)

#: An mmr-0.50% single-tier contract: SOL, XRP and the other 27 pairs at >=100x.
MMR50_TIERS = [RiskTier.from_api({
    "tier": 1, "risk_limit": "500000", "initial_rate": "0.01",
    "maintenance_rate": "0.005", "leverage_max": "100", "deduction": "0",
})]


def snapshot(tiers=BTC_TIERS, age=60.0, symbol="BTC_USDT"):
    return TierSnapshot.of(symbol, tiers, NOW - age)


def check(stop_distance, *, notional=100_000.0, direction=1, tiers=BTC_TIERS,
          params=None, contract=BTC, age=60.0, entry=ENTRY, now=NOW, **kwargs):
    """Assess a stop placed ``stop_distance`` from entry, on the correct side."""
    stop = entry * (1 - stop_distance) if direction > 0 else entry * (1 + stop_distance)
    return assess(
        symbol="BTC_USDT", direction=direction, entry_price=entry, stop_price=stop,
        notional=notional, snapshot=snapshot(tiers, age), now=now,
        params=params or LiquidationParams(), contract=contract, **kwargs,
    )


# --- the formula -----------------------------------------------------------

def test_liquidation_distance_matches_the_phase_1_table():
    p = LiquidationParams()
    assert liquidation_distance(100, 0.003, p.taker_fee) == pytest.approx(0.00625)
    assert liquidation_distance(100, 0.005, p.taker_fee) == pytest.approx(0.00425)
    assert liquidation_distance(125, 0.003, p.taker_fee) == pytest.approx(0.00425)
    assert liquidation_distance(200, 0.003, p.taker_fee) == pytest.approx(0.00125)


def test_the_stop_ceilings_are_the_documented_ones():
    """0.325% on BTC/ETH, 0.125% on the mmr-0.50% pairs, at 100x and a 0.30% buffer."""
    p = LiquidationParams()
    btc = liquidation_distance(100, 0.003, p.taker_fee) - p.liquidation_buffer
    mmr50 = liquidation_distance(100, 0.005, p.taker_fee) - p.liquidation_buffer
    assert btc == pytest.approx(0.00325)
    assert mmr50 == pytest.approx(0.00125)


@pytest.mark.parametrize("stop,expected", [(0.00325, True), (0.00320, True),
                                           (0.00330, False), (0.00400, False)])
def test_the_buffer_boundary_on_btc(stop, expected):
    """0.325% is exactly the widest stop that fits; a hair wider is refused."""
    assert check(stop).ok is expected


@pytest.mark.parametrize("stop,expected", [(0.00125, True), (0.00130, False)])
def test_the_buffer_boundary_on_an_mmr_050_contract(stop, expected):
    assert check(stop, tiers=MMR50_TIERS).ok is expected


def test_a_refusal_reports_how_far_short_it_was():
    verdict = check(0.0040)
    assert not verdict.ok
    assert verdict.stage == "buffer"
    assert verdict.buffer_actual == pytest.approx(0.00625 - 0.0040)
    assert verdict.buffer_required == 0.003
    assert "0.30%" in verdict.reason


def test_required_effective_leverage_solves_the_documented_case():
    """ARCHITECTURE §4: a 0.50% stop at mmr 0.30% needs 72.7x, not 100x."""
    solved = required_effective_leverage(0.005, 0.003, 0.00075, 0.005)
    assert solved == pytest.approx(72.7, abs=0.1)
    # And the solved leverage is self-consistent: at that leverage the buffer is exactly met.
    assert liquidation_distance(solved, 0.003, 0.00075) == pytest.approx(0.005 + 0.005)


def test_an_unsatisfiable_requirement_is_not_a_number():
    """Fees and maintenance alone can exceed the room asked for at any leverage."""
    assert np.isnan(required_effective_leverage(-1.0, 0.003, 0.00075, 0.0))


def test_the_topup_figure_is_reported_even_though_topup_is_disabled():
    """The solver informs skip-vs-trade; nothing posts margin in the shipped profile."""
    verdict = check(0.0050, notional=100_000.0)
    assert not verdict.ok
    # 1 / (0.50% stop + 0.30% buffer + 0.30% mmr + 0.075% fee) = 85.1x. The §4 table's
    # 72.7x is the same solve against the original 0.50% buffer.
    assert verdict.required_effective_leverage == pytest.approx(85.1, abs=0.1)
    assert required_effective_leverage(0.005, 0.003, 0.00075, 0.005) == pytest.approx(
        72.7, abs=0.1)
    assert verdict.required_margin > verdict.posted_margin
    assert verdict.extra_margin == pytest.approx(
        verdict.required_margin - verdict.posted_margin)


# --- tier selection --------------------------------------------------------

def test_the_tier_is_chosen_by_notional_not_by_the_flat_field():
    """BTC's flat maintenance_rate is 0.30%; at 2M notional the real rate is 0.50%."""
    small = check(0.0010, notional=100_000.0)
    large = check(0.0010, notional=2_000_000.0)
    assert small.tier.tier == 1 and small.maintenance_rate == 0.003
    assert large.tier.tier == 5 and large.maintenance_rate == 0.005
    assert large.maintenance_rate > BTC.maintenance_rate
    assert large.liq_distance < small.liq_distance


def test_a_stop_that_fits_at_tier_1_is_refused_at_tier_5():
    """The whole point of tiering: the same stop stops fitting as size grows."""
    assert check(0.00325, notional=100_000.0).ok
    refused = check(0.00325, notional=2_000_000.0)
    assert not refused.ok
    assert refused.stage == "buffer"
    assert refused.tier.tier == 5


@pytest.mark.parametrize("notional,tier", [
    (1, 1), (499_999, 1), (500_000, 1), (500_001, 2), (1_000_000, 2),
    (1_000_001, 3), (1_500_000, 3), (2_999_999, 5), (3_000_000, 5),
])
def test_tier_boundaries_are_inclusive_of_their_risk_limit(notional, tier):
    assert check(0.0010, notional=notional).tier.tier == tier


def test_notional_beyond_the_top_tier_is_refused():
    verdict = check(0.0010, notional=5_000_000.0)
    assert not verdict.ok
    assert verdict.stage == "risk_limit"


def test_tier_selection_agrees_with_the_exchange_client():
    from exchange.gate_client import select_tier as client_select_tier

    for notional in (1, 500_000, 500_001, 1_200_000, 2_999_999):
        verdict = check(0.0010, notional=notional)
        assert verdict.tier.tier == client_select_tier(BTC_TIERS, notional).tier


# --- leverage --------------------------------------------------------------

@pytest.mark.parametrize("leverage", [125, 150, 200])
def test_leverage_above_the_ceiling_is_rejected_at_construction(leverage):
    with pytest.raises(ValueError, match="ceiling"):
        LiquidationParams(leverage=leverage, max_leverage=100)


def test_a_tier_capping_leverage_below_the_configured_value_is_refused():
    """Tier 5 caps BTC at 100x; a 125x config could not hold that position."""
    params = LiquidationParams(leverage=125, max_leverage=125)
    verdict = check(0.0010, notional=2_000_000.0, params=params)
    assert not verdict.ok
    assert verdict.stage == "leverage"
    assert "caps leverage at 100x" in verdict.reason


def test_at_200x_no_stop_can_fit():
    """Liquidation is 0.125% away, inside the 0.30% buffer: nothing is placeable."""
    params = LiquidationParams(leverage=200, max_leverage=200)
    verdict = check(0.0005, params=params)
    assert not verdict.ok
    assert verdict.stage == "buffer"
    assert verdict.liq_distance == pytest.approx(0.00125)


def test_liquidation_on_entry_is_its_own_stage():
    """Past ~266x the maintenance rate and fee alone exceed the whole margin.

    Reached through a tier whose leverage_max is high enough not to fire first, so the
    arithmetic is what refuses rather than the ladder.
    """
    import dataclasses

    permissive = [dataclasses.replace(BTC_TIERS[0], leverage_max=500.0)]
    params = LiquidationParams(leverage=400, max_leverage=400)
    verdict = check(0.0005, params=params, tiers=permissive)
    assert not verdict.ok
    assert verdict.stage == "liquidation_distance"
    assert verdict.liq_distance <= 0


def test_cross_margin_is_refused():
    with pytest.raises(ValueError, match="isolated"):
        LiquidationParams(margin_mode="cross")


def test_margin_topup_requires_it_to_actually_lower_leverage():
    with pytest.raises(ValueError, match="max_effective_leverage"):
        LiquidationParams(allow_margin_topup=True, max_effective_leverage=100, leverage=100)


def test_topup_uses_the_effective_leverage_when_enabled():
    """With top-up on, the guard reasons at the reduced leverage, not the exchange one."""
    params = LiquidationParams(allow_margin_topup=True, max_effective_leverage=70)
    assert params.effective_leverage == 70
    verdict = check(0.0050, params=params)
    assert verdict.ok      # a 0.50% stop fits at 70x and would not at 100x
    assert not check(0.0050).ok


# --- missing, stale, invalid tier data ------------------------------------

def test_no_snapshot_is_a_refusal_not_a_fallback():
    verdict = assess("BTC_USDT", 1, ENTRY, ENTRY * 0.997, 100_000.0, None, NOW)
    assert not verdict.ok
    assert verdict.stage == "tier_data"
    assert "flat field is only tier 1" in verdict.reason


def test_an_empty_ladder_is_refused():
    verdict = check(0.0010, tiers=[])
    assert not verdict.ok
    assert verdict.stage == "tier_data"
    assert "empty" in verdict.reason


def test_a_stale_snapshot_is_refused():
    verdict = check(0.0010, age=7_200.0)
    assert not verdict.ok
    assert verdict.stage == "tier_data"
    assert "7200s old" in verdict.reason


def test_a_snapshot_just_inside_the_age_limit_is_accepted():
    assert check(0.0010, age=3_599.0).ok
    assert not check(0.0010, age=3_601.0).ok


def test_a_snapshot_from_the_future_is_refused():
    """Clocks that disagree make the age meaningless, so the age limit stops protecting."""
    verdict = check(0.0010, age=-600.0)
    assert not verdict.ok
    assert "future" in verdict.reason


def test_small_clock_skew_is_tolerated():
    assert check(0.0010, age=-(CLOCK_SKEW_TOLERANCE_SECONDS - 1)).ok


@pytest.mark.parametrize("field,value", [
    ("maintenance_rate", 0.0), ("maintenance_rate", 1.5), ("maintenance_rate", float("nan")),
    ("risk_limit", 0.0), ("risk_limit", float("inf")),
    ("leverage_max", 0.0), ("leverage_max", float("nan")),
])
def test_an_unusable_tier_field_is_refused(field, value):
    import dataclasses

    tiers = [dataclasses.replace(BTC_TIERS[0], **{field: value})]
    verdict = check(0.0010, tiers=tiers)
    assert not verdict.ok
    assert verdict.stage == "tier_data"
    assert field in verdict.reason


def test_a_non_monotonic_ladder_is_refused():
    """Gate's ladders climb in notional and mmr while leverage falls. Anything else is corrupt."""
    import dataclasses

    # maintenance_rate falling as notional grows would understate risk at size.
    broken = [BTC_TIERS[0], dataclasses.replace(BTC_TIERS[1], maintenance_rate=0.001)]
    verdict = check(0.0010, tiers=broken)
    assert not verdict.ok
    assert "maintenance_rate falls" in verdict.reason

    # leverage_max rising with size is equally impossible.
    broken = [BTC_TIERS[0], dataclasses.replace(BTC_TIERS[1], leverage_max=250.0)]
    assert "leverage_max rises" in check(0.0010, tiers=broken).reason

    # Two tiers covering the same notional make tier selection ambiguous.
    broken = [BTC_TIERS[0], dataclasses.replace(BTC_TIERS[1], risk_limit=500_000.0)]
    assert "risk_limit does not increase" in check(0.0010, tiers=broken).reason


# --- input validation ------------------------------------------------------

@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
def test_unusable_prices_are_refused(bad):
    assert not assess("BTC_USDT", 1, bad, 64_000.0, 100_000.0, snapshot(), NOW).ok
    assert not assess("BTC_USDT", 1, ENTRY, bad, 100_000.0, snapshot(), NOW).ok
    assert not assess("BTC_USDT", 1, ENTRY, 64_000.0, bad, snapshot(), NOW).ok


@pytest.mark.parametrize("direction", [0, 2, -3, None])
def test_an_invalid_direction_is_refused(direction):
    verdict = assess("BTC_USDT", direction, ENTRY, 64_000.0, 100_000.0, snapshot(), NOW)
    assert not verdict.ok
    assert verdict.stage == "direction"


def test_a_stop_on_the_wrong_side_of_entry_is_refused():
    """A long whose stop sits above entry is not protected, whatever the arithmetic says."""
    above = assess("BTC_USDT", 1, ENTRY, ENTRY * 1.001, 100_000.0, snapshot(), NOW)
    below = assess("BTC_USDT", -1, ENTRY, ENTRY * 0.999, 100_000.0, snapshot(), NOW)
    for verdict in (above, below):
        assert not verdict.ok
        assert verdict.stage == "stop_side"


def test_shorts_mirror_longs():
    long = check(0.00325, direction=1)
    short = check(0.00325, direction=-1)
    assert long.ok and short.ok
    assert long.liq_distance == pytest.approx(short.liq_distance)
    assert long.liq_price < ENTRY < short.liq_price
    assert long.buffer_actual == pytest.approx(short.buffer_actual, abs=1e-6)


# --- rounding --------------------------------------------------------------

@pytest.mark.parametrize("direction", [1, -1])
@pytest.mark.parametrize("tick", [0.0, 0.01, 0.1, 1.0, 25.0])
def test_the_predicted_liq_price_rounds_toward_entry(direction, tick):
    """Rounding away from entry would overstate the room and widen the apparent buffer."""
    raw = liquidation_price(ENTRY, direction, 0.00625, tick=0.0)
    rounded = liquidation_price(ENTRY, direction, 0.00625, tick=tick)
    if direction > 0:
        assert rounded >= raw          # liquidation is below entry; toward entry is up
    else:
        assert rounded <= raw
    assert abs(rounded - raw) <= (tick or 0.0)
    if tick:
        assert rounded / tick == pytest.approx(round(rounded / tick), abs=1e-6)


def test_a_coarse_price_grid_can_turn_a_pass_into_a_refusal():
    """The fractional check can pass while the price check fails by a tick."""
    import dataclasses

    coarse = dataclasses.replace(BTC, mark_price_round=100.0, order_price_round=100.0)
    fine = check(0.00325, contract=BTC)
    blunt = check(0.00325, contract=coarse)
    assert fine.ok
    assert not blunt.ok
    assert blunt.stage == "buffer"
    assert "grid" in blunt.reason


def test_the_liq_price_is_reported_on_the_contract_grid():
    verdict = check(0.0010, contract=BTC)
    assert verdict.liq_price / 0.01 == pytest.approx(round(verdict.liq_price / 0.01), abs=1e-6)


# --- integration with the Phase 6 sizer -----------------------------------

def _candles(n=300, price=ENTRY, sigma=0.0005, seed=3):
    rng = np.random.default_rng(seed)
    close = price * np.exp(np.cumsum(rng.normal(0, sigma, n)))
    open_ = np.concatenate([[close[0]], close[:-1]])
    return Candles(
        time=np.arange(n, dtype=float) * 60, open=open_,
        high=np.maximum(open_, close) * 1.001, low=np.minimum(open_, close) * 0.999,
        close=close, volume=np.full(n, 1000.0),
    )


def _plan(equity=10_000.0, tiers=BTC_TIERS, candles=None):
    series = candles if candles is not None else _candles()
    return plan_position(
        symbol="BTC_USDT", direction=1, entry_price=float(series.close[-1]),
        candles=series, contract=BTC, tiers=tiers, equity=equity, available=equity,
        params=SizingParams(),
    )


def test_a_phase_6_plan_passes_the_guard():
    """A stop the sizer capped at the ceiling must clear the buffer by construction."""
    plan = _plan()
    assert plan.ok
    verdict = assess_plan(plan, snapshot(), NOW, contract=BTC)
    assert verdict.ok, verdict.reason
    assert verdict.buffer_actual >= verdict.buffer_required - 1e-12


def test_the_guard_re_derives_rather_than_trusting_the_plan():
    """A plan sized against tier 1 while the position lands in tier 5 must be caught."""
    plan = _plan()
    tier5_only = [RiskTier.from_api({
        "tier": 5, "risk_limit": "3000000", "initial_rate": "0.01",
        "maintenance_rate": "0.005", "leverage_max": "100", "deduction": "0",
    })]
    verdict = assess_plan(plan, snapshot(tier5_only), NOW, contract=BTC)
    assert not verdict.ok
    assert verdict.stage == "buffer"


def test_a_refused_plan_is_refused_again():
    plan = _plan(equity=1.0)          # too small to size
    assert not plan.ok
    verdict = assess_plan(plan, snapshot(), NOW, contract=BTC)
    assert not verdict.ok
    assert verdict.stage == "plan"


def test_assess_plan_checks_available_margin():
    plan = _plan()
    verdict = assess_plan(plan, snapshot(), NOW, contract=BTC, available_margin=1.0)
    assert not verdict.ok
    assert verdict.stage == "margin"


def test_the_guard_does_not_change_phase_6_behaviour():
    """Phase 6 sizing is identical whether or not the guard runs."""
    plan_before = _plan()
    assess_plan(plan_before, snapshot(), NOW, contract=BTC)
    plan_after = _plan()
    assert plan_before.size == plan_after.size
    assert plan_before.stop.price == plan_after.stop.price


# --- post-fill verification ------------------------------------------------

def test_the_exchange_liq_price_is_what_counts():
    ok = verify_fill("BTC_USDT", 1, ENTRY, ENTRY * (1 - 0.00325), ENTRY * (1 - 0.00625), NOW)
    assert ok.ok
    assert ok.buffer_actual == pytest.approx(0.003, abs=1e-9)


def test_a_liq_price_inside_the_buffer_recommends_flattening():
    verdict = verify_fill("BTC_USDT", 1, ENTRY, ENTRY * (1 - 0.00325), ENTRY * (1 - 0.005), NOW)
    assert not verdict.ok
    assert verdict.stage == "buffer"
    assert verdict.action == "flatten"


def test_liquidation_beyond_the_stop_recommends_flattening():
    """The position would liquidate before its own stop fills."""
    verdict = verify_fill("BTC_USDT", 1, ENTRY, ENTRY * (1 - 0.005), ENTRY * (1 - 0.003), NOW)
    assert not verdict.ok
    assert verdict.action == "flatten"
    assert "before its stop fills" in verdict.reason


@pytest.mark.parametrize("reported", [0.0, -1.0, float("nan"), None])
def test_a_missing_liq_price_is_unverified_and_refused(reported):
    verdict = verify_fill("BTC_USDT", 1, ENTRY, ENTRY * 0.997, reported, NOW)
    assert not verdict.ok
    assert verdict.stage == "liq_price"
    assert verdict.action == "flatten"


def test_a_liq_price_on_the_wrong_side_of_entry_is_refused():
    verdict = verify_fill("BTC_USDT", 1, ENTRY, ENTRY * 0.997, ENTRY * 1.006, NOW)
    assert not verdict.ok
    assert "wrong side" in verdict.reason


def test_reported_leverage_of_zero_means_cross_margin():
    verdict = verify_fill("BTC_USDT", 1, ENTRY, ENTRY * (1 - 0.00325),
                          ENTRY * (1 - 0.00625), NOW, reported_leverage=0)
    assert not verdict.ok
    assert verdict.stage == "margin_mode"
    assert "cross margin" in verdict.reason


def test_reported_leverage_above_the_ceiling_is_refused():
    verdict = verify_fill("BTC_USDT", 1, ENTRY, ENTRY * (1 - 0.00325),
                          ENTRY * (1 - 0.00625), NOW, reported_leverage=150)
    assert not verdict.ok
    assert verdict.stage == "leverage"


def test_a_liq_price_far_from_the_prediction_is_refused():
    """A large drift means the position is not the one that was sized."""
    stop = ENTRY * (1 - 0.00325)
    predicted = ENTRY * (1 - 0.00625)
    actual = ENTRY * (1 - 0.010)        # further away, so the buffer test alone would pass
    verdict = verify_fill("BTC_USDT", 1, ENTRY, stop, actual, NOW,
                          predicted_liq_price=predicted)
    assert not verdict.ok
    assert verdict.stage == "liq_price_mismatch"
    assert verdict.action == "flatten"


def test_a_small_drift_from_the_prediction_is_tolerated():
    stop = ENTRY * (1 - 0.00325)
    verdict = verify_fill("BTC_USDT", 1, ENTRY, stop, ENTRY * (1 - 0.0064), NOW,
                          predicted_liq_price=ENTRY * (1 - 0.00625))
    assert verdict.ok


def test_verification_can_be_advisory_when_the_switch_is_off():
    """With verify_liq_price false the verdict still refuses but recommends nothing."""
    params = LiquidationParams(verify_liq_price=False)
    verdict = verify_fill("BTC_USDT", 1, ENTRY, ENTRY * (1 - 0.00325),
                          ENTRY * (1 - 0.005), NOW, params)
    assert not verdict.ok
    assert verdict.action == ""


# --- config wiring ---------------------------------------------------------

def test_params_from_shipped_config():
    p = LiquidationParams.from_config(load_config())
    assert p.leverage == 100
    assert p.max_leverage == 100
    assert p.margin_mode == "isolated"
    assert p.liquidation_buffer == 0.003
    assert p.taker_fee == 0.00075
    assert p.allow_margin_topup is False
    assert p.verify_liq_price is True
    assert p.tier_max_age_seconds == 3600
    assert p.liq_price_tolerance == 0.002


def test_the_tolerance_must_be_inside_the_buffer(monkeypatch, tmp_path):
    """A tolerance at or above the buffer would accept a liq_price that eats all of it."""
    from tests.test_config import _mutate

    with pytest.raises(ConfigError, match="liq_price_tolerance"):
        _mutate(monkeypatch, tmp_path, **{"protection.liq_price_tolerance": 0.003})


def test_the_tier_age_limit_must_be_positive(monkeypatch, tmp_path):
    from tests.test_config import _mutate

    with pytest.raises(ConfigError, match="risk_tier_max_age_seconds"):
        _mutate(monkeypatch, tmp_path, **{"protection.risk_tier_max_age_seconds": 0})


# --- no order path ---------------------------------------------------------

def test_the_guard_has_no_exchange_import():
    module = importlib.import_module("risk.liquidation_guard")
    source = Path(module.__file__).read_text(encoding="utf-8")
    imports = "\n".join(
        line for line in source.splitlines() if line.startswith(("import ", "from "))
    )
    assert "exchange" not in imports
    assert "execution" not in imports


def test_the_guard_cannot_close_anything_itself():
    """`action="flatten"` is a recommendation; Phase 10 acts on it."""
    import risk.liquidation_guard as guard

    forbidden = ("place", "submit", "cancel", "execute", "send")
    names = [n for n in dir(guard) if not n.startswith("_")]
    assert not [n for n in names if any(word in n.lower() for word in forbidden)]


def test_every_refusal_names_a_stage_and_explains_itself():
    refusals = [
        check(0.0040),
        check(0.0010, tiers=[]),
        check(0.0010, age=99_999.0),
        check(0.0010, notional=9_000_000.0),
        assess("BTC_USDT", 0, ENTRY, 64_000.0, 100_000.0, snapshot(), NOW),
    ]
    for verdict in refusals:
        assert isinstance(verdict, LiquidationVerdict)
        assert not verdict.ok
        assert not verdict           # falsy, so `if assess(...)` is safe
        assert verdict.stage
        assert verdict.reason
        assert "REFUSED" in verdict.summary()
