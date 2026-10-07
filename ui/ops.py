"""
Run a blocking call (e.g. stopping a server that's saving) on a worker thread
while a local event loop keeps the UI responsive, then return its result or
re-raise its exception. For callers that need the result before continuing.
"""
from __future__ import annotations

from PySide6.QtCore import QEventLoop, QThread, Signal


class _Runner(QThread):
    finished_with = Signal(object, object)  # result, exception

    def __init__(self, fn):
        super().__init__()
        import powershell
        # A web action's "no permission prompts" block follows the work
        # onto this thread.
        self._fn = powershell.carry_gate(fn)

    def run(self) -> None:
        try:
            self.finished_with.emit(self._fn(), None)
        except Exception as e:  # noqa: BLE001 - re-raised on the caller's thread
            self.finished_with.emit(None, e)


def run_responsive(fn):
    loop = QEventLoop()
    box = {}

    def done(result, error):
        box["result"], box["error"] = result, error
        loop.quit()

    runner = _Runner(fn)
    runner.finished_with.connect(done)
    runner.start()
    if "result" not in box:
        loop.exec()
    runner.wait()
    if box.get("error") is not None:
        raise box["error"]
    return box.get("result")
