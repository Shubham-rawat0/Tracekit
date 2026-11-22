"""Logging helpers and compatibility shim for trace context access."""

from __future__ import annotations

import logging
import os

# Context accessors are re-exported here for old import paths; their implementation lives in core.context.
from tracekit.core.context import (
    _current_trace_context,
    get_current_span,
    get_current_trace,
    reset_current_span,
    reset_current_trace,
    set_current_span,
    set_current_trace,
)

__all__ = [
    "_current_trace_context",
    "get_current_trace",
    "set_current_trace",
    "reset_current_trace",
    "get_current_span",
    "set_current_span",
    "reset_current_span",
    "configure_logging",
    "get_logger",
]


# Guarding on logger.handlers prevents duplicate output when configure_logging is called repeatedly.
def configure_logging(level: str | int | None = None) -> logging.Logger:
    """Configure and return the root ``tracekit`` logger."""

    if level is None:
        level = os.getenv("TRACEKIT_LOG_LEVEL", "INFO")
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    logger = logging.getLogger("tracekit")
    logger.setLevel(level)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        logger.addHandler(handler)
    return logger


# Re-running configure_logging ensures the shared "tracekit" logger is ready before returning a child.
def get_logger(name: str | None = None) -> logging.Logger:
    """Return a child logger of ``tracekit``."""

    configure_logging()
    if not name:
        return logging.getLogger("tracekit")
    return logging.getLogger(f"tracekit.{name}")
