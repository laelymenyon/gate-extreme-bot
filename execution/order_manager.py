"""Order state machine: submit, then find out what actually happened.

PHASE 8.

The rule this module exists to enforce is that **an API response is not proof**. A 200 on
a POST means the exchange accepted the request, not that the order filled, not that it
filled at the price asked, and not that it filled once. Every terminal fact — filled or
not, how much, at what average price — is re-read from the exchange with
``GET /futures/{settle}/orders/{id}`` before anything downstream is allowed to believe it.
At 100x, a position the bot thinks is flat, or thinks is protected, is how an account ends.

**Unfilled is a normal outcome.** Entries are post-only (``tif="poc"``) because the maker
fee is a rebate and fee drag at these stop widths decides profitability (ARCHITECTURE §5).
Post-only orders frequently do not fill; the manager cancels cleanly after
``take_profit.entry_fill_timeout_seconds`` and reports ``EXPIRED``. That is not an error
path, and callers must not treat it as one.

**Nothing reaches the network unless three switches agree.** :meth:`OrderManager.for_config`
returns a :class:`SimulatedGateway` unless ``Config.live_enabled`` is true, so DRY_RUN and
paper runs execute against an in-process book and cannot emit a request. Behind that, the
Phase 2 client raises ``WriteBlocked`` before opening a socket. Simulation is the default
and the live path is opt-in, so a wiring mistake fails toward doing nothing.

**Idempotency is not optional.** Every write carries a ``t-`` client id derived from its
purpose, symbol and a caller-supplied nonce, so a retry after a timeout is detectable
rather than a second position. The Phase 2 client refuses to retry a write without one.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping, Protocol

__all__ = [
    "OrderState",
    "OrderRecord",
    "ExecutionError",
    "OrderGateway",
    "SimulatedGateway",
    "ExecutionParams",
    "OrderManager",
    "idempotency_key",
    "TEXT_KEY_RE",
]

#: Gate.io's client-id shape, mirrored from ``exchange.gate_client._TEXT_KEY_RE`` so a bad
#: key is rejected here — where the caller can see why — rather than at the socket.
TEXT_KEY_RE = re.compile(r"^t-[0-9A-Za-z_.\-]{1,28}$")


class ExecutionError(Exception):
    """A state the caller must resolve. Never raised for an ordinary unfilled entry."""


class OrderState(str, Enum):
    """Where an order is. ``UNKNOWN`` is a real state, not a placeholder.

    ``UNKNOWN`` means the exchange could not be asked, or answered in a way that could not
    be interpreted. It is deliberately distinct from ``REJECTED``: a rejected order
    certainly does not exist, while an unknown one might, and the two demand opposite
    responses.
    """

    NEW = "new"
    SUBMITTED = "submitted"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"          # post-only entry that never filled — a normal outcome
    REJECTED = "rejected"
    UNKNOWN = "unknown"

    @property
    def terminal(self) -> bool:
        return self in (
            OrderState.FILLED, OrderState.CANCELLED, OrderState.EXPIRED,
            OrderState.REJECTED,
        )

    @property
    def has_exposure(self) -> bool:
        """Whether size may exist on the exchange because of this order.

        ``UNKNOWN`` counts. An order whose fate could not be established must be assumed to
        have filled until proven otherwise; the opposite assumption leaves leveraged size
        unprotected.
        """
        return self in (
            OrderState.FILLED, OrderState.PARTIALLY_FILLED, OrderState.UNKNOWN,
        )


@dataclass(frozen=True)
class OrderRecord:
    """What the exchange says about one order. Every field is read back, never assumed."""

    order_id: str
    symbol: str
    state: OrderState
    requested_size: int          # signed contract count: + long, - short
    filled_size: int = 0         # signed, same convention
    average_price: float = 0.0
    price: str = "0"
    tif: str = "poc"
    text: str = ""
    reduce_only: bool = False
    reason: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def direction(self) -> int:
        return 1 if self.requested_size > 0 else -1 if self.requested_size < 0 else 0

    @property
    def remaining(self) -> int:
        return self.requested_size - self.filled_size

    @property
    def filled(self) -> bool:
        return self.state is OrderState.FILLED

    def summary(self) -> str:
        return (
            f"{self.symbol} {self.state.value} {self.filled_size}/{self.requested_size} "
            f"@ {self.average_price:g}"
        )


def idempotency_key(purpose: str, symbol: str, nonce: int | str) -> str:
    """Build a ``t-`` client id that survives a retry.

    The key is the whole reason a timed-out write is recoverable: resubmitting with the
    same id lets the exchange collapse the duplicate instead of opening a second position.
    Gate.io allows 28 characters after ``t-``, so the symbol is abbreviated deliberately
    (``BTC_USDT`` -> ``btc``) rather than truncated blindly into a collision.
    """
    base = str(symbol).split("_")[0][:6].lower()
    key = re.sub(r"[^0-9A-Za-z_.\-]", "", f"t-{str(purpose)[:3]}{base}{nonce}")
    if not TEXT_KEY_RE.match(key):
        raise ValueError(
            f"cannot build a valid client id from purpose={purpose!r}, symbol={symbol!r}, "
            f"nonce={nonce!r}; got {key!r}"
        )
    return key


def _filled_size(raw: Mapping[str, Any], requested_size: int) -> int:
    """Signed filled quantity. Gate reports ``left`` with the same sign as ``size``."""
    try:
        size = int(raw.get("size", requested_size))
        left = int(raw.get("left", 0))
    except (TypeError, ValueError):
        return 0
    return size - left


def _state_from_raw(raw: Mapping[str, Any], requested_size: int) -> tuple[OrderState, str]:
    """Interpret a Gate.io order payload.

    ``status`` is ``open`` or ``finished``; when finished, ``finish_as`` says how. A payload
    that says neither is ``UNKNOWN`` — not ``REJECTED``, because we cannot prove the order
    does not exist.
    """
    status = str(raw.get("status", "")).lower()
    finish_as = str(raw.get("finish_as", "")).lower()
    filled = _filled_size(raw, requested_size)

    if status == "open":
        return (
            OrderState.PARTIALLY_FILLED if filled else OrderState.OPEN,
            f"open, {raw.get('left')} left",
        )
    if status in ("finished", "cancelled", "canceled") or finish_as:
        if finish_as == "filled":
            return OrderState.FILLED, "filled"
        if finish_as in ("cancelled", "canceled", "_new", "poc", "stp"):
            # A post-only order that would have crossed is cancelled on arrival.
            return (
                OrderState.PARTIALLY_FILLED if filled else OrderState.CANCELLED,
                f"cancelled ({finish_as or 'no reason given'})",
            )
        if finish_as in ("ioc", "fok", "auto_deleveraged", "reduce_only",
                         "position_closed", "liquidated"):
            if filled and filled == requested_size:
                return OrderState.FILLED, f"finished as {finish_as}"
            return (
                OrderState.PARTIALLY_FILLED if filled else OrderState.CANCELLED,
                f"finished as {finish_as}",
            )
        return OrderState.UNKNOWN, f"unrecognised finish_as {finish_as!r}"
    return OrderState.UNKNOWN, f"unrecognised status {status!r}"


class OrderGateway(Protocol):
    """The subset of the exchange this layer touches.

    A protocol rather than the concrete client, so the simulator is a peer of the live path
    instead of a special case threaded through it with conditionals.
    """

    async def place_order(self, symbol: str, size: int, *, price: str | None = None,
                          tif: str = "poc", reduce_only: bool = False,
                          close: bool = False, text: str) -> dict[str, Any]: ...

    async def get_order(self, order_id: str | int) -> dict[str, Any]: ...

    async def cancel_order(self, order_id: str | int) -> dict[str, Any]: ...

    async def place_price_trigger_order(self, symbol: str, *, trigger_price: str,
                                        order_price: str = "0", size: int = 0, rule: int,
                                        price_type: int = 1, tif: str = "ioc",
                                        reduce_only: bool = True, close: bool = False,
                                        text: str, expiration: int = 0) -> dict[str, Any]: ...

    async def list_price_orders(self, symbol: str | None = None) -> list[dict[str, Any]]: ...

    async def cancel_price_order(self, order_id: str | int) -> dict[str, Any]: ...

    async def get_position(self, symbol: str) -> dict[str, Any]: ...

    async def countdown_cancel_all(self, timeout_seconds: int,
                                   symbol: str | None = None) -> dict[str, Any]: ...


class SimulatedGateway:
    """In-process exchange for paper and DRY_RUN runs. Opens no socket, ever.

    Fills are modelled honestly rather than generously, because a simulator that fills
    everything makes post-only look free and would flatter every result built on it:

    * A post-only (``poc``) entry rests. It fills only when :meth:`advance` is given a price
      that trades through it — a buy at or below its limit, a sell at or above.
    * A market order (``price="0"``, ``tif="ioc"``) fills immediately at the last price.
    * A price-triggered order fires when :meth:`advance` crosses its trigger and fills at
      the trigger price. Modelling slippage beyond that is the backtester's job (Phase 9),
      not a number invented here.
    """

    def __init__(self, *, last_price: float = 0.0, leverage: int = 100,
                 maintenance_rate: float = 0.003, taker_fee: float = 0.00075) -> None:
        self.last_price = float(last_price)
        self.leverage = int(leverage)
        self.maintenance_rate = float(maintenance_rate)
        self.taker_fee = float(taker_fee)
        self.orders: dict[str, dict[str, Any]] = {}
        self.price_orders: dict[str, dict[str, Any]] = {}
        self.positions: dict[str, dict[str, Any]] = {}
        self.countdown_seconds: int | None = None
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._next_id = 1000

    # --- internals ---------------------------------------------------------

    def _record(self, name: str, **kwargs: Any) -> None:
        self.calls.append((name, kwargs))

    def _id(self) -> str:
        self._next_id += 1
        return str(self._next_id)

    def _apply_fill(self, symbol: str, size: int, price: float) -> None:
        position = self.positions.setdefault(
            symbol,
            {"contract": symbol, "size": 0, "entry_price": 0.0, "liq_price": 0.0,
             "leverage": str(self.leverage), "margin": 0.0},
        )
        old = int(position["size"])
        new = old + size
        if new == 0:
            position.update(size=0, entry_price=0.0, liq_price=0.0, margin=0.0)
            return
        if old == 0 or (old > 0) != (size > 0):
            position["entry_price"] = float(price)
        else:                                   # same-direction add: weighted average
            position["entry_price"] = (
                (abs(old) * float(position["entry_price"]) + abs(size) * float(price))
                / abs(new)
            )
        position["size"] = new
        # Mirrors the Phase 7 formula, so a simulated position is checkable by the same
        # guard that checks a live one.
        entry = float(position["entry_price"])
        distance = (1.0 / self.leverage) - self.maintenance_rate - self.taker_fee
        position["liq_price"] = entry * (1 - distance) if new > 0 else entry * (1 + distance)

    def _fill(self, order: dict[str, Any], price: float) -> None:
        order["left"] = 0
        order["status"] = "finished"
        order["finish_as"] = "filled"
        order["fill_price"] = str(price)
        self._apply_fill(order["contract"], int(order["size"]), float(price))

    def _maybe_fill(self, order: dict[str, Any], price: float) -> None:
        """A resting limit fills only when the market trades through it."""
        limit = float(order["price"])
        if limit <= 0:
            return
        size = int(order["size"])
        if (size > 0 and price <= limit) or (size < 0 and price >= limit):
            self._fill(order, limit)

    # --- gateway surface ---------------------------------------------------

    async def place_order(self, symbol: str, size: int, *, price: str | None = None,
                          tif: str = "poc", reduce_only: bool = False,
                          close: bool = False, text: str) -> dict[str, Any]:
        if not TEXT_KEY_RE.match(text):
            raise ValueError(f"text={text!r} is not a valid client id")
        if size == 0 and not close:
            raise ValueError("size=0 is only valid together with close=True")
        if price is None and tif != "ioc":
            raise ValueError("a market order (price 0) requires tif='ioc'")
        self._record("place_order", symbol=symbol, size=size, price=price, tif=tif,
                     reduce_only=reduce_only, close=close, text=text)

        # A duplicate client id returns the original order rather than a second one — the
        # behaviour the idempotency key exists to buy.
        for existing in self.orders.values():
            if existing["text"] == text:
                return dict(existing)

        if close:
            size = -int(self.positions.get(symbol, {}).get("size", 0))

        order = {
            "id": self._id(), "contract": symbol, "size": size, "left": size,
            "price": "0" if price is None else str(price), "tif": tif, "text": text,
            "status": "open", "finish_as": "", "fill_price": "0",
            "is_reduce_only": reduce_only, "is_close": close,
        }
        self.orders[order["id"]] = order

        if price is None:                       # market: crosses immediately
            self._fill(order, self.last_price)
        elif self.last_price:
            self._maybe_fill(order, self.last_price)
        return dict(order)

    async def get_order(self, order_id: str | int) -> dict[str, Any]:
        self._record("get_order", order_id=order_id)
        try:
            return dict(self.orders[str(order_id)])
        except KeyError:
            raise ExecutionError(f"unknown order {order_id}") from None

    async def cancel_order(self, order_id: str | int) -> dict[str, Any]:
        self._record("cancel_order", order_id=order_id)
        order = self.orders.get(str(order_id))
        if order is None:
            raise ExecutionError(f"unknown order {order_id}")
        if order["status"] == "open":
            order["status"] = "finished"
            order["finish_as"] = "cancelled"
        return dict(order)

    async def place_price_trigger_order(self, symbol: str, *, trigger_price: str,
                                        order_price: str = "0", size: int = 0, rule: int,
                                        price_type: int = 1, tif: str = "ioc",
                                        reduce_only: bool = True, close: bool = False,
                                        text: str, expiration: int = 0) -> dict[str, Any]:
        if rule not in (1, 2):
            raise ValueError("rule must be 1 (>=) or 2 (<=)")
        if price_type not in (0, 1, 2):
            raise ValueError("price_type must be 0 (last), 1 (mark), or 2 (index)")
        if not TEXT_KEY_RE.match(text):
            raise ValueError(f"text={text!r} is not a valid client id")
        self._record("place_price_trigger_order", symbol=symbol,
                     trigger_price=trigger_price, size=size, rule=rule,
                     price_type=price_type, reduce_only=reduce_only, close=close, text=text)

        order = {
            "id": self._id(),
            "status": "open",
            "initial": {"contract": symbol, "size": size, "price": order_price, "tif": tif,
                        "text": text, "reduce_only": reduce_only, "close": close},
            "trigger": {"strategy_type": 0, "price_type": price_type,
                        "price": str(trigger_price), "rule": rule,
                        "expiration": expiration},
        }
        self.price_orders[order["id"]] = order
        return dict(order)

    async def list_price_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        self._record("list_price_orders", symbol=symbol)
        return [
            dict(order) for order in self.price_orders.values()
            if order["status"] == "open"
            and (symbol is None or order["initial"]["contract"] == symbol)
        ]

    async def cancel_price_order(self, order_id: str | int) -> dict[str, Any]:
        self._record("cancel_price_order", order_id=order_id)
        order = self.price_orders.get(str(order_id))
        if order is None:
            raise ExecutionError(f"unknown price order {order_id}")
        order["status"] = "cancelled"
        return dict(order)

    async def get_position(self, symbol: str) -> dict[str, Any]:
        self._record("get_position", symbol=symbol)
        return dict(self.positions.get(
            symbol,
            {"contract": symbol, "size": 0, "entry_price": 0.0, "liq_price": 0.0,
             "leverage": str(self.leverage), "margin": 0.0},
        ))

    async def countdown_cancel_all(self, timeout_seconds: int,
                                   symbol: str | None = None) -> dict[str, Any]:
        self._record("countdown_cancel_all", timeout_seconds=timeout_seconds, symbol=symbol)
        self.countdown_seconds = int(timeout_seconds)
        return {"triggle_time": int(timeout_seconds)}

    # --- driving the simulation -------------------------------------------

    def advance(self, price: float) -> list[str]:
        """Move the market. Returns the ids of everything that filled or triggered."""
        self.last_price = float(price)
        touched: list[str] = []

        for order in list(self.orders.values()):
            if order["status"] == "open":
                self._maybe_fill(order, self.last_price)
                if order["status"] != "open":
                    touched.append(order["id"])

        for order in list(self.price_orders.values()):
            if order["status"] != "open":
                continue
            trigger = float(order["trigger"]["price"])
            rule = int(order["trigger"]["rule"])
            if not (self.last_price >= trigger if rule == 1 else self.last_price <= trigger):
                continue
            order["status"] = "finished"
            initial = order["initial"]
            size = int(initial["size"])
            if initial.get("close") or size == 0:
                size = -int(self.positions.get(initial["contract"], {}).get("size", 0))
            if size:
                self._apply_fill(initial["contract"], size, trigger)
            touched.append(order["id"])
        return touched


@dataclass(frozen=True)
class ExecutionParams:
    """Timings and order semantics from ``take_profit`` and ``execution``."""

    entry_tif: str = "poc"
    entry_fill_timeout_seconds: float = 20.0
    poll_interval_seconds: float = 0.5
    verify_attempts: int = 3

    def __post_init__(self) -> None:
        if self.entry_tif not in ("poc", "gtc", "ioc", "fok"):
            raise ValueError(f"entry_tif {self.entry_tif!r} is not a Gate.io time-in-force")
        if self.entry_fill_timeout_seconds <= 0:
            raise ValueError("entry_fill_timeout_seconds must be > 0")
        if self.poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be > 0")
        if self.verify_attempts < 1:
            raise ValueError("verify_attempts must be >= 1")

    @classmethod
    def from_config(cls, cfg: Any) -> "ExecutionParams":
        base = cls()
        return cls(
            entry_tif=str(cfg.get("take_profit.entry_tif", base.entry_tif)),
            entry_fill_timeout_seconds=float(
                cfg.get("take_profit.entry_fill_timeout_seconds",
                        base.entry_fill_timeout_seconds)
            ),
            poll_interval_seconds=float(
                cfg.get("execution.poll_interval_seconds", base.poll_interval_seconds)
            ),
            verify_attempts=int(
                cfg.get("protection.sl_retry_attempts", base.verify_attempts)
            ),
        )


class OrderManager:
    """Submits orders, then establishes from the exchange what actually happened."""

    def __init__(self, gateway: OrderGateway, params: ExecutionParams | None = None,
                 *, live: bool = False, clock: Any = None, sleep: Any = None) -> None:
        self.gateway = gateway
        self.params = params or ExecutionParams()
        self.live = bool(live)
        self._clock = clock or time.monotonic
        self._sleep_fn = sleep
        self.history: list[OrderRecord] = []

    @classmethod
    def for_config(cls, cfg: Any, client: OrderGateway | None = None,
                   params: ExecutionParams | None = None,
                   *, last_price: float = 0.0, **kwargs: Any) -> "OrderManager":
        """Build from config, defaulting to simulation.

        The live gateway is used **only** when all three safety switches agree *and* a
        client was actually supplied. Every other combination — including "live_enabled but
        nobody passed a client" — lands on the simulator, so a wiring mistake fails toward
        doing nothing rather than toward something irreversible.
        """
        live = bool(getattr(cfg, "live_enabled", False)) and client is not None
        gateway: OrderGateway = client if live else SimulatedGateway(
            last_price=last_price, leverage=int(cfg.get("leverage.default", 100)),
            taker_fee=float(cfg.get("backtest.fee_taker", 0.00075)),
        )
        return cls(gateway, params or ExecutionParams.from_config(cfg), live=live, **kwargs)

    # --- reading the truth -------------------------------------------------

    async def read_order(self, order_id: str, symbol: str,
                         requested_size: int) -> OrderRecord:
        """Re-read one order from the exchange and interpret it.

        An unreadable order becomes ``UNKNOWN`` rather than an exception: the caller has to
        decide what to do, and for an entry the safe assumption is that size may exist.
        """
        try:
            raw = await self.gateway.get_order(order_id)
        except Exception as exc:  # noqa: BLE001 — any failure means "we do not know"
            return OrderRecord(
                order_id=str(order_id), symbol=symbol, state=OrderState.UNKNOWN,
                requested_size=requested_size,
                reason=f"could not re-read the order: {type(exc).__name__}: {exc}",
            )
        return self.record_from_raw(raw, symbol, requested_size)

    def record_from_raw(self, raw: Mapping[str, Any], symbol: str,
                        requested_size: int) -> OrderRecord:
        state, reason = _state_from_raw(raw, requested_size)
        price = raw.get("fill_price") or raw.get("fill_price_avg") or 0
        return OrderRecord(
            order_id=str(raw.get("id", "")),
            symbol=str(raw.get("contract", symbol)),
            state=state,
            requested_size=requested_size,
            filled_size=_filled_size(raw, requested_size),
            average_price=float(price or 0),
            price=str(raw.get("price", "0")),
            tif=str(raw.get("tif", "")),
            text=str(raw.get("text", "")),
            reduce_only=bool(raw.get("is_reduce_only", False)),
            reason=reason,
            raw=dict(raw),
        )

    # --- entry -------------------------------------------------------------

    async def submit_entry(self, symbol: str, size: int, price: str, nonce: int | str,
                           *, tif: str | None = None) -> OrderRecord:
        """Place a post-only entry and wait, bounded, for the exchange's verdict.

        Returns ``EXPIRED`` when the order never filled and was cancelled. That is the
        expected outcome much of the time and is not an error: at these stop widths a taker
        entry needs a ~73% win rate to break even (ARCHITECTURE §5), so not filling is
        strictly better than filling expensively.
        """
        if size == 0:
            raise ValueError("cannot submit an entry for 0 contracts")
        tif = tif or self.params.entry_tif
        text = idempotency_key("ent", symbol, nonce)

        raw = await self.gateway.place_order(
            symbol, size, price=price, tif=tif, reduce_only=False, text=text,
        )
        order_id = str(raw.get("id", "") or "")
        if not order_id:
            record = OrderRecord(
                order_id="", symbol=symbol, state=OrderState.UNKNOWN, requested_size=size,
                text=text,
                reason="the exchange returned no order id; the order may or may not exist",
            )
            self.history.append(record)
            return record

        # The submit response is a receipt, not a result. Poll for the fact.
        deadline = self._now() + self.params.entry_fill_timeout_seconds
        record = await self.read_order(order_id, symbol, size)
        while not record.state.terminal and self._now() < deadline:
            await self._sleep(self.params.poll_interval_seconds)
            record = await self.read_order(order_id, symbol, size)

        if record.state.terminal:
            self.history.append(record)
            return record

        # Timed out still resting: cancel, then re-read to learn whether the cancel raced a
        # fill. Trusting the cancel is exactly the assumption that leaves an unprotected
        # position behind.
        try:
            await self.gateway.cancel_order(order_id)
        except Exception as exc:  # noqa: BLE001
            record = await self.read_order(order_id, symbol, size)
            if not record.state.has_exposure:
                record = replace(
                    record, state=OrderState.UNKNOWN,
                    reason=f"cancel failed and the order is not confirmed gone: {exc}",
                )
            self.history.append(record)
            return record

        record = await self.read_order(order_id, symbol, size)
        if record.state is OrderState.CANCELLED and record.filled_size == 0:
            record = replace(
                record, state=OrderState.EXPIRED,
                reason=(f"post-only entry did not fill within "
                        f"{self.params.entry_fill_timeout_seconds:g}s and was cancelled"),
            )
        self.history.append(record)
        return record

    # --- exit --------------------------------------------------------------

    async def close_position(self, symbol: str, nonce: int | str,
                             reason: str = "") -> OrderRecord:
        """Market-close whatever is open, then confirm it is gone.

        Uses ``close=True`` with ``reduce_only=True`` so it can only reduce exposure: a
        mis-signed size here would otherwise open a position in the opposite direction.
        """
        text = idempotency_key("cls", symbol, nonce)
        raw = await self.gateway.place_order(
            symbol, 0, price=None, tif="ioc", reduce_only=True, close=True, text=text,
        )
        order_id = str(raw.get("id", "") or "")
        if order_id:
            record = await self.read_order(order_id, symbol, int(raw.get("size", 0) or 0))
        else:
            record = OrderRecord(order_id="", symbol=symbol, state=OrderState.UNKNOWN,
                                 requested_size=0,
                                 reason="close returned no order id")
        if reason:
            record = replace(record, reason=f"{reason}: {record.reason}")
        self.history.append(record)
        return record

    async def position_size(self, symbol: str) -> int:
        """Size held, straight from the exchange — the only authority on what is open."""
        raw = await self.gateway.get_position(symbol)
        try:
            return int(raw.get("size", 0) or 0)
        except (TypeError, ValueError):
            raise ExecutionError(
                f"{symbol}: position size {raw.get('size')!r} could not be read"
            ) from None

    async def arm_dead_man_switch(self, seconds: int, symbol: str | None = None) -> None:
        """Ask the exchange to cancel our orders if we stop checking in.

        A crashed bot must not leave resting orders behind with nobody watching them.
        """
        await self.gateway.countdown_cancel_all(int(seconds), symbol)

    # --- clock -------------------------------------------------------------

    def _now(self) -> float:
        return float(self._clock())

    async def _sleep(self, seconds: float) -> None:
        if self._sleep_fn is not None:
            result = self._sleep_fn(seconds)
            if asyncio.iscoroutine(result):
                await result
            return
        await asyncio.sleep(seconds)
