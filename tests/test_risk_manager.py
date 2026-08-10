"""PHASE 6 tests — the breaker ladder.

Two properties matter more than any individual limit:

1. **A tripped breaker latches.** It stays tripped across a restart, and only the clock
   (daily loss, cooldowns, streak) or a human (drawdown) clears it.
2. **The unknown case refuses.** Missing equity, a broken clock, a corrupt state file —
   every one of them produces "no", never "probably fine".

Time is injected everywhere, so nothing here sleeps or depends on the wall clock.
"""

from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

import pytest

from config import load_config
from risk.risk_manager import (
    Breaker,
    MemoryRiskStore,
    RiskDecision,
    RiskManager,
    RiskParams,
    SqliteRiskStore,
    utc_day,
)

#: 2025-08-10 00:00:00 UTC — a day boundary, so offsets read as time-of-day.
T0 = 1_754_784_000.0
DAY = 86_400.0
EQUITY = 10_000.0


def manager(store=None, **params):
    return RiskManager(RiskParams(**params), store if store is not None else MemoryRiskStore())


def allowed(m, now, equity=EQUITY, open_positions=0):
    return m.can_trade(now=now, equity=equity, open_positions=open_positions)


# --- the happy path --------------------------------------------------------

def test_a_fresh_manager_permits_trading():
    decision = allowed(manager(), T0)
    assert decision.allowed
    assert decision.breaker is None
    assert "permitted" in decision.summary()


def test_a_win_only_serves_the_shorter_cooldown():
    m = manager()
    allowed(m, T0)
    m.record_trade(now=T0, pnl=+50.0)
    assert not allowed(m, T0 + 30).allowed          # inside the 60s post-win cooldown
    assert allowed(m, T0 + 61).allowed
    assert m.state.consecutive_losses == 0


def test_a_loss_serves_the_longer_cooldown():
    m = manager()
    allowed(m, T0)
    m.record_trade(now=T0, pnl=-25.0)
    blocked = allowed(m, T0 + 100)
    assert not blocked.allowed
    assert blocked.breaker is Breaker.COOLDOWN
    assert "200s of the 300s post-loss cooldown remain" in blocked.reason
    assert blocked.retry_after == pytest.approx(200.0)
    assert allowed(m, T0 + 301).allowed


def test_a_breakeven_trade_counts_as_neither_win_nor_loss_for_the_streak():
    m = manager()
    allowed(m, T0)
    m.record_trade(now=T0, pnl=-10.0)
    assert m.state.consecutive_losses == 1
    m.record_trade(now=T0 + 400, pnl=0.0)
    assert m.state.consecutive_losses == 1          # a scratch does not break the streak


def test_position_size_never_scales_with_recent_losses():
    """No martingale: the risk fraction takes no arguments, so there is nothing to scale."""
    m = manager(max_consecutive_losses=99)
    allowed(m, T0)
    before = m.risk_fraction()
    for i in range(5):
        m.record_trade(now=T0 + i * 400, pnl=-20.0)
    assert m.risk_fraction() == before == 0.0025
    assert m.risk_amount(EQUITY) == 25.0


# --- consecutive losses ----------------------------------------------------

def test_three_consecutive_losses_latch_until_the_next_day():
    m = manager()
    allowed(m, T0)
    for i in range(3):
        m.record_trade(now=T0 + i * 400, pnl=-20.0)

    blocked = allowed(m, T0 + 5_000)
    assert not blocked.allowed
    assert blocked.breaker is Breaker.CONSECUTIVE_LOSSES
    assert "3 consecutive losses" in blocked.reason

    # Still latched late the same day, long past every cooldown.
    assert not allowed(m, T0 + DAY - 1).allowed
    # The next UTC day clears it, and the counter with it.
    assert allowed(m, T0 + DAY + 1).allowed
    assert m.state.consecutive_losses == 0


def test_a_win_resets_the_streak_before_it_latches():
    m = manager()
    allowed(m, T0)
    m.record_trade(now=T0, pnl=-20.0)
    m.record_trade(now=T0 + 400, pnl=-20.0)
    m.record_trade(now=T0 + 800, pnl=+40.0)
    m.record_trade(now=T0 + 1_200, pnl=-20.0)
    assert m.state.consecutive_losses == 1
    assert allowed(m, T0 + 2_000).allowed


# --- daily loss ------------------------------------------------------------

def test_daily_loss_trips_at_the_limit_and_clears_overnight():
    m = manager(max_consecutive_losses=99)
    allowed(m, T0)
    m.record_trade(now=T0, pnl=-100.0, equity=EQUITY - 100)   # exactly 1.0% of 10k
    blocked = allowed(m, T0 + 5_000, equity=EQUITY - 100)
    assert not blocked.allowed
    assert blocked.breaker is Breaker.DAILY_LOSS
    assert "1.00%" in blocked.reason

    resumed = allowed(m, T0 + DAY + 1, equity=EQUITY - 100)
    assert resumed.allowed
    assert m.state.day == utc_day(T0 + DAY + 1)
    assert m.state.day_start_equity == EQUITY - 100      # the new day starts from here


def test_just_under_the_daily_limit_does_not_trip():
    m = manager(max_consecutive_losses=99)
    allowed(m, T0)
    m.record_trade(now=T0, pnl=-99.0, equity=EQUITY - 99)     # 0.99%
    assert allowed(m, T0 + 5_000, equity=EQUITY - 99).allowed


def test_the_day_baseline_is_the_days_starting_equity_not_the_peak():
    """After an overnight reset, yesterday's loss must not count against today."""
    m = manager(max_consecutive_losses=99)
    allowed(m, T0)
    m.record_trade(now=T0, pnl=-90.0, equity=9_910.0)     # 0.9%: under the 1% limit
    assert allowed(m, T0 + 5_000, equity=9_910.0).allowed

    # New day. A further 0.9% loss is 0.9% *of today*, so it must not trip.
    allowed(m, T0 + DAY, equity=9_910.0)
    m.record_trade(now=T0 + DAY + 10, pnl=-89.0, equity=9_821.0)
    decision = allowed(m, T0 + DAY + 5_000, equity=9_821.0)
    assert decision.allowed, decision.reason


def test_intraday_equity_alone_trips_the_daily_breaker():
    """An open position bleeding out must halt trading before any trade is recorded.

    At 100x a drawdown arrives through the mark price, not through a closed trade; a
    breaker that only counts settled PnL would notice far too late.
    """
    m = manager()
    allowed(m, T0)
    blocked = allowed(m, T0 + 60, equity=EQUITY * 0.985)
    assert not blocked.allowed
    assert blocked.breaker is Breaker.DAILY_LOSS


# --- drawdown --------------------------------------------------------------

def test_drawdown_latches_and_survives_the_next_day():
    m = manager(max_consecutive_losses=99)
    allowed(m, T0)
    m.record_trade(now=T0, pnl=-300.0, equity=9_700.0)    # 3% off the 10k peak
    blocked = allowed(m, T0 + 60, equity=9_700.0)
    assert not blocked.allowed
    assert blocked.breaker is Breaker.DRAWDOWN

    # A new day clears the daily breaker. The drawdown latch is not on a timer.
    for offset in (DAY + 1, 5 * DAY, 400 * DAY):
        assert not allowed(m, T0 + offset, equity=9_700.0).allowed
    # Nor does recovering the equity clear it.
    assert not allowed(m, T0 + 500 * DAY, equity=11_000.0).allowed


def test_the_high_water_mark_only_ever_rises():
    m = manager()
    allowed(m, T0, equity=EQUITY)
    allowed(m, T0 + 100, equity=12_000.0)
    assert m.state.peak_equity == 12_000.0
    allowed(m, T0 + 200, equity=11_700.0)                 # 2.5% off the peak
    assert m.state.peak_equity == 12_000.0
    # 3% below the *peak*, not below the day's opening balance — the account is still
    # 16% up on the day, so only the drawdown breaker can be the one that fires.
    blocked = allowed(m, T0 + 300, equity=12_000.0 * 0.97)
    assert not blocked.allowed
    assert blocked.breaker is Breaker.DRAWDOWN


def test_drawdown_is_reported_ahead_of_the_daily_limit():
    """When both are breached, surface the one that needs a human."""
    m = manager(max_consecutive_losses=99)
    allowed(m, T0)
    decision = allowed(m, T0 + 60, equity=EQUITY * 0.90)
    assert not decision.allowed
    assert decision.breaker is Breaker.DRAWDOWN


# --- open positions, symbols already held, manual halt --------------------

def test_max_open_positions_blocks_without_latching():
    m = manager()
    blocked = allowed(m, T0, open_positions=1)
    assert not blocked.allowed
    assert blocked.breaker is Breaker.MAX_OPEN_POSITIONS
    assert not m.tripped                       # a condition, not a latch
    assert allowed(m, T0 + 1, open_positions=0).allowed


def test_adding_to_a_held_symbol_is_refused_as_averaging_down():
    m = manager(max_open_positions=3)
    m.can_trade(now=T0, equity=EQUITY, open_positions=1, symbol="BTC_USDT",
                open_symbols=["ETH_USDT"])
    blocked = m.can_trade(now=T0, equity=EQUITY, open_positions=1, symbol="BTC_USDT",
                          open_symbols=["BTC_USDT"])
    assert not blocked.allowed
    assert blocked.breaker is Breaker.ALREADY_OPEN
    assert "averaging down" in blocked.reason


def test_manual_halt_latches_until_reset():
    m = manager()
    m.trip("operator halted the bot", now=T0)
    blocked = allowed(m, T0)
    assert not blocked.allowed
    assert blocked.breaker is Breaker.MANUAL
    assert blocked.reason == "operator halted the bot"
    assert not allowed(m, T0 + 90 * DAY).allowed
    m.reset(Breaker.MANUAL)
    assert allowed(m, T0 + 90 * DAY + 1).allowed


# --- reset semantics -------------------------------------------------------

def test_reset_rebaselines_so_it_does_not_instantly_retrip():
    """Clearing a latch while the condition still holds would make the reset theatre.

    The drawdown is carried in from the previous day, so it is the only breaker in play:
    the account is flat on the day and 3% off its high-water mark.
    """
    m = manager()
    allowed(m, T0)                                        # peak 10_000
    blocked = allowed(m, T0 + DAY, equity=9_700.0)
    assert blocked.breaker is Breaker.DRAWDOWN

    assert m.reset(Breaker.DRAWDOWN, now=T0 + DAY + 10) == (Breaker.DRAWDOWN,)
    assert m.state.peak_equity == 9_700.0                 # measurement restarts here
    assert allowed(m, T0 + DAY + 20, equity=9_700.0).allowed
    # The new high-water mark is honoured: another 3% down trips again.
    assert not allowed(m, T0 + DAY + 30, equity=9_700.0 * 0.97).allowed


def test_reset_of_the_streak_latch_clears_the_counter():
    m = manager()
    allowed(m, T0)
    for i in range(3):
        m.record_trade(now=T0 + i * 400, pnl=-20.0)
    m.reset(Breaker.CONSECUTIVE_LOSSES)
    assert m.state.consecutive_losses == 0
    assert allowed(m, T0 + 5_000).allowed


def test_bare_reset_clears_everything():
    m = manager()
    allowed(m, T0)
    m.trip("halt", now=T0)
    m.record_trade(now=T0, pnl=-400.0, equity=9_600.0)
    cleared = m.reset(now=T0 + 10)
    assert set(cleared) >= {Breaker.MANUAL, Breaker.DRAWDOWN, Breaker.DAILY_LOSS}
    assert not m.tripped
    assert allowed(m, T0 + 400, equity=9_600.0).allowed


def test_resetting_one_breaker_leaves_the_others_latched():
    m = manager()
    allowed(m, T0)
    m.trip("halt", now=T0)
    # Drawdown outranks a manual halt in the reporting order.
    assert allowed(m, T0 + DAY, equity=9_700.0).breaker is Breaker.DRAWDOWN
    m.reset(Breaker.DRAWDOWN, now=T0 + DAY + 5)
    assert allowed(m, T0 + DAY + 10, equity=9_700.0).breaker is Breaker.MANUAL


def test_retripping_keeps_the_original_reason():
    m = manager()
    m.trip("the first reason", now=T0)
    m.trip("a later, less informative reason", now=T0 + 100)
    assert m.kill_switches[Breaker.MANUAL].reason == "the first reason"
    assert m.kill_switches[Breaker.MANUAL].tripped_at == T0


# --- unknown state refuses -------------------------------------------------

@pytest.mark.parametrize("equity", [None, float("nan"), float("inf"), -1.0, 0.0, "10000"])
def test_unusable_equity_refuses(equity):
    decision = manager().can_trade(now=T0, equity=equity, open_positions=0)
    assert not decision.allowed
    assert decision.breaker is Breaker.UNKNOWN_STATE


@pytest.mark.parametrize("now", [None, float("nan"), float("inf"), -1.0, 0.0])
def test_unusable_clock_refuses(now):
    decision = manager().can_trade(now=now, equity=EQUITY, open_positions=0)
    assert not decision.allowed
    assert decision.breaker is Breaker.UNKNOWN_STATE


@pytest.mark.parametrize("count", [None, -1, 1.5, "one", float("nan")])
def test_unusable_position_count_refuses(count):
    decision = manager().can_trade(now=T0, equity=EQUITY, open_positions=count)
    assert not decision.allowed
    assert decision.breaker is Breaker.UNKNOWN_STATE


def test_a_clock_going_backwards_refuses():
    """Time running backwards means the cooldown arithmetic cannot be trusted."""
    m = manager()
    allowed(m, T0 + 1_000)
    decision = allowed(m, T0 + 500)
    assert not decision.allowed
    assert decision.breaker is Breaker.UNKNOWN_STATE
    assert "backwards" in decision.reason


def test_sub_second_clock_jitter_is_tolerated():
    """An NTP correction is not a corrupted state; a jump of seconds is."""
    m = manager()
    allowed(m, T0 + 1_000)
    assert allowed(m, T0 + 999.5).allowed


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), None])
def test_recording_an_uninterpretable_trade_raises(bad):
    """A garbage trade record is a caller bug, and it must not be absorbed silently."""
    m = manager()
    allowed(m, T0)
    with pytest.raises(ValueError):
        m.record_trade(now=T0 + 10, pnl=bad)


def test_recording_a_trade_before_any_equity_is_known_raises():
    with pytest.raises(ValueError, match="equity"):
        manager().record_trade(now=T0, pnl=-10.0)


# --- persistence -----------------------------------------------------------

def test_state_and_latches_survive_a_restart(tmp_path):
    db = tmp_path / "nested" / "trades.db"      # the directory is created on demand
    m = manager(SqliteRiskStore(db), max_consecutive_losses=99)
    allowed(m, T0)
    m.record_trade(now=T0, pnl=-350.0, equity=9_650.0)
    assert m.tripped

    restarted = manager(SqliteRiskStore(db), max_consecutive_losses=99)
    decision = allowed(restarted, T0 + 10 * DAY, equity=9_650.0)
    assert not decision.allowed
    assert decision.breaker is Breaker.DRAWDOWN
    assert restarted.state.peak_equity == EQUITY            # the high-water mark persisted
    assert restarted.state.last_trade_won is False


def test_a_restart_cannot_clear_a_losing_streak(tmp_path):
    db = tmp_path / "trades.db"
    m = manager(SqliteRiskStore(db))
    allowed(m, T0)
    for i in range(3):
        m.record_trade(now=T0 + i * 400, pnl=-20.0)

    restarted = manager(SqliteRiskStore(db))
    assert restarted.state.consecutive_losses == 3
    assert allowed(restarted, T0 + 5_000).breaker is Breaker.CONSECUTIVE_LOSSES


def test_a_reset_also_survives_a_restart(tmp_path):
    db = tmp_path / "trades.db"
    m = manager(SqliteRiskStore(db))
    allowed(m, T0)
    # Yesterday's high-water mark against today's balance: drawdown alone is in play.
    assert allowed(m, T0 + DAY, equity=9_650.0).breaker is Breaker.DRAWDOWN
    m.reset(Breaker.DRAWDOWN, now=T0 + DAY + 10)

    restarted = manager(SqliteRiskStore(db))
    assert allowed(restarted, T0 + DAY + 400, equity=9_650.0).allowed
    assert restarted.state.peak_equity == 9_650.0


def test_the_database_uses_wal(tmp_path):
    db = tmp_path / "trades.db"
    SqliteRiskStore(db)
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_the_store_only_creates_its_own_tables(tmp_path):
    """Phase 11 adds the trade schema to this same file, so nothing here may claim it."""
    db = tmp_path / "trades.db"
    SqliteRiskStore(db)
    with sqlite3.connect(db) as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    assert tables == {"risk_state", "kill_switches"}


def test_a_corrupt_state_row_refuses_rather_than_trading_blind(tmp_path):
    db = tmp_path / "trades.db"
    m = manager(SqliteRiskStore(db))
    allowed(m, T0)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE risk_state SET equity = 'banana' WHERE id = 1")

    reloaded = manager(SqliteRiskStore(db))
    decision = allowed(reloaded, T0 + 100)
    assert not decision.allowed
    assert decision.breaker is Breaker.UNKNOWN_STATE
    assert "could not be read" in decision.reason
    assert reloaded.tripped


def test_an_unreadable_database_fails_loudly_at_startup(tmp_path):
    """A kill-switch store that cannot be opened stops the process, not just the trade."""
    db = tmp_path / "trades.db"
    db.write_text("this is not a database", encoding="utf-8")
    with pytest.raises(sqlite3.DatabaseError):
        SqliteRiskStore(db)


def test_an_unrecognised_latch_row_still_blocks(tmp_path):
    """A latch written by a future phase must not be silently dropped."""
    db = tmp_path / "trades.db"
    SqliteRiskStore(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO kill_switches (breaker, tripped_at, reason, "
            "manual_reset_required) VALUES ('something_from_phase_9', ?, 'unknown', 1)",
            (T0,),
        )
    decision = allowed(manager(SqliteRiskStore(db)), T0 + 10)
    assert not decision.allowed
    assert decision.breaker is Breaker.UNKNOWN_STATE


def test_status_reports_the_numbers_a_dashboard_needs(tmp_path):
    m = manager(max_consecutive_losses=99)
    allowed(m, T0)
    m.record_trade(now=T0, pnl=-150.0, equity=9_850.0)
    status = m.status()
    assert status["equity"] == 9_850.0
    assert status["peak_equity"] == EQUITY
    assert status["drawdown"] == pytest.approx(0.015)
    assert status["daily_loss"] == pytest.approx(0.015)
    assert status["day"] == utc_day(T0)
    assert status["trades_today"] == 1
    assert status["risk_per_trade"] == 0.0025
    assert Breaker.DAILY_LOSS.value in status["tripped"]


# --- config wiring ---------------------------------------------------------

def test_params_from_shipped_config():
    p = RiskParams.from_config(load_config())
    assert p.per_trade == 0.0025
    assert p.max_daily_loss == 0.01
    assert p.max_drawdown == 0.03
    assert p.max_consecutive_losses == 3
    assert p.max_open_positions == 1
    assert p.cooldown_after_loss_seconds == 300
    assert p.cooldown_after_win_seconds == 60


@pytest.mark.parametrize("overrides", [
    {"per_trade": 0.0},
    {"per_trade": 1.5},
    {"max_daily_loss": 0.0},
    {"max_drawdown": 1.5},
    {"max_consecutive_losses": 0},
    {"max_open_positions": 0},
    {"cooldown_after_loss_seconds": -1},
    {"per_trade": 0.02, "max_daily_loss": 0.01},        # one stop-out trips the day
    {"max_daily_loss": 0.05, "max_drawdown": 0.03},     # the day outlives the account
])
def test_unsafe_params_are_rejected_at_construction(overrides):
    with pytest.raises(ValueError):
        RiskParams(**overrides)


class _StubConfig:
    def __init__(self, values):
        self._values = values

    def get(self, path, default=None):
        return self._values.get(path, default)


@pytest.mark.parametrize("forbidden", ["risk.martingale", "risk.averaging_down"])
def test_from_config_refuses_the_permanently_disabled_strategies(forbidden):
    with pytest.raises(ValueError, match="permanently disabled"):
        RiskParams.from_config(_StubConfig({forbidden: True}))


# --- no order path ---------------------------------------------------------

def test_the_manager_cannot_place_or_close_anything():
    """The risk manager answers yes or no. Acting on that answer is Phase 8's job."""
    forbidden = ("order", "close", "execute", "place", "submit", "cancel", "liquidat")
    names = [n for n in dir(RiskManager) if not n.startswith("_")]
    assert not [n for n in names if any(word in n.lower() for word in forbidden)]


def test_execution_has_not_started():
    """Phase 7 landed the liquidation guard; execution is still Phase 10.

    This is the moving boundary marker: it named Phase 7 while the guard was a stub, and
    now names the next unimplemented layer. Nothing in the repo may place an order.
    """
    importlib.import_module("risk.liquidation_guard")     # Phase 7: implemented
    for module in ("execution.order_manager", "execution.protection"):
        with pytest.raises(NotImplementedError):
            importlib.import_module(module)


def test_every_refusal_names_a_breaker_and_explains_itself():
    m = manager()
    refusals = [
        m.can_trade(now=T0, equity=None, open_positions=0),
        m.can_trade(now=None, equity=EQUITY, open_positions=0),
        m.can_trade(now=T0, equity=EQUITY, open_positions=5),
    ]
    for decision in refusals:
        assert isinstance(decision, RiskDecision)
        assert not decision.allowed
        assert decision.breaker is not None
        assert decision.reason
        assert "refused" in decision.summary()
        assert not decision            # falsy, so `if manager.can_trade(...)` is safe
