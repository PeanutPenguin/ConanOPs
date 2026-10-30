"""Small helper shared by everything that runs QThread workers."""
from __future__ import annotations

from PySide6.QtCore import Qt


# Workers held here outlive the window or page that started them, so closing
# (and Python freeing) a window mid-run never destroys a running QThread --
# Qt aborts the whole process when that happens.
IN_FLIGHT: list = []


def keep_running_past_owner(worker) -> None:
    """For workers created without a Qt parent: held until they finish."""
    keep_until_finished(IN_FLIGHT, worker)


def keep_until_finished(holder: list, worker) -> None:
    """Keeps `worker` referenced (in `holder`) until its thread has really
    finished, so Qt never destroys a QThread that's still running.

    The release runs on the GUI thread (queued), never on the worker's own
    thread: dropping the last reference there would destroy the QThread from
    inside itself while it's finishing, which can hang or crash."""
    if worker is None:
        return
    holder.append(worker)

    def release(w=worker):
        w.wait(5000)  # finished was emitted; let the thread fully exit first
        if w in holder:
            holder.remove(w)
    worker.finished.connect(release, Qt.QueuedConnection)
