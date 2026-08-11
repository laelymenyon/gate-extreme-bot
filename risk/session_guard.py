"""Session calendar enforcement for non-24/7 contracts.

PHASE 15.

23 of the 31 tradable pairs are synthetics — tokenized equities, indices, FX and
commodities — whose venues actually close. A stop-loss cannot execute while the venue is
closed, so a weekend or overnight gap jumps straight past a 0.125% stop and the loss is
bounded by *liquidation*, not by the stop: risk-per-trade does not hold across a gap. This
is the module that makes the configured universe tradeable without trusting that.

It answers two questions about a symbol at an instant:

* may we *enter*? — no inside ``no_entry_after_open_minutes`` of an open, no inside
  ``no_entry_before_close_minutes`` of a close, and never while the venue is closed.
* must we be *flat*? — yes inside ``flat_before_close_minutes`` of a close.

**The calendar is deliberately conservative.** Sessions are expressed as the *intersection*
of the DST regimes — the hours that are open in both summer and winter — so the guard can
only refuse entry earlier than the venue allows, never allow it later. Missing the first
hour of the US session is a missed opportunity; missing a close is a liquidation.

Only the four genuinely 24/7 crypto pairs (BTC, ETH, SOL, XRP — the only contracts whose
venues never halt, verified against the live contract list in Phase 1) are always open.
Everything else, including the gold tokens PAXG and XAUT which ``config.yaml`` groups under
metals, has sessions.

**Unknown is closed.** `session_guard.treat_unknown_session_as_closed` must be ``true``
(validation refuses otherwise), so a symbol with no entry in the tables below is a CLOSED
symbol. An unrecognised contract is a closed contract; the alternative is a calendar guess
that could claim a closed venue is open.

Both trading loops (``paper/loop.py`` and ``live/loop.py``) enforce the verdict identically:
a veto before the signal chain blocks entries, and a force-flatten opens once the close
window arrives. The backtester does not: it replays recorded history, where the bars
themselves are the truth about what was tradable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "SessionParams",
    "SessionVerdict",
    "SessionWindow",
    "session_verdict",
    "trading_windows",
]

#: The only contracts whose venue never halts (Phase 1, verified against /contracts).
_CRYPTO_24_7 = frozenset({"BTC_USDT", "ETH_USDT", "SOL_USDT", "XRP_USDT"})

_US_EQUITIES = frozenset({
    "AAPLX_USDT", "AMZNX_USDT", "GOOGLX_USDT", "METAX_USDT", "MSFT_USDT",
    "NVDAX_USDT", "SPCX_USDT", "TSLAX_USDT", "TSM_USDT",
})
_US_INDICES = frozenset({"SPX500_USDT", "NAS100_USDT", "US30_USDT"})
_UK_INDEX = frozenset({"UK100_USDT"})
_HK_INDEX = frozenset({"HK50_USDT"})
_JP_INDEX = frozenset({"JPN225_USDT"})
_FX = frozenset({"EURUSD_USDT", "GBPUSD_USDT"})
_METALS = frozenset({
    "XAU_USDT", "XAG_USDT", "XPT_USDT", "XPD_USDT", "XCU_USDT",
    "PAXG_USDT", "XAUT_USDT",          # config.yaml groups the gold tokens under metals
})
_ENERGY = frozenset({"CL_USDT", "BZ_USDT", "NG_USDT"})


@dataclass(frozen=True)
class SessionWindow:
    """One open interval. ``weekday`` is 0=Monday .. 6=Sunday, times are UTC minutes."""

    weekday: int
    start_minute: int
    end_minute: int

    def contains(self, weekday: int, minute: int) -> bool:
        return self.weekday == weekday and self.start_minute <= minute < self.end_minute


def _weekdays(start: int, end: int) -> tuple[SessionWindow, ...]:
    """Mon-Fri window repeated across the trading week."""
    return tuple(SessionWindow(day, start, end) for day in range(5))


# DST intersections (the hours open in *both* regimes), so the guard fails toward closed:
#   US equities/indices  EST 14:30-21:00, EDT 13:30-20:00  -> 14:30-20:00
#   UK100                GMT 08:00-16:30, BST 07:00-15:30  -> 08:00-15:30
#   JPN225               JST 00:00-06:00 (no DST)
#   HK50                 HKT 01:30-04:00 + 05:00-08:00 (no DST)
#   FX / metals / energy conservative 00:00-21:00 Mon-Fri (skips Sunday opens and
#   Friday late closes, which is the safe direction)
_SCHEDULES: dict[str, tuple[SessionWindow, ...]] = {}
for _symbol in _US_EQUITIES | _US_INDICES:
    _SCHEDULES[_symbol] = _weekdays(14 * 60 + 30, 20 * 60)
for _symbol in _UK_INDEX:
    _SCHEDULES[_symbol] = _weekdays(8 * 60, 15 * 60 + 30)
for _symbol in _JP_INDEX:
    _SCHEDULES[_symbol] = _weekdays(0, 6 * 60)
for _symbol in _HK_INDEX:
    _SCHEDULES[_symbol] = tuple(
        SessionWindow(day, start, end)
        for day in range(5) for start, end in ((1 * 60 + 30, 4 * 60), (5 * 60, 8 * 60))
    )
for _symbol in _FX | _METALS | _ENERGY:
    _SCHEDULES[_symbol] = _weekdays(0, 21 * 60)


def trading_windows(symbol: str) -> tuple[SessionWindow, ...] | None:
    """The venue's open windows for ``symbol``, or None when the calendar is unknown.

    ``None`` means "no schedule known" — which the guard treats as closed, not open.
    24/7 crypto returns a midnight-to-midnight window for every weekday; the verdict
    short-circuits those symbols before window arithmetic (see `session_verdict`), so the
    1440 boundary is descriptive, never a close.
    """
    if symbol in _CRYPTO_24_7:
        return tuple(
            SessionWindow(day, 0, 1440) for day in range(7)
        )
    return _SCHEDULES.get(symbol)


@dataclass(frozen=True)
class SessionParams:
    """The guard's thresholds from ``session_guard`` in ``config.yaml``."""

    enabled: bool = True
    flat_before_close_minutes: int = 30
    no_entry_before_close_minutes: int = 60
    no_entry_after_open_minutes: int = 15
    treat_unknown_session_as_closed: bool = True

    def __post_init__(self) -> None:
        if self.flat_before_close_minutes < 0 or self.no_entry_before_close_minutes < 0 \
                or self.no_entry_after_open_minutes < 0:
            raise ValueError("session guard windows must be non-negative")
        if self.flat_before_close_minutes > self.no_entry_before_close_minutes:
            raise ValueError(
                "flat_before_close_minutes must be <= no_entry_before_close_minutes: "
                "the flatten window may only start after entries are already blocked"
            )

    @classmethod
    def from_config(cls, cfg: Any) -> "SessionParams":
        base = cls()

        def guard(name: str, default: Any) -> Any:
            return cfg.get(f"session_guard.{name}", default)

        return cls(
            enabled=bool(guard("enabled", base.enabled)),
            flat_before_close_minutes=int(
                guard("flat_before_close_minutes", base.flat_before_close_minutes)
            ),
            no_entry_before_close_minutes=int(
                guard("no_entry_before_close_minutes", base.no_entry_before_close_minutes)
            ),
            no_entry_after_open_minutes=int(
                guard("no_entry_after_open_minutes", base.no_entry_after_open_minutes)
            ),
            treat_unknown_session_as_closed=bool(
                guard("treat_unknown_session_as_closed",
                      base.treat_unknown_session_as_closed)
            ),
        )


@dataclass(frozen=True)
class SessionVerdict:
    """What the calendar says about ``symbol`` at one instant.

    ``stage`` is the counter key the loops report under ("closed", "before_close",
    "after_open", "unknown", "disabled", or "ok").
    """

    symbol: str
    venue_open: bool
    entry_allowed: bool
    must_flat: bool
    stage: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        """Entry permitted and no flatten required."""
        return self.entry_allowed and not self.must_flat


def _utc_weekday_minute(now: float) -> tuple[int, int]:
    """(weekday 0=Mon..6=Sun, minutes since 00:00 UTC) for an epoch timestamp.

    1970-01-01 was a Thursday, hence the +3 offset.
    """
    stamp = int(now)
    return ((stamp // 86400) + 3) % 7, (stamp % 86400) // 60


def _format_minutes(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def session_verdict(symbol: str, now: float,
                    params: SessionParams | None = None) -> SessionVerdict:
    """The calendar's ruling for ``symbol`` at ``now`` (epoch seconds, UTC)."""
    params = params or SessionParams()
    if not params.enabled:
        return SessionVerdict(
            symbol, venue_open=True, entry_allowed=True, must_flat=False,
            stage="disabled", reason="session_guard is disabled",
        )

    # 24/7 venues never close, so the window machinery must not see them. In particular
    # the window table represents them as midnight-to-midnight, and treating that 1440
    # boundary as a real close would flatten BTC at 23:30 UTC every day.
    if symbol in _CRYPTO_24_7:
        return SessionVerdict(
            symbol, venue_open=True, entry_allowed=True, must_flat=False,
            stage="ok", reason=f"{symbol} trades 24/7",
        )

    windows = trading_windows(symbol)
    if windows is None:
        if params.treat_unknown_session_as_closed:
            return SessionVerdict(
                symbol, venue_open=False, entry_allowed=False, must_flat=False,
                stage="unknown",
                reason=f"no session calendar is known for {symbol}; treating it as closed",
            )
        return SessionVerdict(
            symbol, venue_open=True, entry_allowed=True, must_flat=False,
            stage="ok", reason=f"no session calendar for {symbol}; treated as open",
        )

    weekday, minute = _utc_weekday_minute(now)
    current = [w for w in windows if w.contains(weekday, minute)]
    if not current:
        return SessionVerdict(
            symbol, venue_open=False, entry_allowed=False, must_flat=False,
            stage="closed",
            reason=(f"{symbol} venue is closed (weekday {weekday}, "
                    f"{_format_minutes(minute)} UTC)"),
        )

    window = current[0]
    minutes_to_close = window.end_minute - minute
    if minutes_to_close <= params.flat_before_close_minutes:
        return SessionVerdict(
            symbol, venue_open=True, entry_allowed=False, must_flat=True,
            stage="before_close",
            reason=f"{symbol} closes in {minutes_to_close}m; flattening and blocking entries",
        )
    if minutes_to_close <= params.no_entry_before_close_minutes:
        return SessionVerdict(
            symbol, venue_open=True, entry_allowed=False, must_flat=False,
            stage="before_close",
            reason=f"{symbol} closes in {minutes_to_close}m; entries blocked",
        )

    minutes_since_open = minute - window.start_minute
    if minutes_since_open < params.no_entry_after_open_minutes:
        return SessionVerdict(
            symbol, venue_open=True, entry_allowed=False, must_flat=False,
            stage="after_open",
            reason=f"{symbol} opened {minutes_since_open}m ago; entries blocked",
        )

    return SessionVerdict(
        symbol, venue_open=True, entry_allowed=True, must_flat=False, stage="ok",
        reason=f"venue open ({_format_minutes(window.start_minute)}-"
               f"{_format_minutes(window.end_minute)} UTC)",
    )
