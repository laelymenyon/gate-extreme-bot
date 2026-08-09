"""Phase 1 tests: config validation and the fail-closed live-trading gate.

The gate is the single most safety-critical piece of Phase 1, so it is tested
exhaustively across all eight switch combinations.
"""

from __future__ import annotations

import itertools

import pytest

import config as config_module
from config import ConfigError, load_config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Isolate tests from the developer's real .env."""
    monkeypatch.setattr(config_module, "ENV_PATH", config_module.ROOT / ".env.absent")
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.delenv("GATE_API_KEY", raising=False)
    monkeypatch.delenv("GATE_API_SECRET", raising=False)


# --- the safety gate ------------------------------------------------------

def test_default_is_dry_run():
    cfg = load_config()
    assert cfg.dry_run is True
    assert cfg.live_enabled is False


def test_unset_dry_run_defaults_to_true():
    cfg = load_config(run_mode="live", confirm_live=True)
    assert cfg.env_dry_run is True
    assert cfg.live_enabled is False


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "", "banana", "  true  "])
def test_dry_run_stays_true_for_anything_but_explicit_false(monkeypatch, value):
    monkeypatch.setenv("DRY_RUN", value)
    cfg = load_config(run_mode="live", confirm_live=True)
    assert cfg.live_enabled is False, f"DRY_RUN={value!r} must not enable live trading"


@pytest.mark.parametrize("dry_run,mode,confirm", list(itertools.product(
    ["true", "false"], ["paper", "backtest", "live"], [False, True],
)))
def test_live_requires_all_three_switches(monkeypatch, dry_run, mode, confirm):
    monkeypatch.setenv("DRY_RUN", dry_run)
    monkeypatch.setenv("GATE_API_KEY", "k")
    monkeypatch.setenv("GATE_API_SECRET", "s")
    cfg = load_config(run_mode=mode, confirm_live=confirm)
    expected = (dry_run == "false" and mode == "live" and confirm is True)
    assert cfg.live_enabled is expected
    assert cfg.dry_run is not expected


def test_live_without_credentials_is_refused(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    with pytest.raises(ConfigError, match="GATE_API_KEY"):
        load_config(run_mode="live", confirm_live=True)


def test_invalid_run_mode_rejected():
    with pytest.raises(ConfigError, match="run_mode"):
        load_config(run_mode="yolo")


def test_credentials_never_leak_in_repr(monkeypatch):
    monkeypatch.setenv("GATE_API_KEY", "SUPERSECRETKEY")
    monkeypatch.setenv("GATE_API_SECRET", "SUPERSECRETVALUE")
    cfg = load_config()
    rendered = repr(cfg.credentials) + repr(cfg)
    assert "SUPERSECRETKEY" not in rendered
    assert "SUPERSECRETVALUE" not in rendered


# --- shipped config sanity -------------------------------------------------

def test_shipped_config_validates():
    cfg = load_config()
    assert cfg.get("leverage.minimum") >= 100
    assert cfg.get("leverage.default") >= cfg.get("leverage.minimum")
    assert cfg.get("strategy.minimum_score") >= 80
    assert cfg.get("strategy.minimum_rr") >= 2
    assert cfg.get("leverage.margin_mode") == "isolated"


def test_scoring_weights_sum_to_100():
    weights = load_config().section("strategy")["scoring_weights"]
    assert sum(weights.values()) == 100
    assert weights == {
        "trend": 25, "momentum": 20, "volume": 15,
        "price_action": 20, "volatility": 10, "support_resistance": 10,
    }


def test_martingale_and_averaging_down_are_off():
    cfg = load_config()
    assert cfg.get("risk.martingale") is False
    assert cfg.get("risk.averaging_down") is False


def test_sl_uses_mark_price_to_match_liquidation():
    assert load_config().get("protection.sl_price_type") == "mark"


def test_tp_ladder_leaves_a_runner():
    tp = load_config().section("take_profit")
    assert tp["tp1_close_pct"] + tp["tp2_close_pct"] < 1.0


def test_fees_match_live_verified_values():
    cfg = load_config()
    assert cfg.get("backtest.fee_taker") == 0.00075
    assert cfg.get("backtest.fee_maker") == -0.0001


def test_missing_key_raises_rather_than_defaulting():
    cfg = load_config()
    with pytest.raises(ConfigError, match="nope.nothing"):
        cfg.get("nope.nothing")
    assert cfg.get("nope.nothing", "fallback") == "fallback"


# --- consequences of the pure-100x decision --------------------------------

def _mutate(monkeypatch, tmp_path, **overrides):
    """Write a variant of the shipped config and load it."""
    import copy
    import yaml as _yaml

    with config_module.CONFIG_PATH.open(encoding="utf-8") as handle:
        raw = _yaml.safe_load(handle)
    for dotted, value in overrides.items():
        node = raw
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = value
    path = tmp_path / "config.yaml"
    path.write_text(_yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_PATH", path)
    return load_config()


def test_shipped_config_is_pure_100x():
    cfg = load_config()
    assert cfg.get("leverage.allow_margin_topup") is False
    assert cfg.get("leverage.max_effective_leverage") == cfg.get("leverage.default") == 100


def test_buffer_is_the_agreed_030_percent():
    assert load_config().get("protection.liquidation_buffer") == 0.003


def test_stop_fits_between_entry_and_liquidation():
    """At a 0.30% buffer both maintenance tiers must leave usable stop room."""
    cfg = load_config()
    lev = cfg.get("leverage.default")
    taker = cfg.get("backtest.fee_taker")
    buffer = cfg.get("protection.liquidation_buffer")

    best = config_module.max_stop_distance(
        lev, config_module.BEST_MAINTENANCE_RATE, taker, buffer)
    common = config_module.max_stop_distance(
        lev, config_module.COMMON_MAINTENANCE_RATE, taker, buffer)

    assert best == pytest.approx(0.00325, abs=1e-9)    # BTC_USDT, ETH_USDT
    assert common == pytest.approx(0.00125, abs=1e-9)  # the other 29, incl. SOL and XRP
    assert cfg.get("stop_loss.min_distance") <= common


def test_taker_entry_rejected_at_high_leverage(monkeypatch, tmp_path):
    with pytest.raises(ConfigError, match="post-only"):
        _mutate(monkeypatch, tmp_path, **{"take_profit.entry_tif": "ioc"})


@pytest.mark.parametrize("leverage", [150, 200])
def test_leverage_above_125_is_rejected_as_unreachable(monkeypatch, tmp_path, leverage):
    """At a 0.30% buffer, 150x leaves -0.008% and 200x leaves -0.175% of stop room."""
    with pytest.raises(ConfigError, match="unreachable configuration"):
        _mutate(monkeypatch, tmp_path, **{"leverage.default": leverage})


def test_125x_became_reachable_but_is_not_configured(monkeypatch, tmp_path):
    """Lowering the buffer to 0.30% made 125x mathematically viable (0.125% of stop room).

    We still run 100x by explicit decision. This test pins the fact so that a future
    bump to 125x is a deliberate change rather than an accident.
    """
    shipped_leverage = load_config().get("leverage.default")  # read before _mutate repoints CONFIG_PATH
    cfg = _mutate(monkeypatch, tmp_path, **{"leverage.default": 125})
    assert cfg.get("leverage.default") == 125
    room = config_module.max_stop_distance(
        125, config_module.BEST_MAINTENANCE_RATE,
        cfg.get("backtest.fee_taker"), cfg.get("protection.liquidation_buffer"))
    assert room == pytest.approx(0.00125, abs=1e-9)
    assert shipped_leverage == 100  # shipped config stays at 100x


def test_session_guard_must_fail_closed(monkeypatch, tmp_path):
    with pytest.raises(ConfigError, match="fail closed"):
        _mutate(
            monkeypatch, tmp_path,
            **{"session_guard.treat_unknown_session_as_closed": False},
        )


def test_universe_covers_all_31_high_leverage_pairs():
    symbols = load_config().get("universe.symbols")
    assert len(symbols) == 31
    assert len(set(symbols)) == 31
    for required in ("BTC_USDT", "ETH_USDT", "SOL_USDT", "XRP_USDT"):
        assert required in symbols


def test_all_31_pairs_now_pass_the_buffer():
    """The point of the 0.30% buffer: every >=100x pair becomes tradable, incl. SOL and XRP."""
    cfg = load_config()
    lev = cfg.get("leverage.default")
    taker = cfg.get("backtest.fee_taker")
    buffer = cfg.get("protection.liquidation_buffer")

    for mmr in (config_module.BEST_MAINTENANCE_RATE, config_module.COMMON_MAINTENANCE_RATE):
        assert config_module.max_stop_distance(lev, mmr, taker, buffer) > 0


def test_restoring_the_050_buffer_would_exclude_the_mmr_050_pairs():
    """Regression guard: documents exactly what the buffer change bought."""
    cfg = load_config()
    lev, taker = cfg.get("leverage.default"), cfg.get("backtest.fee_taker")
    assert config_module.max_stop_distance(
        lev, config_module.COMMON_MAINTENANCE_RATE, taker, 0.005) < 0
    assert config_module.max_stop_distance(
        lev, config_module.COMMON_MAINTENANCE_RATE, taker, 0.003) > 0


def test_lower_buffer_did_not_relax_the_other_guarantees():
    """Everything the user asked to keep must still hold."""
    cfg = load_config()
    assert cfg.get("leverage.default") == 100
    assert cfg.get("leverage.minimum") == 100
    assert cfg.get("leverage.margin_mode") == "isolated"
    assert cfg.get("leverage.allow_margin_topup") is False
    assert cfg.get("protection.verify_liq_price") is True
    assert cfg.get("protection.emergency_close_on_sl_failure") is True
    assert cfg.get("risk.max_daily_loss") == 0.01
    assert cfg.get("risk.max_drawdown") == 0.03
    assert cfg.get("risk.max_consecutive_losses") == 3
    assert cfg.get("risk.max_open_positions") == 1
