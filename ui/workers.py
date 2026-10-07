"""Small helper shared by everything that runs QThread workers."""
from __future__ import annotations


def keep_until_finished(holder: list, worker) -> None:
    """Keeps `worker` referenced (in `holder`) until its thread has really
    finished, so Qt never destroys a QThread that's still running."""
    if worker is None:
        return
    holder.append(worker)
    worker.finished.connect(lambda w=worker: holder.remove(w) if w in holder else None)
