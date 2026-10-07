"""
Shared rotating log at ~/ConanOps/conanops.log. The packaged app has no
console, so background failures must be logged here to be seen.

Usage: _log = applog.get_logger(__name__)
"""
from __future__ import annotations

import logging
import logging.handlers
import os

_LOG_DIR = None
_LOG_PATH = None
_configured = False


def _configure() -> None:
    global _configured, _LOG_DIR, _LOG_PATH
    if _configured:
        return
    _LOG_DIR = os.path.join(os.path.expanduser("~"), "ConanOps")
    _LOG_PATH = os.path.join(_LOG_DIR, "conanops.log")
    os.makedirs(_LOG_DIR, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        _LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    ))
    root = logging.getLogger("conanops")
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Logger under 'conanops'; `name` (usually __name__) is only a prefix."""
    _configure()
    short = name.rsplit(".", 1)[-1]
    return logging.getLogger(f"conanops.{short}")


def log_path() -> str:
    return _LOG_PATH
