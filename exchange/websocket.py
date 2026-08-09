"""Gate.io futures WebSocket client — wss://fx-ws.gateio.ws/v4/ws/usdt

PHASE 3.

Protocol verified against the official `gateio/gatews` SDK and confirmed by connecting
to the live endpoint on 2026-08-09:

* Request frame::

      {"time": <unix>, "channel": "futures.tickers", "event": "subscribe",
       "payload": ["BTC_USDT"]}

* Private channels add::

      "auth": {"method": "api_key", "KEY": <key>,
               "SIGN": HMAC_SHA512(secret, "channel=%s&event=%s&time=%d")}

  Note this is a *different* signing scheme from REST — the payload is the
  ``channel=...&event=...&time=...`` string, not the five-line REST envelope.

* Heartbeat: send ``{"time": t, "channel": "futures.ping"}``; server replies on
  ``futures.pong``.

* Subscription errors observed live::

      code 1 -> "request payload does not follow json schema"
      code 2 -> "Unknown channel ..."
      code 4 -> "INVALID_KEY: Invalid key provided"

Design rules this module enforces:

* **WebSocket is never source of truth across a disconnect.** After any reconnect the
  cached view is dropped and rebuilt from REST before the feed is marked healthy again.
* **Fail-closed staleness.** ``is_healthy()`` is False whenever data is missing, stale,
  a resync is pending, or the socket is down. Signal generation must gate on it.
* **The market-data layer cannot answer liquidation questions.** It refuses to expose a
  maintenance rate; callers are routed to :meth:`MarketDataFeed.select_tier`, which
  resolves the tier from actual notional via REST. See ``docs/ARCHITECTURE.md`` §4.

Nothing here can place an order: this module never issues a write, and the REST client
it calls for resync blocks writes independently.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Mapping

import websockets

from exchange.gate_client import GateAPIError, GateFuturesClient, RiskTier, select_tier

log = logging.getLogger(__name__)

# Public channels.
CH_TICKERS = "futures.tickers"
CH_BOOK_TICKER = "futures.book_ticker"
CH_ORDER_BOOK_UPDATE = "futures.order_book_update"
# Private channels (require auth).
CH_ORDERS = "futures.orders"
CH_POSITIONS = "futures.positions"

CH_PING = "futures.ping"
CH_PONG = "futures.pong"

PRIVATE_CHANNELS = frozenset({CH_ORDERS, CH_POSITIONS})

# Subscription error codes seen on the live endpoint.
ERR_BAD_SCHEMA = 1
ERR_UNKNOWN_CHANNEL = 2
ERR_INVALID_KEY = 4
# Retrying these cannot help — the request or the credentials are wrong.
_TERMINAL_SUB_ERRORS = frozenset({ERR_BAD_SCHEMA, ERR_UNKNOWN_CHANNEL, ERR_INVALID_KEY})


class SubscriptionError(Exception):
    """The exchange rejected a subscription."""

    def __init__(self, channel: str, code: int, message: str) -> None:
        super().__init__(f"{channel}: [{code}] {message}")
        self.channel = channel
        self.code = code
        self.message = message

    @property
    def terminal(self) -> bool:
        return self.code in _TERMINAL_SUB_ERRORS


class FeedNotHealthy(Exception):
    """Market data was requested while the feed was stale, down, or unreconciled."""


class LiquidationTierUnavailable(Exception):
    """A maintenance rate was requested from the market-data layer.

    The WebSocket carries no risk-tier information, and the contract-level
    ``maintenance_rate`` is only tier 1 — using it would understate liquidation risk as
    notional grows (BTC_USDT has 19 tiers; mmr climbs 0.30% -> 0.50%+). Callers must go
    through :meth:`MarketDataFeed.select_tier`, which resolves the tier from REST.
    """


@dataclass
class Ticker:
    symbol: str
    last: float
    mark_price: float
    index_price: float
    funding_rate: float
    volume_24h_quote: float
    received_at: float

    @classmethod
    def from_event(cls, raw: Mapping[str, Any], received_at: float) -> "Ticker":
        return cls(
            symbol=raw["contract"],
            last=float(raw["last"]),
            mark_price=float(raw.get("mark_price") or raw["last"]),
            index_price=float(raw.get("index_price") or 0) or float(raw["last"]),
            funding_rate=float(raw.get("funding_rate") or 0),
            volume_24h_quote=float(raw.get("volume_24h_quote") or 0),
            received_at=received_at,
        )


@dataclass
class BookTop:
    """Top of book from futures.book_ticker.

    `u` is the order-book update id and is monotonically increasing per contract; it is
    what lets us drop duplicate and out-of-order frames.
    """

    symbol: str
    bid: float
    bid_size: int
    ask: float
    ask_size: int
    update_id: int
    exchange_ts_ms: int
    received_at: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        """Spread as a fraction of mid."""
        mid = self.mid
        return (self.ask - self.bid) / mid if mid > 0 else float("inf")

    @classmethod
    def from_event(cls, raw: Mapping[str, Any], received_at: float) -> "BookTop":
        bid, ask = float(raw["b"]), float(raw["a"])
        if bid <= 0 or ask <= 0:
            raise ValueError(f"non-positive book prices: bid={bid} ask={ask}")
        if ask < bid:
            raise ValueError(f"crossed book: bid={bid} > ask={ask}")
        return cls(
            symbol=raw["s"],
            bid=bid,
            bid_size=int(raw.get("B") or 0),
            ask=ask,
            ask_size=int(raw.get("A") or 0),
            update_id=int(raw.get("u") or 0),
            exchange_ts_ms=int(raw.get("t") or 0),
            received_at=received_at,
        )


@dataclass
class OrderEvent:
    order_id: str
    symbol: str
    size: int
    left: int
    status: str
    finish_as: str
    fill_price: float
    text: str
    exchange_ts_ms: int

    @classmethod
    def from_event(cls, raw: Mapping[str, Any]) -> "OrderEvent":
        return cls(
            order_id=str(raw["id"]),
            symbol=raw["contract"],
            size=int(raw.get("size") or 0),
            left=int(raw.get("left") or 0),
            status=str(raw.get("status") or ""),
            finish_as=str(raw.get("finish_as") or ""),
            fill_price=float(raw.get("fill_price") or 0),
            text=str(raw.get("text") or ""),
            exchange_ts_ms=int(float(raw.get("update_time") or raw.get("create_time") or 0) * 1000),
        )


@dataclass
class PositionEvent:
    symbol: str
    size: int
    entry_price: float
    liq_price: float
    leverage: float
    margin: float
    unrealised_pnl: float
    exchange_ts_ms: int
    update_id: int

    @property
    def side(self) -> str:
        if self.size > 0:
            return "long"
        return "short" if self.size < 0 else "flat"

    @classmethod
    def from_event(cls, raw: Mapping[str, Any]) -> "PositionEvent":
        return cls(
            symbol=raw["contract"],
            size=int(raw.get("size") or 0),
            entry_price=float(raw.get("entry_price") or 0),
            liq_price=float(raw.get("liq_price") or 0),
            leverage=float(raw.get("leverage") or 0),
            margin=float(raw.get("margin") or 0),
            unrealised_pnl=float(raw.get("unrealised_pnl") or 0),
            exchange_ts_ms=int(raw.get("time_ms") or (int(raw.get("time") or 0) * 1000)),
            update_id=int(raw.get("update_id") or 0),
        )


@dataclass
class FeedStats:
    connects: int = 0
    disconnects: int = 0
    reconnect_attempts: int = 0
    messages: int = 0
    malformed: int = 0
    duplicates: int = 0
    out_of_order: int = 0
    stale_rejections: int = 0
    subscription_failures: int = 0
    pongs: int = 0
    heartbeat_timeouts: int = 0
    resyncs: int = 0
    resync_failures: int = 0
    tier_bypass_attempts: int = 0


def build_request(channel: str, event: str, payload: list[Any], *,
                  credentials=None, timestamp: int | None = None) -> dict[str, Any]:
    """Build a subscribe/unsubscribe frame, signing it when the channel is private.

    The WS signing payload is ``channel=<c>&event=<e>&time=<t>`` — deliberately
    different from the REST envelope. Verified against the official SDK.
    """
    ts = int(time.time()) if timestamp is None else int(timestamp)
    request: dict[str, Any] = {
        "time": ts, "channel": channel, "event": event, "payload": payload,
    }
    if channel in PRIVATE_CHANNELS:
        if credentials is None or not credentials.present:
            raise ValueError(f"{channel} is a private channel and requires API credentials")
        message = f"channel={channel}&event={event}&time={ts}"
        request["auth"] = {
            "method": "api_key",
            "KEY": credentials.key,
            "SIGN": hmac.new(
                credentials.secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha512
            ).hexdigest(),
        }
    return request


class MarketDataFeed:
    """Realtime market and account state, with a fail-closed staleness watchdog.

    The feed owns three guarantees:

    1. Data served by :meth:`ticker` / :meth:`book` is fresh, or the call raises.
    2. After a disconnect the cache is dropped and rebuilt from REST before the feed
       reports healthy again.
    3. Liquidation-tier questions are refused and routed to REST.
    """

    def __init__(
        self,
        config,
        rest: GateFuturesClient,
        symbols: Iterable[str],
        *,
        connect_factory: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cfg = config
        self._rest = rest
        self._symbols = list(symbols)
        self._clock = clock
        self._connect_factory = connect_factory or websockets.connect

        ws = config.section("websocket")
        self._url = ws["url"]
        self._connect_timeout = float(ws.get("connect_timeout_seconds", 15))
        self._ping_interval = float(ws.get("ping_interval_seconds", 5))
        self._pong_timeout = float(ws.get("pong_timeout_seconds", 15))

        stale = ws["staleness"]
        self._ticker_max_age = float(stale["ticker_seconds"])
        self._book_max_age = float(stale["book_seconds"])
        self._warmup = float(stale.get("warmup_seconds", 10))

        rc = ws["reconnect"]
        self._base_delay = float(rc["base_delay_seconds"])
        self._max_delay = float(rc["max_delay_seconds"])
        self._jitter = float(rc.get("jitter_ratio", 0.25))
        self._max_attempts_window = int(rc.get("max_attempts_per_window", 10))
        self._window_seconds = float(rc.get("window_seconds", 300))

        self._resync_cfg = ws["resync"]
        self._resync_attempts = int(self._resync_cfg.get("max_attempts", 3))

        # Cached state. Cleared on every disconnect — never trusted across one.
        self._tickers: dict[str, Ticker] = {}
        self._books: dict[str, BookTop] = {}
        self._positions: dict[str, PositionEvent] = {}
        self._orders: dict[str, OrderEvent] = {}

        # Duplicate / ordering guards, keyed per symbol or order id.
        self._last_book_id: dict[str, int] = {}
        self._last_ticker_ms: dict[str, int] = {}
        self._last_position_id: dict[str, int] = {}
        self._seen_order_events: dict[str, int] = {}

        self._connected = False
        self._resync_pending = True     # nothing is trusted until the first resync lands
        self._connected_at: float | None = None
        self._last_pong: float | None = None
        self._reconnect_times: list[float] = []
        self._subscribed: set[str] = set()

        self.stats = FeedStats()
        self._ws: Any = None
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()

    # --- health ------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def resync_pending(self) -> bool:
        return self._resync_pending

    def _in_warmup(self) -> bool:
        if self._connected_at is None:
            return False
        return (self._clock() - self._connected_at) < self._warmup

    def age_of(self, symbol: str, kind: str = "book") -> float:
        """Seconds since the last update, or +inf when nothing has arrived."""
        cache = self._books if kind == "book" else self._tickers
        entry = cache.get(symbol)
        if entry is None:
            return float("inf")
        return self._clock() - entry.received_at

    def is_stale(self, symbol: str, kind: str = "book") -> bool:
        limit = self._book_max_age if kind == "book" else self._ticker_max_age
        return self.age_of(symbol, kind) > limit

    def _unhealthy_reason(self, symbol: str, kind: str | None = None) -> str | None:
        """Single definition of 'healthy'. Returns None when the feed is usable.

        Both :meth:`is_healthy` and the guarded accessors go through here so there is
        exactly one predicate — an accessor must never be more permissive than the
        health check a caller consulted.
        """
        if not self._connected:
            return "websocket is not connected"
        if self._resync_pending:
            return "REST resync still pending; WebSocket state is not trusted"
        if self._last_pong is not None and (
            self._clock() - self._last_pong > self._pong_timeout
        ):
            return (f"no pong for {self._clock() - self._last_pong:.1f}s "
                    f"(limit {self._pong_timeout:.1f}s); link presumed dead")
        if self._in_warmup():
            elapsed = self._clock() - (self._connected_at or 0)
            return f"feed is warming up ({elapsed:.1f}s of {self._warmup:.1f}s)"

        for k in ((kind,) if kind else ("book", "ticker")):
            cache = self._books if k == "book" else self._tickers
            limit = self._book_max_age if k == "book" else self._ticker_max_age
            if symbol not in cache:
                return f"no {k} data received yet"
            if self.is_stale(symbol, k):
                return (f"{k} data is stale ({self.age_of(symbol, k):.1f}s old, "
                        f"limit {limit:.1f}s)")
        return None

    def is_healthy(self, symbol: str | None = None) -> bool:
        """Fail-closed. False whenever anything about the feed is uncertain."""
        targets = [symbol] if symbol else self._symbols
        return all(self._unhealthy_reason(sym) is None for sym in targets)

    def health_report(self, symbol: str | None = None) -> dict[str, Any]:
        targets = [symbol] if symbol else self._symbols
        return {
            "connected": self._connected,
            "resync_pending": self._resync_pending,
            "warming_up": self._in_warmup(),
            "healthy": self.is_healthy(symbol),
            "symbols": {
                sym: {
                    "book_age": self.age_of(sym, "book"),
                    "ticker_age": self.age_of(sym, "ticker"),
                    "stale": self.is_stale(sym, "book") or self.is_stale(sym, "ticker"),
                    "reason": self._unhealthy_reason(sym),
                }
                for sym in targets
            },
        }

    # --- guarded accessors -------------------------------------------------

    def ticker(self, symbol: str) -> Ticker:
        self._require_healthy(symbol, "ticker")
        return self._tickers[symbol]

    def book(self, symbol: str) -> BookTop:
        self._require_healthy(symbol, "book")
        return self._books[symbol]

    def _require_healthy(self, symbol: str, kind: str) -> None:
        # Deliberately checks *every* stream for the symbol, not just `kind`.
        # Book and ticker arrive on the same socket, so one going stale while the other
        # stays fresh means a subscription died — the fresh stream is not trustworthy
        # evidence that the feed is alive. This keeps the accessors exactly as strict
        # as is_healthy(); an accessor must never serve data the watchdog rejects.
        reason = self._unhealthy_reason(symbol)
        if reason is not None:
            self.stats.stale_rejections += 1
            raise FeedNotHealthy(f"{symbol}: {reason} (requested {kind})")

    def position(self, symbol: str) -> PositionEvent | None:
        """Cached position. None means flat *as far as the feed knows*.

        Never use this to decide whether protection exists — Phase 10 re-reads the
        position and its protective orders from REST before acting.
        """
        if self._resync_pending:
            raise FeedNotHealthy(f"{symbol}: REST resync pending; position state untrusted")
        return self._positions.get(symbol)

    # --- the liquidation-tier boundary -------------------------------------

    def maintenance_rate(self, symbol: str) -> float:
        """Always raises. The WebSocket has no tier information.

        Kept as an explicit trap: a caller reaching for a flat maintenance rate gets a
        loud error naming the correct interface instead of a silently wrong number.
        """
        self.stats.tier_bypass_attempts += 1
        raise LiquidationTierUnavailable(
            f"{symbol}: the market-data layer cannot supply a maintenance rate. "
            "Maintenance rate is tiered by notional (BTC_USDT has 19 tiers, 0.30% -> 0.50%+). "
            "Use MarketDataFeed.select_tier(symbol, notional) instead."
        )

    async def select_tier(self, symbol: str, notional: float) -> RiskTier:
        """Resolve the risk tier for an actual position notional.

        Phase 3 supplies the integration point only; the full liquidation engine lands
        in Phase 7 and consumes this. Tiers come from REST
        (``/futures/{settle}/risk_limit_tiers``) and are cached by the REST client.
        """
        if notional < 0:
            raise ValueError(f"notional must be >= 0, got {notional}")
        tiers = await self._rest.get_risk_tiers(symbol)
        return select_tier(tiers, notional)


    # --- message handling --------------------------------------------------

    def handle_message(self, raw: str | bytes) -> str | None:
        """Parse and apply one frame. Returns the channel handled, or None if dropped.

        Every drop path increments a stat rather than raising: one bad frame must not
        kill the feed, but it must be visible.
        """
        self.stats.messages += 1

        try:
            message = json.loads(raw)
        except (ValueError, TypeError):
            self.stats.malformed += 1
            log.warning("dropping unparseable frame (%d bytes)", len(raw or ""))
            return None

        if not isinstance(message, dict):
            self.stats.malformed += 1
            return None

        channel = message.get("channel")
        event = message.get("event")

        if channel == CH_PONG:
            self._last_pong = self._clock()
            self.stats.pongs += 1
            return channel

        error = message.get("error")
        if error:
            code = int(error.get("code", 0) or 0)
            self.stats.subscription_failures += 1
            raise SubscriptionError(str(channel), code, str(error.get("message", "")))

        if event == "subscribe":
            result = message.get("result") or {}
            if result.get("status") == "success":
                self._subscribed.add(str(channel))
            return channel

        if event != "update":
            return channel

        result = message.get("result")
        if result is None:
            self.stats.malformed += 1
            return None

        try:
            if channel == CH_TICKERS:
                return self._apply_tickers(result, message)
            if channel == CH_BOOK_TICKER:
                return self._apply_book_ticker(result)
            if channel == CH_ORDERS:
                return self._apply_orders(result)
            if channel == CH_POSITIONS:
                return self._apply_positions(result)
        except (KeyError, TypeError, ValueError) as exc:
            # Partial or malformed payload — a missing field, a bad number, a crossed book.
            self.stats.malformed += 1
            log.warning("malformed %s payload: %s", channel, exc)
            return None

        return channel

    def _apply_tickers(self, result: Any, message: Mapping[str, Any]) -> str:
        rows = result if isinstance(result, list) else [result]
        event_ms = int(message.get("time_ms") or 0)
        now = self._clock()
        for row in rows:
            symbol = row["contract"]
            # Out-of-order guard: the exchange stamps each frame; older frames are dropped.
            previous = self._last_ticker_ms.get(symbol)
            if previous is not None and event_ms and event_ms < previous:
                self.stats.out_of_order += 1
                continue
            if previous is not None and event_ms and event_ms == previous:
                self.stats.duplicates += 1
                continue
            if event_ms:
                self._last_ticker_ms[symbol] = event_ms
            self._tickers[symbol] = Ticker.from_event(row, now)
        return CH_TICKERS

    def _apply_book_ticker(self, result: Mapping[str, Any]) -> str:
        top = BookTop.from_event(result, self._clock())
        previous = self._last_book_id.get(top.symbol)
        if previous is not None and top.update_id:
            if top.update_id == previous:
                self.stats.duplicates += 1
                return CH_BOOK_TICKER
            if top.update_id < previous:
                self.stats.out_of_order += 1
                return CH_BOOK_TICKER
        if top.update_id:
            self._last_book_id[top.symbol] = top.update_id
        self._books[top.symbol] = top
        return CH_BOOK_TICKER

    def _apply_orders(self, result: Any) -> str:
        rows = result if isinstance(result, list) else [result]
        for row in rows:
            event = OrderEvent.from_event(row)
            seen = self._seen_order_events.get(event.order_id)
            if seen is not None and event.exchange_ts_ms:
                if event.exchange_ts_ms < seen:
                    self.stats.out_of_order += 1
                    continue
                if event.exchange_ts_ms == seen and event.order_id in self._orders:
                    self.stats.duplicates += 1
                    continue
            if event.exchange_ts_ms:
                self._seen_order_events[event.order_id] = event.exchange_ts_ms
            self._orders[event.order_id] = event
        return CH_ORDERS

    def _apply_positions(self, result: Any) -> str:
        rows = result if isinstance(result, list) else [result]
        for row in rows:
            event = PositionEvent.from_event(row)
            # Gate.io increments update_id on every position change — the strongest
            # ordering signal available, so prefer it over timestamps.
            previous = self._last_position_id.get(event.symbol)
            if previous is not None and event.update_id:
                if event.update_id == previous:
                    self.stats.duplicates += 1
                    continue
                if event.update_id < previous:
                    self.stats.out_of_order += 1
                    continue
            if event.update_id:
                self._last_position_id[event.symbol] = event.update_id
            if event.size == 0:
                self._positions.pop(event.symbol, None)
            else:
                self._positions[event.symbol] = event
        return CH_POSITIONS


    # --- reconnect / resync ------------------------------------------------

    def _invalidate(self, reason: str) -> None:
        """Drop every cached value. WebSocket state is not trusted across a disconnect."""
        log.warning("invalidating feed state: %s", reason)
        self._connected = False
        self._connected_at = None
        self._last_pong = None
        self._tickers.clear()
        self._books.clear()
        self._positions.clear()
        self._orders.clear()
        self._last_book_id.clear()
        self._last_ticker_ms.clear()
        self._last_position_id.clear()
        self._seen_order_events.clear()
        self._subscribed.clear()
        self._resync_pending = True

    def reconnect_delay(self, attempt: int) -> float:
        """Exponential backoff with jitter, capped. attempt is 0-based."""
        delay = min(self._base_delay * (2 ** attempt), self._max_delay)
        return delay + random.uniform(0, delay * self._jitter)

    def _record_reconnect(self) -> None:
        """Track attempts so a reconnect storm is detectable rather than infinite."""
        now = self._clock()
        cutoff = now - self._window_seconds
        self._reconnect_times = [t for t in self._reconnect_times if t > cutoff]
        self._reconnect_times.append(now)
        self.stats.reconnect_attempts += 1

    def reconnect_storm(self) -> bool:
        now = self._clock()
        cutoff = now - self._window_seconds
        recent = [t for t in self._reconnect_times if t > cutoff]
        return len(recent) > self._max_attempts_window

    async def resync(self) -> dict[str, Any]:
        """Rebuild authoritative state from REST after a (re)connect.

        This is the only path that clears ``resync_pending``. Until it succeeds the feed
        reports unhealthy and every guarded accessor raises, so no signal can be built
        on post-disconnect WebSocket state.
        """
        cfg = self._resync_cfg
        summary: dict[str, Any] = {
            "positions": 0, "open_orders": 0, "price_orders": 0, "contracts": 0,
        }

        if not self._cfg.credentials.present:
            # Public-only mode (paper without keys): market state can still be reconciled.
            log.info("resync: no credentials, reconciling public market state only")

        last_error: Exception | None = None
        for attempt in range(self._resync_attempts):
            try:
                # Market state first: contract specs and risk tiers back the tier interface.
                for symbol in self._symbols:
                    await self._rest.get_contract(symbol)
                    await self._rest.get_risk_tiers(symbol)
                    summary["contracts"] += 1

                if self._cfg.credentials.present:
                    if cfg.get("reconcile_positions", True):
                        positions = await self._rest.list_positions(holding=True)
                        self._positions.clear()
                        self._last_position_id.clear()
                        for raw in positions:
                            event = PositionEvent.from_event(raw)
                            if event.size != 0:
                                self._positions[event.symbol] = event
                                if event.update_id:
                                    self._last_position_id[event.symbol] = event.update_id
                        summary["positions"] = len(self._positions)

                    if cfg.get("reconcile_open_orders", True):
                        self._orders.clear()
                        self._seen_order_events.clear()
                        for symbol in self._symbols:
                            for raw in await self._rest.list_open_orders(symbol):
                                event = OrderEvent.from_event(raw)
                                self._orders[event.order_id] = event
                        summary["open_orders"] = len(self._orders)

                    if cfg.get("reconcile_price_orders", True):
                        # The protective SL/TP orders. Counted, not cached: Phase 10
                        # re-reads them from REST at decision time regardless.
                        total = 0
                        for symbol in self._symbols:
                            total += len(await self._rest.list_price_orders(symbol))
                        summary["price_orders"] = total

                self._resync_pending = False
                self.stats.resyncs += 1
                log.info("resync complete: %s", summary)
                return summary

            except (GateAPIError, OSError, asyncio.TimeoutError) as exc:
                last_error = exc
                self.stats.resync_failures += 1
                log.warning("resync attempt %d/%d failed: %s",
                            attempt + 1, self._resync_attempts, exc)
                if attempt < self._resync_attempts - 1:
                    await asyncio.sleep(self.reconnect_delay(attempt))

        # Resync failed: stay unhealthy. Better to trade nothing than to trade blind.
        self._resync_pending = True
        raise FeedNotHealthy(f"REST resync failed after {self._resync_attempts} attempts: "
                             f"{last_error}")

    # --- connection --------------------------------------------------------

    def subscriptions(self) -> list[dict[str, Any]]:
        """Frames for every channel this feed needs, public first."""
        frames: list[dict[str, Any]] = []
        for symbol in self._symbols:
            frames.append(build_request(CH_TICKERS, "subscribe", [symbol]))
            frames.append(build_request(CH_BOOK_TICKER, "subscribe", [symbol]))
        if self._cfg.credentials.present:
            user_id = str(self._cfg.get("websocket.user_id", "") or "")
            for symbol in self._symbols:
                payload = [user_id, symbol] if user_id else [symbol]
                frames.append(build_request(
                    CH_ORDERS, "subscribe", payload, credentials=self._cfg.credentials))
                frames.append(build_request(
                    CH_POSITIONS, "subscribe", payload, credentials=self._cfg.credentials))
        return frames

    async def _subscribe_all(self, ws: Any) -> None:
        for frame in self.subscriptions():
            await ws.send(json.dumps(frame))

    async def _heartbeat(self, ws: Any) -> None:
        """Send pings; a missing pong means the link is dead even if the socket is open."""
        while not self._stop.is_set():
            await asyncio.sleep(self._ping_interval)
            try:
                await ws.send(json.dumps({"time": int(time.time()), "channel": CH_PING}))
            except Exception:  # socket already going down; the read loop will handle it
                return
            if self._last_pong is not None and (
                self._clock() - self._last_pong > self._pong_timeout
            ):
                self.stats.heartbeat_timeouts += 1
                log.warning("no pong for %.1fs — forcing reconnect", self._pong_timeout)
                with contextlib.suppress(Exception):
                    await ws.close()
                return

    async def run(self) -> None:
        """Connect, subscribe, resync, and stay connected until :meth:`stop`."""
        attempt = 0
        while not self._stop.is_set():
            try:
                async with self._connect_factory(
                    self._url, open_timeout=self._connect_timeout
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    self._connected_at = self._clock()
                    self._last_pong = self._clock()  # grace until the first pong
                    self.stats.connects += 1
                    attempt = 0
                    log.info("websocket connected: %s", self._url)

                    await self._subscribe_all(ws)

                    # REST is the source of truth after any (re)connect.
                    await self.resync()

                    heartbeat = asyncio.create_task(self._heartbeat(ws))
                    try:
                        async for raw in ws:
                            try:
                                self.handle_message(raw)
                            except SubscriptionError as exc:
                                if exc.terminal:
                                    log.error("terminal subscription failure: %s", exc)
                                    raise
                                log.warning("subscription failure: %s", exc)
                    finally:
                        heartbeat.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await heartbeat

            except SubscriptionError:
                self._invalidate("terminal subscription failure")
                raise
            except asyncio.CancelledError:
                self._invalidate("cancelled")
                raise
            except Exception as exc:
                self.stats.disconnects += 1
                self._invalidate(f"{type(exc).__name__}: {exc}")

                if self._stop.is_set():
                    break
                self._record_reconnect()
                if self.reconnect_storm():
                    log.error("reconnect storm: %d attempts in %.0fs — giving up",
                              len(self._reconnect_times), self._window_seconds)
                    raise FeedNotHealthy(
                        f"reconnect storm: more than {self._max_attempts_window} attempts "
                        f"in {self._window_seconds:.0f}s"
                    ) from exc

                delay = self.reconnect_delay(attempt)
                log.warning("reconnecting in %.2fs (attempt %d)", delay, attempt + 1)
                await asyncio.sleep(delay)
                attempt += 1

        self._invalidate("stopped")

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()




