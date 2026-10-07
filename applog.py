"""
Centralized logging for ConanOps.

Writes a rotating log file to ~/ConanOps/conanops.log so failures that
happen in background threads or best-effort code paths (SteamCMD,
webhooks, netsh/UPnP, backup pruning) are visible after the fact.
Previously most of these were either silently swallowed
(`except OSError: pass`) or sent to a bare `print()`, which is
useless once the app is packaged with PyInstaller's `--windowed` flag
(no visible console at all).

Usage:
    import applog
    _log = applog.get_logger(__name__)
    _log.warning("...")
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
    """Returns a logger under the shared 'conanops' hierarchy, writing
    to ~/ConanOps/conanops.log. `name` is typically __name__ from the
    calling module, just for the log line prefix -- it doesn't need to
    be unique."""
    _configure()
    short = name.rsplit(".", 1)[-1]
    return logging.getLogger(f"conanops.{short}")


def log_path() -> str:
    return _LOG_PATH
