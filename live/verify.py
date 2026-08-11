"""Production connectivity/auth check, and the explicit live-order verification.

PHASE 15 — the two commands an operator runs deliberately, after the gate is open:

* :func:`check_connectivity` — proves the credentials sign correctly and the account is
  readable, without changing anything. It is the answer to "did my keys work, and what
  does the production account actually look like right now?"
* :func:`verify_live_order` — the FINAL barrier before the strategy loop is trusted with
  the account. It places **one** real market order of the minimum contract size,
  immediately protects it with a verified stop-loss, then closes it, so the complete
  production lifecycle — submit -> acknowledge -> fill -> protect -> monitor -> close ->
  record — is witnessed on the real account exactly once, by a human who typed the
  symbol and the word SEND.

Both refuse unless the three switches agree (``DRY_RUN=false``, ``--mode live``,
``--confirm-live``). ``verify_live_order`` adds its own barrier on top: the account must
be flat, funded and reachable, kill-switches must be clear, the contract must be tradable
and affordable, the market must be readable, and the operator must type the exact
confirmation phrase. There is no fallback to simulation anywhere: a supplied
``SimulatedGateway`` is refused by name, and when no client is supplied a real
``GateFuturesClient`` is built.

Nothing here claims success it did not read back, and no failure path abandons the
account: once an order may exist, any exception or unprovable step routes through
:func:`_recover`, which market-closes what is open, leaves any resting stop in force
(the stop is the one thing protecting a position whose close just failed), disarms the
dead-man countdown so the stop is not cancelled behind the operator's back, and then the
command reports exactly what it knows.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from database.models import EquityPoint, TradeRecord, TradeStore
from exchange.gate_client import Contract, GateAPIError, RiskTier
from execution.order_manager import (
    ExecutionParams,
    OrderManager,
    SimulatedGateway,
    idempotency_key,
)
from execution.preflight import AccountSnapshot
from execution.protection import ProtectionEngine, ProtectionParams
from paper.loop import PaperTrade

__all__ = [
    "check_connectivity",
    "verify_live_order",
]

_W = 66


def _rule(width: int = _W) -> str:
    return "=" * width


async def check_connectivity(cfg: Any, *, symbol: str | None = None,
                             client: Any = None,
                             print_fn: Callable[..., Any] = print) -> int:
    """Prove credentials + production reachability. Read-only; places nothing.

    Exit codes: 0 everything readable, 1 auth/read failure, 2 credentials absent.
    """
    print_fn(_rule())
    print_fn("  production connectivity + auth check (READ-ONLY)")
    print_fn(_rule())
    if not getattr(cfg.credentials, "present", False):
        print_fn("  GATE_API_KEY/GATE_API_SECRET are empty.")
        print_fn("  Put real keys in .env, then re-run. Nothing has been sent.")
        print_fn("  This check never places an order.")
        return 2

    symbol = symbol or str(list(cfg.get("universe.symbols"))[0])
    owns_client = client is None
    if owns_client:
        # Imported at call time so tests can patch `exchange.gate_client.GateFuturesClient`
        # and so no client class is bound to this module at import.
        from exchange.gate_client import GateFuturesClient

        client = GateFuturesClient(cfg)
        await client.__aenter__()
    try:
        # 1. authentication — the signed read itself is the proof of the signature.
        try:
            account = await client.get_account()
        except GateAPIError as exc:
            print_fn(f"  auth          : FAILED [{exc.status} {exc.label}] {exc.message}")
            if exc.label == "INVALID_KEY":
                print_fn("                  the signature was accepted but the key was")
                print_fn("                  rejected — check GATE_API_KEY in .env")
            elif exc.label == "INVALID_SIGNATURE":
                print_fn("                  the signature itself was rejected — check")
                print_fn("                  GATE_API_SECRET in .env")
            else:
                print_fn("                  the account could not be read; see error above")
            return 1
        print_fn("  auth          : OK — signed futures account read accepted")

        total = float(account.get("total", 0.0) or 0.0)
        available = float(account.get("available", 0.0) or 0.0)
        currency = str(account.get("currency", "?"))
        print_fn(f"  balance       : {total:.2f} {currency} total, "
                 f"{available:.2f} {currency} available")

        positions = []
        try:
            positions = await client.list_positions(holding=True)
        except GateAPIError as exc:
            print_fn(f"  positions     : unreadable [{exc.status} {exc.label}] {exc.message}")
            return 1
        held = [p for p in positions if int(p.get("size", 0) or 0) != 0]
        print_fn(f"  positions     : {len(held)} open (entry/mark/liq read per position below)")

        open_orders: list[dict] = []
        price_orders: list[dict] = []
        for reader, sink, label in (
            (client.list_open_orders, open_orders, "open orders"),
            (client.list_price_orders, price_orders, "price-triggered orders"),
        ):
            try:
                sink.extend(await reader())
            except GateAPIError as exc:
                print_fn(f"  {label:<12} : unreadable [{exc.status} {exc.label}]")
        print_fn(f"  open orders   : {len(open_orders)} normal, "
                 f"{len(price_orders)} price-triggered (stops/TPs)")

        # 2. the market side of the same production endpoint.
        try:
            contract = await client.get_contract(symbol)
        except GateAPIError as exc:
            print_fn(f"  contract      : unreadable [{exc.status} {exc.label}] {exc.message}")
            return 1
        if isinstance(contract, dict):
            contract = Contract.from_api(contract)
        print_fn(f"  contract      : {symbol} status={contract.status} "
                 f"lev={contract.leverage_max:g}x "
                 f"quanto={contract.quanto_multiplier} "
                 f"min_size={contract.order_size_min}")
        try:
            ticker = await client.get_ticker(symbol)
            print_fn(f"  market        : last={ticker.get('last')} "
                     f"mark={ticker.get('mark_price')} "
                     f"24h_q={ticker.get('volume_24h_quote')}")
        except GateAPIError as exc:
            print_fn(f"  market        : unreadable [{exc.status} {exc.label}]")
            return 1
        try:
            tiers = await client.get_risk_tiers(symbol)
        except GateAPIError as exc:
            print_fn(f"  risk tiers    : unreadable [{exc.status} {exc.label}] {exc.message}")
            return 1
        tiers = [t if isinstance(t, RiskTier) else RiskTier.from_api(t) for t in tiers]
        if not tiers:
            print_fn(f"  risk tiers    : none returned for {symbol} — refusing to continue")
            return 1
        print_fn(f"  risk tiers    : {len(tiers)} for {symbol} "
                 f"(tier-1 mmr={tiers[0].maintenance_rate})")

        for position in held:
            print_fn(f"  position      : {position.get('contract')} size={position.get('size')} "
                     f"entry={position.get('entry_price')} "
                     f"mark={position.get('mark_price')} liq={position.get('liq_price')}")

        print_fn(_rule())
        print_fn("  result: production credentials and market data READ OK. "
                 "No order was placed.")
        return 0
    finally:
        if owns_client and client is not None:
            try:
                await client.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass


async def _read_mark(client: Any, symbol: str) -> float:
    """The mark (or last) price, best effort — used only when fill prices are missing."""
    try:
        ticker = await client.get_ticker(symbol)
        return float(ticker.get("mark_price") or ticker.get("last") or 0.0)
    except Exception:  # noqa: BLE001
        return 0.0


async def _recover(manager: OrderManager, symbol: str, nonce: int,
                   print_fn: Callable[..., Any]) -> None:
    """Best-effort return to a known state after a step that may have moved the account.

    Runs only after an order was (possibly) submitted, so it must never raise: every
    failure inside is reported and swallowed, and the caller says what remains.

    It closes what is open and then disarms the dead-man countdown, so the verified stop
    (if one was placed) survives instead of being cancelled 60 seconds later. It never
    removes a resting protective order: if the close is failing, the stop is the only
    thing protecting the position, and removing it is the one outcome worse than the
    failure being reported.
    """
    try:
        held = abs(await manager.position_size(symbol))
    except Exception:  # noqa: BLE001
        held = 0
    if held != 0:
        print_fn(f"  recovery      : {held} contracts open after the failure — closing...")
        try:
            record = await manager.close_position(
                symbol, f"{nonce}r", reason="verification failure",
            )
            print_fn(f"  recovery      : close submitted — {record.summary()}")
        except Exception as exc:  # noqa: BLE001
            print_fn(f"  recovery      : defensive close FAILED: {type(exc).__name__}: {exc}")
            print_fn("                 the resting stop-loss (if any) is left in force;")
            print_fn("                 check the account now.")
    # If the position is now confirmed flat, remove the symbol's resting protective
    # orders so a stale reduce-only stop cannot fire into a later position — the same
    # cleanup the normal completion path does. If the close is still unconfirmed, leave
    # them: they are the only protection the position has.
    try:
        remaining = abs(await manager.position_size(symbol))
    except Exception:  # noqa: BLE001
        remaining = None
    if remaining == 0:
        try:
            for order in await manager.gateway.list_price_orders(symbol):
                await manager.gateway.cancel_price_order(order.get("id"))
        except Exception as exc:  # noqa: BLE001
            print_fn(f"  recovery      : resting protective orders could not be cancelled: {exc}")
            print_fn("                 a stale stop may fire into a later position; check them.")
    try:
        await manager.gateway.countdown_cancel_all(0, symbol)
    except Exception as exc:  # noqa: BLE001
        print_fn(f"  recovery      : dead-man countdown could not be disarmed: {exc}")
        print_fn("                 it will cancel the resting stop shortly; act now.")


async def verify_live_order(
    cfg: Any,
    *,
    symbol: str,
    store: TradeStore | None = None,
    client: Any = None,
    print_fn: Callable[..., Any] = print,
    input_fn: Callable[[str], str] = input,
) -> int:
    """One real order, protected and closed, witnessed on the production account.

    Exit codes: 0 full lifecycle verified; 1 a step failed (reported, the account was
    recovered or is provably still protected); 2 refused before anything moved (gate,
    credentials, symbol, confirmation); 3 account conditions unmet (unreachable,
    unfunded, not flat, kill-switch latched, untradable contract, unreadable market).

    This is the only place in the repository that places an order without running the
    strategy. It is deliberately the opposite of the strategy's post-only entries — a
    market order guarantees the fill so the whole lifecycle is witnessed in one round
    trip — and it is deliberately minimum-size.
    """
    print_fn(_rule())
    print_fn("  LIVE ORDER VERIFICATION — REAL PRODUCTION ORDER")
    print_fn(_rule())
    if not getattr(cfg, "live_enabled", False):
        print_fn("  refused: the safety gate is CLOSED.")
        print_fn("  All three are required: DRY_RUN=false, --mode live, --confirm-live.")
        print_fn("  No order was sent.")
        return 2
    if not getattr(cfg.credentials, "present", False):
        print_fn("  refused: GATE_API_KEY/GATE_API_SECRET are empty. No order was sent.")
        return 2
    if not symbol:
        print_fn("  refused: verification requires an explicit --symbol. No order was sent.")
        return 2
    if isinstance(client, SimulatedGateway):
        print_fn("  refused: the client is the in-process simulator. Verification must use")
        print_fn("  the production exchange client. No order was sent.")
        return 2

    store = store or TradeStore.from_config(cfg)
    owns_client = client is None
    if owns_client:
        from exchange.gate_client import GateFuturesClient

        client = GateFuturesClient(cfg)
        await client.__aenter__()
    try:
        # --- account conditions, read and reported before anything moves ----------
        try:
            account = await client.get_account()
            positions = await client.list_positions(holding=True)
        except GateAPIError as exc:
            print_fn(f"  account       : unreadable [{exc.status} {exc.label}] {exc.message}")
            print_fn("  refused: the account could not be read. No order was sent.")
            return 3
        open_orders: list[dict] = []
        price_orders: list[dict] = []
        for reader, sink in ((client.list_open_orders, open_orders),
                             (client.list_price_orders, price_orders)):
            try:
                sink.extend(await reader())
            except GateAPIError:
                pass
        snapshot = AccountSnapshot.from_api(
            account, positions, list(open_orders) + list(price_orders)
        )
        print_fn(f"  account       : {snapshot.total:.2f} {snapshot.currency or 'USDT'} total, "
                 f"{snapshot.available:.2f} available")
        if not snapshot.reachable:
            print_fn("  refused: account unreachable. No order was sent.")
            return 3
        if snapshot.available <= 0:
            print_fn("  refused: no available balance. No order was sent.")
            return 3
        if snapshot.open_positions or snapshot.open_orders:
            print_fn(f"  refused: the account must start flat, but it holds "
                     f"{snapshot.open_positions} position(s) and "
                     f"{snapshot.open_orders} order(s). No order was sent.")
            return 3

        tripped = store.kill_switches()
        if tripped:
            listed = "; ".join(f"{name}: {reason[:60]}" for name, reason in sorted(tripped.items()))
            print_fn(f"  refused: kill-switches are latched ({listed}). No order was sent.")
            return 3

        contract = await client.get_contract(symbol)
        if isinstance(contract, dict):
            contract = Contract.from_api(contract)
        if not contract.tradable:
            print_fn(f"  refused: {symbol} is not tradable "
                     f"(status={contract.status}, delisting={contract.in_delisting}).")
            return 3
        mark = await _read_mark(client, symbol)
        if not mark:
            # The whole point of this command is to witness facts. Confirming an order
            # against an unreadable market would turn the barrier into a formality.
            print_fn(f"  refused: the {symbol} market could not be read, so the order size, ")
            print_fn("  margin and stop below would be guesses. No order was sent.")
            return 3
        size = max(int(contract.order_size_min), 1)
        leverage = max(int(cfg.get("leverage.default", 100)), 1)
        notional = abs(size) * contract.quanto_multiplier * mark
        margin = notional / leverage
        if margin > snapshot.available:
            print_fn(f"  refused: minimum size {size} needs {margin:.4f} {snapshot.currency} "
                     f"margin at {leverage}x but only {snapshot.available:.2f} is available.")
            return 3

        # --- the final confirmation barrier --------------------------------------
        direction = 1
        print_fn("")
        print_fn("  THIS WILL PLACE A REAL MARKET ORDER:")
        print_fn(f"    {symbol}  {size} contract(s), LONG, at market, {leverage}x isolated")
        print_fn("  then place a stop-loss, verify it, close the position, and record")
        print_fn("  the round trip in the audit trail.")
        prompt = (f"  Type exactly  {symbol} SEND  to proceed (anything else aborts): ")
        answer = str(input_fn(prompt)).strip()
        if answer != f"{symbol} SEND":
            print_fn("")
            print_fn("  aborted: the confirmation did not match. No order was placed.")
            return 2
        print_fn("")

        manager = OrderManager.for_config(
            cfg, client=client, params=ExecutionParams.from_config(cfg),
        )
        protection = ProtectionEngine(manager, ProtectionParams.from_config(cfg))
        nonce = int(time.time())

        # The order must rest on the margin basis the estimate above assumed; set the
        # configured leverage first, and refuse if it cannot be set.
        print_fn(f"  leverage      : setting {symbol} to {leverage}x isolated...")
        try:
            await client.set_leverage(symbol, leverage)
        except Exception as exc:  # noqa: BLE001
            print_fn(f"  leverage      : FAILED — {type(exc).__name__}: {exc}")
            print_fn("  refused: the leverage could not be set, so the margin basis of the")
            print_fn("  order is unknown. No order was sent.")
            return 2
        print_fn("  leverage      : OK")

        try:
            # --- entry: submit, then read back what actually happened ------------
            print_fn(f"  entry         : submitting REAL market order for {size} contract(s)...")
            text = idempotency_key("vfy", symbol, nonce)
            raw = await manager.gateway.place_order(
                symbol, size * direction, price=None, tif="ioc", text=text,
            )
            order_id = str(raw.get("id", "") or "")
            if not order_id:
                print_fn("  entry         : the exchange returned no order id — the order may or")
                print_fn("                 may not exist. Refusing to guess. No position claim is made.")
                await _recover(manager, symbol, nonce, print_fn)
                return 1
            record = await manager.read_order(order_id, symbol, size * direction)
            print_fn(f"  entry         : {record.summary()}")
            if not record.state.has_exposure:
                print_fn(f"  entry         : did not fill ({record.state.value}). Nothing to protect.")
                return 1
            filled = abs(record.filled_size) or size
            if record.state.value == "unknown" or not record.filled_size:
                # The fill could not be proven from the order read. The exchange is the truth:
                held = abs(await manager.position_size(symbol))
                if held == 0:
                    print_fn("  entry         : fill unconfirmed and no position exists. "
                             "Nothing to protect.")
                    return 1
                filled = held
                print_fn(f"  entry         : fill unconfirmed but a position of {held} exists; "
                         "continuing with the exchange as the truth")
            entry_price = record.average_price or mark
            if not entry_price:
                print_fn("  entry         : no fill price could be established.")
                await _recover(manager, symbol, nonce, print_fn)
                return 1
            print_fn(f"  entry         : FILLED {filled} @ {entry_price:g} (order {order_id})")

            # --- protection: place the stop, then verify it exists ----------------
            stop_distance = float(cfg.get("stop_loss.min_distance", 0.001))
            stop_price = entry_price * (1.0 - stop_distance * direction)
            print_fn(f"  protection    : placing stop at {stop_price:g}...")
            result = await protection.protect(
                symbol, direction, entry_price, stop_price, filled, nonce,
            )
            if not result.ok:
                print_fn(f"  protection    : FAILED — {result.summary()}")
                if not result.flattened:
                    print_fn("  protection    : the position may still be open and UNPROTECTED.")
            else:
                print_fn(f"  protection    : OK — stop {result.stop_order_id} verified at "
                         f"{result.stop_price:g} after {result.attempts} attempt(s); "
                         f"{len(result.take_profits)} TP leg(s)")

            # --- close: market close, then prove flat ------------------------------
            try:
                already_flat = (await manager.position_size(symbol)) == 0
            except Exception:  # noqa: BLE001
                already_flat = False
            if already_flat:
                # Protection's emergency close got there first.
                print_fn("  close         : position confirmed flat (protection closed it)")
                close_record = None
            else:
                print_fn("  close         : closing the position...")
                try:
                    close_record = await manager.close_position(
                        symbol, f"{nonce}x", reason="live order verification",
                    )
                    print_fn(f"  close         : {close_record.summary()}")
                except Exception as exc:  # noqa: BLE001
                    print_fn(f"  close         : FAILED — {type(exc).__name__}: {exc}")
                    await _recover(manager, symbol, nonce, print_fn)
                    return 1
                try:
                    remaining = await manager.position_size(symbol)
                except Exception as exc:  # noqa: BLE001
                    print_fn(f"  close         : could not confirm flat — "
                             f"{type(exc).__name__}: {exc}")
                    await _recover(manager, symbol, nonce, print_fn)
                    return 1
                if remaining != 0:
                    print_fn(f"  close         : UNCONFIRMED — {remaining} contracts still open.")
                    print_fn("                 The verified stop remains in force; the dead-man")
                    try:
                        await manager.gateway.countdown_cancel_all(0, symbol)
                        print_fn("                 countdown was disarmed so the stop is not cancelled.")
                    except Exception:  # noqa: BLE001
                        print_fn("                 countdown could NOT be disarmed — it will cancel")
                        print_fn("                 the stop shortly; act now.")
                    print_fn("                 Do not run the strategy loop until this is resolved.")
                    return 1
                print_fn("  close         : position confirmed flat")

            # remove any resting protective orders so none can fire into a later position
            for order_id in ([result.stop_order_id] if result.stop_order_id else []) + [
                leg.order_id for leg in result.take_profits if leg.order_id
            ]:
                try:
                    await manager.gateway.cancel_price_order(order_id)
                except Exception:  # noqa: BLE001
                    pass
            # The countdown no longer has anything to watch: disarm so nothing is
            # cancelled behind the operator's back (there is nothing left to cancel).
            try:
                await manager.gateway.countdown_cancel_all(0, symbol)
            except Exception:  # noqa: BLE001
                pass

            # --- record the witnessed round trip (mode="live") ---------------------
            exit_price = close_record.average_price if close_record and close_record.average_price \
                else entry_price
            coins = abs(filled) * contract.quanto_multiplier
            gross = direction * (exit_price - entry_price) * coins
            taker = float(cfg.get("backtest.fee_taker", 0.00075))
            entry_fee = abs(entry_price) * coins * taker
            exit_fee = abs(exit_price) * coins * taker
            fees = entry_fee + exit_fee
            net = gross - fees
            risk_amount = (abs(entry_price - stop_price)) * abs(filled) * contract.quanto_multiplier
            try:
                after = await client.get_account()
                equity_after = float(after.get("total", snapshot.total) or snapshot.total)
            except Exception:  # noqa: BLE001
                equity_after = snapshot.total + net
            now = time.time()
            trade = PaperTrade(
                symbol=symbol, direction=direction, entry_time=now, exit_time=now,
                entry_price=entry_price, exit_price=exit_price, size=filled * direction,
                stop_price=stop_price, exit_reason="verify", gross_pnl=gross, fees=fees,
                net_pnl=net, equity_after=equity_after,
                r_multiple=net / risk_amount if risk_amount > 0 else 0.0,
                margin=margin, liquidation_price=0.0,
            )
            store.record_trade(TradeRecord.from_paper(
                trade, leverage=leverage, mode="live",
            ))
            store.record_equity(EquityPoint(
                timestamp=now, equity=equity_after, balance=equity_after,
                open_positions=0, note="verify",
            ))

            print_fn(_rule())
            if result.ok:
                print_fn("  LIVE ORDER VERIFICATION COMPLETE — entry, protection and exit")
                print_fn("  were all verified against production Gate.io.")
                print_fn(f"  entry {filled} @ {entry_price:g} -> exit @ {exit_price:g}; "
                         f"net {net:+.4f} {snapshot.currency or 'USDT'} after fees "
                         f"({fees:.4f}). Recorded as mode=live.")
                return 0
            print_fn("  VERIFICATION INCOMPLETE — the position was closed and the account is")
            print_fn("  flat, but the protective stop could not be verified on production.")
            print_fn("  Investigate before trusting the strategy loop.")
            return 1
        except Exception as exc:  # noqa: BLE001
            # An order may already exist. Recover first, then report — never a traceback
            # with the account in an unknown state.
            print_fn(f"  error         : {type(exc).__name__}: {exc}")
            await _recover(manager, symbol, nonce, print_fn)
            print_fn(_rule())
            print_fn("  verification FAILED — see above. The account was market-closed by the")
            print_fn("  recovery path where possible; the dead-man countdown was disarmed so")
            print_fn("  any resting stop still protects what remains.")
            return 1
    finally:
        if owns_client and client is not None:
            try:
                await client.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
