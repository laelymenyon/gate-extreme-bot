"""Phase 2 tests: REST client signing, write-guard, retry policy, idempotency.

No network. A fake aiohttp session records requests and replays scripted responses,
so every path is exercised deterministically. The only live-verified constants
(fees, leverage limits, tier shapes) are asserted against payloads captured from the
real API during Phase 1.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import aiohttp
import pytest

import config as config_module
from config import load_config
from exchange.gate_client import (
    PREFIX,
    Contract,
    GateAPIError,
    GateFuturesClient,
    RateLimiter,
    RiskTier,
    WriteBlocked,
    select_tier,
)

# pytest.ini sets asyncio_mode=auto, so async tests need no per-test marker and
# the sync tests in this file stay sync.


# --- fake transport --------------------------------------------------------

class FakeResponse:
    def __init__(self, status: int, payload):
        self.status = status
        self._payload = payload

    async def text(self):
        return self._payload if isinstance(self._payload, str) else json.dumps(self._payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Records every request and returns scripted responses in order."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    def request(self, method, url, *, data=None, headers=None):
        self.calls.append({
            "method": method, "url": url, "data": data, "headers": dict(headers or {}),
        })
        if not self._script:
            raise AssertionError(f"unscripted request: {method} {url}")
        nxt = self._script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        status, payload = nxt
        return FakeResponse(status, payload)

    async def close(self):
        pass


def make_config(monkeypatch, *, live: bool, creds: bool = True):
    monkeypatch.setattr(config_module, "ENV_PATH", config_module.ROOT / ".env.absent")
    monkeypatch.setenv("DRY_RUN", "false" if live else "true")
    if creds:
        monkeypatch.setenv("GATE_API_KEY", "testkey")
        monkeypatch.setenv("GATE_API_SECRET", "testsecret")
    else:
        monkeypatch.delenv("GATE_API_KEY", raising=False)
        monkeypatch.delenv("GATE_API_SECRET", raising=False)
    if live:
        return load_config(run_mode="live", confirm_live=True)
    return load_config(run_mode="paper", confirm_live=False)


def client_for(cfg, script):
    session = FakeSession(script)
    return GateFuturesClient(cfg, session=session), session


# --- the write-guard: the module's most important property -----------------

@pytest.mark.parametrize("call,kwargs", [
    ("set_leverage", {"symbol": "BTC_USDT", "leverage": 100}),
    ("place_order", {"symbol": "BTC_USDT", "size": 1, "price": "60000", "text": "t-abc"}),
    ("cancel_order", {"order_id": 1}),
    ("cancel_price_order", {"order_id": 1}),
    ("cancel_all_price_orders", {"symbol": "BTC_USDT"}),
    ("countdown_cancel_all", {"timeout_seconds": 30}),
])
async def test_writes_blocked_when_gate_closed(monkeypatch, call, kwargs):
    cfg = make_config(monkeypatch, live=False)
    client, session = client_for(cfg, [])
    with pytest.raises(WriteBlocked):
        await getattr(client, call)(**kwargs)
    assert session.calls == [], "a blocked write must not touch the network"
    assert client.stats.writes_blocked == 1


async def test_price_trigger_order_blocked_when_gate_closed(monkeypatch):
    cfg = make_config(monkeypatch, live=False)
    client, session = client_for(cfg, [])
    with pytest.raises(WriteBlocked):
        await client.place_price_trigger_order(
            "BTC_USDT", trigger_price="59000", rule=2, text="t-sl1")
    assert session.calls == []


async def test_reads_allowed_when_gate_closed(monkeypatch):
    """Dry-run must still be able to observe the market and the account."""
    cfg = make_config(monkeypatch, live=False)
    client, session = client_for(cfg, [(200, [{"last": "65000"}]), (200, {"total": "100"})])
    assert await client.get_ticker("BTC_USDT") == {"last": "65000"}
    assert await client.get_account() == {"total": "100"}
    assert len(session.calls) == 2


async def test_writes_allowed_when_gate_open(monkeypatch):
    cfg = make_config(monkeypatch, live=True)
    assert cfg.live_enabled is True
    client, session = client_for(cfg, [(200, {"id": 42, "status": "open"})])
    result = await client.place_order("BTC_USDT", 1, price="60000", text="t-e1")
    assert result["id"] == 42
    assert session.calls[0]["method"] == "POST"


# --- signing ---------------------------------------------------------------

async def test_signature_matches_reference_algorithm(monkeypatch):
    cfg = make_config(monkeypatch, live=False)
    client, session = client_for(cfg, [(200, {"total": "1"})])
    await client.get_account()

    sent = session.calls[0]
    headers = sent["headers"]
    assert headers["KEY"] == "testkey"

    expected_body_hash = hashlib.sha512(b"").hexdigest()
    payload = f"GET\n{PREFIX}/futures/usdt/accounts\n\n{expected_body_hash}\n{headers['Timestamp']}"
    expected = hmac.new(b"testsecret", payload.encode(), hashlib.sha512).hexdigest()
    assert headers["SIGN"] == expected


async def test_signed_path_includes_api_v4_prefix(monkeypatch):
    """Phase 1 verified the signature covers '/api/v4<path>', not the bare path."""
    cfg = make_config(monkeypatch, live=False)
    client, session = client_for(cfg, [(200, {"total": "1"})])
    await client.get_account()
    headers = session.calls[0]["headers"]

    body_hash = hashlib.sha512(b"").hexdigest()
    without_prefix = f"GET\n/futures/usdt/accounts\n\n{body_hash}\n{headers['Timestamp']}"
    wrong = hmac.new(b"testsecret", without_prefix.encode(), hashlib.sha512).hexdigest()
    assert headers["SIGN"] != wrong


async def test_body_is_hashed_into_signature(monkeypatch):
    cfg = make_config(monkeypatch, live=True)
    client, session = client_for(cfg, [(200, {"id": 1})])
    await client.place_order("BTC_USDT", -2, price="60000", text="t-x1")

    sent = session.calls[0]
    body = sent["data"]
    headers = sent["headers"]
    payload = (f"POST\n{PREFIX}/futures/usdt/orders\n\n"
               f"{hashlib.sha512(body.encode()).hexdigest()}\n{headers['Timestamp']}")
    assert headers["SIGN"] == hmac.new(b"testsecret", payload.encode(), hashlib.sha512).hexdigest()
    assert json.loads(body)["size"] == -2  # negative size == short


async def test_public_endpoint_query_is_sorted_and_unsigned(monkeypatch):
    cfg = make_config(monkeypatch, live=False)
    client, session = client_for(cfg, [(200, [])])
    await client.get_candlesticks("BTC_USDT", interval="5m", limit=10)

    call = session.calls[0]
    assert call["url"].split("?", 1)[1] == "contract=BTC_USDT&interval=5m&limit=10"  # alphabetical
    assert "SIGN" not in call["headers"], "public market data must not be signed"


async def test_authed_query_string_participates_in_signature(monkeypatch):
    cfg = make_config(monkeypatch, live=False)
    client, session = client_for(cfg, [(200, [])])
    await client.list_open_orders("BTC_USDT")

    call = session.calls[0]
    query = call["url"].split("?", 1)[1]
    assert query == "contract=BTC_USDT&status=open"  # alphabetical

    headers = call["headers"]
    body_hash = hashlib.sha512(b"").hexdigest()
    payload = (f"GET\n{PREFIX}/futures/usdt/orders\n{query}\n"
               f"{body_hash}\n{headers['Timestamp']}")
    assert headers["SIGN"] == hmac.new(
        b"testsecret", payload.encode(), hashlib.sha512).hexdigest()

    # A signature computed without the query must not match.
    without_query = f"GET\n{PREFIX}/futures/usdt/orders\n\n{body_hash}\n{headers['Timestamp']}"
    assert headers["SIGN"] != hmac.new(
        b"testsecret", without_query.encode(), hashlib.sha512).hexdigest()


async def test_auth_without_credentials_fails_before_network(monkeypatch):
    cfg = make_config(monkeypatch, live=False, creds=False)
    client, session = client_for(cfg, [])
    with pytest.raises(GateAPIError, match="NO_CREDENTIALS"):
        await client.get_account()
    assert session.calls == []


async def test_boolean_query_params_encode_lowercase(monkeypatch):
    cfg = make_config(monkeypatch, live=False)
    client, session = client_for(cfg, [(200, [])])
    await client.list_positions(holding=True)
    assert "holding=true" in session.calls[0]["url"]


# --- retry / backoff -------------------------------------------------------

async def test_retries_on_500_then_succeeds(monkeypatch):
    cfg = make_config(monkeypatch, live=False)
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    client, session = client_for(cfg, [
        (500, {"label": "SERVER_ERROR", "message": "boom"}),
        (200, [{"last": "1"}]),
    ])
    assert await client.get_ticker("BTC_USDT") == {"last": "1"}
    assert client.stats.retries == 1


async def test_retries_on_429(monkeypatch):
    cfg = make_config(monkeypatch, live=False)
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    client, _ = client_for(cfg, [
        (429, {"label": "TOO_MANY_REQUESTS", "message": "slow down"}),
        (200, [{"last": "2"}]),
    ])
    await client.get_ticker("BTC_USDT")
    assert client.stats.rate_limited == 1


async def test_terminal_error_is_not_retried(monkeypatch):
    cfg = make_config(monkeypatch, live=False)
    client, session = client_for(cfg, [(401, {"label": "INVALID_KEY", "message": "bad key"})])
    with pytest.raises(GateAPIError) as exc:
        await client.get_account()
    assert exc.value.label == "INVALID_KEY"
    assert exc.value.retryable is False
    assert len(session.calls) == 1


async def test_network_error_on_read_is_retried(monkeypatch):
    cfg = make_config(monkeypatch, live=False)
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    client, _ = client_for(cfg, [
        aiohttp.ClientConnectionError("reset"),
        (200, [{"last": "3"}]),
    ])
    assert await client.get_ticker("BTC_USDT") == {"last": "3"}


async def test_retry_is_resigned_with_fresh_timestamp(monkeypatch):
    """A stale timestamp would be rejected as REQUEST_EXPIRED."""
    cfg = make_config(monkeypatch, live=False)
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    client, session = client_for(cfg, [
        (503, {"label": "UNAVAILABLE", "message": "x"}),
        (200, {"total": "1"}),
    ])
    await client.get_account()
    assert len(session.calls) == 2
    assert "SIGN" in session.calls[0]["headers"]
    assert "SIGN" in session.calls[1]["headers"]


# --- idempotency -----------------------------------------------------------

@pytest.mark.parametrize("bad", ["abc", "t-", "no-prefix", "t-" + "x" * 29, "t-bad chars!"])
async def test_order_text_key_must_be_valid(monkeypatch, bad):
    cfg = make_config(monkeypatch, live=True)
    client, session = client_for(cfg, [])
    with pytest.raises(ValueError, match="t-"):
        await client.place_order("BTC_USDT", 1, price="60000", text=bad)
    assert session.calls == []


async def test_order_carries_idempotency_key(monkeypatch):
    cfg = make_config(monkeypatch, live=True)
    client, session = client_for(cfg, [(200, {"id": 7})])
    await client.place_order("BTC_USDT", 1, price="60000", text="t-entry.1")
    assert json.loads(session.calls[0]["data"])["text"] == "t-entry.1"


async def test_market_order_requires_ioc(monkeypatch):
    cfg = make_config(monkeypatch, live=True)
    client, _ = client_for(cfg, [])
    with pytest.raises(ValueError, match="ioc"):
        await client.place_order("BTC_USDT", 1, price=None, tif="gtc", text="t-m1")


async def test_market_order_sends_price_zero(monkeypatch):
    cfg = make_config(monkeypatch, live=True)
    client, session = client_for(cfg, [(200, {"id": 8})])
    await client.place_order("BTC_USDT", 0, price=None, tif="ioc",
                             close=True, reduce_only=True, text="t-flat")
    body = json.loads(session.calls[0]["data"])
    assert body["price"] == "0" and body["close"] is True and body["reduce_only"] is True


async def test_size_zero_requires_close(monkeypatch):
    cfg = make_config(monkeypatch, live=True)
    client, _ = client_for(cfg, [])
    with pytest.raises(ValueError, match="close=True"):
        await client.place_order("BTC_USDT", 0, price="60000", text="t-z")


# --- stop-loss trigger semantics -------------------------------------------

async def test_stop_defaults_to_mark_price_and_reduce_only(monkeypatch):
    """Liquidation uses mark price, so the SL must trigger off mark price too."""
    cfg = make_config(monkeypatch, live=True)
    client, session = client_for(cfg, [(200, {"id": 9})])
    await client.place_price_trigger_order(
        "BTC_USDT", trigger_price="59000", rule=2, size=0, close=True, text="t-sl")
    body = json.loads(session.calls[0]["data"])
    assert body["trigger"]["price_type"] == 1        # mark
    assert body["trigger"]["rule"] == 2              # trigger when price <= 59000
    assert body["trigger"]["strategy_type"] == 0
    assert body["initial"]["reduce_only"] is True
    assert body["initial"]["price"] == "0"           # market exit: a stop must fill


@pytest.mark.parametrize("rule", [0, 3, -1])
async def test_invalid_trigger_rule_rejected(monkeypatch, rule):
    cfg = make_config(monkeypatch, live=True)
    client, _ = client_for(cfg, [])
    with pytest.raises(ValueError, match="rule"):
        await client.place_price_trigger_order(
            "BTC_USDT", trigger_price="1", rule=rule, text="t-a")


async def test_cross_margin_leverage_rejected(monkeypatch):
    """leverage=0 means cross margin on Gate.io; this bot is isolated-only."""
    cfg = make_config(monkeypatch, live=True)
    client, _ = client_for(cfg, [])
    with pytest.raises(ValueError, match="cross margin"):
        await client.set_leverage("BTC_USDT", 0)


# --- contract / tier maths against live-verified payloads ------------------

BTC_RAW = {
    "name": "BTC_USDT", "leverage_max": "200", "leverage_min": "1",
    "maintenance_rate": "0.003", "quanto_multiplier": "0.0001",
    "order_size_min": 1, "order_size_max": 12000000,
    "order_price_round": "0.1", "mark_price_round": "0.01",
    "taker_fee_rate": "0.00075", "maker_fee_rate": "-0.0001",
    "risk_limit_base": "500000", "in_delisting": False, "status": "trading",
}

BTC_TIERS_RAW = [
    {"tier": 1, "risk_limit": "500000", "initial_rate": "0.005",
     "maintenance_rate": "0.003", "leverage_max": "200", "deduction": "0"},
    {"tier": 2, "risk_limit": "1000000", "initial_rate": "0.006666",
     "maintenance_rate": "0.0035", "leverage_max": "150.01", "deduction": "250"},
    {"tier": 3, "risk_limit": "1500000", "initial_rate": "0.008",
     "maintenance_rate": "0.004", "leverage_max": "125", "deduction": "750"},
    {"tier": 5, "risk_limit": "3000000", "initial_rate": "0.01",
     "maintenance_rate": "0.005", "leverage_max": "100", "deduction": "2500"},
]


def test_contract_parses_live_payload():
    c = Contract.from_api(BTC_RAW)
    assert c.tradable
    assert c.leverage_max == 200 and c.maintenance_rate == 0.003
    assert c.taker_fee_rate == 0.00075
    assert c.maker_fee_rate == -0.0001  # a rebate, not a cost


def test_contract_size_conversion_uses_quanto_multiplier():
    c = Contract.from_api(BTC_RAW)
    assert c.contracts_for_coin_amount(0.05) == 500     # 0.05 BTC / 0.0001
    assert c.coin_amount(500) == pytest.approx(0.05)
    assert c.notional(500, 65000) == pytest.approx(3250)
    assert c.contracts_for_coin_amount(0.00019) == 1    # rounds down, never up


def test_short_size_is_negative_but_notional_is_positive():
    c = Contract.from_api(BTC_RAW)
    assert c.notional(-500, 65000) == pytest.approx(3250)


def test_tier_selection_rises_with_notional():
    tiers = [RiskTier.from_api(t) for t in BTC_TIERS_RAW]
    assert select_tier(tiers, 100_000).maintenance_rate == 0.003
    assert select_tier(tiers, 500_000).maintenance_rate == 0.003     # inclusive bound
    assert select_tier(tiers, 500_001).maintenance_rate == 0.0035
    assert select_tier(tiers, 1_200_000).maintenance_rate == 0.004
    assert select_tier(tiers, 99_000_000).maintenance_rate == 0.005  # clamps to last


def test_flat_maintenance_rate_understates_risk_at_size():
    """Why the guard must use tiers: the contract field is only tier 1."""
    contract = Contract.from_api(BTC_RAW)
    tiers = [RiskTier.from_api(t) for t in BTC_TIERS_RAW]
    at_size = select_tier(tiers, 2_000_000)
    assert at_size.maintenance_rate > contract.maintenance_rate


def test_tier_selection_requires_tiers():
    with pytest.raises(ValueError, match="no risk tiers"):
        select_tier([], 1000)


# --- rate limiter ----------------------------------------------------------

async def test_rate_limiter_allows_burst_then_throttles(monkeypatch):
    limiter = RateLimiter(rate_per_second=100, burst=3)
    for _ in range(3):
        await limiter.acquire()   # burst capacity, no sleep needed

    slept = []

    async def record(delay):
        slept.append(delay)

    monkeypatch.setattr("asyncio.sleep", record)
    await limiter.acquire()
    assert slept, "the 4th call must wait for a token"


def test_rate_limiter_rejects_nonpositive_rate():
    with pytest.raises(ValueError):
        RateLimiter(0)


# --- caching ---------------------------------------------------------------

async def test_contract_and_tier_lookups_are_cached(monkeypatch):
    cfg = make_config(monkeypatch, live=False)
    client, session = client_for(cfg, [(200, BTC_RAW), (200, BTC_TIERS_RAW)])
    assert (await client.get_contract("BTC_USDT")).name == "BTC_USDT"
    assert (await client.get_contract("BTC_USDT")).name == "BTC_USDT"
    assert len(await client.get_risk_tiers("BTC_USDT")) == 4
    assert len(await client.get_risk_tiers("BTC_USDT")) == 4
    assert len(session.calls) == 2, "second lookups must be served from cache"


async def test_candlestick_limit_is_bounded(monkeypatch):
    cfg = make_config(monkeypatch, live=False)
    client, _ = client_for(cfg, [])
    with pytest.raises(ValueError, match="2000"):
        await client.get_candlesticks("BTC_USDT", limit=2001)


async def test_mark_price_candles_use_prefix(monkeypatch):
    cfg = make_config(monkeypatch, live=False)
    client, session = client_for(cfg, [(200, [])])
    await client.get_candlesticks("BTC_USDT", price_type="mark")
    assert "contract=mark_BTC_USDT" in session.calls[0]["url"]


async def _no_sleep(_delay):
    return None
