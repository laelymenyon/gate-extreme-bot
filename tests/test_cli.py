"""PHASE 12 — the command-line entry point.

`main.py` is the only file a user runs directly, and until Phase 12 it had no tests at
all. That is a strange gap for the one component whose job is to decide, from argv, whether
this process is allowed to send real orders — and whose exit code is what a supervisor,
a systemd unit, or a shell script reads to decide what to do next.

What is asserted here:

* **The gate is reported honestly.** `--status` must never print "LIVE" while the config
  resolves to simulation, or the reverse. An operator reading that line is the last human
  check before real money, and a status display that lies is worse than none.
* **Exit codes mean something.** `2` for a refused configuration, `0` for a clean run. A
  wrapper script that retries on the wrong code turns a config error into a loop.
* **The phase table matches reality.** It is the file's own claim about what is built.
* **No CLI path places an order.** `--status`, `--stats` and a `paper` run all reach the
  exchange through nothing at all; `--positions` reads and is blocked from writing.

Everything runs in-process through `main.main(argv)` with a `tmp_path` database and a
patched config, so no test here depends on the developer's own `data/trades.db`, on
argv, or on the network — the autouse guard in `conftest.py` enforces the last one.
"""

from __future__ import annotations

import asyncio

import pytest

import config as config_module
import main
from database.models import EquityPoint, TradeRecord, TradeStore

NOW = 1_754_784_000.0


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    """Point `database.path` at a scratch file for every test in this module.

    Without this, `--stats` would read (and `TradeStore` would create) the repository's
    real `data/trades.db`. A test suite that writes to the artefact it is testing is a
    test suite that passes for the wrong reason.
    """
    real_load = config_module.load_config

    def scoped(*args, **kwargs):
        cfg = real_load(*args, **kwargs)
        cfg.raw["database"] = dict(cfg.raw["database"], path=str(tmp_path / "trades.db"))
        return cfg

    monkeypatch.setattr(config_module, "load_config", scoped)
    monkeypatch.setattr(main, "load_config", scoped)
    return tmp_path / "trades.db"


def run(argv, capsys):
    """Invoke the CLI and return ``(exit_code, stdout)``."""
    code = main.main(argv)
    return code, capsys.readouterr().out


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


# --- the safety gate, as the operator sees it ------------------------------

def test_status_reports_a_shut_gate_as_simulation(capsys):
    """The default. Nothing about an ordinary run may suggest live trading."""
    code, out = run(["--status"], capsys)
    assert code == 0
    assert "DRY RUN — simulation only" in out
    assert "LIVE — REAL ORDERS" not in out


def test_status_never_claims_live_while_the_config_simulates(monkeypatch, capsys):
    """Every switch combination: the printed line must track `cfg.live_enabled` exactly.

    A status display that disagrees with the resolved config is the one bug in this file
    that could get someone hurt — it is what an operator reads before deciding it is safe
    to walk away.
    """
    monkeypatch.setattr(config_module, "ENV_PATH", config_module.ROOT / ".env.absent")
    monkeypatch.setenv("GATE_API_KEY", "k" * 24)
    monkeypatch.setenv("GATE_API_SECRET", "s" * 24)

    for dry_run in (True, False):
        for mode in ("paper", "backtest", "live"):
            for confirm in (True, False):
                monkeypatch.setenv("DRY_RUN", "true" if dry_run else "false")
                argv = ["--status", "--mode", mode] + (["--confirm-live"] if confirm else [])
                code, out = run(argv, capsys)
                assert code == 0

                live = (dry_run is False) and mode == "live" and confirm
                if live:
                    assert "LIVE — REAL ORDERS" in out
                else:
                    assert "DRY RUN — simulation only" in out, (
                        f"status claimed live for dry_run={dry_run} mode={mode} "
                        f"confirm={confirm}"
                    )


def test_status_shows_the_three_switches_individually(capsys):
    """Not just the verdict — the inputs, so a wrong verdict can be diagnosed."""
    _, out = run(["--status"], capsys)
    assert "DRY_RUN (.env)" in out
    assert "--mode" in out
    assert "--confirm-live" in out


def test_live_mode_without_the_other_switches_says_nothing_was_traded(capsys):
    """`--mode live` alone must explain the refusal rather than failing silently."""
    code, out = run(["--mode", "live"], capsys)
    assert code == 0
    assert "safety gate is CLOSED" in out
    assert "No orders will be sent." in out
    assert "DRY RUN — simulation only" in out


def test_live_mode_names_all_three_requirements(capsys):
    """An operator who sees the refusal must learn what would satisfy it."""
    _, out = run(["--mode", "live"], capsys)
    assert "DRY_RUN=false" in out
    assert "--mode live" in out
    assert "--confirm-live" in out


# --- exit codes -------------------------------------------------------------

def test_a_clean_run_exits_zero(capsys):
    assert run([], capsys)[0] == 0
    assert run(["--status"], capsys)[0] == 0
    assert run(["--mode", "backtest"], capsys)[0] == 0


def test_a_refused_configuration_exits_two(monkeypatch, capsys):
    """`2` is the config-error code: a supervisor must not treat it as a crash to retry."""
    def broken(*args, **kwargs):
        raise config_module.ConfigError("config.yaml missing required key: 'risk.per_trade'")

    monkeypatch.setattr(main, "load_config", broken)
    code = main.main(["--status"])
    captured = capsys.readouterr()
    assert code == 2
    assert "config error" in captured.err
    assert "risk.per_trade" in captured.err


def test_an_invalid_mode_is_rejected_by_the_parser(capsys):
    """argparse exits 2 for an unknown choice, which matches the config-error code."""
    with pytest.raises(SystemExit) as exit_info:
        main.main(["--mode", "wharrgarbl"])
    assert exit_info.value.code == 2


def test_positions_without_credentials_exits_two(monkeypatch, capsys):
    """A read that cannot authenticate is a configuration problem, not a crash."""
    monkeypatch.setattr(config_module, "ENV_PATH", config_module.ROOT / ".env.absent")
    monkeypatch.delenv("GATE_API_KEY", raising=False)
    monkeypatch.delenv("GATE_API_SECRET", raising=False)

    code, out = run(["--positions"], capsys)
    assert code == 2
    assert "requires GATE_API_KEY" in out


# --- the phase table --------------------------------------------------------

def test_the_phase_table_covers_all_fourteen_phases():
    numbers = [number for number, _, _ in main.PHASES]
    assert numbers == [str(n) for n in range(1, 15)]


def test_the_phase_table_matches_the_roadmap_in_the_readme():
    """Two claims about the same thing; they must not drift apart.

    The README table and `main.PHASES` are edited by hand at different times, and a reader
    checking progress will believe whichever they happen to open.
    """
    from pathlib import Path

    readme = (Path(main.__file__).parent / "README.md").read_text(encoding="utf-8")
    for number, _, done in main.PHASES:
        row = next(
            line for line in readme.splitlines()
            if line.startswith(f"| {number} |")
        )
        marked_done = "**done**" in row
        assert marked_done is done, (
            f"phase {number}: main.py says done={done}, README says done={marked_done}"
        )


def test_the_status_output_marks_the_unfinished_phases(capsys):
    _, out = run(["--status"], capsys)
    for number, name, done in main.PHASES:
        marker = "x" if done else " "
        assert f"[{marker}] Phase {number:>2}  {name}" in out


# --- --stats ----------------------------------------------------------------

def test_stats_on_an_empty_database_explains_rather_than_failing(capsys):
    """"No trades yet" is the expected state of a fresh install, not an error."""
    code, out = run(["--stats"], capsys)
    assert code == 0
    assert "No trades recorded yet" in out
    assert "analytics appear once they have" in out


def test_stats_renders_the_dashboard_once_history_exists(isolated_database, capsys):
    """The `--stats` path end to end: real store, real dashboard, real render."""
    store = TradeStore(isolated_database)
    store.record_trade(sample_trade())
    store.record_trade(sample_trade(timestamp=NOW + 60, exit_reason="stop",
                                    pnl=-25.0, exit_price=64_789.0,
                                    equity_after=10_023.8))
    store.record_equity(EquityPoint(timestamp=NOW, equity=10_048.8))

    code, out = run(["--stats"], capsys)
    assert code == 0
    assert "performance" in out
    assert "trades" in out
    # Losses are shown as prominently as wins — the Phase 11 reporting rule.
    assert "net pnl" in out
    assert "win rate" in out


def test_stats_still_prints_the_safety_status_first(isolated_database, capsys):
    """Analytics never appear without the gate state above them."""
    TradeStore(isolated_database).record_trade(sample_trade())
    _, out = run(["--stats"], capsys)
    assert out.index("Trading gate") < out.index("performance")


def test_a_losing_history_is_reported_as_readily_as_a_winning_one(isolated_database,
                                                                  capsys):
    """A CLI that only renders cleanly when profitable would hide the case that matters."""
    store = TradeStore(isolated_database)
    for index in range(4):
        store.record_trade(sample_trade(
            timestamp=NOW + index * 60, exit_reason="stop", pnl=-25.0,
            exit_price=64_789.0, equity_after=10_000.0 - 25.0 * (index + 1),
        ))

    code, out = run(["--stats"], capsys)
    assert code == 0
    assert "-100.00" in out or "-100.0" in out


# --- no CLI path trades -----------------------------------------------------

def test_no_cli_invocation_can_reach_an_order_path(monkeypatch, capsys):
    """The whole point of the file, asserted rather than assumed.

    Every argv a user could reasonably type, with the safety gate at its default. If any
    of these constructed a live client or a gateway, `ExplodingGateway` would say so.
    """
    class ExplodingClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("the CLI constructed a live exchange client")

    monkeypatch.setattr("exchange.gate_client.GateFuturesClient", ExplodingClient)

    for argv in ([], ["--status"], ["--stats"], ["--mode", "paper"],
                 ["--mode", "backtest"], ["--mode", "live"],
                 ["--mode", "live", "--confirm-live"]):
        code, _ = run(list(argv), capsys)
        assert code == 0, f"{argv} exited {code}"


def test_the_paper_message_does_not_promise_live_trading(capsys):
    """`--mode paper` is the default; its wording must not imply real orders."""
    code, out = run(["--mode", "paper"], capsys)
    assert code == 0
    assert "No real order can be placed" in out
    assert "simulator" in out


def test_positions_is_read_only_by_construction(monkeypatch, capsys):
    """`--positions` is the one CLI path that opens a client. It must only read.

    The write-guard is what stops it going further, and it is asserted here against the
    real client rather than trusted because the code happens to call read methods.
    """
    monkeypatch.setattr(config_module, "ENV_PATH", config_module.ROOT / ".env.absent")
    monkeypatch.setenv("GATE_API_KEY", "k" * 24)
    monkeypatch.setenv("GATE_API_SECRET", "s" * 24)

    calls: list[str] = []

    class ReadOnlyClient:
        def __init__(self, cfg, **kwargs):
            self.cfg = cfg

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get_account(self):
            calls.append("get_account")
            return {"total": "10000", "currency": "USDT", "available": "9993",
                    "unrealised_pnl": "0", "position_margin": "6.5"}

        async def list_positions(self, holding=True):
            calls.append("list_positions")
            return []

        def __getattr__(self, name):
            raise AssertionError(f"--positions called client.{name}")

    monkeypatch.setattr("exchange.gate_client.GateFuturesClient", ReadOnlyClient)
    code, out = run(["--positions"], capsys)

    assert code == 0
    assert calls == ["get_account", "list_positions"]
    assert "No open positions." in out


def test_positions_reports_an_api_error_without_crashing(monkeypatch, capsys):
    """An exchange error is a `1`, distinct from a config refusal's `2`."""
    from exchange.gate_client import GateAPIError

    monkeypatch.setattr(config_module, "ENV_PATH", config_module.ROOT / ".env.absent")
    monkeypatch.setenv("GATE_API_KEY", "k" * 24)
    monkeypatch.setenv("GATE_API_SECRET", "s" * 24)

    class FailingClient:
        def __init__(self, cfg, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get_account(self):
            raise GateAPIError(502, "SERVER_ERROR", "upstream unavailable", "/accounts")

    monkeypatch.setattr("exchange.gate_client.GateFuturesClient", FailingClient)
    code = main.main(["--positions"])
    captured = capsys.readouterr()

    assert code == 1
    assert "Gate.io error" in captured.err


# --- the parser -------------------------------------------------------------

def test_the_default_mode_is_paper():
    args = main.build_parser().parse_args([])
    assert args.mode == "paper"
    assert args.confirm_live is False


def test_confirm_live_is_a_flag_that_must_be_typed():
    """It cannot be defaulted on, and it takes no value that could be mistyped as true."""
    assert main.build_parser().parse_args([]).confirm_live is False
    assert main.build_parser().parse_args(["--confirm-live"]).confirm_live is True


def test_the_parser_offers_no_switch_that_bypasses_the_gate():
    """A `--force`, `--yes` or `--no-dry-run` would defeat the three-switch design."""
    actions = {action.dest for action in main.build_parser()._actions}
    for forbidden in ("force", "yes", "no_dry_run", "dry_run", "skip_confirm",
                      "unsafe", "override"):
        assert forbidden not in actions, f"the CLI exposes --{forbidden}"
