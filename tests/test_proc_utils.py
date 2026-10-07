from __future__ import annotations

import subprocess

import proc_utils


def test_hidden_window_kwargs_is_noop_off_windows(monkeypatch):
    monkeypatch.setattr(proc_utils.sys, "platform", "linux")
    assert proc_utils.hidden_window_kwargs() == {}


def test_hidden_window_kwargs_sets_create_no_window_on_windows(monkeypatch):
    """CREATE_NO_WINDOW is a Windows-only subprocess constant that
    doesn't exist on other platforms, so this only imports/references
    it once we've confirmed we're pretending to be on win32."""
    monkeypatch.setattr(proc_utils.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)

    kwargs = proc_utils.hidden_window_kwargs()

    assert kwargs == {"creationflags": 0x08000000}


def test_hidden_console_kwargs_is_noop_off_windows(monkeypatch):
    monkeypatch.setattr(proc_utils.sys, "platform", "linux")
    assert proc_utils.hidden_console_kwargs() == {}


def test_steamcmd_subprocess_calls_use_a_hidden_real_console(monkeypatch):
    """SteamCMD must get a real-but-hidden console (CREATE_NEW_CONSOLE +
    SW_HIDE), not CREATE_NO_WINDOW's no-console-at-all: the same
    app_update that failed with "Missing configuration" from ConanOps
    succeeded from a normal console. Still no visible window either way."""
    import steamcmd

    seen_kwargs = []
    fake = {"creationflags": 0x10, "startupinfo": "hidden"}
    monkeypatch.setattr(steamcmd, "hidden_console_kwargs", lambda: dict(fake))

    def fake_run(args, **kwargs):
        seen_kwargs.append(kwargs)
        return subprocess.CompletedProcess(args, 0, "", "")

    def fake_popen(args, **kwargs):
        seen_kwargs.append(kwargs)
        class _P:
            returncode = 0
            stdout = iter(["Success.\n"])
            def wait(self, timeout=None):
                return 0
        return _P()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(steamcmd, "steamcmd_exe_path", lambda d: "C:\\fake\\steamcmd.exe")
    monkeypatch.setattr(steamcmd.os.path, "exists", lambda p: True)

    steamcmd.get_latest_buildid("C:\\fake")
    steamcmd.download_workshop_items("C:\\fake", ["12345"])
    steamcmd._run_cancelable(["C:\\fake\\steamcmd.exe", "+quit"], timeout=10)

    assert len(seen_kwargs) >= 3
    for kwargs in seen_kwargs:
        assert kwargs.get("creationflags") == 0x10
        assert kwargs.get("startupinfo") == "hidden"
        assert kwargs.get("stdin") == subprocess.DEVNULL


def test_network_setup_subprocess_calls_pass_hidden_window_kwargs(monkeypatch):
    """Same reasoning as steamcmd above, for the PowerShell calls used
    to set up/tear down/check Windows Firewall rules."""
    import network_setup

    monkeypatch.setattr(proc_utils.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    # Already elevated -- take the direct path rather than the UAC one.
    monkeypatch.setattr(proc_utils, "is_admin", lambda: True)

    seen_kwargs = []

    def fake_run(args, **kwargs):
        seen_kwargs.append(kwargs)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    network_setup.add_firewall_rules("ab12cd34", 7777, 27015)
    network_setup.remove_firewall_rules("ab12cd34")
    network_setup.firewall_status("ab12cd34", 7777, 27015)

    assert len(seen_kwargs) >= 3
    for kwargs in seen_kwargs:
        assert kwargs.get("creationflags") == 0x08000000


def test_hidden_gui_window_kwargs_is_noop_off_windows(monkeypatch):
    monkeypatch.setattr(proc_utils.sys, "platform", "linux")
    assert proc_utils.hidden_gui_window_kwargs() == {}


def test_hidden_gui_window_kwargs_requests_sw_hide_on_windows(monkeypatch):
    monkeypatch.setattr(proc_utils.sys, "platform", "win32")

    class FakeStartupInfo:
        def __init__(self):
            self.dwFlags = 0
            self.wShowWindow = None

    monkeypatch.setattr(subprocess, "STARTUPINFO", FakeStartupInfo, raising=False)
    monkeypatch.setattr(subprocess, "STARTF_USESHOWWINDOW", 0x1, raising=False)

    kwargs = proc_utils.hidden_gui_window_kwargs()

    assert kwargs["startupinfo"].dwFlags & 0x1
    assert kwargs["startupinfo"].wShowWindow == 0
    assert "creationflags" not in kwargs  # unlike hidden_console_kwargs -- no console requested at all


def test_process_manager_launch_passes_hidden_gui_window_kwargs(monkeypatch, tmp_path):
    """The Conan server's own window popping up on Start is exactly
    this: launch() must hand Popen the SW_HIDE startup info, the same
    best-effort suppression as hidden_gui_window_kwargs() builds."""
    import process_manager
    from models import ServerConfig

    exe_dir = tmp_path / "ConanSandbox" / "Binaries" / "Win64"
    exe_dir.mkdir(parents=True)
    exe_path = exe_dir / "ConanSandboxServer-Win64-Shipping.exe"
    exe_path.write_text("fake")

    fake_kwargs = {"startupinfo": "hidden"}
    monkeypatch.setattr(process_manager, "hidden_gui_window_kwargs", lambda: dict(fake_kwargs))

    seen_kwargs = []

    def fake_popen(args, **kwargs):
        seen_kwargs.append(kwargs)
        return object()

    monkeypatch.setattr(process_manager.subprocess, "Popen", fake_popen)

    server = ServerConfig(id="s1", name="Test", install_dir=str(tmp_path))
    process_manager.launch(server)

    assert len(seen_kwargs) == 1
    assert seen_kwargs[0].get("startupinfo") == "hidden"


def test_open_in_explorer_raises_file_not_found_for_missing_path(tmp_path):
    missing = tmp_path / "does-not-exist"
    try:
        proc_utils.open_in_explorer(str(missing))
        assert False, "expected FileNotFoundError"
    except FileNotFoundError:
        pass


def test_open_in_explorer_calls_startfile_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(proc_utils.sys, "platform", "win32")
    calls = []
    monkeypatch.setattr(proc_utils.os, "startfile", lambda p: calls.append(p), raising=False)

    proc_utils.open_in_explorer(str(tmp_path))

    assert calls == [str(tmp_path)]


def test_open_in_explorer_is_a_noop_off_windows(monkeypatch, tmp_path):
    """No os.startfile() exists off Windows at all -- confirms this
    doesn't try to call it (which would AttributeError) when this app
    somehow runs somewhere else."""
    monkeypatch.setattr(proc_utils.sys, "platform", "linux")

    proc_utils.open_in_explorer(str(tmp_path))  # should not raise


def test_child_env_makes_relaunched_onefile_builds_unpack_their_own_copy(monkeypatch, tmp_path):
    import os
    mei = str(tmp_path / "_MEI12345")
    monkeypatch.setattr(proc_utils.sys, "_MEIPASS", mei, raising=False)
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", mei)
    monkeypatch.setenv("_MEIPASS2", mei)
    monkeypatch.setenv("PATH", os.pathsep.join([mei, "/usr/bin"]))
    env = proc_utils.child_env()
    assert env["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert "_PYI_APPLICATION_HOME_DIR" not in env and "_MEIPASS2" not in env
    assert env["PATH"] == "/usr/bin"
