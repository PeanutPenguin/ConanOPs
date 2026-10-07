from __future__ import annotations

import sys

import pytest

import vcredist
import powershell


def _on_windows(monkeypatch):
    monkeypatch.setattr(vcredist.sys, "platform", "win32")


def test_status_ok_from_registry(monkeypatch):
    _on_windows(monkeypatch)
    monkeypatch.setattr(vcredist, "_registry_version", lambda: (14, 44, 35211, 0))
    st = vcredist.status()
    assert st.state == vcredist.STATUS_OK and st.version_text == "14.44.35211.0"


def test_status_outdated(monkeypatch):
    _on_windows(monkeypatch)
    monkeypatch.setattr(vcredist, "_registry_version", lambda: (14, 29, 30133, 0))
    assert vcredist.status().state == vcredist.STATUS_OUTDATED
    assert vcredist.needs_install()


def test_status_falls_back_to_dlls(monkeypatch):
    _on_windows(monkeypatch)
    monkeypatch.setattr(vcredist, "_registry_version", lambda: None)
    monkeypatch.setattr(vcredist, "_dlls_present", lambda: True)
    assert vcredist.status().state == vcredist.STATUS_OK
    monkeypatch.setattr(vcredist, "_dlls_present", lambda: False)
    assert vcredist.status().state == vcredist.STATUS_MISSING


def test_status_unknown_off_windows(monkeypatch):
    monkeypatch.setattr(vcredist.sys, "platform", "linux")
    assert vcredist.status().state == vcredist.STATUS_UNKNOWN
    assert not vcredist.needs_install()


def test_install_script_verifies_microsoft_signature_from_an_admin_only_copy():
    s = vcredist._install_script(r"C:\Users\Bob's PC\AppData\Local\Temp\x\vc_redist.x64.exe")
    assert "Get-AuthenticodeSignature" in s and "O=Microsoft Corporation" in s
    assert "$env:SystemRoot" in s and "Copy-Item" in s
    assert s.index("Get-AuthenticodeSignature") < s.index("Start-Process")
    assert "'/install','/quiet','/norestart'" in s
    assert "Bob''s PC" in s  # path safely quoted


def test_install_flow(monkeypatch, tmp_path):
    _on_windows(monkeypatch)
    fake = tmp_path / "dl" / "vc_redist.x64.exe"
    fake.parent.mkdir()
    fake.write_bytes(b"x")
    monkeypatch.setattr(vcredist, "_download", lambda progress: str(fake))
    monkeypatch.setattr(powershell, "run_privileged", lambda script, timeout=90.0: powershell.RUN_OK)
    monkeypatch.setattr(vcredist, "status", lambda: vcredist.RuntimeStatus(vcredist.STATUS_OK, (14, 44)))
    assert vcredist.install() == vcredist.INSTALL_OK
    assert not fake.parent.exists()  # downloaded copy cleaned up


def test_install_declined(monkeypatch, tmp_path):
    _on_windows(monkeypatch)
    fake = tmp_path / "dl" / "vc_redist.x64.exe"
    fake.parent.mkdir()
    fake.write_bytes(b"x")
    monkeypatch.setattr(vcredist, "_download", lambda progress: str(fake))
    monkeypatch.setattr(powershell, "run_privileged", lambda script, timeout=90.0: powershell.RUN_DECLINED)
    assert vcredist.install() == vcredist.INSTALL_DECLINED


def test_install_download_failure(monkeypatch):
    _on_windows(monkeypatch)
    monkeypatch.setattr(vcredist, "_download", lambda progress: None)
    assert vcredist.install() == vcredist.INSTALL_DOWNLOAD_FAILED


def test_preflight_blocks_launch_only_when_missing(monkeypatch, tmp_path):
    import models
    import preflight
    import process_manager
    import steamcmd
    monkeypatch.setattr(steamcmd, "is_steamcmd_installed", lambda d: True)
    monkeypatch.setattr(steamcmd, "get_install_state", lambda d: 4)
    monkeypatch.setattr(process_manager, "server_exe_path", lambda d: __file__)
    server = models.ServerConfig(install_dir=str(tmp_path), steamcmd_dir=str(tmp_path), bind_ip="")

    monkeypatch.setattr(vcredist, "status", lambda: vcredist.RuntimeStatus(vcredist.STATUS_MISSING))
    r = preflight.run_preflight(server)
    assert any("Visual C++" in p for p in r.problems)

    monkeypatch.setattr(vcredist, "status", lambda: vcredist.RuntimeStatus(vcredist.STATUS_OUTDATED, (14, 29)))
    r = preflight.run_preflight(server)
    assert not any("Visual C++" in p for p in r.problems)


@pytest.mark.parametrize("state,expected", [
    (vcredist.STATUS_MISSING, "error"), (vcredist.STATUS_OUTDATED, "warning"), (vcredist.STATUS_OK, "ok"),
])
def test_diagnostics_reports_runtime(monkeypatch, tmp_path, state, expected):
    import diagnostics
    from tests.test_diagnostics import _make_installed_server
    import network_setup
    monkeypatch.setattr(network_setup, "firewall_status", lambda sid, gp, qp: [])
    monkeypatch.setattr(network_setup, "firewall_environment", lambda exe_path="": {"checked": False})
    monkeypatch.setattr(network_setup, "discover_igd", lambda timeout=3.0, local_ip=None: None)
    monkeypatch.setattr(network_setup, "get_public_ip", lambda timeout=4.0: None)
    monkeypatch.setattr(vcredist, "status", lambda: vcredist.RuntimeStatus(state, (14, 29) if state == "outdated" else None))
    server = _make_installed_server(tmp_path)
    r = next(x for x in diagnostics.run_diagnostics(server) if x.title == diagnostics.VC_RUNTIME_TITLE)
    assert r.status == expected


def test_wizard_install_worker_installs_runtime_after_download(monkeypatch, tmp_path):
    import steamcmd as steamcmd_mod
    from ui.setup_wizard import _InstallWorker
    monkeypatch.setattr(steamcmd_mod, "install_steamcmd", lambda *a, **k: True)
    monkeypatch.setattr(steamcmd_mod, "update_server",
                        lambda *a, **k: steamcmd_mod.UpdateResult(True, "ok"))
    monkeypatch.setattr(vcredist, "needs_install", lambda: True)
    calls = []
    monkeypatch.setattr(vcredist, "install", lambda progress=None: calls.append(1) or vcredist.INSTALL_DECLINED)
    w = _InstallWorker(str(tmp_path / "sc"), str(tmp_path / "srv"))
    results = []
    w.finished_ok.connect(results.append)
    w.run()
    assert calls == [1]
    assert results == [True]  # a declined runtime install never fails setup
