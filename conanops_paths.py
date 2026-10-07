"""Default locations for ConanOps' own files. Kept apart from models.py so
low-level, non-Qt modules can import it cheaply."""
from __future__ import annotations

import os
import sys

# Subfolder for everything ConanOps creates. self_update.py and
# self_delete.py hardcode this same string on purpose, so a broken update to
# this file can't break their recovery.
APP_DATA_DIRNAME = "data"


def app_install_dir() -> str:
    """Folder holding the .exe (packaged) or the repo root (from source)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def app_data_dir() -> str:
    """app_install_dir()/APP_DATA_DIRNAME. Data lives in a subfolder because
    self-update backs up the whole install dir and "Delete ConanOps (app
    only)" empties it; server files must not be dragged along or deleted."""
    return os.path.join(app_install_dir(), APP_DATA_DIRNAME)


def public_fallback_root() -> str:
    """Fallback when app_install_dir() has a space (and the old default, so
    models._migrate_legacy_data_dir() checks it too). C:\\Users\\Public is
    always present, writable without admin, and has no space."""
    return os.path.join(os.path.dirname(os.path.expanduser("~")), "Public", "ConanOps")


def no_space_root() -> str:
    """Root for config, theme, session history and per-server SteamCMD/
    install/backup folders. Uses app_data_dir() unless its path has a space:
    SteamCMD's app_update can fail with "Missing configuration" then."""
    app_dir = app_install_dir()
    if " " not in app_dir:
        return app_data_dir()
    return public_fallback_root()
