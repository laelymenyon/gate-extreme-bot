"""Live readiness: the last gate before real money, and the reasons it stays shut.

PHASE 14.

Every phase before this one was built to *refuse*. The write-guard refuses a POST, the
liquidation guard refuses a plan, the breakers refuse a day, the paper loop refuses to
exist while the gate is open, and Phase 13 refuses a verdict on a thin sample. This is the
first module whose success condition is *permitting* something, which makes it the one
place where a mistake is expensive in the direction that matters.

So it is built as an audit, not a switch. It reads state, compares it against the
conditions the repository already committed to, and returns GO or NO-GO. It has no way to
open the safety gate — no flag, no environment write, no config mutation. Even a GO leaves
live trading exactly where it was: behind ``DRY_RUN=false`` **and** ``--mode live``
**and** ``--confirm-live``, all three typed by a human who read this report.

**The conditions are not invented here.** README's roadmap already states the rule:

    Live trading is not enabled until paper trading has run correctly and backtesting
    shows the edge is not merely an artifact of leverage.

That sentence is the specification. This module makes it executable, because a precondition
that lives only in prose is one that gets skipped at 2am by someone who is sure they
remember it. Phase 13 already answers the first half — its conduct group asks whether the
machine behaved, and its evidence group asks whether the edge is real in R. Preflight reads
that answer rather than recomputing it, so the two cannot disagree.

**Fail-closed, and stricter than Phase 13.** There, ``INSUFFICIENT`` withheld a verdict.
Here it blocks: "we could not establish this" and "this is fine" must never be the same
outcome when the next step is real money at 100x. A check that cannot be evaluated is a
blocker, and the report says which fact was missing.

**Reading stored history alone can never reach GO.** Three of Phase 13's conduct checks are
events, not records — whether a position was ever carried unprotected, whether the ledger
reconciles against an independent account figure, whether the run ended flat — and a trade
table cannot testify to them. Those come back ``INSUFFICIENT`` from the database, which
blocks here by design. Clearing them requires an *observed* validation report from a
supervised paper run, passed in deliberately. That is the intended path to live, and it
requires a human to have watched the thing run.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from database.models import TradeStore
from monitoring.dashboard import compute
from paper.validation import (
    CheckStatus,
    SessionEvidence,
    ValidationParams,
    ValidationReport,
    validate,
)

__all__ = [
    "PreflightCheck",
    "AccountSnapshot",
    "PreflightReport",
    "preflight",
]

#: Groups, in reporting order. Evidence first: it is the condition the roadmap actually
#: names, and the one an operator is most likely to want to skip.
GROUPS = ("evidence", "gate", "account", "risk")


@dataclass(frozen=True)
class PreflightCheck:
    """One readiness condition. Every one of these blocks — there are no advisories."""

    name: str
    status: CheckStatus
    detail: str
    group: str = "gate"

    @property
    def ok(self) -> bool:
        return self.status is CheckStatus.PASS


@dataclass(frozen=True)
class AccountSnapshot:
    """What the exchange said about the account, read through the Phase 2 client.

    A plain value object so this module never touches the network itself: the caller does
    the reads (which the write-guard already restricts to reads) and hands over the result.
    ``reachable`` defaults False so an unbuilt snapshot blocks rather than passes.
    """

    reachable: bool = False
    currency: str = ""
    total: float = 0.0
    available: float = 0.0
    open_positions: int = 0
    open_orders: int = 0
    leverage: int = 0
    margin_mode: str = ""
    error: str = ""

    @classmethod
    def from_api(cls, account: Mapping[str, Any],
                 positions: Sequence[Mapping[str, Any]] = (),
                 open_orders: Sequence[Mapping[str, Any]] = ()) -> "AccountSnapshot":
        """Build from ``get_account`` / ``list_positions`` payloads."""
        def number(value: Any) -> float:
            try:
                return float(value)
            except (TypeError, ValueError):
                return float("nan")

        held = [p for p in positions if int(p.get("size", 0) or 0) != 0]
        leverage = 0
        margin_mode = ""
        if held:
            leverage = int(float(held[0].get("leverage", 0) or 0))
            # Gate.io reports leverage "0" for cross margin; anything else is isolated.
            margin_mode = "cross" if leverage == 0 else "isolated"
        return cls(
            reachable=True,
            currency=str(account.get("currency", "")),
            total=number(account.get("total")),
            available=number(account.get("available")),
            open_positions=len(held),
            open_orders=len(list(open_orders)),
            leverage=leverage,
            margin_mode=margin_mode,
        )

    @classmethod
    def unreachable(cls, error: str) -> "AccountSnapshot":
        return cls(reachable=False, error=error)


def _evidence_checks(store: TradeStore | None, cfg: Any,
                     observed: ValidationReport | None,
                     params: ValidationParams) -> list[PreflightCheck]:
    """The roadmap's own precondition, read from Phase 13 rather than recomputed."""
    checks: list[PreflightCheck] = []

    if store is None and observed is None:
        return [PreflightCheck(
            "evidence.paper_validated", CheckStatus.INSUFFICIENT,
            "no trade history and no observed validation report were supplied, so nothing "
            "is known about whether paper trading ever ran",
            group="evidence",
        ), PreflightCheck(
            "evidence.backtest_edge", CheckStatus.INSUFFICIENT,
            "no trade history was supplied, so the backtest edge is unknown",
            group="evidence",
        )]

    # --- did paper trading run correctly? ---
    report = observed
    if report is None:
        assert store is not None
        # Deliberately no `cfg`: `from_store` would take the *current* gate state as the
        # gate state *during the run*, and preflight normally runs with the gate open —
        # which would report "the safety gate was OPEN during the run" about history
        # gathered days earlier. Stored history's own evidence of live contamination is a
        # live-mode row, which `SessionEvidence.from_store` already checks.
        report = validate(
            SessionEvidence.from_store(store), store.trades(mode="paper"),
            store.equity_curve(), params=params,
        )

    if not report.evidence.trades and not report.performance.trades:
        checks.append(PreflightCheck(
            "evidence.paper_validated", CheckStatus.FAIL,
            "no paper trades have ever been recorded. The roadmap requires paper trading "
            "to have run correctly before live trading is enabled, and nothing has run",
            group="evidence",
        ))
    elif report.validated:
        checks.append(PreflightCheck(
            "evidence.paper_validated", CheckStatus.PASS,
            f"Phase 13 validation passed over {report.performance.trades} trade(s)",
            group="evidence",
        ))
    else:
        blocking = [c.name for c in report.checks if c.status is not CheckStatus.PASS]
        withheld_only = not report.failures
        checks.append(PreflightCheck(
            "evidence.paper_validated",
            # Withheld and failed are both blockers here, but they are different problems
            # and the operator needs to know which: one needs more data, the other needs
            # a fix.
            CheckStatus.INSUFFICIENT if withheld_only else CheckStatus.FAIL,
            f"Phase 13 verdict is not a pass ({', '.join(blocking)}). "
            + ("Reading stored history cannot establish conduct; supply an observed "
               "validation report from a supervised paper run."
               if withheld_only and not report.evidence.observed
               else report.verdict()),
            group="evidence",
        ))

    # --- is the edge more than leverage? ---
    # Measured in R, over backtest history specifically: the roadmap names backtesting,
    # and R is the unit leverage cannot move.
    backtest = list(store.trades(mode="backtest")) if store is not None else []
    if not backtest:
        checks.append(PreflightCheck(
            "evidence.backtest_edge", CheckStatus.FAIL,
            "no backtest history is stored. The roadmap requires backtesting to show the "
            "edge is not merely an artifact of leverage; that has not been shown",
            group="evidence",
        ))
    else:
        performance = compute(backtest, min_trades_for_verdict=params.min_trades)
        if performance.trades < params.min_trades:
            checks.append(PreflightCheck(
                "evidence.backtest_edge", CheckStatus.INSUFFICIENT,
                f"{performance.trades} backtest trade(s) is below the "
                f"{params.min_trades} needed to tell edge from noise",
                group="evidence",
            ))
        elif not math.isfinite(performance.expectancy_r):
            checks.append(PreflightCheck(
                "evidence.backtest_edge", CheckStatus.INSUFFICIENT,
                "backtest expectancy in R is not a finite number",
                group="evidence",
            ))
        elif performance.expectancy_r > 0 and performance.profit_factor > 1.0:
            checks.append(PreflightCheck(
                "evidence.backtest_edge", CheckStatus.PASS,
                f"{performance.expectancy_r:+.3f}R per trade over "
                f"{performance.trades} backtest trades at PF "
                f"{performance.profit_factor:.2f}",
                group="evidence",
            ))
        else:
            checks.append(PreflightCheck(
                "evidence.backtest_edge", CheckStatus.FAIL,
                f"backtest expectancy is {performance.expectancy_r:+.3f}R at PF "
                f"{performance.profit_factor:.2f}: leverage would only lose it faster",
                group="evidence",
            ))

    return checks


def _gate_checks(cfg: Any) -> list[PreflightCheck]:
    """The three switches and the credentials, reported as the operator set them."""
    checks: list[PreflightCheck] = []
    live = bool(getattr(cfg, "live_enabled", False))

    if live:
        checks.append(PreflightCheck(
            "gate.three_switches", CheckStatus.PASS,
            "DRY_RUN=false, --mode live and --confirm-live all agree",
        ))
    else:
        checks.append(PreflightCheck(
            "gate.three_switches", CheckStatus.FAIL,
            f"the gate is shut (DRY_RUN={getattr(cfg, 'env_dry_run', True)}, "
            f"--mode {getattr(cfg, 'run_mode', '?')}, "
            f"--confirm-live {getattr(cfg, 'confirm_live', False)}). This is the correct "
            "resting state; it is reported as a blocker because live trading is what "
            "preflight is checking readiness for",
        ))

    credentials = getattr(cfg, "credentials", None)
    present = bool(getattr(credentials, "present", False))
    checks.append(PreflightCheck(
        "gate.credentials",
        CheckStatus.PASS if present else CheckStatus.FAIL,
        "API credentials are present" if present
        else "GATE_API_KEY/GATE_API_SECRET are empty",
    ))

    # Config-level protections that would be catastrophic to have drifted. These are
    # already enforced at load; re-read here because preflight is the place that answers
    # "what is actually in force right now", and a reader should not have to trust that
    # the loader ran the check they care about.
    for path, expected, why in (
        ("risk.martingale", False, "martingale sizing"),
        ("risk.averaging_down", False, "averaging down"),
    ):
        actual = bool(cfg.get(path, False))
        checks.append(PreflightCheck(
            f"gate.{path.split('.')[-1]}",
            CheckStatus.PASS if actual is expected else CheckStatus.FAIL,
            f"{why} is disabled" if actual is expected
            else f"{why} is ENABLED via {path}",
        ))

    tif = cfg.get("take_profit.entry_tif", "")
    leverage = int(cfg.get("leverage.default", 0) or 0)
    if leverage >= 100 and tif != "poc":
        checks.append(PreflightCheck(
            "gate.post_only_entry", CheckStatus.FAIL,
            f"entry_tif is {tif!r} at {leverage}x; a taker entry needs a ~73% win rate "
            "merely to break even at this stop width",
        ))
    else:
        checks.append(PreflightCheck(
            "gate.post_only_entry", CheckStatus.PASS,
            f"entry_tif is {tif!r} at {leverage}x",
        ))

    return checks


def _account_checks(snapshot: AccountSnapshot | None, cfg: Any) -> list[PreflightCheck]:
    """What the exchange says. Unread is unknown, and unknown blocks."""
    if snapshot is None:
        return [PreflightCheck(
            "account.reachable", CheckStatus.INSUFFICIENT,
            "the account was not read, so its balance, margin mode and open positions are "
            "unknown. Preflight will not assume them",
            group="account",
        )]
    if not snapshot.reachable:
        return [PreflightCheck(
            "account.reachable", CheckStatus.FAIL,
            f"the account could not be read: {snapshot.error or 'unknown error'}",
            group="account",
        )]

    checks = [PreflightCheck(
        "account.reachable", CheckStatus.PASS,
        f"account read: {snapshot.total:.2f} {snapshot.currency} total, "
        f"{snapshot.available:.2f} available",
        group="account",
    )]

    funded = math.isfinite(snapshot.available) and snapshot.available > 0
    checks.append(PreflightCheck(
        "account.funded",
        CheckStatus.PASS if funded else CheckStatus.FAIL,
        f"{snapshot.available:.2f} {snapshot.currency} available"
        if funded else f"available balance is {snapshot.available!r}",
        group="account",
    ))

    # Starting flat is not a preference. Size, stop and liquidation distance are all
    # derived for a position this bot opened; inheriting someone else's leaves every one
    # of those numbers describing a position that does not exist.
    flat = snapshot.open_positions == 0 and snapshot.open_orders == 0
    checks.append(PreflightCheck(
        "account.flat",
        CheckStatus.PASS if flat else CheckStatus.FAIL,
        "no open positions or resting orders" if flat
        else f"{snapshot.open_positions} open position(s) and "
             f"{snapshot.open_orders} resting order(s); live trading must start flat",
        group="account",
    ))

    # Only checkable when something is open — Gate.io reports margin mode per position.
    configured = str(cfg.get("leverage.margin_mode", "isolated"))
    if not snapshot.margin_mode:
        checks.append(PreflightCheck(
            "account.margin_mode", CheckStatus.PASS,
            f"no open position to contradict the configured {configured} margin",
            group="account",
        ))
    elif snapshot.margin_mode == configured:
        checks.append(PreflightCheck(
            "account.margin_mode", CheckStatus.PASS,
            f"margin mode is {snapshot.margin_mode}",
            group="account",
        ))
    else:
        checks.append(PreflightCheck(
            "account.margin_mode", CheckStatus.FAIL,
            f"margin mode is {snapshot.margin_mode}, not the configured {configured}; "
            "cross margin puts the whole balance behind one position",
            group="account",
        ))

    return checks


def _risk_checks(store: TradeStore | None) -> list[PreflightCheck]:
    """The Phase 6 latches. A tripped breaker is a refusal that outlives the process."""
    if store is None:
        return [PreflightCheck(
            "risk.breakers_clear", CheckStatus.INSUFFICIENT,
            "no store was supplied, so tripped kill-switches cannot be read",
            group="risk",
        )]
    tripped = store.kill_switches()
    if tripped:
        listed = "; ".join(f"{name}: {reason[:60]}" for name, reason in sorted(tripped.items()))
        return [PreflightCheck(
            "risk.breakers_clear", CheckStatus.FAIL,
            f"{len(tripped)} kill-switch(es) are latched — {listed}",
            group="risk",
        )]
    return [PreflightCheck(
        "risk.breakers_clear", CheckStatus.PASS,
        "no kill-switch is latched",
        group="risk",
    )]


@dataclass(frozen=True)
class PreflightReport:
    """GO or NO-GO, and every condition behind it."""

    checks: tuple[PreflightCheck, ...] = ()

    @property
    def blockers(self) -> tuple[PreflightCheck, ...]:
        """Everything that is not a pass. Withheld blocks exactly like failed."""
        return tuple(c for c in self.checks if not c.ok)

    @property
    def ready(self) -> bool:
        """Fail-closed: an empty report is not a pass, and neither is an unknown."""
        return bool(self.checks) and all(c.ok for c in self.checks)

    def verdict(self) -> str:
        if not self.checks:
            return "NO-GO — nothing was checked, which is not the same as nothing wrong."
        if self.ready:
            return (
                "GO — every readiness condition is met. This authorises nothing on its "
                "own: live trading still requires DRY_RUN=false, --mode live and "
                "--confirm-live, typed by a human who has read this report."
            )
        failed = [c for c in self.blockers if c.status is CheckStatus.FAIL]
        unknown = [c for c in self.blockers if c.status is CheckStatus.INSUFFICIENT]
        parts = []
        if failed:
            parts.append(f"{len(failed)} failed ({', '.join(c.name for c in failed)})")
        if unknown:
            parts.append(
                f"{len(unknown)} could not be established "
                f"({', '.join(c.name for c in unknown)})"
            )
        return f"NO-GO — {'; '.join(parts)}. No live order may be sent."

    def render(self) -> str:
        width = 66
        lines = ["=" * width, "  live readiness — preflight", "=" * width]
        mark = {
            CheckStatus.PASS: " pass ", CheckStatus.FAIL: " FAIL ",
            CheckStatus.INSUFFICIENT: "UNKNOWN",
        }
        for group in GROUPS:
            members = [c for c in self.checks if c.group == group]
            if not members:
                continue
            lines.append(f"  {group}")
            for check in members:
                lines.append(f"    [{mark[check.status]}] {check.name}")
                lines.append(f"              {check.detail}")
            lines.append("-" * width)
        lines += [f"  verdict: {self.verdict()}", "=" * width]
        return "\n".join(lines)


def preflight(cfg: Any, store: TradeStore | None = None, *,
              account: AccountSnapshot | None = None,
              validation: ValidationReport | None = None,
              params: ValidationParams | None = None) -> PreflightReport:
    """Audit live readiness. Reads only; returns a report and changes nothing.

    ``validation`` is an *observed* Phase 13 report from a supervised paper run. Without
    it the paper-conduct condition is derived from stored history, which cannot establish
    conduct and therefore blocks — see the module docstring.
    """
    params = params or ValidationParams.from_config(cfg)
    checks = (
        _evidence_checks(store, cfg, validation, params)
        + _gate_checks(cfg)
        + _account_checks(account, cfg)
        + _risk_checks(store)
    )
    return PreflightReport(checks=tuple(checks))
