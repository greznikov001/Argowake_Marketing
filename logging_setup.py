from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import os
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import structlog
from datetime import datetime


_TRANSACTION_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("transaction_id", default=None)
_CONFIGURED = False
_DEFAULT_CONFIG_PATH = Path(".state/logging_config.json")
_DEFAULT_LOG_PATH = Path(".state/argowake.log")
_DEFAULT_MAX_BYTES = 1_048_576


@dataclass(frozen=True)
class LoggingConfig:
    path: Path
    max_bytes: int
    level: str

    @classmethod
    def from_mapping(cls, payload: dict[str, object]) -> "LoggingConfig":
        path = Path(str(payload.get("path") or _DEFAULT_LOG_PATH))
        max_bytes = int(payload.get("max_bytes") or _DEFAULT_MAX_BYTES)
        level = str(payload.get("level") or "INFO").upper()
        return cls(path=path, max_bytes=max_bytes, level=level)


def ensure_logging_config(path: Path = _DEFAULT_CONFIG_PATH) -> LoggingConfig:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        default = {"path": str(_DEFAULT_LOG_PATH), "max_bytes": _DEFAULT_MAX_BYTES, "level": "INFO"}
        path.write_text(json.dumps(default, indent=2, sort_keys=True), encoding="utf-8")
        return LoggingConfig.from_mapping(default)

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Logging config must be a JSON object: {path}")
    config = LoggingConfig.from_mapping(payload)
    if config.max_bytes <= 0:
        raise ValueError("Logging max_bytes must be positive.")
    return config


def configure_logging() -> LoggingConfig:
    global _CONFIGURED
    if _CONFIGURED:
        return ensure_logging_config()

    config = ensure_logging_config()
    _configure_structlog(config.level)
    _CONFIGURED = True
    return config


def set_transaction_id(transaction_id: str | None = None) -> str:
    value = transaction_id or uuid.uuid4().hex
    _TRANSACTION_ID.set(value)
    structlog.contextvars.bind_contextvars(transaction_id=value)
    return value


def get_transaction_id() -> str:
    current = _TRANSACTION_ID.get()
    return current or set_transaction_id()


def _configure_structlog(level: str) -> None:
    config = ensure_logging_config()
    config.path.parent.mkdir(parents=True, exist_ok=True)

    class TimestampRotatingFileHandler(logging.handlers.BaseRotatingHandler):
        def __init__(self, filename: str | Path, max_bytes: int) -> None:
            self.max_bytes = max_bytes
            super().__init__(str(filename), mode="a", encoding="utf-8", delay=False)

        def shouldRollover(self, record):  # noqa: N802
            if self.stream is None:
                self.stream = self._open()
            return os.path.exists(self.baseFilename) and os.path.getsize(self.baseFilename) >= self.max_bytes

        def doRollover(self):  # noqa: N802
            if self.stream:
                self.stream.close()
                self.stream = None
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            base_path = Path(self.baseFilename)
            rotated = base_path.with_name(f"{base_path.stem}-{timestamp}{base_path.suffix}")
            counter = 1
            while rotated.exists():
                rotated = base_path.with_name(f"{base_path.stem}-{timestamp}-{counter}{base_path.suffix}")
                counter += 1
            if base_path.exists():
                base_path.replace(rotated)
            self.stream = self._open()

        def emit(self, record):
            try:
                if self.shouldRollover(record):
                    self.doRollover()
                super().emit(record)
            except Exception:
                self.handleError(record)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.add_logger_name,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(sort_keys=True),
        foreign_pre_chain=shared_processors,
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(_level_value(level))

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    file_handler = TimestampRotatingFileHandler(config.path, max_bytes=config.max_bytes)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)


def _level_value(level_name: str) -> int:
    import logging

    if level_name == "VERBOSE":
        return logging.DEBUG
    value = getattr(logging, level_name.upper(), logging.INFO)
    return value if isinstance(value, int) else logging.INFO
