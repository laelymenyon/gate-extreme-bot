"""Enforces the central safety invariant: no position exists without a verified stop-loss.

PHASE 8.

Everything in this module serves one sequence, and the order is not negotiable:

    entry filled -> place SL -> **re-read the SL from the exchange** -> only then TP ladder

The re-read is the point. A 200 on the stop-loss POST means Gate.io accepted the request,
not that a live trigger exists — and the window between "position open" and "stop
confirmed" is the only moment in this bot's life when leveraged size sits unprotected. At
100x on a contract whose entire liquidation distance is 0.425%, that window is measured in
account balances. So the stop is read back from ``/price_orders`` and matched by client id
before anything else happens, and if it cannot be confirmed within
``protection.sl_retry_attempts`` the position is **market-closed** rather than left naked.

Closing at a loss is the correct outcome there. An unprotected 100x position is not a
trade, it is an open-ended bet on the next candle.

**The stop triggers on mark price** (``price_type=1``), because liquidation is computed off
the mark. A stop that watches the last-traded price is racing a different series than the
one that can liquidate it, and on a wick those two disagree exactly when it matters.

**The stop order is a market order** (``price="0"``): a stop that does not fill is not a
stop. The 0.075% taker fee is budgeted into every break-even figure in ARCHITECTURE §5.

The dead-man switch (``countdown_cancel_all``) is armed around the sequence so that a bot
that crashes mid-trade does not leave resting orders behind with nobody watching them.

Nothing here decides *whether* to trade or *how much* — that is Phases 5-7, already
decided by the time anything in this module runs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from execution.order_manager import (
    ExecutionError,
    OrderManager,
    OrderRecord,
    OrderState,
    idempotency_key,
)

__all__ = [
    "ProtectionParams",
    "TakeProfitLeg",
    "ProtectionResult",
    "stop_trigger_rule",
    "breakeven_price",
    "trailing_stop_price",
    "take_profit_ladder",
    "ProtectionEngine",
]

#: Gate.io ``trigger.price_type``. Mark, because liquidation is priced off the mark.
PRICE_TYPE = {"last": 0, "mark": 1, "index": 2}

#: Gate.io ``trigger.rule``: 1 fires at or above the trigger, 2 at or below.
RULE_ABOVE, RULE_BELOW = 1, 2


@dataclass(frozen=True)
class ProtectionParams:
    """Thresholds from ``protection`` and ``take_profit``. Defaults mirror ``config.yaml``."""

    sl_price_type: str = "mark"
    sl_retry_attempts: int = 3
    emergency_close_on_sl_failure: bool = True
    verify_liq_price: bool = True
    move_to_breakeven: bool = True
    breakeven_trigger_r: float = 1.0
    breakeven_fee_buffer: float = 0.0009
    trailing_stop: bool = True
    trailing_atr_multiplier: float = 1.5
    dead_man_switch_seconds: int = 60
    tp1_r: float = 1.0
    tp1_close_pct: float = 0.40
    tp2_r: float = 2.0
    tp2_close_pct: float = 0.35
    tp3_r: float = 3.0

    def __post_init__(self) -> None:
        if self.sl_price_type not in PRICE_TYPE:
            raise ValueError(
                f"sl_price_type must be one of {sorted(PRICE_TYPE)}, got {self.sl_price_type!r}"
            )
        if self.sl_retry_attempts < 1:
            raise ValueError("sl_retry_attempts must be >= 1")
        if self.tp1_close_pct + self.tp2_close_pct >= 1.0:
            raise ValueError(
                "tp1 and tp2 close fractions must leave a runner (< 1.0 combined)"
            )
        for name in ("tp1_close_pct", "tp2_close_pct"):
            if not 0.0 < getattr(self, name) < 1.0:
                raise ValueError(f"{name} must be in (0, 1)")
        if not self.tp1_r < self.tp2_r < self.tp3_r:
            raise ValueError("take-profit R multiples must increase: tp1 < tp2 < tp3")
        if self.breakeven_fee_buffer < 0:
            raise ValueError("breakeven_fee_buffer must be >= 0")
        if self.dead_man_switch_seconds <= 0:
            raise ValueError("dead_man_switch_seconds must be > 0")

    @classmethod
    def from_config(cls, cfg: Any) -> "ProtectionParams":
        base = cls()

        def protection(name: str, default: Any) -> Any:
            return cfg.get(f"protection.{name}", default)

        def take_profit(name: str, default: Any) -> Any:
            return cfg.get(f"take_profit.{name}", default)

        return cls(
            sl_price_type=str(protection("sl_price_type", base.sl_price_type)),
            sl_retry_attempts=int(protection("sl_retry_attempts", base.sl_retry_attempts)),
            emergency_close_on_sl_failure=bool(
                protection("emergency_close_on_sl_failure",
                           base.emergency_close_on_sl_failure)
            ),
            verify_liq_price=bool(protection("verify_liq_price", base.verify_liq_price)),
            move_to_breakeven=bool(protection("move_to_breakeven", base.move_to_breakeven)),
            breakeven_trigger_r=float(
                protection("breakeven_trigger_r", base.breakeven_trigger_r)
            ),
            breakeven_fee_buffer=float(
                protection("breakeven_fee_buffer", base.breakeven_fee_buffer)
            ),
            trailing_stop=bool(protection("trailing_stop", base.trailing_stop)),
            trailing_atr_multiplier=float(
                protection("trailing_atr_multiplier", base.trailing_atr_multiplier)
            ),
            dead_man_switch_seconds=int(
                protection("dead_man_switch_seconds", base.dead_man_switch_seconds)
            ),
            tp1_r=float(take_profit("tp1_r", base.tp1_r)),
            tp1_close_pct=float(take_profit("tp1_close_pct", base.tp1_close_pct)),
            tp2_r=float(take_profit("tp2_r", base.tp2_r)),
            tp2_close_pct=float(take_profit("tp2_close_pct", base.tp2_close_pct)),
            tp3_r=float(take_profit("tp3_r", base.tp3_r)),
        )

    @property
    def price_type(self) -> int:
        return PRICE_TYPE[self.sl_price_type]


@dataclass(frozen=True)
class TakeProfitLeg:
    """One rung of the ladder. ``size`` is signed opposite the position."""

    name: str
    r_multiple: float
    price: float
    size: int
    order_id: str = ""


@dataclass(frozen=True)
class ProtectionResult:
    """Outcome of protecting a filled position.

    ``ok=False`` with ``flattened=True`` means the position could not be protected and was
    market-closed. ``ok=False`` with ``flattened=False`` is the state that must never be
    reached silently: exposure exists and is unprotected, and the caller has to intervene.
    """

    ok: bool
    stage: str = ""
    reason: str = ""
    symbol: str = ""
    direction: int = 0
    stop_order_id: str = ""
    stop_price: float = float("nan")
    verified: bool = False
    flattened: bool = False
    take_profits: tuple[TakeProfitLeg, ...] = ()
    attempts: int = 0
    close_record: OrderRecord | None = None
    metrics: Mapping[str, float] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.ok

    def summary(self) -> str:
        if self.ok:
            return (
                f"{self.symbol}: stop verified at {self.stop_price:g} "
                f"({len(self.take_profits)} TP legs)"
            )
        if self.flattened:
            return f"{self.symbol}: FLATTENED — {self.reason}"
        return f"{self.symbol}: UNPROTECTED — {self.reason}"


def stop_trigger_rule(direction: int) -> int:
    """Which side the trigger fires from.

    A long's stop sits below entry, so it must fire when price falls **to or below** it
    (rule 2). A short's stop sits above and fires at or above (rule 1). Getting this
    backwards produces an order that can never trigger, which reads as "protected" in every
    listing while protecting nothing.
    """
    if direction not in (1, -1):
        raise ValueError(f"direction must be +1 or -1, got {direction!r}")
    return RULE_BELOW if direction > 0 else RULE_ABOVE


def breakeven_price(entry_price: float, direction: int, fee_buffer: float) -> float:
    """Where "break even" actually is, once the round trip is paid for.

    Moving the stop to the literal entry price is a small guaranteed loss, not a free
    trade: the round trip costs ~0.085% in fees and slippage even with the maker rebate on
    entry. The stop is therefore padded *past* entry in the profitable direction by
    ``protection.breakeven_fee_buffer`` so that being stopped at "break even" really is one.
    """
    if direction not in (1, -1):
        raise ValueError(f"direction must be +1 or -1, got {direction!r}")
    if not math.isfinite(entry_price) or entry_price <= 0:
        raise ValueError(f"entry_price {entry_price!r} is not usable")
    return entry_price * (1.0 + fee_buffer) if direction > 0 else entry_price * (1.0 - fee_buffer)


def trailing_stop_price(current_price: float, direction: int, atr: float,
                        multiplier: float) -> float:
    """An ATR-width trail behind the current price."""
    if direction not in (1, -1):
        raise ValueError(f"direction must be +1 or -1, got {direction!r}")
    if not math.isfinite(atr) or atr <= 0:
        raise ValueError(f"atr {atr!r} is not usable")
    offset = float(atr) * float(multiplier)
    return current_price - offset if direction > 0 else current_price + offset


def ratchet(existing_stop: float, candidate: float, direction: int) -> float:
    """A stop only ever moves toward profit.

    Loosening a stop to give a losing trade "room" is how a 0.25% risk becomes a 3% loss.
    The ratchet makes that unrepresentable rather than merely discouraged.
    """
    if direction not in (1, -1):
        raise ValueError(f"direction must be +1 or -1, got {direction!r}")
    if not math.isfinite(existing_stop):
        return candidate
    return max(existing_stop, candidate) if direction > 0 else min(existing_stop, candidate)


def take_profit_ladder(entry_price: float, stop_price: float, direction: int, size: int,
                       params: ProtectionParams) -> tuple[TakeProfitLeg, ...]:
    """Price and size for each rung, in R multiples of the actual stop distance.

    R is measured from the stop that was really placed, not from a nominal figure, so a
    stop capped by the liquidation ceiling (Phase 6) shrinks the ladder with it — the
    targets stay honest multiples of what is actually at risk.

    Sizes are floored and the runner takes the remainder, so the legs always sum to exactly
    the position. Rounding the last leg up would leave the exchange rejecting a
    reduce-only order for more than is held.
    """
    if direction not in (1, -1):
        raise ValueError(f"direction must be +1 or -1, got {direction!r}")
    if size == 0:
        raise ValueError("cannot build a ladder for a 0-contract position")

    risk = abs(float(entry_price) - float(stop_price))
    if not math.isfinite(risk) or risk <= 0:
        raise ValueError("stop distance must be positive to express targets in R")

    held = abs(int(size))
    first = int(math.floor(held * params.tp1_close_pct))
    second = int(math.floor(held * params.tp2_close_pct))
    runner = held - first - second
    if runner < 0:                       # defensive: __post_init__ already forbids this
        raise ValueError("take-profit fractions consume more than the whole position")

    legs = []
    for name, r_multiple, leg_size in (
        ("tp1", params.tp1_r, first),
        ("tp2", params.tp2_r, second),
        ("tp3", params.tp3_r, runner),
    ):
        if leg_size <= 0:
            # A position too small to split is normal at this account size; the whole
            # thing simply rides to the next rung that does have contracts.
            continue
        target = (
            entry_price + direction * r_multiple * risk
        )
        legs.append(TakeProfitLeg(
            name=name, r_multiple=r_multiple, price=target,
            size=-direction * leg_size,          # reduce-only: opposite the position
        ))
    return tuple(legs)


class ProtectionEngine:
    """Places and verifies protection for a filled position.

    Sequence, enforced by construction rather than by convention: arm the dead-man switch,
    place the stop, **verify it by re-reading the exchange**, then place take-profits. A
    stop that cannot be verified after bounded retries triggers an emergency close.
    """

    def __init__(self, manager: OrderManager, params: ProtectionParams | None = None) -> None:
        self.manager = manager
        self.params = params or ProtectionParams()

    # --- the sequence ------------------------------------------------------

    async def protect(self, symbol: str, direction: int, entry_price: float,
                      stop_price: float, size: int, nonce: int | str) -> ProtectionResult:
        """Run the full protect sequence for a position that has just filled."""
        if direction not in (1, -1):
            raise ValueError(f"direction must be +1 or -1, got {direction!r}")
        if size == 0:
            raise ValueError("cannot protect a 0-contract position")
        if (direction > 0 and stop_price >= entry_price) or (
            direction < 0 and stop_price <= entry_price
        ):
            raise ValueError(
                f"a {'long' if direction > 0 else 'short'} stop at {stop_price:g} is on the "
                f"wrong side of entry {entry_price:g}"
            )

        # Armed first: if the process dies during this sequence, the exchange cleans up.
        try:
            await self.manager.arm_dead_man_switch(
                self.params.dead_man_switch_seconds, symbol
            )
        except Exception as exc:  # noqa: BLE001 — not fatal; the stop still matters more
            pass

        stop_id, attempts, failure = await self._place_and_verify_stop(
            symbol, direction, stop_price, size, nonce
        )

        if not stop_id:
            return await self._emergency_close(
                symbol, direction, stop_price, attempts, failure, nonce
            )

        legs = take_profit_ladder(entry_price, stop_price, direction, size, self.params)
        placed = []
        for index, leg in enumerate(legs):
            try:
                raw = await self.manager.gateway.place_price_trigger_order(
                    symbol,
                    trigger_price=str(leg.price),
                    order_price="0",
                    size=leg.size,
                    rule=RULE_ABOVE if direction > 0 else RULE_BELOW,
                    price_type=self.params.price_type,
                    reduce_only=True,
                    text=idempotency_key(leg.name, symbol, f"{nonce}{index}"),
                )
                placed.append(
                    TakeProfitLeg(leg.name, leg.r_multiple, leg.price, leg.size,
                                  str(raw.get("id", "")))
                )
            except Exception as exc:  # noqa: BLE001
                # A missing take-profit costs upside; a missing stop costs the account.
                # The stop is verified, so this is reported rather than escalated.
                placed.append(leg)

        return ProtectionResult(
            ok=True, stage="protected",
            reason=f"stop verified after {attempts} attempt(s); {len(placed)} TP leg(s)",
            symbol=symbol, direction=direction, stop_order_id=stop_id,
            stop_price=stop_price, verified=True, take_profits=tuple(placed),
            attempts=attempts,
            metrics={"size": float(size), "entry_price": float(entry_price)},
        )

    async def _place_and_verify_stop(self, symbol: str, direction: int, stop_price: float,
                                     size: int, nonce: int | str) -> tuple[str, int, str]:
        """Place the stop and confirm it exists. Returns ``(order_id, attempts, failure)``.

        Verification re-reads ``/price_orders`` and matches on the client id, not on the
        POST response. A response can be a receipt for an order that never became live.
        """
        failure = ""
        for attempt in range(1, self.params.sl_retry_attempts + 1):
            text = idempotency_key("stp", symbol, f"{nonce}{attempt}")
            try:
                await self.manager.gateway.place_price_trigger_order(
                    symbol,
                    trigger_price=str(stop_price),
                    order_price="0",                 # market: a stop that does not fill is not a stop
                    size=-size,                      # reduce-only, opposite the position
                    rule=stop_trigger_rule(direction),
                    price_type=self.params.price_type,
                    reduce_only=True,
                    text=text,
                )
            except Exception as exc:  # noqa: BLE001
                failure = f"placement failed: {type(exc).__name__}: {exc}"
                continue

            verified_id = await self._verify_stop(symbol, text)
            if verified_id:
                return verified_id, attempt, ""
            failure = "the stop was accepted but is not live on the exchange"
        return "", self.params.sl_retry_attempts, failure

    async def _verify_stop(self, symbol: str, text: str) -> str:
        """Find our stop among the exchange's open price-triggered orders."""
        try:
            live = await self.manager.gateway.list_price_orders(symbol)
        except Exception:  # noqa: BLE001 — unverifiable is indistinguishable from absent
            return ""
        for order in live:
            initial = order.get("initial") or {}
            if str(initial.get("text", "")) == text:
                return str(order.get("id", ""))
        return ""

    async def _emergency_close(self, symbol: str, direction: int, stop_price: float,
                               attempts: int, failure: str,
                               nonce: int | str) -> ProtectionResult:
        """No verified stop: close the position rather than leave it exposed."""
        reason = (
            f"no verified stop-loss after {attempts} attempt(s)"
            + (f" ({failure})" if failure else "")
        )
        if not self.params.emergency_close_on_sl_failure:
            return ProtectionResult(
                ok=False, stage="unprotected",
                reason=reason + "; emergency_close_on_sl_failure is off, so the position "
                                "has been left open and unprotected",
                symbol=symbol, direction=direction, stop_price=stop_price,
                attempts=attempts,
            )

        record = await self.manager.close_position(
            symbol, f"{nonce}x", reason="emergency close: no verified stop"
        )
        remaining = None
        try:
            remaining = await self.manager.position_size(symbol)
        except ExecutionError:
            remaining = None

        flattened = remaining == 0
        return ProtectionResult(
            ok=False,
            stage="flattened" if flattened else "unprotected",
            reason=(
                reason + ("; position market-closed" if flattened else
                          f"; the close could not be confirmed (size={remaining})")
            ),
            symbol=symbol, direction=direction, stop_price=stop_price,
            flattened=flattened, attempts=attempts, close_record=record,
        )

    # --- managing the stop over the life of the trade ---------------------

    async def move_stop(self, symbol: str, direction: int, new_stop: float,
                        current_stop_id: str, size: int, nonce: int | str,
                        existing_stop: float = float("nan")) -> ProtectionResult:
        """Replace the resting stop with one closer to profit.

        New stop first, old stop second. Cancelling first would open a window with no
        protection at all, which is the exact state this module exists to prevent — and
        the window would sit right where the trade is already moving fast.

        The ratchet is applied here too, so a caller that computes a looser trailing stop
        from stale data cannot widen the risk.
        """
        candidate = ratchet(existing_stop, float(new_stop), direction)
        if math.isfinite(existing_stop) and candidate == existing_stop:
            return ProtectionResult(
                ok=True, stage="unchanged",
                reason=f"stop stays at {existing_stop:g}; {new_stop:g} would loosen it",
                symbol=symbol, direction=direction, stop_order_id=current_stop_id,
                stop_price=existing_stop, verified=True,
            )

        stop_id, attempts, failure = await self._place_and_verify_stop(
            symbol, direction, candidate, size, f"{nonce}m"
        )
        if not stop_id:
            return ProtectionResult(
                ok=False, stage="move_failed",
                reason=(f"could not place the replacement stop ({failure}); the original "
                        f"stop {current_stop_id} is still in place"),
                symbol=symbol, direction=direction, stop_order_id=current_stop_id,
                stop_price=existing_stop, verified=True, attempts=attempts,
            )

        if current_stop_id:
            try:
                await self.manager.gateway.cancel_price_order(current_stop_id)
            except Exception as exc:  # noqa: BLE001
                # Two live stops is a safe failure: the nearer one triggers first and the
                # other is reduce-only, so it cannot open anything.
                return ProtectionResult(
                    ok=True, stage="moved_stale",
                    reason=(f"new stop {stop_id} verified at {candidate:g} but the old one "
                            f"could not be cancelled ({exc}); both are reduce-only"),
                    symbol=symbol, direction=direction, stop_order_id=stop_id,
                    stop_price=candidate, verified=True, attempts=attempts,
                )

        return ProtectionResult(
            ok=True, stage="moved",
            reason=f"stop moved to {candidate:g}",
            symbol=symbol, direction=direction, stop_order_id=stop_id,
            stop_price=candidate, verified=True, attempts=attempts,
        )

    async def move_to_breakeven(self, symbol: str, direction: int, entry_price: float,
                                current_stop_id: str, size: int, nonce: int | str,
                                existing_stop: float = float("nan")) -> ProtectionResult:
        """Move the stop to a fee-padded break-even. Caller decides when +1R was reached."""
        if not self.params.move_to_breakeven:
            return ProtectionResult(
                ok=True, stage="disabled", reason="move_to_breakeven is off",
                symbol=symbol, direction=direction, stop_order_id=current_stop_id,
                stop_price=existing_stop, verified=True,
            )
        target = breakeven_price(entry_price, direction, self.params.breakeven_fee_buffer)
        return await self.move_stop(symbol, direction, target, current_stop_id, size,
                                    nonce, existing_stop)

    async def trail(self, symbol: str, direction: int, current_price: float, atr: float,
                    current_stop_id: str, size: int, nonce: int | str,
                    existing_stop: float = float("nan")) -> ProtectionResult:
        """Trail the stop an ATR-width behind price. Never loosens, thanks to the ratchet."""
        if not self.params.trailing_stop:
            return ProtectionResult(
                ok=True, stage="disabled", reason="trailing_stop is off",
                symbol=symbol, direction=direction, stop_order_id=current_stop_id,
                stop_price=existing_stop, verified=True,
            )
        target = trailing_stop_price(current_price, direction, atr,
                                     self.params.trailing_atr_multiplier)
        return await self.move_stop(symbol, direction, target, current_stop_id, size,
                                    nonce, existing_stop)

    # --- reconciliation ----------------------------------------------------

    async def audit(self, symbol: str) -> ProtectionResult:
        """Is what is open right now actually protected?

        For use on startup and after any reconnect, where the answer is genuinely unknown.
        Flat is fine. Size with no live stop is the state that must never persist, so it is
        reported as ``ok=False`` for the caller to resolve.
        """
        size = await self.manager.position_size(symbol)
        if size == 0:
            return ProtectionResult(ok=True, stage="flat", reason="no position open",
                                    symbol=symbol)
        try:
            live = await self.manager.gateway.list_price_orders(symbol)
        except Exception as exc:  # noqa: BLE001
            return ProtectionResult(
                ok=False, stage="unverified",
                reason=f"{size} contracts open and the protective orders could not be read: {exc}",
                symbol=symbol, direction=1 if size > 0 else -1,
            )

        direction = 1 if size > 0 else -1
        stops = [
            order for order in live
            if int(order.get("initial", {}).get("size", 0) or 0) * direction < 0
        ]
        if not stops:
            return ProtectionResult(
                ok=False, stage="unprotected",
                reason=f"{size} contracts open with no protective order on the exchange",
                symbol=symbol, direction=direction,
            )
        return ProtectionResult(
            ok=True, stage="protected",
            reason=f"{size} contracts open with {len(stops)} protective order(s)",
            symbol=symbol, direction=direction, verified=True,
            stop_order_id=str(stops[0].get("id", "")),
            stop_price=float(stops[0].get("trigger", {}).get("price", "nan")),
        )
