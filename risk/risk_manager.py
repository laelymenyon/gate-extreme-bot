"""Account-level circuit breakers.

PHASE 6.

The signal engine decides whether a setup is worth trading. This module decides whether
*trading at all* is still permitted, and it has the last word: every gate here is a veto,
and a veto outranks any score.

Four limits, each with different clearing semantics, because they mean different things:

===================== ========== ======================================================
breaker               threshold  clears
===================== ========== ======================================================
``daily_loss``        1%         at the next UTC day — "stop trading for the day"
``consecutive_losses``3 in a row  at the next UTC day; a win also resets the counter
``drawdown``          3%         **manual reset only** — a restart must not clear it
``max_open_positions``1          not a latch; it re-opens when the position closes
===================== ========== ======================================================

**The latches are persisted, not held in memory.** A process that trips the drawdown
limit and is restarted must come back tripped, otherwise the limit is a suggestion: the
one thing an operator is most tempted to do after a bad run is restart the bot.
:class:`SqliteRiskStore` writes them to the configured SQLite file in WAL mode; the
Phase 11 trade schema will live in the same database and adds its own tables.

**Equity is observed, not assumed.** ``can_trade`` takes the current equity, the number of
open positions, and the symbols already held, and every one of them is required. A missing
or unusable value is a refusal, never a default — the risk manager has no way to
distinguish "flat" from "we could not read the account", and at 100x those are not
interchangeable. Observing equity is itself a risk event: drawdown and daily loss are
evaluated on the reading, so an unrealised move can trip a breaker without a trade closing.

**No martingale, no averaging down, no revenge trading.** :meth:`RiskManager.risk_fraction`
returns the configured fraction whatever the recent history was — it takes no arguments, so
there is nothing to scale it by. Adding to a symbol already held is refused outright, and a
cooldown follows every closed trade, longer after a loss than after a win.

This module imports no ``exchange`` module: there is no network path and no order-placing
path here.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from numbers import Real
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

__all__ = [
    "Breaker",
    "RiskParams",
    "RiskState",
    "KillSwitch",
    "RiskDecision",
    "RiskStore",
    "MemoryRiskStore",
    "SqliteRiskStore",
    "RiskStateUnreadable",
    "RiskManager",
    "utc_day",
]

#: How far the clock may appear to step backwards before the state is treated as unusable.
#: NTP corrections of a few hundred milliseconds are ordinary; a jump of seconds means the
#: cooldown arithmetic cannot be trusted, and at 100x a cooldown skipped is a losing streak
#: traded straight through.
CLOCK_TOLERANCE_SECONDS = 1.0


class RiskStateUnreadable(Exception):
    """Persisted risk state could not be loaded or parsed.

    Raised rather than swallowed: a manager that cannot read its own kill switches has no
    idea whether it is halted, and the only safe answer to every question is no.
    """


class Breaker(str, Enum):
    """Reasons trading can be refused. ``str``-valued so they log and compare readably."""

    UNKNOWN_STATE = "unknown_state"
    DRAWDOWN = "drawdown"
    DAILY_LOSS = "daily_loss"
    CONSECUTIVE_LOSSES = "consecutive_losses"
    MANUAL = "manual"
    MAX_OPEN_POSITIONS = "max_open_positions"
    ALREADY_OPEN = "already_open"
    COOLDOWN = "cooldown"


#: Latching breakers, most severe first. Order is the reporting order: an account that has
#: hit its drawdown limit should say so rather than mention today's loss.
LATCHING: tuple[Breaker, ...] = (
    Breaker.UNKNOWN_STATE,
    Breaker.DRAWDOWN,
    Breaker.DAILY_LOSS,
    Breaker.CONSECUTIVE_LOSSES,
    Breaker.MANUAL,
)

#: Latches that survive a UTC day rollover and can only be cleared by a human.
MANUAL_RESET_ONLY: frozenset[Breaker] = frozenset(
    {Breaker.DRAWDOWN, Breaker.MANUAL, Breaker.UNKNOWN_STATE}
)


def utc_day(timestamp: float) -> str:
    """The UTC calendar day of a unix timestamp, as ``YYYY-MM-DD``.

    UTC rather than local time so that "today's loss" does not depend on where the process
    happens to run, and so a restart in another timezone cannot hand the bot a fresh daily
    allowance.
    """
    return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).date().isoformat()


def _finite(value: Any) -> bool:
    """True only for a real, finite number.

    ``bool`` is excluded even though it is an ``int``: ``True`` as an equity reading is a
    bug in the caller, and silently treating it as ``1.0`` would hide it.
    """
    if isinstance(value, bool) or not isinstance(value, Real):
        return False
    return math.isfinite(float(value))


@dataclass(frozen=True)
class RiskParams:
    """Limits from the ``risk`` and ``filters`` sections. Defaults mirror ``config.yaml``."""

    per_trade: float = 0.0025
    max_daily_loss: float = 0.01
    max_drawdown: float = 0.03
    max_consecutive_losses: int = 3
    max_open_positions: int = 1
    cooldown_after_loss_seconds: float = 300.0
    cooldown_after_win_seconds: float = 60.0

    def __post_init__(self) -> None:
        if not 0.0 < self.per_trade <= 0.05:
            raise ValueError(f"per_trade must be in (0, 0.05], got {self.per_trade!r}")
        if not 0.0 < self.max_daily_loss <= 0.20:
            raise ValueError(f"max_daily_loss must be in (0, 0.20], got {self.max_daily_loss!r}")
        if not 0.0 < self.max_drawdown <= 0.50:
            raise ValueError(f"max_drawdown must be in (0, 0.50], got {self.max_drawdown!r}")
        if self.max_consecutive_losses < 1:
            raise ValueError("max_consecutive_losses must be >= 1")
        if self.max_open_positions < 1:
            raise ValueError("max_open_positions must be >= 1")
        if self.cooldown_after_loss_seconds < 0 or self.cooldown_after_win_seconds < 0:
            raise ValueError("cooldowns must be >= 0 seconds")
        # The ladder must be a ladder: each limit reachable before the next. Duplicated
        # from config validation on purpose — this class is also built directly, by tests
        # and by the backtester, and a second barrier costs two comparisons.
        if self.per_trade > self.max_daily_loss:
            raise ValueError(
                f"per_trade ({self.per_trade}) exceeds max_daily_loss "
                f"({self.max_daily_loss}): a single stop-out would trip the daily breaker"
            )
        if self.max_daily_loss > self.max_drawdown:
            raise ValueError(
                f"max_daily_loss ({self.max_daily_loss}) exceeds max_drawdown "
                f"({self.max_drawdown}): the latch needing a manual reset would trip "
                "before the one that clears overnight"
            )

    @classmethod
    def from_config(cls, cfg: Any) -> "RiskParams":
        """Build from a ``Config``, refusing the strategies that blow up leveraged accounts.

        ``config.py`` already rejects these at load time. Checking again here is deliberate
        duplication: this class is also constructed directly in tests and by the backtester,
        and a second barrier costs one comparison.
        """
        for forbidden in ("risk.martingale", "risk.averaging_down"):
            if cfg.get(forbidden, False):
                raise ValueError(
                    f"{forbidden} is permanently disabled; position size never scales with "
                    "recent losses"
                )
        base = cls()
        return cls(
            per_trade=float(cfg.get("risk.per_trade", base.per_trade)),
            max_daily_loss=float(cfg.get("risk.max_daily_loss", base.max_daily_loss)),
            max_drawdown=float(cfg.get("risk.max_drawdown", base.max_drawdown)),
            max_consecutive_losses=int(
                cfg.get("risk.max_consecutive_losses", base.max_consecutive_losses)
            ),
            max_open_positions=int(
                cfg.get("risk.max_open_positions", base.max_open_positions)
            ),
            cooldown_after_loss_seconds=float(
                cfg.get("filters.cooldown_after_loss_seconds",
                        base.cooldown_after_loss_seconds)
            ),
            cooldown_after_win_seconds=float(
                cfg.get("filters.cooldown_after_win_seconds",
                        base.cooldown_after_win_seconds)
            ),
        )


@dataclass(frozen=True)
class RiskState:
    """Everything the breakers are computed from. Persisted in full."""

    equity: float
    peak_equity: float
    day: str
    day_start_equity: float
    consecutive_losses: int = 0
    last_trade_time: float = 0.0
    last_trade_won: bool | None = None
    trades_today: int = 0
    updated_at: float = 0.0

    @property
    def drawdown(self) -> float:
        """Fraction below the high-water mark. Never negative."""
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - self.equity) / self.peak_equity)

    @property
    def daily_loss(self) -> float:
        """Fraction below the equity this UTC day opened at. Never negative."""
        if self.day_start_equity <= 0:
            return 0.0
        return max(0.0, (self.day_start_equity - self.equity) / self.day_start_equity)


@dataclass(frozen=True)
class KillSwitch:
    """A tripped latch. ``manual_reset_required`` decides whether a new day clears it."""

    breaker: Breaker
    tripped_at: float
    reason: str
    manual_reset_required: bool


@dataclass(frozen=True)
class RiskDecision:
    """Whether a new position may be opened, and which gate said no."""

    allowed: bool
    breaker: Breaker | None = None
    reason: str = ""
    retry_after: float = 0.0     # seconds until a cooldown clears; 0 when not waiting

    def __bool__(self) -> bool:
        return self.allowed

    def summary(self) -> str:
        if self.allowed:
            return "trading permitted"
        return f"refused ({self.breaker.value if self.breaker else '?'}): {self.reason}"


# --- persistence -----------------------------------------------------------

class RiskStore(Protocol):
    """Where latches and state survive a restart."""

    def load(self) -> tuple[RiskState | None, dict[Breaker, KillSwitch]]: ...

    def save_state(self, state: RiskState) -> None: ...

    def trip(self, switch: KillSwitch) -> None: ...

    def clear(self, breaker: Breaker) -> None: ...


class MemoryRiskStore:
    """Non-persistent store for tests and backtests.

    Named for what it does not do. Using it in a live run means a restart clears every
    latch, which is exactly the failure :class:`SqliteRiskStore` exists to prevent.
    """

    def __init__(self) -> None:
        self._state: RiskState | None = None
        self._switches: dict[Breaker, KillSwitch] = {}

    def load(self) -> tuple[RiskState | None, dict[Breaker, KillSwitch]]:
        return self._state, dict(self._switches)

    def save_state(self, state: RiskState) -> None:
        self._state = state

    def trip(self, switch: KillSwitch) -> None:
        self._switches[switch.breaker] = switch

    def clear(self, breaker: Breaker) -> None:
        self._switches.pop(breaker, None)


class SqliteRiskStore:
    """SQLite-backed store. WAL by default, matching ``database.wal``.

    Creates only the two tables it needs and uses ``IF NOT EXISTS`` throughout, so the
    Phase 11 trade schema can share the same file without a migration step.
    """

    def __init__(self, path: str | Path, *, wal: bool = True) -> None:
        self.path = Path(path)
        if self.path.parent and str(self.path.parent) not in ("", "."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._wal = wal
        with self._connect() as conn:
            if wal:
                conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS risk_state (
                    id                 INTEGER PRIMARY KEY CHECK (id = 1),
                    equity             REAL    NOT NULL,
                    peak_equity        REAL    NOT NULL,
                    day                TEXT    NOT NULL,
                    day_start_equity   REAL    NOT NULL,
                    consecutive_losses INTEGER NOT NULL,
                    last_trade_time    REAL    NOT NULL,
                    last_trade_won     INTEGER,
                    trades_today       INTEGER NOT NULL,
                    updated_at         REAL    NOT NULL
                );
                CREATE TABLE IF NOT EXISTS kill_switches (
                    breaker               TEXT PRIMARY KEY,
                    tripped_at            REAL NOT NULL,
                    reason                TEXT NOT NULL,
                    manual_reset_required INTEGER NOT NULL
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def load(self) -> tuple[RiskState | None, dict[Breaker, KillSwitch]]:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT equity, peak_equity, day, day_start_equity, "
                    "consecutive_losses, last_trade_time, last_trade_won, trades_today, "
                    "updated_at FROM risk_state WHERE id = 1"
                ).fetchone()
                switches = conn.execute(
                    "SELECT breaker, tripped_at, reason, manual_reset_required "
                    "FROM kill_switches"
                ).fetchall()

            state = None
            if row is not None:
                state = RiskState(
                    equity=float(row[0]),
                    peak_equity=float(row[1]),
                    day=str(row[2]),
                    day_start_equity=float(row[3]),
                    consecutive_losses=int(row[4]),
                    last_trade_time=float(row[5]),
                    last_trade_won=None if row[6] is None else bool(row[6]),
                    trades_today=int(row[7]),
                    updated_at=float(row[8]),
                )

            tripped: dict[Breaker, KillSwitch] = {}
            for breaker, tripped_at, reason, manual in switches:
                try:
                    name = Breaker(str(breaker))
                except ValueError:
                    # An unrecognised latch is still a latch. Refusing to trade on a row we
                    # cannot interpret is the fail-closed answer; dropping it is not.
                    name = Breaker.UNKNOWN_STATE
                tripped[name] = KillSwitch(name, float(tripped_at), str(reason), bool(manual))
            return state, tripped
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise RiskStateUnreadable(f"{self.path}: {exc}") from exc

    def save_state(self, state: RiskState) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO risk_state (id, equity, peak_equity, day, day_start_equity, "
                "consecutive_losses, last_trade_time, last_trade_won, trades_today, "
                "updated_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET equity=excluded.equity, "
                "peak_equity=excluded.peak_equity, day=excluded.day, "
                "day_start_equity=excluded.day_start_equity, "
                "consecutive_losses=excluded.consecutive_losses, "
                "last_trade_time=excluded.last_trade_time, "
                "last_trade_won=excluded.last_trade_won, "
                "trades_today=excluded.trades_today, updated_at=excluded.updated_at",
                (
                    state.equity, state.peak_equity, state.day, state.day_start_equity,
                    state.consecutive_losses, state.last_trade_time,
                    None if state.last_trade_won is None else int(state.last_trade_won),
                    state.trades_today, state.updated_at,
                ),
            )

    def trip(self, switch: KillSwitch) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO kill_switches (breaker, tripped_at, reason, "
                "manual_reset_required) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(breaker) DO NOTHING",  # keep the original trip time
                (
                    switch.breaker.value, switch.tripped_at, switch.reason,
                    int(switch.manual_reset_required),
                ),
            )

    def clear(self, breaker: Breaker) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM kill_switches WHERE breaker = ?", (breaker.value,))


# --- the manager -----------------------------------------------------------

class RiskManager:
    """Holds the breakers and answers one question: may we open a position now?

        manager = RiskManager(RiskParams.from_config(cfg), SqliteRiskStore(path))
        decision = manager.can_trade(now=time.time(), equity=eq, open_positions=0)
        if decision.allowed:
            ...
        manager.record_trade(now=time.time(), pnl=-12.5, symbol="BTC_USDT")

    Construction reloads whatever the store holds, so a restart resumes mid-day with its
    latches, its peak equity, and its losing streak intact.
    """

    def __init__(self, params: RiskParams | None = None, store: RiskStore | None = None) -> None:
        self.params = params or RiskParams()
        self.store = store if store is not None else MemoryRiskStore()
        self._load_error: str = ""
        try:
            self._state, self._switches = self.store.load()
        except Exception as exc:  # noqa: BLE001 — any failure here means "state unknown"
            # Constructing still succeeds so that `--status` can report *why* the bot is
            # halted. Every decision from here is a refusal until a human intervenes.
            self._state, self._switches = None, {}
            self._load_error = str(exc)

    # --- introspection -----------------------------------------------------

    @property
    def state(self) -> RiskState | None:
        """The persisted state, or None before the first equity observation."""
        return self._state

    @property
    def kill_switches(self) -> Mapping[Breaker, KillSwitch]:
        return dict(self._switches)

    @property
    def tripped(self) -> bool:
        """Whether any latch is currently holding trading closed."""
        return bool(self._switches) or bool(self._load_error)

    def active_breaker(self) -> KillSwitch | None:
        """The most severe tripped latch, or None. Ordering follows :data:`LATCHING`."""
        if self._load_error:
            return KillSwitch(
                Breaker.UNKNOWN_STATE, 0.0,
                f"persisted risk state could not be read ({self._load_error}); refusing "
                "rather than trading without knowing which limits are already tripped",
                manual_reset_required=True,
            )
        for breaker in LATCHING:
            if breaker in self._switches:
                return self._switches[breaker]
        for switch in self._switches.values():
            return switch
        return None

    def risk_fraction(self) -> float:
        """The fraction of equity a single trade may lose.

        Takes no arguments on purpose. There is no history to scale it by, so martingale,
        anti-martingale and "make it back" sizing are unrepresentable rather than merely
        disabled.
        """
        return self.params.per_trade

    def risk_amount(self, equity: float) -> float:
        return float(equity) * self.params.per_trade

    # --- observation -------------------------------------------------------

    def observe_equity(self, now: float, equity: float) -> None:
        """Record an equity reading and evaluate the equity-driven breakers.

        Called by :meth:`can_trade`, and worth calling directly on a schedule: drawdown at
        100x can arrive through an open position's mark price without any trade closing,
        and a breaker that only looks at closed trades would notice far too late.

        Raises :class:`RiskStateUnreadable` when the persisted state could not be loaded —
        there is nothing to update and nothing that may be assumed.
        """
        if self._load_error:
            raise RiskStateUnreadable(self._load_error)
        if not _finite(now) or float(now) <= 0:
            raise ValueError(f"unusable observation time {now!r}")
        if not _finite(equity) or float(equity) <= 0:
            raise ValueError(f"unusable equity observation {equity!r}")

        now = float(now)
        equity = float(equity)
        day = utc_day(now)

        if self._state is None:
            self._state = RiskState(
                equity=equity, peak_equity=equity, day=day, day_start_equity=equity,
                updated_at=now,
            )
        else:
            state = self._state
            if day != state.day:
                # A new UTC day: today's loss is measured from here, and the latches that
                # mean "stop for today" clear. The high-water mark does not reset — an
                # account is not restored to health by a calendar change.
                state = replace(
                    state, day=day, day_start_equity=equity, trades_today=0,
                    consecutive_losses=0,
                )
                for breaker in (Breaker.DAILY_LOSS, Breaker.CONSECUTIVE_LOSSES):
                    if breaker in self._switches:
                        self._switches.pop(breaker)
                        self.store.clear(breaker)
            self._state = replace(
                state, equity=equity, peak_equity=max(state.peak_equity, equity),
                updated_at=now,
            )

        self._evaluate_equity_breakers(now)
        self.store.save_state(self._state)

    def _evaluate_equity_breakers(self, now: float) -> None:
        state = self._state
        if state is None:
            return
        if state.drawdown >= self.params.max_drawdown:
            self._trip(
                Breaker.DRAWDOWN, now,
                f"drawdown {state.drawdown * 100:.2f}% reached the "
                f"{self.params.max_drawdown * 100:.2f}% limit "
                f"(peak {state.peak_equity:.2f}, now {state.equity:.2f})",
            )
        if state.daily_loss >= self.params.max_daily_loss:
            self._trip(
                Breaker.DAILY_LOSS, now,
                f"down {state.daily_loss * 100:.2f}% on {state.day}, at the "
                f"{self.params.max_daily_loss * 100:.2f}% daily limit "
                f"(day opened {state.day_start_equity:.2f}, now {state.equity:.2f})",
            )

    def _trip(self, breaker: Breaker, now: float, reason: str) -> None:
        """Latch a breaker. Re-tripping keeps the original reason and timestamp."""
        if breaker in self._switches:
            return
        switch = KillSwitch(
            breaker=breaker,
            tripped_at=float(now),
            reason=reason,
            manual_reset_required=breaker in MANUAL_RESET_ONLY,
        )
        self._switches[breaker] = switch
        self.store.trip(switch)

    # --- the decision ------------------------------------------------------

    def can_trade(
        self,
        now: float,
        equity: float,
        open_positions: int,
        symbol: str | None = None,
        open_symbols: Iterable[str] = (),
    ) -> RiskDecision:
        """May a new position be opened right now?

        Every argument is required and every one of them is checked. ``open_positions``
        and ``open_symbols`` come from the exchange, not from state kept here: the
        exchange is the only authority on what is actually open, and a second copy would
        eventually disagree with it.
        """
        if not _finite(now) or float(now) <= 0:
            return RiskDecision(False, Breaker.UNKNOWN_STATE, f"unusable timestamp {now!r}")
        if not _finite(equity) or float(equity) <= 0:
            return RiskDecision(
                False, Breaker.UNKNOWN_STATE,
                f"equity {equity!r} is unusable; refusing rather than assuming an account "
                "balance",
            )
        if not _finite(open_positions) or float(open_positions) != int(open_positions):
            return RiskDecision(
                False, Breaker.UNKNOWN_STATE,
                f"open position count {open_positions!r} is not a whole number",
            )
        open_positions = int(open_positions)
        if open_positions < 0:
            return RiskDecision(
                False, Breaker.UNKNOWN_STATE,
                f"open position count {open_positions} is negative",
            )

        # A clock that has stepped backwards invalidates every elapsed-time answer this
        # class gives — cooldowns, the day boundary, the trip timestamps. Refusing is the
        # only safe reading; the alternative is trading straight through a cooldown.
        previous = self._state
        if previous is not None and float(now) < previous.updated_at - CLOCK_TOLERANCE_SECONDS:
            return RiskDecision(
                False, Breaker.UNKNOWN_STATE,
                f"clock went backwards: now={now:.0f} precedes the last observation at "
                f"{previous.updated_at:.0f}",
            )

        try:
            self.observe_equity(now, equity)
        except RiskStateUnreadable:
            switch = self.active_breaker()
            return RiskDecision(False, Breaker.UNKNOWN_STATE, switch.reason if switch else "")
        state = self._state
        assert state is not None  # observe_equity always leaves a state behind

        switch = self.active_breaker()
        if switch is not None:
            return RiskDecision(False, switch.breaker, switch.reason)

        if open_positions >= self.params.max_open_positions:
            return RiskDecision(
                False, Breaker.MAX_OPEN_POSITIONS,
                f"{open_positions} position(s) already open, limit is "
                f"{self.params.max_open_positions}",
            )

        held = {str(item) for item in open_symbols}
        if symbol is not None and str(symbol) in held:
            return RiskDecision(
                False, Breaker.ALREADY_OPEN,
                f"a position in {symbol} is already open; adding to it would be averaging "
                "down, which is permanently disabled",
            )

        if state.last_trade_won is not None:
            elapsed = max(0.0, float(now) - state.last_trade_time)
            cooldown = (
                self.params.cooldown_after_win_seconds if state.last_trade_won
                else self.params.cooldown_after_loss_seconds
            )
            if elapsed < cooldown:
                outcome = "win" if state.last_trade_won else "loss"
                return RiskDecision(
                    False, Breaker.COOLDOWN,
                    f"{cooldown - elapsed:.0f}s of the {cooldown:.0f}s post-{outcome} "
                    "cooldown remain",
                    retry_after=cooldown - elapsed,
                )

        return RiskDecision(True, None, "all risk limits clear")

    # --- trade outcomes ----------------------------------------------------

    def record_trade(
        self,
        now: float,
        pnl: float,
        equity: float | None = None,
        symbol: str | None = None,
    ) -> RiskDecision:
        """Register a closed trade and re-evaluate the breakers.

        ``equity`` is the account equity after the trade; when omitted it is derived from
        the previous reading plus ``pnl``. Pass the exchange's own figure whenever it is
        available — fees and funding make the derived value an approximation.

        Returns the decision that now applies, so a caller can log the breaker that a
        losing trade just tripped without a second call.
        """
        if not _finite(now) or not _finite(pnl):
            raise ValueError(f"unusable trade record: now={now!r}, pnl={pnl!r}")

        now = float(now)
        pnl = float(pnl)

        if equity is None:
            previous = self._state.equity if self._state is not None else None
            if previous is None:
                raise ValueError(
                    "record_trade needs an equity figure before any has been observed; "
                    "call observe_equity first or pass equity="
                )
            equity = previous + pnl
        if not _finite(equity) or float(equity) <= 0:
            raise ValueError(f"unusable post-trade equity {equity!r}")

        # Rollover and the equity breakers first, so a trade closing just after midnight
        # is counted against the new day rather than the one that has already ended.
        self.observe_equity(now, float(equity))
        state = self._state
        assert state is not None

        if pnl < 0:
            streak = state.consecutive_losses + 1
        elif pnl > 0:
            streak = 0
        else:
            streak = state.consecutive_losses  # a scratch is neither a win nor a loss

        self._state = replace(
            state,
            consecutive_losses=streak,
            last_trade_time=now,
            last_trade_won=pnl > 0,
            trades_today=state.trades_today + 1,
            updated_at=now,
        )

        if streak >= self.params.max_consecutive_losses:
            self._trip(
                Breaker.CONSECUTIVE_LOSSES, now,
                f"{streak} consecutive losses reached the "
                f"{self.params.max_consecutive_losses} limit",
            )

        self.store.save_state(self._state)

        switch = self.active_breaker()
        if switch is not None:
            return RiskDecision(False, switch.breaker, switch.reason)
        return RiskDecision(True, None, "all risk limits clear")

    # --- operator actions --------------------------------------------------

    def trip(self, reason: str, now: float, breaker: Breaker = Breaker.MANUAL) -> None:
        """Latch a breaker by hand — the panic switch. Requires :meth:`reset` to clear."""
        self._trip(breaker, now, reason)

    def reset(self, breaker: Breaker | None = None, now: float | None = None) -> tuple[Breaker, ...]:
        """Clear latches and re-baseline what they measured. A human action only.

        Re-baselining is what makes the reset mean anything. Clearing the drawdown latch
        while the high-water mark still sits 3% above current equity re-trips it on the
        very next observation, so the account would be permanently halted and the reset
        would be theatre. Acknowledging the drawdown therefore moves the high-water mark
        down to current equity — the new measurement starts here — and clearing a
        daily-loss latch likewise restarts the day from the current balance.

        That is a deliberate loss of history, which is exactly why nothing in the bot
        calls this: the drawdown limit exists to force a human to look at *why* before
        trading resumes. With no argument it clears everything, drawdown included.
        """
        targets = tuple(self._switches) if breaker is None else (breaker,)
        cleared = []
        for target in targets:
            if target in self._switches:
                self._switches.pop(target)
                self.store.clear(target)
                cleared.append(target)

        state = self._state
        if state is not None and cleared:
            if Breaker.DRAWDOWN in cleared:
                state = replace(state, peak_equity=state.equity)
            if Breaker.DAILY_LOSS in cleared:
                state = replace(state, day_start_equity=state.equity)
            if Breaker.CONSECUTIVE_LOSSES in cleared:
                state = replace(state, consecutive_losses=0)
            if now is not None:
                state = replace(state, updated_at=float(now))
            self._state = state
            self.store.save_state(state)
        return tuple(cleared)

    def status(self) -> dict[str, Any]:
        """A flat summary for the logger and the Phase 11 dashboard."""
        state = self._state
        return {
            "equity": None if state is None else state.equity,
            "peak_equity": None if state is None else state.peak_equity,
            "drawdown": None if state is None else state.drawdown,
            "daily_loss": None if state is None else state.daily_loss,
            "day": None if state is None else state.day,
            "trades_today": None if state is None else state.trades_today,
            "consecutive_losses": None if state is None else state.consecutive_losses,
            "risk_per_trade": self.params.per_trade,
            "tripped": {b.value: s.reason for b, s in self._switches.items()},
        }
