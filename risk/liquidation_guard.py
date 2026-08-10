"""The most safety-critical module: does the stop fit between entry and liquidation?

PHASE 7.

Everything else in the bot can be wrong and cost a trade. This one being wrong costs the
margin, because at 100x the distance from entry to liquidation is 0.625% on BTC and 0.425%
on everything else — a single bad candle wide. The rule it exists to enforce is one line:

    **Liquidation is never a stop-loss.**

A position is only allowed to exist when the protective stop sits at least
``protection.liquidation_buffer`` clear of the liquidation price, measured on the *mark*
price series, because that is the series liquidation is computed from.

    liq_distance ~= 1/leverage - maintenance_rate - taker_fee

``maintenance_rate`` is the trap. Gate.io's contract-level field is only tier 1 —
BTC_USDT has 19 tiers and the rate climbs 0.30% -> 0.35% -> 0.45% as notional grows, while
``leverage_max`` *falls* per tier (200x at tier 1 down to 50x at tier 8). Reading the flat
field understates liquidation risk precisely when the position is big enough for it to
matter. This module therefore takes a :class:`TierSnapshot` — never a bare rate — and
resolves the tier from the position's actual notional.

**Fail-closed, everywhere.** Missing tiers, an empty ladder, a stale snapshot, a snapshot
timestamped in the future, a non-monotonic ladder, a non-finite rate, cross margin, or a
leverage above the configured ceiling all produce a refusal naming the stage that refused.
There is no default maintenance rate and no "probably fine" path: refusing a trade costs
an opportunity, and guessing costs the margin.

**Rounding is conservative in one direction only.** The predicted liquidation price is
snapped onto the contract's mark-price grid *toward the entry*, so a rounded prediction is
never further from entry than the truth. Every comparison in this module is made against
that pessimistic figure.

**Nothing here places, moves, or closes an order.** ``verify_fill`` returns
``action="flatten"`` as a *recommendation* for Phase 10 to act on; this module has no
network path and imports no ``exchange`` module. It also does not modify anything from
Phases 1-6: it consumes the Phase 6 :class:`~risk.position_sizer.PositionPlan` and answers
a question about it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from risk.position_sizer import (
    ContractSpec,
    PositionPlan,
    TierSpec,
    liquidation_distance,
    select_tier,
)

__all__ = [
    "TierSnapshot",
    "LiquidationParams",
    "LiquidationVerdict",
    "liquidation_distance",
    "liquidation_price",
    "required_effective_leverage",
    "assess",
    "assess_plan",
    "verify_fill",
]

#: Slack allowed on a snapshot timestamped slightly ahead of the local clock. Beyond this
#: the two clocks disagree, and an age computed from them cannot be trusted.
CLOCK_SKEW_TOLERANCE_SECONDS = 5.0

#: Comparisons on distances that were derived from prices carry ordinary float error;
#: real violations are ticks wide, many orders of magnitude above this.
_FLOAT_SLOP = 1e-12


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


@dataclass(frozen=True)
class TierSnapshot:
    """A contract's risk-limit ladder, with the time it was read from the exchange.

    The timestamp is not decoration. Tier ladders change rarely but they do change, and a
    guard reasoning from a ladder fetched days ago is reasoning about a different contract.
    :func:`assess` refuses a snapshot older than ``protection.risk_tier_max_age_seconds``.
    """

    symbol: str
    tiers: tuple[TierSpec, ...]
    fetched_at: float

    @classmethod
    def of(cls, symbol: str, tiers: Sequence[TierSpec], fetched_at: float) -> "TierSnapshot":
        return cls(symbol=symbol, tiers=tuple(tiers), fetched_at=float(fetched_at))

    def age(self, now: float) -> float:
        return float(now) - self.fetched_at


@dataclass(frozen=True)
class LiquidationParams:
    """Limits from ``leverage``, ``protection`` and ``backtest``. Defaults mirror the config."""

    leverage: int = 100
    max_leverage: int = 100
    margin_mode: str = "isolated"
    liquidation_buffer: float = 0.003
    taker_fee: float = 0.00075
    allow_margin_topup: bool = False
    max_effective_leverage: int = 100
    verify_liq_price: bool = True
    tier_max_age_seconds: float = 3600.0
    liq_price_tolerance: float = 0.002

    def __post_init__(self) -> None:
        if int(self.leverage) < 1:
            raise ValueError(f"leverage must be >= 1, got {self.leverage!r}")
        if int(self.max_leverage) < 1:
            raise ValueError(f"max_leverage must be >= 1, got {self.max_leverage!r}")
        if self.leverage > self.max_leverage:
            raise ValueError(
                f"leverage {self.leverage}x exceeds the {self.max_leverage}x ceiling"
            )
        if self.margin_mode != "isolated":
            raise ValueError(
                f"margin_mode must be 'isolated', got {self.margin_mode!r}; cross margin "
                "puts the whole account behind one position and is forbidden"
            )
        if not 0.0 <= self.liquidation_buffer <= 0.10:
            raise ValueError(f"liquidation_buffer out of range: {self.liquidation_buffer!r}")
        if self.tier_max_age_seconds <= 0:
            raise ValueError("tier_max_age_seconds must be > 0")
        if self.liq_price_tolerance < 0:
            raise ValueError("liq_price_tolerance must be >= 0")
        if self.allow_margin_topup and self.max_effective_leverage >= self.leverage:
            raise ValueError(
                "allow_margin_topup requires max_effective_leverage < leverage: topping up "
                "margin only helps if it lowers effective leverage"
            )

    @classmethod
    def from_config(cls, cfg: Any) -> "LiquidationParams":
        base = cls()
        return cls(
            leverage=int(cfg.get("leverage.default", base.leverage)),
            max_leverage=int(cfg.get("leverage.max_effective_leverage", base.max_leverage)),
            margin_mode=str(cfg.get("leverage.margin_mode", base.margin_mode)),
            liquidation_buffer=float(
                cfg.get("protection.liquidation_buffer", base.liquidation_buffer)
            ),
            taker_fee=float(cfg.get("backtest.fee_taker", base.taker_fee)),
            allow_margin_topup=bool(
                cfg.get("leverage.allow_margin_topup", base.allow_margin_topup)
            ),
            max_effective_leverage=int(
                cfg.get("leverage.max_effective_leverage", base.max_effective_leverage)
            ),
            verify_liq_price=bool(
                cfg.get("protection.verify_liq_price", base.verify_liq_price)
            ),
            tier_max_age_seconds=float(
                cfg.get("protection.risk_tier_max_age_seconds", base.tier_max_age_seconds)
            ),
            liq_price_tolerance=float(
                cfg.get("protection.liq_price_tolerance", base.liq_price_tolerance)
            ),
        )

    @property
    def effective_leverage(self) -> int:
        """Leverage the position actually runs at. Top-up disabled means it is the config value."""
        return (
            int(self.max_effective_leverage) if self.allow_margin_topup else int(self.leverage)
        )


@dataclass(frozen=True)
class LiquidationVerdict:
    """May this position exist? ``ok=False`` means do not open it, or close it if it exists."""

    ok: bool
    stage: str = ""
    reason: str = ""
    symbol: str = ""
    direction: int = 0
    tier: TierSpec | None = None
    maintenance_rate: float = float("nan")
    liq_distance: float = float("nan")     # fraction of entry price
    liq_price: float = float("nan")        # conservative: rounded toward entry
    stop_distance: float = float("nan")
    buffer_actual: float = float("nan")    # liq_distance - stop_distance
    buffer_required: float = float("nan")
    required_effective_leverage: float = float("nan")
    required_margin: float = float("nan")
    posted_margin: float = float("nan")
    extra_margin: float = float("nan")     # what a top-up would have to add
    action: str = ""                       # "" | "flatten"
    metrics: Mapping[str, float] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.ok

    def summary(self) -> str:
        if self.ok:
            return (
                f"{self.symbol}: stop clears liquidation by "
                f"{self.buffer_actual * 100:.3f}% (need {self.buffer_required * 100:.2f}%)"
            )
        return f"{self.symbol}: REFUSED at {self.stage} — {self.reason}"


def _no(stage: str, reason: str, **extra: Any) -> LiquidationVerdict:
    return LiquidationVerdict(ok=False, stage=stage, reason=reason, **extra)


def liquidation_price(entry_price: float, direction: int, liq_distance: float,
                      tick: float = 0.0) -> float:
    """Predicted liquidation price, rounded onto the mark-price grid **toward entry**.

    Toward entry is the only safe direction. Rounding away would report liquidation as
    further off than it is and quietly widen the apparent buffer by up to a tick; on a
    contract whose whole buffer is 0.30% of price, a tick is not negligible. Rounding
    toward entry can only understate the room available.
    """
    raw = (
        entry_price * (1.0 - liq_distance) if direction > 0
        else entry_price * (1.0 + liq_distance)
    )
    if not _finite(tick) or tick <= 0:
        return raw
    # Long: liquidation sits below entry, so toward entry is up.
    if direction > 0:
        return math.ceil(raw / tick - 1e-9) * tick
    return math.floor(raw / tick + 1e-9) * tick


def required_effective_leverage(stop_distance: float, maintenance_rate: float,
                                taker_fee: float, buffer: float) -> float:
    """Highest leverage at which ``stop_distance`` still clears liquidation by ``buffer``.

    Solves ``1/L - mmr - taker >= stop + buffer`` for L. Returns ``inf`` when the stop is
    so tight that any leverage works, and ``nan`` when the requirement is unsatisfiable at
    any leverage (the fees and maintenance rate alone already exceed the room asked for).
    """
    denominator = float(stop_distance) + float(buffer) + float(maintenance_rate) + float(taker_fee)
    if denominator <= 0:
        return float("nan")
    return 1.0 / denominator


def _validate_snapshot(snapshot: Any, now: float, params: LiquidationParams) -> str:
    """Return the reason a snapshot is unusable, or ``""`` if it is sound."""
    if snapshot is None:
        return (
            "no risk-tier snapshot supplied; the maintenance rate is tiered by notional and "
            "the contract's flat field is only tier 1"
        )
    tiers = tuple(getattr(snapshot, "tiers", ()) or ())
    if not tiers:
        return "risk-tier ladder is empty; there is no maintenance rate to reason from"

    fetched_at = getattr(snapshot, "fetched_at", None)
    if not _finite(fetched_at) or not _finite(now):
        return f"risk-tier snapshot has an unusable timestamp ({fetched_at!r} against {now!r})"

    age = float(now) - float(fetched_at)
    if age < -CLOCK_SKEW_TOLERANCE_SECONDS:
        return (
            f"risk-tier snapshot is timestamped {-age:.0f}s in the future; the clocks "
            "disagree, so its age cannot be trusted"
        )
    if age > params.tier_max_age_seconds:
        return (
            f"risk-tier snapshot is {age:.0f}s old, past the "
            f"{params.tier_max_age_seconds:.0f}s limit; re-read /risk_limit_tiers"
        )

    ordered = sorted(tiers, key=lambda t: getattr(t, "tier", 0))
    previous = None
    for tier in ordered:
        rate = getattr(tier, "maintenance_rate", None)
        limit = getattr(tier, "risk_limit", None)
        lev_max = getattr(tier, "leverage_max", None)
        if not _finite(rate) or not 0.0 < float(rate) < 1.0:
            return f"tier {getattr(tier, 'tier', '?')} has an unusable maintenance_rate {rate!r}"
        if not _finite(limit) or float(limit) <= 0:
            return f"tier {getattr(tier, 'tier', '?')} has an unusable risk_limit {limit!r}"
        if not _finite(lev_max) or float(lev_max) <= 0:
            return f"tier {getattr(tier, 'tier', '?')} has an unusable leverage_max {lev_max!r}"
        if previous is not None:
            # Gate's ladders climb in notional and maintenance rate while leverage falls.
            # A ladder that does not is either corrupt or something this module has never
            # seen, and either way it must not be reasoned from.
            if float(limit) <= float(previous.risk_limit):
                return (
                    f"risk_limit does not increase at tier {getattr(tier, 'tier', '?')}: "
                    f"{previous.risk_limit} -> {limit}"
                )
            if float(rate) < float(previous.maintenance_rate):
                return (
                    f"maintenance_rate falls at tier {getattr(tier, 'tier', '?')}: "
                    f"{previous.maintenance_rate} -> {rate}"
                )
            if float(lev_max) > float(previous.leverage_max):
                return (
                    f"leverage_max rises at tier {getattr(tier, 'tier', '?')}: "
                    f"{previous.leverage_max} -> {lev_max}"
                )
        previous = tier
    return ""


def assess(
    symbol: str,
    direction: int,
    entry_price: float,
    stop_price: float,
    notional: float,
    snapshot: TierSnapshot | None,
    now: float,
    params: LiquidationParams | None = None,
    contract: ContractSpec | None = None,
    posted_margin: float | None = None,
) -> LiquidationVerdict:
    """Decide whether this position may exist. Every gate is a veto.

    Order: snapshot validity -> inputs -> margin mode -> tier -> leverage ceilings ->
    liquidation distance -> the buffer, checked in both fractional and price terms.

    ``notional`` is the position's size in settle currency, which is what selects the tier.
    ``posted_margin`` defaults to ``notional / effective_leverage``; pass the exchange's
    figure when it is known.
    """
    params = params or LiquidationParams()

    stale = _validate_snapshot(snapshot, now, params)
    if stale:
        return _no("tier_data", stale, symbol=symbol, direction=direction,
                   buffer_required=params.liquidation_buffer)

    assert snapshot is not None  # _validate_snapshot rejects None

    if direction not in (1, -1):
        return _no("direction", f"direction must be +1 or -1, got {direction!r}", symbol=symbol)
    for name, value in (("entry_price", entry_price), ("stop_price", stop_price)):
        if not _finite(value) or float(value) <= 0:
            return _no("inputs", f"{name} {value!r} is not a usable price",
                       symbol=symbol, direction=direction)
    if not _finite(notional) or float(notional) <= 0:
        return _no("inputs", f"notional {notional!r} is not usable",
                   symbol=symbol, direction=direction)

    if params.margin_mode != "isolated":
        return _no("margin_mode",
                   f"margin_mode is {params.margin_mode!r}; cross margin puts the whole "
                   "account behind one position and is forbidden",
                   symbol=symbol, direction=direction)

    # The stop must be on the correct side of entry, or "distance to liquidation" is
    # meaningless — a long whose stop sits above entry is not protected at all.
    if direction > 0 and stop_price >= entry_price:
        return _no("stop_side", f"long stop {stop_price:g} is not below entry {entry_price:g}",
                   symbol=symbol, direction=direction)
    if direction < 0 and stop_price <= entry_price:
        return _no("stop_side", f"short stop {stop_price:g} is not above entry {entry_price:g}",
                   symbol=symbol, direction=direction)

    leverage = params.effective_leverage
    if leverage > params.max_leverage:
        return _no("leverage",
                   f"{leverage}x exceeds the {params.max_leverage}x ceiling",
                   symbol=symbol, direction=direction)

    tier = select_tier(snapshot.tiers, float(notional))
    mmr = float(tier.maintenance_rate)

    if leverage > float(tier.leverage_max):
        return _no("leverage",
                   f"tier {tier.tier} caps leverage at {tier.leverage_max:g}x for a "
                   f"{notional:,.0f} notional, below the {leverage}x in use",
                   symbol=symbol, direction=direction, tier=tier, maintenance_rate=mmr)

    top_limit = max(float(t.risk_limit) for t in snapshot.tiers)
    if float(notional) > top_limit:
        return _no("risk_limit",
                   f"notional {notional:,.0f} exceeds the top tier's {top_limit:,.0f} limit",
                   symbol=symbol, direction=direction, tier=tier, maintenance_rate=mmr)

    liq_distance = liquidation_distance(leverage, mmr, params.taker_fee)
    stop_distance = abs(float(entry_price) - float(stop_price)) / float(entry_price)
    buffer_actual = liq_distance - stop_distance

    tick = 0.0
    if contract is not None:
        tick = float(
            getattr(contract, "mark_price_round", None)
            or getattr(contract, "order_price_round", 0.0)
            or 0.0
        )
    liq = liquidation_price(float(entry_price), direction, liq_distance, tick)

    solved = required_effective_leverage(
        stop_distance, mmr, params.taker_fee, params.liquidation_buffer
    )
    margin = (
        float(notional) / leverage if posted_margin is None else float(posted_margin)
    )
    required_margin = float(notional) / solved if _finite(solved) and solved > 0 else float("nan")
    extra = required_margin - margin if _finite(required_margin) else float("nan")

    common = dict(
        symbol=symbol, direction=direction, tier=tier, maintenance_rate=mmr,
        liq_distance=liq_distance, liq_price=liq, stop_distance=stop_distance,
        buffer_actual=buffer_actual, buffer_required=params.liquidation_buffer,
        required_effective_leverage=solved, required_margin=required_margin,
        posted_margin=margin, extra_margin=extra,
        metrics={
            "leverage": float(leverage),
            "notional": float(notional),
            "entry_price": float(entry_price),
            "stop_price": float(stop_price),
            "tier_age_seconds": snapshot.age(now),
            "max_stop_distance": liq_distance - params.liquidation_buffer,
        },
    )

    if liq_distance <= 0:
        return _no(
            "liquidation_distance",
            f"at {leverage}x with a {mmr * 100:.2f}% maintenance rate the liquidation "
            f"distance is {liq_distance * 100:.3f}% — the position is liquidated on entry",
            **common,
        )

    if buffer_actual < params.liquidation_buffer - _FLOAT_SLOP:
        # The top-up solver still runs and reports what would be needed, so the refusal
        # says how far short it was rather than merely that it was short. With
        # allow_margin_topup false (the shipped profile) that figure only informs the
        # skip-vs-trade decision; nothing here posts margin.
        hint = (
            f"; {extra:,.2f} of extra isolated margin would fix it "
            f"({solved:.1f}x effective)"
            if _finite(extra) and extra > 0 else ""
        )
        return _no(
            "buffer",
            f"stop is {stop_distance * 100:.3f}% from entry but liquidation is only "
            f"{liq_distance * 100:.3f}% away, leaving {buffer_actual * 100:.3f}% of buffer "
            f"against the {params.liquidation_buffer * 100:.2f}% required{hint}",
            **common,
        )

    # The same check in price terms, against the pessimistically rounded liquidation
    # price. The fractional test can pass while this one fails by a tick.
    gap = (stop_price - liq) if direction > 0 else (liq - stop_price)
    if gap / float(entry_price) < params.liquidation_buffer - _FLOAT_SLOP:
        return _no(
            "buffer",
            f"after rounding onto the {tick:g} mark-price grid the stop {stop_price:g} "
            f"clears liquidation {liq:g} by only {gap / entry_price * 100:.3f}%, inside "
            f"the {params.liquidation_buffer * 100:.2f}% buffer",
            **common,
        )

    return LiquidationVerdict(
        ok=True,
        stage="ok",
        reason=(
            f"stop {stop_distance * 100:.3f}% clears liquidation {liq_distance * 100:.3f}% "
            f"by {buffer_actual * 100:.3f}% at tier {tier.tier} (mmr {mmr * 100:.2f}%)"
        ),
        **common,
    )


def assess_plan(
    plan: PositionPlan,
    snapshot: TierSnapshot | None,
    now: float,
    params: LiquidationParams | None = None,
    contract: ContractSpec | None = None,
    available_margin: float | None = None,
) -> LiquidationVerdict:
    """Run :func:`assess` against a Phase 6 :class:`~risk.position_sizer.PositionPlan`.

    The integration seam. Phase 6 sizes from risk and caps the stop at a ceiling derived
    from the same tier maths; this re-derives the buffer independently from the plan's
    *final* numbers, so a sizing bug shows up as a refusal here rather than as a position.
    A plan that was itself refused is refused again — there is nothing to protect.
    """
    params = params or LiquidationParams()
    if not plan.ok or plan.stop is None or not plan.stop.ok:
        return _no("plan", f"position plan was refused upstream: {plan.reason}",
                   symbol=plan.symbol, direction=plan.direction)

    verdict = assess(
        symbol=plan.symbol,
        direction=plan.direction,
        entry_price=plan.entry_price,
        stop_price=plan.stop.price,
        notional=plan.notional,
        snapshot=snapshot,
        now=now,
        params=params,
        contract=contract,
        posted_margin=plan.margin,
    )
    if verdict.ok and available_margin is not None and plan.margin > float(available_margin):
        return _no("margin", f"position needs {plan.margin:.2f} of isolated margin but only "
                             f"{float(available_margin):.2f} is available",
                   symbol=plan.symbol, direction=plan.direction, tier=verdict.tier,
                   maintenance_rate=verdict.maintenance_rate,
                   liq_distance=verdict.liq_distance, liq_price=verdict.liq_price,
                   stop_distance=verdict.stop_distance, buffer_actual=verdict.buffer_actual,
                   buffer_required=verdict.buffer_required, posted_margin=plan.margin)
    return verdict


def verify_fill(
    symbol: str,
    direction: int,
    entry_price: float,
    stop_price: float,
    reported_liq_price: float,
    now: float,
    params: LiquidationParams | None = None,
    reported_leverage: float | None = None,
    predicted_liq_price: float | None = None,
) -> LiquidationVerdict:
    """Re-verify a filled position against the exchange's **own** ``liq_price``.

    An API response is not proof. The fill may have slipped, the leverage may not have
    applied, the position may have landed in a stricter tier than planned — and every one
    of those moves liquidation without moving the stop. So the authoritative check is the
    exchange's figure, read back after the fill, not the prediction made before it.

    Returns ``action="flatten"`` when the position is unsafe and
    ``protection.verify_liq_price`` is on. This module does not close anything; acting on
    the recommendation is Phase 10's job.
    """
    params = params or LiquidationParams()
    flatten = "flatten" if params.verify_liq_price else ""

    def refuse(stage: str, reason: str, **extra: Any) -> LiquidationVerdict:
        return _no(stage, reason, symbol=symbol, direction=direction, action=flatten,
                   buffer_required=params.liquidation_buffer, **extra)

    if direction not in (1, -1):
        return refuse("direction", f"direction must be +1 or -1, got {direction!r}")
    for name, value in (("entry_price", entry_price), ("stop_price", stop_price)):
        if not _finite(value) or float(value) <= 0:
            return refuse("inputs", f"{name} {value!r} is not a usable price")

    if not _finite(reported_liq_price) or float(reported_liq_price) <= 0:
        # Gate reports 0 for a position it considers unliquidatable, but a filled isolated
        # position always has a liquidation price. Absent means unverified, and an
        # unverified leveraged position is the thing this module exists to prevent.
        return refuse("liq_price",
                      f"the exchange reported no usable liq_price ({reported_liq_price!r}); "
                      "an isolated position without one cannot be verified")

    # leverage 0 is how Gate.io signals cross margin.
    if reported_leverage is not None:
        if not _finite(reported_leverage) or float(reported_leverage) <= 0:
            return refuse("margin_mode",
                          f"the exchange reports leverage {reported_leverage!r}, which means "
                          "cross margin; this bot requires isolated")
        if float(reported_leverage) > params.max_leverage:
            return refuse("leverage",
                          f"the exchange reports {float(reported_leverage):g}x, above the "
                          f"{params.max_leverage}x ceiling")

    liq = float(reported_liq_price)
    stop = float(stop_price)
    entry = float(entry_price)

    wrong_side = (direction > 0 and liq >= entry) or (direction < 0 and liq <= entry)
    if wrong_side:
        return refuse("liq_price",
                      f"reported liq_price {liq:g} is on the wrong side of entry {entry:g} "
                      f"for a {'long' if direction > 0 else 'short'}")

    gap = (stop - liq) if direction > 0 else (liq - stop)
    buffer_actual = gap / entry
    stop_distance = abs(entry - stop) / entry

    common = dict(
        symbol=symbol, direction=direction, liq_price=liq, stop_distance=stop_distance,
        buffer_actual=buffer_actual, buffer_required=params.liquidation_buffer,
        metrics={"entry_price": entry, "stop_price": stop, "verified_at": float(now)},
    )

    if gap <= 0:
        return _no("liq_price",
                   f"liquidation {liq:g} is at or beyond the stop {stop:g} — the position "
                   "would liquidate before its stop fills",
                   action=flatten, **common)

    if buffer_actual < params.liquidation_buffer - _FLOAT_SLOP:
        return _no("buffer",
                   f"the exchange's liq_price {liq:g} leaves the stop only "
                   f"{buffer_actual * 100:.3f}% of room against the "
                   f"{params.liquidation_buffer * 100:.2f}% required",
                   action=flatten, **common)

    if predicted_liq_price is not None and _finite(predicted_liq_price):
        drift = abs(liq - float(predicted_liq_price)) / entry
        if drift > params.liq_price_tolerance:
            return _no("liq_price_mismatch",
                       f"the exchange's liq_price {liq:g} differs from the predicted "
                       f"{float(predicted_liq_price):g} by {drift * 100:.3f}% of entry, past "
                       f"the {params.liq_price_tolerance * 100:.2f}% tolerance; the position "
                       "is not the one that was sized",
                       action=flatten, **common)

    return LiquidationVerdict(
        ok=True,
        stage="ok",
        reason=(
            f"exchange liq_price {liq:g} leaves the stop {buffer_actual * 100:.3f}% of room "
            f"against the {params.liquidation_buffer * 100:.2f}% required"
        ),
        **common,
    )
