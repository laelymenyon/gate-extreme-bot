"""gate-extreme-bot — CLI entry point.

Every run mode reports its readiness state. Three of the four then stop: `--status`,
`--backtest` and `--paper` describe what exists rather than driving it.

`--mode live` is the exception, and the only path in this file that can place an order. It
reaches the Phase 15 runner (`live/loop.py`) **only** when all three safety switches agree —
`DRY_RUN=false` in .env, `--mode live`, and `--confirm-live`. With any switch shut this file
prints the refusal and the readiness audit, and exits without constructing a client. With all
three open the runner still refuses to trade unless its own preflight reports GO.

    python main.py --status
    python main.py --mode paper
    python main.py --mode backtest
    python main.py --preflight
    python main.py --connectivity          # read-only credentials + account + market check
    python main.py --mode live --confirm-live
    python main.py --verify-live-order --symbol BTC_USDT
                                          # FINAL barrier: one real order, protected and
                                          # closed, after typing "BTC_USDT SEND"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from config import ConfigError, load_config

PHASES = [
    ("1",  "Environment + architecture",   True),
    ("2",  "Gate.io REST client",          True),
    ("3",  "Market data + WebSocket",      True),
    ("4",  "Indicators",                   True),
    ("5",  "Signal scoring",               True),
    ("6",  "Risk manager",                 True),
    ("7",  "Liquidation protection",       True),
    ("8",  "Order execution",             True),
    ("9",  "Backtesting",                  True),
    ("10", "Paper trading loop",          True),
    ("11", "Dashboard + database",         True),
    ("12", "Testing",                      True),
    ("13", "Paper trading validation",      True),
    ("14", "Live readiness",                True),
    ("15", "Live trading runner",           True),
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Gate.io high-leverage futures bot (DRY_RUN by default)",
    )
    parser.add_argument("--mode", choices=("paper", "backtest", "live"), default="paper")
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="Required alongside --mode live AND DRY_RUN=false before any real order is sent",
    )
    parser.add_argument("--status", action="store_true", help="Show config and safety state")
    parser.add_argument("--positions", action="store_true", help="Show open positions")
    parser.add_argument("--stats", action="store_true", help="Show performance analytics")
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Grade the stored paper history against the Phase 13 acceptance criteria",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Audit live readiness and report GO/NO-GO. Reads only; opens nothing",
    )
    parser.add_argument(
        "--connectivity",
        action="store_true",
        help="Read-only production check: credentials/auth, balance, positions, orders, "
             "contract, mark price, risk tiers. Never places an order",
    )
    parser.add_argument(
        "--verify-live-order",
        action="store_true",
        help="FINAL barrier: one explicit real market order (minimum size), immediately "
             "protected and closed, to verify the production lifecycle. Requires all three "
             "switches AND typing the symbol plus SEND at the prompt",
    )
    parser.add_argument(
        "--symbol",
        help="Contract to trade (default: the first symbol in universe.symbols)",
    )
    parser.add_argument(
        "--steps",
        type=int,
        help="Run this many loop iterations, then stop (default: run until Ctrl-C)",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=5.0,
        help="Seconds between loop iterations (default: 5.0)",
    )
    return parser


def print_status(cfg) -> None:
    lev = cfg.section("leverage")
    risk = cfg.section("risk")
    gate = "LIVE — REAL ORDERS" if cfg.live_enabled else "DRY RUN — simulation only"

    print("=" * 66)
    print("  gate-extreme-bot")
    print("=" * 66)
    print(f"  Trading gate      : {gate}")
    print(f"    DRY_RUN (.env)  : {cfg.env_dry_run}")
    print(f"    --mode          : {cfg.run_mode}")
    print(f"    --confirm-live  : {cfg.confirm_live}")
    print(f"  Credentials       : {'present' if cfg.credentials.present else 'EMPTY'}")
    print("-" * 66)
    print(f"  Strategy mode     : {cfg.get('mode')}")
    print(f"  Leverage          : {lev['default']}x (minimum {lev['minimum']}x, {lev['margin_mode']})")
    print(f"  Risk per trade    : {risk['per_trade'] * 100:.2f}%")
    print(f"  Daily loss limit  : {risk['max_daily_loss'] * 100:.2f}%")
    print(f"  Max drawdown      : {risk['max_drawdown'] * 100:.2f}%")
    print(f"  Min score         : {cfg.get('strategy.minimum_score')}/100")
    print(f"  Min R:R           : {cfg.get('strategy.minimum_rr')}")
    print(f"  Liq buffer        : {cfg.get('protection.liquidation_buffer') * 100:.2f}%")
    symbols = cfg.get("universe.symbols")
    print(f"  Universe          : {len(symbols)} pairs (>= {lev['minimum']}x)")
    print(f"    tradable        : {', '.join(symbols[:4])}, +{len(symbols) - 4} more")
    print("-" * 66)
    print("  Build progress")
    for number, name, done in PHASES:
        print(f"    [{'x' if done else ' '}] Phase {number:>2}  {name}")
    print("=" * 66)


async def show_positions(cfg) -> int:
    """Read-only. Uses the Phase 2 client; the write-guard blocks any state change."""
    from exchange.gate_client import GateAPIError, GateFuturesClient

    if not cfg.credentials.present:
        print("\n--positions requires GATE_API_KEY/GATE_API_SECRET in .env")
        return 2

    async with GateFuturesClient(cfg) as client:
        try:
            account = await client.get_account()
            positions = await client.list_positions(holding=True)
        except GateAPIError as exc:
            print(f"\nGate.io error: {exc}", file=sys.stderr)
            return 1

    print(f"\n  Total balance     : {account.get('total', '?')} {account.get('currency', '')}")
    print(f"  Available         : {account.get('available', '?')}")
    print(f"  Unrealised PnL    : {account.get('unrealised_pnl', '?')}")
    print(f"  Position margin   : {account.get('position_margin', '?')}")

    if not positions:
        print("\n  No open positions.")
        return 0

    print(f"\n  {len(positions)} open position(s):")
    for pos in positions:
        size = int(pos.get("size", 0))
        side = "LONG" if size > 0 else "SHORT"
        print(f"    {pos.get('contract'):<14} {side:<5} size={size:<8} "
              f"entry={pos.get('entry_price')} mark={pos.get('mark_price')} "
              f"liq={pos.get('liq_price')} lev={pos.get('leverage')}x "
              f"uPnL={pos.get('unrealised_pnl')}")
    return 0


def show_stats(cfg) -> str:
    """Render the Phase 11 dashboard over whatever history the database holds."""
    from database.models import TradeStore
    from monitoring.dashboard import Dashboard

    store = TradeStore.from_config(cfg)
    if store.count() == 0:
        return (f"No trades recorded yet in {store.path}. Paper or backtest runs write "
                "their history here; analytics appear once they have.")
    return Dashboard.from_config(cfg, store).render()


def show_validation(cfg) -> str:
    """Grade the stored paper history against the Phase 13 acceptance criteria.

    Reads only. Session-scoped conduct properties are withheld rather than assumed here —
    the database records outcomes, not events — so this path reports what the history
    supports and never reaches VALIDATED on its own. Validating a *run* means watching
    one: `paper.validation.run_session` then `validate`.
    """
    from database.models import TradeStore
    from paper.validation import SessionEvidence, ValidationParams, validate

    store = TradeStore.from_config(cfg)
    evidence = SessionEvidence.from_store(store, cfg)
    if not evidence.trades:
        return (f"No paper trades recorded yet in {store.path}. Validation grades a "
                "paper run's history; run one first.")
    report = validate(
        evidence, list(evidence.trades), list(evidence.curve),
        params=ValidationParams.from_config(cfg),
    )
    return report.render()


async def read_account_snapshot(cfg) -> "AccountSnapshot | None":
    """Read the futures account read-only through the Phase 2 client.

    Returns None when credentials are absent, or an ``AccountSnapshot`` that carries the
    failure when the read itself fails — either way the preflight report then says what
    actually happened instead of "not read". Only GETs are issued; the write-guard refuses
    anything else before a socket opens, so this path cannot change account state.
    """
    from exchange.gate_client import GateAPIError, GateFuturesClient
    from execution.preflight import AccountSnapshot

    if not cfg.credentials.present:
        return None
    async with GateFuturesClient(cfg) as client:
        try:
            account = await client.get_account()
            positions = await client.list_positions(holding=True)
            open_orders: list[dict] = []
            price_orders: list[dict] = []
            for reader, sink in ((client.list_open_orders, open_orders),
                                 (client.list_price_orders, price_orders)):
                try:
                    sink.extend(await reader())
                except GateAPIError:
                    pass  # resting orders are a fact we try to read; absence is not proof
            return AccountSnapshot.from_api(
                account, positions, list(open_orders) + list(price_orders)
            )
        except GateAPIError as exc:
            return AccountSnapshot.unreachable(str(exc))


def show_preflight(cfg, account=None) -> str:
    """Audit live readiness and report GO/NO-GO.

    Reads only. ``--preflight`` reads the account itself through the Phase 2 client (whose
    write-guard keeps that read-only), so the account group passes or fails on facts rather
    than "not read" whenever credentials are present. Without credentials the section
    reports "not read", which blocks — the same outcome as an unreadable account.

    This function authorises nothing. It cannot open the safety gate, and a GO still
    leaves live trading behind all three switches.
    """
    from database.models import TradeStore
    from execution.preflight import preflight

    store = TradeStore.from_config(cfg)
    return preflight(
        cfg, store, account=account, validation=observed_report(store, cfg),
    ).render()


def observed_report(store, cfg) -> Any | None:
    """The latest supervised paper run's re-graded report, or None when there is none.

    The evidence a watched run left behind is testimony about events — whether a position
    was ever carried unprotected, whether the ledger reconciled, whether the run ended
    flat — that the trades table cannot answer. Preflight withholds those (which blocks)
    unless it is handed an observed report, so the CLI hands it one. The report is
    re-derived here from the stored evidence, never read back: a pass asserted by storage
    would be a pass nobody checked.

    Corrupt or incompatible evidence refuses rather than half-reads: a misread field would
    become a conduct claim no run ever made, and that is exactly what preflight exists to
    block.
    """
    from paper.validation import ValidationParams, stored_session, validate

    try:
        evidence = stored_session(store)
    except ValueError as exc:
        # Fail closed and say so. Returning None leaves preflight with nothing to clear
        # the conduct checks with, which is a NO-GO — the same outcome as never having run
        # a supervised session, which is the honest reading of unreadable testimony.
        print(f"stored paper evidence is unusable: {exc}", file=sys.stderr)
        return None
    if evidence is None:
        return None
    return validate(
        evidence, list(evidence.trades), list(evidence.curve),
        params=ValidationParams.from_config(cfg),
    )


def run_live_mode(cfg, args) -> int:
    """Delegate to the Phase 15 live runner. Only reachable with the gate open.

    The closed-gate branch in :func:`main` never arrives here, so this function does not
    re-explain the switches — it starts the loop that `live/loop.py` refuses to start on
    its own terms if preflight is NO-GO.

    The observed paper evidence is loaded and passed explicitly, exactly as `--preflight`
    does, so the audit an operator read before typing this command is the audit the runner
    then performs. A run whose evidence has gone missing is a NO-GO, not a silent fallback
    to stored history — which cannot establish conduct and would block anyway.
    """
    from database.models import TradeStore
    from live.loop import run_live

    store = TradeStore.from_config(cfg)
    exit_code, _report, _pf = asyncio.run(run_live(
        cfg,
        symbol=args.symbol,
        steps=args.steps,
        poll_seconds=args.poll_seconds,
        store=store,
        validation=observed_report(store, cfg),
    ))
    return exit_code


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        cfg = load_config(run_mode=args.mode, confirm_live=args.confirm_live)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.status or args.positions or args.stats or args.validate or args.preflight \
            or args.connectivity:
        print_status(cfg)
        exit_code = 0
        if args.preflight:
            # The audit reads the account itself so its account group can pass or fail on
            # facts. Read-only: the Phase 2 write-guard refuses any state change before a
            # socket opens, and the CLI never supplies a live-enabled config here anyway.
            account = asyncio.run(read_account_snapshot(cfg))
        else:
            account = None
        if args.positions:
            exit_code = asyncio.run(show_positions(cfg))
        if args.stats:
            print()
            print(show_stats(cfg))
        if args.validate:
            print()
            print(show_validation(cfg))
        if args.preflight:
            print()
            print(show_preflight(cfg, account))
        if args.connectivity:
            from live.verify import check_connectivity

            exit_code = asyncio.run(check_connectivity(cfg, symbol=args.symbol))
        return exit_code

    print_status(cfg)

    if args.verify_live_order:
        # The explicit one-order verification. Behind a shut gate this is a refusal, not a
        # run: the gate-closed branch below exists for `--mode live`, but an order command
        # that could not possibly be allowed must say so and stop.
        if not cfg.live_enabled:
            print("\nLive order verification requested but the safety gate is CLOSED.")
            print("All three are required: DRY_RUN=false in .env, --mode live, "
                  "--confirm-live.")
            print("No order will be sent.")
            print()
            print(show_preflight(cfg))
            print("\nNo order was sent. Open all three switches to reach the verification,")
            print("which still asks you to type the symbol and SEND before anything moves.")
            return 0
        from database.models import TradeStore
        from live.verify import verify_live_order

        print()
        return asyncio.run(verify_live_order(
            cfg, symbol=args.symbol, store=TradeStore.from_config(cfg),
        ))

    if args.mode == "live":
        if not cfg.live_enabled:
            # Unchanged from Phase 14: a refused live run explains what would satisfy it
            # and exits 0, because "you did not open the gate" is not a program error.
            print("\nLive mode requested but the safety gate is CLOSED.")
            print("All three are required: DRY_RUN=false in .env, --mode live, --confirm-live.")
            print("No orders will be sent.")
            print()
            print(show_preflight(cfg))
            print("\nNo order was sent. Open all three switches to reach the live runner,")
            print("which still refuses to start unless preflight reports GO.")
            return 0
        # The gate is open. From here the live runner owns the decision, and its own
        # preflight is what allows or refuses the first order.
        print()
        return run_live_mode(cfg, args)

    if args.mode == "paper":
        print("\nPaper trading is implemented (paper/loop.py) and Phase 13 grades a run "
              "against explicit acceptance")
        print("criteria (paper/validation.py). Driving it needs a symbol and a data "
              "source; see tests/test_validation.py")
        print("for a complete runnable example, and --validate to grade the history a "
              "run leaves behind.")
        print("No real order can be placed: PaperTrader refuses to construct while the "
              "safety gate is open, and it")
        print("only ever uses the in-process simulator.")
    else:
        print(f"\nAll 15 phases complete. The '{args.mode}' engine is not wired to a "
              "runner; nothing was traded.")
    print("Architecture and verified API findings: docs/ARCHITECTURE.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
