"""PHASE 14 tests — live readiness.

This is the first module in the repository whose *success* condition is permitting
something, so the tests are weighted accordingly: most of them drive a condition to
NO-GO, and the few that reach GO exist mainly to prove the ones that refuse are not
refusing for a trivial reason.

Three properties matter more than the individual checks:

* **Unknown blocks.** Phase 13 could withhold a verdict and leave conduct clean. Here
  ``INSUFFICIENT`` is a blocker, because "we could not establish this" and "this is fine"
  must not share an outcome when the next step is real money at 100x.
* **Stored history alone can never reach GO.** Three of Phase 13's conduct checks are
  events a trade table cannot record. Clearing them takes an observed validation report
  from a supervised run, and a test asserts the database route cannot substitute.
* **Preflight authorises nothing.** It has no path to the safety gate, and a GO changes
  no switch. Asserted structurally, not assumed.

Offline and deterministic: `tmp_path` databases, hand-built snapshots, no client.
"""

from __future__ import annotations

import pytest

from config import load_config
from database.models import EquityPoint, TradeRecord, TradeStore
from execution.preflight import AccountSnapshot, PreflightReport, preflight
from paper.validation import CheckStatus, ValidationParams, validate
from tests.test_validation import evidence as session_evidence
from tests.test_validation import record

NOW = 1_754_784_000.0


@pytest.fixture
def store(tmp_path):
    return TradeStore(tmp_path / "trades.db")


def statuses(report):
    return {check.name: check.status for check in report.checks}


def live_cfg(monkeypatch, *, dry_run=False, mode="live", confirm=True, creds=True):
    """A real Config with the three switches set explicitly."""
    import config as config_module

    monkeypatch.setattr(config_module, "ENV_PATH", config_module.ROOT / ".env.absent")
    monkeypatch.setenv("DRY_RUN", "false" if not dry_run else "true")
    if creds:
        monkeypatch.setenv("GATE_API_KEY", "k" * 24)
        monkeypatch.setenv("GATE_API_SECRET", "s" * 24)
    else:
        monkeypatch.delenv("GATE_API_KEY", raising=False)
        monkeypatch.delenv("GATE_API_SECRET", raising=False)
    return config_module.load_config(run_mode=mode, confirm_live=confirm)


def healthy_account(**overrides):
    fields = dict(reachable=True, currency="USDT", total=10_000.0, available=9_900.0,
                  open_positions=0, open_orders=0, leverage=100, margin_mode="")
    fields.update(overrides)
    return AccountSnapshot(**fields)


def passing_validation(count=1000):
    """An observed Phase 13 report that passes — the intended route to GO."""
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


def ready(monkeypatch, store, **overrides):
    """Every condition met. The only route to GO, assembled deliberately."""
    profitable_backtest(store)
    kwargs = dict(
        store=store, account=healthy_account(), validation=passing_validation(),
        params=ValidationParams(min_trades=1000),
    )
    kwargs.update(overrides)
    return preflight(live_cfg(monkeypatch), **kwargs)


# --- the roadmap's own precondition -----------------------------------------

def test_an_empty_database_is_a_no_go(store, monkeypatch):
    """The state this repository is actually in: nothing has ever run."""
    report = preflight(live_cfg(monkeypatch), store)

    assert not report.ready
    found = statuses(report)
    assert found["evidence.paper_validated"] is CheckStatus.FAIL
    assert found["evidence.backtest_edge"] is CheckStatus.FAIL
    assert "NO-GO" in report.verdict()


def test_no_backtest_history_blocks_even_when_paper_passed(store, monkeypatch):
    """Both halves of the roadmap sentence are required, not either one."""
    report = preflight(live_cfg(monkeypatch), store, account=healthy_account(),
                       validation=passing_validation())

    assert statuses(report)["evidence.backtest_edge"] is CheckStatus.FAIL
    assert not report.ready


def test_a_losing_backtest_blocks(store, monkeypatch):
    """A measured negative edge is a refusal, not a number to interpret."""
    for index in range(1000):
        store.record_trade(record(timestamp=NOW + index, pnl=-25.0, r_multiple=-0.4,
                                  mode="backtest"))
    report = preflight(live_cfg(monkeypatch), store, account=healthy_account(),
                       validation=passing_validation())

    check = next(c for c in report.checks if c.name == "evidence.backtest_edge")
    assert check.status is CheckStatus.FAIL
    assert "leverage would only lose it faster" in check.detail


def test_a_thin_backtest_sample_blocks_rather_than_passing(store, monkeypatch):
    """Below the threshold the edge is unestablished, and unestablished blocks here."""
    profitable_backtest(store, count=10)
    report = preflight(live_cfg(monkeypatch), store, account=healthy_account(),
                       validation=passing_validation(),
                       params=ValidationParams(min_trades=1000))

    assert statuses(report)["evidence.backtest_edge"] is CheckStatus.INSUFFICIENT
    assert not report.ready


def test_stored_history_alone_can_never_reach_go(store, monkeypatch):
    """Conduct is an event. A trade table cannot testify to it, so it cannot authorise.

    This is the property that stops someone pointing preflight at a database full of
    profitable rows and reading GO out of it.
    """
    profitable_backtest(store)
    for index in range(1000):
        store.record_trade(record(timestamp=NOW + index, pnl=48.8, r_multiple=1.9,
                                  mode="paper"))
    store.record_equity(EquityPoint(NOW, 10_000.0))

    report = preflight(live_cfg(monkeypatch), store, account=healthy_account(),
                       params=ValidationParams(min_trades=1000))

    check = next(c for c in report.checks if c.name == "evidence.paper_validated")
    assert check.status is CheckStatus.INSUFFICIENT
    assert "supervised paper run" in check.detail
    assert not report.ready


def test_a_failed_validation_is_reported_as_failed_not_merely_unknown(store, monkeypatch):
    """More data fixes withheld; only a fix fixes failed. The operator needs the difference."""
    profitable_backtest(store)
    broken = validate(session_evidence(unprotected=1), [record()],
                      [EquityPoint(NOW, 10_048.8)], params=ValidationParams(min_trades=1))

    report = preflight(live_cfg(monkeypatch), store, account=healthy_account(),
                       validation=broken)
    assert statuses(report)["evidence.paper_validated"] is CheckStatus.FAIL
    assert not report.ready


# --- the gate ----------------------------------------------------------------

def test_a_shut_gate_is_a_blocker_that_says_it_is_the_correct_resting_state(store,
                                                                            monkeypatch):
    """Reported as a blocker without implying anything is broken."""
    report = preflight(load_config(), store)
    check = next(c for c in report.checks if c.name == "gate.three_switches")
    assert check.status is CheckStatus.FAIL
    assert "correct resting state" in check.detail


def test_missing_credentials_block(store, monkeypatch):
    """`load_config` already refuses live-without-keys, so this is the shut-gate path.

    Preflight still checks it rather than trusting the loader: the report's job is to say
    what is in force now, not to assume which validations ran upstream.
    """
    import config as config_module

    monkeypatch.setattr(config_module, "ENV_PATH", config_module.ROOT / ".env.absent")
    monkeypatch.delenv("GATE_API_KEY", raising=False)
    monkeypatch.delenv("GATE_API_SECRET", raising=False)
    cfg = config_module.load_config()

    report = preflight(cfg, store)
    assert statuses(report)["gate.credentials"] is CheckStatus.FAIL
    assert not report.ready


def test_live_mode_without_credentials_is_refused_before_preflight(monkeypatch):
    """The loader is the first barrier; preflight is not the only thing standing there."""
    import config as config_module
    from config import ConfigError

    monkeypatch.setattr(config_module, "ENV_PATH", config_module.ROOT / ".env.absent")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.delenv("GATE_API_KEY", raising=False)
    monkeypatch.delenv("GATE_API_SECRET", raising=False)

    with pytest.raises(ConfigError, match="GATE_API_KEY"):
        config_module.load_config(run_mode="live", confirm_live=True)


def test_the_shipped_config_keeps_martingale_and_averaging_down_off(store, monkeypatch):
    report = preflight(live_cfg(monkeypatch), store)
    found = statuses(report)
    assert found["gate.martingale"] is CheckStatus.PASS
    assert found["gate.averaging_down"] is CheckStatus.PASS


def test_post_only_entry_is_verified_at_the_configured_leverage(store, monkeypatch):
    report = preflight(live_cfg(monkeypatch), store)
    assert statuses(report)["gate.post_only_entry"] is CheckStatus.PASS


# --- the account -------------------------------------------------------------

def test_an_unread_account_blocks_rather_than_being_assumed(store, monkeypatch):
    """Offline preflight must not assume a balance, a margin mode, or a flat book."""
    report = preflight(live_cfg(monkeypatch), store)
    check = next(c for c in report.checks if c.name == "account.reachable")
    assert check.status is CheckStatus.INSUFFICIENT
    assert not report.ready


def test_an_unreachable_account_blocks(store, monkeypatch):
    report = preflight(live_cfg(monkeypatch), store,
                       account=AccountSnapshot.unreachable("INVALID_KEY"))
    assert statuses(report)["account.reachable"] is CheckStatus.FAIL


def test_an_existing_open_position_blocks(store, monkeypatch):
    """Size, stop and liquidation distance all describe a position this bot opened."""
    report = ready(monkeypatch, store, account=healthy_account(open_positions=1))
    check = next(c for c in report.checks if c.name == "account.flat")
    assert check.status is CheckStatus.FAIL
    assert "must start flat" in check.detail
    assert not report.ready


def test_a_resting_order_blocks_too(store, monkeypatch):
    report = ready(monkeypatch, store, account=healthy_account(open_orders=2))
    assert statuses(report)["account.flat"] is CheckStatus.FAIL


def test_cross_margin_blocks(store, monkeypatch):
    """Cross margin puts the whole balance behind one position."""
    report = ready(monkeypatch, store,
                   account=healthy_account(open_positions=1, margin_mode="cross"))
    check = next(c for c in report.checks if c.name == "account.margin_mode")
    assert check.status is CheckStatus.FAIL
    assert "cross margin" in check.detail


def test_an_unfunded_account_blocks(store, monkeypatch):
    report = ready(monkeypatch, store, account=healthy_account(available=0.0))
    assert statuses(report)["account.funded"] is CheckStatus.FAIL


def test_the_snapshot_reads_gate_payloads(store):
    """`from_api` parses what the Phase 2 client actually returns."""
    snapshot = AccountSnapshot.from_api(
        {"total": "10000", "available": "9900", "currency": "USDT"},
        [{"contract": "BTC_USDT", "size": 100, "leverage": "100"},
         {"contract": "ETH_USDT", "size": 0, "leverage": "100"}],
    )
    assert snapshot.reachable
    assert snapshot.total == 10_000.0
    assert snapshot.open_positions == 1          # the size-0 row is not a position
    assert snapshot.margin_mode == "isolated"


def test_a_zero_leverage_position_reads_as_cross_margin():
    """Gate.io reports leverage "0" for cross, which this bot forbids."""
    snapshot = AccountSnapshot.from_api(
        {"total": "1", "available": "1", "currency": "USDT"},
        [{"contract": "BTC_USDT", "size": 5, "leverage": "0"}],
    )
    assert snapshot.margin_mode == "cross"


# --- the latches -------------------------------------------------------------

def test_a_tripped_kill_switch_blocks(store, monkeypatch):
    """A latch outlives the process, so it must outlive a restart into live mode."""
    from risk.risk_manager import Breaker, KillSwitch, SqliteRiskStore

    risk = SqliteRiskStore(store.path)
    risk.trip(KillSwitch(Breaker.DRAWDOWN, NOW, "3% drawdown limit reached", True))

    report = ready(monkeypatch, store)
    check = next(c for c in report.checks if c.name == "risk.breakers_clear")
    assert check.status is CheckStatus.FAIL
    assert "drawdown" in check.detail
    assert not report.ready


# --- fail-closed --------------------------------------------------------------

def test_an_empty_report_is_not_ready():
    """`all()` over nothing is True; the verdict must not inherit that."""
    empty = PreflightReport()
    assert not empty.ready
    assert "nothing was checked" in empty.verdict()


def test_unknown_blocks_exactly_like_failed(store, monkeypatch):
    """The Phase 13 difference: there withheld left conduct clean, here it blocks."""
    report = preflight(live_cfg(monkeypatch), store)
    unknown = [c for c in report.blockers if c.status is CheckStatus.INSUFFICIENT]
    assert unknown, "expected at least one unestablished check"
    assert not report.ready
    assert "could not be established" in report.verdict()


def test_no_store_and_no_validation_blocks_on_evidence(monkeypatch):
    report = preflight(live_cfg(monkeypatch))
    found = statuses(report)
    assert found["evidence.paper_validated"] is CheckStatus.INSUFFICIENT
    assert found["evidence.backtest_edge"] is CheckStatus.INSUFFICIENT
    assert not report.ready


# --- the one path to GO -------------------------------------------------------

def test_every_condition_met_reaches_go(store, monkeypatch):
    """Assembled deliberately, so the refusals above are not refusing trivially."""
    report = ready(monkeypatch, store)
    for check in report.checks:
        assert check.ok, f"{check.name}: {check.detail}"
    assert report.ready
    assert "GO" in report.verdict()


def test_a_go_still_authorises_nothing(store, monkeypatch):
    """The verdict says so in words, because this is the sentence that matters most."""
    report = ready(monkeypatch, store)
    assert report.ready
    verdict = report.verdict()
    assert "authorises nothing" in verdict
    assert "--confirm-live" in verdict
    assert "human" in verdict


def test_removing_any_single_condition_removes_the_go(store, monkeypatch):
    """No check is decorative: each one alone is sufficient to refuse."""
    for name, overrides in (
        ("account.flat", {"account": healthy_account(open_positions=1)}),
        ("account.funded", {"account": healthy_account(available=0.0)}),
        ("account.reachable", {"account": None}),
        ("evidence.paper_validated", {"validation": None}),
    ):
        report = ready(monkeypatch, store, **overrides)
        assert not report.ready, f"{name} did not block a GO"


# --- it cannot open the gate --------------------------------------------------

def test_preflight_owns_no_way_to_enable_live_trading():
    """The audit must not be able to become the thing it audits.

    Scans executable code with the docstrings stripped: the prose *should* name DRY_RUN
    and --confirm-live, since explaining the gate is most of this module's job. What must
    not exist is a statement that writes one.
    """
    import ast
    import execution.preflight as module

    tree = ast.parse(open(module.__file__, encoding="utf-8").read())
    for node in ast.walk(tree):
        # Drop every docstring so documentation is not mistaken for behaviour.
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(
                    body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                node.body = body[1:]
    code = ast.unparse(tree)

    for forbidden in ("os.environ", "setenv", "putenv", "load_dotenv",
                      "place_order", "submit_entry", "aiohttp", "requests"):
        assert forbidden not in code, f"execution/preflight.py executes {forbidden!r}"

    # No assignment to a gate attribute anywhere in the module.
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Attribute):
                assert target.attr not in ("live_enabled", "confirm_live", "env_dry_run",
                                           "run_mode", "dry_run"), \
                    f"preflight assigns to gate attribute {target.attr!r}"


def test_preflight_does_not_mutate_the_config(store, monkeypatch):
    """Reads only. The gate it reports on must be the gate it was handed."""
    cfg = live_cfg(monkeypatch)
    before = (cfg.live_enabled, cfg.env_dry_run, cfg.run_mode, cfg.confirm_live)
    ready(monkeypatch, store)
    preflight(cfg, store)
    assert (cfg.live_enabled, cfg.env_dry_run, cfg.run_mode, cfg.confirm_live) == before


def test_preflight_writes_nothing_to_the_database(store, monkeypatch):
    """An audit that changes its subject is not an audit."""
    profitable_backtest(store, count=5)
    before = (store.count(), len(store.equity_curve()))
    preflight(live_cfg(monkeypatch), store, account=healthy_account())
    assert (store.count(), len(store.equity_curve())) == before


def test_the_report_renders_its_blockers_visibly(store, monkeypatch):
    rendered = preflight(live_cfg(monkeypatch), store).render()
    assert "live readiness" in rendered
    assert "FAIL" in rendered
    assert "evidence.paper_validated" in rendered
    assert "NO-GO" in rendered
