"""PHASE 8 tests — order execution and the stop-loss invariant.

The property these tests exist to protect:

    **A position may not exist without a verified stop-loss.**

So the ordering assertions matter as much as the outcomes: the stop is placed and
*confirmed by re-reading the exchange* before a single take-profit is submitted, and a stop
that cannot be confirmed flattens the position rather than leaving leveraged size exposed.

Nothing here touches the network. The simulated gateway is a peer of the live path, and a
separate set of tests asserts the live path cannot be reached while the safety gate is shut.
"""

from __future__ import annotations

import pytest

from config import load_config
from execution.order_manager import (
    ExecutionParams,
    OrderManager,
    OrderRecord,
    OrderState,
    SimulatedGateway,
    TEXT_KEY_RE,
    idempotency_key,
)
from execution.protection import (
    ProtectionEngine,
    ProtectionParams,
    RULE_ABOVE,
    RULE_BELOW,
    breakeven_price,
    ratchet,
    stop_trigger_rule,
    take_profit_ladder,
    trailing_stop_price,
)

ENTRY = 65_000.0
SIZE = 1_174                       # what 0.25% of 10k risks on a 0.325% BTC stop
STOP = ENTRY * (1 - 0.00325)       # the Phase 6/7 ceiling on BTC at 100x


def gateway(**kwargs):
    kwargs.setdefault("last_price", ENTRY)
    return SimulatedGateway(**kwargs)


def manager(gw=None, **kwargs):
    params = ExecutionParams(
        entry_fill_timeout_seconds=kwargs.pop("timeout", 0.05),
        poll_interval_seconds=kwargs.pop("poll", 0.01),
        **kwargs,
    )
    return OrderManager(gw if gw is not None else gateway(), params)


def engine(om=None, **params):
    om = om if om is not None else manager()
    return ProtectionEngine(om, ProtectionParams(**params))


def names(gw):
    """The gateway calls in order — how the sequencing assertions are made."""
    return [name for name, _ in gw.calls]


async def filled_position(gw=None, size=SIZE):
    gw = gw if gw is not None else gateway()
    om = manager(gw)
    await om.submit_entry("BTC_USDT", size, str(ENTRY), 1)
    return gw, om


# --- idempotency keys ------------------------------------------------------

def test_keys_match_the_exchange_shape():
    for purpose in ("ent", "cls", "stp", "tp1"):
        key = idempotency_key(purpose, "BTC_USDT", 7)
        assert TEXT_KEY_RE.match(key), key


def test_keys_are_stable_for_the_same_nonce_and_differ_across_nonces():
    """Stability is the point: a retry must reuse the id so the exchange can dedupe."""
    assert idempotency_key("ent", "BTC_USDT", 7) == idempotency_key("ent", "BTC_USDT", 7)
    assert idempotency_key("ent", "BTC_USDT", 7) != idempotency_key("ent", "BTC_USDT", 8)
    assert idempotency_key("ent", "BTC_USDT", 7) != idempotency_key("stp", "BTC_USDT", 7)
    assert idempotency_key("ent", "BTC_USDT", 7) != idempotency_key("ent", "ETH_USDT", 7)


def test_an_unusable_nonce_is_rejected_rather_than_truncated():
    with pytest.raises(ValueError):
        idempotency_key("ent", "BTC_USDT", "x" * 40)


async def test_a_duplicate_key_returns_the_original_order():
    """The behaviour the key buys: resubmitting cannot open a second position."""
    gw = gateway()
    first = await gw.place_order("BTC_USDT", 10, price="64000", text="t-dup1")
    second = await gw.place_order("BTC_USDT", 10, price="64000", text="t-dup1")
    assert first["id"] == second["id"]
    assert len(gw.orders) == 1


# --- the entry -------------------------------------------------------------

async def test_a_filled_entry_is_reported_from_the_exchange_not_the_response():
    gw = gateway()
    om = manager(gw)
    record = await om.submit_entry("BTC_USDT", SIZE, str(ENTRY), 1)
    assert record.state is OrderState.FILLED
    assert record.filled_size == SIZE
    assert record.average_price == ENTRY
    # The verdict came from a re-read, not from the submit response.
    assert "get_order" in names(gw)


async def test_an_unfilled_post_only_entry_expires_and_is_not_an_error():
    """Post-only frequently does not fill. That is the design working, not a failure."""
    gw = gateway(last_price=ENTRY)
    om = manager(gw)
    record = await om.submit_entry("BTC_USDT", SIZE, str(ENTRY * 0.998), 1)
    assert record.state is OrderState.EXPIRED
    assert record.filled_size == 0
    assert not record.state.has_exposure
    assert "did not fill" in record.reason
    assert await om.position_size("BTC_USDT") == 0


async def test_the_entry_is_cancelled_when_it_times_out():
    gw = gateway()
    om = manager(gw)
    await om.submit_entry("BTC_USDT", SIZE, str(ENTRY * 0.998), 1)
    assert "cancel_order" in names(gw)


async def test_an_entry_that_fills_during_the_cancel_race_is_reported_as_filled():
    """The cancel is not trusted: the order is re-read afterwards."""
    gw = gateway()

    original_cancel = gw.cancel_order

    async def fill_then_cancel(order_id):
        gw.advance(ENTRY * 0.998)      # fills the resting buy just before the cancel lands
        return await original_cancel(order_id)

    gw.cancel_order = fill_then_cancel
    om = manager(gw)
    record = await om.submit_entry("BTC_USDT", SIZE, str(ENTRY * 0.998), 1)
    assert record.state is OrderState.FILLED
    assert record.state.has_exposure


async def test_an_unreadable_order_is_unknown_and_counts_as_exposure():
    """UNKNOWN is not REJECTED: we cannot prove the size does not exist."""
    gw = gateway()
    om = manager(gw)

    async def explode(order_id):
        raise RuntimeError("network went away")

    gw.get_order = explode
    record = await om.read_order("999", "BTC_USDT", SIZE)
    assert record.state is OrderState.UNKNOWN
    assert record.state.has_exposure
    assert "could not re-read" in record.reason


@pytest.mark.parametrize("status,finish_as,left,expected", [
    ({"status": "finished", "finish_as": "filled"}, None, 0, OrderState.FILLED),
    ({"status": "open", "finish_as": ""}, None, 100, OrderState.OPEN),
    ({"status": "open", "finish_as": ""}, None, 40, OrderState.PARTIALLY_FILLED),
    ({"status": "finished", "finish_as": "cancelled"}, None, 100, OrderState.CANCELLED),
    ({"status": "finished", "finish_as": "cancelled"}, None, 40, OrderState.PARTIALLY_FILLED),
    ({"status": "finished", "finish_as": "_new"}, None, 100, OrderState.CANCELLED),
    ({"status": "weird", "finish_as": ""}, None, 100, OrderState.UNKNOWN),
    ({"status": "finished", "finish_as": "who_knows"}, None, 100, OrderState.UNKNOWN),
])
def test_exchange_statuses_map_onto_the_state_machine(status, finish_as, left, expected):
    raw = dict(status, id="1", contract="BTC_USDT", size=100, left=left)
    record = OrderManager(gateway()).record_from_raw(raw, "BTC_USDT", 100)
    assert record.state is expected


def test_partial_fills_report_signed_sizes_for_both_directions():
    om = OrderManager(gateway())
    long = om.record_from_raw(
        {"id": "1", "contract": "BTC_USDT", "size": 100, "left": 40, "status": "open"},
        "BTC_USDT", 100)
    short = om.record_from_raw(
        {"id": "2", "contract": "BTC_USDT", "size": -100, "left": -40, "status": "open"},
        "BTC_USDT", -100)
    assert long.filled_size == 60 and long.remaining == 40
    assert short.filled_size == -60 and short.remaining == -40


async def test_a_zero_size_entry_is_refused():
    with pytest.raises(ValueError):
        await manager().submit_entry("BTC_USDT", 0, str(ENTRY), 1)


# --- the stop-loss invariant ----------------------------------------------

async def test_the_stop_is_placed_and_verified_before_any_take_profit():
    """The ordering *is* the invariant, so it is asserted on the call log."""
    gw, om = await filled_position()
    gw.calls.clear()
    result = await engine(om).protect("BTC_USDT", 1, ENTRY, STOP, SIZE, 7)
    assert result.ok and result.verified

    sequence = names(gw)
    first_trigger = sequence.index("place_price_trigger_order")
    verification = sequence.index("list_price_orders")
    later_triggers = [
        i for i, name in enumerate(sequence)
        if name == "place_price_trigger_order" and i > first_trigger
    ]
    assert verification > first_trigger, "the stop must be re-read after being placed"
    assert later_triggers, "take-profits should follow"
    assert min(later_triggers) > verification, "no TP may precede stop verification"


async def test_the_stop_uses_mark_price_and_a_market_order():
    gw, om = await filled_position()
    await engine(om).protect("BTC_USDT", 1, ENTRY, STOP, SIZE, 7)
    stop = next(kw for name, kw in gw.calls if name == "place_price_trigger_order")
    assert stop["price_type"] == 1               # mark, the series liquidation uses
    assert stop["reduce_only"] is True
    placed = next(iter(gw.price_orders.values()))
    assert placed["initial"]["price"] == "0"     # market: a stop that does not fill is not a stop


@pytest.mark.parametrize("direction,rule", [(1, RULE_BELOW), (-1, RULE_ABOVE)])
def test_the_trigger_rule_matches_the_side(direction, rule):
    """A backwards rule produces an order that can never fire but lists as protection."""
    assert stop_trigger_rule(direction) == rule


async def test_the_stop_size_is_opposite_the_position():
    gw, om = await filled_position()
    await engine(om).protect("BTC_USDT", 1, ENTRY, STOP, SIZE, 7)
    stop = next(kw for name, kw in gw.calls if name == "place_price_trigger_order")
    assert stop["size"] == -SIZE


async def test_an_unverifiable_stop_flattens_the_position():
    """The whole point: exposure without a confirmed stop is closed, not tolerated."""
    gw, om = await filled_position()

    async def accept_but_never_list(*args, **kwargs):
        return {"id": "ghost", "status": "open"}

    gw.place_price_trigger_order = accept_but_never_list
    result = await engine(om, sl_retry_attempts=3).protect("BTC_USDT", 1, ENTRY, STOP, SIZE, 7)

    assert not result.ok
    assert result.flattened
    assert result.attempts == 3
    assert "no verified stop-loss" in result.reason
    assert await om.position_size("BTC_USDT") == 0
    assert "FLATTENED" in result.summary()


async def test_a_stop_that_fails_to_place_is_retried_then_flattens():
    gw, om = await filled_position()
    attempts = []

    async def always_fail(*args, **kwargs):
        attempts.append(kwargs.get("text"))
        raise RuntimeError("exchange said no")

    gw.place_price_trigger_order = always_fail
    result = await engine(om, sl_retry_attempts=2).protect("BTC_USDT", 1, ENTRY, STOP, SIZE, 7)
    assert len(attempts) == 2
    assert len(set(attempts)) == 2, "each retry needs its own client id"
    assert result.flattened
    assert await om.position_size("BTC_USDT") == 0


async def test_disabling_emergency_close_reports_an_unprotected_position_loudly():
    gw, om = await filled_position()

    async def accept_but_never_list(*args, **kwargs):
        return {"id": "ghost", "status": "open"}

    gw.place_price_trigger_order = accept_but_never_list
    result = await engine(om, emergency_close_on_sl_failure=False).protect(
        "BTC_USDT", 1, ENTRY, STOP, SIZE, 7)
    assert not result.ok
    assert not result.flattened
    assert result.stage == "unprotected"
    assert "UNPROTECTED" in result.summary()
    assert await om.position_size("BTC_USDT") == SIZE      # untouched, as configured


async def test_the_dead_man_switch_is_armed_first():
    gw, om = await filled_position()
    gw.calls.clear()
    await engine(om).protect("BTC_USDT", 1, ENTRY, STOP, SIZE, 7)
    assert names(gw)[0] == "countdown_cancel_all"
    assert gw.countdown_seconds == 60


async def test_a_stop_on_the_wrong_side_of_entry_is_refused():
    _, om = await filled_position()
    with pytest.raises(ValueError, match="wrong side"):
        await engine(om).protect("BTC_USDT", 1, ENTRY, ENTRY * 1.01, SIZE, 7)
    with pytest.raises(ValueError, match="wrong side"):
        await engine(om).protect("BTC_USDT", -1, ENTRY, ENTRY * 0.99, -SIZE, 7)


async def test_a_failed_take_profit_does_not_undo_a_verified_stop():
    """A missing TP costs upside; a missing stop costs the account. They are not equal."""
    gw, om = await filled_position()
    calls = {"n": 0}
    original = gw.place_price_trigger_order

    async def fail_after_the_stop(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("TP rejected")
        return await original(*args, **kwargs)

    gw.place_price_trigger_order = fail_after_the_stop
    result = await engine(om).protect("BTC_USDT", 1, ENTRY, STOP, SIZE, 7)
    assert result.ok and result.verified
    assert all(leg.order_id == "" for leg in result.take_profits)
    assert await om.position_size("BTC_USDT") == SIZE


# --- the take-profit ladder ------------------------------------------------

def test_the_ladder_sizes_sum_to_exactly_the_position():
    legs = take_profit_ladder(ENTRY, STOP, 1, SIZE, ProtectionParams())
    assert sum(abs(leg.size) for leg in legs) == SIZE
    assert [leg.name for leg in legs] == ["tp1", "tp2", "tp3"]
    assert all(leg.size < 0 for leg in legs)      # reduce-only, opposite a long


def test_the_ladder_is_measured_in_r_from_the_actual_stop():
    risk = ENTRY - STOP
    legs = take_profit_ladder(ENTRY, STOP, 1, SIZE, ProtectionParams())
    for leg in legs:
        assert leg.price == pytest.approx(ENTRY + leg.r_multiple * risk)


def test_a_capped_stop_shrinks_the_targets_with_it():
    """R is the real risk, so a stop capped by the liquidation ceiling moves the targets."""
    wide = take_profit_ladder(ENTRY, ENTRY * (1 - 0.01), 1, SIZE, ProtectionParams())
    tight = take_profit_ladder(ENTRY, ENTRY * (1 - 0.00125), 1, SIZE, ProtectionParams())
    assert tight[0].price < wide[0].price


def test_the_short_ladder_mirrors_the_long_one():
    long = take_profit_ladder(ENTRY, ENTRY * 0.99, 1, SIZE, ProtectionParams())
    short = take_profit_ladder(ENTRY, ENTRY * 1.01, -1, -SIZE, ProtectionParams())
    for a, b in zip(long, short):
        assert a.price - ENTRY == pytest.approx(ENTRY - b.price)
        assert a.size < 0 < b.size


def test_a_position_too_small_to_split_still_gets_a_ladder():
    legs = take_profit_ladder(ENTRY, STOP, 1, 2, ProtectionParams())
    assert sum(abs(leg.size) for leg in legs) == 2


def test_the_runner_takes_the_remainder_so_nothing_is_over_closed():
    for size in range(1, 60):
        legs = take_profit_ladder(ENTRY, STOP, 1, size, ProtectionParams())
        assert sum(abs(leg.size) for leg in legs) == size, size


def test_a_zero_width_stop_cannot_express_r():
    with pytest.raises(ValueError, match="positive"):
        take_profit_ladder(ENTRY, ENTRY, 1, SIZE, ProtectionParams())


# --- break-even, trailing, the ratchet ------------------------------------

def test_breakeven_is_padded_past_entry_by_the_fee_buffer():
    """Moving the stop to literal entry is a small guaranteed loss, not a free trade."""
    long = breakeven_price(ENTRY, 1, 0.0009)
    short = breakeven_price(ENTRY, -1, 0.0009)
    assert long > ENTRY and short < ENTRY
    assert long == pytest.approx(ENTRY * 1.0009)
    assert short == pytest.approx(ENTRY * 0.9991)


def test_the_fee_buffer_covers_the_round_trip():
    """§12 needs ~0.085% of movement before break-even means anything."""
    cfg = load_config()
    buffer = cfg.get("protection.breakeven_fee_buffer")
    round_trip = cfg.get("backtest.fee_taker") + abs(cfg.get("backtest.fee_maker"))
    assert buffer >= round_trip


@pytest.mark.parametrize("direction", [1, -1])
def test_trailing_sits_an_atr_multiple_behind_price(direction):
    stop = trailing_stop_price(ENTRY, direction, 100.0, 1.5)
    assert abs(ENTRY - stop) == pytest.approx(150.0)
    assert (stop < ENTRY) if direction > 0 else (stop > ENTRY)


@pytest.mark.parametrize("direction,existing,candidate,expected", [
    (1, 64_000.0, 64_500.0, 64_500.0),      # tighter: accepted
    (1, 64_500.0, 64_000.0, 64_500.0),      # looser: refused
    (-1, 66_000.0, 65_500.0, 65_500.0),
    (-1, 65_500.0, 66_000.0, 65_500.0),
])
def test_the_ratchet_never_loosens_a_stop(direction, existing, candidate, expected):
    """Giving a losing trade room is how 0.25% of risk becomes 3%."""
    assert ratchet(existing, candidate, direction) == expected


async def test_moving_a_stop_places_the_new_one_before_cancelling_the_old():
    """Cancel-first would open a gap with no protection at all."""
    gw, om = await filled_position()
    protection = engine(om)
    result = await protection.protect("BTC_USDT", 1, ENTRY, STOP, SIZE, 7)
    gw.calls.clear()

    moved = await protection.move_to_breakeven(
        "BTC_USDT", 1, ENTRY, result.stop_order_id, SIZE, 8, existing_stop=STOP)
    assert moved.ok
    sequence = names(gw)
    assert sequence.index("place_price_trigger_order") < sequence.index("cancel_price_order")
    assert moved.stop_price == pytest.approx(breakeven_price(ENTRY, 1, 0.0009))


async def test_a_loosening_move_is_a_no_op():
    gw, om = await filled_position()
    protection = engine(om)
    result = await protection.protect("BTC_USDT", 1, ENTRY, STOP, SIZE, 7)
    gw.calls.clear()

    moved = await protection.move_stop("BTC_USDT", 1, ENTRY * 0.90,
                                       result.stop_order_id, SIZE, 9, existing_stop=STOP)
    assert moved.ok
    assert moved.stage == "unchanged"
    assert moved.stop_price == STOP
    assert "place_price_trigger_order" not in names(gw)


async def test_a_failed_replacement_leaves_the_original_stop_alone():
    gw, om = await filled_position()
    protection = engine(om)
    result = await protection.protect("BTC_USDT", 1, ENTRY, STOP, SIZE, 7)

    async def refuse(*args, **kwargs):
        raise RuntimeError("no")

    gw.place_price_trigger_order = refuse
    moved = await protection.move_stop("BTC_USDT", 1, ENTRY, result.stop_order_id,
                                       SIZE, 9, existing_stop=STOP)
    assert not moved.ok
    assert moved.stage == "move_failed"
    assert moved.verified                                   # the old stop is still live
    assert result.stop_order_id in gw.price_orders
    assert gw.price_orders[result.stop_order_id]["status"] == "open"


async def test_a_failed_cancel_leaves_two_reduce_only_stops_and_says_so():
    gw, om = await filled_position()
    protection = engine(om)
    result = await protection.protect("BTC_USDT", 1, ENTRY, STOP, SIZE, 7)

    async def refuse(order_id):
        raise RuntimeError("cancel failed")

    gw.cancel_price_order = refuse
    moved = await protection.move_stop("BTC_USDT", 1, ENTRY * 0.999,
                                       result.stop_order_id, SIZE, 9, existing_stop=STOP)
    assert moved.ok
    assert moved.stage == "moved_stale"
    assert "reduce-only" in moved.reason


async def test_disabled_features_are_reported_not_silently_skipped():
    _, om = await filled_position()
    off = engine(om, move_to_breakeven=False, trailing_stop=False)
    assert (await off.move_to_breakeven("BTC_USDT", 1, ENTRY, "1", SIZE, 1)).stage == "disabled"
    assert (await off.trail("BTC_USDT", 1, ENTRY, 100.0, "1", SIZE, 1)).stage == "disabled"


# --- reconciliation --------------------------------------------------------

async def test_audit_reports_flat_when_nothing_is_open():
    result = await engine().audit("BTC_USDT")
    assert result.ok and result.stage == "flat"


async def test_audit_catches_size_with_no_protective_order():
    """The state that must never persist: exposure with nothing behind it."""
    gw, om = await filled_position()
    result = await ProtectionEngine(om, ProtectionParams()).audit("BTC_USDT")
    assert not result.ok
    assert result.stage == "unprotected"
    assert "no protective order" in result.reason


async def test_audit_confirms_a_protected_position():
    gw, om = await filled_position()
    protection = engine(om)
    await protection.protect("BTC_USDT", 1, ENTRY, STOP, SIZE, 7)
    result = await protection.audit("BTC_USDT")
    assert result.ok and result.verified
    assert result.stage == "protected"


# --- the simulated exchange behaves like the real one ---------------------

async def test_the_simulator_only_fills_a_resting_limit_when_price_reaches_it():
    """A simulator that fills everything would make post-only look free."""
    gw = gateway(last_price=ENTRY)
    await gw.place_order("BTC_USDT", 10, price=str(ENTRY * 0.99), text="t-rest1")
    assert await gw.get_position("BTC_USDT") == {**await gw.get_position("BTC_USDT")}
    assert int((await gw.get_position("BTC_USDT"))["size"]) == 0
    gw.advance(ENTRY * 0.99)
    assert int((await gw.get_position("BTC_USDT"))["size"]) == 10


async def test_a_triggered_stop_closes_the_position_in_the_simulator():
    gw, om = await filled_position()
    protection = engine(om)
    await protection.protect("BTC_USDT", 1, ENTRY, STOP, SIZE, 7)
    gw.advance(STOP)
    assert await om.position_size("BTC_USDT") == 0


async def test_the_simulated_liquidation_price_matches_the_phase_7_formula():
    gw, om = await filled_position()
    position = await gw.get_position("BTC_USDT")
    expected = ENTRY * (1 - ((1 / 100) - 0.003 - 0.00075))
    assert float(position["liq_price"]) == pytest.approx(expected)


async def test_closing_uses_reduce_only_so_it_cannot_open_a_reversal():
    gw, om = await filled_position()
    await om.close_position("BTC_USDT", 5, reason="test")
    close = next(kw for name, kw in gw.calls
                 if name == "place_order" and kw.get("close"))
    assert close["reduce_only"] is True
    assert close["close"] is True
    assert await om.position_size("BTC_USDT") == 0


# --- the safety gate -------------------------------------------------------

def test_a_dry_run_config_gets_the_simulator_not_the_client():
    """No live orders by default: the gate is shut, so the network is unreachable."""
    cfg = load_config()
    assert cfg.live_enabled is False
    om = OrderManager.for_config(cfg, client=object(), last_price=ENTRY)
    assert isinstance(om.gateway, SimulatedGateway)
    assert om.live is False


def test_live_enabled_without_a_client_still_simulates():
    """A wiring mistake must fail toward doing nothing."""
    class _Cfg:
        live_enabled = True

        def get(self, path, default=None):
            return default

    om = OrderManager.for_config(_Cfg())
    assert isinstance(om.gateway, SimulatedGateway)
    assert om.live is False


async def test_the_client_write_guard_is_the_second_barrier():
    """Even handed a real client with the gate shut, no request leaves the process."""
    from exchange.gate_client import GateFuturesClient, WriteBlocked

    cfg = load_config()
    client = GateFuturesClient(cfg, session=object())
    om = OrderManager(client, ExecutionParams(), live=True)
    with pytest.raises(WriteBlocked):
        await om.submit_entry("BTC_USDT", 1, str(ENTRY), 1)
    assert client.stats.writes_blocked == 1
    assert client.stats.requests == 0        # nothing reached the network


def test_execution_params_reject_unusable_values():
    for bad in ({"entry_tif": "vibes"}, {"entry_fill_timeout_seconds": 0},
                {"poll_interval_seconds": 0}, {"verify_attempts": 0}):
        with pytest.raises(ValueError):
            ExecutionParams(**bad)


def test_protection_params_reject_unusable_values():
    for bad in ({"sl_price_type": "vibes"}, {"sl_retry_attempts": 0},
                {"tp1_close_pct": 0.7, "tp2_close_pct": 0.4},
                {"tp1_r": 3.0, "tp2_r": 2.0}, {"dead_man_switch_seconds": 0}):
        with pytest.raises(ValueError):
            ProtectionParams(**bad)


def test_params_from_shipped_config():
    cfg = load_config()
    execution = ExecutionParams.from_config(cfg)
    protection = ProtectionParams.from_config(cfg)
    assert execution.entry_tif == "poc"                  # post-only, enforced at >=100x
    assert execution.entry_fill_timeout_seconds == 20
    assert protection.sl_price_type == "mark"
    assert protection.price_type == 1
    assert protection.sl_retry_attempts == 3
    assert protection.emergency_close_on_sl_failure is True
    assert protection.dead_man_switch_seconds == 60
    assert (protection.tp1_close_pct, protection.tp2_close_pct) == (0.40, 0.35)


def test_the_shipped_ladder_leaves_a_runner():
    protection = ProtectionParams.from_config(load_config())
    assert protection.tp1_close_pct + protection.tp2_close_pct < 1.0
