"""The interactive control panel (``main.py --menu``, ``menu.py``).

The panel is a dispatch layer over the existing CLI: it adds no trading logic, so what is
asserted here is that the layer is honest about what it does:

* **Every item maps to a real handler** and the numbered keys match the documented menu.
* **Read-only items only read.** Each account/market item is driven with a fake client
  that explodes on any method it should not call, and the exact call sequence is
  asserted — the same discipline `test_cli.py` applies to ``--positions``.
* **Live actions keep every barrier.** "Start Live Bot" refuses behind ``DRY_RUN=true``
  without constructing a client; it refuses without credentials; and even when armed it
  requires the typed ``LIVE SEND`` phrase before it delegates to the existing
  ``main.run_live_mode`` (which itself re-checks the gate and preflight).
* **Emergency Flatten uses the existing safe close.** Behind a shut gate it refuses (the
  write-guard's own rule); with the gate open it requires ``FLATTEN SEND`` and then
  drives ``OrderManager.close_position`` — asserted against the fake gateway's call log
  as a reduce-only ``close=True`` market order, the exact shape ``live/loop.py`` uses.
* **Kill-switch actions persist through ``RiskManager``**, require typed confirmation,
  and the header reflects the latch.
* **Nothing prints secrets**, the header reports LOCKED/LIVE-ARMED honestly, and a broken
  action (invalid choice, missing log file) never crashes the panel.

Everything runs in-process with a scripted input queue and a ``tmp_path`` database, and
the suite-wide network guard in ``conftest.py`` is still in force — no test here opens a
socket.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import config as config_module
import main
import menu
from database.models import EquityPoint, TradeRecord, TradeStore
from risk.risk_manager import SqliteRiskStore

NOW = 1_754_784_000.0


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    """Point ``database.path`` and ``logging.file`` at scratch files for every test.

    Without this, the panel's read-only actions would touch the repository's real
    ``data/trades.db`` and ``logs/bot.log`` — a suite that reads (or worse, creates)
    the artefact it is testing is a suite that passes for the wrong reason.
    """
    real_load = config_module.load_config

    def scoped(*args, **kwargs):
        cfg = real_load(*args, **kwargs)
        cfg.raw["database"] = dict(cfg.raw["database"], path=str(tmp_path / "trades.db"))
        cfg.raw["logging"] = dict(cfg.raw["logging"], file=str(tmp_path / "bot.log"))
        return cfg

    monkeypatch.setattr(config_module, "load_config", scoped)
    monkeypatch.setattr(main, "load_config", scoped)
    # Never read the developer's own .env during tests: the suite must be deterministic
    # (DRY_RUN defaults true) and must not load real API keys.
    monkeypatch.setattr(config_module, "ENV_PATH", config_module.ROOT / ".env.absent")
    return tmp_path / "trades.db"


class ScriptedInput:
    """Hands out answers one at a time; EOF when the script runs dry."""

    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)

    def __call__(self, prompt: str = "") -> str:
        if not self._answers:
            raise EOFError("scripted input exhausted")
        return self._answers.pop(0)


def drive(answers: list[str], *, probe: bool = False, **kwargs) -> int:
    """Run the panel with a scripted input queue; return its exit code."""
    io = menu.MenuIO(input_fn=ScriptedInput(answers))
    cfg = config_module.load_config()
    return menu.run_menu(cfg, io=io, probe=probe, **kwargs)


def install_fake_client(monkeypatch, cls):
    """Replace ``GateFuturesClient`` with a fake; returns the instances it built."""
    instances: list = []

    def factory(cfg, **kwargs):
        instance = cls(cfg, **kwargs)
        instances.append(instance)
        return instance

    monkeypatch.setattr("exchange.gate_client.GateFuturesClient", factory)
    return instances


def set_keys(monkeypatch) -> None:
    monkeypatch.setenv("GATE_API_KEY", "k" * 24)
    monkeypatch.setenv("GATE_API_SECRET", "s" * 24)


def sample_trade(**overrides):
    fields = dict(
        timestamp=NOW, symbol="BTC_USDT", side="long", leverage=100,
        entry_price=65_000.0, exit_price=65_650.0, size=100, margin=6.5,
        stop_loss=64_789.0, fees=1.2, pnl=48.8, pnl_percent=0.00488,
        r_multiple=1.9, signal_score=88.0, market_regime="TRENDING",
        exit_reason="tp2", equity_after=10_048.8, mode="paper",
    )
    fields.update(overrides)
    return TradeRecord(**fields)


# --- the dispatch table ----------------------------------------------------

def test_menu_has_items_1_to_15_with_callable_handlers():
    assert [key for key, *_ in menu.MENU] == [str(n) for n in range(1, 16)]
    for key, label, category, handler in menu.MENU:
        assert callable(handler), f"item {key} ({label}) has no handler"
        assert category in ("ACCOUNT", "TRADING", "RISK & SAFETY", "SYSTEM")
        assert menu.DISPATCH[key][1] is handler


def test_read_only_annotation_is_explicit():
    """The panel itself documents which items never touch exchange state."""
    read_only = {"1", "2", "3", "4", "7", "8", "9", "12", "13", "14"}
    for key in read_only:
        assert menu._NOTES[key] == "read-only", f"item {key} must be annotated read-only"
    effectful = {"5", "6", "10", "11", "15"}
    for key in effectful:
        assert menu._NOTES[key] != "read-only", f"item {key} is not read-only"


def test_parser_exposes_the_menu_flag():
    assert main.build_parser().parse_args(["--menu"]).menu is True


def test_main_dispatches_the_menu_flag(monkeypatch, capsys):
    seen: dict = {}

    def fake_run_menu(cfg, *, symbol=None, poll_seconds=5.0):
        seen["symbol"] = symbol
        seen["poll"] = poll_seconds
        return 7

    monkeypatch.setattr(menu, "run_menu", fake_run_menu)
    code = main.main(["--menu"])
    assert code == 7
    assert seen["symbol"] is None
    assert seen["poll"] == 5.0


# --- the loop itself -------------------------------------------------------

def test_menu_renders_and_exits_on_zero(capsys):
    code = drive(["0"], probe=False)
    out = capsys.readouterr().out
    assert code == 0
    assert "GATE EXTREME BOT" in out
    assert "LIVE TRADING PANEL" in out
    for label in ("Account Balance", "Positions", "Start Live Bot", "Bot Status",
                  "Kill Switch", "Emergency Flatten", "Connectivity Check",
                  "Preflight Check", "View Logs", "Update From GitHub", "Exit"):
        assert label in out


def test_an_unknown_choice_does_not_crash(capsys):
    code = drive(["99", "", "0"], probe=False)
    out = capsys.readouterr().out
    assert code == 0
    assert "'99' is not a menu item" in out


def test_eof_exits_cleanly(capsys):
    code = drive([], probe=False)
    assert code == 0
    assert "Interrupted — goodbye" in capsys.readouterr().out


def test_header_reports_locked_by_default(capsys):
    code = drive([], probe=False)
    out = capsys.readouterr().out
    assert code == 0
    assert "LOCKED — DRY RUN, no real orders" in out


def test_header_reports_armed_when_dry_run_is_false(monkeypatch, capsys):
    monkeypatch.setenv("DRY_RUN", "false")
    set_keys(monkeypatch)
    code = drive([], probe=False)
    out = capsys.readouterr().out
    assert code == 0
    assert "LIVE-ARMED" in out
    assert "not audited" in out


# --- ACCOUNT: read-only by construction ------------------------------------

class ReadOnlyClient:
    """Only the GET-style reads the panel is allowed to use. Any other method explodes."""

    def __init__(self, cfg, **kwargs):
        self.calls: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_account(self):
        self.calls.append("get_account")
        return {"total": "10000", "currency": "USDT", "available": "9993",
                "unrealised_pnl": "0", "position_margin": "6.5"}

    async def list_positions(self, holding=True):
        self.calls.append("list_positions")
        return []

    async def list_open_orders(self):
        self.calls.append("list_open_orders")
        return []

    async def list_price_orders(self):
        self.calls.append("list_price_orders")
        return []

    async def get_ticker(self, symbol):
        self.calls.append("get_ticker")
        return {"last": "65000", "mark_price": "65000", "change_percentage": "-0.42",
                "funding_rate": "0.0001", "volume_24h_quote": "123456789"}

    def __getattr__(self, name):
        raise AssertionError(f"a read-only menu item called client.{name}")


# --- ACCOUNT item call sequences -------------------------------------------

def _expect_calls(monkeypatch, choice: str, expected: list[str], capsys) -> None:
    set_keys(monkeypatch)
    instances = install_fake_client(monkeypatch, ReadOnlyClient)
    code = drive([choice, "", "0"], probe=False)
    assert code == 0
    assert instances[0].calls == expected, f"item {choice} made unexpected calls"
    capsys.readouterr()


def test_balance_reads_only_the_account(monkeypatch, capsys):
    _expect_calls(monkeypatch, "1", ["get_account"], capsys)


def test_positions_reads_account_then_positions(monkeypatch, capsys):
    _expect_calls(monkeypatch, "2", ["get_account", "list_positions"], capsys)


def test_open_orders_reads_only_order_lists(monkeypatch, capsys):
    _expect_calls(monkeypatch, "3", ["list_open_orders", "list_price_orders"], capsys)


def test_market_reads_only_tickers(monkeypatch, capsys):
    set_keys(monkeypatch)
    instances = install_fake_client(monkeypatch, ReadOnlyClient)
    code = drive(["4", "", "0"], probe=False)
    assert code == 0
    calls = instances[0].calls
    assert len(calls) == 31
    assert set(calls) == {"get_ticker"}
    out = capsys.readouterr().out
    assert "BTC_USDT" in out and "last=65000" in out


def test_account_items_without_credentials_explain_rather_than_crash(capsys):
    code = drive(["1", "", "0"], probe=False)
    assert code == 0
    assert "GATE_API_KEY/GATE_API_SECRET in .env" in capsys.readouterr().out


# --- TRADING: the live barriers --------------------------------------------

def test_start_live_behind_a_shut_gate_constructs_no_client(monkeypatch, capsys):
    class ExplodingClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("the panel built a live client behind a shut gate")

    monkeypatch.setattr("exchange.gate_client.GateFuturesClient", ExplodingClient)
    code = drive(["5", "", "0"], probe=False)
    assert code == 0
    out = capsys.readouterr().out
    assert "DRY_RUN=true in .env" in out
    assert "No order was sent." in out


def test_start_live_without_credentials_refuses(monkeypatch, capsys):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.delenv("GATE_API_KEY", raising=False)
    monkeypatch.delenv("GATE_API_SECRET", raising=False)

    class ExplodingClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("no client may be built without credentials")

    monkeypatch.setattr("exchange.gate_client.GateFuturesClient", ExplodingClient)
    code = drive(["5", "", "0"], probe=False)
    assert code == 0
    assert "No order was sent." in capsys.readouterr().out


def test_start_live_requires_the_typed_phrase_then_delegates(monkeypatch, capsys):
    monkeypatch.setenv("DRY_RUN", "false")
    set_keys(monkeypatch)
    calls: list = []

    def fake_run_live_mode(cfg, args):
        calls.append((cfg, args))
        return 0

    monkeypatch.setattr(menu.main, "run_live_mode", fake_run_live_mode)
    code = drive(["5", "nope", "", "5", "LIVE SEND", "", "0"], probe=False)
    assert code == 0
    out = capsys.readouterr().out
    assert "Aborted. No order was sent." in out
    assert len(calls) == 1, "only the confirmed attempt may reach the live runner"
    live_cfg, args = calls[0]
    assert live_cfg.live_enabled is True
    assert args.steps is None
    assert args.symbol is None


def test_stop_bot_reports_when_nothing_is_running(capsys):
    code = drive(["6", "", "0"], probe=False)
    assert code == 0
    assert "No running live-bot process" in capsys.readouterr().out


def test_bot_status_reuses_the_cli_status(monkeypatch, capsys):
    printed = []

    def fake_print_status(cfg):
        printed.append(cfg)

    monkeypatch.setattr(menu.main, "print_status", fake_print_status)
    code = drive(["7", "", "0"], probe=False)
    assert code == 0
    assert len(printed) == 1


def test_trade_history_reads_store_and_dashboard(isolated_database, capsys):
    store = TradeStore(isolated_database)
    store.record_trade(sample_trade())
    store.record_equity(EquityPoint(timestamp=NOW, equity=10_048.8))

    code = drive(["8", "", "0"], probe=False)
    assert code == 0
    out = capsys.readouterr().out
    assert "performance" in out
    assert "net pnl" in out
    assert "BTC_USDT" in out


def test_trade_history_empty_store_explains(capsys):
    code = drive(["8", "", "0"], probe=False)
    assert code == 0
    assert "No trades recorded yet" in capsys.readouterr().out


# --- RISK & SAFETY ---------------------------------------------------------

def test_risk_settings_is_read_only(capsys):
    code = drive(["9", "", "0"], probe=False)
    assert code == 0
    out = capsys.readouterr().out
    assert "risk per trade" in out
    assert "max drawdown" in out
    assert "kill switches" in out


def test_kill_switch_trip_requires_confirmation_and_persists(isolated_database, capsys):
    code = drive(["10", "x", "", "10", "t", "TRIP SEND", "", "0"], probe=False)
    assert code == 0
    out = capsys.readouterr().out
    assert "No change." in out                      # the "x" attempt did nothing
    assert "LATCHED and persisted" in out
    assert "HALTED" in out                          # the header now shows the latch

    _state, switches = SqliteRiskStore(isolated_database).load()
    assert "manual" in switches
    assert switches["manual"].manual_reset_required is True


def test_kill_switch_reset_requires_confirmation(isolated_database, capsys):
    code = drive(
        ["10", "t", "TRIP SEND", "", "10", "r", "RESET SEND", "", "0"], probe=False,
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "Cleared: manual." in out
    _state, switches = SqliteRiskStore(isolated_database).load()
    assert switches == {}


def test_emergency_flatten_refused_behind_a_shut_gate(monkeypatch, capsys):
    class ExplodingClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("the panel built a client behind a shut gate")

    monkeypatch.setattr("exchange.gate_client.GateFuturesClient", ExplodingClient)
    code = drive(["11", "", "0"], probe=False)
    assert code == 0
    out = capsys.readouterr().out
    assert "refused" in out
    assert "write-guard" in out


class LiveFlatFake:
    """A live client holding one BTC position; close() reports it flat."""

    def __init__(self, cfg, **kwargs):
        self.calls: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def list_positions(self, holding=True):
        self.calls.append("list_positions")
        return [{"contract": "BTC_USDT", "size": 5, "entry_price": "65000",
                 "mark_price": "65000", "liq_price": "64800"}]

    async def place_order(self, symbol, size, *, price=None, tif="poc",
                          reduce_only=False, close=False, text=""):
        self.calls.append(("place_order", {"symbol": symbol, "size": size, "price": price,
                                           "tif": tif, "reduce_only": reduce_only,
                                           "close": close, "text": text}))
        return {"id": "900", "contract": symbol, "size": size, "left": 0,
                "status": "finished", "finish_as": "filled", "fill_price": "65000"}

    async def get_order(self, order_id):
        self.calls.append("get_order")
        return {"id": order_id, "contract": "BTC_USDT", "size": 0, "left": 0,
                "status": "finished", "finish_as": "filled", "fill_price": "65000"}

    async def get_position(self, symbol):
        self.calls.append("get_position")
        return {"contract": symbol, "size": 0}

    async def list_price_orders(self, symbol=None):
        self.calls.append("list_price_orders")
        return []

    async def cancel_price_order(self, order_id):
        self.calls.append("cancel_price_order")
        return {}


def test_emergency_flatten_confirms_then_uses_the_safe_close(monkeypatch, capsys):
    monkeypatch.setenv("DRY_RUN", "false")
    set_keys(monkeypatch)
    instances = install_fake_client(monkeypatch, LiveFlatFake)

    code = drive(["11", "nope", "", "11", "FLATTEN SEND", "", "0"], probe=False)
    assert code == 0
    out = capsys.readouterr().out
    assert "Aborted. Nothing was closed." in out

    # The aborted attempt only read; the confirmed one drove the safe close.
    assert instances[0].calls == ["list_positions"]
    closed = [c for c in instances[-1].calls
              if isinstance(c, tuple) and c[0] == "place_order"]
    assert len(closed) == 1
    payload = closed[0][1]
    assert payload["close"] is True            # close=True: can only reduce exposure
    assert payload["reduce_only"] is True
    assert payload["tif"] == "ioc"
    assert "flat confirmed" in out
    assert "Emergency flatten finished" in out


# --- SYSTEM ----------------------------------------------------------------

def test_connectivity_delegates_to_the_existing_check(monkeypatch, capsys):
    import live.verify

    calls: list = []

    async def fake_check(cfg, *, symbol=None, client=None, print_fn=print):
        calls.append((symbol, print_fn))
        return 0

    monkeypatch.setattr(live.verify, "check_connectivity", fake_check)
    code = drive(["12", "", "0"], probe=False)
    assert code == 0
    assert len(calls) == 1
    assert "OK (full check" in capsys.readouterr().out


def test_preflight_check_runs_the_existing_audit(monkeypatch, capsys):
    seen: list = []

    async def fake_snapshot(cfg):
        seen.append(cfg)
        return None

    monkeypatch.setattr(menu.main, "read_account_snapshot", fake_snapshot)
    code = drive(["13", "", "0"], probe=False)
    assert code == 0
    assert len(seen) == 1
    out = capsys.readouterr().out
    assert "live readiness — preflight" in out
    assert "NO-GO" in out


def test_view_logs_is_graceful_when_missing(capsys):
    code = drive(["14", "", "0"], probe=False)
    assert code == 0
    assert "No log file at" in capsys.readouterr().out


def test_view_logs_tails_the_file(isolated_database, capsys):
    log = Path(isolated_database).parent / "bot.log"
    log.write_text("\n".join(f"line {i}" for i in range(50)), encoding="utf-8")

    code = drive(["14", "", "0"], probe=False)
    assert code == 0
    out = capsys.readouterr().out
    assert "line 49" in out
    assert "line 0" not in out  # only the last 40 lines


# --- secrets and resilience -------------------------------------------------

def test_no_menu_action_prints_secrets(monkeypatch, capsys):
    monkeypatch.setenv("DRY_RUN", "false")
    set_keys(monkeypatch)
    install_fake_client(monkeypatch, ReadOnlyClient)

    code = drive(
        ["1", "", "2", "", "3", "", "4", "", "7", "", "8", "", "9", "", "14", "", "0"],
        probe=False,
    )
    assert code == 0
    out = capsys.readouterr().out
    assert ("k" * 24) not in out
    assert ("s" * 24) not in out


def test_a_failing_action_returns_to_the_menu(monkeypatch, capsys):
    """A handler that throws must be reported as a line, not kill the panel."""

    def explode(cfg, session, io):
        raise RuntimeError("boom from a handler under test")

    monkeypatch.setitem(menu.DISPATCH, "5", ("Start Live Bot", explode))
    code = drive(["5", "", "0"], probe=False)
    assert code == 0
    out = capsys.readouterr().out
    assert "Start Live Bot failed cleanly" in out
    assert "boom from a handler under test" in out


# --- Update From GitHub: thin git wrapper, never run for real in tests ----------

def fake_git_run(script: list[str]):
    """A subprocess.run stand-in that answers git queries without touching the repo."""
    calls: list[list[str]] = []

    def run(args, **kwargs):
        calls.append(list(args))
        if "pull" in args:
            return subprocess.CompletedProcess(args, 0, "Updating 1c4b9ac..abc1234\n", "")
        if "status" in args:
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "abc1234", "")

    run.calls = calls
    return run


def test_update_from_github_aborts_without_the_phrase(monkeypatch, capsys):
    import subprocess as subprocess_module

    run = fake_git_run([])
    monkeypatch.setattr(subprocess_module, "run", run)
    code = drive(["15", "nope", "", "0"], probe=False)
    assert code == 0
    assert "Aborted. Nothing was pulled." in capsys.readouterr().out
    # The pre-confirmation status read happened; the pull itself never did.
    assert "pull" not in " ".join(" ".join(args) for args in run.calls)


def test_update_from_github_pulls_fast_forward_only(monkeypatch, capsys):
    import subprocess as subprocess_module

    run = fake_git_run([])
    monkeypatch.setattr(subprocess_module, "run", run)
    code = drive(["15", "PULL SEND", "", "0"], probe=False)
    assert code == 0
    out = capsys.readouterr().out
    assert "updated — new HEAD: abc1234" in out
    pull_call = next(args for args in run.calls if "pull" in args)
    assert pull_call == ["git", "pull", "--ff-only", "origin", "main"]
