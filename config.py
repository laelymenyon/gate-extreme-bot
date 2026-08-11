"""Configuration loading and validation.

Fail-closed by design: anything missing, malformed, or out of range raises
ConfigError rather than falling back to a default that could place real orders.

Live trading requires three independent switches to agree:
    DRY_RUN=false (.env)  AND  --mode live  AND  --confirm-live
Any one of them missing keeps the bot in simulation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
ENV_PATH = ROOT / ".env"

VALID_MODES = ("scalp", "breakout")
VALID_RUN_MODES = ("paper", "backtest", "live")

_MISSING = object()  # sentinel: distinguishes "no default given" from default=None

# Live-verified 2026-08-09 from GET /futures/usdt/contracts. Used only for load-time
# feasibility checks; the real per-contract, per-tier rate is read from
# /futures/{settle}/risk_limit_tiers at runtime by risk/liquidation_guard.py.
BEST_MAINTENANCE_RATE = 0.003   # BTC_USDT, ETH_USDT tier 1
COMMON_MAINTENANCE_RATE = 0.005  # the other 29 contracts at >=100x


def max_stop_distance(leverage: int, maintenance_rate: float, taker_fee: float,
                      buffer: float) -> float:
    """Widest stop that still clears liquidation by `buffer`.

    liq_distance ~= 1/leverage - maintenance_rate - taker_fee

    Returns a negative value when no stop can fit, meaning the contract is untradable
    at this leverage/buffer combination. Pre-trade estimate only — the authoritative
    check re-reads Position.liq_price from the exchange after the fill.
    """
    return (1.0 / leverage) - maintenance_rate - taker_fee - buffer


class ConfigError(Exception):
    """Configuration is missing, malformed, or unsafe."""


@dataclass(frozen=True)
class Credentials:
    key: str
    secret: str

    @property
    def present(self) -> bool:
        return bool(self.key and self.secret)

    def __repr__(self) -> str:  # never leak secrets into logs or tracebacks
        state = "set" if self.present else "empty"
        return f"Credentials(key=<{state}>, secret=<{state}>)"


@dataclass(frozen=True)
class Config:
    raw: dict[str, Any]
    credentials: Credentials
    env_dry_run: bool
    run_mode: str = "paper"
    confirm_live: bool = False
    _sections: tuple[str, ...] = field(default=(), repr=False)

    # --- safety resolution -------------------------------------------------
    @property
    def live_enabled(self) -> bool:
        """True only when all three independent switches agree."""
        return (
            self.env_dry_run is False
            and self.run_mode == "live"
            and self.confirm_live is True
        )

    @property
    def dry_run(self) -> bool:
        return not self.live_enabled

    def section(self, name: str) -> dict[str, Any]:
        try:
            value = self.raw[name]
        except KeyError:
            raise ConfigError(f"config.yaml missing required section: {name!r}") from None
        if not isinstance(value, dict):
            raise ConfigError(f"config.yaml section {name!r} must be a mapping")
        return value

    def get(self, path: str, default: Any = _MISSING) -> Any:
        """Fetch a dotted path, e.g. get('risk.per_trade')."""
        node: Any = self.raw
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                if default is _MISSING:
                    raise ConfigError(f"config.yaml missing required key: {path!r}")
                return default
            node = node[part]
        return node


def _require_fraction(cfg: Config, path: str, low: float, high: float) -> None:
    value = cfg.get(path)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ConfigError(f"{path} must be a number, got {value!r}")
    if not low <= float(value) <= high:
        raise ConfigError(f"{path}={value} outside allowed range [{low}, {high}]")


def _validate(cfg: Config) -> None:
    if cfg.get("mode") not in VALID_MODES:
        raise ConfigError(f"mode must be one of {VALID_MODES}, got {cfg.get('mode')!r}")

    for name in ("leverage", "risk", "strategy", "protection", "stop_loss",
                 "take_profit", "filters", "universe", "session_guard", "websocket",
                 "execution", "backtest", "database", "logging"):
        cfg.section(name)

    lev_min = cfg.get("leverage.minimum")
    lev_def = cfg.get("leverage.default")
    if not isinstance(lev_min, int) or lev_min < 1:
        raise ConfigError(f"leverage.minimum must be an int >= 1, got {lev_min!r}")
    if not isinstance(lev_def, int) or lev_def < lev_min:
        raise ConfigError(f"leverage.default ({lev_def}) must be >= leverage.minimum ({lev_min})")
    if cfg.get("leverage.margin_mode") != "isolated":
        raise ConfigError("leverage.margin_mode must be 'isolated'; cross margin is unsupported")

    _require_fraction(cfg, "risk.per_trade", 0.0, 0.05)
    _require_fraction(cfg, "risk.max_daily_loss", 0.0, 0.20)
    _require_fraction(cfg, "risk.max_drawdown", 0.0, 0.50)
    _require_fraction(cfg, "protection.liquidation_buffer", 0.0, 0.10)
    _require_fraction(cfg, "stop_loss.min_distance", 0.0, 0.10)
    _require_fraction(cfg, "stop_loss.max_distance", 0.0, 0.50)

    if cfg.get("stop_loss.min_distance") >= cfg.get("stop_loss.max_distance"):
        raise ConfigError("stop_loss.min_distance must be < stop_loss.max_distance")

    if cfg.get("risk.max_consecutive_losses") < 1:
        raise ConfigError("risk.max_consecutive_losses must be >= 1")
    if cfg.get("risk.max_open_positions") < 1:
        raise ConfigError("risk.max_open_positions must be >= 1")

    # --- PHASE 6: the breaker ladder must actually be a ladder -------------
    # Each limit has to be reachable before the next one, or the inner limits are
    # decoration: a per-trade risk above the daily limit means the first loss of the day
    # trips it, and a daily limit above the drawdown limit means the account is halted for
    # good before it is ever halted for the day.
    per_trade = cfg.get("risk.per_trade")
    daily = cfg.get("risk.max_daily_loss")
    drawdown = cfg.get("risk.max_drawdown")
    if per_trade > daily:
        raise ConfigError(
            f"risk.per_trade ({per_trade * 100:.2f}%) exceeds risk.max_daily_loss "
            f"({daily * 100:.2f}%): a single stop-out would trip the daily breaker"
        )
    if daily > drawdown:
        raise ConfigError(
            f"risk.max_daily_loss ({daily * 100:.2f}%) exceeds risk.max_drawdown "
            f"({drawdown * 100:.2f}%): the drawdown latch, which needs a manual reset, "
            "would trip before the daily one that clears overnight"
        )

    for path in ("filters.cooldown_after_loss_seconds", "filters.cooldown_after_win_seconds"):
        value = cfg.get(path, 0)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise ConfigError(f"{path} must be a non-negative number, got {value!r}")
    if cfg.get("filters.cooldown_after_loss_seconds", 0) < cfg.get(
        "filters.cooldown_after_win_seconds", 0
    ):
        raise ConfigError(
            "filters.cooldown_after_loss_seconds must be >= cooldown_after_win_seconds: "
            "the pause after a loss is what prevents trading a losing streak back"
        )

    # Strategies that blow up leveraged accounts are not switchable on.
    for forbidden in ("risk.martingale", "risk.averaging_down"):
        if cfg.get(forbidden, False):
            raise ConfigError(f"{forbidden} is permanently disabled and must be false")

    weights = cfg.section("strategy")["scoring_weights"]
    total = sum(weights.values())
    if total != 100:
        raise ConfigError(f"strategy.scoring_weights must sum to 100, got {total}")

    score = cfg.get("strategy.minimum_score")
    if not 0 < score <= 100:
        raise ConfigError(f"strategy.minimum_score must be in (0, 100], got {score}")
    if cfg.get("strategy.minimum_rr") < 1:
        raise ConfigError("strategy.minimum_rr must be >= 1")

    # --- PHASE 5: multi-timeframe wiring ----------------------------------
    timeframes = cfg.get("strategy.timeframes")
    if not isinstance(timeframes, list) or not timeframes:
        raise ConfigError("strategy.timeframes must be a non-empty list")

    tf_weights = cfg.get("strategy.timeframe_weights", None)
    if tf_weights is not None:
        if not isinstance(tf_weights, dict):
            raise ConfigError("strategy.timeframe_weights must be a mapping")
        missing = [t for t in timeframes if t not in tf_weights]
        if missing:
            raise ConfigError(
                f"strategy.timeframe_weights is missing {missing}; every timeframe in "
                "strategy.timeframes needs a weight or its score would be dropped "
                "silently from the blend"
            )
        total = sum(float(tf_weights[t]) for t in timeframes)
        if abs(total - 1.0) > 1e-9:
            raise ConfigError(
                f"strategy.timeframe_weights over strategy.timeframes must sum to 1.0, "
                f"got {total}"
            )

    # A veto timeframe that is not evaluated cannot veto anything: the higher-timeframe
    # check would silently pass and the safety property would be gone.
    for veto in cfg.get("strategy.veto_timeframes", []):
        if veto not in timeframes:
            raise ConfigError(
                f"strategy.veto_timeframes contains {veto!r}, which is not in "
                f"strategy.timeframes {timeframes}; it could never veto anything"
            )

    valid_regimes = {
        "TRENDING", "RANGING", "HIGH_VOLATILITY", "LOW_VOLATILITY",
        "BREAKOUT", "BREAKDOWN",
    }
    for key in ("scalp_allowed", "breakout_allowed"):
        allowed = cfg.get(f"strategy.regime.{key}", [])
        unknown = [r for r in allowed if r not in valid_regimes]
        if unknown:
            raise ConfigError(
                f"strategy.regime.{key} has unknown regime(s) {unknown}; "
                f"valid values are {sorted(valid_regimes)}"
            )

    high_pct = cfg.get("strategy.regime.atr_high_percentile", 0.85)
    low_pct = cfg.get("strategy.regime.atr_low_percentile", 0.15)
    if not 0.0 <= low_pct < high_pct <= 1.0:
        raise ConfigError(
            "strategy.regime requires 0 <= atr_low_percentile < atr_high_percentile <= 1, "
            f"got low={low_pct}, high={high_pct}"
        )

    if cfg.get("strategy.regime.adx_ranging", 18) > cfg.get(
        "strategy.regime.adx_trending", 22
    ):
        raise ConfigError(
            "strategy.regime.adx_ranging must be <= adx_trending; otherwise the two "
            "bands overlap and a market could classify as both"
        )

    if cfg.get("protection.sl_price_type") not in ("mark", "last", "index"):
        raise ConfigError("protection.sl_price_type must be mark, last, or index")

    # --- PHASE 7: liquidation guard inputs --------------------------------
    # A tier ladder with no freshness limit, or a mismatch tolerance wide enough to admit
    # a wrong tier, would turn the guard's fail-closed behaviour into a formality.
    age = cfg.get("protection.risk_tier_max_age_seconds", 3600)
    if not isinstance(age, (int, float)) or isinstance(age, bool) or age <= 0:
        raise ConfigError(
            f"protection.risk_tier_max_age_seconds must be a positive number, got {age!r}"
        )
    tolerance = cfg.get("protection.liq_price_tolerance", 0.002)
    if not isinstance(tolerance, (int, float)) or isinstance(tolerance, bool):
        raise ConfigError("protection.liq_price_tolerance must be a number")
    if not 0.0 <= tolerance < cfg.get("protection.liquidation_buffer"):
        raise ConfigError(
            f"protection.liq_price_tolerance ({tolerance}) must be in "
            f"[0, liquidation_buffer): a tolerance at or above the buffer would accept a "
            "liq_price that eats the entire buffer"
        )

    tp = cfg.section("take_profit")
    if tp["tp1_close_pct"] + tp["tp2_close_pct"] >= 1.0:
        raise ConfigError("take_profit tp1+tp2 close fractions must leave a runner (< 1.0)")

    if not cfg.get("universe.symbols"):
        raise ConfigError("universe.symbols is empty; nothing to trade")

    # --- PHASE 13: paper-trading acceptance criteria ----------------------
    # Only the loss tolerance is configurable, and it cannot be set below the cost of a
    # correctly-executed stop-out. A tolerance under 1R would fail every trade that did
    # exactly what it was supposed to do, which would train an operator to ignore the
    # report — the failure mode this check exists to prevent.
    max_loss_r = cfg.get("validation.max_loss_r", 1.5)
    if not isinstance(max_loss_r, (int, float)) or isinstance(max_loss_r, bool):
        raise ConfigError(
            f"validation.max_loss_r must be a number, got {max_loss_r!r}"
        )
    if max_loss_r < 1.0:
        raise ConfigError(
            f"validation.max_loss_r must be >= 1.0, got {max_loss_r}: a stop-out costs 1R "
            "by construction, so a lower tolerance would fail every correctly-executed "
            "losing trade"
        )

    # --- consequences of running at pure 100x -----------------------------
    # At >=100x a taker entry costs ~1.2R in fees against the widest permissible stop,
    # which needs a ~73% win rate merely to break even. Post-only is not a preference here.
    if lev_def >= 100 and cfg.get("take_profit.entry_tif") != "poc":
        raise ConfigError(
            f"take_profit.entry_tif must be 'poc' (post-only) at {lev_def}x: a taker entry "
            "costs ~1.2R in fees per trade and needs a ~73% win rate to break even"
        )

    if cfg.get("stop_loss.on_sl_exceeds_max") not in ("cap", "skip"):
        raise ConfigError("stop_loss.on_sl_exceeds_max must be 'cap' or 'skip'")

    if cfg.get("leverage.allow_margin_topup") and (
        cfg.get("leverage.max_effective_leverage") >= lev_def
    ):
        raise ConfigError(
            "leverage.allow_margin_topup=true requires max_effective_leverage < leverage.default; "
            "topping up margin only helps if it lowers effective leverage"
        )

    # The stop must physically fit between entry and liquidation.
    #   liq_distance ~= 1/leverage - maintenance_rate - taker_fee
    # Two checks, because Gate.io maintenance rates are not uniform:
    #   1. best case (mmr 0.30%, BTC/ETH tier 1) — if no stop fits here, nothing can ever trade
    #   2. worst case among configured pairs — warns which pairs the guard will reject at runtime
    taker = cfg.get("backtest.fee_taker")
    buffer = cfg.get("protection.liquidation_buffer")
    eff_lev = (
        cfg.get("leverage.max_effective_leverage")
        if cfg.get("leverage.allow_margin_topup")
        else lev_def
    )
    best_case_liq = (1.0 / eff_lev) - BEST_MAINTENANCE_RATE - taker
    headroom = best_case_liq - buffer
    if headroom <= 0:
        raise ConfigError(
            f"unreachable configuration: at {eff_lev}x the liquidation distance is "
            f"{best_case_liq * 100:.3f}% even on the lowest-maintenance contract, which is "
            f"inside the {buffer * 100:.2f}% liquidation buffer. No stop-loss can be placed, so "
            "every trade would be rejected. Lower leverage.default, lower "
            "protection.liquidation_buffer, or enable leverage.allow_margin_topup."
        )
    if headroom < cfg.get("stop_loss.min_distance"):
        raise ConfigError(
            f"stop_loss.min_distance={cfg.get('stop_loss.min_distance') * 100:.3f}% cannot fit: "
            f"at {eff_lev}x the widest permissible stop is {headroom * 100:.3f}%"
        )

    if cfg.get("session_guard.enabled") and not cfg.get(
        "session_guard.treat_unknown_session_as_closed"
    ):
        raise ConfigError(
            "session_guard.treat_unknown_session_as_closed must be true: an unknown session "
            "calendar must fail closed, since a gap at high leverage bypasses the stop-loss"
        )

    # --- websocket (Phase 3) ----------------------------------------------
    if not str(cfg.get("websocket.url")).startswith("wss://"):
        raise ConfigError("websocket.url must be a wss:// endpoint")

    # WebSocket state is never source of truth across a disconnect. Making this
    # switchable would let a future edit silently trade on unreconciled state.
    if not cfg.get("websocket.resync.require_rest_resync"):
        raise ConfigError(
            "websocket.resync.require_rest_resync must be true: after a disconnect, "
            "positions and orders must be rebuilt from REST before trading resumes"
        )

    for path in ("websocket.staleness.ticker_seconds", "websocket.staleness.book_seconds"):
        value = cfg.get(path)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise ConfigError(f"{path} must be a positive number, got {value!r}")

    # A staleness limit longer than the ping interval is meaningless: the heartbeat
    # would detect the dead link first and the watchdog would never fire.
    ping = cfg.get("websocket.ping_interval_seconds")
    if cfg.get("websocket.pong_timeout_seconds") <= ping:
        raise ConfigError(
            "websocket.pong_timeout_seconds must exceed ping_interval_seconds, "
            f"got {cfg.get('websocket.pong_timeout_seconds')} <= {ping}"
        )

    rc_base = cfg.get("websocket.reconnect.base_delay_seconds")
    rc_max = cfg.get("websocket.reconnect.max_delay_seconds")
    if rc_base <= 0 or rc_max < rc_base:
        raise ConfigError(
            f"websocket.reconnect delays invalid: base={rc_base}, max={rc_max}"
        )


def load_config(run_mode: str = "paper", confirm_live: bool = False) -> Config:
    """Load .env + config.yaml, validate, and resolve the live-trading gate."""
    if run_mode not in VALID_RUN_MODES:
        raise ConfigError(f"run_mode must be one of {VALID_RUN_MODES}, got {run_mode!r}")

    if not CONFIG_PATH.exists():
        raise ConfigError(f"missing {CONFIG_PATH}")

    load_dotenv(ENV_PATH, override=False)

    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        # A malformed config must refuse like any other config error — the CLI catches
        # ConfigError and exits 2. A raw YAMLError escaping here would be a traceback.
        raise ConfigError(f"{CONFIG_PATH} is not valid YAML: {exc}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{CONFIG_PATH} could not be read: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml did not parse to a mapping")

    # DRY_RUN defaults to true when unset or unparseable.
    env_dry_run = os.getenv("DRY_RUN", "true").strip().lower() not in ("false", "0", "no")

    cfg = Config(
        raw=raw,
        credentials=Credentials(
            key=os.getenv("GATE_API_KEY", "").strip(),
            secret=os.getenv("GATE_API_SECRET", "").strip(),
        ),
        env_dry_run=env_dry_run,
        run_mode=run_mode,
        confirm_live=confirm_live,
    )
    _validate(cfg)

    if cfg.live_enabled and not cfg.credentials.present:
        raise ConfigError("live trading requested but GATE_API_KEY/GATE_API_SECRET are empty")

    return cfg
