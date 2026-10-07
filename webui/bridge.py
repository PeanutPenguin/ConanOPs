"""Runs web-request actions on the GUI thread, which owns all app state.

During a web action nobody is at the PC, so message boxes are captured and
returned as text (questions answer "No"), and UAC prompts are suppressed.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Callable, List, Tuple

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

import applog
import powershell

_log = applog.get_logger(__name__)


class WebActionError(Exception):
    """Raised by an action to send a plain message back with HTTP 400."""


NEEDS_PC_NOTE = ("Not done: {what} needs Windows' permission, which can only be given at the PC -- do it in the "
                 "app there. (With \"Run with admin rights\" on in App Settings, this works from the web too.)")
QUEUED_NOTE = ("Waiting for the PC: {what} needs Windows' permission, which can only be given there. ConanOps "
               "will ask the next time someone uses it. (With \"Run with admin rights\" on in App Settings, "
               "this happens right away.)")


@contextmanager
def captured_dialogs():
    with powershell.no_prompts() as blocked, _captured() as messages:
        try:
            yield messages
        finally:
            for what in dict.fromkeys(blocked):
                messages.append(NEEDS_PC_NOTE.format(what=what))


@contextmanager
def _captured():
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
        """Runs fn() on the GUI thread; returns (result, captured messages).
        Re-raises fn's exception, or raises TimeoutError."""
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
