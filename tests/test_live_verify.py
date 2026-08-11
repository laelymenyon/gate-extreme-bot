"""PHASE 15 — the production connectivity/auth check and the explicit live-order verification.

`live/verify.py` holds the two commands an operator runs deliberately on the way to a real
order:

* `check_connectivity` — proves the credentials sign and the account is readable, placing
  nothing. The tests pin that it is read-only by construction and reports a rejected key
  honestly instead of claiming authentication.
* `verify_live_order` — the FINAL barrier: one real market order of minimum size, protected
  and then closed. The tests pin every refusal (shut gate, empty credentials, missing
  symbol, simulator client, wrong typed confirmation, non-flat account) and the one full
  round trip, which must record `mode="live"` and end flat.

Offline throughout, exactly like the rest of the suite: the fake client composes the Phase 8
simulator, and conftest.py's autouse guard fails any test that reaches the network.
"""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

import config as config_module
from config import Credentials
from database.models import TradeStore
from exchange.gate_client import GateAPIError
from execution.order_manager import SimulatedGateway
from live.verify import check_connectivity, verify_live_order
from tests.test_live import BTC, TIERS, FakeClient, live_cfg

ENTRY = 65_000.0
SEND = "BTC_USDT SEND"


@pytest.fixture
def store(tmp_path):
    return TradeStore(tmp_path / "trades.db")


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    """Same isolation test_live.py needs: verification latches kill-switches to SQLite."""
    real_load = config_module.load_config

    def scoped(*args, **kwargs):
        cfg = real_load(*args, **kwargs)
        cfg.raw["database"] = dict(cfg.raw["database"], path=str(tmp_path / "trades.db"))
        return cfg

    monkeypatch.setattr(config_module, "load_config", scoped)
    return tmp_path / "trades.db"


class ConnectivityClient(FakeClient):
    """FakeClient plus the one ticker read the connectivity check uses."""

    async def get_ticker(self, symbol):
        self.calls.append("get_ticker")
        return {"contract": symbol, "last": str(ENTRY), "mark_price": str(ENTRY),
                "volume_24h_quote": "1000000"}


_WRITE_CALLS = {
    "place_order", "cancel_order", "place_price_trigger_order", "cancel_price_order",
    "set_leverage", "countdown_cancel_all", "cancel_all_price_orders",
}


def run(coro):
    return asyncio.run(coro)


# --- connectivity: read-only, honest -----------------------------------------

def test_connectivity_refuses_without_credentials(monkeypatch):
    cfg = live_cfg(monkeypatch, mode="paper", creds=False)
    out: list[str] = []
    assert run(check_connectivity(cfg, print_fn=out.append)) == 2
    assert "GATE_API_KEY" in "\n".join(out)
    assert "never places an order" in "\n".join(out)


def test_connectivity_reads_only_and_reports_the_account(monkeypatch):
    cfg = live_cfg(monkeypatch)
    client = ConnectivityClient()
    out: list[str] = []
    code = run(check_connectivity(cfg, symbol="BTC_USDT", client=client,
                                  print_fn=out.append))
    assert code == 0
    text = "\n".join(out)
    assert "auth          : OK" in text
    assert "10000.00 USDT total" in text
    assert "0 open" in text or "0 price-triggered" in text
    assert "BTC_USDT" in text
    # Everything it touched was a read. The write-guard would refuse writes anyway,
    # but the check must not even attempt them.
    assert not (_WRITE_CALLS & set(client.calls)), client.calls
    assert "No order was placed" in text


def test_connectivity_reports_a_rejected_key_without_faking_auth(monkeypatch):
    cfg = live_cfg(monkeypatch)
    client = ConnectivityClient()

    async def bad_account():
        raise GateAPIError(401, "INVALID_KEY", "Invalid key provided", "/accounts")

    client.get_account = bad_account
    out: list[str] = []
    code = run(check_connectivity(cfg, symbol="BTC_USDT", client=client,
                                  print_fn=out.append))
    assert code == 1
    text = "\n".join(out)
    assert "auth          : FAILED" in text
    assert "INVALID_KEY" in text
    assert "check GATE_API_KEY" in text


# --- verification: every refusal, then the one round trip ---------------------

def test_verification_refuses_a_shut_gate(monkeypatch):
    cfg = live_cfg(monkeypatch, dry_run=True)
    assert not cfg.live_enabled
    out: list[str] = []
    code = run(verify_live_order(cfg, symbol="BTC_USDT", print_fn=out.append,
                                 input_fn=lambda p: SEND))
    assert code == 2
    assert "safety gate is CLOSED" in "\n".join(out)


def test_verification_refuses_empty_credentials(monkeypatch):
    cfg = dataclasses.replace(live_cfg(monkeypatch), credentials=Credentials("", ""))
    assert cfg.live_enabled and not cfg.credentials.present
    out: list[str] = []
    code = run(verify_live_order(cfg, symbol="BTC_USDT", print_fn=out.append,
                                 input_fn=lambda p: SEND))
    assert code == 2
    assert "GATE_API_KEY/GATE_API_SECRET are empty" in "\n".join(out)


def test_verification_requires_an_explicit_symbol(monkeypatch):
    cfg = live_cfg(monkeypatch)
    out: list[str] = []
    code = run(verify_live_order(cfg, symbol=None, print_fn=out.append,
                                 input_fn=lambda p: SEND))
    assert code == 2
    assert "--symbol" in "\n".join(out)


def test_verification_refuses_the_simulator_by_name(monkeypatch):
    """The one path that must never claim a live result from an in-process book."""
    cfg = live_cfg(monkeypatch)
    sim = SimulatedGateway(last_price=ENTRY)
    out: list[str] = []
    code = run(verify_live_order(cfg, symbol="BTC_USDT", client=sim,
                                 print_fn=out.append, input_fn=lambda p: SEND))
    assert code == 2
    assert "simulator" in "\n".join(out)


def test_verification_refuses_a_wrong_confirmation_without_an_order(monkeypatch):
    cfg = live_cfg(monkeypatch)
    client = FakeClient()
    out: list[str] = []
    code = run(verify_live_order(cfg, symbol="BTC_USDT", client=client,
                                 print_fn=out.append, input_fn=lambda p: "BTC_USDT NOPE"))
    assert code == 2
    assert "aborted" in "\n".join(out)
    assert "place_order" not in client.calls


def test_verification_refuses_a_non_flat_account(monkeypatch):
    cfg = live_cfg(monkeypatch)
    client = FakeClient()
    run(client.sim.place_order("BTC_USDT", 5, price=None, tif="ioc", text="t-hold"))
    out: list[str] = []
    code = run(verify_live_order(cfg, symbol="BTC_USDT", client=client,
                                 print_fn=out.append, input_fn=lambda p: SEND))
    assert code == 3
    assert "must start flat" in "\n".join(out)
    assert "place_order" not in client.calls


def test_verification_completes_a_full_round_trip(monkeypatch, store):
    """Entry -> fill -> verified stop -> close -> flat, all recorded mode=live."""
    cfg = live_cfg(monkeypatch)
    client = FakeClient()
    out: list[str] = []
    code = run(verify_live_order(cfg, symbol="BTC_USDT", store=store, client=client,
                                 print_fn=out.append, input_fn=lambda p: SEND))
    text = "\n".join(out)
    assert code == 0, text
    assert "LIVE ORDER VERIFICATION COMPLETE" in text
    assert "entry         : FILLED" in text
    assert "protection    : OK" in text
    assert "position confirmed flat" in text
    assert "place_order" in client.calls          # the real market entry
    assert "place_price_trigger_order" in client.calls   # the protective stop
    assert run(client.get_position("BTC_USDT"))["size"] == 0
    assert ("BTC_USDT", 100) in client.leverage_set, (
        "the order must rest on the configured leverage, set after the confirmation"
    )

    recorded = store.trades()
    assert recorded, "the witnessed round trip must be in the audit trail"
    assert {row.mode for row in recorded} == {"live"}
    assert recorded[-1].exit_reason == "verify"


def test_verification_sets_the_configured_leverage_only_after_the_confirmation(monkeypatch):
    """Nothing — not even a leverage write — may happen before the operator types SEND."""
    cfg = live_cfg(monkeypatch)
    client = FakeClient()
    out: list[str] = []
    code = run(verify_live_order(cfg, symbol="BTC_USDT", client=client,
                                 print_fn=out.append, input_fn=lambda p: "BTC_USDT NOPE"))
    assert code == 2
    assert client.leverage_set == [], "the leverage write must wait for the confirmation"
    assert "set_leverage" not in client.calls


def test_verification_refuses_when_the_leverage_cannot_be_set(monkeypatch):
    """An unsettable leverage means the margin basis of the order would be a guess."""
    cfg = live_cfg(monkeypatch)
    client = FakeClient(leverage_error=RuntimeError("margin mode is locked"))
    out: list[str] = []
    code = run(verify_live_order(cfg, symbol="BTC_USDT", client=client,
                                 print_fn=out.append, input_fn=lambda p: SEND))
    assert code == 2
    assert "leverage" in "\n".join(out)
    assert "place_order" not in client.calls


def test_verification_refuses_when_the_market_cannot_be_read(monkeypatch):
    """Confirming an order against an unreadable market would turn the barrier into a
    formality — the size, margin and stop below the prompt would all be guesses."""
    cfg = live_cfg(monkeypatch)
    client = FakeClient()

    async def no_ticker(symbol):
        raise GateAPIError(503, "UPSTREAM", "tickers down", "/tickers")

    client.get_ticker = no_ticker
    out: list[str] = []
    code = run(verify_live_order(cfg, symbol="BTC_USDT", client=client,
                                 print_fn=out.append, input_fn=lambda p: SEND))
    assert code == 3
    assert "could not be read" in "\n".join(out)
    assert "place_order" not in client.calls


def test_recovery_cancels_resting_stops_after_a_successful_retry_close(monkeypatch, store):
    """A failed close that the recovery path then completes must not leave a stale stop.

    The verified stop protected the position; once the retry close proves the account
    flat, a still-resting reduce-only stop could fire into a *later* position — the exact
    hazard the normal completion path cleans up. Recovery must clean up the same way.
    """
    cfg = live_cfg(monkeypatch)
    client = FakeClient()
    state = {"close_failures": 1}

    async def flaky_close(symbol, size, **kwargs):
        if kwargs.get("close"):
            if state["close_failures"] > 0:
                state["close_failures"] -= 1
                raise GateAPIError(429, "RATE_LIMIT", "busy", "/orders")
        return await client.sim.place_order(symbol, size, **kwargs)

    client.place_order = flaky_close
    out: list[str] = []
    code = run(verify_live_order(cfg, symbol="BTC_USDT", store=store, client=client,
                                 print_fn=out.append, input_fn=lambda p: SEND))
    text = "\n".join(out)
    assert code == 1, text
    assert "close         : FAILED" in text, text
    assert "recovery      : close submitted" in text, text
    assert run(client.get_position("BTC_USDT"))["size"] == 0
    assert not run(client.list_price_orders("BTC_USDT")), (
        "the stale stop must be cancelled once the retry close proved flat"
    )


def test_verification_recovers_when_an_exception_hits_after_the_entry(monkeypatch, store):
    """An exception after a submitted order must never escape as a traceback.

    The stop placement fails, and the emergency close fails too — the worst case. The
    command reports it, attempts the defensive close, leaves any resting stop in force,
    disarms the dead-man countdown (so nothing is cancelled behind the operator's back)
    and returns 1. The account state is stated, not hidden.
    """
    cfg = live_cfg(monkeypatch)
    client = FakeClient()

    async def exploding_stop(*args, **kwargs):
        raise GateAPIError(503, "UPSTREAM", "price orders down", "/price_orders")

    client.place_price_trigger_order = exploding_stop

    async def no_close(symbol, size, **kwargs):
        if kwargs.get("close"):
            raise GateAPIError(429, "RATE_LIMIT", "busy", "/orders")
        return await client.sim.place_order(symbol, size, **kwargs)

    client.place_order = no_close

    out: list[str] = []
    code = run(verify_live_order(cfg, symbol="BTC_USDT", store=store, client=client,
                                 print_fn=out.append, input_fn=lambda p: SEND))
    text = "\n".join(out)
    assert code == 1, text
    assert "recovery" in text, text
    assert "defensive close FAILED" in text, text
    assert "verification FAILED" in text, text
    assert run(client.get_position("BTC_USDT"))["size"] != 0, (
        "the still-open position is reported, never claimed flat"
    )
    assert client.sim.countdown_seconds == 0, (
        "the countdown must be disarmed so nothing is cancelled behind the operator"
    )
    assert store.trades() == [], (
        "an unverified round trip is not recorded as a completed trade"
    )


def test_verification_reports_an_unverifiable_stop_honestly(monkeypatch, store):
    """A stop that cannot be verified is not claimed; the position is still closed flat."""
    cfg = live_cfg(monkeypatch)
    client = FakeClient()

    async def ghost(*args, **kwargs):
        client.calls.append("place_price_trigger_order")
        return {"id": "ghost", "status": "open"}

    client.place_price_trigger_order = ghost
    out: list[str] = []
    code = run(verify_live_order(cfg, symbol="BTC_USDT", store=store, client=client,
                                 print_fn=out.append, input_fn=lambda p: SEND))
    text = "\n".join(out)
    assert code == 1, text
    assert "protection    : FAILED" in text
    assert "VERIFICATION INCOMPLETE" in text
    assert "position confirmed flat" in text      # it still ended flat — honestly reported
    assert run(client.get_position("BTC_USDT"))["size"] == 0
    assert store.trades(), "the failed round trip is still recorded for the audit trail"
