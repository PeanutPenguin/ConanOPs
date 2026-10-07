"""Where ConanOps' own files live by default, shared across the parts
of the app that need it (models.py for its own config/theme/session
data, ui/setup_wizard.py for new servers' SteamCMD/install folders).
Kept separate from models.py so lower-level, non-Qt modules don't need
to import the whole config/dataclass module just for a path.
"""
from __future__ import annotations

import os
import sys

# The subfolder name everything ConanOps creates lives under, when
# no_space_root() prefers app_install_dir() (see its own docstring).
# Self-update, the self-delete "app only" path, and this app's own
# startup file sweep all need to know this exact name too, so they can
# each avoid touching it while operating on APP_INSTALL_DIR as a
# whole -- see self_update.py and self_delete.py, both of which
# hardcode this same string rather than importing this module, so
# that a broken update to THIS file can't also break their ability to
# recover from it (see self_update.py's own comment on the same
# tradeoff for its backup/marker paths).
APP_DATA_DIRNAME = "data"


def app_install_dir() -> str:
    """Where ConanOps itself lives: the folder holding the .exe for a
    packaged build, or the repo root when run from source. The one
    source of truth for this -- ui/main_window.py's APP_INSTALL_DIR
    is just this, computed once at import time; self_update.py,
    main.py's relaunch/self-delete, and no_space_root() below (see its
    docstring) all key off the same value via one or the other."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def app_data_dir() -> str:
    """app_install_dir(), plus the dedicated APP_DATA_DIRNAME
    subfolder -- what no_space_root() actually returns when it
    prefers app_install_dir() (see its docstring). Everything ConanOps
    creates lands in THIS subfolder, not loose alongside the app's own
    program files: the app's self-updater backs up and overlays
    APP_INSTALL_DIR as a whole when updating itself, and "Delete
    ConanOps (app only)" removes APP_INSTALL_DIR's contents entirely
    -- if server installs, backups, and config lived directly in
    APP_INSTALL_DIR rather than a subfolder carved out from it, an
    app update's own backup/restore would drag every server's files
    (many GB, and world-save files that could be locked by a running
    server) along with it, and "app only" deletion would take every
    server down with it despite promising not to."""
    return os.path.join(app_install_dir(), APP_DATA_DIRNAME)


def public_fallback_root() -> str:
    """The guaranteed-no-space fallback no_space_root() uses when
    app_install_dir() itself has a space in it -- and, before
    app_install_dir() became the preferred default, what
    no_space_root() always returned. Split out under its own name
    (rather than inlined in no_space_root()) so
    models.py's _migrate_legacy_data_dir() can check it too: anyone
    who's been running a version where THIS was the default has real
    data sitting here that the newer app_install_dir()-based default
    needs to find on upgrade, not just the even-older ~/ConanOps
    location that predates this path entirely.

    C:\\Users\\Public is on the same drive as the user's profile,
    always exists, is writable by standard (non-admin) accounts by
    design, and its own path is guaranteed not to have a space."""
    return os.path.join(os.path.dirname(os.path.expanduser("~")), "Public", "ConanOps")


def no_space_root() -> str:
    """The one folder everything ConanOps creates by default lives
    under -- app config, theme, session history, and (each under
    their own server id) SteamCMD/install/backup folders.

    Prefers app_data_dir() -- a dedicated subfolder alongside the app
    itself, e.g. inside the folder someone downloaded/extracted
    ConanOps into -- over public_fallback_root(), PROVIDED
    app_install_dir() has no space in it: SteamCMD's app_update can
    fail with "ERROR! ... (Missing configuration)" when its install
    path contains one, and that's true of app_install_dir() just as
    much as it was of %USERPROFILE% (e.g.
    "C:\\Users\\Jane Doe\\Downloads\\ConanOps" -- confirmed against a
    real install this exact way, for the profile-folder case this app
    used even before public_fallback_root() existed). Falls back to
    public_fallback_root() when app_install_dir() has a space -- see
    that function's own docstring for why it's safe."""
    app_dir = app_install_dir()
    if " " not in app_dir:
        return app_data_dir()
    return public_fallback_root()
