"""
Background QThread worker for restoring a backup (stop, restore, relaunch),
since a large restore would freeze the UI on the main thread.
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal

import backup_manager
import process_manager


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
            process_manager.graceful_stop(self.server)
            backup_manager.restore_backup(self.server.install_dir, self.entry)
            if self.was_running:
                process_manager.launch(self.server)
            self.finished_restore.emit(True, "")
        except Exception as e:  # noqa: BLE001 - surface any failure to the UI instead of crashing the thread silently
            self.finished_restore.emit(False, str(e))
