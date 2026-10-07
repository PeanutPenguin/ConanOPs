"""
Background QThread workers for SteamCMD update checking and applying.
Shared between the manual "Check Now" / "Update Now" buttons on the
Updates page and the automatic paths (auto-update when nobody's online,
and the update-check that runs after a scheduled restart) so there's one
place that actually talks to SteamCMD, not two copies that could drift.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QThread, Signal

import applog
import steamcmd
import changelog as changelog_mod
import self_update

_log = applog.get_logger(__name__)


class CheckWorker(QThread):
    """Checks Steam for the latest build id and fetches the latest news
    item, in one background pass."""
    finished_check = Signal(object, object)  # latest_buildid: str|None, changelog_info: ChangelogInfo

    def __init__(self, steamcmd_dir: str, parent=None):
        super().__init__(parent)
        self.steamcmd_dir = steamcmd_dir

    def run(self) -> None:
        # Anything unexpected here (steamcmd's own subprocess plumbing,
        # a malformed VDF dump, a network hiccup fetch_latest_news()
        # didn't already swallow) must still emit -- callers key an
        # in-flight/"busy" guard off this worker existing, so if run()
        # raised straight through without emitting, that guard would
        # never clear and this server could never be checked again
        # for the rest of the session.
        try:
            latest = steamcmd.get_latest_buildid(self.steamcmd_dir)
            info = changelog_mod.fetch_latest_news()
        except Exception as e:  # noqa: BLE001 - always emit so callers' busy-guards clear
            _log.error(f"Update check failed unexpectedly: {e}")
            latest, info = None, changelog_mod.ChangelogInfo()
        self.finished_check.emit(latest, info)


class UpdateWorker(QThread):
    """The whole update sequence, off the UI thread, in the only safe
    order:

      1. stop the server (if `stop_server` is given) -- with RCON on,
         this asks it to save and waits for the save to finish, which
         can take a while on a big world;
      2. back up the world (if `backup_destination` is given) -- AFTER
         the stop, so the backup is a consistent copy of a closed
         database, not a live one mid-write;
      3. check there's enough free disk space;
      4. run SteamCMD's update + validate.

    Steps 2 and 3 failing stop everything before SteamCMD touches a
    file (result.reason "backup" / "disk"). Relaunching afterwards is
    the caller's decision -- see main_window._on_update_apply_finished.
    """
    progress = Signal(str)
    finished_update = Signal(object)  # steamcmd.UpdateResult

    def __init__(self, steamcmd_dir: str, install_dir: str, parent=None, *, stop_server=None,
                 backup_server=None, backup_destination: str = "", check_disk: bool = True):
        super().__init__(parent)
        self.steamcmd_dir = steamcmd_dir
        self.install_dir = install_dir
        self.stop_server = stop_server
        self.backup_server = backup_server
        self.backup_destination = backup_destination
        self.check_disk = check_disk

    def run(self) -> None:
        # See CheckWorker.run()'s comment -- an unhandled exception here
        # would leave the update-busy flag stuck for the rest of the
        # session, so always emit.
        try:
            result = self._run_steps()
        except Exception as e:  # noqa: BLE001
            _log.error(f"Update failed unexpectedly: {e}")
            result = steamcmd.UpdateResult(False, f"Update failed unexpectedly: {e}", reason="steamcmd")
        self.finished_update.emit(result)

    def _run_steps(self):
        import backup_manager
        import process_manager

        if self.stop_server is not None and self.stop_server.install_dir and process_manager.is_running(self.stop_server.install_dir):
            self.progress.emit("Stopping the server (saving the world first if RCON is on)…")
            process_manager.graceful_stop(self.stop_server)

        if self.backup_server is not None and self.backup_destination:
            self.progress.emit("Backing up the world before updating…")
            try:
                # None just means there's no world yet to back up (a fresh
                # server) -- fine to carry on. A real failure raises.
                backup_manager.create_backup_for_server(
                    self.backup_server, self.backup_destination, backup_manager.TRIGGER_PRE_UPDATE,
                )
            except Exception as e:  # noqa: BLE001
                _log.error(f"Pre-update backup failed: {e}")
                return steamcmd.UpdateResult(
                    False, f"The pre-update backup failed ({e}), so the update wasn't started -- nothing was "
                           f"changed. Check the backup folder on Backups settings.", reason="backup",
                )

        if self.check_disk:
            ok, free, needed = steamcmd.check_update_disk_space(self.install_dir)
            if not ok:
                return steamcmd.UpdateResult(
                    False, f"Not enough free disk space to update safely: {steamcmd.format_gb(free)} free, about "
                           f"{steamcmd.format_gb(needed)} needed. Nothing was changed -- free up space and it will "
                           f"retry.", reason="disk",
                )

        return steamcmd.update_server(self.steamcmd_dir, self.install_dir, progress=self.progress.emit)


class ModDownloadWorker(QThread):
    """Downloads/updates the given Workshop mods via SteamCMD (callers
    pass every mod on the server, enabled or not).
    See steamcmd.download_workshop_items()'s docstring for why this
    exists -- modlist.txt pointing at a .pak doesn't make the .pak
    appear on disk."""
    finished_download = Signal(object)  # steamcmd.UpdateResult

    def __init__(self, steamcmd_dir: str, workshop_ids: list, parent=None):
        super().__init__(parent)
        self.steamcmd_dir = steamcmd_dir
        self.workshop_ids = workshop_ids

    def run(self) -> None:
        try:
            result = steamcmd.download_workshop_items(self.steamcmd_dir, self.workshop_ids)
        except Exception as e:  # noqa: BLE001 - always emit so the caller's UI doesn't hang waiting
            _log.error(f"Mod download failed unexpectedly: {e}")
            result = steamcmd.UpdateResult(False, f"Mod download failed unexpectedly: {e}")
        self.finished_download.emit(result)


class SelfUpdateWorker(QThread):
    """Runs self_update.apply_update() off the GUI thread. Applying a
    ConanOps update copies the app's own files (potentially a lot of
    them, and on Windows possibly hitting a locked-file rename -- see
    self_update._copy_file_with_swap) which can take long enough that
    running it directly on the UI thread made Qt report "Not
    Responding" and invited a force-close mid-copy, the one thing this
    whole module is designed to survive gracefully."""
    finished_update = Signal(object)  # self_update.UpdateResult

    def __init__(self, zip_path: str, install_dir: str, parent=None):
        super().__init__(parent)
        self.zip_path = zip_path
        self.install_dir = install_dir

    def run(self) -> None:
        try:
            result = self_update.apply_update(self.zip_path, self.install_dir)
        except self_update.UpdateValidationError as e:
            result = self_update.UpdateResult(False, str(e))
        except Exception as e:  # noqa: BLE001 - always emit so the button doesn't stay stuck on "Updating..."
            _log.error(f"Self-update failed unexpectedly: {e}")
            result = self_update.UpdateResult(False, f"Update failed unexpectedly: {e}")
        self.finished_update.emit(result)


class AppUpdateCheckWorker(QThread):
    """app_updates.check_latest() off the GUI thread. Emits
    (ReleaseInfo or None, error message or "")."""
    finished_check = Signal(object, str)

    def run(self) -> None:
        import app_updates
        try:
            self.finished_check.emit(app_updates.check_latest(), "")
        except app_updates.UpdateCheckError as e:
            self.finished_check.emit(None, str(e))
        except Exception as e:  # noqa: BLE001 - always emit
            _log.error(f"Update check failed unexpectedly: {e}")
            self.finished_check.emit(None, "Couldn't check for updates.")


class AppUpdateInstallWorker(QThread):
    """Downloads a release's update zip (with progress), verifies it, and
    installs it with self_update.apply_update(). Emits progress(done,
    total) while downloading and finished_update(UpdateResult) once --
    always, so the UI never stays stuck."""
    progress = Signal(int, int)
    finished_update = Signal(object)

    def __init__(self, info, install_dir: str, parent=None):
        super().__init__(parent)
        self.info = info
        self.install_dir = install_dir

    def run(self) -> None:
        import app_updates
        path = ""
        try:
            path = app_updates.download(
                self.info, progress=lambda d, t: self.progress.emit(int(d), int(t)),
                should_cancel=self.isInterruptionRequested,
            )
            result = self_update.apply_update(path, self.install_dir)
        except app_updates.UpdateCheckError as e:
            result = self_update.UpdateResult(False, str(e))
        except self_update.UpdateValidationError as e:
            result = self_update.UpdateResult(False, str(e))
        except Exception as e:  # noqa: BLE001
            _log.error(f"Online update failed unexpectedly: {e}")
            result = self_update.UpdateResult(False, f"Update failed unexpectedly: {e}")
        finally:
            if path:
                app_updates.cleanup(path)
        self.finished_update.emit(result)
