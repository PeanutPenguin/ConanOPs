"""The taskbar should show the ConanOps icon, not Python's."""
from __future__ import annotations

import sys
import types

import pytest


def test_sets_an_explicit_app_id_on_windows(monkeypatch):
    pytest.importorskip("PySide6")
    import main
    calls = []
    fake_ctypes = types.SimpleNamespace(windll=types.SimpleNamespace(shell32=types.SimpleNamespace(
        SetCurrentProcessExplicitAppUserModelID=lambda app_id: calls.append(app_id))))
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)
    assert main.set_windows_app_id() is True
    assert calls == [main.APP_USER_MODEL_ID]


def test_does_nothing_off_windows(monkeypatch):
    pytest.importorskip("PySide6")
    import main
    monkeypatch.setattr(sys, "platform", "linux")
    assert main.set_windows_app_id() is False


def test_app_id_is_set_before_qapplication():
    import inspect
    pytest.importorskip("PySide6")
    import main
    src = inspect.getsource(main.main)
    assert src.index("set_windows_app_id()") < src.index("QApplication(sys.argv)")
