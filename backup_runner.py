"""
Background QThread worker for restoring a backup (stop, restore, relaunch),
since a large restore would freeze the UI on the main thread.
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal

import backup_manager
import process_manager


class BackupWorker(QThread):
    """Makes one backup (save, safe copy, check, second copy, thin out old
    ones) off the window's thread, so a big world never freezes ConanOps."""
    finished_backup = Signal(object)  # backup_manager.BackupOutcome

    def __init__(self, server, trigger: str, label: str = "", parent=None):
        super().__init__(parent)
        self.server = server
        self.trigger = trigger
        self.label = label

    def run(self) -> None:
        self.finished_backup.emit(backup_manager.run_backup_job(self.server, self.trigger, self.label))


class RestoreWorker(QThread):
    """Stops the server, restores `entry`, relaunches if it was running.
    Failures are reported via the signal, never raised."""
    finished_restore = Signal(bool, str)  # success, error message (empty on success)

    def __init__(self, server, entry, was_running: bool, parent=None):
        super().__init__(parent)
        self.server = server
        self.entry = entry
        self.was_running = was_running

    def run(self) -> None:
        try:
            if process_manager.graceful_stop(self.server) is False:
                raise process_manager.StillRunningError(process_manager.STILL_RUNNING_MESSAGE +
                                                        " Nothing was restored.")
            backup_manager.restore_backup(self.server.install_dir, self.entry)
            if self.was_running:
                process_manager.launch(self.server)
            self.finished_restore.emit(True, "")
        except Exception as e:  # noqa: BLE001 - surface any failure to the UI instead of crashing the thread silently
            self.finished_restore.emit(False, str(e))
