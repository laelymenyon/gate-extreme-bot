"""PHASE 3 tests — WebSocket, staleness watchdog, REST reconciliation.

Payload fixtures are real frames captured from wss://fx-ws.gateio.ws/v4/ws/usdt on
2026-08-09, not invented shapes. Error codes are the ones the live endpoint returned.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from config import ConfigError, load_config
from exchange.gate_client import RiskTier
from exchange.websocket import (
    CH_BOOK_TICKER,
    CH_ORDERS,
    CH_POSITIONS,
    CH_TICKERS,
    BookTop,
    FeedNotHealthy,
    LiquidationTierUnavailable,
    MarketDataFeed,
    OrderEvent,
    PositionEvent,
    SubscriptionError,
    Ticker,
    build_request,
)

SYMBOL = "BTC_USDT"


# --- fixtures: real captured frames ---------------------------------------

TICKER_FRAME = {
    "time": 1754697600,
    "time_ms": 1754697600123,
    "channel": "futures.tickers",
    "event": "update",
    "result": [{
        "contract": "BTC_USDT",
        "last": "118432.1",
        "mark_price": "118430.5",
        "index_price": "118429.8",
        "funding_rate": "0.0000315",
        "volume_24h_quote": "1842991233",
        "change_percentage": "1.23",
    }],
}

BOOK_TICKER_FRAME = {
    "time": 1754697600,
    "time_ms": 1754697600456,
    "channel": "futures.book_ticker",
    "event": "update",
    "result": {
        "t": 1754697600456,
        "u": 90_000_001,
        "s": "BTC_USDT",
        "b": "118431.9",
        "B": 4213,
        "a": "118432.3",
        "A": 1877,
    },
}

ORDER_FRAME = {
    "time": 1754697601,
    "channel": "futures.orders",
    "event": "update",
    "result": [{
        "id": "778899001",
        "contract": "BTC_USDT",
        "size": 100,
        "left": 0,
        "status": "finished",
        "finish_as": "filled",
        "fill_price": "118432.0",
        "text": "t-phase3",
        "create_time": 1754697601,
        "update_time": 1754697601,
    }],
}

POSITION_FRAME = {
    "time": 1754697602,
    "channel": "futures.positions",
    "event": "update",
    "result": [{
        "contract": "BTC_USDT",
        "size": 100,
        "entry_price": "118432.0",
        "liq_price": "117300.0",
        "leverage": "100",
        "margin": "11.84",
        "unrealised_pnl": "0.12",
        "time_ms": 1754697602000,
        "update_id": 42,
    }],
}


class FakeRest:
    """REST double. Records calls so we can assert reconciliation actually happened."""

    def __init__(self, *, tiers=None, positions=None, open_orders=None, fail_times=0):
        self.calls: list[str] = []
        self.fail_times = fail_times
        # First three real BTC_USDT tiers as fetched in Phase 2: mmr climbs with notional.
        self._tiers = tiers or [
            RiskTier(tier=1, risk_limit=500_000.0, initial_rate=0.004,
                     maintenance_rate=0.003, leverage_max=125.0, deduction=0.0),
            RiskTier(tier=2, risk_limit=1_000_000.0, initial_rate=0.005,
                     maintenance_rate=0.004, leverage_max=100.0, deduction=500.0),
            RiskTier(tier=3, risk_limit=3_000_000.0, initial_rate=0.01,
                     maintenance_rate=0.005, leverage_max=50.0, deduction=1500.0),
        ]
        self._positions = positions if positions is not None else []
        self._open_orders = open_orders if open_orders is not None else []

    async def get_contract(self, symbol):
        self.calls.append(f"contract:{symbol}")
        if self.fail_times > 0:
            self.fail_times -= 1
            raise OSError("REST down")
        return {"name": symbol, "maintenance_rate": "0.003"}

    async def get_risk_tiers(self, symbol):
        self.calls.append(f"tiers:{symbol}")
        return self._tiers

    async def list_positions(self, holding=True):
        self.calls.append("positions")
        return self._positions

    async def list_open_orders(self, symbol):
        self.calls.append(f"open_orders:{symbol}")
        return self._open_orders

    async def list_price_orders(self, symbol):
        self.calls.append(f"price_orders:{symbol}")
        return []


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def cfg():
    return load_config("paper")


@pytest.fixture
def cfg_with_creds(monkeypatch):
    """Config whose credentials are present, so private reconciliation paths run."""
    import config as config_module
    monkeypatch.setattr(config_module, "ENV_PATH", config_module.ROOT / ".env.absent")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("GATE_API_KEY", "testkey")
    monkeypatch.setenv("GATE_API_SECRET", "testsecret")
    return load_config("paper")


@pytest.fixture
def instant_sleep(monkeypatch):
    """Make backoff sleeps instant without recursing into the patched function."""
    real_sleep = asyncio.sleep

    async def fake(_delay):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake)
    return fake


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def feed(cfg, clock):
    return MarketDataFeed(cfg, FakeRest(), [SYMBOL], clock=clock)


def make_healthy(feed, clock):
    """Bring a feed to healthy: connected, resynced, warmed up, fresh data."""
    feed._connected = True
    feed._connected_at = clock()
    feed._last_pong = clock()
    feed._resync_pending = False
    feed.handle_message(json.dumps(TICKER_FRAME))
    feed.handle_message(json.dumps(BOOK_TICKER_FRAME))
    clock.advance(11)          # clear warmup (10s)
    feed._last_pong = clock()  # keep heartbeat fresh
    feed._tickers[SYMBOL].received_at = clock()
    feed._books[SYMBOL].received_at = clock()
    return feed


# --- subscribe / connect frames -------------------------------------------

def test_public_subscribe_frame_shape():
    frame = build_request(CH_TICKERS, "subscribe", [SYMBOL], timestamp=1754697600)
    assert frame == {
        "time": 1754697600,
        "channel": "futures.tickers",
        "event": "subscribe",
        "payload": [SYMBOL],
    }
    assert "auth" not in frame


def test_private_subscribe_frame_is_signed():
    class Creds:
        present = True
        key = "testkey"
        secret = "testsecret"

    frame = build_request(CH_POSITIONS, "subscribe", ["12345", SYMBOL],
                          credentials=Creds(), timestamp=1754697600)
    # Signing payload is channel=..&event=..&time=.. — not the REST envelope.
    import hashlib
    import hmac
    expected = hmac.new(
        b"testsecret",
        b"channel=futures.positions&event=subscribe&time=1754697600",
        hashlib.sha512,
    ).hexdigest()
    assert frame["auth"] == {"method": "api_key", "KEY": "testkey", "SIGN": expected}


def test_private_channel_without_credentials_is_refused():
    with pytest.raises(ValueError, match="private channel"):
        build_request(CH_ORDERS, "subscribe", [SYMBOL])


def test_subscriptions_cover_all_four_required_channels(cfg, feed):
    channels = {f["channel"] for f in feed.subscriptions()}
    assert CH_TICKERS in channels
    assert CH_BOOK_TICKER in channels
    if cfg.credentials.present:
        assert CH_ORDERS in channels and CH_POSITIONS in channels
    else:
        # No keys in this environment: private channels are correctly omitted rather
        # than sent unsigned and rejected.
        assert CH_ORDERS not in channels


# --- parsing ---------------------------------------------------------------

def test_ticker_parsing(feed):
    assert feed.handle_message(json.dumps(TICKER_FRAME)) == CH_TICKERS
    t = feed._tickers[SYMBOL]
    assert isinstance(t, Ticker)
    assert t.last == pytest.approx(118432.1)
    assert t.mark_price == pytest.approx(118430.5)
    assert t.funding_rate == pytest.approx(0.0000315)


def test_book_ticker_parsing(feed):
    assert feed.handle_message(json.dumps(BOOK_TICKER_FRAME)) == CH_BOOK_TICKER
    b = feed._books[SYMBOL]
    assert isinstance(b, BookTop)
    assert (b.bid, b.ask) == (pytest.approx(118431.9), pytest.approx(118432.3))
    assert b.update_id == 90_000_001
    assert b.mid == pytest.approx((118431.9 + 118432.3) / 2)
    assert b.spread == pytest.approx(0.4 / b.mid)


def test_order_event_parsing(feed):
    assert feed.handle_message(json.dumps(ORDER_FRAME)) == CH_ORDERS
    o = feed._orders["778899001"]
    assert isinstance(o, OrderEvent)
    assert (o.status, o.finish_as, o.left) == ("finished", "filled", 0)
    assert o.fill_price == pytest.approx(118432.0)


def test_position_event_parsing(feed):
    assert feed.handle_message(json.dumps(POSITION_FRAME)) == CH_POSITIONS
    p = feed._positions[SYMBOL]
    assert isinstance(p, PositionEvent)
    assert p.size == 100 and p.side == "long"
    assert p.liq_price == pytest.approx(117300.0)
    assert p.leverage == pytest.approx(100.0)


def test_position_closed_is_removed_from_cache(feed):
    feed.handle_message(json.dumps(POSITION_FRAME))
    closed = json.loads(json.dumps(POSITION_FRAME))
    closed["result"][0].update(size=0, update_id=43)
    feed.handle_message(json.dumps(closed))
    assert SYMBOL not in feed._positions


def test_crossed_book_is_rejected_as_malformed(feed):
    bad = json.loads(json.dumps(BOOK_TICKER_FRAME))
    bad["result"].update(b="118440.0", a="118430.0")
    assert feed.handle_message(json.dumps(bad)) is None
    assert feed.stats.malformed == 1
    assert SYMBOL not in feed._books


# --- malformed / partial payloads -----------------------------------------

@pytest.mark.parametrize("payload,label", [
    ("not json at all", "garbage"),
    ("", "empty"),
    ('{"channel": "futures.tickers", "event": "update"', "truncated json"),
    ('[1,2,3]', "json but not an object"),
    ('{"channel":"futures.tickers","event":"update","result":null}', "null result"),
    ('{"channel":"futures.tickers","event":"update","result":[{"last":"1"}]}',
     "ticker missing contract"),
    ('{"channel":"futures.book_ticker","event":"update","result":{"s":"BTC_USDT"}}',
     "book missing prices"),
    ('{"channel":"futures.book_ticker","event":"update",'
     '"result":{"s":"BTC_USDT","b":"abc","a":"1"}}', "book non-numeric price"),
    ('{"channel":"futures.book_ticker","event":"update",'
     '"result":{"s":"BTC_USDT","b":"0","a":"0"}}', "book zero prices"),
    ('{"channel":"futures.positions","event":"update","result":[{"size":1}]}',
     "position missing contract"),
    ('{"channel":"futures.orders","event":"update","result":[{"contract":"BTC_USDT"}]}',
     "order missing id"),
])
def test_malformed_payloads_are_dropped_not_raised(feed, payload, label):
    assert feed.handle_message(payload) is None, label
    assert feed.stats.malformed == 1, label
    # Nothing was cached from a bad frame.
    assert not feed._books and not feed._positions and not feed._orders


def test_malformed_frame_does_not_disturb_good_state(feed, clock):
    make_healthy(feed, clock)
    good_bid = feed._books[SYMBOL].bid
    feed.handle_message("}{ broken")
    assert feed._books[SYMBOL].bid == good_bid
    assert feed.is_healthy(SYMBOL)


# --- duplicate events ------------------------------------------------------

def test_duplicate_book_update_is_ignored(feed):
    feed.handle_message(json.dumps(BOOK_TICKER_FRAME))
    feed.handle_message(json.dumps(BOOK_TICKER_FRAME))
    assert feed.stats.duplicates == 1
    assert feed._last_book_id[SYMBOL] == 90_000_001


def test_duplicate_position_event_is_ignored(feed):
    feed.handle_message(json.dumps(POSITION_FRAME))
    feed.handle_message(json.dumps(POSITION_FRAME))
    assert feed.stats.duplicates == 1
    assert feed._positions[SYMBOL].update_id == 42


def test_duplicate_order_event_is_ignored(feed):
    feed.handle_message(json.dumps(ORDER_FRAME))
    feed.handle_message(json.dumps(ORDER_FRAME))
    assert feed.stats.duplicates == 1
    assert len(feed._orders) == 1


def test_duplicate_ticker_frame_is_ignored(feed):
    feed.handle_message(json.dumps(TICKER_FRAME))
    feed.handle_message(json.dumps(TICKER_FRAME))
    assert feed.stats.duplicates == 1


# --- out-of-order events ---------------------------------------------------

def test_out_of_order_book_update_does_not_overwrite(feed):
    feed.handle_message(json.dumps(BOOK_TICKER_FRAME))
    stale = json.loads(json.dumps(BOOK_TICKER_FRAME))
    stale["result"].update(u=89_999_000, b="100000.0", a="100000.5")
    feed.handle_message(json.dumps(stale))
    assert feed.stats.out_of_order == 1
    assert feed._books[SYMBOL].bid == pytest.approx(118431.9)  # newer value kept


def test_out_of_order_position_event_does_not_overwrite(feed):
    feed.handle_message(json.dumps(POSITION_FRAME))
    stale = json.loads(json.dumps(POSITION_FRAME))
    stale["result"][0].update(update_id=41, size=999)
    feed.handle_message(json.dumps(stale))
    assert feed.stats.out_of_order == 1
    assert feed._positions[SYMBOL].size == 100


def test_out_of_order_position_close_cannot_resurrect_position(feed):
    """A late size=0 frame must not wipe a live position — that would hide real risk."""
    feed.handle_message(json.dumps(POSITION_FRAME))
    late_close = json.loads(json.dumps(POSITION_FRAME))
    late_close["result"][0].update(size=0, update_id=40)
    feed.handle_message(json.dumps(late_close))
    assert SYMBOL in feed._positions
    assert feed._positions[SYMBOL].size == 100


def test_out_of_order_ticker_is_dropped(feed):
    feed.handle_message(json.dumps(TICKER_FRAME))
    stale = json.loads(json.dumps(TICKER_FRAME))
    stale["time_ms"] = 1754697599000
    stale["result"][0]["last"] = "1.0"
    feed.handle_message(json.dumps(stale))
    assert feed.stats.out_of_order == 1
    assert feed._tickers[SYMBOL].last == pytest.approx(118432.1)


def test_out_of_order_order_event_is_dropped(feed):
    feed.handle_message(json.dumps(ORDER_FRAME))
    stale = json.loads(json.dumps(ORDER_FRAME))
    stale["result"][0].update(update_time=1754697000, status="open", left=100)
    feed.handle_message(json.dumps(stale))
    assert feed.stats.out_of_order == 1
    assert feed._orders["778899001"].status == "finished"


# --- staleness watchdog: fail-closed --------------------------------------

def test_fresh_feed_is_healthy(feed, clock):
    make_healthy(feed, clock)
    assert feed.is_healthy(SYMBOL)
    assert feed.book(SYMBOL).bid == pytest.approx(118431.9)
    assert feed.ticker(SYMBOL).last == pytest.approx(118432.1)


def test_book_goes_stale_after_threshold(feed, clock):
    make_healthy(feed, clock)
    clock.advance(6)  # book_seconds = 5
    assert feed.is_stale(SYMBOL, "book")
    assert not feed.is_healthy(SYMBOL)
    with pytest.raises(FeedNotHealthy, match="stale"):
        feed.book(SYMBOL)
    assert feed.stats.stale_rejections == 1


def test_ticker_goes_stale_after_threshold(feed, clock):
    make_healthy(feed, clock)
    clock.advance(11)  # ticker_seconds = 10
    assert feed.is_stale(SYMBOL, "ticker")
    with pytest.raises(FeedNotHealthy, match="stale"):
        feed.ticker(SYMBOL)


def test_never_received_data_is_stale_not_missing(feed, clock):
    """Fail-closed: absence of data must read as stale, never as 'fine'."""
    feed._connected = True
    feed._resync_pending = False
    feed._connected_at = clock() - 100
    assert feed.age_of(SYMBOL, "book") == float("inf")
    assert feed.is_stale(SYMBOL, "book")
    assert not feed.is_healthy(SYMBOL)
    with pytest.raises(FeedNotHealthy, match="no book data"):
        feed.book(SYMBOL)


def test_disconnected_feed_is_never_healthy(feed, clock):
    make_healthy(feed, clock)
    feed._connected = False
    assert not feed.is_healthy(SYMBOL)
    with pytest.raises(FeedNotHealthy, match="not connected"):
        feed.book(SYMBOL)


def test_unresynced_feed_is_never_healthy(feed, clock):
    """Even with perfectly fresh data, an un-reconciled feed must refuse to serve."""
    make_healthy(feed, clock)
    feed._resync_pending = True
    assert not feed.is_healthy(SYMBOL)
    with pytest.raises(FeedNotHealthy, match="resync"):
        feed.book(SYMBOL)
    with pytest.raises(FeedNotHealthy, match="resync"):
        feed.position(SYMBOL)


def test_warmup_suppresses_health_right_after_connect(feed, clock):
    feed._connected = True
    feed._resync_pending = False
    feed._connected_at = clock()
    feed._last_pong = clock()
    feed.handle_message(json.dumps(TICKER_FRAME))
    feed.handle_message(json.dumps(BOOK_TICKER_FRAME))
    assert not feed.is_healthy(SYMBOL), "warmup must suppress health"
    clock.advance(11)
    feed._last_pong = clock()
    feed._tickers[SYMBOL].received_at = clock()
    feed._books[SYMBOL].received_at = clock()
    assert feed.is_healthy(SYMBOL)


def test_health_report_exposes_reason(feed, clock):
    make_healthy(feed, clock)
    clock.advance(6)
    report = feed.health_report(SYMBOL)
    assert report["healthy"] is False
    assert report["connected"] is True
    assert report["symbols"][SYMBOL]["stale"] is True
    assert report["symbols"][SYMBOL]["book_age"] >= 6


def test_health_requires_every_symbol_fresh(cfg, clock):
    multi = MarketDataFeed(cfg, FakeRest(), [SYMBOL, "ETH_USDT"], clock=clock)
    make_healthy(multi, clock)
    # BTC is fresh, ETH never arrived -> the whole feed is unhealthy.
    assert multi.is_healthy(SYMBOL)
    assert not multi.is_healthy()


# --- accessors must never be looser than is_healthy() ---------------------

def _unhealthy_states(feed, clock):
    """Every way the feed can be unusable, as (label, setup) pairs."""
    def disconnected():
        feed._connected = False

    def resync_pending():
        feed._resync_pending = True

    def warming_up():
        feed._connected_at = clock()

    def pong_timeout():
        feed._last_pong = clock() - (feed._pong_timeout + 1)

    def stale_book():
        clock.advance(feed._book_max_age + 1)

    return [
        ("disconnected", disconnected),
        ("resync pending", resync_pending),
        ("warming up", warming_up),
        ("pong timeout", pong_timeout),
        ("stale book", stale_book),
    ]


@pytest.mark.parametrize("label", [
    "disconnected", "resync pending", "warming up", "pong timeout", "stale book",
])
def test_accessors_refuse_whenever_feed_is_unhealthy(cfg, clock, label):
    """Regression guard: book()/ticker() must not serve data is_healthy() rejects.

    Caught in Phase 3 live testing — during warmup is_healthy() was False while
    book() still returned data, so a caller that trusted the accessor alone could
    have produced a signal from a feed the watchdog considered unusable.
    """
    feed = MarketDataFeed(cfg, FakeRest(), [SYMBOL], clock=clock)
    make_healthy(feed, clock)
    assert feed.is_healthy(SYMBOL)

    dict(_unhealthy_states(feed, clock))[label]()

    assert not feed.is_healthy(SYMBOL), f"{label}: expected unhealthy"
    with pytest.raises(FeedNotHealthy):
        feed.book(SYMBOL)
    with pytest.raises(FeedNotHealthy):
        feed.ticker(SYMBOL)


def test_health_report_names_the_reason(cfg, clock):
    feed = MarketDataFeed(cfg, FakeRest(), [SYMBOL], clock=clock)
    feed._connected = True
    feed._resync_pending = False
    feed._connected_at = clock()
    feed._last_pong = clock()
    feed.handle_message(json.dumps(TICKER_FRAME))
    feed.handle_message(json.dumps(BOOK_TICKER_FRAME))
    report = feed.health_report(SYMBOL)
    assert report["healthy"] is False
    assert "warming up" in report["symbols"][SYMBOL]["reason"]


# --- heartbeat -------------------------------------------------------------

def test_pong_updates_heartbeat(feed, clock):
    feed._connected = True
    feed._last_pong = clock() - 100
    feed.handle_message(json.dumps({"channel": "futures.pong", "event": "", "result": None}))
    assert feed.stats.pongs == 1
    assert feed._last_pong == clock()


def test_missing_pong_makes_feed_unhealthy(feed, clock):
    make_healthy(feed, clock)
    feed._last_pong = clock() - 20  # pong_timeout = 15
    assert not feed.is_healthy(SYMBOL)


# --- subscription failure --------------------------------------------------

@pytest.mark.parametrize("code,message,terminal", [
    (1, "request payload does not follow json schema", True),
    (2, "Unknown channel futures.nope", True),
    (4, "INVALID_KEY: Invalid key provided", True),
    (99, "temporary server problem", False),
])
def test_subscription_error_raised_and_classified(feed, code, message, terminal):
    frame = json.dumps({
        "channel": "futures.orders", "event": "subscribe",
        "error": {"code": code, "message": message},
    })
    with pytest.raises(SubscriptionError) as exc:
        feed.handle_message(frame)
    assert exc.value.code == code
    assert exc.value.terminal is terminal
    assert feed.stats.subscription_failures == 1


def test_successful_subscribe_is_recorded(feed):
    frame = json.dumps({
        "channel": "futures.tickers", "event": "subscribe",
        "payload": [SYMBOL], "result": {"status": "success"},
    })
    assert feed.handle_message(frame) == CH_TICKERS
    assert CH_TICKERS in feed._subscribed


# --- liquidation tier boundary --------------------------------------------

def test_market_data_layer_refuses_to_supply_maintenance_rate(feed):
    """The WS layer must not become a backdoor to a flat maintenance rate."""
    with pytest.raises(LiquidationTierUnavailable, match="select_tier"):
        feed.maintenance_rate(SYMBOL)
    assert feed.stats.tier_bypass_attempts == 1


def test_feed_exposes_no_maintenance_rate_attribute(feed, clock):
    make_healthy(feed, clock)
    ticker = feed.ticker(SYMBOL)
    book = feed.book(SYMBOL)
    for obj in (ticker, book):
        assert not hasattr(obj, "maintenance_rate")
        assert not hasattr(obj, "mmr")


async def test_select_tier_resolves_from_notional(feed):
    small = await feed.select_tier(SYMBOL, 100_000)
    medium = await feed.select_tier(SYMBOL, 900_000)
    large = await feed.select_tier(SYMBOL, 2_500_000)
    assert small.maintenance_rate == 0.003
    assert medium.maintenance_rate == 0.004
    assert large.maintenance_rate == 0.005
    assert small.maintenance_rate < large.maintenance_rate


async def test_select_tier_goes_through_rest(cfg, clock):
    rest = FakeRest()
    f = MarketDataFeed(cfg, rest, [SYMBOL], clock=clock)
    await f.select_tier(SYMBOL, 100_000)
    assert f"tiers:{SYMBOL}" in rest.calls


async def test_select_tier_rejects_negative_notional(feed):
    with pytest.raises(ValueError, match="notional"):
        await feed.select_tier(SYMBOL, -1)


async def test_tier_selection_is_not_cached_across_notionals(feed):
    """A larger position must not reuse a smaller position's tier."""
    first = await feed.select_tier(SYMBOL, 100_000)
    second = await feed.select_tier(SYMBOL, 2_500_000)
    assert first.tier != second.tier


# --- disconnect: state invalidation ---------------------------------------

def test_disconnect_clears_all_cached_state(feed, clock):
    make_healthy(feed, clock)
    feed.handle_message(json.dumps(POSITION_FRAME))
    feed.handle_message(json.dumps(ORDER_FRAME))
    assert feed._books and feed._positions and feed._orders

    feed._invalidate("connection lost")

    assert not feed._books, "book cache survived a disconnect"
    assert not feed._tickers
    assert not feed._positions, "position cache survived a disconnect"
    assert not feed._orders
    assert not feed._subscribed
    assert feed._resync_pending is True
    assert feed.connected is False


def test_disconnect_clears_dedup_state_so_new_ids_are_accepted(feed, clock):
    """Post-reconnect the exchange may restart update ids; stale guards must not block."""
    make_healthy(feed, clock)
    feed.handle_message(json.dumps(POSITION_FRAME))
    feed._invalidate("connection lost")
    assert not feed._last_book_id and not feed._last_position_id
    assert not feed._last_ticker_ms and not feed._seen_order_events

    feed._connected = True
    feed._resync_pending = False
    lower_id = json.loads(json.dumps(BOOK_TICKER_FRAME))
    lower_id["result"]["u"] = 1
    feed.handle_message(json.dumps(lower_id))
    assert feed._books[SYMBOL].update_id == 1
    assert feed.stats.out_of_order == 0


def test_no_signal_data_available_between_disconnect_and_resync(feed, clock):
    """The whole point: nothing readable until REST has reconciled."""
    make_healthy(feed, clock)
    feed._invalidate("connection lost")

    # Reconnected, fresh frames flowing — but resync has not run.
    feed._connected = True
    feed._connected_at = clock() - 100
    feed._last_pong = clock()
    feed.handle_message(json.dumps(TICKER_FRAME))
    feed.handle_message(json.dumps(BOOK_TICKER_FRAME))

    assert not feed.is_healthy(SYMBOL)
    for accessor in (feed.book, feed.ticker, feed.position):
        with pytest.raises(FeedNotHealthy):
            accessor(SYMBOL)


# --- reconnect: backoff + jitter ------------------------------------------

def test_backoff_grows_exponentially_and_is_capped(feed):
    base = feed._base_delay
    max_delay = feed._max_delay
    for attempt in range(12):
        delay = feed.reconnect_delay(attempt)
        expected = min(base * (2 ** attempt), max_delay)
        assert expected <= delay <= expected * (1 + feed._jitter) + 1e-9
        assert delay <= max_delay * (1 + feed._jitter)


def test_backoff_has_jitter(feed):
    delays = {feed.reconnect_delay(4) for _ in range(40)}
    assert len(delays) > 1, "identical delays would synchronise reconnect storms"


def test_reconnect_storm_is_detected(feed, clock):
    for _ in range(feed._max_attempts_window):
        feed._record_reconnect()
    assert not feed.reconnect_storm()
    feed._record_reconnect()
    assert feed.reconnect_storm()


def test_reconnect_attempts_age_out_of_the_window(feed, clock):
    for _ in range(feed._max_attempts_window + 1):
        feed._record_reconnect()
    assert feed.reconnect_storm()
    clock.advance(feed._window_seconds + 1)
    assert not feed.reconnect_storm(), "old attempts must not count forever"


# --- REST resync ----------------------------------------------------------

async def test_resync_clears_pending_and_reconciles_market_state(cfg, clock):
    rest = FakeRest()
    f = MarketDataFeed(cfg, rest, [SYMBOL], clock=clock)
    assert f.resync_pending is True

    summary = await f.resync()

    assert f.resync_pending is False
    assert summary["contracts"] == 1
    assert f"contract:{SYMBOL}" in rest.calls
    assert f"tiers:{SYMBOL}" in rest.calls, "risk tiers must be reconciled, not assumed"
    assert f.stats.resyncs == 1


async def test_resync_retries_then_succeeds(cfg, clock, instant_sleep):
    rest = FakeRest(fail_times=2)
    f = MarketDataFeed(cfg, rest, [SYMBOL], clock=clock)
    await f.resync()
    assert f.resync_pending is False
    assert f.stats.resync_failures == 2


async def test_resync_failure_leaves_feed_fail_closed(cfg, clock, instant_sleep):
    rest = FakeRest(fail_times=99)
    f = MarketDataFeed(cfg, rest, [SYMBOL], clock=clock)
    f._connected = True

    with pytest.raises(FeedNotHealthy, match="resync failed"):
        await f.resync()

    assert f.resync_pending is True
    assert not f.is_healthy(SYMBOL)


async def test_resync_replaces_position_cache_from_rest(cfg_with_creds, clock):
    """REST is authoritative: a position the WS never mentioned must appear."""
    cfg = cfg_with_creds
    rest = FakeRest(positions=[{
        "contract": SYMBOL, "size": -50, "entry_price": "118000.0",
        "liq_price": "119100.0", "leverage": "100", "margin": "5.9",
        "unrealised_pnl": "-0.4", "time_ms": 1754697700000, "update_id": 77,
    }])
    f = MarketDataFeed(cfg, rest, [SYMBOL], clock=clock)

    # A stale WS view claiming long 100.
    f.handle_message(json.dumps(POSITION_FRAME))
    assert f._positions[SYMBOL].size == 100

    await f.resync()

    assert "positions" in rest.calls
    assert f._positions[SYMBOL].size == -50, "REST state must overwrite WS state"
    assert f._positions[SYMBOL].side == "short"
    assert f._last_position_id[SYMBOL] == 77


async def test_resync_reconciles_open_and_price_orders(cfg_with_creds, clock):
    cfg = cfg_with_creds
    rest = FakeRest(open_orders=[{
        "id": "999", "contract": SYMBOL, "size": 10, "left": 10,
        "status": "open", "create_time": 1754697700, "update_time": 1754697700,
    }])
    f = MarketDataFeed(cfg, rest, [SYMBOL], clock=clock)

    # A phantom order the WS thought was live.
    f.handle_message(json.dumps(ORDER_FRAME))
    summary = await f.resync()

    assert "778899001" not in f._orders, "phantom WS order survived reconciliation"
    assert "999" in f._orders
    assert summary["open_orders"] == 1
    assert f"price_orders:{SYMBOL}" in rest.calls, "protective orders must be reconciled"


# --- integration: the run() loop ------------------------------------------

class FakeWS:
    """Scriptable WebSocket. Yields frames, then raises to simulate a drop."""

    def __init__(self, frames, *, raise_at_end=None, hold_seconds=0.0):
        self.sent: list[dict] = []
        self._frames = list(frames)
        self._raise_at_end = raise_at_end
        self._hold = hold_seconds
        self.closed = False

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def close(self):
        self.closed = True

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for frame in self._frames:
            yield frame
        if self._hold:
            # Keep the socket "open" so heartbeats have time to fire.
            await asyncio.sleep(self._hold)
        if self._raise_at_end is not None:
            raise self._raise_at_end

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def connector(*sockets):
    """Return a connect factory handing out each socket in turn."""
    queue = list(sockets)

    def factory(url, **kwargs):
        if not queue:
            raise ConnectionError("no more sockets")
        return queue.pop(0)

    return factory


async def run_briefly(feed, seconds=0.05):
    """Run feed.run() for a moment, then stop it and settle, ignoring shutdown noise."""
    task = asyncio.create_task(feed.run())
    await asyncio.sleep(seconds)
    await feed.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_connect_subscribe_and_resync_marks_healthy(cfg, clock):
    ws = FakeWS([json.dumps(TICKER_FRAME), json.dumps(BOOK_TICKER_FRAME)],
                raise_at_end=ConnectionError("closed"))
    rest = FakeRest()
    f = MarketDataFeed(cfg, rest, [SYMBOL], connect_factory=connector(ws), clock=clock)

    await run_briefly(f)

    subscribed = {frame["channel"] for frame in ws.sent}
    assert CH_TICKERS in subscribed
    assert CH_BOOK_TICKER in subscribed
    assert f.stats.connects == 1
    assert f.stats.resyncs >= 1, "resync must run on connect before data is trusted"
    assert f"contract:{SYMBOL}" in rest.calls


async def test_reconnect_runs_a_fresh_resync(cfg, clock, monkeypatch):
    first = FakeWS([json.dumps(BOOK_TICKER_FRAME)], raise_at_end=ConnectionError("drop"))
    second = FakeWS([json.dumps(BOOK_TICKER_FRAME)], raise_at_end=ConnectionError("drop"))
    rest = FakeRest()
    f = MarketDataFeed(cfg, rest, [SYMBOL],
                       connect_factory=connector(first, second), clock=clock)
    # Skip the real backoff wait without patching asyncio.sleep globally.
    monkeypatch.setattr(f, "reconnect_delay", lambda attempt: 0.0)

    await run_briefly(f, 0.1)

    assert f.stats.connects == 2, "did not reconnect after the drop"
    assert f.stats.disconnects >= 1
    assert f.stats.resyncs == 2, "each reconnect must trigger its own REST resync"
    assert f.stats.reconnect_attempts >= 1


async def test_heartbeat_ping_is_sent(cfg, clock, monkeypatch):
    monkeypatch.setitem(cfg.section("websocket"), "ping_interval_seconds", 0.01)
    ws = FakeWS([], raise_at_end=ConnectionError("drop"), hold_seconds=0.12)

    f = MarketDataFeed(cfg, FakeRest(), [SYMBOL],
                       connect_factory=connector(ws), clock=clock)
    await run_briefly(f, 0.09)

    pings = [frame for frame in ws.sent if frame.get("channel") == "futures.ping"]
    assert pings, "no heartbeat ping was sent"


async def test_terminal_subscription_failure_stops_the_loop(cfg, clock):
    """An INVALID_KEY subscribe error must not be retried forever."""
    bad = json.dumps({
        "channel": "futures.orders", "event": "subscribe",
        "error": {"code": 4, "message": "INVALID_KEY: Invalid key provided"},
    })
    ws = FakeWS([bad])
    f = MarketDataFeed(cfg, FakeRest(), [SYMBOL],
                       connect_factory=connector(ws), clock=clock)

    with pytest.raises(SubscriptionError) as exc:
        await f.run()
    assert exc.value.code == 4
    assert f.resync_pending is True, "must fail closed after a terminal subscription error"


async def test_run_stops_cleanly(cfg, clock):
    ws = FakeWS([], raise_at_end=ConnectionError("drop"), hold_seconds=0.05)
    f = MarketDataFeed(cfg, FakeRest(), [SYMBOL],
                       connect_factory=connector(ws), clock=clock)
    await run_briefly(f, 0.02)
    assert f.connected is False
    assert f.resync_pending is True


# --- config validation guards ---------------------------------------------

def _mutate_ws(monkeypatch, tmp_path, **overrides):
    import copy
    import config as config_module
    import yaml as _yaml

    raw = _yaml.safe_load(config_module.CONFIG_PATH.read_text(encoding="utf-8"))
    for dotted, value in overrides.items():
        node = raw
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = value
    path = tmp_path / "config.yaml"
    path.write_text(_yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_PATH", path)
    return load_config("paper")


def test_rest_resync_cannot_be_disabled(monkeypatch, tmp_path):
    with pytest.raises(ConfigError, match="rebuilt from REST"):
        _mutate_ws(monkeypatch, tmp_path,
                   **{"websocket.resync.require_rest_resync": False})


def test_ws_url_must_be_wss(monkeypatch, tmp_path):
    with pytest.raises(ConfigError, match="wss://"):
        _mutate_ws(monkeypatch, tmp_path, **{"websocket.url": "ws://insecure"})


@pytest.mark.parametrize("path", [
    "websocket.staleness.ticker_seconds",
    "websocket.staleness.book_seconds",
])
def test_staleness_thresholds_must_be_positive(monkeypatch, tmp_path, path):
    with pytest.raises(ConfigError, match="positive"):
        _mutate_ws(monkeypatch, tmp_path, **{path: 0})


def test_pong_timeout_must_exceed_ping_interval(monkeypatch, tmp_path):
    with pytest.raises(ConfigError, match="pong_timeout"):
        _mutate_ws(monkeypatch, tmp_path,
                   **{"websocket.pong_timeout_seconds": 5,
                      "websocket.ping_interval_seconds": 5})


def test_phase1_and_phase2_decisions_are_untouched():
    """Phase 3 must not have relaxed anything already decided."""
    c = load_config("paper")
    assert c.get("leverage.default") == 100
    assert c.get("leverage.minimum") == 100
    assert c.get("leverage.margin_mode") == "isolated"
    assert c.get("leverage.allow_margin_topup") is False
    assert c.get("protection.liquidation_buffer") == 0.003
    assert c.get("protection.emergency_close_on_sl_failure") is True
    assert c.get("protection.verify_liq_price") is True
    assert c.get("risk.max_daily_loss") == 0.01
    assert c.get("risk.max_drawdown") == 0.03
    assert c.get("risk.max_consecutive_losses") == 3
    assert c.get("risk.max_open_positions") == 1
    assert c.get("take_profit.entry_tif") == "poc"
    assert c.dry_run is True





