"""Paper-trading validation: the gate between "it is built" and "it may be considered".

PHASE 13.

Phase 10 built the loop, Phase 11 stored what it did, Phase 12 tested the seams between
them. None of those answers the question the roadmap actually gates live trading on:

    Live trading is not enabled until paper trading has **run correctly** and backtesting
    shows the edge is **not merely an artifact of leverage**.

Two claims, and they fail in different ways, so this module keeps them apart.

**"Ran correctly" is a question about the machine, not the money.** It is answerable from
a handful of trades: was every filled entry protected, did any position get liquidated,
did a single loss exceed what the sizer budgeted, does the stored ledger reconcile with
the equity the loop finished on. A run that lost money while obeying every invariant
passes this; a run that made money while leaving one position unprotected does not. These
checks have no configuration switch, because a safety property you can tune off is not one.

**"Not merely an artifact of leverage" is a question about R.** Position size is
``risk / stop_distance`` (README invariant 3), so a trade's R-multiple is what it earned
per unit of *risk* — a number leverage cannot move. 100x and 10x with the same stop produce
the same R and different margin. Expectancy in R is therefore the only expectancy worth
gating on, and it is the number Phase 13 discovered paper trading was not recording at all
(see §20 of ARCHITECTURE): ``PaperTrade`` had no ``r_multiple`` field, so every paper trade
stored ``0.0`` and any sufficiently long paper run would have been reported as having no
edge regardless of what it earned.

**The verdict fails closed.** ``INSUFFICIENT`` is not ``PASS``. A criterion that cannot be
evaluated does not pass by default, and a run below ``backtest.min_trades_for_verdict``
is withheld rather than graded — the same threshold and the same refusal Phase 9 and
Phase 11 already use, read from the same config key so the three cannot drift apart.

**Passing is not permission.** This module returns a report. It sets no flag, writes no
config, and touches nothing the safety gate reads. Enabling live trading is Phase 14's
decision and a human's, and a green report here is an input to it, not a substitute.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

from database.models import EquityPoint, TradeRecord, TradeStore
from execution.order_manager import SimulatedGateway
from monitoring.dashboard import Performance, compute

__all__ = [
    "CheckStatus",
    "Check",
    "EVIDENCE_FORMAT",
    "ValidationParams",
    "SessionEvidence",
    "ValidationReport",
    "record_session",
    "run_session",
    "stored_session",
    "validate",
]

#: Bumped when the stored evidence payload changes meaning. An unrecognised format is
#: refused rather than guessed at: a misread field here would be a conduct claim nobody
#: made.
EVIDENCE_FORMAT = 1

#: The scalar facts a watched run attests to. Trades and the curve are handled separately
#: because they are records rather than scalars.
_EVIDENCE_FIELDS = (
    "symbol", "steps", "starting_equity", "equity", "simulated", "live_gate_open",
    "entries_attempted", "entries_filled", "entries_expired", "protection_failures",
    "flattened", "unprotected", "open_at_end",
)


class CheckStatus(str, Enum):
    """Three-valued on purpose. "Cannot tell" is not "fine"."""

    PASS = "pass"
    FAIL = "fail"
    INSUFFICIENT = "insufficient"


@dataclass(frozen=True)
class Check:
    """One acceptance criterion and what it measured."""

    name: str
    status: CheckStatus
    detail: str
    #: Conduct checks are safety properties and have no configurable threshold. Evidence
    #: checks are about sample size and edge, and can only withhold a verdict.
    conduct: bool = True

    @property
    def ok(self) -> bool:
        return self.status is CheckStatus.PASS


@dataclass(frozen=True)
class ValidationParams:
    """Thresholds. Only the evidence checks have any — see the module docstring."""

    #: Reused from ``backtest.min_trades_for_verdict`` rather than redefined, so Phase 9,
    #: Phase 11 and Phase 13 cannot quietly disagree about what a sample is worth.
    min_trades: int = 1000
    #: The account-level limit the Phase 6 breaker already enforces.
    max_drawdown: float = 0.03
    #: A stop-out costs 1R by construction. Slippage, fees and a gapped stop make the
    #: realised figure worse, and the liquidation buffer bounds how much worse before the
    #: position is closed for us. Beyond this, the loss was not the one that was sized,
    #: which is a conduct failure rather than a bad trade.
    max_loss_r: float = 1.5

    def __post_init__(self) -> None:
        if self.min_trades < 1:
            raise ValueError("min_trades must be >= 1")
        if not 0.0 < self.max_drawdown < 1.0:
            raise ValueError("max_drawdown must be in (0, 1)")
        if self.max_loss_r < 1.0:
            raise ValueError(
                "max_loss_r must be >= 1.0: a stop-out costs 1R by construction, so a "
                "tolerance below it would fail every correctly-executed losing trade"
            )

    @classmethod
    def from_config(cls, cfg: Any) -> "ValidationParams":
        base = cls()
        return cls(
            min_trades=int(
                cfg.get("backtest.min_trades_for_verdict", base.min_trades)
            ),
            max_drawdown=float(cfg.get("risk.max_drawdown", base.max_drawdown)),
            max_loss_r=float(cfg.get("validation.max_loss_r", base.max_loss_r)),
        )


@dataclass(frozen=True)
class SessionEvidence:
    """What a finished paper run is willing to swear to.

    Built from the trader and its gateway rather than from the report alone, because two
    of the conduct checks are about what the *exchange side* did — which gateway answered,
    and whether anything was left holding size. A report cannot be asked that.
    """

    symbol: str = ""
    steps: int = 0
    starting_equity: float = 0.0
    equity: float = 0.0
    simulated: bool = False
    live_gate_open: bool = True
    entries_attempted: int = 0
    entries_filled: int = 0
    entries_expired: int = 0
    protection_failures: int = 0
    flattened: int = 0
    unprotected: int = 0
    open_at_end: bool = False
    trades: tuple[Any, ...] = ()
    curve: tuple[EquityPoint, ...] = ()
    rejections: Mapping[str, int] = field(default_factory=dict)
    #: True when this came from watching a run, False when it was reconstructed from the
    #: database afterwards. Some conduct properties — whether a position ever existed
    #: unprotected, whether the run ended flat — are events, not records: a trade table
    #: cannot testify to them either way. Those checks are withheld rather than assumed
    #: when this is False, which is why reading history alone can never reach VALIDATED.
    observed: bool = True

    @classmethod
    def of(cls, trader: Any, curve: Sequence[EquityPoint] = ()) -> "SessionEvidence":
        report = trader.report
        rejections = dict(report.rejections)
        return cls(
            symbol=report.symbol,
            steps=report.steps,
            starting_equity=report.starting_equity,
            equity=report.equity,
            # The real type, not a name comparison: this is the assertion that the run
            # could not have reached the exchange, and it should be checked the way the
            # rest of the repo checks it.
            simulated=isinstance(trader.orders.gateway, SimulatedGateway),
            live_gate_open=bool(getattr(trader.config, "live_enabled", False)),
            entries_attempted=report.entries_attempted,
            entries_filled=report.entries_filled,
            entries_expired=report.entries_expired,
            protection_failures=report.protection_failures,
            flattened=report.flattened,
            # A protection failure that was *not* flattened is a position that existed
            # without a verified stop. That is the one outcome invariant 1 forbids.
            unprotected=int(rejections.get("protection:unprotected", 0)),
            open_at_end=trader.open_position is not None,
            trades=tuple(report.trades),
            curve=tuple(curve),
            rejections=rejections,
            observed=True,
        )

    @classmethod
    def from_store(cls, store: TradeStore, cfg: Any = None,
                   mode: str = "paper") -> "SessionEvidence":
        """Reconstruct what the database can attest to about past runs.

        Deliberately partial. The store holds outcomes, not events, so this fills in only
        what a record proves and leaves ``observed`` False for the rest.
        """
        trades = store.trades(mode=mode)
        curve = store.equity_curve()
        if curve:
            starting, equity = curve[0].equity, curve[-1].equity
        elif trades:
            starting = trades[0].equity_after - trades[0].pnl
            equity = trades[-1].equity_after
        else:
            starting = equity = 0.0
        return cls(
            steps=0,
            starting_equity=starting,
            equity=equity,
            # A live-mode row in a database being read as paper validation is itself the
            # finding, so it is checked rather than assumed.
            simulated=not store.trades(mode="live"),
            live_gate_open=bool(getattr(cfg, "live_enabled", False)) if cfg else False,
            entries_filled=len(trades),
            trades=tuple(trades),
            curve=tuple(curve),
            observed=False,
        )

    @property
    def net_pnl(self) -> float:
        return self.equity - self.starting_equity

    def to_payload(self, *, leverage: int = 100, mode: str = "paper") -> dict[str, Any]:
        """Serialise what this run witnessed, so a later process can grade it.

        Trades are converted to :class:`~database.models.TradeRecord` — the audit-trail
        shape — and carried *with* the evidence rather than left to be re-read from the
        trades table. Reconciliation compares one session's closing equity against the
        trades that session booked, and a table holding several runs would sum all of them
        against one run's equity move.

        Only the facts are stored. No check result and no verdict is written, so a restored
        session is re-graded against the current criteria on every read.
        """
        trades = []
        for trade in self.trades:
            record = (
                trade if isinstance(trade, TradeRecord)
                else TradeRecord.from_paper(trade, leverage=leverage, mode=mode)
            )
            row = asdict(record)
            row.pop("id", None)
            trades.append(row)
        return {
            "format": EVIDENCE_FORMAT,
            "observed": bool(self.observed),
            **{name: getattr(self, name) for name in _EVIDENCE_FIELDS},
            "rejections": dict(self.rejections),
            "trades": trades,
            "curve": [asdict(point) for point in self.curve],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SessionEvidence":
        """Rebuild a witnessed session. Raises ``ValueError`` on anything unrecognised.

        Fail-closed on the format: a payload this code does not understand is refused rather
        than partially read, because a field that silently defaulted would become a conduct
        claim that no run actually made.
        """
        version = payload.get("format")
        if version != EVIDENCE_FORMAT:
            raise ValueError(
                f"session evidence format {version!r} is not the expected "
                f"{EVIDENCE_FORMAT}; it was written by a different version of this bot "
                "and will not be guessed at"
            )
        scalars = {}
        for name in _EVIDENCE_FIELDS:
            if name not in payload:
                raise ValueError(f"session evidence is missing {name!r}")
            scalars[name] = payload[name]
        return cls(
            symbol=str(scalars["symbol"]),
            steps=int(scalars["steps"]),
            starting_equity=float(scalars["starting_equity"]),
            equity=float(scalars["equity"]),
            simulated=bool(scalars["simulated"]),
            live_gate_open=bool(scalars["live_gate_open"]),
            entries_attempted=int(scalars["entries_attempted"]),
            entries_filled=int(scalars["entries_filled"]),
            entries_expired=int(scalars["entries_expired"]),
            protection_failures=int(scalars["protection_failures"]),
            flattened=int(scalars["flattened"]),
            unprotected=int(scalars["unprotected"]),
            open_at_end=bool(scalars["open_at_end"]),
            trades=tuple(TradeRecord(**row) for row in payload.get("trades", ())),
            curve=tuple(EquityPoint(**row) for row in payload.get("curve", ())),
            rejections=dict(payload.get("rejections", {})),
            observed=bool(payload["observed"]),
        )


def record_session(store: TradeStore, evidence: SessionEvidence, *, leverage: int,
                   mode: str = "paper", regime: str = "") -> int:
    """Persist a finished session: every trade, and the equity curve it was sampled on.

    The curve is written as well as the trades because :func:`monitoring.dashboard.compute`
    measures drawdown from it, and with no curve at all that figure is ``0.0`` — a run that
    never recorded one reports "no drawdown ever", which at 100x is the most flattering
    possible lie. Returns the number of trades stored.
    """
    for point in evidence.curve:
        store.record_equity(point)
    stored = 0
    for trade in evidence.trades:
        store.record_trade(
            TradeRecord.from_paper(trade, leverage=leverage, mode=mode, regime=regime)
        )
        stored += 1
    return stored


async def run_session(trader: Any, *, steps: int | None = None,
                      store: TradeStore | None = None, leverage: int = 100,
                      mode: str = "paper", snapshot_stride: int = 1,
                      evidence_path: str | None = None) -> SessionEvidence:
    """Drive a paper run, sampling equity as it goes, and optionally persist it.

    Sampling happens through :meth:`~paper.loop.PaperTrader.run`'s ``on_step`` hook rather
    than in a second loop of our own, so the run being measured is the run that would have
    happened anyway — the source advances exactly once per step either way.

    ``evidence_path`` optionally points at a ``TradeStore`` (typically ``database.path``)
    for the observed evidence: a supervised run's testimony is an event, and events die
    with the process unless they are written down. The store used for ``store`` is not
    assumed — a run may want its trades persisted and its testimony separate, and storing
    the testimony in the same file as the trades is the default that keeps both beside
    what they describe.
    """
    if snapshot_stride < 1:
        raise ValueError("snapshot_stride must be >= 1")

    curve: list[EquityPoint] = []

    def snapshot(bot: Any) -> None:
        if bot.report.steps % snapshot_stride:
            return
        position = bot.open_position
        curve.append(EquityPoint(
            timestamp=bot.source.now(),
            # Mark-to-market, not realised: see PaperTrader.mark_to_market.
            equity=bot.mark_to_market(),
            balance=bot.report.equity,
            unrealised_pnl=bot.unrealised_pnl(bot.source.mark_price(bot.symbol)),
            margin_used=float(position["margin"]) if position else 0.0,
            open_positions=1 if position else 0,
            note=mode,
        ))

    await trader.run(steps, on_step=snapshot)
    evidence = SessionEvidence.of(trader, curve)
    if store is not None:
        record_session(store, evidence, leverage=leverage, mode=mode)
    if evidence_path is not None:
        # The run's own last observed instant, not the wall clock: a replayed session is
        # timestamped by the data it replayed, and ordering reads by row id (which
        # `latest_session_evidence` does) is what makes "most recent" mean insertion order
        # regardless.
        recorded_at = (
            curve[-1].timestamp if curve
            else float(getattr(trader.source, "now", lambda: 0.0)())
        )
        TradeStore(evidence_path).record_session_evidence(
            evidence.to_payload(leverage=leverage, mode=mode),
            recorded_at=recorded_at, mode=mode,
        )
    return evidence


def stored_session(store: TradeStore, *, mode: str = "paper") -> SessionEvidence | None:
    """The most recently recorded witnessed session, or None when none exists.

    Raises ``ValueError`` if the stored payload is corrupt or was written by an
    incompatible version — a misread testimony must refuse, not become a conduct claim.
    """
    payload = store.latest_session_evidence(mode=mode)
    return SessionEvidence.from_payload(payload) if payload is not None else None


# --- the criteria -----------------------------------------------------------

def _conduct_checks(evidence: SessionEvidence, trades: Sequence[TradeRecord],
                    params: ValidationParams) -> list[Check]:
    """Safety properties. Every one of these is a documented invariant, and none of them
    has a threshold an operator can move."""
    checks: list[Check] = []

    # 1. The run could not have reached the exchange.
    if evidence.live_gate_open:
        checks.append(Check(
            "simulation_only", CheckStatus.FAIL,
            "the safety gate was OPEN during the run; a paper result gathered with live "
            "trading enabled is not a paper result",
        ))
    elif not evidence.simulated:
        checks.append(Check(
            "simulation_only", CheckStatus.FAIL,
            "orders were routed to something other than SimulatedGateway",
        ))
    else:
        checks.append(Check(
            "simulation_only", CheckStatus.PASS,
            "gate shut and every order went to the in-process simulator",
        ))

    # 2. Invariant 1 — no position without a verified stop.
    if not evidence.observed:
        checks.append(Check(
            "stop_on_every_position", CheckStatus.INSUFFICIENT,
            "withheld: whether a position was ever carried unprotected is an event, and "
            "the trade table records outcomes. Only a watched run can answer this",
        ))
    elif evidence.unprotected:
        checks.append(Check(
            "stop_on_every_position", CheckStatus.FAIL,
            f"{evidence.unprotected} position(s) were carried without a verified stop",
        ))
    else:
        flattened = (
            f"; {evidence.flattened} unprotectable entr(ies) were market-closed as designed"
            if evidence.flattened else ""
        )
        checks.append(Check(
            "stop_on_every_position", CheckStatus.PASS,
            f"{evidence.entries_filled} filled entr(ies), none carried unprotected"
            + flattened,
        ))

    # 2b. The durable half of the same invariant: a stop that was placed leaves a record,
    #     and that record must describe a stop on the losing side of the entry. This one
    #     survives into the database, so it is checkable long after the run.
    stopless = [t for t in trades if not t.stop_loss]
    wrong_side = [
        t for t in trades
        if t.stop_loss and (
            (t.direction > 0 and t.stop_loss >= t.entry_price)
            or (t.direction < 0 and t.stop_loss <= t.entry_price)
        )
    ]
    if stopless:
        checks.append(Check(
            "stop_recorded", CheckStatus.FAIL,
            f"{len(stopless)} stored trade(s) carry no stop price at all",
        ))
    elif wrong_side:
        checks.append(Check(
            "stop_recorded", CheckStatus.FAIL,
            f"{len(wrong_side)} stored trade(s) have a stop on the profitable side of "
            "entry, which is not a stop",
        ))
    else:
        checks.append(Check(
            "stop_recorded", CheckStatus.PASS,
            f"all {len(trades)} stored trade(s) carry a stop on the losing side of entry",
        ))

    # 3. Invariant 2 — liquidation is never the stop.
    liquidated = [t for t in trades if t.exit_reason == "liquidation"]
    checks.append(Check(
        "no_liquidation",
        CheckStatus.FAIL if liquidated else CheckStatus.PASS,
        f"{len(liquidated)} trade(s) ended in liquidation" if liquidated
        else "no position reached its liquidation price",
    ))

    # 4. Invariant 3 — the loss that arrives is the loss that was sized.
    graded = [t for t in trades if t.r_multiple]
    worst = min((t.r_multiple for t in graded), default=0.0)
    if not graded and trades:
        checks.append(Check(
            "loss_within_budget", CheckStatus.FAIL,
            f"{len(trades)} trade(s) stored no R-multiple, so the loss budget cannot be "
            "checked at all — an unmeasurable invariant is a failed one",
        ))
    elif worst < -params.max_loss_r:
        checks.append(Check(
            "loss_within_budget", CheckStatus.FAIL,
            f"worst trade lost {-worst:.2f}R against a {params.max_loss_r:.2f}R tolerance",
        ))
    elif worst < 0:
        checks.append(Check(
            "loss_within_budget", CheckStatus.PASS,
            f"worst trade lost {-worst:.2f}R, within the {params.max_loss_r:.2f}R tolerance",
        ))
    else:
        # Every graded trade made money. Saying "worst loss 1.62R" here — the smallest
        # *win* — would report a profit as a loss, which is the exact species of
        # misreporting this phase exists to catch.
        checks.append(Check(
            "loss_within_budget", CheckStatus.PASS,
            f"no graded trade lost anything; the smallest was {worst:+.2f}R",
        ))

    # 5. The ledger is the audit trail, so it has to agree with the account.
    booked = sum(t.pnl for t in trades)
    # Flattened entries cost fees without producing a trade, so they are a legitimate
    # difference between the two figures rather than a discrepancy.
    drift = abs(evidence.net_pnl - booked)
    if not evidence.observed:
        checks.append(Check(
            "ledger_reconciles", CheckStatus.INSUFFICIENT,
            "withheld: reconciliation compares a run's closing equity against the trades "
            "it booked, and stored history has no independent account figure to check",
        ))
    elif evidence.flattened:
        checks.append(Check(
            "ledger_reconciles", CheckStatus.PASS,
            f"{drift:+.4f} between account and ledger, explained by "
            f"{evidence.flattened} flattened entr(ies) that cost fees without a trade",
        ))
    elif drift > max(1e-6, abs(evidence.net_pnl) * 1e-9):
        checks.append(Check(
            "ledger_reconciles", CheckStatus.FAIL,
            f"account moved {evidence.net_pnl:+.4f} but the stored trades sum to "
            f"{booked:+.4f}; the audit trail does not describe the account",
        ))
    else:
        checks.append(Check(
            "ledger_reconciles", CheckStatus.PASS,
            f"stored trades sum to the account's {evidence.net_pnl:+.4f} move",
        ))

    # 6. Nothing may be left holding size when the run ends.
    if not evidence.observed:
        checks.append(Check(
            "flat_at_end", CheckStatus.INSUFFICIENT,
            "withheld: whether a run ended flat is an event the trade table does not record",
        ))
    else:
        checks.append(Check(
            "flat_at_end",
            CheckStatus.FAIL if evidence.open_at_end else CheckStatus.PASS,
            "the run ended with an open position" if evidence.open_at_end
            else "the run ended flat",
        ))

    return checks


def _evidence_checks(evidence: SessionEvidence, performance: Performance,
                     curve: Sequence[EquityPoint], params: ValidationParams) -> list[Check]:
    """Sample size and edge. These withhold a verdict; they never bless one."""
    checks: list[Check] = []
    count = performance.trades

    # 7. Did the run exercise anything at all? Every conduct check above passes
    #    vacuously on a run that never traded, so this is what stops silence reading
    #    as success.
    if not evidence.entries_filled:
        checks.append(Check(
            "exercised", CheckStatus.INSUFFICIENT,
            f"{evidence.steps} step(s), {evidence.entries_attempted} entr(ies) attempted, "
            "none filled — nothing was validated. For a score-80 filter that is a "
            "possible outcome, not necessarily a fault",
            conduct=False,
        ))
    elif not evidence.observed:
        checks.append(Check(
            "exercised", CheckStatus.PASS,
            f"{count} stored trade(s) from past runs",
            conduct=False,
        ))
    else:
        checks.append(Check(
            "exercised", CheckStatus.PASS,
            f"{evidence.entries_filled} of {evidence.entries_attempted} entr(ies) filled "
            f"over {evidence.steps} steps",
            conduct=False,
        ))

    # 8. The sample threshold Phase 9 and Phase 11 already refuse below.
    if count < params.min_trades:
        checks.append(Check(
            "sample_size", CheckStatus.INSUFFICIENT,
            f"{count} trade(s) is below the {params.min_trades} needed to tell edge from "
            "noise; these figures describe this sample only",
            conduct=False,
        ))
    else:
        checks.append(Check(
            "sample_size", CheckStatus.PASS,
            f"{count} trades meets the {params.min_trades}-trade threshold",
            conduct=False,
        ))

    # 9. Expectancy in R — the leverage-independent question.
    if count < params.min_trades:
        checks.append(Check(
            "edge_not_from_leverage", CheckStatus.INSUFFICIENT,
            "withheld: expectancy is not graded below the sample threshold",
            conduct=False,
        ))
    elif not math.isfinite(performance.expectancy_r):
        checks.append(Check(
            "edge_not_from_leverage", CheckStatus.INSUFFICIENT,
            "expectancy in R is not a finite number, so no edge can be claimed from it",
            conduct=False,
        ))
    elif performance.expectancy_r > 0 and performance.profit_factor > 1.0:
        checks.append(Check(
            "edge_not_from_leverage", CheckStatus.PASS,
            f"{performance.expectancy_r:+.3f}R per trade at PF "
            f"{performance.profit_factor:.2f}; R is measured per unit of risk, so this "
            "is not leverage restating itself",
            conduct=False,
        ))
    else:
        checks.append(Check(
            "edge_not_from_leverage", CheckStatus.FAIL,
            f"{performance.expectancy_r:+.3f}R per trade at PF "
            f"{performance.profit_factor:.2f}: leverage would only lose it faster",
            conduct=False,
        ))

    # 10. Drawdown, measured from the curve rather than from closed trades.
    if not curve and count:
        checks.append(Check(
            "drawdown_within_limit", CheckStatus.INSUFFICIENT,
            "no equity curve was recorded, so drawdown reads as 0.00% whatever happened",
            conduct=False,
        ))
    elif performance.max_drawdown > params.max_drawdown:
        checks.append(Check(
            "drawdown_within_limit", CheckStatus.FAIL,
            f"peak-to-trough {performance.max_drawdown * 100:.2f}% exceeded the "
            f"{params.max_drawdown * 100:.2f}% account limit",
            conduct=False,
        ))
    else:
        checks.append(Check(
            "drawdown_within_limit", CheckStatus.PASS,
            f"peak-to-trough {performance.max_drawdown * 100:.2f}%, within the "
            f"{params.max_drawdown * 100:.2f}% limit",
            conduct=False,
        ))

    return checks


@dataclass(frozen=True)
class ValidationReport:
    """The verdict, and every measurement behind it."""

    checks: tuple[Check, ...]
    performance: Performance
    params: ValidationParams
    evidence: SessionEvidence

    @property
    def conduct(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if c.conduct)

    @property
    def failures(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if c.status is CheckStatus.FAIL)

    @property
    def withheld(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if c.status is CheckStatus.INSUFFICIENT)

    @property
    def conduct_ok(self) -> bool:
        """Whether the machine misbehaved anywhere it could be observed.

        Withheld conduct checks do not count against this — reading stored history cannot
        observe them, and reporting "did not behave correctly" for a question nobody could
        ask would be a false accusation. They still block :attr:`validated`, which is what
        keeps the unobserved case from passing.
        """
        return not any(c.status is CheckStatus.FAIL for c in self.conduct)

    @property
    def validated(self) -> bool:
        """Fail-closed: everything passed, and nothing was merely unmeasured."""
        return bool(self.checks) and all(c.ok for c in self.checks)

    def verdict(self) -> str:
        if not self.conduct_ok:
            names = ", ".join(c.name for c in self.failures if c.conduct)
            return (
                f"NOT VALIDATED — the run did not behave correctly ({names}). This is a "
                "defect to fix, not a strategy result to interpret."
            )
        edge_failures = [c for c in self.failures if not c.conduct]
        if edge_failures:
            names = ", ".join(c.name for c in edge_failures)
            return (
                f"NOT VALIDATED — conduct was clean, but the evidence argues against the "
                f"strategy ({names})."
            )
        if self.withheld:
            names = ", ".join(c.name for c in self.withheld)
            return (
                f"WITHHELD — conduct was clean; there is not yet enough evidence to grade "
                f"the strategy ({names}). Withheld is not passed."
            )
        return (
            "VALIDATED — conduct was clean and the sample supports a positive edge in R. "
            "This is an input to the Phase 14 live-readiness decision, not authorisation: "
            "nothing here opens the safety gate."
        )

    def render(self) -> str:
        width = 62
        lines = ["=" * width, "  paper-trading validation", "=" * width]
        mark = {
            CheckStatus.PASS: "pass", CheckStatus.FAIL: "FAIL",
            CheckStatus.INSUFFICIENT: "----",
        }

        for heading, group in (
            ("conduct — did the machine behave?", self.conduct),
            ("evidence — did the run prove anything?",
             tuple(c for c in self.checks if not c.conduct)),
        ):
            lines += [f"  {heading}", "-" * width]
            for check in group:
                lines.append(f"  [{mark[check.status]}] {check.name}")
                lines.append(f"         {check.detail}")
            lines.append("-" * width)

        performance = self.performance
        lines += [
            f"  trades                    : {performance.trades}",
            f"  expectancy (R)            : {performance.expectancy_r:+.3f}",
            f"  profit factor             : {performance.profit_factor:.2f}",
            f"  net pnl                   : {performance.net_pnl:+.2f}",
            f"  max drawdown              : {performance.max_drawdown * 100:.2f}%",
            "-" * width,
            f"  verdict: {self.verdict()}",
            "=" * width,
        ]
        return "\n".join(lines)


def validate(evidence: SessionEvidence, trades: Sequence[TradeRecord],
             curve: Sequence[EquityPoint] = (), *,
             params: ValidationParams | None = None) -> ValidationReport:
    """Judge a paper run. ``trades`` and ``curve`` are the stored history it produced.

    Metrics come from :func:`monitoring.dashboard.compute` rather than being recalculated,
    so the numbers in this report and the numbers ``--stats`` prints are the same numbers.
    """
    params = params or ValidationParams()
    effective_curve = list(curve or evidence.curve)
    performance = compute(
        trades, effective_curve,
        starting_equity=evidence.starting_equity,
        min_trades_for_verdict=params.min_trades,
    )
    checks = (
        _conduct_checks(evidence, trades, params)
        + _evidence_checks(evidence, performance, effective_curve, params)
    )
    return ValidationReport(
        checks=tuple(checks), performance=performance, params=params, evidence=evidence,
    )
