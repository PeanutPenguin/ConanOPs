"""
Runs web requests' actions on the app's GUI thread.

The web server answers on background threads, but ConanOps' state (the
server list, the settings pages, the workers) belongs to the GUI thread.
Every action is posted there and the request waits for the result, so a
web click runs exactly the code a click in the app would.

While a web action runs, any dialog the app would normally pop up (a
warning, a "are you sure?") is captured instead of shown -- nobody is
sitting at the PC to click it -- and returned to the browser as text.
Questions are answered "No", the safe choice.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Callable, List, Tuple

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

import applog

_log = applog.get_logger(__name__)


class WebActionError(Exception):
    """Raised by an action to send a plain message back with HTTP 400."""


@contextmanager
def captured_dialogs():
    messages: List[str] = []
    saved = {name: getattr(QMessageBox, name) for name in ("information", "warning", "critical", "question")}
    saved_exec = QDialog.exec

    def make(kind, answer):
        def stub(parent=None, title="", text="", *args, **kwargs):
            messages.append(f"{title}: {text}" if title else str(text))
            return answer
        return staticmethod(stub)

    QMessageBox.information = make("information", QMessageBox.Ok)
    QMessageBox.warning = make("warning", QMessageBox.Ok)
    QMessageBox.critical = make("critical", QMessageBox.Ok)
    QMessageBox.question = make("question", QMessageBox.No)
    QDialog.exec = lambda self: (messages.append(f"(needs the app: {self.windowTitle()})"), QDialog.Rejected)[1]
    try:
        yield messages
    finally:
        for name, fn in saved.items():
            setattr(QMessageBox, name, fn)
        QDialog.exec = saved_exec


class GuiBridge(QObject):
    _invoke = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._invoke.connect(self._run)

    @Slot(object)
    def _run(self, job: dict) -> None:
        try:
            with captured_dialogs() as msgs:
                job["result"] = job["fn"]()
            job["messages"] = msgs
        except BaseException as e:  # noqa: BLE001 - handed back to the request thread
            job["error"] = e
        finally:
            job["done"].set()

    def call(self, fn: Callable[[], Any], timeout: float = 60.0) -> Tuple[Any, List[str]]:
        """Runs fn() on the GUI thread; returns (result, captured dialog
        messages). Raises whatever fn raised, or TimeoutError."""
        app = QApplication.instance()
        if app is None or QThread.currentThread() is app.thread():
            with captured_dialogs() as msgs:
                return fn(), msgs
        job = {"fn": fn, "done": threading.Event(), "messages": []}
        self._invoke.emit(job)
        if not job["done"].wait(timeout):
            raise TimeoutError("ConanOps didn't respond in time.")
        if "error" in job:
            raise job["error"]
        return job.get("result"), job["messages"]
