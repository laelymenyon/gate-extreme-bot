"""PHASE 11 tests — database, logging, dashboard.

Three things these tests exist to protect:

1. **The trade record is an audit trail**, so it is append-only, it survives a restart, and
   it shares one file with the Phase 6 kill switches without either clobbering the other.
2. **Secrets never reach a log line** — enforced by a filter on the logger, not by
   discipline at call sites, and verified against message, args, extras and nested values.
3. **The metrics can embarrass the bot.** Empty input yields NaN rather than zero, a small
   sample yields no verdict, and drawdown comes from the equity curve rather than from
   closed trades.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest

from config import load_config
from database.models import SCHEMA_VERSION, EquityPoint, TradeRecord, TradeStore
from monitoring.dashboard import (
    Dashboard,
    Performance,
    compute,
    drawdown_from_curve,
    liquidation_distance_pct,
    max_consecutive_losses,
)
from monitoring.logger import (
    REDACTED,
    ConsoleFormatter,
    JsonFormatter,
    SecretRedactor,
    get_logger,
    log_skip,
    setup_logging,
)
from risk.risk_manager import Breaker, RiskManager, RiskParams, SqliteRiskStore

T0 = 1_754_784_000.0          # 2025-08-10 00:00:00 UTC
EQUITY = 10_000.0


def trade(pnl=50.0, *, offset=0.0, reason="tp3", regime="TRENDING", symbol="BTC_USDT",
          side="long", score=85.0, r=None, fees=5.0, mode="paper", equity=None):
    return TradeRecord(
        timestamp=T0 + offset,
        symbol=symbol,
        side=side,
        leverage=100,
        entry_price=65_000.0,
        exit_price=65_100.0,
        size=1_000,
        margin=76.9,
        stop_loss=64_789.0,
        fees=fees,
        pnl=pnl,
        pnl_percent=pnl / EQUITY,
        r_multiple=(pnl / 25.0) if r is None else r,
        signal_score=score,
        market_regime=regime,
        exit_reason=reason,
        duration_seconds=600.0,
        equity_after=EQUITY + pnl if equity is None else equity,
        mode=mode,
    )


def store(tmp_path, name="trades.db"):
    return TradeStore(tmp_path / name)


# --- the schema ------------------------------------------------------------

def test_the_store_creates_its_tables_and_a_version(tmp_path):
    db = store(tmp_path)
    assert db.schema_version() == SCHEMA_VERSION
    with sqlite3.connect(db.path) as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    assert {"trades", "equity_curve", "schema_meta"} <= tables


def test_the_database_uses_wal(tmp_path):
    db = store(tmp_path)
    with sqlite3.connect(db.path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_a_missing_directory_is_created(tmp_path):
    db = TradeStore(tmp_path / "deep" / "nested" / "trades.db")
    assert db.path.exists()


def test_opening_an_existing_store_does_not_wipe_it(tmp_path):
    first = store(tmp_path)
    first.record_trade(trade())
    assert TradeStore(first.path).count() == 1


# --- it shares the file with the Phase 6 kill switches --------------------

def test_the_trade_store_and_the_risk_store_coexist(tmp_path):
    """One file, two owners. A restart must recover breakers and history together."""
    path = tmp_path / "trades.db"
    risk = RiskManager(RiskParams(), SqliteRiskStore(path))
    risk.can_trade(now=T0, equity=EQUITY, open_positions=0)
    risk.trip("operator halted", now=T0)

    trades = TradeStore(path)
    trades.record_trade(trade())

    # Neither dropped the other's tables.
    assert trades.count() == 1
    reloaded = RiskManager(RiskParams(), SqliteRiskStore(path))
    assert Breaker.MANUAL in reloaded.kill_switches
    assert reloaded.state is not None


def test_the_store_reads_kill_switches_but_never_writes_them(tmp_path):
    path = tmp_path / "trades.db"
    risk = RiskManager(RiskParams(), SqliteRiskStore(path))
    risk.can_trade(now=T0, equity=EQUITY, open_positions=0)
    risk.trip("drawdown acknowledged", now=T0, breaker=Breaker.MANUAL)

    reported = TradeStore(path).kill_switches()
    assert reported == {"manual": "drawdown acknowledged"}
    assert not hasattr(TradeStore, "trip")
    assert not hasattr(TradeStore, "clear")


def test_kill_switches_are_empty_when_no_risk_manager_has_run(tmp_path):
    """A missing table means "not used yet", not an error."""
    assert store(tmp_path).kill_switches() == {}


# --- append-only -----------------------------------------------------------

def test_there_is_no_way_to_edit_or_delete_a_trade():
    """A record that can be rewritten after the fact is not an audit trail."""
    for name in ("update_trade", "delete_trade", "delete", "update", "clear_trades"):
        assert not hasattr(TradeStore, name)


def test_trades_round_trip_every_field(tmp_path):
    db = store(tmp_path)
    original = trade(pnl=-42.5, reason="stop", regime="RANGING", score=91.5)
    row_id = db.record_trade(original)
    [loaded] = db.trades()

    assert loaded.id == row_id
    for field in ("timestamp", "symbol", "side", "leverage", "entry_price", "exit_price",
                  "size", "margin", "stop_loss", "fees", "pnl", "pnl_percent",
                  "r_multiple", "signal_score", "market_regime", "exit_reason",
                  "duration_seconds", "equity_after", "mode"):
        assert getattr(loaded, field) == getattr(original, field), field


def test_the_reasoning_is_stored_not_just_the_pnl(tmp_path):
    """"Which setups, in which regime, at what score" is the question worth asking."""
    db = store(tmp_path)
    db.record_trade(trade(regime="BREAKOUT", score=88.0, reason="tp2"))
    [loaded] = db.trades()
    assert loaded.market_regime == "BREAKOUT"
    assert loaded.signal_score == 88.0
    assert loaded.exit_reason == "tp2"


def test_trades_come_back_in_chronological_order(tmp_path):
    db = store(tmp_path)
    for offset in (600.0, 0.0, 1200.0):
        db.record_trade(trade(offset=offset))
    assert [t.timestamp for t in db.trades()] == [T0, T0 + 600.0, T0 + 1200.0]


@pytest.mark.parametrize("kwargs,expected", [
    ({"symbol": "ETH_USDT"}, 1),
    ({"since": T0 + 600.0}, 2),
    ({"mode": "backtest"}, 1),
])
def test_trades_can_be_filtered(tmp_path, kwargs, expected):
    db = store(tmp_path)
    db.record_trade(trade(offset=0.0))
    db.record_trade(trade(offset=600.0, symbol="ETH_USDT"))
    db.record_trade(trade(offset=1200.0, mode="backtest"))
    assert len(db.trades(**kwargs)) == expected


def test_history_survives_a_restart(tmp_path):
    db = store(tmp_path)
    db.record_trades([trade(offset=0.0), trade(offset=60.0, pnl=-10.0)])
    reopened = TradeStore(db.path)
    assert reopened.count() == 2
    assert [t.pnl for t in reopened.trades()] == [50.0, -10.0]


# --- the equity curve is independent of the trades ------------------------

def test_the_equity_curve_is_recorded_separately(tmp_path):
    """Drawdown at 100x arrives through mark price, without any trade closing."""
    db = store(tmp_path)
    for i in range(4):
        db.record_equity(EquityPoint(T0 + i * 60, EQUITY - i * 100, open_positions=1))
    assert db.count() == 0                      # no trade closed
    curve = db.equity_curve()
    assert [point.equity for point in curve] == [10_000.0, 9_900.0, 9_800.0, 9_700.0]
    assert all(point.open_positions == 1 for point in curve)


def test_the_curve_can_be_filtered_by_time(tmp_path):
    db = store(tmp_path)
    for i in range(4):
        db.record_equity(EquityPoint(T0 + i * 60, EQUITY))
    assert len(db.equity_curve(since=T0 + 120)) == 2


def test_drawdown_from_an_open_position_beats_a_trade_only_view(tmp_path):
    """The mark-price dip is the number that decides survival, and only the curve has it."""
    db = store(tmp_path)
    db.record_equity(EquityPoint(T0, EQUITY))
    db.record_equity(EquityPoint(T0 + 60, EQUITY * 0.95))     # unrealised
    db.record_equity(EquityPoint(T0 + 120, EQUITY))
    db.record_trade(trade(pnl=1.0, offset=180.0, equity=EQUITY + 1.0))

    dashboard = Dashboard(db, starting_equity=EQUITY)
    performance = dashboard.performance()
    assert performance.max_drawdown == pytest.approx(0.05)
    assert performance.net_pnl == 1.0            # the closed trade barely moved


# --- adapters --------------------------------------------------------------

def test_a_paper_trade_converts_without_losing_the_reasoning():
    class PaperTrade:
        symbol = "BTC_USDT"
        direction = -1
        entry_time = T0
        exit_time = T0 + 900
        entry_price = 65_000.0
        exit_price = 64_500.0
        size = -1_000
        stop_price = 65_211.0
        exit_reason = "tp1"
        fees = 6.0
        net_pnl = 44.0
        equity_after = 10_044.0
        score = 87.0

    record = TradeRecord.from_paper(PaperTrade(), leverage=100, regime="TRENDING")
    assert record.side == "short"
    assert record.direction == -1
    assert record.pnl == 44.0
    assert record.signal_score == 87.0
    assert record.market_regime == "TRENDING"
    assert record.duration_seconds == 900
    assert record.pnl_percent == pytest.approx(44.0 / 10_000.0)
    assert record.won


def test_a_backtest_trade_uses_the_same_adapter():
    """Paper and backtest history must be comparable, so one adapter serves both."""
    from backtest.engine import Trade

    bt = Trade(symbol="BTC_USDT", direction=1, entry_time=T0, exit_time=T0 + 60,
               entry_price=65_000.0, exit_price=65_200.0, size=1_000,
               stop_price=64_789.0, exit_reason="stop", gross_pnl=20.0, fees=5.0,
               funding=0.5, r_multiple=0.6, equity_after=10_014.5, score=83.0)
    record = TradeRecord.from_paper(bt, leverage=100, mode="backtest")
    assert record.mode == "backtest"
    assert record.pnl == pytest.approx(bt.net_pnl)
    assert record.funding == 0.5
    assert record.r_multiple == 0.6


def test_the_store_builds_from_config(tmp_path, monkeypatch):
    cfg = load_config()
    monkeypatch.chdir(tmp_path)
    db = TradeStore.from_config(cfg)
    assert db.path.name == "trades.db"
    assert db.wal is True


# --- secret redaction ------------------------------------------------------

def test_a_registered_secret_is_redacted():
    redactor = SecretRedactor(["SUPERSECRETKEY123456"])
    assert "SUPERSECRETKEY123456" not in redactor.scrub("key=SUPERSECRETKEY123456")
    assert REDACTED in redactor.scrub("key=SUPERSECRETKEY123456")


def test_a_signature_shaped_value_is_redacted_even_if_unregistered():
    """The failure being prevented is an operator pasting a log into an issue tracker."""
    redactor = SecretRedactor()
    signature = "a3f8" * 24
    assert signature not in redactor.scrub(f"SIGN={signature}")


def test_env_credentials_are_picked_up_automatically(monkeypatch):
    monkeypatch.setenv("GATE_API_KEY", "K" * 32)
    monkeypatch.setenv("GATE_API_SECRET", "S" * 40)
    redactor = SecretRedactor()
    scrubbed = redactor.scrub(f"key={'K' * 32} secret={'S' * 40}")
    assert "K" * 32 not in scrubbed
    assert "S" * 40 not in scrubbed


def test_secret_keyed_fields_are_redacted_by_name():
    redactor = SecretRedactor()
    scrubbed = redactor.scrub({"SIGN": "short", "sign": "x", "note": "fine"})
    assert scrubbed["SIGN"] == REDACTED
    assert scrubbed["sign"] == REDACTED
    assert scrubbed["note"] == "fine"


def test_nested_secrets_cannot_escape():
    redactor = SecretRedactor(["TOPSECRETVALUE99"])
    payload = {"headers": {"SIGN": "abc"}, "body": ["TOPSECRETVALUE99", {"k": "TOPSECRETVALUE99"}]}
    scrubbed = redactor.scrub(payload)
    assert scrubbed["headers"]["SIGN"] == REDACTED
    assert scrubbed["body"][0] == REDACTED
    assert scrubbed["body"][1]["k"] == REDACTED


def test_redaction_applies_to_the_whole_record(tmp_path):
    """Attached to the logger, so message, args and extras are all covered."""
    path = tmp_path / "bot.log"
    logger = setup_logging(level="INFO", path=str(path), console=False,
                           secrets=["LEAKEDSECRET1234"])
    logger.info("submitting with key=%s", "LEAKEDSECRET1234",
                extra={"SIGN": "abcd", "detail": "LEAKEDSECRET1234"})
    contents = path.read_text(encoding="utf-8")
    assert "LEAKEDSECRET1234" not in contents
    assert contents.count(REDACTED) >= 2


def test_config_credentials_are_registered(tmp_path, monkeypatch):
    monkeypatch.setenv("GATE_API_KEY", "A" * 30)
    monkeypatch.setenv("GATE_API_SECRET", "B" * 30)
    cfg = load_config()
    path = tmp_path / "bot.log"
    logger = setup_logging(cfg, path=str(path), console=False)
    logger.info("creds %s / %s", "A" * 30, "B" * 30)
    contents = path.read_text(encoding="utf-8")
    assert "A" * 30 not in contents
    assert "B" * 30 not in contents


def test_redaction_cannot_be_switched_off(tmp_path, monkeypatch):
    """A switch that disables redaction is one that eventually gets left off."""
    import config as config_module
    import yaml as _yaml

    with config_module.CONFIG_PATH.open(encoding="utf-8") as handle:
        raw = _yaml.safe_load(handle)
    raw["logging"]["redact_secrets"] = False
    variant = tmp_path / "config.yaml"
    variant.write_text(_yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_PATH", variant)
    cfg = config_module.load_config()

    path = tmp_path / "bot.log"
    logger = setup_logging(cfg, path=str(path), console=False, secrets=["STILLSECRET123"])
    logger.info("value=%s", "STILLSECRET123")
    assert "STILLSECRET123" not in path.read_text(encoding="utf-8")


def test_short_values_are_not_registered_as_secrets():
    """Masking "1" would redact half of every log line and hide nothing useful."""
    redactor = SecretRedactor(["abc"])
    assert redactor.scrub("value=abc") == "value=abc"


# --- log shape -------------------------------------------------------------

def test_the_file_is_one_json_object_per_line(tmp_path):
    path = tmp_path / "bot.log"
    logger = setup_logging(level="INFO", path=str(path), console=False)
    logger.info("first", extra={"symbol": "BTC_USDT"})
    logger.warning("second")
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 2
    payload = json.loads(lines[0])
    assert payload["message"] == "first"
    assert payload["symbol"] == "BTC_USDT"
    assert payload["level"] == "INFO"


def test_an_exception_is_captured_in_the_record(tmp_path):
    path = tmp_path / "bot.log"
    logger = setup_logging(level="INFO", path=str(path), console=False)
    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("failed")
    payload = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert "ValueError: boom" in payload["exception"]


def test_setup_is_idempotent(tmp_path):
    """Calling it twice must not double every line."""
    path = tmp_path / "bot.log"
    setup_logging(level="INFO", path=str(path), console=False)
    logger = setup_logging(level="INFO", path=str(path), console=False)
    logger.info("once")
    assert len([l for l in path.read_text(encoding="utf-8").splitlines() if l]) == 1
    assert len([f for f in logger.filters if isinstance(f, SecretRedactor)]) == 1


def test_a_skip_records_its_stage(tmp_path):
    """Skips are the primary signal: the bot is designed to reject almost everything."""
    path = tmp_path / "bot.log"
    logger = setup_logging(level="INFO", path=str(path), console=False)
    log_skip(logger, "BTC_USDT", "regime", "no clear regime on 5m", bars=210)
    payload = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert payload["event"] == "skip"
    assert payload["stage"] == "regime"
    assert payload["symbol"] == "BTC_USDT"
    assert payload["bars"] == 210


def test_the_console_line_is_human_readable():
    record = logging.LogRecord("gate", logging.INFO, __file__, 1, "no clear regime",
                               None, None)
    record.symbol = "BTC_USDT"
    record.stage = "regime"
    rendered = ConsoleFormatter().format(record)
    assert "BTC_USDT regime: no clear regime" in rendered
    assert "INFO" in rendered


def test_get_logger_namespaces_under_gate():
    assert get_logger("paper").name == "gate.paper"
    assert get_logger().name == "gate"


# --- metrics ---------------------------------------------------------------

def test_no_trades_yields_nan_not_zero():
    """"Nothing was learned" must not read as "every trade lost"."""
    performance = compute([], [])
    assert performance.trades == 0
    import math

    assert math.isnan(performance.win_rate)
    assert math.isnan(performance.profit_factor)
    assert math.isnan(performance.expectancy)
    assert "no trades" in performance.verdict()


def test_the_metric_set_matches_the_contract():
    trades = [trade(50.0, offset=0), trade(-25.0, offset=60), trade(80.0, offset=120),
              trade(-25.0, offset=180)]
    performance = compute(trades, [], starting_equity=EQUITY)
    assert performance.trades == 4
    assert performance.wins == 2 and performance.losses == 2
    assert performance.win_rate == 0.5
    assert performance.loss_rate == 0.5
    assert performance.profit_factor == pytest.approx(130 / 50)
    assert performance.expectancy == pytest.approx(80 / 4)
    assert performance.avg_win == pytest.approx(65.0)
    assert performance.avg_loss == pytest.approx(-25.0)
    assert performance.largest_win == 80.0
    assert performance.largest_loss == -25.0
    assert performance.fees_paid == 20.0
    assert performance.net_pnl == 80.0


def test_expectancy_is_reported_in_r_as_well_as_cash():
    """R is what survives a change of account size; cash is not."""
    trades = [trade(50.0, r=2.0), trade(-25.0, r=-1.0)]
    assert compute(trades, []).expectancy_r == pytest.approx(0.5)


def test_profit_factor_is_infinite_when_nothing_was_lost():
    assert compute([trade(50.0), trade(20.0)], []).profit_factor == float("inf")


@pytest.mark.parametrize("pnls,expected", [
    ([-1, -1, -1], 3),
    ([-1, 1, -1, -1], 2),
    ([1, 1], 0),
    ([-1, 0, -1], 2),          # a scratch neither extends nor breaks the streak
    ([], 0),
])
def test_max_consecutive_losses(pnls, expected):
    assert max_consecutive_losses([trade(float(p)) for p in pnls]) == expected


def test_drawdown_is_measured_from_the_running_peak():
    curve = [EquityPoint(T0, 10_000.0), EquityPoint(T0 + 1, 12_000.0),
             EquityPoint(T0 + 2, 10_800.0), EquityPoint(T0 + 3, 11_000.0)]
    drawdown, peak = drawdown_from_curve(curve)
    assert peak == 12_000.0
    assert drawdown == pytest.approx(0.10)


def test_drawdown_of_an_empty_curve_is_zero():
    assert drawdown_from_curve([]) == (0.0, 0.0)


def test_liquidation_distance_is_reported_and_fails_to_nan():
    import math

    assert liquidation_distance_pct(65_000.0, 64_594.0) == pytest.approx(0.00624, abs=1e-4)
    assert math.isnan(liquidation_distance_pct(0.0, 64_594.0))
    assert math.isnan(liquidation_distance_pct(65_000.0, float("nan")))


def test_daily_pnl_counts_only_today():
    trades = [trade(100.0, offset=0.0), trade(-40.0, offset=86_400.0 + 60)]
    performance = compute(trades, [], now=T0 + 86_400.0 + 120)
    assert performance.trades_today == 1
    assert performance.daily_pnl == -40.0


def test_liquidations_are_counted_separately():
    trades = [trade(-300.0, reason="liquidation"), trade(-25.0, reason="stop")]
    assert compute(trades, []).liquidations == 1


def test_exits_and_regimes_are_tallied():
    trades = [trade(reason="stop", regime="TRENDING"),
              trade(reason="stop", regime="RANGING"),
              trade(reason="tp3", regime="TRENDING")]
    performance = compute(trades, [])
    assert performance.by_exit_reason == {"stop": 2, "tp3": 1}
    assert performance.by_regime == {"TRENDING": 2, "RANGING": 1}


# --- the verdict refuses ---------------------------------------------------

def test_a_small_sample_gets_no_verdict():
    performance = compute([trade(50.0)] * 5, [], min_trades_for_verdict=1000)
    assert not performance.conclusive
    assert "INCONCLUSIVE" in performance.verdict()


def test_a_losing_sample_large_enough_is_called_negative():
    performance = compute([trade(-25.0, r=-1.0)] * 3, [], min_trades_for_verdict=3)
    assert performance.conclusive
    assert "NEGATIVE" in performance.verdict()
    assert "Leverage would only lose it faster" in performance.verdict()


def test_a_winning_sample_large_enough_is_called_positive():
    performance = compute([trade(50.0, r=2.0)] * 3, [], min_trades_for_verdict=3)
    assert performance.conclusive
    assert "positive" in performance.verdict()


def test_the_dashboard_threshold_matches_phase_9():
    """The dashboard must not quietly disagree with the backtester about sample size."""
    cfg = load_config()
    dashboard = Dashboard.from_config(cfg, TradeStore(Path("data/trades.db")))
    assert dashboard.min_trades_for_verdict == cfg.get("backtest.min_trades_for_verdict")


def test_nothing_is_annualised_or_extrapolated():
    """A 3-day paper run does not imply a yearly return, so no field claims one."""
    fields = set(Performance.__dataclass_fields__)
    for invented in ("annualised_return", "cagr", "sharpe", "projected_return",
                     "monthly_return"):
        assert invented not in fields


# --- rendering -------------------------------------------------------------

def test_the_report_shows_losses_as_prominently_as_wins(tmp_path):
    db = store(tmp_path)
    db.record_trades([trade(50.0, offset=0.0), trade(-25.0, offset=60.0, reason="stop"),
                      trade(-300.0, offset=120.0, reason="liquidation")])
    rendered = Dashboard(db, starting_equity=EQUITY).render()
    for expected in ("win rate", "loss rate", "largest win / loss",
                     "max consecutive losses", "liquidations", "max drawdown",
                     "fees paid", "verdict"):
        assert expected in rendered
    assert "-300.00" in rendered


def test_the_report_surfaces_a_tripped_kill_switch(tmp_path):
    path = tmp_path / "trades.db"
    risk = RiskManager(RiskParams(), SqliteRiskStore(path))
    risk.can_trade(now=T0, equity=EQUITY, open_positions=0)
    risk.trip("daily loss acknowledged", now=T0)

    db = TradeStore(path)
    db.record_trade(trade())
    rendered = Dashboard(db).render()
    assert "KILL SWITCHES TRIPPED" in rendered
    assert "daily loss acknowledged" in rendered


def test_the_report_handles_an_empty_database(tmp_path):
    rendered = Dashboard(store(tmp_path)).render()
    assert "trades" in rendered
    assert "no trades" in rendered


def test_the_report_can_be_scoped_to_one_symbol(tmp_path):
    db = store(tmp_path)
    db.record_trade(trade(50.0, symbol="BTC_USDT"))
    db.record_trade(trade(-25.0, offset=60.0, symbol="ETH_USDT"))
    assert Dashboard(db).performance(symbol="ETH_USDT").trades == 1
    assert "ETH_USDT" in Dashboard(db).render(symbol="ETH_USDT")


# --- no order path ---------------------------------------------------------

def test_phase_11_cannot_trade():
    """Storage and reporting only: no order path, no network."""
    import importlib

    for name in ("database.models", "monitoring.dashboard", "monitoring.logger"):
        module = importlib.import_module(name)
        source = Path(module.__file__).read_text(encoding="utf-8")
        imports = "\n".join(
            line for line in source.splitlines() if line.startswith(("import ", "from "))
        )
        assert "exchange" not in imports, name
        assert "execution" not in imports, name
        assert "aiohttp" not in imports, name

    for cls in (TradeStore, Dashboard):
        names = [n for n in dir(cls) if not n.startswith("_")]
        assert not [n for n in names
                    if any(word in n.lower()
                           for word in ("order", "place", "submit", "cancel", "close"))]
