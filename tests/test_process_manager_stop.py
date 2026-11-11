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


def test_stop_reports_failure_if_kill_also_times_out(monkeypatch):
    """2.0: a server that's still there must not be reported as stopped --
    a restore or update would then work on a running server's files."""
    fake = _FakeProc(terminate_hangs=True, kill_hangs=True)
    fake.is_running = lambda: True
    monkeypatch.setattr(process_manager, "find_running_pid", lambda install_dir: 4242)
    monkeypatch.setattr(psutil, "Process", lambda pid: fake)

    result = process_manager.stop("/tmp/fake", timeout=5.0)

    assert result is False
    assert len(fake.wait_calls) == 2


def test_stop_reports_failure_when_windows_refuses(monkeypatch):
    """A server started with admin rights can't be ended by a ConanOps
    running without them."""
    fake = _FakeProc()

    def refuse():
        raise psutil.AccessDenied(4242)
    fake.terminate = refuse
    fake.is_running = lambda: True
    monkeypatch.setattr(process_manager, "find_running_pid", lambda install_dir: 4242)
    monkeypatch.setattr(psutil, "Process", lambda pid: fake)
    assert process_manager.stop("/tmp/fake", timeout=5.0) is False


def test_restart_refuses_to_launch_a_second_copy(monkeypatch):
    import models
    import pytest
    launched = []
    monkeypatch.setattr(process_manager, "graceful_stop", lambda s: False)
    monkeypatch.setattr(process_manager, "launch", lambda s: launched.append(s))
    with pytest.raises(process_manager.StillRunningError):
        process_manager.restart(models.ServerConfig(name="x", install_dir="/tmp/fake"))
    assert launched == []


def test_launch_does_nothing_when_already_running(tmp_path, monkeypatch):
    import models
    import subprocess
    root = tmp_path / "S"
    exe = process_manager.server_exe_path(str(root))
    import os
    os.makedirs(os.path.dirname(exe))
    open(exe, "w").close()
    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: started.append(a))
    monkeypatch.setattr(process_manager, "find_running_pid", lambda d: 4242)
    assert process_manager.launch(models.ServerConfig(name="x", install_dir=str(root))) is None
    assert started == []


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


def test_name_and_password_kept_in_step_under_both_sections(tmp_path):
    import models
    import os
    root = tmp_path / "S"
    eng = process_manager.engine_ini_path(str(root))
    os.makedirs(os.path.dirname(eng))
    with open(eng, "w") as f:
        f.write("[OnlineSubsystem]\nServerName=Old\nServerPassword=old\n")
    s = models.ServerConfig(name="Chudville", install_dir=str(root))
    s.password = "new"
    process_manager.sync_server_ini(s)
    text = open(eng).read()
    assert "ServerName=Old" not in text and "ServerPassword=old" not in text
    assert text.count("ServerName=Chudville") == 2


def test_other_section_is_never_added(tmp_path):
    import models
    import os
    root = tmp_path / "S"
    eng = process_manager.engine_ini_path(str(root))
    os.makedirs(os.path.dirname(eng))
    open(eng, "w").write("[URL]\nPort=7777\n")
    process_manager.sync_server_ini(models.ServerConfig(name="X", install_dir=str(root)))
    assert "[OnlineSubsystem]" not in open(eng).read()
