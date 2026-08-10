"""PHASE 12 — the repo-wide safety audit.

Every phase note in this repo ends with the same sentence: *no real order was sent*. Each
phase proved that for its own module. Nothing has ever proved it for the **repository**,
and the gap matters, because the claim is not "each layer is careful" — it is that no
combination of layers can reach the exchange with a write while the gate is shut.

These tests are deliberately structural rather than behavioural. A behavioural test asks
"did this call place an order?" and answers for the paths someone thought to call. A
structural test asks "does a path to `place_order` exist from here at all?", and keeps
answering after code is added by someone who never read this file. Both kinds are here; the
structural ones are the load-bearing half.

Four properties, in descending order of how much they would cost to get wrong:

1. **The three switches are each individually necessary.** All eight combinations, driven
   through the real `Config`, the real `GateFuturesClient` write-guard, and the real
   `OrderManager` resolution — not through a mock of any of them.
2. **Every state-changing REST method is behind the write-guard.** Enumerated from the
   client's own API surface, so a method added later is covered without being listed here.
3. **Layers that must not trade cannot.** Strategy, risk, database and monitoring reach no
   order path, by import and by attribute.
4. **The kill switch cannot be cleared by restarting.**

The suite-wide offline guard lives in ``conftest.py``; if any test here could reach the
network, that guard would fail it before these assertions ran.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
from pathlib import Path

import pytest

import config as config_module
from config import load_config
from exchange.gate_client import GateFuturesClient, WriteBlocked
from execution.order_manager import OrderManager, SimulatedGateway
from paper.loop import LiveTradingRefused, PaperTrader
from risk.risk_manager import Breaker, RiskManager, RiskParams, SqliteRiskStore

ROOT = Path(__file__).resolve().parent.parent

#: Modules that decide, measure, or record — none of which may reach the exchange.
NON_TRADING_MODULES = (
    "strategy.indicators",
    "strategy.regime",
    "strategy.scoring",
    "strategy.signal_engine",
    "risk.position_sizer",
    "risk.liquidation_guard",
    "risk.risk_manager",
    "backtest.engine",
    "database.models",
    "monitoring.dashboard",
    "monitoring.logger",
)

#: The only modules allowed to name a live order endpoint at all.
TRADING_MODULES = ("exchange.gate_client", "execution.order_manager", "execution.protection")

NOW = 1_754_784_000.0
EQUITY = 10_000.0


def switched(monkeypatch, *, dry_run: bool, mode: str, confirm: bool, creds: bool = True):
    """Build a real Config with the three switches set explicitly."""
    monkeypatch.setattr(config_module, "ENV_PATH", config_module.ROOT / ".env.absent")
    monkeypatch.setenv("DRY_RUN", "true" if dry_run else "false")
    if creds:
        monkeypatch.setenv("GATE_API_KEY", "k" * 24)
        monkeypatch.setenv("GATE_API_SECRET", "s" * 24)
    else:
        monkeypatch.delenv("GATE_API_KEY", raising=False)
        monkeypatch.delenv("GATE_API_SECRET", raising=False)
    return load_config(run_mode=mode, confirm_live=confirm)


class ExplodingClient:
    """Any attribute access is a test failure. Stands in for the live client."""

    def __getattr__(self, name):
        raise AssertionError(f"the live client was touched: client.{name}")


def _scripted_client(cfg, script):
    """A client over Phase 2's recording fake session, reused rather than reimplemented."""
    from tests.test_gate_client import FakeSession

    session = FakeSession(script)
    return GateFuturesClient(cfg, session=session), session


# --- 1. the three switches --------------------------------------------------

ALL_EIGHT = [
    (dry_run, mode, confirm)
    for dry_run in (True, False)
    for mode in ("live", "paper")
    for confirm in (True, False)
]


@pytest.mark.parametrize("dry_run,mode,confirm", ALL_EIGHT)
def test_only_one_of_eight_switch_combinations_opens_the_gate(monkeypatch, dry_run, mode,
                                                              confirm):
    """The truth table, asserted rather than described.

    `DRY_RUN=false` **and** `--mode live` **and** `--confirm-live`. Seven of eight
    combinations must simulate. README documents this; here it is executed.
    """
    cfg = switched(monkeypatch, dry_run=dry_run, mode=mode, confirm=confirm)
    expected = (dry_run is False) and mode == "live" and confirm is True
    assert cfg.live_enabled is expected
    assert cfg.dry_run is not expected


@pytest.mark.parametrize("dry_run,mode,confirm", ALL_EIGHT)
def test_the_execution_layer_agrees_with_the_gate(monkeypatch, dry_run, mode, confirm):
    """`OrderManager.for_config` must resolve exactly as `live_enabled` says.

    A drift between "the config thinks we are simulating" and "the order manager thinks we
    are live" is the single most expensive bug this repo could have.
    """
    cfg = switched(monkeypatch, dry_run=dry_run, mode=mode, confirm=confirm)
    om = OrderManager.for_config(cfg, client=ExplodingClient(), last_price=65_000.0)

    if cfg.live_enabled:
        assert om.live is True
        assert not isinstance(om.gateway, SimulatedGateway)
    else:
        assert om.live is False
        assert isinstance(om.gateway, SimulatedGateway)


@pytest.mark.parametrize("dry_run,mode,confirm", ALL_EIGHT)
def test_the_write_guard_agrees_with_the_gate(monkeypatch, dry_run, mode, confirm):
    """The client's own barrier, checked against the same truth table.

    This is the second independent barrier: even if the execution layer resolved wrongly,
    a POST still has to get past the client.
    """
    cfg = switched(monkeypatch, dry_run=dry_run, mode=mode, confirm=confirm)
    client = GateFuturesClient(cfg, session=object())

    async def attempt():
        await client.place_order("BTC_USDT", 1, price="60000", text="t-audit1")

    if cfg.live_enabled:
        # The gate is open, so the guard is not what stops it — the fake session is.
        with pytest.raises(Exception) as caught:
            asyncio.run(attempt())
        assert not isinstance(caught.value, WriteBlocked)
    else:
        with pytest.raises(WriteBlocked):
            asyncio.run(attempt())
        assert client.stats.writes_blocked == 1


@pytest.mark.parametrize("mode", ["paper", "backtest", "live"])
def test_no_run_mode_opens_the_gate_on_its_own(monkeypatch, mode):
    """`--mode live` alone is not enough, and the other modes can never be enough."""
    cfg = switched(monkeypatch, dry_run=True, mode=mode, confirm=True)
    assert cfg.live_enabled is False


def test_live_without_credentials_is_refused_outright(monkeypatch):
    """An open gate with no keys must fail loudly at load, not at the first order."""
    from config import ConfigError

    with pytest.raises(ConfigError, match="GATE_API_KEY"):
        switched(monkeypatch, dry_run=False, mode="live", confirm=True, creds=False)


@pytest.mark.parametrize("value", ["", "0", "no", "False", "FALSE", "false"])
def test_only_an_explicit_negative_clears_dry_run(monkeypatch, value):
    """`DRY_RUN` is true unless it is literally false/0/no, so a typo fails safe."""
    cfg = switched(monkeypatch, dry_run=True, mode="live", confirm=True)
    monkeypatch.setenv("DRY_RUN", value)
    monkeypatch.setattr(config_module, "ENV_PATH", config_module.ROOT / ".env.absent")
    reloaded = load_config(run_mode="live", confirm_live=True)
    assert reloaded.live_enabled is (value.strip().lower() in ("false", "0", "no"))
    assert cfg is not reloaded


@pytest.mark.parametrize("value", ["true", "yes", "1", "maybe", "", "  ", "TRUE", "off"])
def test_an_unparseable_dry_run_value_stays_shut(monkeypatch, value):
    """Anything that is not an explicit negative leaves the gate closed."""
    monkeypatch.setattr(config_module, "ENV_PATH", config_module.ROOT / ".env.absent")
    monkeypatch.setenv("GATE_API_KEY", "k" * 24)
    monkeypatch.setenv("GATE_API_SECRET", "s" * 24)
    monkeypatch.setenv("DRY_RUN", value)
    assert load_config(run_mode="live", confirm_live=True).live_enabled is False


# --- 2. every write is behind the guard ------------------------------------

def _write_methods_of_client() -> list[str]:
    """Client coroutines that issue a state-changing request, found in the source.

    Enumerated from the module rather than listed by hand, so a method added later is
    audited automatically instead of being silently exempt.
    """
    source = ast.parse((ROOT / "exchange" / "gate_client.py").read_text(encoding="utf-8"))
    client = next(
        node for node in ast.walk(source)
        if isinstance(node, ast.ClassDef) and node.name == "GateFuturesClient"
    )
    found = []
    for node in client.body:
        if not isinstance(node, ast.AsyncFunctionDef) or node.name.startswith("_"):
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            target = call.func
            if isinstance(target, ast.Attribute) and target.attr == "_request":
                method = call.args[0] if call.args else None
                if isinstance(method, ast.Constant) and method.value in (
                    "POST", "PUT", "DELETE", "PATCH"
                ):
                    found.append(node.name)
    return sorted(set(found))


def test_the_audit_can_actually_see_the_write_methods():
    """If the AST walk found nothing, the next test would pass vacuously."""
    writes = _write_methods_of_client()
    assert len(writes) >= 6, writes
    # The ones whose absence would matter most, named explicitly.
    for required in ("place_order", "cancel_order", "set_leverage",
                     "place_price_trigger_order", "countdown_cancel_all"):
        assert required in writes, f"{required} no longer routes through a write verb"


def test_every_state_changing_client_method_is_blocked_while_the_gate_is_shut(monkeypatch):
    """Enumerated from the source, so a new write method cannot quietly skip the guard."""
    cfg = switched(monkeypatch, dry_run=True, mode="paper", confirm=False)
    client = GateFuturesClient(cfg, session=object())

    sample_args = {
        "set_leverage": {"symbol": "BTC_USDT", "leverage": 100},
        "place_order": {"symbol": "BTC_USDT", "size": 1, "price": "60000",
                        "text": "t-audit2"},
        "cancel_order": {"order_id": 1},
        "cancel_price_order": {"order_id": 1},
        "cancel_all_price_orders": {"symbol": "BTC_USDT"},
        "countdown_cancel_all": {"timeout_seconds": 30},
        "place_price_trigger_order": {"symbol": "BTC_USDT", "trigger_price": "59000",
                                      "rule": 2, "text": "t-audit3"},
    }

    for name in _write_methods_of_client():
        assert name in sample_args, (
            f"{name} is a write method with no audit case; add one to sample_args"
        )
        with pytest.raises(WriteBlocked):
            asyncio.run(getattr(client, name)(**sample_args[name]))

    assert client.stats.writes_blocked == len(_write_methods_of_client())


def test_a_blocked_write_never_reaches_the_transport(monkeypatch):
    """The refusal happens before the session is touched, not after a request is built."""
    cfg = switched(monkeypatch, dry_run=True, mode="paper", confirm=False)

    class ExplodingSession:
        def __getattr__(self, name):
            raise AssertionError(f"a blocked write reached the transport: session.{name}")

    client = GateFuturesClient(cfg, session=ExplodingSession())
    with pytest.raises(WriteBlocked):
        asyncio.run(client.place_order("BTC_USDT", 1, price="60000", text="t-audit4"))


def test_reads_still_work_while_the_gate_is_shut(monkeypatch):
    """The guard must block writes only.

    A dry run that could not read the market would push someone to open the gate to test
    anything, which is the opposite of what this barrier is for.
    """
    cfg = switched(monkeypatch, dry_run=True, mode="paper", confirm=False)
    client, session = _scripted_client(cfg, [
        (200, [{"last": "65000"}]),
        (200, {"total": "10000", "currency": "USDT"}),
        (200, [{"contract": "BTC_USDT", "size": 0}]),
    ])

    async def read_three():
        return (
            await client.get_ticker("BTC_USDT"),
            await client.get_account(),
            await client.list_positions(),
        )

    ticker, account, positions = asyncio.run(read_three())
    assert ticker == {"last": "65000"}
    assert account["total"] == "10000"
    assert positions == [{"contract": "BTC_USDT", "size": 0}]
    assert client.stats.writes_blocked == 0
    assert [call["method"] for call in session.calls] == ["GET", "GET", "GET"]

    # The guard keys on the HTTP verb, so no read appears in the write set.
    reads = ("get_ticker", "get_account", "list_positions", "get_order_book",
             "get_candlesticks", "list_contracts", "get_risk_tiers", "list_price_orders")
    assert not set(reads) & set(_write_methods_of_client())


# --- 3. layers that must not trade cannot ----------------------------------

@pytest.mark.parametrize("name", NON_TRADING_MODULES)
def test_a_non_trading_module_imports_no_execution_path(name):
    """Structural: the import graph, not the call that happened to be tested.

    Phase 11 checks this for its own three modules. Extended here to every module that
    decides or measures, because "cannot trade" is a property of the whole non-trading
    half of the repo, not of the newest part of it.
    """
    module = importlib.import_module(name)
    source = Path(module.__file__).read_text(encoding="utf-8")
    imports = [
        line for line in source.splitlines()
        if line.startswith(("import ", "from ")) and "typing" not in line
    ]
    joined = "\n".join(imports)

    assert "aiohttp" not in joined, f"{name} imports an HTTP client"
    assert "websockets" not in joined, f"{name} imports a websocket client"
    assert "from execution" not in joined and "import execution" not in joined, (
        f"{name} imports the execution layer"
    )
    # `exchange` is allowed only as a typing/protocol reference, never as a live client.
    assert "GateFuturesClient" not in source, f"{name} names the live REST client"


@pytest.mark.parametrize("name", NON_TRADING_MODULES)
def test_a_non_trading_module_exposes_no_order_verb(name):
    """Behavioural backstop: no public callable that sounds like it acts on the exchange."""
    module = importlib.import_module(name)
    forbidden = ("place_order", "submit_order", "cancel_order", "close_position",
                 "set_leverage", "countdown_cancel_all")

    for attr_name, attr in vars(module).items():
        if attr_name.startswith("_") or not inspect.isclass(attr):
            continue
        if attr.__module__ != module.__name__:
            continue
        for member in dir(attr):
            assert member not in forbidden, f"{name}.{attr_name}.{member} can act on orders"


def test_only_the_execution_and_exchange_layers_name_the_order_endpoint():
    """`/orders` should appear in exactly the modules that own it."""
    offenders = []
    for path in ROOT.glob("*/*.py"):
        if path.parts[-2] in ("tests", ".venv", "__pycache__"):
            continue
        module = f"{path.parts[-2]}.{path.stem}"
        if module in TRADING_MODULES or path.stem == "__init__":
            continue
        text = path.read_text(encoding="utf-8")
        if '"/orders"' in text or "'/orders'" in text or '"/price_orders"' in text:
            offenders.append(module)
    assert not offenders, f"an order endpoint is named outside the trading layer: {offenders}"


# --- the paper loop cannot become a live loop ------------------------------

def test_the_paper_trader_refuses_to_exist_when_the_gate_is_open(monkeypatch):
    """Not a branch — a refusal to construct. There is no flag to override it."""
    import numpy as np

    from exchange.gate_client import Contract, RiskTier
    from paper.loop import ReplayMarketSource
    from strategy.indicators import Candles

    cfg = switched(monkeypatch, dry_run=False, mode="live", confirm=True)
    assert cfg.live_enabled

    n = 320
    close = 65_000.0 * np.ones(n)
    series = Candles(
        time=np.arange(n, dtype=float) * 60.0, open=close, high=close * 1.001,
        low=close * 0.999, close=close, volume=np.full(n, 1000.0),
    )
    source = ReplayMarketSource({"1m": series}, "1m", start=300)
    contract = Contract.from_api({
        "name": "BTC_USDT", "leverage_max": "200", "leverage_min": "1",
        "maintenance_rate": "0.003", "quanto_multiplier": "0.0001",
        "order_size_min": 1, "order_size_max": 12000000,
        "order_price_round": "0.1", "mark_price_round": "0.01",
        "taker_fee_rate": "0.00075", "maker_fee_rate": "-0.0001",
        "risk_limit_base": "500000", "in_delisting": False, "status": "trading",
    })
    tiers = [RiskTier.from_api({
        "tier": 1, "risk_limit": "500000", "initial_rate": "0.005",
        "maintenance_rate": "0.003", "leverage_max": "200", "deduction": "0",
    })]

    with pytest.raises(LiveTradingRefused, match="gate is OPEN"):
        PaperTrader(cfg, source, "BTC_USDT", tiers, contract)


def test_the_paper_trader_has_no_switch_to_go_live():
    """A constructor keyword that forced the live gateway would defeat the refusal above."""
    parameters = inspect.signature(PaperTrader.__init__).parameters
    for suspicious in ("live", "live_enabled", "real", "force", "confirm_live", "dry_run"):
        assert suspicious not in parameters, (
            f"PaperTrader accepts {suspicious!r}; the refusal is meant to be unconditional"
        )


# --- 4. the kill switch outlives the process -------------------------------

@pytest.mark.parametrize("breaker", [Breaker.DAILY_LOSS, Breaker.DRAWDOWN,
                                     Breaker.CONSECUTIVE_LOSSES, Breaker.MANUAL])
def test_no_breaker_can_be_cleared_by_restarting(tmp_path, breaker):
    """Invariant 5, for every latch rather than for the one that was convenient.

    The most tempting thing to do after a bad run is restart the bot. Every breaker must
    survive that, or the limit is advisory.
    """
    path = tmp_path / "risk.db"
    params = RiskParams.from_config(load_config())

    first = RiskManager(params, SqliteRiskStore(path))
    first.observe_equity(NOW, EQUITY)
    first.trip("audit", now=NOW, breaker=breaker)
    assert first.tripped
    del first

    restarted = RiskManager(params, SqliteRiskStore(path))
    assert restarted.tripped, f"{breaker.value} did not survive a restart"
    decision = restarted.can_trade(now=NOW + 60, equity=EQUITY, open_positions=0,
                                   symbol="BTC_USDT")
    assert not decision.allowed
    assert decision.breaker is breaker


def test_clearing_a_breaker_requires_an_explicit_reset(tmp_path):
    """Nothing clears a latch implicitly — not a new day, not a fresh process."""
    path = tmp_path / "risk.db"
    params = RiskParams.from_config(load_config())

    manager = RiskManager(params, SqliteRiskStore(path))
    manager.observe_equity(NOW, EQUITY)
    manager.trip("audit", now=NOW, breaker=Breaker.DRAWDOWN)

    # A day later, in a new process: still halted.
    later = RiskManager(params, SqliteRiskStore(path))
    assert later.can_trade(now=NOW + 86_400 * 2, equity=EQUITY,
                           open_positions=0).allowed is False

    later.reset(Breaker.DRAWDOWN, now=NOW + 86_400 * 2)
    assert later.can_trade(now=NOW + 86_400 * 2, equity=EQUITY,
                           open_positions=0).allowed is True

    # And the reset itself persists, so the breaker does not come back on the next restart.
    final = RiskManager(params, SqliteRiskStore(path))
    assert final.can_trade(now=NOW + 86_400 * 2, equity=EQUITY,
                           open_positions=0).allowed is True


# --- the phase boundary ----------------------------------------------------

def test_phase_12_added_no_trading_capability():
    """Phase 12 is tests. It must not have introduced a way to trade.

    The check that matters when reading this commit: the shipped default is still shut, and
    the only new runtime file is a test-suite fixture.
    """
    assert load_config().live_enabled is False
    assert load_config(run_mode="live").live_enabled is False
    assert load_config(run_mode="live", confirm_live=True).live_enabled is False

    # The three switches still resolve through one property, not several copies.
    sources = [
        path for path in ROOT.glob("*/*.py")
        if path.parts[-2] not in ("tests", ".venv", "__pycache__")
    ] + [ROOT / "main.py", ROOT / "config.py"]
    definitions = [
        path for path in sources
        if "def live_enabled" in path.read_text(encoding="utf-8")
    ]
    assert definitions == [ROOT / "config.py"], (
        f"live_enabled is defined in more than one place: {definitions}"
    )
