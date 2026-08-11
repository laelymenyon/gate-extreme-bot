"""PHASE 16 (finalize) — .env and configuration loading, end-to-end.

The suite's hermeticity fixture (tests/conftest.py) keeps the developer's own ``.env``
and any ambient ``GATE_API_*`` exports out of every test — that protects the *tests*.
This file tests the production path itself, with a real ``.env`` file:

* credentials are read from ``.env`` and from nowhere else,
* the values never leak into ``repr`` or any status output,
* ``DRY_RUN=false`` in ``.env`` is only one of three switches and never opens the gate
  on its own,
* a missing ``.env`` loads cleanly with no credentials (dry-run stays on),
* a malformed, empty, or section-less ``config.yaml`` is a ``ConfigError``, never a
  guessed default.

Every test here writes its own ``.env`` / ``config.yaml`` into ``tmp_path`` and points
``config.ENV_PATH`` / ``config.CONFIG_PATH`` at it, so nothing touches the repository's
real files.
"""

from __future__ import annotations

import pytest

import config as config_module
import main
from config import ConfigError, load_config

KEY = "k" * 32
SECRET = "s" * 64


@pytest.fixture(autouse=True)
def _fresh_process_env(monkeypatch):
    """Each test must start with a clean process environment.

    ``load_dotenv`` writes loaded values straight into ``os.environ``, and the module
    shares one process. The suite-wide hermetic fixture (conftest.py) already clears
    these before every test; this makes the guarantee local to this file explicit so
    a future edit here cannot silently read a stale value from an earlier test.
    """
    for name in ("GATE_API_KEY", "GATE_API_SECRET", "DRY_RUN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Point config at scratch files for every test in this module."""
    monkeypatch.setattr(config_module, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "config.yaml")
    # Copy the shipped config so validation has a full, valid base to mutate.
    import copy

    import yaml as _yaml

    with (config_module.ROOT / "config.yaml").open(encoding="utf-8") as handle:
        raw = _yaml.safe_load(handle)
    (tmp_path / "config.yaml").write_text(_yaml.safe_dump(raw), encoding="utf-8")
    return tmp_path


def _write_env(tmp_path, **values) -> None:
    lines = [f"{name}={value}" for name, value in values.items()]
    (tmp_path / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_config(tmp_path, text: str) -> None:
    (tmp_path / "config.yaml").write_text(text, encoding="utf-8")


# --- credentials come from the .env file -----------------------------------

def test_credentials_are_loaded_from_the_env_file(tmp_path):
    _write_env(tmp_path, GATE_API_KEY=KEY, GATE_API_SECRET=SECRET, DRY_RUN="true")
    cfg = load_config()
    assert cfg.credentials.present is True
    assert cfg.credentials.key == KEY
    assert cfg.credentials.secret == SECRET
    assert cfg.env_dry_run is True
    assert cfg.live_enabled is False


def test_dry_run_false_in_env_alone_keeps_the_gate_shut(tmp_path):
    """DRY_RUN=false is one switch; live trading still needs the other two."""
    _write_env(tmp_path, GATE_API_KEY=KEY, GATE_API_SECRET=SECRET, DRY_RUN="false")
    assert load_config().live_enabled is False                      # default mode paper
    assert load_config(run_mode="live").live_enabled is False       # no --confirm-live
    assert load_config(run_mode="paper", confirm_live=True).live_enabled is False


def test_all_three_switches_with_env_file_credentials_open_the_gate(tmp_path):
    """The full gate, driven end to end from a real .env file."""
    _write_env(tmp_path, GATE_API_KEY=KEY, GATE_API_SECRET=SECRET, DRY_RUN="false")
    cfg = load_config(run_mode="live", confirm_live=True)
    assert cfg.live_enabled is True
    assert cfg.dry_run is False


def test_dry_run_false_without_credentials_in_env_is_refused(tmp_path):
    """An open gate with an empty .env must fail at load, not at the first order."""
    _write_env(tmp_path, DRY_RUN="false")
    with pytest.raises(ConfigError, match="GATE_API_KEY"):
        load_config(run_mode="live", confirm_live=True)


def test_partial_env_credentials_are_not_present(tmp_path, monkeypatch):
    monkeypatch.delenv("GATE_API_KEY", raising=False)
    monkeypatch.delenv("GATE_API_SECRET", raising=False)
    _write_env(tmp_path, GATE_API_KEY=KEY)   # secret missing
    assert load_config().credentials.present is False
    # load_dotenv persists into the process environment, so clear it before the
    # second load or the first file's key would survive into the second.
    monkeypatch.delenv("GATE_API_KEY", raising=False)
    monkeypatch.delenv("GATE_API_SECRET", raising=False)
    _write_env(tmp_path, GATE_API_SECRET=SECRET)  # key missing
    assert load_config().credentials.present is False


def test_missing_env_file_loads_cleanly(tmp_path):
    """No .env is the fresh-install state: no crash, no credentials, dry-run on."""
    assert not (tmp_path / ".env").exists()
    cfg = load_config()
    assert cfg.credentials.present is False
    assert cfg.env_dry_run is True
    assert cfg.live_enabled is False


# --- secrets never leak ----------------------------------------------------

def test_loaded_secrets_never_appear_in_repr(tmp_path):
    _write_env(tmp_path, GATE_API_KEY=KEY, GATE_API_SECRET=SECRET)
    cfg = load_config()
    rendered = repr(cfg) + repr(cfg.credentials)
    assert KEY not in rendered
    assert SECRET not in rendered
    assert "set" in repr(cfg.credentials)     # presence, not the value


def test_loaded_secrets_never_appear_in_status_output(tmp_path, capsys):
    """`--status` must print presence, never the material."""
    _write_env(tmp_path, GATE_API_KEY=KEY, GATE_API_SECRET=SECRET)
    cfg = load_config()
    main.print_status(cfg)
    out = capsys.readouterr().out
    assert KEY not in out
    assert SECRET not in out
    assert "Credentials       : present" in out


# --- malformed / missing configuration fails closed ------------------------

def test_malformed_config_yaml_is_refused(tmp_path):
    _write_config(tmp_path, "outer: [unclosed\n")
    with pytest.raises(ConfigError):
        load_config()


def test_empty_config_yaml_is_refused(tmp_path):
    _write_config(tmp_path, "{}\n")
    # An empty mapping fails on its first required key; what matters is the refusal.
    with pytest.raises(ConfigError, match="mode"):
        load_config()


def test_missing_config_yaml_is_refused(tmp_path):
    (tmp_path / "config.yaml").unlink()
    with pytest.raises(ConfigError, match="missing"):
        load_config()


def test_config_without_the_risk_section_is_refused(tmp_path):
    import yaml as _yaml

    with (tmp_path / "config.yaml").open(encoding="utf-8") as handle:
        raw = _yaml.safe_load(handle)
    del raw["risk"]
    _write_config(tmp_path, _yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="risk"):
        load_config()


def test_config_with_an_out_of_range_value_is_refused(tmp_path):
    import yaml as _yaml

    with (tmp_path / "config.yaml").open(encoding="utf-8") as handle:
        raw = _yaml.safe_load(handle)
    raw["risk"]["max_daily_loss"] = 0.99
    _write_config(tmp_path, _yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="max_daily_loss"):
        load_config()
