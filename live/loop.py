"""The live trading loop: the paper stack pointed at the real exchange.

This is the minimum runner that makes ``--mode live`` actually trade. It reuses the same
decision order the paper loop already exercises:

    market data -> risk breakers -> signal -> size -> liquidation guard -> entry -> protect

and the same execution / protection layers. The differences that matter:

* Construction **requires** an open safety gate and a live client. The opposite of
  :class:`~paper.loop.PaperTrader`, which refuses both. There is no flag to run this
  against a closed gate, and no path that falls back to the simulator.
* Equity, position size and protective-order state are read from the exchange, not from
  an in-process book.
* Fills are recorded with ``mode="live"`` so preflight and validation can never mistake
  them for paper evidence.

Preflight is the caller's job. This module will not open the gate, will not skip a
NO-GO, and will not place an order while ``Config.live_enabled`` is false.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from database.models import EquityPoint, TradeRecord, TradeStore
from exchange.gate_client import Contract, GateFuturesClient, RiskTier
from execution.order_manager import (
    ExecutionParams,
    OrderManager,
    OrderState,
    SimulatedGateway,
)
from execution.preflight import AccountSnapshot, PreflightReport, preflight
from execution.protection import ProtectionEngine, ProtectionParams
from paper.loop import PaperFill, PaperReport, PaperTrade, RestMarketSource
from paper.validation import ValidationReport
from risk.liquidation_guard import LiquidationParams, TierSnapshot, assess_plan
from risk.position_sizer import SizingParams, plan_position
from risk.risk_manager import RiskManager, RiskParams, RiskStore, SqliteRiskStore
from risk.session_guard import SessionParams, session_verdict
from strategy.indicators import Candles
from strategy.signal_engine import EngineParams, SignalEngine

log = logging.getLogger(__name__)

__all__ = [
    "LiveGateRefused",
    "LiveReport",
    "LiveTrader",
    "run_live",
]


class LiveGateRefused(Exception):
    """Raised when the live loop is pointed at a closed gate, or a simulated gateway."""


@dataclass
class LiveReport(PaperReport):
    """Paper report counters, plus what the live runner itself decided."""

    preflight_ready: bool = False
    started: bool = False
    stop_reason: str = ""


class LiveTrader:
    """Runs the whole stack against Gate.io, one step per poll.

    Construction refuses a closed safety gate and refuses a simulated gateway. Everything
    else is the same veto chain the paper loop uses: a refused breaker, an unsignalled bar,
    an unsizable account, a guard veto, an unfilled entry. The loop's normal state is doing
    nothing.
    """

    def __init__(
        self,
        config: Any,
        client: Any,
        source: RestMarketSource,
        symbol: str,
        tiers: Sequence[Any],
        contract: Any,
        *,
        starting_equity: float,
        risk_store: RiskStore | None = None,
        store: TradeStore | None = None,
        engine: SignalEngine | None = None,
        decide: Any = None,
    ) -> None:
        if not getattr(config, "live_enabled", False):
            raise LiveGateRefused(
                "the safety gate is CLOSED. LiveTrader requires DRY_RUN=false, "
                "--mode live and --confirm-live; it will not simulate and it will not "
                "open the gate itself."
            )
        if client is None:
            raise LiveGateRefused("LiveTrader requires a live exchange client")

        self.config = config
        self.client = client
        self.source = source
        self.symbol = symbol
        self.tiers = tuple(tiers)
        self.contract = contract
        self._decide = decide
        self.store = store

        self.sizing = SizingParams.from_config(config)
        self.liquidation = LiquidationParams.from_config(config)
        self.protection_params = ProtectionParams.from_config(config)
        self.session_params = SessionParams.from_config(config)
        self.engine = engine or SignalEngine(params=EngineParams.from_config(config))

        db_path = str(config.get("database.path", "data/trades.db"))
        self.risk = RiskManager(
            RiskParams.from_config(config),
            risk_store or SqliteRiskStore(db_path),
        )
        self.orders = OrderManager.for_config(
            config, client=client, params=ExecutionParams.from_config(config),
        )
        if not self.orders.live or isinstance(self.orders.gateway, SimulatedGateway):
            raise LiveGateRefused(
                "the order manager resolved to the simulator; live trading requires a "
                "live gateway behind an open safety gate"
            )
        self.protection = ProtectionEngine(self.orders, self.protection_params)

        equity = float(starting_equity)
        self.report = LiveReport(
            symbol=symbol, equity=equity, starting_equity=equity, started=True,
        )
        self._position: dict[str, Any] | None = None
        self._nonce = int(time.time())
        self._leverage_set = False

    # --- observation -------------------------------------------------------

    @property
    def open_position(self) -> Mapping[str, Any] | None:
        return self._position

    def unrealised_pnl(self, mark: float) -> float:
        position = self._position
        if position is None:
            return 0.0
        coins = abs(int(position["remaining"])) * position["per_contract"]
        return position["direction"] * (float(mark) - position["entry_price"]) * coins

    def mark_to_market(self, mark: float | None = None) -> float:
        if mark is None:
            mark = self.source.mark_price(self.symbol)
        return self.report.equity + self.unrealised_pnl(mark)

    # --- the step ----------------------------------------------------------

    async def step(self) -> LiveReport:
        """One iteration: refresh market data, settle, then decide whether to act."""
        self.report.steps += 1
        await self.source.refresh(self.symbol)
        now = self.source.now()
        mark = self.source.mark_price(self.symbol)

        # Keep the dead-man switch armed while we hold exposure.
        if self._position is not None:
            try:
                await self.orders.arm_dead_man_switch(
                    self.protection_params.dead_man_switch_seconds, self.symbol,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("dead-man switch refresh failed: %s", exc)

        await self._settle(now, mark)
        session = session_verdict(self.symbol, now, self.session_params)

        if self._position is not None:
            # A position must not ride a close: flatten in the flatten window, and keep
            # trying while the venue is closed — the close may have failed just before
            # the shut, and the one thing worse than retrying is a gap across a 0.125%
            # stop. While the venue is open, a position whose stop never verified is
            # re-protected every step until it is protected or gone.
            if session.must_flat or session.stage == "closed":
                await self._flatten_for_session(now)
            elif not self._position.get("protected", True):
                await self._reprotect()
            return self.report

        if not session.entry_allowed:
            # A synthetic's venue is closed, or is about to be: a stop placed now could not
            # execute across the gap, so the loss would be bounded by liquidation instead
            # of by risk. This veto sits before the signal chain on purpose.
            self.report.count(f"session:{session.stage}")
            return self.report

        # A position the loop does not track — opened manually, or orphaned by a previous
        # run — must not be traded on top of. Preflight refuses it at start; if one
        # appears mid-run, block entries until it is gone.
        try:
            foreign = await self.orders.position_size(self.symbol)
        except Exception as exc:  # noqa: BLE001
            log.warning("position re-read failed: %s", exc)
            foreign = 0
        if foreign != 0:
            self.report.count("position:foreign")
            log.critical(
                "an untracked position of %s contracts exists on the exchange; "
                "refusing to enter on top of it", foreign,
            )
            return self.report

        try:
            account = await self.client.get_account()
            available = float(account.get("available", self.report.equity) or 0.0)
            total = float(account.get("total", available) or available)
            # Prefer total for risk/equity; available for margin headroom.
            if total > 0:
                self.report.equity = total
        except Exception as exc:  # noqa: BLE001
            log.warning("account refresh failed: %s", exc)
            available = self.report.equity

        decision = self.risk.can_trade(
            now=now, equity=self.report.equity, open_positions=0, symbol=self.symbol,
        )
        if not decision.allowed:
            self.report.count(decision.breaker.value if decision.breaker else "risk")
            return self.report

        candles = self.source.candles(self.symbol)
        signal = self._signal(candles, now)
        if signal is None or not getattr(signal, "accepted", False):
            self.report.count(getattr(signal, "stage", "no_signal") or "no_signal")
            return self.report

        plan = plan_position(
            symbol=self.symbol, direction=signal.direction, entry_price=mark,
            candles=candles[self.engine.entry_timeframe], contract=self.contract,
            tiers=self.tiers, equity=self.report.equity, available=available,
            params=self.sizing,
        )
        if not plan.ok:
            self.report.count(f"size:{plan.stage}")
            return self.report

        verdict = assess_plan(
            plan, TierSnapshot.of(self.symbol, self.tiers, now), now,
            params=self.liquidation, contract=self.contract,
        )
        if not verdict.ok:
            self.report.count(f"liq:{verdict.stage}")
            return self.report

        await self._enter(plan, now, float(getattr(signal, "score", 0.0)), verdict)
        return self.report

    def _signal(self, candles: Mapping[str, Candles], now: float) -> Any:
        if self._decide is not None:
            return self._decide(symbol=self.symbol, candles=candles, now=now, btc=None)
        return self.engine.evaluate(self.symbol, candles, now)

    # --- entry and protection ---------------------------------------------

    async def _ensure_leverage(self) -> None:
        if self._leverage_set:
            return
        leverage = int(self.config.get("leverage.default", 100))
        await self.client.set_leverage(self.symbol, leverage)
        self._leverage_set = True

    async def _enter(self, plan: Any, now: float, score: float,
                     verdict: Any = None) -> None:
        self.report.entries_attempted += 1
        nonce = self._next_nonce()

        try:
            await self._ensure_leverage()
        except Exception as exc:  # noqa: BLE001
            self.report.count("leverage")
            log.error("set_leverage failed: %s", exc)
            return

        tick = float(getattr(self.contract, "order_price_round", 0.0) or 0.0)
        limit = plan.entry_price - plan.direction * tick
        # Round to the price grid the exchange accepts.
        if tick > 0:
            limit = round(limit / tick) * tick

        record = await self.orders.submit_entry(
            self.symbol, plan.size, str(limit), nonce,
        )
        if record.state is OrderState.EXPIRED:
            self.report.entries_expired += 1
            self.report.count("entry:expired")
            return
        if not record.state.has_exposure:
            self.report.count(f"entry:{record.state.value}")
            return

        self.report.entries_filled += 1
        entry_price = record.average_price or plan.entry_price
        size = record.filled_size or plan.size
        # Prefer the exchange's own size if the fill was partial or signed differently.
        try:
            held = await self.orders.position_size(self.symbol)
            if held != 0:
                size = held
        except Exception as exc:  # noqa: BLE001
            log.warning("post-fill position re-read failed: %s", exc)

        per_contract = plan.coin_amount / abs(plan.size) if plan.size else float(
            getattr(self.contract, "quanto_multiplier", 0.0) or 0.0
        )
        notional = abs(size) * per_contract * entry_price
        entry_fee = self._fee(notional, maker=True)
        self.report.equity -= entry_fee

        result = await self.protection.protect(
            self.symbol, plan.direction, entry_price, plan.stop.price, size, nonce,
        )
        if not result.ok:
            self.report.protection_failures += 1
            if result.flattened:
                self.report.flattened += 1
                self.report.count("protection:flattened")
                exit_fee = self._fee(notional, maker=False)
                self.report.equity -= exit_fee
                self.risk.record_trade(
                    now=now, pnl=-(entry_fee + exit_fee), equity=self.report.equity,
                )
                return
            # Live: real exposure with no verified stop. Never lose track of it — an
            # untracked position means no dead-man switch, no settlement, and a second
            # entry on top of the first. Track it, then try to close it here and now; if
            # the close fails, the position branch re-protects it every step until it is
            # protected or gone.
            self.report.count("protection:unprotected")
            self._position = self._position_dict(
                entry_price, size, per_contract, entry_fee, now, score, plan, verdict,
                protected=False,
            )
            await self._flatten_for_session(
                now, stage="protection", fill_reason="protection_failed",
            )
            return

        self._position = self._position_dict(
            entry_price, size, per_contract, entry_fee, now, score, plan, verdict,
            protected=True, stop_id=result.stop_order_id,
            levels={result.stop_order_id: ("stop", plan.stop.price)}
            | {leg.order_id: (leg.name, leg.price) for leg in result.take_profits
               if leg.order_id},
        )

    def _position_dict(self, entry_price: float, size: int, per_contract: float,
                       fees: float, now: float, score: float, plan: Any, verdict: Any,
                       *, protected: bool, stop_id: str = "",
                       levels: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """The tracked-position shape, shared by the protected and unprotected paths.

        ``protected`` is what the position branch of :meth:`step` reads: False means the
        stop never verified, the dead-man stays armed, and :meth:`_reprotect` keeps
        trying until the position is protected or closed.
        """
        return {
            "direction": 1 if size > 0 else -1,
            "entry_price": entry_price,
            "entry_time": now,
            "size": size,
            "remaining": abs(size),
            "per_contract": per_contract,
            "stop_price": plan.stop.price,
            "stop_order_id": stop_id,
            "fees": fees,
            "realised": 0.0,
            "score": score,
            "fills": [],
            "margin": float(getattr(plan, "margin", 0.0) or 0.0),
            "liquidation_price": float(getattr(verdict, "liq_price", 0.0) or 0.0),
            "levels": dict(levels or {}),
            "protected": protected,
        }

    async def _reprotect(self) -> None:
        """Re-attempt protection for a tracked position whose stop never verified.

        The invariant is that no position exists without a verified stop. A failure
        leaves the position tracked — so the dead-man stays armed, settlement keeps
        watching it, and no second entry lands on top — and this keeps restoring the
        stop every step until it is protected or the position is gone.
        """
        position = self._position
        assert position is not None
        size = abs(int(position["remaining"]))
        if size == 0:
            return
        try:
            result = await self.protection.protect(
                self.symbol, position["direction"], position["entry_price"],
                position["stop_price"], size, self._next_nonce(),
            )
        except Exception as exc:  # noqa: BLE001
            log.error("re-protect failed: %s", exc)
            self.report.count("protection:reprotect_error")
            return
        if result.ok:
            position["protected"] = True
            position["stop_order_id"] = result.stop_order_id
            position["levels"] = {
                result.stop_order_id: ("stop", position["stop_price"])
            } | {leg.order_id: (leg.name, leg.price) for leg in result.take_profits
                 if leg.order_id}
            self.report.count("protection:recovered")
        elif result.flattened:
            # Protection's own emergency close got there first; book the exit.
            await self._flatten_for_session(
                self.source.now(), stage="protection", fill_reason="protection_failed",
            )
        else:
            self.report.count("protection:still_unprotected")

    # --- session close -----------------------------------------------------

    async def _flatten_for_session(self, now: float, *, stage: str = "session",
                                   fill_reason: str = "session") -> None:
        """Close whatever is open, then take protection off.

        A resting stop cannot execute on a closed venue, so holding through the close is
        holding without a stop. The position is market-closed; once it is confirmed gone
        the resting protective orders are cancelled too, so a stale reduce-only order
        cannot fire into a *later* session's position.

        ``stage``/``fill_reason`` let the protection-failure path reuse this same
        close-and-book sequence while reporting under its own counter and exit reason.
        """
        position = self._position
        assert position is not None

        record = None
        try:
            record = await self.orders.close_position(
                self.symbol, self._next_nonce(), reason=f"{fill_reason} close",
            )
        except Exception as exc:  # noqa: BLE001
            # The close failed outright (a closed venue may reject the order). If the
            # exchange then proves the position is gone anyway, keep booking the exit;
            # otherwise keep the position tracked and try again next step.
            log.error("%s flatten: close failed: %s", fill_reason, exc)
        remaining: int | None = None
        try:
            remaining = await self.orders.position_size(self.symbol)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s flatten: position re-read failed: %s", fill_reason, exc)
        if remaining != 0:
            # The close is unconfirmed (or did not happen). The stop and the dead-man
            # switch are still armed; keep the position tracked and try again next step.
            self.report.count(f"{stage}:close_unconfirmed")
            if remaining is None:
                log.error("%s flatten: close could not be confirmed", fill_reason)
            else:
                log.error("%s flatten: %s contracts still open after close",
                          fill_reason, remaining)
            return

        for order_id in list(position.get("levels", {})):
            try:
                await self.orders.gateway.cancel_price_order(order_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("%s flatten: cancelling %s failed: %s", fill_reason, order_id, exc)

        price = (record.average_price if record and record.average_price
                 else self.source.mark_price(self.symbol))
        size = abs(int(position["remaining"]))
        if size:
            self._book_fill(fill_reason, price, size)
        self._position["remaining"] = 0
        self._close_trade(now)
        self.report.count(f"{stage}:flattened")
        if stage == "protection":
            # A protection-failure flatten, whether the engine closed it directly or the
            # tracked-unprotected path closed it here, is a protection flatten.
            self.report.flattened += 1

    # --- settlement --------------------------------------------------------

    async def _settle(self, now: float, mark: float) -> None:
        if self._position is None:
            return

        # A transient read failure must not kill the loop: the position is still being
        # watched (dead-man armed, stop resting), and the reconciliation can happen on
        # the next step. Dying here would stop the bot for no reason other than a blip.
        try:
            held = abs(await self.orders.position_size(self.symbol))
        except Exception as exc:  # noqa: BLE001
            log.warning("settle: position re-read failed: %s", exc)
            self.report.count("settle:unreadable")
            return
        if held == abs(self._position["remaining"]):
            return

        try:
            open_ids = {
                str(order.get("id"))
                for order in await self.orders.gateway.list_price_orders(self.symbol)
            }
        except Exception as exc:  # noqa: BLE001
            # Which level fired cannot be told apart, and guessing would book the shrink
            # at the wrong level's price (the stop sorts nearest to entry and would be
            # booked first). Defer instead: the next successful read reconciles the same
            # way, and the dead-man/stop still protect the position meanwhile.
            log.warning("settle: protective orders unreadable: %s", exc)
            self.report.count("settle:unreadable")
            return
        fired = [
            (order_id, level) for order_id, level in self._position["levels"].items()
            if order_id and order_id not in open_ids
        ]
        fired.sort(key=lambda item: abs(item[1][1] - self._position["entry_price"]))

        closed = abs(self._position["remaining"]) - held
        for order_id, (name, price) in fired:
            if closed <= 0:
                break
            chunk = closed if name in ("stop", "tp3") else min(closed, self._chunk(name))
            self._book_fill(name, price, chunk)
            closed -= chunk
            self._position["levels"].pop(order_id, None)
        if closed > 0:
            self._book_fill("unknown", mark, closed)

        self._position["remaining"] = held
        if held == 0:
            self._close_trade(now)

    def _chunk(self, name: str) -> int:
        fractions = {
            "tp1": self.protection_params.tp1_close_pct,
            "tp2": self.protection_params.tp2_close_pct,
        }
        return int(abs(self._position["size"]) * fractions.get(name, 1.0))

    def _book_fill(self, reason: str, price: float, size: int) -> None:
        position = self._position
        assert position is not None
        coins = size * position["per_contract"]
        pnl = position["direction"] * (price - position["entry_price"]) * coins
        fee = self._fee(abs(coins) * price, maker=False)
        position["realised"] += pnl
        position["fees"] += fee
        position["fills"].append(PaperFill(reason, price, size, pnl, fee))
        self.report.equity += pnl - fee

    def _close_trade(self, now: float) -> None:
        position = self._position
        assert position is not None
        fills = tuple(position["fills"])
        gross = position["realised"]
        fees = position["fees"]
        net = gross - fees
        last = fills[-1] if fills else None

        risk_amount = (
            abs(position["entry_price"] - position["stop_price"])
            * abs(int(position["size"]))
            * position["per_contract"]
        )
        trade = PaperTrade(
            symbol=self.symbol,
            direction=position["direction"],
            entry_time=position["entry_time"],
            exit_time=now,
            entry_price=position["entry_price"],
            exit_price=last.price if last else position["entry_price"],
            size=position["size"],
            stop_price=position["stop_price"],
            exit_reason=last.reason if last else "unknown",
            gross_pnl=gross,
            fees=fees,
            net_pnl=net,
            equity_after=self.report.equity,
            score=position["score"],
            fills=fills,
            r_multiple=net / risk_amount if risk_amount > 0 else 0.0,
            margin=position["margin"],
            liquidation_price=position["liquidation_price"],
        )
        self.report.trades.append(trade)
        self.risk.record_trade(
            now=now, pnl=net, equity=self.report.equity, symbol=self.symbol,
        )
        if self.store is not None:
            leverage = int(self.config.get("leverage.default", 100))
            self.store.record_trade(
                TradeRecord.from_paper(trade, leverage=leverage, mode="live")
            )
            self.store.record_equity(EquityPoint(
                timestamp=now, equity=self.report.equity,
                balance=self.report.equity, open_positions=0, note="live",
            ))
        self._position = None

    def _next_nonce(self) -> int:
        self._nonce += 1
        return self._nonce

    def _fee(self, notional: float, maker: bool) -> float:
        rate = (
            float(self.config.get("backtest.fee_maker", -0.0001)) if maker
            else float(self.config.get("backtest.fee_taker", 0.00075))
        )
        return notional * rate

    # --- driving -----------------------------------------------------------

    async def run(self, steps: int | None = None, poll_seconds: float = 5.0,
                  on_step: Any = None) -> LiveReport:
        """Step until ``steps`` iterations have run, or forever when ``steps`` is None."""
        taken = 0
        try:
            while steps is None or taken < steps:
                await self.step()
                if on_step is not None:
                    on_step(self)
                taken += 1
                if steps is not None and taken >= steps:
                    break
                if poll_seconds:
                    await asyncio.sleep(poll_seconds)
        except asyncio.CancelledError:
            self.report.stop_reason = "cancelled"
            await self._release_on_shutdown()
            raise
        except KeyboardInterrupt:
            self.report.stop_reason = "keyboard_interrupt"
            await self._release_on_shutdown()
        except Exception:
            # An unexpected step error stops the loop too, and the dead-man must be
            # disarmed the same way a graceful stop disarms it: leaving the countdown
            # armed would cancel the resting stop `dead_man_switch_seconds` later and
            # turn a monitored position into a naked one — the exact state the release
            # exists to prevent.
            self.report.stop_reason = "error"
            await self._release_on_shutdown()
            raise
        else:
            if not self.report.stop_reason:
                self.report.stop_reason = "completed" if steps is not None else "stopped"
            await self._release_on_shutdown()
        return self.report

    async def _release_on_shutdown(self) -> None:
        """Cancel the dead-man countdown so a held position's stop is not auto-cancelled.

        The countdown exists to clean up stale orders after a *crash*. On a graceful stop
        that same cleanup would cancel the protective stop of a position still open 60
        seconds later — turning a monitored position into a naked one, which is the exact
        state this repository forbids. Cancelling the countdown (``timeout=0`` on
        ``countdown_cancel_all``) leaves the verified stop and TPs resting, and the stop
        closes the position on its own, which is the safe way to leave.
        """
        if self._position is None:
            # Nothing tracked means either the account is flat or the countdown was never
            # armed on a path that reached a position (protect() arms it only while
            # placing a stop, and the paths where it can raise have no resting stop to
            # save) — releasing is a no-op either way.
            return
        try:
            await self.orders.gateway.countdown_cancel_all(0, self.symbol)
            log.warning(
                "stopping with an open position: the dead-man countdown was cancelled so "
                "the resting stop-loss remains in force; the position closes only when "
                "the stop or a take-profit fires"
            )
        except Exception as exc:  # noqa: BLE001
            log.error("could not cancel the dead-man countdown on shutdown: %s", exc)


async def _read_account_snapshot(client: Any) -> AccountSnapshot:
    """Read account + positions + open orders for preflight. Read-only."""
    try:
        account = await client.get_account()
        positions = await client.list_positions(holding=True)
        try:
            open_orders = await client.list_open_orders()
        except Exception:  # noqa: BLE001 — some accounts have none; treat as empty
            open_orders = []
        try:
            price_orders = await client.list_price_orders()
        except Exception:  # noqa: BLE001
            price_orders = []
        # Resting price-triggered stops count as open exposure for the flat check.
        combined = list(open_orders) + list(price_orders)
        return AccountSnapshot.from_api(account, positions, combined)
    except Exception as exc:  # noqa: BLE001
        return AccountSnapshot.unreachable(str(exc))


async def run_live(
    cfg: Any,
    *,
    symbol: str | None = None,
    steps: int | None = None,
    poll_seconds: float = 5.0,
    client: Any = None,
    store: TradeStore | None = None,
    validation: ValidationReport | None = None,
    account: AccountSnapshot | None = None,
    decide: Any = None,
    on_step: Any = None,
    print_fn: Any = print,
) -> tuple[int, LiveReport | None, PreflightReport]:
    """Start live trading, or refuse with a clear reason.

    Returns ``(exit_code, report_or_None, preflight_report)``.

    Exit codes:
      * ``0`` — ran (or completed the requested steps) successfully
      * ``1`` — exchange / runtime failure after the gate was open
      * ``2`` — configuration / gate refusal
      * ``3`` — preflight NO-GO
    """
    if not getattr(cfg, "live_enabled", False):
        report = preflight(cfg, store or TradeStore.from_config(cfg), account=account,
                           validation=validation)
        print_fn("Live mode requested but the safety gate is CLOSED.")
        print_fn("All three are required: DRY_RUN=false in .env, --mode live, "
                 "--confirm-live.")
        print_fn("No orders will be sent.")
        print_fn()
        print_fn(report.render())
        return 2, None, report

    owns_client = client is None
    store = store or TradeStore.from_config(cfg)
    symbol = symbol or list(cfg.get("universe.symbols"))[0]
    timeframes = tuple(cfg.get("strategy.timeframes", ["1m", "5m", "15m", "1h"]))

    report: LiveReport | None = None
    try:
        if not getattr(cfg.credentials, "present", False):
            print_fn("GATE_API_KEY/GATE_API_SECRET are empty; live trading refused.")
            return 2, None, preflight(cfg, store, account=account,
                                      validation=validation)

        if owns_client:
            client = GateFuturesClient(cfg)
            await client.__aenter__()

        if account is None:
            account = await _read_account_snapshot(client)

        pf = preflight(cfg, store, account=account, validation=validation)
        print_fn(pf.render())
        if not pf.ready:
            print_fn()
            print_fn("Live trading refused: preflight is NO-GO. No orders will be sent.")
            return 3, None, pf

        print_fn()
        print_fn(f"Preflight GO. Starting live trading loop on {symbol}.")
        print_fn("Ctrl-C to stop. Kill-switches and the dead-man switch remain armed.")

        contract = await client.get_contract(symbol)
        if isinstance(contract, dict):
            contract = Contract.from_api(contract)
        tiers_raw = await client.get_risk_tiers(symbol)
        tiers = [
            t if isinstance(t, RiskTier) else RiskTier.from_api(t)
            for t in tiers_raw
        ]

        account_equity = float(account.total or account.available or 0.0)
        if account_equity <= 0:
            print_fn("Account equity is not positive; refusing to start.")
            return 3, None, pf

        source = RestMarketSource(client, timeframes=timeframes)
        await source.refresh(symbol)

        trader = LiveTrader(
            cfg, client, source, symbol, tiers, contract,
            starting_equity=account_equity, store=store, decide=decide,
        )
        report = await trader.run(steps=steps, poll_seconds=poll_seconds, on_step=on_step)
        report.preflight_ready = True
        print_fn()
        print_fn(report.summary())
        if report.stop_reason:
            print_fn(f"stopped: {report.stop_reason}")
        return 0, report, pf
    except LiveGateRefused as exc:
        print_fn(f"live refused: {exc}")
        return 2, report, preflight(cfg, store, account=account, validation=validation)
    except Exception as exc:  # noqa: BLE001
        log.exception("live runner failed")
        print_fn(f"live runner error: {exc}")
        return 1, report, preflight(cfg, store, account=account, validation=validation)
    finally:
        if owns_client and client is not None:
            try:
                await client.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
