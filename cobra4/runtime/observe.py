"""Structured logging.

Default sink is a single-line key=value format on stderr. Set
``COBRA4_LOG_FORMAT=json`` for JSON-line output. Install
``cobra4[otel]`` and set ``COBRA4_OTEL_EXPORT=1`` to also forward each
record to an OpenTelemetry exporter (configured via standard OTel env
vars: ``OTEL_EXPORTER_OTLP_ENDPOINT``, etc.).

``log`` is callable and also exposes ``log.warn(...)`` /
``log.error(...)`` / ``log.info(...)``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, TextIO

_stream: TextIO = sys.stderr
_format = os.environ.get("COBRA4_LOG_FORMAT", "kv")  # "kv" | "json"
_otel_logger = None


def set_stream(stream: TextIO) -> None:
    """Override the destination stream (used by tests)."""
    global _stream
    _stream = stream


def set_format(fmt: str) -> None:
    """Switch between ``"kv"`` (default) and ``"json"``."""
    global _format
    if fmt not in ("kv", "json"):
        raise ValueError(f"unknown format '{fmt}'")
    _format = fmt


def enable_otel() -> None:
    """Wire the OTel logger if installed. Idempotent."""
    global _otel_logger
    if _otel_logger is not None:
        return
    try:
        from opentelemetry import _logs as otel_logs  # type: ignore
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler  # type: ignore
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor  # type: ignore
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter  # type: ignore
    except ImportError:  # pragma: no cover
        _otel_logger = False  # marker that we tried and failed
        return
    provider = LoggerProvider()
    provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    otel_logs.set_logger_provider(provider)
    _otel_logger = provider.get_logger("cobra4")


def _emit(level: str, message: str, fields: dict) -> None:
    ts_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    if _format == "json":
        record = {"ts": ts_iso, "level": level, "msg": message, **fields}
        print(json.dumps(record, default=str), file=_stream)
    else:
        parts = [ts_iso, f"level={level}", f"msg={_quote(message)}"]
        for k, v in fields.items():
            parts.append(f"{k}={_quote(v)}")
        print(" ".join(parts), file=_stream)
    # Optional OTel forwarding
    if os.environ.get("COBRA4_OTEL_EXPORT") == "1":
        if _otel_logger is None:
            enable_otel()
        if _otel_logger:
            try:
                from opentelemetry._logs import LogRecord, SeverityNumber  # type: ignore

                sev = {
                    "info": SeverityNumber.INFO,
                    "warn": SeverityNumber.WARN,
                    "error": SeverityNumber.ERROR,
                }.get(level, SeverityNumber.INFO)
                _otel_logger.emit(
                    LogRecord(
                        timestamp=int(time.time() * 1e9),
                        severity_number=sev,
                        severity_text=level.upper(),
                        body=message,
                        attributes=dict(fields),
                    )
                )
            except Exception:  # pragma: no cover
                pass


def _quote(v: Any) -> str:
    if isinstance(v, (int, float, bool)):
        return str(v)
    s = str(v)
    if any(c in s for c in (" ", "\t", "=", '"')):
        s = s.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{s}"'
    return s


class _LogProxy:
    """Callable + ``.warn`` / ``.error`` accessors."""

    def __call__(self, message: str = "", **fields: Any) -> None:
        from cobra4.runtime.effects import check as _check_effect

        _check_effect("log")
        _emit("info", message, fields)

    def info(self, message: str = "", **fields: Any) -> None:
        from cobra4.runtime.effects import check as _check_effect

        _check_effect("log")
        _emit("info", message, fields)

    def warn(self, message: str = "", **fields: Any) -> None:
        from cobra4.runtime.effects import check as _check_effect

        _check_effect("log")
        _emit("warn", message, fields)

    def error(self, message: str = "", **fields: Any) -> None:
        from cobra4.runtime.effects import check as _check_effect

        _check_effect("log")
        _emit("error", message, fields)


log = _LogProxy()


def silence() -> None:
    """Redirect log output to ``os.devnull``."""
    set_stream(open(os.devnull, "w", encoding="utf-8"))
