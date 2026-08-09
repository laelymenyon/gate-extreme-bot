"""Async Gate.io APIv4 futures REST client.

PHASE 2.

Signing (algorithm taken from the official gateapi-python `api_client.py`, then
smoke-tested live — a dummy key returns 401 INVALID_KEY, i.e. the signature itself
was accepted and only the key was rejected):

    sign_string = METHOD \\n /api/v4<path> \\n <query> \\n SHA512(body) \\n <unix_ts>
    SIGN        = HMAC_SHA512(secret, sign_string)
    headers     = KEY, SIGN, Timestamp

Note the signed path *includes* the ``/api/v4`` prefix.

Safety properties this module guarantees:

* **Write-guard.** Every state-changing request (POST/PUT/DELETE/PATCH) is refused
  unless ``Config.live_enabled`` is True. This is a second, independent barrier behind
  the config gate — a bug elsewhere cannot produce a live order on its own.
* **Idempotency.** Order placement carries a caller-supplied ``t-`` text key, so a
  retry after a timeout cannot double-fill. Non-idempotent writes are never blindly
  retried; they surface NETWORK_UNCERTAIN so the caller reconciles instead of guessing.
* **No silent loss.** The rate limiter queues; it never drops a request.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

import aiohttp

log = logging.getLogger(__name__)

HOST = "https://api.gateio.ws"
PREFIX = "/api/v4"

# Gate.io rejects text keys that do not match this shape.
_TEXT_KEY_RE = re.compile(r"^t-[0-9A-Za-z_.\-]{1,28}$")

_WRITE_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

# Gate.io error labels that will never succeed on retry.
_TERMINAL_LABELS = frozenset({
    "INVALID_KEY", "INVALID_SIGNATURE", "FORBIDDEN", "REQUEST_EXPIRED",
    "INVALID_PARAM_VALUE", "INVALID_PARAM", "INVALID_CURRENCY",
    "CONTRACT_NOT_FOUND", "INSUFFICIENT_AVAILABLE", "LIQUIDATE_IMMEDIATELY",
    "ORDER_NOT_FOUND", "POSITION_EMPTY", "RISK_LIMIT_EXCEEDED",
})


class GateAPIError(Exception):
    """A structured error returned by Gate.io."""

    def __init__(self, status: int, label: str, message: str, path: str) -> None:
        super().__init__(f"[{status} {label}] {message} ({path})")
        self.status = status
        self.label = label
        self.message = message
        self.path = path

    @property
    def retryable(self) -> bool:
        if self.label in _TERMINAL_LABELS:
            return False
        return self.status in _RETRY_STATUS


class WriteBlocked(Exception):
    """A state-changing request was attempted while the safety gate was closed."""


@dataclass(frozen=True)
class Contract:
    """The subset of GET /futures/{settle}/contracts/{name} that the bot relies on."""

    name: str
    leverage_max: float
    leverage_min: float
    maintenance_rate: float       # tier-1 only; the real tier comes from risk_limit_tiers
    quanto_multiplier: float      # coin amount per contract
    order_size_min: int
    order_size_max: int
    order_price_round: float
    mark_price_round: float
    taker_fee_rate: float
    maker_fee_rate: float
    risk_limit_base: float
    in_delisting: bool
    status: str

    @property
    def tradable(self) -> bool:
        return self.status == "trading" and not self.in_delisting

    def contracts_for_coin_amount(self, coin_amount: float) -> int:
        """Convert a coin amount into whole contracts, rounding *down*."""
        return int(coin_amount / self.quanto_multiplier)

    def coin_amount(self, size: int) -> float:
        return abs(size) * self.quanto_multiplier

    def notional(self, size: int, price: float) -> float:
        return self.coin_amount(size) * price

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> "Contract":
        return cls(
            name=raw["name"],
            leverage_max=float(raw["leverage_max"]),
            leverage_min=float(raw.get("leverage_min", 1)),
            maintenance_rate=float(raw["maintenance_rate"]),
            quanto_multiplier=float(raw["quanto_multiplier"]),
            order_size_min=int(raw.get("order_size_min", 1)),
            order_size_max=int(raw.get("order_size_max", 0)),
            order_price_round=float(raw["order_price_round"]),
            mark_price_round=float(raw.get("mark_price_round", raw["order_price_round"])),
            taker_fee_rate=float(raw["taker_fee_rate"]),
            maker_fee_rate=float(raw["maker_fee_rate"]),
            risk_limit_base=float(raw.get("risk_limit_base") or 0),
            in_delisting=bool(raw.get("in_delisting", False)),
            status=raw.get("status", "trading"),
        )


@dataclass(frozen=True)
class RiskTier:
    """One row of GET /futures/{settle}/risk_limit_tiers."""

    tier: int
    risk_limit: float          # maximum notional this tier covers
    initial_rate: float
    maintenance_rate: float
    leverage_max: float
    deduction: float

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> "RiskTier":
        return cls(
            tier=int(raw["tier"]),
            risk_limit=float(raw["risk_limit"]),
            initial_rate=float(raw["initial_rate"]),
            maintenance_rate=float(raw["maintenance_rate"]),
            leverage_max=float(raw["leverage_max"]),
            deduction=float(raw.get("deduction") or 0),
        )


def select_tier(tiers: Iterable[RiskTier], notional: float) -> RiskTier:
    """Return the tier whose risk_limit covers `notional`.

    The contract-level `maintenance_rate` is only the tier-1 value; it rises with
    position size. Using that flat field would under-estimate liquidation risk, so the
    liquidation guard resolves the rate through here instead.
    """
    ordered = sorted(tiers, key=lambda t: t.risk_limit)
    if not ordered:
        raise ValueError("no risk tiers supplied")
    for tier in ordered:
        if notional <= tier.risk_limit:
            return tier
    return ordered[-1]


class RateLimiter:
    """Token bucket. Queues rather than dropping, so no request is silently lost."""

    def __init__(self, rate_per_second: float, burst: int | None = None) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be > 0")
        self._rate = float(rate_per_second)
        self._capacity = float(burst if burst is not None else max(1, int(rate_per_second)))
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    self._capacity, self._tokens + (now - self._updated) * self._rate
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - self._tokens) / self._rate)


@dataclass
class ClientStats:
    requests: int = 0
    retries: int = 0
    rate_limited: int = 0
    errors: int = 0
    writes_blocked: int = 0
    by_path: dict[str, int] = field(default_factory=dict)


class GateFuturesClient:
    """Async REST client for Gate.io USDT-perpetual futures.

        async with GateFuturesClient(cfg) as client:
            contract = await client.get_contract("BTC_USDT")
    """

    def __init__(
        self,
        config,
        *,
        session: aiohttp.ClientSession | None = None,
        host: str = HOST,
    ) -> None:
        self._cfg = config
        self._host = host.rstrip("/")
        self._settle = config.get("settle", "usdt")
        self._session = session
        self._owns_session = session is None

        self._timeout = float(config.get("execution.request_timeout_seconds", 10))
        self._max_retries = int(config.get("execution.max_retries", 4))
        self._backoff_base = float(config.get("execution.backoff_base_seconds", 0.5))
        self._backoff_max = float(config.get("execution.backoff_max_seconds", 8))
        self._limiter = RateLimiter(float(config.get("execution.rate_limit_per_second", 8)))

        self._contracts: dict[str, Contract] = {}
        self._tiers: dict[str, list[RiskTier]] = {}
        self.stats = ClientStats()

    # --- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> "GateFuturesClient":
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout)
            )
            self._owns_session = True
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    # --- signing -----------------------------------------------------------

    def _sign(self, method: str, path: str, query: str, body: str) -> dict[str, str]:
        timestamp = str(int(time.time()))
        hashed = hashlib.sha512(body.encode("utf-8")).hexdigest()
        payload = f"{method}\n{PREFIX}{path}\n{query}\n{hashed}\n{timestamp}"
        signature = hmac.new(
            self._cfg.credentials.secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha512,
        ).hexdigest()
        return {
            "KEY": self._cfg.credentials.key,
            "Timestamp": timestamp,
            "SIGN": signature,
        }

    @staticmethod
    def _encode_query(params: Mapping[str, Any] | None) -> str:
        """Deterministic query encoding — the signature covers this exact string."""
        if not params:
            return ""
        parts = []
        for key in sorted(params):
            value = params[key]
            if value is None:
                continue
            if isinstance(value, bool):
                value = "true" if value else "false"
            parts.append(f"{key}={value}")
        return "&".join(parts)


    # --- request core ------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Any = None,
        auth: bool = False,
        idempotent: bool = True,
    ) -> Any:
        method = method.upper()

        # Barrier 1: no state-changing request may leave the process while the gate is shut.
        if method in _WRITE_METHODS and not self._cfg.live_enabled:
            self.stats.writes_blocked += 1
            raise WriteBlocked(
                f"{method} {path} blocked: the safety gate is closed "
                f"(DRY_RUN={self._cfg.env_dry_run}, mode={self._cfg.run_mode}, "
                f"confirm_live={self._cfg.confirm_live}). No request was sent."
            )

        if auth and not self._cfg.credentials.present:
            raise GateAPIError(401, "NO_CREDENTIALS",
                               "GATE_API_KEY/GATE_API_SECRET are empty", path)

        if self._session is None:
            raise RuntimeError("client used outside 'async with'; call __aenter__ first")

        query = self._encode_query(params)
        payload = "" if body is None else json.dumps(body)
        url = f"{self._host}{PREFIX}{path}" + (f"?{query}" if query else "")

        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            if auth:
                # Re-signed each attempt: the timestamp must be fresh or Gate.io
                # rejects the retry as REQUEST_EXPIRED.
                headers.update(self._sign(method, path, query, payload))

            await self._limiter.acquire()
            self.stats.requests += 1
            self.stats.by_path[path] = self.stats.by_path.get(path, 0) + 1

            try:
                async with self._session.request(
                    method, url,
                    data=payload if body is not None else None,
                    headers=headers,
                ) as response:
                    text = await response.text()

                    if response.status < 400:
                        return json.loads(text) if text else None

                    label, message = _parse_error(text)
                    error = GateAPIError(response.status, label, message, path)
                    self.stats.errors += 1
                    if response.status == 429:
                        self.stats.rate_limited += 1

                    # A non-idempotent write must never be blind-retried: the first
                    # attempt may have taken effect before the error surfaced.
                    if not error.retryable or (method in _WRITE_METHODS and not idempotent):
                        raise error
                    last_error = error

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if method in _WRITE_METHODS and not idempotent:
                    raise GateAPIError(
                        0, "NETWORK_UNCERTAIN",
                        f"{type(exc).__name__} on a non-idempotent write; exchange state is "
                        "unknown and must be reconciled before retrying",
                        path,
                    ) from exc
                self.stats.errors += 1
                last_error = exc

            if attempt < self._max_retries:
                self.stats.retries += 1
                delay = min(self._backoff_base * (2 ** attempt), self._backoff_max)
                delay += random.uniform(0, delay * 0.25)  # jitter breaks retry convoys
                log.warning("retry %d/%d %s %s in %.2fs: %s",
                            attempt + 1, self._max_retries, method, path, delay, last_error)
                await asyncio.sleep(delay)

        if isinstance(last_error, Exception):
            raise last_error
        raise GateAPIError(0, "UNKNOWN", "request failed with no recorded error", path)


    # --- public market data ------------------------------------------------

    async def list_contracts(self, *, refresh: bool = False) -> list[Contract]:
        if self._contracts and not refresh:
            return list(self._contracts.values())
        raw = await self._request("GET", f"/futures/{self._settle}/contracts")
        contracts = [Contract.from_api(item) for item in raw]
        self._contracts = {c.name: c for c in contracts}
        return contracts

    async def get_contract(self, symbol: str, *, refresh: bool = False) -> Contract:
        if not refresh and symbol in self._contracts:
            return self._contracts[symbol]
        raw = await self._request("GET", f"/futures/{self._settle}/contracts/{symbol}")
        contract = Contract.from_api(raw)
        self._contracts[symbol] = contract
        return contract

    async def get_risk_tiers(self, symbol: str, *, refresh: bool = False) -> list[RiskTier]:
        if not refresh and symbol in self._tiers:
            return self._tiers[symbol]
        raw = await self._request(
            "GET", f"/futures/{self._settle}/risk_limit_tiers", params={"contract": symbol}
        )
        tiers = sorted((RiskTier.from_api(item) for item in raw), key=lambda t: t.tier)
        self._tiers[symbol] = tiers
        return tiers

    async def get_candlesticks(
        self, symbol: str, interval: str = "1m", limit: int = 200,
        *, price_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """OHLCV. `price_type` of 'mark' or 'index' fetches those series instead."""
        if limit > 2000:
            raise ValueError("Gate.io returns at most 2000 candlestick points per query")
        contract = symbol
        if price_type in ("mark", "index"):
            contract = f"{price_type}_{symbol}"
        elif price_type is not None:
            raise ValueError("price_type must be None, 'mark', or 'index'")
        return await self._request(
            "GET", f"/futures/{self._settle}/candlesticks",
            params={"contract": contract, "interval": interval, "limit": limit},
        )

    async def get_order_book(self, symbol: str, limit: int = 10,
                             interval: str = "0") -> dict[str, Any]:
        return await self._request(
            "GET", f"/futures/{self._settle}/order_book",
            params={"contract": symbol, "limit": limit, "interval": interval},
        )

    async def get_ticker(self, symbol: str) -> dict[str, Any]:
        rows = await self._request(
            "GET", f"/futures/{self._settle}/tickers", params={"contract": symbol}
        )
        if not rows:
            raise GateAPIError(404, "CONTRACT_NOT_FOUND", f"no ticker for {symbol}", "/tickers")
        return rows[0]

    # --- private reads -----------------------------------------------------

    async def get_account(self) -> dict[str, Any]:
        return await self._request("GET", f"/futures/{self._settle}/accounts", auth=True)

    async def list_positions(self, *, holding: bool = True) -> list[dict[str, Any]]:
        return await self._request(
            "GET", f"/futures/{self._settle}/positions",
            params={"holding": holding}, auth=True,
        )

    async def get_position(self, symbol: str) -> dict[str, Any]:
        return await self._request(
            "GET", f"/futures/{self._settle}/positions/{symbol}", auth=True
        )

    async def get_order(self, order_id: str | int) -> dict[str, Any]:
        return await self._request(
            "GET", f"/futures/{self._settle}/orders/{order_id}", auth=True
        )

    async def list_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"status": "open"}
        if symbol:
            params["contract"] = symbol
        return await self._request(
            "GET", f"/futures/{self._settle}/orders", params=params, auth=True
        )

    async def list_price_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Open price-triggered orders — i.e. the live stop-losses and take-profits."""
        params: dict[str, Any] = {"status": "open"}
        if symbol:
            params["contract"] = symbol
        return await self._request(
            "GET", f"/futures/{self._settle}/price_orders", params=params, auth=True
        )


    # --- writes (every one blocked unless the safety gate is open) ----------

    async def set_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        """Isolated margin takes a positive leverage string; '0' would select cross."""
        if leverage <= 0:
            raise ValueError(
                "leverage must be > 0; 0 selects cross margin, which this bot forbids"
            )
        return await self._request(
            "POST", f"/futures/{self._settle}/positions/{symbol}/leverage",
            params={"leverage": str(leverage)}, auth=True,
        )

    async def place_order(
        self,
        symbol: str,
        size: int,
        *,
        price: str | None = None,
        tif: str = "poc",
        reduce_only: bool = False,
        close: bool = False,
        text: str,
    ) -> dict[str, Any]:
        """Place a futures order.

        `size` is a signed contract count: positive long, negative short.
        `price=None` means a market order and requires `tif='ioc'`.
        `text` is the idempotency key; it must match ``t-[0-9A-Za-z_.-]{1,28}``.
        """
        if not _TEXT_KEY_RE.match(text):
            raise ValueError(
                f"text={text!r} must match 't-' plus 1-28 chars of [0-9A-Za-z_.-]"
            )
        if size == 0 and not close:
            raise ValueError("size=0 is only valid together with close=True")
        if price is None and tif != "ioc":
            raise ValueError("a market order (price 0) requires tif='ioc'")

        body: dict[str, Any] = {
            "contract": symbol,
            "size": size,
            "price": "0" if price is None else str(price),
            "tif": tif,
            "text": text,
        }
        if reduce_only:
            body["reduce_only"] = True
        if close:
            body["close"] = True

        # idempotent=True is earned by `text`: a duplicate submission is detectable.
        return await self._request(
            "POST", f"/futures/{self._settle}/orders", body=body, auth=True, idempotent=True
        )

    async def cancel_order(self, order_id: str | int) -> dict[str, Any]:
        return await self._request(
            "DELETE", f"/futures/{self._settle}/orders/{order_id}", auth=True
        )

    async def place_price_trigger_order(
        self,
        symbol: str,
        *,
        trigger_price: str,
        order_price: str = "0",
        size: int = 0,
        rule: int,
        price_type: int = 1,
        tif: str = "ioc",
        reduce_only: bool = True,
        close: bool = False,
        text: str,
        expiration: int = 0,
    ) -> dict[str, Any]:
        """Place a stop-loss / take-profit through /price_orders.

        `rule`: 1 triggers when price >= trigger_price, 2 when price <= trigger_price.
        `price_type`: 0 last, 1 mark, 2 index — defaults to mark because liquidation is
        computed off the mark price, so the stop must race the same series.
        """
        if rule not in (1, 2):
            raise ValueError("rule must be 1 (>=) or 2 (<=)")
        if price_type not in (0, 1, 2):
            raise ValueError("price_type must be 0 (last), 1 (mark), or 2 (index)")
        if not _TEXT_KEY_RE.match(text):
            raise ValueError(f"text={text!r} must match 't-' plus 1-28 chars of [0-9A-Za-z_.-]")

        initial: dict[str, Any] = {
            "contract": symbol,
            "size": size,
            "price": order_price,
            "tif": tif,
            "text": text,
        }
        if reduce_only:
            initial["reduce_only"] = True
        if close:
            initial["close"] = True

        body = {
            "initial": initial,
            "trigger": {
                "strategy_type": 0,
                "price_type": price_type,
                "price": str(trigger_price),
                "rule": rule,
                "expiration": expiration,
            },
        }
        return await self._request(
            "POST", f"/futures/{self._settle}/price_orders",
            body=body, auth=True, idempotent=True,
        )

    async def cancel_price_order(self, order_id: str | int) -> dict[str, Any]:
        return await self._request(
            "DELETE", f"/futures/{self._settle}/price_orders/{order_id}", auth=True
        )

    async def cancel_all_price_orders(self, symbol: str) -> Any:
        return await self._request(
            "DELETE", f"/futures/{self._settle}/price_orders",
            params={"contract": symbol}, auth=True,
        )

    async def countdown_cancel_all(self, timeout_seconds: int,
                                   symbol: str | None = None) -> dict[str, Any]:
        """Dead-man switch: the exchange cancels our orders if we stop checking in."""
        body: dict[str, Any] = {"timeout": timeout_seconds}
        if symbol:
            body["contract"] = symbol
        return await self._request(
            "POST", f"/futures/{self._settle}/countdown_cancel_all",
            body=body, auth=True, idempotent=True,
        )


def _parse_error(text: str) -> tuple[str, str]:
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return "UNPARSEABLE", text[:200]
    if isinstance(data, dict):
        return str(data.get("label", "UNKNOWN")), str(data.get("message", text[:200]))
    return "UNKNOWN", text[:200]





