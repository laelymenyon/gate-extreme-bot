"""PHASE 15 tests — the live trading runner.

Phase 10 proved the layers hand off correctly; Phase 14 proved the repository can tell
whether it is *allowed* to trade. This phase is the first that can actually send an order,
so these tests are less about the strategy than about the conditions under which that
becomes possible — and, mostly, about the conditions under which it does not.

What is asserted here:

* **The gate is a construction-time refusal, not a branch.** :class:`LiveTrader` will not
  exist while the safety gate is shut, and will not exist holding a simulated gateway. Both
  are the mirror image of `PaperTrader`'s refusal, and neither has an override flag.
* **Preflight is load-bearing.** `run_live` refuses a NO-GO with exit code 3 and constructs
  no trader. A GO is assembled deliberately here (the same route `tests/test_preflight.py`
  uses) so that "preflight blocked it" cannot be the accidental reason a test passes.
* **Exit codes mean something**, because a supervisor reads them: 0 ran, 2 refused, 3
  NO-GO. Phase 12 made this claim about the CLI; it has to survive a mode that trades.
* **Live fills are recorded as live.** A paper row and a live row in the same database
  would corrupt both preflight's evidence and Phase 13's verdict.

The exchange is a fake throughout, and `conftest.py`'s autouse guard fails any test that
reaches the network regardless.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

import config as config_module
from database.models import EquityPoint, TradeStore
from exchange.gate_client import Contract, RiskTier
from execution.order_manager import SimulatedGateway
from execution.preflight import AccountSnapshot
from live.loop import LiveGateRefused, LiveReport, LiveTrader, run_live
from paper.loop import RestMarketSource
from paper.validation import ValidationParams, validate
from tests.test_validation import evidence as session_evidence
from tests.test_validation import record

NOW = 1_754_784_000.0
ENTRY = 65_000.0
WARMUP = 300

BTC_RAW = {
    "name": "BTC_USDT", "leverage_max": "200", "leverage_min": "1",
    "maintenance_rate": "0.003", "quanto_multiplier": "0.0001",
    "order_size_min": 1, "order_size_max": 12000000,
    "order_price_round": "0.1", "mark_price_round": "0.01",
    "taker_fee_rate": "0.00075", "maker_fee_rate": "-0.0001",
    "risk_limit_base": "500000", "in_delisting": False, "status": "trading",
}
BTC = Contract.from_api(BTC_RAW)
TIER_RAW = {
    "tier": 1, "risk_limit": "500000", "initial_rate": "0.005",
    "maintenance_rate": "0.003", "leverage_max": "200", "deduction": "0",
}
TIERS = [RiskTier.from_api(TIER_RAW)]


# --- fixtures and fakes -----------------------------------------------------

@pytest.fixture
def store(tmp_path):
    return TradeStore(tmp_path / "trades.db")


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    """Point `database.path` at a scratch file for every test in this module.

    Not optional here. Unlike `PaperTrader`, which defaults to a `MemoryRiskStore`,
    `LiveTrader` builds a `SqliteRiskStore` from `database.path` — correct for live trading,
    where a latched kill-switch has to outlive the process, but it means an un-isolated test
    reads *and latches breakers in* the repository's real `data/trades.db`. A suite that
    writes to the artefact it is testing passes for the wrong reason, and here it would also
    leave a tripped kill-switch behind for the next real run.
    """
    real_load = config_module.load_config

    def scoped(*args, **kwargs):
        cfg = real_load(*args, **kwargs)
        cfg.raw["database"] = dict(cfg.raw["database"], path=str(tmp_path / "trades.db"))
        return cfg

    monkeypatch.setattr(config_module, "load_config", scoped)
    return tmp_path / "trades.db"


def live_cfg(monkeypatch, *, dry_run=False, mode="live", confirm=True, creds=True):
    """A real Config with the three switches set explicitly.

    The entry timings are compressed. `OrderManager` polls a resting entry with real
    `asyncio.sleep`, and `LiveTrader` builds it from config rather than accepting an
    injected clock, so config is the seam that keeps a 20-second production timeout from
    becoming a 20-second test. Nothing else about the order semantics is touched — in
    particular `entry_tif` stays `poc`, which preflight checks.
    """
    monkeypatch.setattr(config_module, "ENV_PATH", config_module.ROOT / ".env.absent")
    monkeypatch.setenv("DRY_RUN", "true" if dry_run else "false")
    if creds:
        monkeypatch.setenv("GATE_API_KEY", "k" * 24)
        monkeypatch.setenv("GATE_API_SECRET", "s" * 24)
    else:
        monkeypatch.delenv("GATE_API_KEY", raising=False)
        monkeypatch.delenv("GATE_API_SECRET", raising=False)
    cfg = config_module.load_config(run_mode=mode, confirm_live=confirm)
    cfg.raw["take_profit"] = dict(cfg.raw["take_profit"],
                                  entry_fill_timeout_seconds=0.05)
    cfg.raw["execution"] = dict(cfg.raw.get("execution", {}),
                                poll_interval_seconds=0.01)
    return cfg


def rows(close, interval=60.0):
    """Candlestick payloads in the shape `Candles.from_gate` expects."""
    close = np.asarray(close, dtype=float)
    open_ = np.concatenate([[close[0]], close[:-1]])
    return [
        {"t": i * interval, "o": o, "h": max(o, c) * 1.0008,
         "l": min(o, c) * 0.9992, "c": c, "v": 1000.0}
        for i, (o, c) in enumerate(zip(open_, close))
    ]


def quiet(n=WARMUP + 20, start=ENTRY, sigma=0.0003, seed=2):
    rng = np.random.default_rng(seed)
    return start * np.exp(np.cumsum(rng.normal(0, sigma, n)))


class Signal:
    """Minimal stand-in for a Phase 5 Signal."""

    def __init__(self, direction=1, score=88.0, accepted=True, stage="accepted"):
        self.direction = direction
        self.score = score
        self.accepted = accepted
        self.stage = stage


def never(**kwargs):
    return None


def once(direction=1, at=1):
    state = {"n": 0}

    def decide(**kwargs):
        state["n"] += 1
        return Signal(direction) if state["n"] == at else None

    return decide


class FakeClient:
    """A live-shaped exchange. Records every call; opens no socket.

    Deliberately *not* a `SimulatedGateway` subclass — the runner refuses that type by
    name, and a fake that inherited it could not reach the code under test. This composes
    one instead, so order mechanics stay the Phase 8 implementation rather than a
    reimplementation that could agree with a bug.
    """

    def __init__(self, *, total=10_000.0, available=9_900.0, candles=None,
                 leverage_error=None, fills_entries=False):
        self.sim = SimulatedGateway(last_price=ENTRY, leverage=100)
        self.calls: list[str] = []
        self.total = total
        self.available = available
        self._candles = candles if candles is not None else rows(quiet())
        self._leverage_error = leverage_error
        self._fills_entries = fills_entries
        self.leverage_set: list[tuple[str, int]] = []
        self.entered = False
        self.exited = False

    # --- reads ---
    async def get_account(self):
        self.calls.append("get_account")
        return {"currency": "USDT", "total": str(self.total),
                "available": str(self.available), "unrealised_pnl": "0"}

    async def list_positions(self, holding=True):
        self.calls.append("list_positions")
        held = self.sim.positions.get("BTC_USDT")
        return [held] if held and int(held.get("size", 0)) else []

    async def list_open_orders(self, symbol=None):
        self.calls.append("list_open_orders")
        return [o for o in self.sim.orders.values() if o["status"] == "open"]

    async def get_contract(self, symbol, refresh=False):
        self.calls.append("get_contract")
        return BTC

    async def get_risk_tiers(self, symbol, refresh=False):
        self.calls.append("get_risk_tiers")
        return TIERS

    async def get_candlesticks(self, symbol, interval, limit):
        self.calls.append(f"get_candlesticks:{interval}")
        return self._candles

    async def get_ticker(self, symbol):
        self.calls.append("get_ticker")
        return {"contract": symbol, "last": str(self.sim.last_price),
                "mark_price": str(self.sim.last_price), "volume_24h_quote": "1000000"}

    async def set_leverage(self, symbol, leverage):
        self.calls.append("set_leverage")
        if self._leverage_error is not None:
            raise self._leverage_error
        self.leverage_set.append((symbol, int(leverage)))
        return {}

    # --- order gateway surface, delegated to the Phase 8 simulator ---
    async def place_order(self, symbol, size, **kwargs):
        self.calls.append("place_order")
        raw = await self.sim.place_order(symbol, size, **kwargs)
        if self._fills_entries and not kwargs.get("reduce_only") \
                and not kwargs.get("close"):
            # A post-only entry rests until the market trades through it. The fake market
            # never moves on its own, so nudge it to the limit — the simulator's own fill
            # rule then decides, rather than this fake asserting a fill into existence.
            self.sim.advance(float(raw["price"]))
        return raw

    async def get_order(self, order_id):
        return await self.sim.get_order(order_id)

    async def cancel_order(self, order_id):
        self.calls.append("cancel_order")
        return await self.sim.cancel_order(order_id)

    async def place_price_trigger_order(self, symbol, **kwargs):
        self.calls.append("place_price_trigger_order")
        return await self.sim.place_price_trigger_order(symbol, **kwargs)

    async def list_price_orders(self, symbol=None):
        return await self.sim.list_price_orders(symbol)

    async def cancel_price_order(self, order_id):
        return await self.sim.cancel_price_order(order_id)

    async def get_position(self, symbol):
        return await self.sim.get_position(symbol)

    async def countdown_cancel_all(self, timeout_seconds, symbol=None):
        self.calls.append("countdown_cancel_all")
        return await self.sim.countdown_cancel_all(timeout_seconds, symbol)

    # --- lifecycle ---
    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc):
        self.exited = True
        return False


def healthy_account(**overrides):
    fields = dict(reachable=True, currency="USDT", total=10_000.0, available=9_900.0,
                  open_positions=0, open_orders=0, leverage=100, margin_mode="")
    fields.update(overrides)
    return AccountSnapshot(**fields)


def passing_validation(count=1000):
    trades = [record(pnl=48.8, r_multiple=1.9) for _ in range(count)]
    curve = [EquityPoint(NOW + i, 10_000.0 + 48.8 * i) for i in range(count + 1)]
    report = validate(
        session_evidence(equity=10_000.0 + 48.8 * count), trades, curve,
        params=ValidationParams(min_trades=count),
    )
    assert report.validated, "fixture must be a passing validation"
    return report


def profitable_backtest(store, count=1000):
    for index in range(count):
        store.record_trade(record(timestamp=NOW + index, pnl=48.8, r_multiple=1.9,
                                  mode="backtest"))


def go_kwargs(store):
    """Everything preflight needs to reach GO — the one route, assembled deliberately."""
    profitable_backtest(store)
    return dict(store=store, account=healthy_account(), validation=passing_validation())


def trader(cfg, client=None, *, decide=None, store=None, equity=10_000.0):
    client = client or FakeClient()
    source = RestMarketSource(client, timeframes=("1m",))
    asyncio.run(source.refresh("BTC_USDT"))
    return LiveTrader(
        cfg, client, source, "BTC_USDT", TIERS, BTC,
        starting_equity=equity, store=store,
        decide=decide if decide is not None else never,
    )


def call_run_live(cfg, **kwargs):
    """Invoke the runner with printing suppressed, returning (code, report, preflight)."""
    kwargs.setdefault("print_fn", lambda *a, **k: None)
    kwargs.setdefault("poll_seconds", 0)
    return asyncio.run(run_live(cfg, **kwargs))


# --- it cannot run behind a shut gate --------------------------------------

@pytest.mark.parametrize("dry_run,mode,confirm", [
    (True, "live", True), (False, "paper", True), (False, "live", False),
    (True, "paper", False),
])
def test_a_shut_gate_refuses_to_construct(monkeypatch, dry_run, mode, confirm):
    """The mirror of `PaperTrader`'s refusal: the object will not exist.

    Seven of eight switch combinations are shut, and none of them may produce a trader.
    """
    cfg = live_cfg(monkeypatch, dry_run=dry_run, mode=mode, confirm=confirm)
    assert not cfg.live_enabled
    with pytest.raises(LiveGateRefused, match="CLOSED"):
        trader(cfg)


def test_the_live_trader_has_no_switch_to_force_the_gate(monkeypatch):
    """A constructor keyword that opened the gate would defeat the refusal above."""
    import inspect

    parameters = inspect.signature(LiveTrader.__init__).parameters
    for suspicious in ("live", "live_enabled", "force", "confirm_live", "dry_run",
                       "simulate", "paper"):
        assert suspicious not in parameters, (
            f"LiveTrader accepts {suspicious!r}; the refusal is meant to be unconditional"
        )


def test_a_missing_client_is_refused(monkeypatch):
    cfg = live_cfg(monkeypatch)
    source = RestMarketSource(FakeClient(), timeframes=("1m",))
    with pytest.raises(LiveGateRefused, match="requires a live exchange client"):
        LiveTrader(cfg, None, source, "BTC_USDT", TIERS, BTC, starting_equity=10_000.0)


def test_a_simulated_gateway_is_refused_even_with_the_gate_open(monkeypatch):
    """The gate being open is not enough: the runner must have resolved to a real gateway.

    `OrderManager.for_config` falls back to the simulator whenever a client is missing, so
    "live_enabled but simulating" is a reachable wiring mistake. It must not be a silent one
    — a run that reported live fills from an in-process simulator is the failure mode this
    phase most needs to exclude.
    """
    cfg = live_cfg(monkeypatch)
    client = FakeClient()
    source = RestMarketSource(client, timeframes=("1m",))
    asyncio.run(source.refresh("BTC_USDT"))

    import live.loop as live_loop

    real_for_config = live_loop.OrderManager.for_config

    def simulating(cls_cfg, client=None, params=None, **kwargs):
        # Force the fallback the wiring mistake would produce.
        return real_for_config(cls_cfg, client=None, params=params, **kwargs)

    monkeypatch.setattr(live_loop.OrderManager, "for_config", simulating)
    with pytest.raises(LiveGateRefused, match="simulator"):
        LiveTrader(cfg, client, source, "BTC_USDT", TIERS, BTC, starting_equity=10_000.0)


# --- run_live refuses before it reaches an order ---------------------------

def test_a_shut_gate_exits_two_and_builds_no_client(monkeypatch, store):
    """Exit 2 is "you did not open the gate", and nothing was constructed to find out."""
    cfg = live_cfg(monkeypatch, dry_run=True)
    assert not cfg.live_enabled

    class ExplodingClient:
        def __init__(self, *a, **k):
            raise AssertionError("run_live constructed a client behind a shut gate")

    monkeypatch.setattr("exchange.gate_client.GateFuturesClient", ExplodingClient)
    code, report, pf = call_run_live(cfg, store=store)
    assert code == 2
    assert report is None
    assert not pf.ready


def test_empty_credentials_are_refused_with_the_documented_code(monkeypatch, store):
    """The gate can only be open with credentials present, so this is a defensive path.

    `load_config` refuses to build this combination at all, so it is assembled directly.
    It still has to honour the documented contract: `run_live` returns a triple, and an
    exception here would escape a caller that is only handling exit codes.
    """
    import dataclasses

    from config import Credentials

    cfg = dataclasses.replace(live_cfg(monkeypatch), credentials=Credentials("", ""))
    assert cfg.live_enabled
    assert not cfg.credentials.present

    code, report, pf = call_run_live(cfg, store=store)
    assert code == 2
    assert report is None
    assert not pf.ready


def test_a_preflight_no_go_refuses_with_exit_three(monkeypatch, store):
    """The database is empty, so preflight blocks — and no order may be attempted."""
    cfg = live_cfg(monkeypatch)
    client = FakeClient()
    code, report, pf = call_run_live(
        cfg, client=client, store=store, account=healthy_account(), steps=1,
    )
    assert code == 3
    assert report is None
    assert not pf.ready
    assert "place_order" not in client.calls


def test_a_no_go_is_refused_even_when_the_strategy_would_have_signalled(monkeypatch, store):
    """Preflight sits in front of the decision chain, not alongside it."""
    cfg = live_cfg(monkeypatch)
    client = FakeClient()
    code, _report, _pf = call_run_live(
        cfg, client=client, store=store, account=healthy_account(),
        steps=1, decide=once(),
    )
    assert code == 3
    assert "place_order" not in client.calls


def test_an_unfunded_account_refuses_to_start(monkeypatch, store):
    """Zero equity would size every position against nothing."""
    cfg = live_cfg(monkeypatch)
    client = FakeClient()
    kwargs = go_kwargs(store)
    kwargs["account"] = healthy_account(total=0.0, available=0.0)
    code, report, _pf = call_run_live(cfg, client=client, steps=1, **kwargs)
    assert code == 3
    assert report is None
    assert "place_order" not in client.calls


def test_an_unreachable_account_is_a_no_go(monkeypatch, store):
    cfg = live_cfg(monkeypatch)
    kwargs = go_kwargs(store)
    kwargs["account"] = AccountSnapshot.unreachable("connection reset")
    code, report, pf = call_run_live(cfg, client=FakeClient(), steps=1, **kwargs)
    assert code == 3
    assert report is None
    assert not pf.ready


# --- the one path that starts ----------------------------------------------

def test_a_go_starts_the_loop_and_exits_zero(monkeypatch, store):
    """The whole point of the phase: every condition met, and the runner runs."""
    cfg = live_cfg(monkeypatch)
    client = FakeClient()
    code, report, pf = call_run_live(
        cfg, client=client, steps=3, **go_kwargs(store),
    )
    assert pf.ready, pf.verdict()
    assert code == 0
    assert isinstance(report, LiveReport)
    assert report.started is True
    assert report.preflight_ready is True
    assert report.steps == 3
    assert report.stop_reason == "completed"


def test_the_runner_reads_the_contract_and_tiers_from_the_exchange(monkeypatch, store):
    """Sizing and the liquidation guard are only as correct as these two reads."""
    cfg = live_cfg(monkeypatch)
    client = FakeClient()
    code, _report, _pf = call_run_live(cfg, client=client, steps=1, **go_kwargs(store))
    assert code == 0
    assert "get_contract" in client.calls
    assert "get_risk_tiers" in client.calls


def test_the_runner_does_not_close_a_client_it_was_given(monkeypatch, store):
    """Ownership matters: closing a caller's client would break a supervising process."""
    cfg = live_cfg(monkeypatch)
    client = FakeClient()
    call_run_live(cfg, client=client, steps=1, **go_kwargs(store))
    assert client.entered is False
    assert client.exited is False


def test_equity_is_taken_from_the_exchange_not_from_config(monkeypatch, store):
    """A live account's size is a fact to be read, never a number carried from a paper run."""
    cfg = live_cfg(monkeypatch)
    kwargs = go_kwargs(store)
    kwargs["account"] = healthy_account(total=4_242.0, available=4_200.0)
    _code, report, _pf = call_run_live(
        cfg, client=FakeClient(total=4_242.0, available=4_200.0), steps=1, **kwargs,
    )
    assert report.starting_equity == pytest.approx(4_242.0)


# --- the step's veto chain --------------------------------------------------

def test_an_unsignalled_step_does_nothing(monkeypatch):
    """The loop's normal state. No signal means no order, and it is counted, not logged."""
    cfg = live_cfg(monkeypatch)
    client = FakeClient()
    bot = trader(cfg, client, decide=never)
    report = asyncio.run(bot.run(steps=2, poll_seconds=0))
    assert report.entries_attempted == 0
    assert "place_order" not in client.calls
    assert report.rejections.get("no_signal") == 2


def test_a_tripped_breaker_stops_the_step_before_the_signal(monkeypatch, tmp_path):
    """A latched kill-switch outlives the process; the live loop must honour it."""
    from risk.risk_manager import Breaker, KillSwitch, SqliteRiskStore

    cfg = live_cfg(monkeypatch)
    risk_store = SqliteRiskStore(str(tmp_path / "trades.db"))
    risk_store.trip(KillSwitch(
        breaker=Breaker.MANUAL, tripped_at=NOW, reason="halted by an operator",
        manual_reset_required=True,
    ))

    client = FakeClient()
    source = RestMarketSource(client, timeframes=("1m",))
    asyncio.run(source.refresh("BTC_USDT"))
    bot = LiveTrader(
        cfg, client, source, "BTC_USDT", TIERS, BTC,
        starting_equity=10_000.0, risk_store=risk_store, decide=once(),
    )
    report = asyncio.run(bot.run(steps=1, poll_seconds=0))
    assert report.entries_attempted == 0
    assert "place_order" not in client.calls


def test_a_failed_set_leverage_aborts_the_entry(monkeypatch):
    """Entering at the wrong leverage would invalidate every liquidation number."""
    cfg = live_cfg(monkeypatch)
    client = FakeClient(leverage_error=RuntimeError("margin mode is locked"))
    bot = trader(cfg, client, decide=once())
    report = asyncio.run(bot.run(steps=1, poll_seconds=0))
    assert report.rejections.get("leverage") == 1
    assert "place_order" not in client.calls


def test_leverage_is_set_once_not_per_entry(monkeypatch):
    cfg = live_cfg(monkeypatch)
    client = FakeClient()
    bot = trader(cfg, client, decide=lambda **kw: Signal(1))
    asyncio.run(bot.run(steps=3, poll_seconds=0))
    assert len(client.leverage_set) <= 1


# --- live evidence stays live ----------------------------------------------

def test_a_closed_trade_is_recorded_as_live(monkeypatch, store):
    """A live fill written as `paper` would corrupt preflight's own evidence.

    Phase 13 grades paper history and Phase 14 reads that grade to authorise live trading.
    A live row misfiled as paper would let a live run's results authorise the next one.
    """
    cfg = live_cfg(monkeypatch)
    client = FakeClient(fills_entries=True)
    bot = trader(cfg, client, decide=once(), store=store)

    # Drive a full round trip through the Phase 8 simulator behind the fake client.
    asyncio.run(bot.step())
    assert bot.open_position is not None, "the entry should have filled"

    client.sim.advance(bot.open_position["stop_price"] * 0.999)
    asyncio.run(bot.step())

    recorded = store.trades()
    assert recorded, "a completed trade should have been persisted"
    assert {row.mode for row in recorded} == {"live"}
    assert not store.trades(mode="paper")


def test_the_equity_curve_is_marked_live(monkeypatch, store):
    cfg = live_cfg(monkeypatch)
    client = FakeClient(fills_entries=True)
    bot = trader(cfg, client, decide=once(), store=store)
    asyncio.run(bot.step())
    assert bot.open_position is not None, "the entry should have filled"
    client.sim.advance(bot.open_position["stop_price"] * 0.999)
    asyncio.run(bot.step())

    curve = store.equity_curve()
    assert curve, "closing a trade should have written an equity point"
    assert any(point.note == "live" for point in curve)


def test_a_filled_entry_is_always_protected(monkeypatch):
    """The invariant the whole execution layer exists for: no naked live position."""
    cfg = live_cfg(monkeypatch)
    client = FakeClient(fills_entries=True)
    bot = trader(cfg, client, decide=once())
    asyncio.run(bot.step())

    assert bot.open_position is not None, "the entry should have filled"
    assert bot.open_position["stop_order_id"], "a filled position must carry a stop"
    assert "place_price_trigger_order" in client.calls


# --- hard failure paths ------------------------------------------------------

def test_an_unexpected_step_error_releases_the_dead_man(monkeypatch):
    """Every stop path disarms the countdown, not just Ctrl-C.

    An error that kills the loop must not leave the countdown armed: it would cancel the
    resting stop `dead_man_switch_seconds` later and turn a monitored position into a
    naked one — the exact state the release exists to prevent.
    """
    cfg = live_cfg(monkeypatch)
    client = FakeClient(fills_entries=True)
    bot = trader(cfg, client, decide=once())
    asyncio.run(bot.step())
    assert bot.open_position is not None
    assert client.sim.countdown_seconds == 60      # armed while exposed

    async def boom(symbol, interval, limit):
        raise RuntimeError("market data went away")

    client.get_candlesticks = boom                 # the step's first read now dies

    with pytest.raises(RuntimeError):
        asyncio.run(bot.run(steps=1, poll_seconds=0))
    assert bot.report.stop_reason == "error"
    assert client.sim.countdown_seconds == 0       # released even on an error stop
    assert asyncio.run(client.list_price_orders("BTC_USDT")), (
        "the resting stop must survive an error stop"
    )


def test_an_unprotected_position_is_tracked_and_never_double_entered(monkeypatch):
    """A filled position whose stop never verifies must not vanish from the loop's view.

    An untracked live position means no dead-man switch, no settlement, and — because
    `can_trade` is told the account is flat — a second entry on top of the first. The
    failure path tracks it instead, so the loop holds, re-protects each step, and never
    attempts a second entry.
    """
    cfg = live_cfg(monkeypatch)
    cfg.raw["protection"] = dict(cfg.raw["protection"],
                                  emergency_close_on_sl_failure=False)
    client = FakeClient(fills_entries=True)

    async def ghost(*args, **kwargs):
        # The stop is accepted but never becomes live, so it can never verify.
        return {"id": "ghost", "status": "open"}

    client.place_price_trigger_order = ghost

    async def no_close(symbol, size, **kwargs):
        if kwargs.get("close"):
            raise RuntimeError("the venue refuses the close")
        raw = await client.sim.place_order(symbol, size, **kwargs)
        if client._fills_entries and not kwargs.get("reduce_only"):
            # Replicate FakeClient's fill nudge so the post-only entry still fills.
            client.sim.advance(float(raw["price"]))
        return raw

    client.place_order = no_close

    bot = trader(cfg, client, decide=once())
    report = asyncio.run(bot.run(steps=3, poll_seconds=0))

    assert report.entries_attempted == 1, (
        "a second entry must never be attempted on top of an unprotected position"
    )
    assert report.rejections.get("protection:unprotected") == 1
    assert bot.open_position is not None, "the unclosed position must stay tracked"
    assert bot.open_position["protected"] is False
    assert report.rejections.get("protection:still_unprotected", 0) >= 1
    assert client.sim.positions["BTC_USDT"]["size"] != 0


def test_a_price_order_read_failure_defers_settlement_instead_of_guessing(monkeypatch):
    """If which level fired cannot be read, the shrink must not be booked at a guessed price.

    The stop sorts nearest to entry, so a fallback that treated every resting level as
    fired would book a TP1-sized (or manual) shrink as a *stop* fill at the stop price.
    Settlement defers instead; the next successful read reconciles honestly.
    """
    cfg = live_cfg(monkeypatch)
    client = FakeClient(fills_entries=True)
    bot = trader(cfg, client, decide=once())
    asyncio.run(bot.step())
    assert bot.open_position is not None, "the entry should have filled"

    # The exchange position disappears (manual close) while the stop never fired.
    asyncio.run(client.sim.place_order(
        "BTC_USDT", 0, price=None, tif="ioc", text="t-manual-close",
        reduce_only=True, close=True))
    assert asyncio.run(client.list_price_orders("BTC_USDT")), "the stop must still rest"

    async def blind(symbol=None):
        raise RuntimeError("price orders endpoint down")

    client.list_price_orders = blind

    asyncio.run(bot.step())                       # settle cannot identify the level
    assert bot.report.rejections.get("settle:unreadable") == 1
    assert bot.open_position is not None, "settlement must defer, not guess"
    assert not bot.report.trades, "no fill may be fabricated from a failed read"

    client.list_price_orders = client.sim.list_price_orders   # the read heals
    asyncio.run(bot.step())
    assert bot.open_position is None
    assert bot.report.trades[-1].exit_reason == "unknown"
    assert bot.report.trades[-1].exit_price == pytest.approx(
        bot.source.mark_price("BTC_USDT")
    )


def test_an_untracked_position_on_the_exchange_blocks_entries(monkeypatch):
    """A position opened manually (or orphaned by a previous run) is never traded on top of."""
    cfg = live_cfg(monkeypatch)
    client = FakeClient()
    asyncio.run(client.sim.place_order(
        "BTC_USDT", 3, price=None, tif="ioc", text="t-manual"))

    bot = trader(cfg, client, decide=once())
    report = asyncio.run(bot.run(steps=2, poll_seconds=0))

    assert report.entries_attempted == 0
    assert report.rejections.get("position:foreign") == 2
    assert "place_order" not in client.calls


# --- graceful shutdown ------------------------------------------------------

def test_a_graceful_stop_releases_the_dead_man_countdown(monkeypatch):
    """Stopping with an open position must not let the countdown cancel its stop.

    The countdown exists to clean up after a *crash*. On a deliberate stop it would
    cancel the protective stop 60 seconds later — turning a monitored position into a
    naked one, which is the exact state this repository forbids. The release
    (``timeout=0``) keeps the verified stop resting.
    """
    cfg = live_cfg(monkeypatch)
    client = FakeClient(fills_entries=True)
    bot = trader(cfg, client, decide=once())
    asyncio.run(bot.step())
    assert bot.open_position is not None
    assert client.sim.countdown_seconds == 60      # armed while exposed

    asyncio.run(bot._release_on_shutdown())
    assert client.sim.countdown_seconds == 0       # released
    assert bot.open_position is not None           # the position is not closed for us
    assert asyncio.run(client.list_price_orders("BTC_USDT")), (
        "the resting stop must survive the shutdown"
    )


def test_completing_steps_with_an_open_position_releases_the_countdown(monkeypatch):
    """The run() exit path calls the release: a bounded run ending mid-trade is safe."""
    cfg = live_cfg(monkeypatch)
    client = FakeClient(fills_entries=True)
    bot = trader(cfg, client, decide=once())
    report = asyncio.run(bot.run(steps=2, poll_seconds=0))
    assert report.stop_reason == "completed"
    assert bot.open_position is not None
    assert client.sim.countdown_seconds == 0


# --- the dead-man switch ----------------------------------------------------

def test_holding_a_position_keeps_the_dead_man_switch_armed(monkeypatch):
    """A crashed bot must not leave a live position with resting orders and nobody watching."""
    cfg = live_cfg(monkeypatch)
    client = FakeClient(fills_entries=True)
    bot = trader(cfg, client, decide=once())
    asyncio.run(bot.step())
    assert bot.open_position is not None, "the entry should have filled"

    before = client.calls.count("countdown_cancel_all")
    asyncio.run(bot.step())
    assert client.calls.count("countdown_cancel_all") > before


def test_an_unfilled_entry_is_reported_as_expired_not_as_an_error(monkeypatch):
    """Not filling is the common case at these stop widths, and it is not a failure."""
    cfg = live_cfg(monkeypatch)
    client = FakeClient(fills_entries=False)
    bot = trader(cfg, client, decide=once())
    report = asyncio.run(bot.run(steps=1, poll_seconds=0))

    assert bot.open_position is None
    assert report.entries_attempted == 1
    assert report.entries_filled == 0
    assert report.entries_expired == 1
    assert "cancel_order" in client.calls


# --- reporting --------------------------------------------------------------

def test_the_report_distinguishes_never_started_from_started(monkeypatch, store):
    """`started` is what tells an operator whether a silent run traded or refused."""
    cfg = live_cfg(monkeypatch)
    _code, refused, _pf = call_run_live(
        cfg, client=FakeClient(), store=store, account=healthy_account(), steps=1,
    )
    assert refused is None

    _code, started, _pf = call_run_live(
        cfg, client=FakeClient(), steps=1, **go_kwargs(store),
    )
    assert started is not None and started.started is True
