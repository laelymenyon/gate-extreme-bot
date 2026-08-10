"""gate-extreme-bot — CLI entry point.

CLI scaffold. The strategy, risk, execution and backtest layers are built, but
nothing is wired into a run loop yet — every run mode reports its readiness state
and exits. No orders can be placed by this file.

    python main.py --status
    python main.py --mode paper
    python main.py --mode backtest
    python main.py --mode live --confirm-live
"""

from __future__ import annotations

import argparse
import asyncio
import sys

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
    ("10", "Paper trading loop",          False),
    ("11", "Dashboard + database",         False),
    ("12", "Testing",                      False),
    ("13", "Paper trading validation",     False),
    ("14", "Live readiness",               False),
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        cfg = load_config(run_mode=args.mode, confirm_live=args.confirm_live)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.status or args.positions or args.stats:
        print_status(cfg)
        exit_code = 0
        if args.positions:
            exit_code = asyncio.run(show_positions(cfg))
        if args.stats:
            print("\n--stats: requires Phase 11 (database + analytics). Not implemented yet.")
        return exit_code

    print_status(cfg)

    if args.mode == "live" and not cfg.live_enabled:
        print("\nLive mode requested but the safety gate is CLOSED.")
        print("All three are required: DRY_RUN=false in .env, --mode live, --confirm-live.")
        print("No orders will be sent.")

    print(f"\nPhases 1-9 complete. The '{args.mode}' engine arrives in a later phase; "
          "nothing was traded.")
    print("Architecture and verified API findings: docs/ARCHITECTURE.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
