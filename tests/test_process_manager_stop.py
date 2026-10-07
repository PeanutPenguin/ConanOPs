from __future__ import annotations

import psutil

import process_manager


class _FakeProc:
    def __init__(self, terminate_hangs=False, kill_hangs=False):
        self.terminate_hangs = terminate_hangs
        self.kill_hangs = kill_hangs
        self.terminated = False
        self.killed = False
        self.wait_calls = []

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.wait_calls[0] is timeout and len(self.wait_calls) == 1 and self.terminate_hangs:
            raise psutil.TimeoutExpired(timeout)
        if len(self.wait_calls) == 2 and self.kill_hangs:
            raise psutil.TimeoutExpired(timeout)
        return None


def test_stop_returns_true_immediately_when_nothing_is_running(monkeypatch):
    monkeypatch.setattr(process_manager, "find_running_pid", lambda install_dir: None)
    assert process_manager.stop("/tmp/fake") is True


def test_stop_waits_after_kill_when_terminate_times_out(monkeypatch):
    """The actual fix: after falling back to kill(), this must
    confirm the process has actually exited (wait()) instead of
    returning the instant kill() is issued -- launching a replacement
    before the old process (and its ports) are truly gone is exactly
    the kind of race that made a fine mod look broken or a broken one
    look fine, depending on timing."""
    fake = _FakeProc(terminate_hangs=True)
    monkeypatch.setattr(process_manager, "find_running_pid", lambda install_dir: 4242)
    monkeypatch.setattr(psutil, "Process", lambda pid: fake)

    result = process_manager.stop("/tmp/fake", timeout=5.0)

    assert result is True
    assert fake.terminated is True
    assert fake.killed is True
    assert len(fake.wait_calls) == 2  # once after terminate(), once after kill()


def test_stop_still_returns_true_if_kill_also_times_out(monkeypatch):
    """Nothing more this function can do at that point, but it must
    not raise -- the caller's own backstop (auto_bisect_runner.py's
    _wait_for_process_exit) is what takes over from here."""
    fake = _FakeProc(terminate_hangs=True, kill_hangs=True)
    monkeypatch.setattr(process_manager, "find_running_pid", lambda install_dir: 4242)
    monkeypatch.setattr(psutil, "Process", lambda pid: fake)

    result = process_manager.stop("/tmp/fake", timeout=5.0)

    assert result is True
    assert len(fake.wait_calls) == 2


def test_stop_returns_true_when_terminate_succeeds_without_kill(monkeypatch):
    fake = _FakeProc(terminate_hangs=False)
    monkeypatch.setattr(process_manager, "find_running_pid", lambda install_dir: 4242)
    monkeypatch.setattr(psutil, "Process", lambda pid: fake)

    result = process_manager.stop("/tmp/fake", timeout=5.0)

    assert result is True
    assert fake.killed is False
    assert len(fake.wait_calls) == 1


def test_stop_handles_process_already_gone_during_kill(monkeypatch):
    class _VanishingProc(_FakeProc):
        def kill(self):
            raise psutil.NoSuchProcess(pid=4242)

    fake = _VanishingProc(terminate_hangs=True)
    monkeypatch.setattr(process_manager, "find_running_pid", lambda install_dir: 4242)
    monkeypatch.setattr(psutil, "Process", lambda pid: fake)

    assert process_manager.stop("/tmp/fake", timeout=5.0) is True
