"""SQLite persistence: the trade record, the equity curve, and what they are for.

PHASE 11.

Two tables, in the same file the Phase 6 kill-switch store already uses
(``database.path``, WAL). Sharing the file is deliberate: a restart has to recover the
tripped breakers *and* the history that justifies them from one place, and two files can
disagree. Every statement is ``IF NOT EXISTS``, and this module does not touch
``risk_state`` or ``kill_switches`` — those belong to :class:`~risk.risk_manager.SqliteRiskStore`
and are read here only to report them.

**The trade record is the audit trail, so it stores what was decided as well as what
happened.** Signal score, market regime and the exit reason are columns, not log lines,
because the question worth asking after a losing week is not "how much" but "which setups,
in which regime, at what score". A schema that only records PnL cannot answer it.

**The equity curve is separate from the trades.** Drawdown at 100x can arrive through an
open position's mark price without any trade closing (the same reason Phase 6 observes
equity on every call), so a curve reconstructed from closed trades understates the worst
moment. Snapshots are appended independently.

Nothing here decides anything and nothing here places an order. It is storage.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "TradeRecord",
    "EquityPoint",
    "TradeStore",
    "SCHEMA_VERSION",
]

#: Bumped only when a column changes meaning. Additive columns do not need it.
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class TradeRecord:
    """One completed round trip, with the reasoning that produced it.

    Field list follows the Phase 1 contract (ARCHITECTURE §21). ``pnl`` is net of fees, so
    it is the figure the equity curve moves by; ``fees`` is retained separately because fee
    drag is the dominant cost at these stop widths and hiding it inside PnL would make it
    unauditable.
    """

    timestamp: float
    symbol: str
    side: str                       # long | short
    leverage: int
    entry_price: float
    exit_price: float
    size: int                       # signed contract count
    margin: float
    stop_loss: float
    take_profit: float = 0.0
    fees: float = 0.0
    funding: float = 0.0
    pnl: float = 0.0                # net of fees and funding
    pnl_percent: float = 0.0        # of equity at entry
    r_multiple: float = 0.0
    signal_score: float = 0.0
    market_regime: str = ""
    exit_reason: str = ""
    duration_seconds: float = 0.0
    liquidation_price: float = 0.0
    equity_after: float = 0.0
    mode: str = "paper"             # paper | backtest | live
    id: int | None = None

    @property
    def won(self) -> bool:
        return self.pnl > 0

    @property
    def direction(self) -> int:
        return 1 if self.side == "long" else -1

    @classmethod
    def from_paper(cls, trade: Any, *, leverage: int, margin: float = 0.0,
                   equity_before: float | None = None, regime: str = "",
                   mode: str = "paper") -> "TradeRecord":
        """Build from a Phase 10 ``PaperTrade`` or a Phase 9 ``Trade``.

        Both carry the same shape for the fields that matter, so one adapter serves paper
        and backtest runs and the stored history is comparable across them.
        """
        equity_after = float(getattr(trade, "equity_after", 0.0) or 0.0)
        net = float(getattr(trade, "net_pnl", 0.0) or 0.0)
        before = equity_before if equity_before is not None else equity_after - net
        stop = float(getattr(trade, "stop_price", 0.0) or 0.0)
        entry = float(getattr(trade, "entry_price", 0.0) or 0.0)
        risk = abs(entry - stop) * abs(int(getattr(trade, "size", 0) or 0))
        return cls(
            timestamp=float(getattr(trade, "exit_time", 0.0) or 0.0),
            symbol=str(getattr(trade, "symbol", "")),
            side="long" if int(getattr(trade, "direction", 0) or 0) > 0 else "short",
            leverage=int(leverage),
            entry_price=entry,
            exit_price=float(getattr(trade, "exit_price", 0.0) or 0.0),
            size=int(getattr(trade, "size", 0) or 0),
            margin=float(margin),
            stop_loss=stop,
            fees=float(getattr(trade, "fees", 0.0) or 0.0),
            funding=float(getattr(trade, "funding", 0.0) or 0.0),
            pnl=net,
            pnl_percent=(net / before) if before else 0.0,
            r_multiple=float(getattr(trade, "r_multiple", 0.0) or 0.0),
            signal_score=float(getattr(trade, "score", 0.0) or 0.0),
            market_regime=str(regime),
            exit_reason=str(getattr(trade, "exit_reason", "")),
            duration_seconds=(
                float(getattr(trade, "exit_time", 0.0) or 0.0)
                - float(getattr(trade, "entry_time", 0.0) or 0.0)
            ),
            equity_after=equity_after,
            mode=mode,
        )


@dataclass(frozen=True)
class EquityPoint:
    """One equity observation. Mark-to-market, not only on trade close."""

    timestamp: float
    equity: float
    balance: float = 0.0
    unrealised_pnl: float = 0.0
    margin_used: float = 0.0
    open_positions: int = 0
    note: str = ""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp         REAL    NOT NULL,
    symbol            TEXT    NOT NULL,
    side              TEXT    NOT NULL,
    leverage          INTEGER NOT NULL,
    entry_price       REAL    NOT NULL,
    exit_price        REAL    NOT NULL,
    size              INTEGER NOT NULL,
    margin            REAL    NOT NULL,
    stop_loss         REAL    NOT NULL,
    take_profit       REAL    NOT NULL,
    fees              REAL    NOT NULL,
    funding           REAL    NOT NULL,
    pnl               REAL    NOT NULL,
    pnl_percent       REAL    NOT NULL,
    r_multiple        REAL    NOT NULL,
    signal_score      REAL    NOT NULL,
    market_regime     TEXT    NOT NULL,
    exit_reason       TEXT    NOT NULL,
    duration_seconds  REAL    NOT NULL,
    liquidation_price REAL    NOT NULL,
    equity_after      REAL    NOT NULL,
    mode              TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS trades_timestamp ON trades (timestamp);
CREATE INDEX IF NOT EXISTS trades_symbol ON trades (symbol);
CREATE TABLE IF NOT EXISTS equity_curve (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp      REAL    NOT NULL,
    equity         REAL    NOT NULL,
    balance        REAL    NOT NULL,
    unrealised_pnl REAL    NOT NULL,
    margin_used    REAL    NOT NULL,
    open_positions INTEGER NOT NULL,
    note           TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS equity_timestamp ON equity_curve (timestamp);
"""

_TRADE_COLUMNS = (
    "timestamp", "symbol", "side", "leverage", "entry_price", "exit_price", "size",
    "margin", "stop_loss", "take_profit", "fees", "funding", "pnl", "pnl_percent",
    "r_multiple", "signal_score", "market_regime", "exit_reason", "duration_seconds",
    "liquidation_price", "equity_after", "mode",
)


class TradeStore:
    """Append-only trade and equity history.

        store = TradeStore("data/trades.db")
        store.record_trade(TradeRecord(...))
        store.record_equity(EquityPoint(now, equity))

    Append-only on purpose: there is no ``update_trade`` and no ``delete``. A record that
    can be edited after the fact is not an audit trail, and the one time anybody would want
    to edit it is the one time it matters.
    """

    def __init__(self, path: str | Path, *, wal: bool = True) -> None:
        self.path = Path(path)
        if str(self.path.parent) not in ("", "."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.wal = wal
        with self._connect() as conn:
            if wal:
                conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('version', ?) "
                "ON CONFLICT(key) DO NOTHING",
                (str(SCHEMA_VERSION),),
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    # --- writes ------------------------------------------------------------

    def record_trade(self, trade: TradeRecord) -> int:
        """Append one trade. Returns its row id."""
        values = asdict(trade)
        with self._connect() as conn:
            cursor = conn.execute(
                f"INSERT INTO trades ({', '.join(_TRADE_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _TRADE_COLUMNS)})",
                tuple(values[name] for name in _TRADE_COLUMNS),
            )
            return int(cursor.lastrowid)

    def record_trades(self, trades: Iterable[TradeRecord]) -> int:
        count = 0
        for trade in trades:
            self.record_trade(trade)
            count += 1
        return count

    def record_equity(self, point: EquityPoint) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO equity_curve (timestamp, equity, balance, unrealised_pnl, "
                "margin_used, open_positions, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (point.timestamp, point.equity, point.balance, point.unrealised_pnl,
                 point.margin_used, point.open_positions, point.note),
            )

    # --- reads -------------------------------------------------------------

    def trades(self, symbol: str | None = None, since: float | None = None,
               mode: str | None = None) -> list[TradeRecord]:
        clauses, params = [], []
        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(symbol)
        if since is not None:
            clauses.append("timestamp >= ?")
            params.append(float(since))
        if mode is not None:
            clauses.append("mode = ?")
            params.append(mode)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM trades{where} ORDER BY timestamp, id", tuple(params)
            ).fetchall()
        return [self._trade_from_row(row) for row in rows]

    @staticmethod
    def _trade_from_row(row: sqlite3.Row) -> TradeRecord:
        data = {key: row[key] for key in row.keys()}
        return TradeRecord(
            id=int(data.pop("id")),
            **{
                name: (int(data[name]) if name in ("leverage", "size") else data[name])
                for name in _TRADE_COLUMNS
            },
        )

    def equity_curve(self, since: float | None = None) -> list[EquityPoint]:
        where = " WHERE timestamp >= ?" if since is not None else ""
        params = (float(since),) if since is not None else ()
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM equity_curve{where} ORDER BY timestamp, id", params
            ).fetchall()
        return [
            EquityPoint(
                timestamp=row["timestamp"], equity=row["equity"], balance=row["balance"],
                unrealised_pnl=row["unrealised_pnl"], margin_used=row["margin_used"],
                open_positions=int(row["open_positions"]), note=row["note"],
            )
            for row in rows
        ]

    def kill_switches(self) -> dict[str, str]:
        """The Phase 6 latches, read but never written here.

        The table belongs to ``SqliteRiskStore``; the dashboard needs to display it, and a
        missing table simply means no risk manager has run against this file yet.
        """
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT breaker, reason FROM kill_switches"
                ).fetchall()
        except sqlite3.Error:
            return {}
        return {row["breaker"]: row["reason"] for row in rows}

    def count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0])

    def schema_version(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'version'"
            ).fetchone()
        return int(row["value"]) if row else 0

    @classmethod
    def from_config(cls, cfg: Any) -> "TradeStore":
        return cls(cfg.get("database.path", "data/trades.db"),
                   wal=bool(cfg.get("database.wal", True)))
