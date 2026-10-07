from __future__ import annotations

import sys
import types

import pytest

import startup_registration


class _FakeKey:
    def __init__(self, store, path):
        self.store = store
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install_fake_winreg(monkeypatch, initial: dict = None):
    """Installs a minimal fake winreg module in sys.modules (works
    even on non-Windows, since startup_registration only ever does
    `import winreg` inside each function, at call time) backed by a
    plain dict standing in for the registry key's values."""
    store = dict(initial or {})
    fake = types.ModuleType("winreg")
    fake.HKEY_CURRENT_USER = 1
    fake.KEY_READ = 1
    fake.KEY_SET_VALUE = 2
    fake.REG_SZ = 1

    def CreateKey(hive, path):
        return _FakeKey(store, path)

    def OpenKey(hive, path, reserved, access):
        if not store.get("_exists", True):
            raise FileNotFoundError(path)
        return _FakeKey(store, path)

    def SetValueEx(key, name, reserved, value_type, value):
        key.store[name] = value
        key.store["_exists"] = True

    def QueryValueEx(key, name):
        if name not in key.store:
            raise FileNotFoundError(name)
        return key.store[name], fake.REG_SZ

    def DeleteValue(key, name):
        if name not in key.store:
            raise FileNotFoundError(name)
        del key.store[name]

    fake.CreateKey = CreateKey
    fake.OpenKey = OpenKey
    fake.SetValueEx = SetValueEx
    fake.QueryValueEx = QueryValueEx
    fake.DeleteValue = DeleteValue

    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(startup_registration.sys, "platform", "win32")
    return store


def test_is_registered_false_off_windows(monkeypatch):
    monkeypatch.setattr(startup_registration.sys, "platform", "linux")
    assert startup_registration.is_registered() is False


def test_register_then_is_registered_true(monkeypatch):
    _install_fake_winreg(monkeypatch)
    startup_registration.register()
    assert startup_registration.is_registered() is True


def test_is_registered_false_when_nothing_set(monkeypatch):
    _install_fake_winreg(monkeypatch, initial={"_exists": False})
    assert startup_registration.is_registered() is False


def test_is_registered_false_when_value_points_elsewhere(monkeypatch):
    """A stale entry from a moved/reinstalled copy shouldn't read as
    "registered" -- it wouldn't actually launch this install."""
    store = _install_fake_winreg(monkeypatch)
    store["ConanOps"] = "\"C:\\Some\\Old\\Path\\ConanOps.exe\""
    assert startup_registration.is_registered() is False


def test_unregister_removes_the_value(monkeypatch):
    _install_fake_winreg(monkeypatch)
    startup_registration.register()
    assert startup_registration.is_registered() is True

    startup_registration.unregister()

    assert startup_registration.is_registered() is False


def test_unregister_is_safe_when_never_registered(monkeypatch):
    _install_fake_winreg(monkeypatch, initial={"_exists": False})
    startup_registration.unregister()  # must not raise


def test_register_updates_a_stale_entry(monkeypatch):
    store = _install_fake_winreg(monkeypatch)
    store["ConanOps"] = "\"C:\\Old\\ConanOps.exe\""

    startup_registration.register()

    assert startup_registration.is_registered() is True


def test_command_line_frozen_build_uses_bare_executable(monkeypatch):
    monkeypatch.setattr(startup_registration.sys, "frozen", True, raising=False)
    monkeypatch.setattr(startup_registration.sys, "executable", "C:\\ConanOps\\ConanOps.exe", raising=False)
    assert startup_registration._command_line() == '"C:\\ConanOps\\ConanOps.exe"'


def test_command_line_source_install_includes_main_py(monkeypatch):
    monkeypatch.setattr(startup_registration.sys, "frozen", False, raising=False)
    monkeypatch.setattr(startup_registration.sys, "executable", "C:\\Python\\python.exe", raising=False)
    result = startup_registration._command_line()
    assert result.startswith('"C:\\Python\\python.exe" "')
    assert result.endswith('main.py"')
