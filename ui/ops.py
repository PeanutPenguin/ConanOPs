"""
Run a blocking call (stopping a server that's saving its world) without
freezing the window: the call runs on a worker thread while a local
event loop keeps the UI painting and responsive, then this returns the
call's result (or re-raises its exception) like a normal function call.

For places that genuinely need the result before carrying on -- e.g.
the manual bisect's "reset the world" step, which has to have the
server stopped before it can touch the save files.
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
