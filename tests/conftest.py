"""PHASE 12 — suite-wide guarantees that hold for every test in this repo.

Every phase from 2 onward has claimed "no network" in its notes. Until now that was a
convention: the tests inject fake sessions and fake sockets, and nothing checked that they
all did. A single forgotten fake — a new test that builds a real
:class:`aiohttp.ClientSession`, or a helper that resolves a hostname — would reach the
public internet from a suite whose entire safety argument is that it cannot.

So the claim is enforced here instead of repeated in docstrings. Any attempt to open an
outbound connection or resolve a name fails the test that made it, loudly, naming the
address. Loopback and AF_UNIX are left alone: asyncio's own self-pipe uses them, and
blocking those would break the event loop rather than any exchange call.

This is a *test-suite* guard. It does not touch the production code paths, which have their
own independent barrier — the write-guard in ``exchange/gate_client.py`` and the three-switch
gate in ``config.py``.
"""

from __future__ import annotations

import socket

import pytest

_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost", ""})


class NetworkUseInTests(AssertionError):
    """Raised when a test tries to reach the network.

    Deliberately an ``AssertionError``: this is a failed assertion about the suite, not a
    connection problem to be caught and retried by whatever code triggered it.
    """


def _is_loopback(address: object) -> bool:
    """True for the addresses asyncio needs internally; False for anything routable."""
    if isinstance(address, (str, bytes)):        # AF_UNIX path, or an abstract socket
        return True
    if isinstance(address, tuple) and address:
        host = address[0]
        if isinstance(host, bytes):
            host = host.decode("utf-8", "replace")
        return str(host) in _LOOPBACK
    return False


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that opens an outbound connection or resolves a hostname.

    Autouse, so it covers tests written after this one without their authors opting in —
    which is the only version of this guard that is worth having.
    """

    def blocked(what: str, target: object):
        raise NetworkUseInTests(
            f"this test tried to {what} {target!r}. The suite runs entirely offline: "
            "exchange interactions go through a fake session, a scripted socket, or "
            "SimulatedGateway. No test may depend on a real API, live market data, or DNS."
        )

    real_connect = socket.socket.connect

    def guarded_connect(self, address, *args, **kwargs):
        if not _is_loopback(address):
            blocked("connect to", address)
        return real_connect(self, address, *args, **kwargs)

    real_connect_ex = socket.socket.connect_ex

    def guarded_connect_ex(self, address, *args, **kwargs):
        if not _is_loopback(address):
            blocked("connect to", address)
        return real_connect_ex(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(
        socket, "create_connection",
        lambda address, *a, **k: blocked("connect to", address),
    )

    # Name resolution is blocked separately: a leaked hostname is a leaked intention even
    # when the connection that would have followed never happens. Loopback still resolves,
    # because the event loop resolves it while setting itself up.
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex"):
        real = getattr(socket, name)

        def guarded_resolve(host, *args, _real=real, **kwargs):
            if str(host) not in _LOOPBACK:
                blocked("resolve", host)
            return _real(host, *args, **kwargs)

        monkeypatch.setattr(socket, name, guarded_resolve)
