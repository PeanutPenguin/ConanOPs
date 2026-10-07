"""
Background QThread worker for restoring a backup.

restore_backup() extracts a zip (and, before that, takes a full
safety backup of the current world) -- for a large save this can take
a real amount of time, and running it on the main thread would freeze
the whole UI for the duration. This wraps the full restore sequence
(stop the server, restore, relaunch if it was running) so it happens
off the main thread, mirroring the same QThread-worker pattern
`update_runner.py` uses for SteamCMD updates.
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal

import backup_manager
import process_manager


class RestoreWorker(QThread):
    """Stops the server, restores `entry` into it, and relaunches it
    (only if it was running before). Any failure at any step is
    caught and reported via the signal rather than raised -- a broken
    restore shouldn't crash the whole app."""
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
