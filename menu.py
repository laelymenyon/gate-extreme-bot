"""Interactive control panel (TUI) for gate-extreme-bot.

``python main.py --menu`` opens a numbered menu over an SSH terminal. It adds **no new
trading logic**: every action delegates to an existing function, command, or safety
mechanism, and it removes none of the existing barriers.

The rules this module obeys:

* **Read-only stays read-only.** Account balance, positions, open orders, tickers, logs,
  status, risk settings, connectivity and preflight only ever issue GET-style calls
  through the Phase 2 client (whose write-guard refuses any state change on its own).
* **Live trading keeps every barrier.** "Start Live Bot" still requires ``DRY_RUN=false``
  in ``.env``, a typed confirmation (the panel's stand-in for ``--confirm-live``), the
  live runner's own gate check, its own preflight GO requirement, and the client's
  write-guard. There is no ``--force``, no bypass, no fallback to simulation.
* **Emergency Flatten uses the existing safe close.** It market-closes through
  ``OrderManager.close_position`` (reduce-only, ``close=True``), re-reads the position
  until it is proven flat, and then cancels the symbol's resting protective orders —
  the same sequence ``live/loop.py`` uses to flatten. Like every write path, it is
  refused by the write-guard unless the safety gate is open, so it refuses honestly when
  ``DRY_RUN=true`` instead of trying to get around anything.
* **Kill switches go through ``RiskManager``.** Tripping latches the persisted ``manual``
  breaker; resetting calls the existing ``reset()`` (which re-baselines what it cleared)
  — both only after a typed confirmation.
* **No secrets are ever printed.** Only presence/absence of credentials is shown, and the
  structured logger redacts keys anyway.
* **The menu survives errors.** Every handler runs under a guard that reports the failure
  as a line and returns to the menu; a broken API, a missing log file, or a git hiccup
  never crashes the panel.
* **The header shows the safety state.** LIVE-READY / LIVE-ARMED (preflight NO-GO) /
  LOCKED (dry run) / HALTED (kill switch latched), plus the last account-connectivity
  probe result, persisted kill switches, and any running live-bot process.

Every handler is ``handler(cfg, session, io)``; async handlers are awaited by the
dispatcher. ``session`` is a plain dict the menu keeps between actions.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import signal
import subprocess
import sys
import time
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import config
import main  # reuse the existing CLI entry points — never reimplement them

__all__ = [
    "MenuIO",
    "run_menu",
    "MENU",
    "DISPATCH",
    "find_bot_processes",
]

_LOG_TAIL = 40
_MAX_TICKERS = 31

#: ANSI foreground colours (16-colour palette — the portable default on every Linux
#: terminal, including the busybox/limited palettes found on phone SSH clients).
_FG = {
    "black": 30, "red": 31, "green": 32, "yellow": 33, "blue": 34,
    "magenta": 35, "cyan": 36, "white": 37,
    "bright_black": 90, "bright_red": 91, "bright_green": 92,
    "bright_yellow": 93, "bright_blue": 94, "bright_magenta": 95,
    "bright_cyan": 96, "bright_white": 97,
}


def _colors_enabled(print_fn: Callable[..., Any]) -> bool:
    """Colours only when stdout is a real terminal and the user has not opted out.

    ``NO_COLOR`` (the de-facto standard) disables them unconditionally; a non-TTY
    destination (pipe, file, captured test output) renders plain text. A custom
    ``print_fn`` is assumed to be a capture/bridge and stays plain.
    """
    if os.environ.get("NO_COLOR", ""):
        return False
    if print_fn is print:
        try:
            return bool(sys.stdout.isatty())
        except (AttributeError, ValueError):
            return False
    return False


class MenuIO:
    """The panel's only I/O seam: tests script stdin and capture output through it.

    ``color`` defaults to auto-detection (TTY + no ``NO_COLOR``); pass ``True``/``False``
    to force a choice (used by tests and by embedders).
    """

    def __init__(self, input_fn: Callable[[str], str] = input,
                 print_fn: Callable[..., Any] = print,
                 *, color: bool | None = None) -> None:
        self._input = input_fn
        self._print = print_fn
        self._color = _colors_enabled(print_fn) if color is None else bool(color)
        if os.environ.get("NO_COLOR", ""):
            self._color = False

    def ask(self, prompt: str = "") -> str:
        return str(self._input(prompt))

    def out(self, *args: Any, **kwargs: Any) -> None:
        self._print(*args, **kwargs)

    def clear_screen(self) -> str:
        """The clear-screen control sequence, or '' when colours/controls are off.

        Gated on the same capability as the colours so a piped or captured run never
        emits terminal control codes into a file.
        """
        return "\033[2J\033[H" if self._color else ""

    # --- ANSI styling (no-op when colours are disabled) --------------------

    def paint(self, text: str, *, fg: str | None = None, bold: bool = False) -> str:
        """Wrap ``text`` in ANSI codes; returns ``text`` unchanged when disabled."""
        if not self._color or not text:
            return text
        codes: list[str] = []
        if bold:
            codes.append("1")
        if fg in _FG:
            codes.append(str(_FG[fg]))
        if not codes:
            return text
        return f"\033[{';'.join(codes)}m{text}\033[0m"

    def title(self, text: str) -> str:
        return self.paint(text, fg="cyan", bold=True)

    def section(self, text: str, fg: str) -> str:
        return self.paint(text, fg=fg, bold=True)

    def number(self, text: str) -> str:
        return self.paint(text, bold=True)

    def warning(self, text: str) -> str:
        return self.paint(text, fg="red")

    def caution(self, text: str) -> str:
        return self.paint(text, fg="yellow")

    def success(self, text: str) -> str:
        return self.paint(text, fg="green")

    def subtle(self, text: str) -> str:
        return self.paint(text, fg="bright_black")


# --- process + connectivity introspection ---------------------------------

def find_bot_processes() -> list[tuple[int, str]]:
    """Running live-bot processes: ``python .../main.py --mode live ...``.

    The panel itself (``--menu``) is excluded. Returns ``(pid, cmdline)`` pairs. This is
    the only mechanism "Stop Bot" uses: a SIGINT to the live runner is the same signal
    Ctrl-C sends it, and the runner's shutdown path disarms the dead-man switch safely.
    """
    found: list[tuple[int, str]] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return found
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            raw = Path(f"/proc/{entry}/cmdline").read_bytes()
        except OSError:
            continue
        tokens = [t for t in raw.decode("utf-8", "replace").split("\0") if t]
        if not tokens or "--menu" in tokens:
            continue
        if any(t.endswith("main.py") for t in tokens) \
                and "--mode" in tokens and "live" in tokens:
            found.append((int(entry), " ".join(tokens)[:120]))
    return found


def _now_hhmm() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M")


async def _probe_connectivity(cfg: Any, symbol: str | None) -> str:
    """A quick read-only probe: signed account read + one ticker. Places nothing."""
    from exchange.gate_client import GateFuturesClient

    symbol = symbol or str(list(cfg.get("universe.symbols"))[0])
    async with GateFuturesClient(cfg) as client:
        account = await client.get_account()
        ticker = await client.get_ticker(symbol)
    total = float(account.get("total", 0.0) or 0.0)
    currency = str(account.get("currency", "USDT"))
    mark = ticker.get("mark_price", "?")
    return (f"OK — {total:.2f} {currency} total, {symbol} mark={mark} "
            f"(checked {_now_hhmm()} UTC)")


def _startup_probe(cfg: Any, session: dict[str, Any]) -> None:
    """Best-effort connectivity probe run once when the panel opens."""
    if not cfg.credentials.present:
        session["connectivity"] = "not checked (no API keys in .env)"
        return
    try:
        session["connectivity"] = asyncio.run(_probe_connectivity(cfg, session.get("symbol")))
    except Exception as exc:  # noqa: BLE001 — a failed probe must never block the panel
        label = getattr(exc, "label", None) or type(exc).__name__
        message = getattr(exc, "message", None) or str(exc)
        session["connectivity"] = f"FAILED — {label}: {message[:70]}"


# --- the header ------------------------------------------------------------

def _status_badge(cfg: Any, session: dict[str, Any]) -> str:
    kill = session.get("kill_switches") or {}
    if kill:
        return f"HALTED — kill switch latched ({', '.join(sorted(kill))})"
    if cfg.env_dry_run:
        return "LOCKED — DRY RUN, no real orders"
    if session.get("preflight") == "GO":
        return "LIVE-READY — gate armed, preflight GO"
    if session.get("preflight") == "NO-GO":
        return "LIVE-ARMED — preflight NO-GO (see item 13)"
    return "LIVE-ARMED — preflight not audited (see item 13)"


def _status_color(cfg: Any, session: dict[str, Any]) -> str:
    """The STATUS badge colour: red HALTED, yellow LOCKED/armed, green live-ready."""
    if session.get("kill_switches"):
        return "red"
    if cfg.env_dry_run:
        return "yellow"
    if session.get("preflight") == "GO":
        return "green"
    return "yellow"  # LIVE-ARMED: preflight NO-GO or not yet audited


def _render_header(cfg: Any, session: dict[str, Any], io: MenuIO) -> str:
    """The framed title, the branding line, and the live safety state."""
    inner = 46
    kill = session.get("kill_switches") or {}
    procs = session.get("processes") or []

    creds = io.paint("present" if cfg.credentials.present else "EMPTY",
                     fg="green" if cfg.credentials.present else "red", bold=True)
    dry = io.paint(str(cfg.env_dry_run).lower().ljust(5),
                   fg="yellow" if cfg.env_dry_run else "green")
    preflight = session.get("preflight", "not audited")
    preflight_color = {"GO": "green", "NO-GO": "red"}.get(preflight, "yellow")
    api = str(session.get("connectivity", "not checked"))
    api_color = "green" if api.startswith("OK") else \
        ("red" if "FAILED" in api else "bright_black")
    kill_part = io.warning(", ".join(sorted(kill))) if kill else io.subtle("none")
    procs_part = (io.success("pid " + " ".join(str(p) for p, _ in procs)) if procs
                  else io.subtle("not running"))

    def framed(text: str) -> str:
        return io.title(f"║{text.center(inner)}║")

    return "\n".join([
        io.title("╔" + "═" * inner + "╗"),
        framed("GATE EXTREME BOT"),
        framed("LIVE TRADING PANEL"),
        io.paint(f"║{'BY KANGSEBLAK'.center(inner)}║", fg="bright_black"),
        io.title("╚" + "═" * inner + "╝"),
        io.paint(f"STATUS   : {_status_badge(cfg, session)}",
                 fg=_status_color(cfg, session), bold=True),
        f"DRY_RUN  : {dry}   CREDS : {creds}",
        f"PREFLIGHT: {io.paint(preflight.ljust(18), fg=preflight_color)} KILL  : {kill_part}",
        f"BOT PROC : {procs_part}",
        f"API      : {io.paint(api, fg=api_color)}",
        io.paint("─" * 48, fg="bright_black"),
    ])


# --- shared helpers --------------------------------------------------------

def _pause(io: MenuIO) -> bool:
    """Wait for Enter. Returns False when the caller should exit the menu (EOF/^C)."""
    try:
        io.ask("\n  " + io.subtle("Press Enter to return to the menu..."))
    except (EOFError, KeyboardInterrupt):
        return False
    return True


def _live_cfg() -> Any:
    """The live-switch config. ``confirm_live=True`` stands for the panel's own typed
    confirmation: the menu still refuses unless the operator typed the phrase, and
    ``live_enabled`` still requires ``DRY_RUN=false`` in ``.env``."""
    return config.load_config(run_mode="live", confirm_live=True)


def _live_denied(io: MenuIO, reason: str) -> None:
    io.out(io.warning(f"\n  refused: {reason}"))
    io.out(io.subtle("  All three are still required: DRY_RUN=false in .env, the panel's typed"))
    io.out(io.subtle("  confirmation (--confirm-live), and the live runner's own preflight GO."))
    io.out(io.warning("  No order was sent."))


def _live_barrier(io: MenuIO, cfg: Any) -> Any | None:
    """Resolve the live config, or explain why live actions are impossible.

    Returns None when live trading is not reachable — the caller must abort without
    touching the exchange. ``cfg.env_dry_run`` is the panel's own snapshot of ``.env``,
    checked before anything is loaded, and ``_live_cfg()`` re-reads ``.env`` fresh so a
    live action is always judged on the current file.
    """
    if cfg.env_dry_run:
        _live_denied(io, "DRY_RUN=true in .env")
        return None
    try:
        live_cfg = _live_cfg()
    except config.ConfigError as exc:
        _live_denied(io, str(exc))
        return None
    if not live_cfg.live_enabled:
        _live_denied(io, "the safety gate is CLOSED")
        return None
    return live_cfg


# --- ACCOUNT (all read-only) -----------------------------------------------

async def account_balance(cfg: Any, session: dict[str, Any], io: MenuIO) -> None:
    from exchange.gate_client import GateAPIError, GateFuturesClient

    if not cfg.credentials.present:
        io.out("\n  Account reads require GATE_API_KEY/GATE_API_SECRET in .env.")
        return
    async with GateFuturesClient(cfg) as client:
        try:
            account = await client.get_account()
        except GateAPIError as exc:
            io.out(io.warning(f"\n  Gate.io error: {exc}"))
            return
        except Exception as exc:  # noqa: BLE001 — transport failures (timeout/connect)
            io.out(io.warning(f"\n  exchange read failed: {type(exc).__name__}: {exc}"))
            return
    io.out(f"\n  Total          : {account.get('total', '?')} {account.get('currency', '')}")
    io.out(f"  Available      : {account.get('available', '?')}")
    io.out(f"  Unrealised PnL : {account.get('unrealised_pnl', '?')}")
    io.out(f"  Position margin: {account.get('position_margin', '?')}")


async def positions(cfg: Any, session: dict[str, Any], io: MenuIO) -> None:
    """The existing ``--positions`` path, unchanged."""
    await main.show_positions(cfg)


async def open_orders(cfg: Any, session: dict[str, Any], io: MenuIO) -> None:
    from exchange.gate_client import GateAPIError, GateFuturesClient

    if not cfg.credentials.present:
        io.out("\n  Order reads require GATE_API_KEY/GATE_API_SECRET in .env.")
        return
    async with GateFuturesClient(cfg) as client:
        try:
            normal = await client.list_open_orders()
            price = await client.list_price_orders()
        except GateAPIError as exc:
            io.out(io.warning(f"\n  Gate.io error: {exc}"))
            return
        except Exception as exc:  # noqa: BLE001 — transport failures (timeout/connect)
            io.out(io.warning(f"\n  exchange read failed: {type(exc).__name__}: {exc}"))
            return
    io.out(f"\n  {len(normal)} open order(s), {len(price)} price-triggered (stops/TPs):")
    for order in normal:
        io.out(f"    {order.get('contract', '?'):<14} id={order.get('id')} "
               f"size={order.get('size')} price={order.get('price')} tif={order.get('tif')}")
    for order in price:
        trigger = order.get("trigger", {})
        initial = order.get("initial", {})
        io.out(f"    {initial.get('contract', '?'):<14} TRIGGER id={order.get('id')} "
               f"at={trigger.get('price')} size={initial.get('size')} rule={trigger.get('rule')}")


async def market_ticker(cfg: Any, session: dict[str, Any], io: MenuIO) -> None:
    from exchange.gate_client import GateAPIError, GateFuturesClient

    symbols = list(cfg.get("universe.symbols"))[:_MAX_TICKERS]
    io.out(f"\n  reading {len(symbols)} ticker(s) — read-only, please wait...")
    async with GateFuturesClient(cfg) as client:
        for symbol in symbols:
            try:
                ticker = await client.get_ticker(symbol)
            except GateAPIError as exc:
                io.out(io.warning(f"  {symbol:<12} unreadable [{exc.label}]"))
                continue
            except Exception as exc:  # noqa: BLE001 — transport failures (timeout/connect)
                io.out(io.warning(f"  {symbol:<12} unreadable [{type(exc).__name__}]"))
                continue
            io.out(f"  {symbol:<12} last={ticker.get('last', '?'):<12} "
                   f"mark={ticker.get('mark_price', '?'):<12} "
                   f"chg={ticker.get('change_percentage', '?')}% "
                   f"fund={ticker.get('funding_rate', '?')} "
                   f"24hq={ticker.get('volume_24h_quote', '?')}")


# --- TRADING ---------------------------------------------------------------

def start_live_bot(cfg: Any, session: dict[str, Any], io: MenuIO) -> None:
    """Start the existing live runner — every barrier stays in place.

    ``DRY_RUN=false`` in .env, then the panel's own typed confirmation, then
    ``main.run_live_mode`` -> ``live/loop.run_live``, which re-checks the gate and refuses
    to start unless its own preflight reports GO over real account reads.
    """
    live_cfg = _live_barrier(io, cfg)
    if live_cfg is None:
        return

    io.out("\n  This starts the Phase 15 live runner on the REAL account.")
    io.out("  Entries are post-only, every fill is protected with a verified stop-loss,")
    io.out("  and the runner refuses to start unless preflight reports GO.")
    answer = io.ask("  Type exactly  LIVE SEND  to start (anything else aborts): ")
    if answer.strip() != "LIVE SEND":
        io.out(io.warning("\n  Aborted. No order was sent."))
        return

    args = types.SimpleNamespace(
        symbol=session.get("symbol"),
        steps=None,
        poll_seconds=session.get("poll_seconds", 5.0),
    )
    io.out("\n  Starting the live runner. Ctrl-C stops it (the runner disarms the")
    io.out("  dead-man switch on stop). This blocks until it stops.\n")
    try:
        code = main.run_live_mode(live_cfg, args)
    except KeyboardInterrupt:
        io.out("\n  Stopped by Ctrl-C — the runner's shutdown already ran. Returning to menu.")
        code = 0
    summary = (f"\n  live runner exited with code {code} "
               f"(0=ran, 1=runtime error, 2=refused, 3=preflight NO-GO).")
    io.out(io.success(summary) if code == 0 else io.caution(summary))


def stop_bot(cfg: Any, session: dict[str, Any], io: MenuIO) -> None:
    """Send the running live bot the same signal Ctrl-C would: SIGINT. Read-only up to
    the typed confirmation; the runner's own shutdown path handles the dead-man switch."""
    procs = find_bot_processes()
    session["processes"] = procs
    if not procs:
        io.out("\n  No running live-bot process was found.")
        io.out("  (The panel itself is excluded. A bot started from this panel stops")
        io.out("  with Ctrl-C inside its run.)")
        return
    io.out(f"\n  {len(procs)} live-bot process(es) found:")
    for pid, cmdline in procs:
        io.out(f"    pid {pid}: {cmdline}")
    answer = io.ask("  Type exactly  STOP SEND  to send SIGINT (graceful, like Ctrl-C): ")
    if answer.strip() != "STOP SEND":
        io.out(io.warning("\n  Aborted. No signal was sent."))
        return
    for pid, _cmdline in procs:
        try:
            os.kill(pid, signal.SIGINT)
            io.out(io.success(f"    SIGINT sent to pid {pid}."))
            io.out("    The runner disarms the dead-man switch on stop, so a resting")
            io.out("    stop-loss remains in force.")
        except ProcessLookupError:
            io.out(io.caution(f"    pid {pid} is already gone."))
        except PermissionError:
            io.out(io.warning(f"    pid {pid}: permission denied — run the panel as the bot's user."))


def bot_status(cfg: Any, session: dict[str, Any], io: MenuIO) -> None:
    """The existing ``--status`` report, plus what the panel has observed."""
    from database.models import TradeStore

    main.print_status(cfg)
    store = TradeStore.from_config(cfg)
    kill = store.kill_switches()
    procs = find_bot_processes()
    session["processes"] = procs
    io.out("\n  Kill switches : " + (", ".join(sorted(kill)) if kill else "none"))
    io.out("  Bot process   : " + ("pid " + " ".join(str(p) for p, _ in procs)
                                   if procs else "not running"))
    io.out(f"  API           : {session.get('connectivity', 'not checked')}")
    io.out(f"  Preflight     : {session.get('preflight', 'not audited')} (run item 13)")


def trade_history(cfg: Any, session: dict[str, Any], io: MenuIO) -> None:
    from database.models import TradeStore
    from monitoring.dashboard import Dashboard

    store = TradeStore.from_config(cfg)
    trades = store.trades()
    if not trades:
        io.out(f"\n  No trades recorded yet in {store.path}.")
        io.out("  Paper, backtest or live runs write their history here.")
        return
    io.out(f"\n  {len(trades)} trade(s) on record — most recent first:")
    for record in reversed(trades[-15:]):
        when = datetime.fromtimestamp(record.timestamp, tz=timezone.utc).strftime("%m-%d %H:%M")
        # .6g keeps normal-scale prices decimal (65000 -> 65000) instead of 6.5e+04,
        # while still being compact for the 0.00xx metals/FX pairs.
        io.out(f"  {when}  {record.symbol:<10} {record.side:<5} {record.size:>6}  "
               f"{record.entry_price:.6g} -> {record.exit_price:.6g}  "
               f"{record.r_multiple:+.2f}R  {record.pnl:+.2f}  "
               f"{record.exit_reason:<12} {record.mode}")
    io.out("\n" + Dashboard.from_config(cfg, store).render())


# --- RISK & SAFETY ---------------------------------------------------------

def risk_settings(cfg: Any, session: dict[str, Any], io: MenuIO) -> None:
    from risk.risk_manager import RiskManager, RiskParams, SqliteRiskStore

    params = RiskParams.from_config(cfg)
    manager = RiskManager(
        params, SqliteRiskStore(str(cfg.get("database.path", "data/trades.db"))),
    )
    state = manager.status()
    io.out("\n  configured limits (read-only)")
    io.out(f"    risk per trade           : {params.per_trade * 100:.2f}% of equity")
    io.out(f"    max daily loss           : {params.max_daily_loss * 100:.2f}%  (clears at UTC midnight)")
    io.out(f"    max drawdown             : {params.max_drawdown * 100:.2f}%  (manual reset only)")
    io.out(f"    max consecutive losses   : {params.max_consecutive_losses}")
    io.out(f"    max open positions       : {params.max_open_positions}")
    io.out(f"    cooldown after loss/win  : "
           f"{params.cooldown_after_loss_seconds:.0f}s / {params.cooldown_after_win_seconds:.0f}s")
    io.out("  current state")
    eq = state["equity"]
    peak = state["peak_equity"]
    dd = state["drawdown"]
    dl = state["daily_loss"]
    io.out(f"    equity / peak            : "
           f"{eq if eq is None else f'{eq:.2f}'} / {peak if peak is None else f'{peak:.2f}'}")
    io.out(f"    drawdown / daily loss    : "
           f"{'n/a' if dd is None else f'{dd * 100:.2f}%'} / "
           f"{'n/a' if dl is None else f'{dl * 100:.2f}%'}")
    io.out(f"    trades today / streak    : {state['trades_today']} / {state['consecutive_losses']}")
    tripped = state["tripped"]
    if tripped:
        io.out(io.warning("    KILL SWITCHES LATCHED:"))
        for name, reason in sorted(tripped.items()):
            io.out(f"      {name}: {reason}")
    else:
        io.out("    kill switches            : none latched")


def kill_switch(cfg: Any, session: dict[str, Any], io: MenuIO) -> None:
    from risk.risk_manager import RiskManager, RiskParams, SqliteRiskStore

    manager = RiskManager(
        RiskParams.from_config(cfg),
        SqliteRiskStore(str(cfg.get("database.path", "data/trades.db"))),
    )
    switches = manager.kill_switches
    if switches:
        io.out("\n  latched kill switches:")
        for breaker, switch in sorted(switches.items(), key=lambda kv: str(kv[0])):
            note = "manual reset required" if switch.manual_reset_required \
                else "clears at UTC midnight"
            io.out(f"    {breaker.value}: {switch.reason[:80]}  ({note})")
    else:
        io.out("\n  no kill switch is latched.")
    io.out("\n  The kill switch halts NEW entries only. It never closes an open position;")
    io.out("  use item 11 (Emergency Flatten) for that.")

    action = io.ask("  [T]rip the manual kill switch, [R]eset latches, [0] back: ").strip().lower()
    if action == "t":
        answer = io.ask("  Type exactly  TRIP SEND  to latch the manual kill switch: ")
        if answer.strip() != "TRIP SEND":
            io.out(io.warning("\n  Aborted. No latch was set."))
            return
        manager.trip("manual trip from the control panel", time.time())
        session["kill_switches"] = {"manual": "manual trip from the control panel"}
        io.out(io.caution("\n  The manual kill switch is LATCHED and persisted to SQLite."))
        io.out("  New entries are blocked until a human resets it")
        io.out("  (drawdown re-baselining included).")
    elif action == "r":
        io.out(io.caution("\n  Resetting clears every latch and RE-BASELINES what they measured:"))
        io.out("  the drawdown high-water mark moves to current equity.")
        answer = io.ask("  Type exactly  RESET SEND  to clear all latches: ")
        if answer.strip() != "RESET SEND":
            io.out(io.warning("\n  Aborted. Latches remain in force."))
            return
        cleared = manager.reset()
        session["kill_switches"] = manager.kill_switches
        names = ", ".join(breaker.value for breaker in cleared) if cleared else "nothing to clear"
        io.out(io.success(f"\n  Cleared: {names}."))
    else:
        io.out("\n  No change.")


async def emergency_flatten(cfg: Any, session: dict[str, Any], io: MenuIO) -> None:
    """Emergency flatten through the existing safe close mechanism.

    Mirrors ``live/loop._flatten_for_session``: market-close (reduce-only, ``close=True``
    via ``OrderManager.close_position``), re-read until proven flat, then cancel the
    symbol's resting protective orders. The Phase 2 write-guard refuses the whole thing
    unless the safety gate is open — that barrier is never bypassed.
    """
    from exchange.gate_client import GateAPIError, GateFuturesClient
    from execution.order_manager import ExecutionParams, OrderManager

    live_cfg = _live_barrier(io, cfg)
    if live_cfg is None:
        # _live_barrier already explained the refusal. One extra line states why this
        # item in particular is impossible: flattening is a state change, and the
        # exchange write-guard refuses those while the gate is shut.
        io.out(io.subtle("  (Flattening is a state change, refused by the exchange write-guard"))
        io.out(io.subtle("   while the gate is shut — the existing barrier, not a panel limitation.)"))
        return

    async with GateFuturesClient(live_cfg) as client:
        try:
            raw_positions = await client.list_positions(holding=True)
        except GateAPIError as exc:
            io.out(f"\n  Gate.io error: {exc}")
            return
        except Exception as exc:  # noqa: BLE001 — transport failures (timeout/connect)
            io.out(f"\n  exchange read failed: {type(exc).__name__}: {exc}")
            return
        held = [p for p in raw_positions if int(p.get("size", 0) or 0) != 0]
        if not held:
            io.out(io.subtle("\n  No open positions. Nothing to flatten."))
            return

        io.out(f"\n  {len(held)} open position(s):")
        for position in held:
            io.out(f"    {position.get('contract'):<14} size={position.get('size')} "
                   f"entry={position.get('entry_price')} mark={position.get('mark_price')} "
                   f"liq={position.get('liq_price')}")
        answer = io.ask(f"  Type exactly  FLATTEN SEND  to market-close {len(held)} "
                        "position(s): ")
        if answer.strip() != "FLATTEN SEND":
            io.out(io.warning("\n  Aborted. Nothing was closed."))
            return

        manager = OrderManager.for_config(
            live_cfg, client=client,
            params=ExecutionParams.from_config(live_cfg),
        )
        nonce = int(time.time())
        for position in held:
            symbol = str(position.get("contract"))
            io.out(f"\n  flattening {symbol}...")
            try:
                record = await manager.close_position(
                    symbol, nonce, reason="emergency flatten (panel)",
                )
                io.out(f"    close: {record.summary()}")
                remaining = await manager.position_size(symbol)
                if remaining != 0:
                    io.out(io.warning(f"    WARNING: {remaining} contracts still open. The resting"))
                    io.out(io.warning("    stop (if any) remains in force; verify the account now."))
                else:
                    resting = await client.list_price_orders(symbol)
                    for order in resting:
                        await client.cancel_price_order(order.get("id"))
                    io.out(io.success(f"    flat confirmed; {len(resting)} resting protective "
                                      "order(s) cancelled."))
            except Exception as exc:  # noqa: BLE001 — report, never crash the panel
                io.out(io.warning(f"    flatten failed: {type(exc).__name__}: {exc}"))
                io.out(io.warning("    the resting stop (if any) is left in force; verify the account."))
        io.out(io.success("\n  Emergency flatten finished. Confirm with items 1-3."))


# --- SYSTEM ----------------------------------------------------------------

def connectivity_check(cfg: Any, session: dict[str, Any], io: MenuIO) -> None:
    """The existing ``--connectivity`` read-only production check, verbatim."""
    import live.verify

    code = asyncio.run(live.verify.check_connectivity(
        cfg, symbol=session.get("symbol"), print_fn=io.out,
    ))
    if code == 0:
        session["connectivity"] = f"OK (full check at {_now_hhmm()} UTC)"
    else:
        session["connectivity"] = "FAILED — see the check output above"


async def preflight_check(cfg: Any, session: dict[str, Any], io: MenuIO) -> None:
    """The existing ``--preflight`` audit: real account reads, GO/NO-GO, read-only."""
    from database.models import TradeStore
    from execution.preflight import preflight

    store = TradeStore.from_config(cfg)
    account = await main.read_account_snapshot(cfg)
    report = preflight(
        cfg, store, account=account,
        validation=main.observed_report(store, cfg),
    )
    session["preflight"] = "GO" if report.ready else "NO-GO"
    io.out("\n" + report.render())


def view_logs(cfg: Any, session: dict[str, Any], io: MenuIO) -> None:
    path = Path(str(cfg.get("logging.file", "logs/bot.log")))
    if not path.exists():
        io.out(io.subtle(f"\n  No log file at {path} yet. Runs write here once logging starts."))
        return
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    tail = lines[-_LOG_TAIL:]
    io.out(f"\n  {path} — last {len(tail)} of {len(lines)} line(s):")
    for line in tail:
        io.out(f"  {line}")


def update_from_github(cfg: Any, session: dict[str, Any], io: MenuIO) -> None:
    """Fetch + fast-forward ``origin/main``. A system command behind typed confirmation."""
    root = config.ROOT

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=str(root), capture_output=True, text=True, timeout=120,
        )

    try:
        head = git("rev-parse", "--short", "HEAD")
        status = git("status", "--short")
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        io.out(f"\n  git is unavailable here: {exc}")
        return

    io.out(f"\n  repository : {root}")
    io.out(f"  current HEAD: {head.stdout.strip() or 'unknown'}")
    if status.stdout.strip():
        io.out("  local changes present (a fast-forward will not overwrite them):")
        for line in status.stdout.splitlines()[:5]:
            io.out(f"    {line}")
    else:
        io.out("  working tree clean")
    answer = io.ask("  Type exactly  PULL SEND  to fetch and fast-forward origin/main: ")
    if answer.strip() != "PULL SEND":
        io.out(io.warning("\n  Aborted. Nothing was pulled."))
        return
    result = git("pull", "--ff-only", "origin", "main")
    io.out(result.stdout)
    if result.returncode != 0:
        io.out(io.warning(f"  git pull failed ({result.returncode}):"))
        io.out(result.stderr.strip() or io.subtle("  (no error text)"))
        io.out(io.warning("  Nothing was changed. Run it manually to see the full error."))
    else:
        new_head = git("rev-parse", "--short", "HEAD")
        io.out(io.success(f"  updated — new HEAD: {new_head.stdout.strip()}"))


# --- the dispatch table ----------------------------------------------------

#: Human annotation shown next to each item: what the action may do.
_NOTES: dict[str, str] = {
    "1": "read-only", "2": "read-only", "3": "read-only", "4": "read-only",
    "5": "REAL ORDERS — full barriers", "6": "stops the live bot process",
    "7": "read-only", "8": "read-only", "9": "read-only",
    "10": "halts NEW entries (persisted latch)", "11": "closes open positions",
    "12": "read-only", "13": "read-only", "14": "read-only",
    "15": "git pull (fast-forward only)",
}

#: Every menu entry: (key, label, category, handler). Handler signature is
#: ``handler(cfg, session, io)`` and may be a coroutine.
MENU: list[tuple[str, str, str, Callable[..., Any]]] = [
    ("1",  "Account Balance",    "ACCOUNT",      account_balance),
    ("2",  "Positions",          "ACCOUNT",      positions),
    ("3",  "Open Orders",        "ACCOUNT",      open_orders),
    ("4",  "Market / Ticker",    "ACCOUNT",      market_ticker),
    ("5",  "Start Live Bot",     "TRADING",      start_live_bot),
    ("6",  "Stop Bot",           "TRADING",      stop_bot),
    ("7",  "Bot Status",         "TRADING",      bot_status),
    ("8",  "Trade History",      "TRADING",      trade_history),
    ("9",  "Risk Settings",      "RISK & SAFETY", risk_settings),
    ("10", "Kill Switch",        "RISK & SAFETY", kill_switch),
    ("11", "Emergency Flatten",  "RISK & SAFETY", emergency_flatten),
    ("12", "Connectivity Check", "SYSTEM",       connectivity_check),
    ("13", "Preflight Check",    "SYSTEM",       preflight_check),
    ("14", "View Logs",          "SYSTEM",       view_logs),
    ("15", "Update From GitHub", "SYSTEM",       update_from_github),
]

DISPATCH: dict[str, tuple[str, Callable[..., Any]]] = {
    key: (label, handler) for key, label, _category, handler in MENU
}


#: Section-header colours: cyan read-only, green trading, yellow risk, blue system.
_SECTION_COLORS = {
    "ACCOUNT": "cyan", "TRADING": "green", "RISK & SAFETY": "yellow", "SYSTEM": "blue",
}

#: Per-item note colours: dim for read-only, red/yellow where the action has teeth.
_NOTE_COLORS = {
    "1": "bright_black", "2": "bright_black", "3": "bright_black", "4": "bright_black",
    "5": "red", "6": "yellow", "7": "bright_black", "8": "bright_black",
    "9": "bright_black", "10": "yellow", "11": "red",
    "12": "bright_black", "13": "bright_black", "14": "bright_black", "15": "yellow",
}


def _render_menu(io: MenuIO) -> str:
    lines: list[str] = []
    for category in ("ACCOUNT", "TRADING", "RISK & SAFETY", "SYSTEM"):
        lines.append("  " + io.section(category, _SECTION_COLORS[category]))
        for key, label, cat, _handler in MENU:
            if cat != category:
                continue
            note = _NOTES.get(key, "")
            note_painted = io.paint(note, fg=_NOTE_COLORS.get(key)) if note else ""
            # Pad the plain key first, then paint, so columns line up with or without colour.
            lines.append(f"  {io.number(f'{key:<3}')}{label:<22}{note_painted}")
    lines.append("")
    lines.append(f"  {io.number('0')}   Exit")
    return "\n".join(lines)


# --- the loop --------------------------------------------------------------

def run_menu(cfg: Any, *, symbol: str | None = None, poll_seconds: float = 5.0,
             io: MenuIO | None = None, probe: bool = True) -> int:
    """Run the control panel until the operator exits. Returns the exit code."""
    io = io or MenuIO()
    session: dict[str, Any] = {
        "symbol": symbol,
        "poll_seconds": poll_seconds,
        "preflight": "not audited",
        "connectivity": "not checked",
        "processes": [],
        "kill_switches": {},
    }

    from database.models import TradeStore

    store = TradeStore.from_config(cfg)
    session["kill_switches"] = store.kill_switches()
    session["processes"] = find_bot_processes()
    if probe:
        _startup_probe(cfg, session)

    while True:
        io.out(io.clear_screen() + _render_header(cfg, session, io))
        io.out(_render_menu(io))
        io.out(io.subtle("  Select an action by number. Press 0 to exit."))
        try:
            choice = io.ask("  Choice: ").strip()
        except (EOFError, KeyboardInterrupt):
            io.out(io.subtle("\n  Interrupted — goodbye. This session placed no orders."))
            return 0
        if choice == "0":
            return 0
        entry = DISPATCH.get(choice)
        if entry is None:
            io.out(io.warning(f"\n  '{choice}' is not a menu item."))
            if not _pause(io):
                return 0
            continue

        label, handler = entry
        io.out(io.paint(f"\n  --- {label} ---", bold=True))
        try:
            result = handler(cfg, session, io)
            if inspect.isawaitable(result):
                asyncio.run(result)
        except KeyboardInterrupt:
            io.out(io.caution("\n  Interrupted — the runner's shutdown already ran. Returning to menu."))
        except Exception as exc:  # noqa: BLE001 — one failing action must not kill the panel
            io.out(io.warning(f"\n  {label} failed cleanly: {type(exc).__name__}: {exc}"))
            io.out(io.subtle("  No state was changed by the panel itself. See the logs for details."))
        if not _pause(io):
            return 0
