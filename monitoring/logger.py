"""Structured logging with secret redaction.

PHASE 11.

Three properties, in order of how badly getting them wrong would hurt:

1. **Secrets never reach a log line.** ``GATE_API_KEY``, ``GATE_API_SECRET`` and the
   ``SIGN`` header are redacted from every record — message, arguments and extras alike —
   by a filter attached to the logger rather than by discipline at the call sites. A
   redaction that depends on every future caller remembering it is not a redaction. The
   filter also catches anything that *looks* like a key (a long hex run) even when it was
   not one of the configured values, because the failure mode being prevented is an
   operator pasting a log into an issue tracker.
2. **Skips are logged, not swallowed.** A score-80 filter across six categories and four
   timeframes is designed to reject almost everything (ARCHITECTURE §7), so "did nothing"
   is the normal outcome and the reason it did nothing is the primary signal. ``skip()``
   exists for exactly that and records the stage that refused.
3. **Machine-readable to file, human-readable to console.** The JSON file is what gets
   grepped after a bad day; the console line is what a person watches. Neither is derived
   from the other by parsing.

Nothing here decides anything and nothing here can place an order.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

__all__ = [
    "REDACTED",
    "SecretRedactor",
    "JsonFormatter",
    "ConsoleFormatter",
    "setup_logging",
    "get_logger",
    "log_skip",
]

#: What a redacted value is replaced with. Distinctive on purpose: seeing it in a log is
#: evidence the filter ran, whereas an empty string is indistinguishable from absence.
REDACTED = "***REDACTED***"

#: Env vars whose values must never appear in a record.
SECRET_ENV_VARS = ("GATE_API_KEY", "GATE_API_SECRET")

#: Record keys whose values are secrets whatever they contain.
SECRET_KEYS = frozenset({
    "sign", "key", "secret", "api_key", "api_secret", "gate_api_key",
    "gate_api_secret", "authorization", "password", "token",
})

#: A long hex run is what a Gate.io key or an HMAC signature looks like. Matching on shape
#: catches a secret that was never registered with the filter — the case discipline misses.
_HEX_RUN = re.compile(r"\b[0-9a-fA-F]{32,}\b")


class SecretRedactor(logging.Filter):
    """Strips secrets from every record that passes through the logger.

    Attached to the logger, not the handler, so it applies to every destination including
    ones added later. Values are collected from the environment at construction *and* can
    be registered explicitly, since a config-supplied credential may never be exported.
    """

    def __init__(self, extra_values: Iterable[str] = (), *, redact_hex: bool = True) -> None:
        super().__init__()
        self._values: set[str] = set()
        self.redact_hex = redact_hex
        for name in SECRET_ENV_VARS:
            self.register(os.getenv(name, ""))
        for value in extra_values:
            self.register(value)

    def register(self, value: str | None) -> None:
        """Add a literal value to redact. Short values are ignored as too noisy to mask."""
        if value and len(str(value)) >= 8:
            self._values.add(str(value))

    def scrub(self, value: Any) -> Any:
        """Redact recursively. Containers are walked so a nested secret cannot escape."""
        if isinstance(value, str):
            return self._scrub_text(value)
        if isinstance(value, Mapping):
            return {
                key: (REDACTED if str(key).lower() in SECRET_KEYS else self.scrub(item))
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            scrubbed = [self.scrub(item) for item in value]
            return type(value)(scrubbed) if isinstance(value, tuple) else scrubbed
        return value

    def _scrub_text(self, text: str) -> str:
        for secret in self._values:
            if secret in text:
                text = text.replace(secret, REDACTED)
        if self.redact_hex:
            text = _HEX_RUN.sub(REDACTED, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.scrub(record.msg)
        if record.args:
            record.args = self.scrub(record.args)
        for key, value in list(record.__dict__.items()):
            if key in _RESERVED:
                continue
            record.__dict__[key] = (
                REDACTED if key.lower() in SECRET_KEYS else self.scrub(value)
            )
        return True


#: LogRecord attributes that belong to logging itself, not to the caller's extras.
_RESERVED = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
    "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
    "relativeCreated", "thread", "threadName", "processName", "process", "taskName",
    "message", "asctime",
})


class JsonFormatter(logging.Formatter):
    """One JSON object per line — what gets grepped after a bad day."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": record.created,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = _jsonable(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return str(value)


class ConsoleFormatter(logging.Formatter):
    """A compact human line. Skips are dimmed so the rare accepted signal stands out."""

    def format(self, record: logging.LogRecord) -> str:
        stage = getattr(record, "stage", "")
        symbol = getattr(record, "symbol", "")
        prefix = " ".join(part for part in (symbol, stage) if part)
        head = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7}"
        body = record.getMessage()
        return f"{head} {prefix + ': ' if prefix else ''}{body}"


@dataclass(frozen=True)
class _Setup:
    logger: logging.Logger
    redactor: SecretRedactor


def setup_logging(cfg: Any = None, *, level: str | None = None, path: str | None = None,
                  json_file: bool | None = None, console: bool = True,
                  secrets: Iterable[str] = ()) -> logging.Logger:
    """Configure the root ``gate`` logger. Idempotent — safe to call more than once.

    Redaction is attached to the logger rather than to a handler, so a handler added later
    by anything else inherits it.
    """
    if cfg is not None:
        level = level or str(cfg.get("logging.level", "INFO"))
        path = path if path is not None else str(cfg.get("logging.file", "logs/bot.log"))
        json_file = (
            json_file if json_file is not None else bool(cfg.get("logging.json", True))
        )
        if not cfg.get("logging.redact_secrets", True):
            # Not honoured as written. A switch that turns off redaction is a switch that
            # eventually gets left off, and the cost is a leaked API key.
            logging.getLogger("gate").warning(
                "logging.redact_secrets=false is ignored; redaction is not optional"
            )
        credentials = getattr(cfg, "credentials", None)
        if credentials is not None:
            secrets = list(secrets) + [
                getattr(credentials, "key", ""), getattr(credentials, "secret", ""),
            ]

    logger = logging.getLogger("gate")
    logger.setLevel(getattr(logging, str(level or "INFO").upper(), logging.INFO))
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    for existing in [f for f in logger.filters if isinstance(f, SecretRedactor)]:
        logger.removeFilter(existing)

    redactor = SecretRedactor(secrets)
    logger.addFilter(redactor)

    if path:
        destination = Path(path)
        if str(destination.parent) not in ("", "."):
            destination.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(destination, encoding="utf-8")
        # JSON unless explicitly told otherwise. The file is the artefact that gets grepped
        # after a bad day, and a human-formatted one cannot be parsed back reliably.
        file_handler.setFormatter(
            ConsoleFormatter() if json_file is False else JsonFormatter()
        )
        logger.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(ConsoleFormatter())
        logger.addHandler(stream)

    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


def get_logger(name: str = "") -> logging.Logger:
    return logging.getLogger(f"gate.{name}" if name else "gate")


def log_skip(logger: logging.Logger, symbol: str, stage: str, reason: str,
             **extra: Any) -> None:
    """Record a setup that was not taken.

    Skips are the primary signal, not noise. The bot is designed to reject almost
    everything, so "3400 bars, 3390 skipped at the regime stage" is the finding — and
    without the stage it is unactionable.
    """
    logger.info(reason, extra={"event": "skip", "symbol": symbol, "stage": stage, **extra})
