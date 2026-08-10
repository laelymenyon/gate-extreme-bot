"""Risk-derived position sizing.

PHASE 6.

Turns an accepted :class:`~strategy.signal_engine.Signal` into a concrete, checked
order size. Two rules govern everything here:

* **Size comes from risk, never from leverage.**
  ``size = (equity x risk.per_trade) / stop_distance``. Leverage does not appear in that
  expression and cannot change it — it only decides how much margin the resulting
  notional locks up. ``test_leverage_changes_margin_but_not_risk`` pins that.
* **Rounding only ever reduces exposure.** Contracts are floored, never rounded up, and
  every cap in this module shrinks the position. The one case where the exchange minimum
  would force the position *up* is a refusal, not a rounding: an ``order_size_min`` that
  risks more than the per-trade budget means the trade cannot be taken at this account
  size, and quietly taking it anyway would break the only number the whole design rests on.

**The stop ceiling comes from the tiered maintenance rate, never the flat field.**
``liq_distance ~= 1/leverage - maintenance_rate - taker_fee``, and the widest stop that
still clears liquidation by ``protection.liquidation_buffer`` is that minus the buffer
(``config.max_stop_distance``, the single definition of the formula). Gate.io's
contract-level ``maintenance_rate`` is only the tier-1 value — BTC_USDT has 19 tiers and
the rate climbs 0.30% -> 0.35% -> 0.45% with notional — so this module requires a tier
object and refuses to run without one. The flat field would understate liquidation risk
exactly when the position is large enough for it to matter.

Because the tier depends on notional and notional depends on the stop, which depends on
the tier, the two are solved by a short fixed-point iteration. It is monotone: a higher
tier means a higher maintenance rate, a tighter ceiling, a tighter stop and therefore a
larger notional, so the tier index only ever climbs and the loop terminates within the
number of tiers. A run that somehow fails to settle refuses the trade.

**What this module is not.** It does not import ``exchange/``: contracts and tiers arrive
as structural protocols, so there is no network path and no order-placing path in this
layer (``test_risk_package_has_no_exchange_import``). The margin top-up solver and the
post-fill re-read of the exchange's own ``liq_price`` are Phase 7; the ceiling computed
here is a pre-trade estimate from published tier data, which is exactly what deciding
*whether to place* an order needs.

**No lookahead.** ``plan_position`` takes ``as_of`` and truncates through
``Candles.head``, so replaying bar *i* and running live at bar *i* are the same code path.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

import numpy as np

from config import max_stop_distance
from strategy.indicators import Candles, atr, confirmed_swing_levels

__all__ = [
    "ContractSpec",
    "TierSpec",
    "SizingParams",
    "StopPlan",
    "PositionPlan",
    "select_tier",
    "liquidation_distance",
    "resolve_stop",
    "plan_position",
]

#: Tolerance for re-checking a distance that was just derived from a price.
#: ``entry * (1 - 0.001)`` does not divide back to exactly 0.001, so a stop sitting on the
#: floor would otherwise be rejected by its own rounding error. Real grid violations are
#: one tick wide — many orders of magnitude above this.
_FLOAT_SLOP = 1e-12


class ContractSpec(Protocol):
    """The contract fields sizing needs.

    A structural protocol rather than an import: ``exchange.gate_client.Contract``
    satisfies it, and so does a test double, but ``risk/`` never imports the module that
    can place orders.
    """

    name: str
    quanto_multiplier: float      # coin amount per contract
    order_size_min: int
    order_size_max: int
    order_price_round: float


class TierSpec(Protocol):
    """One row of ``GET /futures/{settle}/risk_limit_tiers``.

    Satisfied by ``exchange.gate_client.RiskTier``.
    """

    tier: int
    risk_limit: float             # maximum notional this tier covers
    maintenance_rate: float
    leverage_max: float


@dataclass(frozen=True)
class SizingParams:
    """Thresholds from ``risk``, ``leverage``, ``protection`` and ``stop_loss``.

    Defaults mirror the shipped ``config.yaml``.
    """

    risk_per_trade: float = 0.0025
    leverage: int = 100
    liquidation_buffer: float = 0.003
    taker_fee: float = 0.00075
    maker_fee: float = -0.0001
    sl_method: str = "auto"              # auto | atr | structure
    atr_period: int = 14
    atr_multiplier: float = 1.5
    min_distance: float = 0.001
    max_distance: float = 0.02
    structure_lookback: int = 50
    on_sl_exceeds_max: str = "cap"       # cap | skip
    min_sl_atr_ratio: float = 0.20
    swing_left: int = 2
    swing_right: int = 2

    def __post_init__(self) -> None:
        if not 0.0 < self.risk_per_trade <= 0.05:
            raise ValueError(
                f"risk_per_trade must be in (0, 0.05], got {self.risk_per_trade!r}"
            )
        if int(self.leverage) < 1:
            raise ValueError(f"leverage must be >= 1, got {self.leverage!r}")
        if self.liquidation_buffer < 0:
            raise ValueError("liquidation_buffer must be >= 0")
        if not 0.0 < self.min_distance < self.max_distance:
            raise ValueError(
                f"require 0 < min_distance < max_distance, got "
                f"{self.min_distance!r} / {self.max_distance!r}"
            )
        if self.sl_method not in ("auto", "atr", "structure"):
            raise ValueError(f"sl_method must be auto, atr or structure, got {self.sl_method!r}")
        if self.on_sl_exceeds_max not in ("cap", "skip"):
            raise ValueError(
                f"on_sl_exceeds_max must be 'cap' or 'skip', got {self.on_sl_exceeds_max!r}"
            )
        if self.atr_multiplier <= 0:
            raise ValueError("atr_multiplier must be > 0")
        if self.min_sl_atr_ratio < 0:
            raise ValueError("min_sl_atr_ratio must be >= 0")

    @classmethod
    def from_config(cls, cfg: Any) -> "SizingParams":
        base = cls()

        def stop(name: str, default: Any) -> Any:
            return cfg.get(f"stop_loss.{name}", default)

        return cls(
            risk_per_trade=float(cfg.get("risk.per_trade", base.risk_per_trade)),
            leverage=int(cfg.get("leverage.default", base.leverage)),
            liquidation_buffer=float(
                cfg.get("protection.liquidation_buffer", base.liquidation_buffer)
            ),
            taker_fee=float(cfg.get("backtest.fee_taker", base.taker_fee)),
            maker_fee=float(cfg.get("backtest.fee_maker", base.maker_fee)),
            sl_method=str(stop("method", base.sl_method)),
            atr_period=int(stop("atr_period", base.atr_period)),
            atr_multiplier=float(stop("atr_multiplier", base.atr_multiplier)),
            min_distance=float(stop("min_distance", base.min_distance)),
            max_distance=float(stop("max_distance", base.max_distance)),
            structure_lookback=int(stop("structure_lookback", base.structure_lookback)),
            on_sl_exceeds_max=str(stop("on_sl_exceeds_max", base.on_sl_exceeds_max)),
            min_sl_atr_ratio=float(stop("min_sl_atr_ratio", base.min_sl_atr_ratio)),
        )

    def risk_amount(self, equity: float) -> float:
        """The cash a single stop-out is allowed to cost. Flat — never scaled by history."""
        return float(equity) * self.risk_per_trade


@dataclass(frozen=True)
class StopPlan:
    """Where the stop goes, and why. ``ok=False`` means no stop fits — do not trade."""

    ok: bool
    stage: str = ""
    reason: str = ""
    distance: float = float("nan")       # fraction of entry price
    price: float = float("nan")
    method: str = ""                     # atr | structure
    capped: bool = False
    ceiling: float = float("nan")        # widest stop the liquidation buffer permits
    atr_distance: float = float("nan")   # ATR as a fraction of price, for the noise check


@dataclass(frozen=True)
class PositionPlan:
    """A fully checked order size, or a refusal carrying the stage that refused it."""

    symbol: str
    ok: bool
    stage: str = ""
    reason: str = ""
    direction: int = 0
    size: int = 0                        # signed contract count: + long, - short
    entry_price: float = float("nan")
    stop: StopPlan | None = None
    risk_amount: float = 0.0             # the budget
    max_loss: float = 0.0                # what a stop-out actually costs, price only
    max_loss_after_fees: float = 0.0     # ...plus the round-trip fee this stop implies
    notional: float = 0.0
    margin: float = 0.0
    coin_amount: float = 0.0
    leverage: int = 0
    tier: TierSpec | None = None
    capped_by: tuple[str, ...] = ()      # every limit that shrank the position
    metrics: Mapping[str, float] = field(default_factory=dict)

    @property
    def side(self) -> str:
        return {1: "long", -1: "short"}.get(self.direction, "none")

    def summary(self) -> str:
        if not self.ok:
            return f"{self.symbol}: no size — {self.reason}"
        stop = self.stop.distance * 100 if self.stop else float("nan")
        return (
            f"{self.symbol}: {self.side} {abs(self.size)} contracts, stop {stop:.3f}%, "
            f"risking {self.max_loss:.2f} of {self.risk_amount:.2f} budget, "
            f"margin {self.margin:.2f}"
        )


def _no_stop(stage: str, reason: str, **extra: Any) -> StopPlan:
    return StopPlan(ok=False, stage=stage, reason=reason, **extra)


def _no(symbol: str, stage: str, reason: str, **extra: Any) -> PositionPlan:
    return PositionPlan(symbol=symbol, ok=False, stage=stage, reason=reason, **extra)


def select_tier(tiers: Sequence[TierSpec], notional: float) -> TierSpec:
    """The tier whose ``risk_limit`` covers ``notional``.

    Deliberately mirrors ``exchange.gate_client.select_tier`` instead of importing it, so
    that ``risk/`` keeps no path to the order-placing module.
    ``test_tier_selection_agrees_with_the_exchange_client`` pins the two together against
    the 19-tier BTC_USDT ladder captured live, which is a stronger guarantee than a shared
    import: it checks behaviour rather than provenance.
    """
    ordered = sorted(tiers, key=lambda t: t.risk_limit)
    if not ordered:
        raise ValueError("no risk tiers supplied")
    for tier in ordered:
        if notional <= tier.risk_limit:
            return tier
    return ordered[-1]


def liquidation_distance(leverage: int, maintenance_rate: float, taker_fee: float) -> float:
    """Distance from entry to liquidation as a fraction of entry price.

    ``1/leverage - maintenance_rate - taker_fee``. A pre-trade estimate from published
    tier data; the authoritative check re-reads ``Position.liq_price`` from the exchange
    after the fill, which is Phase 7.
    """
    return (1.0 / float(leverage)) - float(maintenance_rate) - float(taker_fee)


def _tick_toward_entry(price: float, tick: float, direction: int) -> float:
    """Round a stop price onto the contract's price grid, toward the entry.

    Toward entry is the safety-critical direction: it can only shorten the distance to the
    stop, never lengthen it, so a rounded stop can never end up nearer liquidation than the
    ceiling allows. The cost is at most one tick of extra tightness, and the caller re-derives
    the distance from the rounded price so the position is sized on the stop that will
    actually be placed rather than on the unrounded ideal.
    """
    if not np.isfinite(tick) or tick <= 0:
        return price
    # Long: the stop sits below entry, so toward entry is up. Short: the mirror.
    if direction > 0:
        return math.ceil(price / tick - 1e-9) * tick
    return math.floor(price / tick + 1e-9) * tick


def resolve_stop(
    view: Candles,
    direction: int,
    entry_price: float,
    ceiling: float,
    params: SizingParams,
    price_tick: float = 0.0,
) -> StopPlan:
    """Where the protective stop goes for this bar, or why none fits.

    Order of operations, each step recorded so a refusal can be explained afterwards:

    1. **Candidate distance.** ``atr`` uses ``atr_multiplier x ATR``; ``structure`` uses the
       nearest confirmed swing level beyond the entry; ``auto`` takes the wider of the two.
       A structure stop with no confirmed pivot yields NaN, which falls back to ATR —
       Phase 4's rule that a missing level is missing data, never evidence of open space.
    2. **Floor** at ``stop_loss.min_distance``.
    3. **Ceiling** at ``min(stop_loss.max_distance, liquidation ceiling)``. Exceeding it is
       resolved by ``on_sl_exceeds_max``: ``cap`` clamps and trades, ``skip`` refuses.
    4. **Snap to the price grid**, toward entry, and re-derive the distance from the
       rounded price.
    5. **Noise check.** A stop tighter than ``min_sl_atr_ratio`` x ATR sits inside ordinary
       bar-to-bar movement and is not a stop at all, so the trade is skipped whatever the
       score said.
    """
    if direction not in (1, -1):
        raise ValueError(f"direction must be +1 (long) or -1 (short), got {direction!r}")
    if not np.isfinite(entry_price) or entry_price <= 0:
        return _no_stop("entry_price", f"entry price {entry_price!r} is not usable")

    atr_now = float("nan")
    if len(view):
        atr_now = float(atr(view.high, view.low, view.close, params.atr_period)[-1])
    atr_fraction = (
        atr_now / entry_price if np.isfinite(atr_now) and atr_now > 0 else float("nan")
    )

    atr_candidate = (
        params.atr_multiplier * atr_fraction if np.isfinite(atr_fraction) else float("nan")
    )

    structure_candidate = float("nan")
    if params.sl_method in ("auto", "structure") and len(view):
        values = view.low if direction > 0 else view.high
        levels = confirmed_swing_levels(
            values, as_of=len(view) - 1, left=params.swing_left,
            right=params.swing_right, lookback=params.structure_lookback,
            high=direction < 0,
        )
        beyond = levels[levels < entry_price] if direction > 0 else levels[levels > entry_price]
        if beyond.size:
            level = float(beyond.max() if direction > 0 else beyond.min())
            structure_candidate = abs(entry_price - level) / entry_price

    if params.sl_method == "atr":
        candidate, method = atr_candidate, "atr"
    elif params.sl_method == "structure":
        # No confirmed pivot is missing information, not open space: fall back to ATR
        # rather than inventing a level or refusing a structurally sound setup.
        if np.isfinite(structure_candidate):
            candidate, method = structure_candidate, "structure"
        else:
            candidate, method = atr_candidate, "atr"
    else:  # auto — the wider of the two survives more noise
        candidate, method = float("nan"), ""
        for value, name in ((atr_candidate, "atr"), (structure_candidate, "structure")):
            if np.isfinite(value) and (not np.isfinite(candidate) or value > candidate):
                candidate, method = value, name

    if not np.isfinite(candidate) or candidate <= 0:
        return _no_stop(
            "stop_source",
            "no usable stop distance: ATR is unavailable and no confirmed structure level "
            "lies beyond the entry",
            atr_distance=atr_fraction, ceiling=ceiling,
        )

    effective_ceiling = min(float(ceiling), params.max_distance)
    if not np.isfinite(effective_ceiling) or effective_ceiling <= 0:
        return _no_stop(
            "liquidation_ceiling",
            f"no stop fits: liquidation leaves {ceiling * 100:.3f}% of room at "
            f"{params.leverage}x after the {params.liquidation_buffer * 100:.2f}% buffer",
            atr_distance=atr_fraction, ceiling=ceiling, method=method,
        )
    if params.min_distance > effective_ceiling:
        return _no_stop(
            "liquidation_ceiling",
            f"stop_loss.min_distance {params.min_distance * 100:.3f}% exceeds the widest "
            f"permissible stop {effective_ceiling * 100:.3f}%",
            atr_distance=atr_fraction, ceiling=effective_ceiling, method=method,
        )

    distance = max(candidate, params.min_distance)
    capped = False
    if distance > effective_ceiling:
        if params.on_sl_exceeds_max == "skip":
            return _no_stop(
                "sl_exceeds_max",
                f"{method} stop {distance * 100:.3f}% exceeds the "
                f"{effective_ceiling * 100:.3f}% ceiling and on_sl_exceeds_max=skip",
                atr_distance=atr_fraction, ceiling=effective_ceiling, method=method,
            )
        # Clamp to one tick *inside* the ceiling rather than onto it.
        #
        # The Phase 7 guard re-checks this stop against a liquidation price it rounds
        # toward entry — its own conservative rule — which shortens the gap by up to one
        # tick. Capping onto the ceiling exactly leaves nothing for that, so whether a
        # maximally-capped stop survives the guard came down to where the last decimals
        # landed: it passed most of the time and was vetoed intermittently, for no reason
        # anyone chose. Reserving a tick here makes the two layers compose deterministically.
        #
        # The reserve costs a tick of stop room and moves the stop *closer* to entry, so it
        # is the safe direction. ``ceiling`` still reports the true limit; only the distance
        # actually used is pulled inside it.
        reserve = float(price_tick) / entry_price if float(price_tick or 0) > 0 else 0.0
        distance = max(params.min_distance, effective_ceiling - reserve)
        capped = True

    raw_price = entry_price * (1.0 - distance) if direction > 0 else entry_price * (1.0 + distance)
    stop_price = _tick_toward_entry(raw_price, float(price_tick), direction)
    if not np.isfinite(stop_price) or stop_price <= 0:
        return _no_stop(
            "price_grid", f"stop price {stop_price!r} is not a usable price",
            atr_distance=atr_fraction, ceiling=effective_ceiling, method=method,
        )
    distance = abs(entry_price - stop_price) / entry_price

    if distance <= 0:
        return _no_stop(
            "price_grid",
            f"the stop rounds onto the entry price at a tick of {price_tick:g}; there is "
            "no room for a stop on this price grid",
            atr_distance=atr_fraction, ceiling=effective_ceiling, method=method,
        )
    if distance < params.min_distance - _FLOAT_SLOP:
        return _no_stop(
            "price_grid",
            f"rounding onto the {price_tick:g} price grid pulled the stop to "
            f"{distance * 100:.3f}%, inside the {params.min_distance * 100:.3f}% minimum",
            atr_distance=atr_fraction, ceiling=effective_ceiling, method=method,
        )

    # A stop inside ordinary bar-to-bar movement is not protection, it is a coin flip that
    # pays the fee twice. At the 0.125% ceiling on an mmr-0.50% contract this is the check
    # that keeps the bot out of contracts whose noise is wider than their permissible stop.
    # With no ATR there is nothing to compare against; that path is only reachable via a
    # structure stop, since the other two methods already refuse without one.
    if np.isfinite(atr_fraction) and distance < params.min_sl_atr_ratio * atr_fraction:
        return _no_stop(
            "noise",
            f"stop {distance * 100:.3f}% is only {distance / atr_fraction:.2f} ATR wide, "
            f"below the {params.min_sl_atr_ratio:.2f} minimum — inside noise",
            atr_distance=atr_fraction, ceiling=effective_ceiling, method=method,
        )

    return StopPlan(
        ok=True,
        stage="ok",
        reason=(
            f"{method} stop {distance * 100:.3f}%"
            + (f", capped at the {effective_ceiling * 100:.3f}% ceiling" if capped else "")
        ),
        distance=distance,
        price=stop_price,
        method=method,
        capped=capped,
        ceiling=effective_ceiling,
        atr_distance=atr_fraction,
    )


def plan_position(
    symbol: str,
    direction: int,
    entry_price: float,
    candles: Candles,
    contract: ContractSpec,
    tiers: Sequence[TierSpec],
    equity: float,
    available: float,
    params: SizingParams | None = None,
    as_of: int | None = None,
) -> PositionPlan:
    """Size one position, or refuse and say which check refused it.

    ``available`` is the account's free margin. ``tiers`` must be the contract's real
    ``risk_limit_tiers`` — an empty sequence is a refusal, not a fallback to the flat
    ``maintenance_rate``, because that field is only tier 1 and understates liquidation
    risk precisely as size grows.
    """
    params = params or SizingParams()

    if direction not in (1, -1):
        return _no(symbol, "direction",
                   f"direction must be +1 (long) or -1 (short), got {direction!r}")
    if not np.isfinite(entry_price) or entry_price <= 0:
        return _no(symbol, "entry_price", f"entry price {entry_price!r} is not usable")
    if not np.isfinite(equity) or equity <= 0:
        return _no(symbol, "equity", f"equity {equity!r} is not usable")
    if not np.isfinite(available) or available < 0:
        return _no(symbol, "margin", f"available margin {available!r} is not usable")
    if not tiers:
        return _no(
            symbol, "risk_tiers",
            "no risk_limit_tiers supplied; the stop ceiling must come from the tier "
            "matching actual notional, and the contract's flat maintenance_rate is only "
            "the tier-1 value",
        )

    multiplier = float(contract.quanto_multiplier)
    if not np.isfinite(multiplier) or multiplier <= 0:
        return _no(symbol, "contract",
                   f"quanto_multiplier {multiplier!r} is not usable")

    if len(candles) == 0:
        return _no(symbol, "data", "no candles supplied")
    if as_of is None:
        as_of = len(candles) - 1
    as_of = int(as_of)
    if not 0 <= as_of < len(candles):
        return _no(symbol, "data", f"as_of must be in [0, {len(candles) - 1}], got {as_of}")
    # Truncate rather than promise not to peek — the same guard Phase 5 uses.
    view = candles.head(as_of + 1)

    budget = params.risk_amount(equity)
    ordered = sorted(tiers, key=lambda t: t.risk_limit)

    # --- tier / stop fixed point -------------------------------------------
    # Start at the lowest tier (lowest maintenance rate, widest ceiling, widest stop,
    # smallest notional) and climb. Each escalation can only raise the notional, so the
    # tier index is non-decreasing and the loop settles within the length of the ladder.
    tier = ordered[0]
    stop: StopPlan | None = None
    for _ in range(len(ordered) + 1):
        ceiling = max_stop_distance(
            params.leverage, tier.maintenance_rate, params.taker_fee,
            params.liquidation_buffer,
        )
        stop = resolve_stop(
            view, direction, entry_price, ceiling, params,
            price_tick=float(getattr(contract, "order_price_round", 0.0) or 0.0),
        )
        if not stop.ok:
            return _no(symbol, stop.stage, stop.reason, direction=direction,
                       entry_price=entry_price, stop=stop, risk_amount=budget,
                       leverage=params.leverage, tier=tier)
        # The notional the budget implies before flooring. Selecting the tier from this
        # upper bound is the conservative side: the floored position is never larger, so
        # the maintenance rate used is never lower than the one that will actually apply.
        chosen = select_tier(ordered, budget / stop.distance)
        if chosen.tier == tier.tier:
            break
        tier = chosen
    else:
        return _no(
            symbol, "risk_tiers",
            "position size and risk tier did not converge; refusing rather than sizing "
            "against a maintenance rate that does not match the resulting notional",
            direction=direction, entry_price=entry_price, stop=stop,
            risk_amount=budget, leverage=params.leverage, tier=tier,
        )

    assert stop is not None and stop.ok  # the loop returns on any failure

    if params.leverage > tier.leverage_max:
        return _no(
            symbol, "risk_tiers",
            f"tier {tier.tier} caps leverage at {tier.leverage_max:g}x, below the "
            f"configured {params.leverage}x; the exchange would reject the position",
            direction=direction, entry_price=entry_price, stop=stop,
            risk_amount=budget, leverage=params.leverage, tier=tier,
        )

    # --- size -------------------------------------------------------------
    # Loss if the stop fills, per contract: multiplier coins x the stop's price distance.
    loss_per_contract = multiplier * entry_price * stop.distance
    if loss_per_contract <= 0:
        return _no(symbol, "size", "stop distance rounds to zero loss per contract",
                   direction=direction, entry_price=entry_price, stop=stop,
                   risk_amount=budget, leverage=params.leverage, tier=tier)

    size = int(math.floor(budget / loss_per_contract))  # floor: never over-risk
    capped_by: list[str] = []

    order_max = int(getattr(contract, "order_size_max", 0) or 0)
    if order_max > 0 and size > order_max:
        size = order_max
        capped_by.append("order_size_max")

    # The top tier's risk_limit is the largest notional the exchange will carry.
    top_limit = float(ordered[-1].risk_limit)
    max_size_by_tier = int(math.floor(top_limit / (multiplier * entry_price)))
    if size > max_size_by_tier:
        size = max_size_by_tier
        capped_by.append("risk_limit")

    order_min = int(getattr(contract, "order_size_min", 1) or 1)
    if size < order_min:
        # Deliberately not rounded up. The minimum order would risk more than the budget,
        # and silently exceeding risk.per_trade would break the one invariant everything
        # else is built on.
        smallest = order_min * loss_per_contract
        return _no(
            symbol, "order_size_min",
            f"the smallest tradable order ({order_min} contracts) would risk "
            f"{smallest:.2f}, above the {budget:.2f} budget "
            f"({params.risk_per_trade * 100:.2f}% of {equity:.2f})",
            direction=direction, entry_price=entry_price, stop=stop, risk_amount=budget,
            leverage=params.leverage, tier=tier, capped_by=tuple(capped_by),
        )

    coin_amount = size * multiplier
    notional = coin_amount * entry_price
    margin = notional / float(params.leverage)
    max_loss = size * loss_per_contract
    # Entry is post-only (maker, a rebate) and the stop is taker — see ARCHITECTURE §5.
    # Sizing follows the spec's price-only formula; the fee-inclusive figure is reported
    # so the real cost of a stop-out is never quietly optimistic.
    fee_cost = notional * (params.maker_fee + params.taker_fee)

    if margin > available:
        # A refusal never carries a size: there is no partial answer here, and a caller
        # that skipped the `ok` check must not find a tradable number waiting for it.
        return _no(
            symbol, "margin",
            f"{size} contracts need {margin:.2f} of isolated margin at "
            f"{params.leverage}x but only {available:.2f} is available",
            direction=direction, entry_price=entry_price, stop=stop, risk_amount=budget,
            leverage=params.leverage, tier=tier, capped_by=tuple(capped_by),
            notional=notional, margin=margin,
        )

    return PositionPlan(
        symbol=symbol,
        ok=True,
        stage="ok",
        reason=(
            f"{abs(size)} contracts risking {max_loss:.2f} of a {budget:.2f} budget on a "
            f"{stop.distance * 100:.3f}% stop"
            + (f" (capped by {', '.join(capped_by)})" if capped_by else "")
        ),
        direction=direction,
        size=size * direction,
        entry_price=entry_price,
        stop=stop,
        risk_amount=budget,
        max_loss=max_loss,
        max_loss_after_fees=max_loss + fee_cost,
        notional=notional,
        margin=margin,
        coin_amount=coin_amount,
        leverage=params.leverage,
        tier=tier,
        capped_by=tuple(capped_by),
        metrics={
            "loss_per_contract": loss_per_contract,
            "maintenance_rate": float(tier.maintenance_rate),
            "liquidation_distance": liquidation_distance(
                params.leverage, tier.maintenance_rate, params.taker_fee
            ),
            "stop_ceiling": stop.ceiling,
            "risk_used_fraction": max_loss / budget if budget else float("nan"),
            "equity": float(equity),
        },
    )
