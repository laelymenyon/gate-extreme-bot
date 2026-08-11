"""PHASE 15 — the session calendar guard (`risk/session_guard.py`).

The configured universe is 31 symbols; only BTC/ETH/SOL/XRP never close. Everything else —
tokenized equities, indices, FX, metals — has a venue that shuts, and a stop cannot execute
on a closed venue: at 100x a gap jumps straight past a 0.125% stop, so the loss would be
bounded by liquidation instead of by risk. This module is the calendar that makes it
impossible to trade into a close.

Asserted here:

* 24/7 crypto is never blocked and never forced flat.
* Session-bound symbols are closed outside their windows (weekends included), and the
  windows are the DST-intersection hours — deliberately narrower than the venue's.
* Entries are blocked `no_entry_before_close_minutes` before a close and
  `no_entry_after_open_minutes` after an open; a position must be flat
  `flat_before_close_minutes` before a close.
* An unknown symbol is CLOSED, not open — the config demands fail-closed.
* Both loops honour the verdict: a blocked window prevents an order, and a position held
  into the flatten window is market-closed and its protective orders cancelled.

Offline throughout: the live path uses `tests/test_live.py`'s FakeClient and the autouse
network guard in conftest.py holds for this module too.
"""

from __future__ import annotations

import asyncio

import pytest

import config as config_module
from live.loop import LiveTrader
from paper.loop import PaperTrader
from risk.session_guard import SessionParams, session_verdict, trading_windows
from tests.test_live import BTC, TIERS, FakeClient, live_cfg, once
from tests.test_paper import candles as paper_candles
from tests.test_paper import quiet

# 2026-08-10 is a Monday. All timestamps are UTC.
SAT_1200 = 1_754_740_800.0                 # 2026-08-08 12:00 UTC (Saturday)
MON_1400 = 1_754_920_800.0                 # 2026-08-10 14:00 UTC
MON_1440 = MON_1400 + 2_400.0              # 14:40 — 10 min after the US open (14:30)
MON_1515 = MON_1400 + 4_500.0              # 15:15 — 45 min after the US open
MON_1915 = MON_1400 + 18_900.0             # 19:15 — 45 min before the US close (20:00)
MON_1930 = MON_1400 + 19_800.0             # 19:30 — 30 min before the US close
MON_2015 = MON_1400 + 22_500.0             # 20:15 — 45 min before the 21:00 metals close
MON_2030 = MON_1400 + 23_400.0             # 20:30 — 30 min before the 21:00 metals close

ENTRY = 65_000.0


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    """Same isolation `test_live.py` needs: LiveTrader persists kill-switches to SQLite."""
    real_load = config_module.load_config

    def scoped(*args, **kwargs):
        cfg = real_load(*args, **kwargs)
        cfg.raw["database"] = dict(cfg.raw["database"], path=str(tmp_path / "trades.db"))
        return cfg

    monkeypatch.setattr(config_module, "load_config", scoped)
    return tmp_path / "trades.db"


def params(**overrides):
    fields = dict(enabled=True, flat_before_close_minutes=30,
                  no_entry_before_close_minutes=60, no_entry_after_open_minutes=15,
                  treat_unknown_session_as_closed=True)
    fields.update(overrides)
    return SessionParams(**fields)


# --- the calendar itself ---------------------------------------------------

def test_the_24_7_crypto_set_is_never_blocked():
    # The midnight boundary is the trap: the window table represents 24/7 as
    # midnight-to-midnight, and a bug once read that 1440 boundary as a real close —
    # flattening BTC at 23:30 UTC and blocking entries until 00:15 UTC, every day.
    for symbol in ("BTC_USDT", "ETH_USDT", "SOL_USDT", "XRP_USDT"):
        for now in (SAT_1200, MON_1400, MON_1915,
                    MON_1400 + 35_400,   # 23:50 UTC Monday
                    MON_1400 + 36_600):  # 00:10 UTC Tuesday
            verdict = session_verdict(symbol, now, params())
            assert verdict.entry_allowed, f"{symbol} blocked at {now}"
            assert not verdict.must_flat, f"{symbol} forced flat at {now}"


def test_a_us_equity_is_closed_on_the_weekend():
    verdict = session_verdict("NAS100_USDT", SAT_1200, params())
    assert verdict.stage == "closed"
    assert not verdict.venue_open
    assert not verdict.entry_allowed


def test_a_us_equity_is_closed_before_its_open():
    """The window is the DST intersection (14:30-20:00 UTC), not the venue's full hours."""
    verdict = session_verdict("NAS100_USDT", MON_1400, params())
    assert verdict.stage == "closed"
    assert not verdict.entry_allowed


def test_a_us_equity_is_tradable_mid_session():
    verdict = session_verdict("NAS100_USDT", MON_1515, params())
    assert verdict.ok
    assert verdict.venue_open
    assert verdict.stage == "ok"


def test_entries_are_blocked_after_the_open():
    verdict = session_verdict("NAS100_USDT", MON_1440, params())
    assert verdict.stage == "after_open"
    assert verdict.venue_open
    assert not verdict.entry_allowed
    assert not verdict.must_flat


def test_entries_are_blocked_before_the_close():
    verdict = session_verdict("NAS100_USDT", MON_1915, params())
    assert verdict.stage == "before_close"
    assert verdict.venue_open
    assert not verdict.entry_allowed
    assert not verdict.must_flat


def test_the_flatten_window_opens_at_flat_before_close_minutes():
    """At exactly 30 min to close the position must be flat; entry is already blocked."""
    verdict = session_verdict("NAS100_USDT", MON_1930, params())
    assert verdict.must_flat
    assert not verdict.entry_allowed


def test_an_existing_position_is_not_flattenable_while_closed():
    """A closed venue cannot execute the flatten; the veto is entry, not a pretend close."""
    verdict = session_verdict("NAS100_USDT", SAT_1200, params())
    assert not verdict.must_flat


def test_uk_uses_its_own_session():
    # 08:00-15:30 UTC. Mid-morning is tradable; 15:45 is after the close.
    assert session_verdict("UK100_USDT", MON_1400 - 2 * 3600, params()).ok
    closed = session_verdict("UK100_USDT", MON_1400 + 6_300, params())  # 15:45 UTC
    assert closed.stage == "closed"


def test_hk_has_a_lunch_break():
    # Morning window 01:30-04:00, afternoon 05:00-08:00; the break is 04:00-05:00 UTC.
    verdict = session_verdict("HK50_USDT", MON_1400 - 34_200, params())  # 04:30 UTC
    assert verdict.stage == "closed"
    assert session_verdict("HK50_USDT", MON_1400 - 30_600, params()).ok  # 05:30 UTC


def test_japan_has_no_dst():
    assert session_verdict("JPN225_USDT", MON_1400 - 10 * 3600, params()).ok  # 04:00 UTC
    assert session_verdict("JPN225_USDT", MON_1400 - 7 * 3600, params()).stage == "closed"


def test_fx_closes_for_the_weekend():
    assert session_verdict("EURUSD_USDT", MON_1400 - 7200, params()).ok
    assert session_verdict("EURUSD_USDT", SAT_1200, params()).stage == "closed"


def test_gold_tokens_are_session_bound_like_metals():
    """config.yaml groups PAXG/XAUT under metals, so the guard does too."""
    assert session_verdict("PAXG_USDT", MON_1515, params()).ok
    assert session_verdict("PAXG_USDT", MON_2015, params()).stage == "before_close"
    assert session_verdict("PAXG_USDT", SAT_1200, params()).stage == "closed"
    assert session_verdict("XAUT_USDT", MON_2030, params()).must_flat


def test_an_unknown_symbol_fails_closed():
    verdict = session_verdict("MEME_USDT", MON_1515, params())
    assert verdict.stage == "unknown"
    assert not verdict.venue_open
    assert not verdict.entry_allowed


def test_treating_the_unknown_as_open_is_a_knob_the_config_forbids():
    """`session_guard.treat_unknown_session_as_closed=false` fails config validation."""
    verdict = session_verdict(
        "MEME_USDT", MON_1515,
        params(treat_unknown_session_as_closed=False),
    )
    assert verdict.entry_allowed


def test_disabling_the_guard_opens_everything():
    verdict = session_verdict("NAS100_USDT", SAT_1200, params(enabled=False))
    assert verdict.ok
    assert verdict.stage == "disabled"


def test_the_flatten_window_may_not_start_after_entries_unblock():
    with pytest.raises(ValueError, match="flat_before_close"):
        SessionParams(flat_before_close_minutes=90, no_entry_before_close_minutes=60)


def test_every_configured_symbol_has_a_schedule():
    """Adding a symbol to the universe without a calendar entry would fail this test."""
    cfg = config_module.load_config()
    for symbol in cfg.get("universe.symbols"):
        verdict = session_verdict(symbol, MON_1515, params())
        assert verdict.stage != "unknown", f"{symbol} has no session calendar"


def test_the_window_table_shape():
    assert len(trading_windows("BTC_USDT")) == 7          # every weekday
    windows = trading_windows("NAS100_USDT")
    assert windows and (windows[0].weekday, windows[0].start_minute,
                        windows[0].end_minute) == (0, 14 * 60 + 30, 20 * 60)
    assert len(trading_windows("HK50_USDT")) == 10        # two windows x five days
    assert trading_windows("MEME_USDT") is None


# --- the paper loop enforces it ---------------------------------------------

class ScriptedSource:
    """A MarketSource whose clock we can move: same protocol, no replay cursor math."""

    def __init__(self, now: float, mark: float = ENTRY):
        self._now = now
        self._mark = mark
        self._candles = {"1m": paper_candles(quiet(n=340))}

    def now(self) -> float:
        return self._now

    def candles(self, symbol: str):
        return self._candles

    def mark_price(self, symbol: str) -> float:
        return self._mark

    def advance(self) -> bool:
        return True


def paper_bot(monkeypatch, now: float, *, decide=None):
    cfg = config_module.load_config()
    source = ScriptedSource(now)
    return PaperTrader(cfg, source, "NAS100_USDT", TIERS, BTC,
                       decide=decide if decide is not None else (lambda **kw: None))


def test_paper_loop_refuses_an_entry_in_the_pre_close_window(monkeypatch):
    bot = paper_bot(monkeypatch, MON_1915, decide=once())
    asyncio.run(bot.step())
    assert bot.report.entries_attempted == 0
    assert bot.report.rejections.get("session:before_close") == 1
    assert not bot.gateway.calls                     # no order was even attempted


def test_paper_loop_flattens_a_position_before_the_close(monkeypatch):
    bot = paper_bot(monkeypatch, MON_1515)
    asyncio.run(bot.gateway.place_order(
        "NAS100_USDT", 10, price=None, tif="ioc", text="t-session-hold"))
    bot._position = {
        "direction": 1, "entry_price": ENTRY, "entry_time": MON_1515, "size": 10,
        "remaining": 10, "per_contract": 0.0001, "stop_price": ENTRY * 0.99675,
        "stop_order_id": "stp-s", "fees": 0.0, "realised": 0.0, "score": 88.0,
        "fills": [], "margin": 0.0, "liquidation_price": 0.0,
        "levels": {"stp-s": ("stop", ENTRY * 0.99675)},
    }

    bot.source._now = MON_1930
    asyncio.run(bot.step())

    assert bot._position is None
    assert bot.report.rejections.get("session:flattened") == 1
    assert bot.report.trades[-1].exit_reason == "session"
    assert asyncio.run(bot.orders.position_size("NAS100_USDT")) == 0


# --- the live loop enforces it ----------------------------------------------

def live_trader(monkeypatch, clock: dict, *, decide=None, store=None,
                client=None, symbol="NAS100_USDT"):
    cfg = live_cfg(monkeypatch)
    client = client or FakeClient(fills_entries=True)
    from paper.loop import RestMarketSource

    source = RestMarketSource(client, timeframes=("1m",), clock=lambda: clock["now"])
    asyncio.run(source.refresh(symbol))
    return LiveTrader(
        cfg, client, source, symbol, TIERS, BTC,
        starting_equity=10_000.0, store=store,
        decide=decide if decide is not None else (lambda **kw: None),
    ), client


def test_live_loop_refuses_an_entry_in_the_pre_close_window(monkeypatch):
    clock = {"now": MON_1915}
    bot, client = live_trader(monkeypatch, clock, decide=once())
    asyncio.run(bot.step())
    assert bot.report.entries_attempted == 0
    assert bot.report.rejections.get("session:before_close") == 1
    assert "place_order" not in client.calls


def test_live_loop_flattens_before_the_close_and_removes_protection(monkeypatch,
                                                                    isolated_database):
    """The full round trip: enter mid-session, then get flattened at the close window."""
    from database.models import TradeStore

    store = TradeStore(isolated_database)
    clock = {"now": MON_1515}
    bot, client = live_trader(monkeypatch, clock, decide=once(), store=store)

    asyncio.run(bot.step())
    assert bot.open_position is not None, "the entry should have filled mid-session"

    resting_before = asyncio.run(client.list_price_orders("NAS100_USDT"))
    assert resting_before, "a filled position must carry protective orders"

    clock["now"] = MON_1930
    asyncio.run(bot.step())

    assert bot.open_position is None
    assert bot.report.rejections.get("session:flattened") == 1
    assert bot.report.trades[-1].exit_reason == "session"
    assert not asyncio.run(client.list_price_orders("NAS100_USDT")), (
        "resting stop/TPs must not survive the flatten into a later session"
    )
    recorded = store.trades()
    assert recorded and recorded[-1].mode == "live"


def test_the_live_loop_keeps_attempting_the_flatten_after_the_venue_closes(monkeypatch,
                                                                           isolated_database):
    """If the flatten fails in the window, the loop must keep trying while closed.

    The verdict stops saying ``must_flat`` once the venue is closed — but a position that
    missed its flatten window is exactly the one that must not be given up on: it would
    ride the gap with a stop that cannot execute. The loop therefore treats an open
    position on a closed venue as still needing the flatten.
    """
    from database.models import TradeStore

    store = TradeStore(isolated_database)
    clock = {"now": MON_1515}
    bot, client = live_trader(monkeypatch, clock, decide=once(), store=store)

    asyncio.run(bot.step())
    assert bot.open_position is not None, "the entry should have filled mid-session"

    async def no_close(symbol, size, **kwargs):
        if kwargs.get("close"):
            raise RuntimeError("the venue is shutting")
        return await client.sim.place_order(symbol, size, **kwargs)

    client.place_order = no_close

    clock["now"] = MON_1930             # flatten window — the close fails
    asyncio.run(bot.step())
    assert bot.open_position is not None
    assert bot.report.rejections.get("session:close_unconfirmed") == 1

    clock["now"] = SAT_1200             # venue closed — the loop must still be trying
    asyncio.run(bot.step())
    assert bot.report.rejections.get("session:close_unconfirmed") == 2
    assert bot.open_position is not None
    assert asyncio.run(client.list_price_orders("NAS100_USDT")), (
        "the resting stop stays in force while the close is unconfirmed"
    )


def test_the_paper_loop_keeps_attempting_the_flatten_after_the_venue_closes(monkeypatch):
    """The rehearsal behaves like the live run: a missed flatten is retried, not dropped."""
    bot = paper_bot(monkeypatch, MON_1515)
    asyncio.run(bot.gateway.place_order(
        "NAS100_USDT", 10, price=None, tif="ioc", text="t-session-hold"))
    bot._position = {
        "direction": 1, "entry_price": ENTRY, "entry_time": MON_1515, "size": 10,
        "remaining": 10, "per_contract": 0.0001, "stop_price": ENTRY * 0.99675,
        "stop_order_id": "stp-s", "fees": 0.0, "realised": 0.0, "score": 88.0,
        "fills": [], "margin": 0.0, "liquidation_price": 0.0,
        "levels": {"stp-s": ("stop", ENTRY * 0.99675)},
        "protected": True,
    }
    real_place = bot.gateway.place_order

    async def no_close(symbol, size, **kwargs):
        if kwargs.get("close"):
            raise RuntimeError("the venue is shutting")
        return await real_place(symbol, size, **kwargs)

    bot.gateway.place_order = no_close

    bot.source._now = MON_1930
    asyncio.run(bot.step())
    assert bot.report.rejections.get("session:close_unconfirmed") == 1

    bot.source._now = SAT_1200
    asyncio.run(bot.step())
    assert bot.report.rejections.get("session:close_unconfirmed") == 2
    assert bot._position is not None
