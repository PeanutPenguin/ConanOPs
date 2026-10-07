from __future__ import annotations

import os
import shutil
import socket

import pytest

import diagnostics
import models


def _make_installed_server(tmp_path, name="server"):
    """A server config pointing at a real (but empty of actual game
    binaries) install dir, with steamcmd 'installed' via a stub file,
    a fully-installed-per-manifest appmanifest, and a fake server exe
    -- i.e. everything a passing diagnostic run expects to find."""
    install_dir = tmp_path / name
    install_dir.mkdir()
    steamcmd_dir = tmp_path / f"{name}-steamcmd"
    steamcmd_dir.mkdir()
    (steamcmd_dir / "steamcmd.exe").write_text("stub")

    steamapps = install_dir / "steamapps"
    steamapps.mkdir()
    import steamcmd as steamcmd_mod
    manifest = steamapps / f"appmanifest_{steamcmd_mod.APP_ID}.acf"
    manifest.write_text('"AppState"\n{\n\t"StateFlags"\t\t"4"\n\t"buildid"\t\t"1"\n}\n')

    exe_dir = install_dir / "ConanSandbox" / "Binaries" / "Win64"
    exe_dir.mkdir(parents=True)
    exe_path = exe_dir / "ConanSandboxServer-Win64-Shipping.exe"
    exe_path.write_text("stub")

    # "Fully healthy" also means the server has actually run at least
    # once and created its world-save folder -- see the "World save"
    # diagnostic check, which is what backup failures trace back to.
    (install_dir / "ConanSandbox" / "Saved").mkdir(parents=True)

    server = models.ServerConfig(
        name=name,
        install_dir=str(install_dir),
        steamcmd_dir=str(steamcmd_dir),
        game_port=17777,
        query_port=17780,  # not game_port + 1 -- the server binds that one itself
        bind_ip="127.0.0.1",
    )
    return server


def _titles_with_status(results, status):
    return [r.title for r in results if r.status == status]


@pytest.fixture(autouse=True)
def _fast_network_defaults(monkeypatch):
    """Diagnostics now includes checks that hit netsh, UPnP SSDP
    discovery, and an external HTTP call for the public IP -- none of
    which exist, are fast, or are even network-reachable in this test
    environment. Defaults every test to a quick, deterministic "not
    applicable" shape so tests that aren't specifically about these
    checks stay fast and don't depend on real network access; tests
    that DO care about one of these override it with their own
    monkeypatch, same as any other fixture default."""
    import network_setup
    import proc_utils
    monkeypatch.setattr(proc_utils, "is_admin", lambda: False)
    monkeypatch.setattr(network_setup, "firewall_status", lambda name, gp, qp: [
        network_setup.FirewallResult(False, f"{label} port {port}")
        for label, port in (("Game", gp), ("Game+1", gp + 1), ("Query", qp))
    ])
    monkeypatch.setattr(network_setup, "discover_igd", lambda timeout=3.0, local_ip=None: None)
    monkeypatch.setattr(network_setup, "get_public_ip", lambda timeout=4.0: "203.0.113.1")
    # A discovered router reports every port forwarded to this PC, and a
    # public WAN address matching the public IP, unless a test says otherwise.
    monkeypatch.setattr(network_setup, "get_port_mapping",
                        lambda device, port, proto="UDP", timeout=5.0: {"NewInternalClient": ""})
    monkeypatch.setattr(network_setup, "get_external_ip", lambda device, timeout=5.0: "203.0.113.1")
    monkeypatch.setattr(network_setup, "firewall_environment",
                        lambda exe_path="": {"checked": True, "block_rules": [], "third_party": []})
    import windows_update
    monkeypatch.setattr(windows_update, "auto_sign_in_status", lambda: windows_update.AUTO_SIGN_IN_ON)


def test_missing_install_dir_reports_single_error_and_stops():
    server = models.ServerConfig(name="t", install_dir="")
    results = diagnostics.run_diagnostics(server)
    assert len(results) == 1
    assert results[0].status == diagnostics.STATUS_ERROR
    assert "install folder" in results[0].message.lower()


def test_install_dir_that_does_not_exist_reports_error_and_stops(tmp_path):
    server = models.ServerConfig(name="t", install_dir=str(tmp_path / "nonexistent"))
    results = diagnostics.run_diagnostics(server)
    assert len(results) == 1
    assert results[0].status == diagnostics.STATUS_ERROR


def test_missing_steamcmd_flagged_as_error(tmp_path):
    server = _make_installed_server(tmp_path)
    server.steamcmd_dir = str(tmp_path / "no-steamcmd-here")
    results = diagnostics.run_diagnostics(server)
    steamcmd_result = next(r for r in results if r.title == "SteamCMD")
    assert steamcmd_result.status == diagnostics.STATUS_ERROR


def test_missing_server_exe_flagged_as_error(tmp_path):
    server = _make_installed_server(tmp_path)
    exe_path = server.install_dir + "/ConanSandbox/Binaries/Win64/ConanSandboxServer-Win64-Shipping.exe"
    os.remove(exe_path)
    results = diagnostics.run_diagnostics(server)
    files_result = next(r for r in results if r.title == "Server files")
    assert files_result.status == diagnostics.STATUS_ERROR


def test_incomplete_install_state_flagged_as_warning(tmp_path):
    server = _make_installed_server(tmp_path)
    manifest = os.path.join(server.install_dir, "steamapps", f"appmanifest_{__import__('steamcmd').APP_ID}.acf")
    with open(manifest, "w") as f:
        f.write('"AppState"\n{\n\t"StateFlags"\t\t"2"\n\t"buildid"\t\t"1"\n}\n')
    results = diagnostics.run_diagnostics(server)
    files_result = next(r for r in results if r.title == "Server files")
    assert files_result.status == diagnostics.STATUS_WARNING


def test_same_game_and_query_port_is_an_error(tmp_path):
    server = _make_installed_server(tmp_path)
    server.query_port = server.game_port
    results = diagnostics.run_diagnostics(server)
    ports_result = next(r for r in results if r.title == "Ports")
    assert ports_result.status == diagnostics.STATUS_ERROR


def test_port_reserved_by_another_server_is_an_error(tmp_path):
    server = _make_installed_server(tmp_path)
    results = diagnostics.run_diagnostics(server, reserved_ports={server.game_port})
    game_port_result = next(r for r in results if r.title.startswith("Game port"))
    assert game_port_result.status == diagnostics.STATUS_ERROR


def test_port_in_use_by_something_else_is_a_warning(tmp_path):
    server = _make_installed_server(tmp_path)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", 0))
    held_port = s.getsockname()[1]
    server.game_port = held_port
    try:
        results = diagnostics.run_diagnostics(server)
        game_port_result = next(r for r in results if r.title.startswith("Game port"))
        assert game_port_result.status == diagnostics.STATUS_WARNING
    finally:
        s.close()


def test_free_ports_report_ok(tmp_path):
    server = _make_installed_server(tmp_path)
    results = diagnostics.run_diagnostics(server)
    game_port_result = next(r for r in results if r.title.startswith("Game port"))
    query_port_result = next(r for r in results if r.title.startswith("Query port"))
    assert game_port_result.status == diagnostics.STATUS_OK
    assert query_port_result.status == diagnostics.STATUS_OK


def test_stale_bind_ip_is_a_warning(tmp_path, monkeypatch):
    server = _make_installed_server(tmp_path)
    server.bind_ip = "10.99.99.99"
    monkeypatch.setattr("network_utils.list_local_ipv4s", lambda: {"192.168.1.5"})
    results = diagnostics.run_diagnostics(server)
    bind_ip_result = next(r for r in results if r.title == "Bind IP")
    assert bind_ip_result.status == diagnostics.STATUS_WARNING


def test_valid_bind_ip_reports_ok(tmp_path, monkeypatch):
    server = _make_installed_server(tmp_path)
    server.bind_ip = "192.168.1.5"
    monkeypatch.setattr("network_utils.list_local_ipv4s", lambda: {"192.168.1.5"})
    results = diagnostics.run_diagnostics(server)
    bind_ip_result = next(r for r in results if r.title == "Bind IP")
    assert bind_ip_result.status == diagnostics.STATUS_OK


def test_empty_bind_ip_is_a_warning(tmp_path):
    server = _make_installed_server(tmp_path)
    server.bind_ip = ""
    results = diagnostics.run_diagnostics(server)
    bind_ip_result = next(r for r in results if r.title == "Bind IP")
    assert bind_ip_result.status == diagnostics.STATUS_WARNING


def test_not_running_is_a_warning_not_an_error(tmp_path):
    """Server not running is common/expected (they haven't started it
    yet) -- shouldn't read as a hard failure."""
    server = _make_installed_server(tmp_path)
    results = diagnostics.run_diagnostics(server)
    running_result = next(r for r in results if r.title == "Running state")
    assert running_result.status == diagnostics.STATUS_WARNING


def test_backup_destination_not_configured_is_a_warning(tmp_path):
    server = _make_installed_server(tmp_path)
    server.backup_destination = ""
    results = diagnostics.run_diagnostics(server)
    backup_result = next(r for r in results if r.title == "Backup folder")
    assert backup_result.status == diagnostics.STATUS_WARNING


def test_backup_destination_missing_folder_is_ok_since_it_gets_created(tmp_path):
    server = _make_installed_server(tmp_path)
    server.backup_destination = str(tmp_path / "not-created-yet")
    results = diagnostics.run_diagnostics(server)
    backup_result = next(r for r in results if r.title == "Backup folder")
    assert backup_result.status == diagnostics.STATUS_OK
    # must be read-only -- diagnostics should never create it itself
    assert not os.path.isdir(server.backup_destination)


def test_backup_destination_existing_and_writable_is_ok(tmp_path):
    server = _make_installed_server(tmp_path)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    server.backup_destination = str(backup_dir)
    results = diagnostics.run_diagnostics(server)
    backup_result = next(r for r in results if r.title == "Backup folder")
    assert backup_result.status == diagnostics.STATUS_OK


def test_fully_healthy_server_has_no_errors(tmp_path, monkeypatch):
    import network_setup
    import proc_utils

    server = _make_installed_server(tmp_path)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    server.backup_destination = str(backup_dir)
    monkeypatch.setattr("network_utils.list_local_ipv4s", lambda: {"127.0.0.1"})
    monkeypatch.setattr(proc_utils, "is_admin", lambda: True)
    monkeypatch.setattr(network_setup, "firewall_status", lambda name, gp, qp: [
        network_setup.FirewallResult(True, f"{label} port {port}")
        for label, port in (("Game", gp), ("Game+1", gp + 1), ("Query", qp))
    ])
    monkeypatch.setattr(network_setup, "discover_igd", lambda timeout=3.0, local_ip=None: network_setup.UpnpDevice(
        control_url="http://192.168.1.1/control", service_type="urn:schemas-upnp-org:service:WANIPConnection:1",
    ))

    results = diagnostics.run_diagnostics(server)

    assert _titles_with_status(results, diagnostics.STATUS_ERROR) == []
    # "Running state" is expected to be a warning here since nothing is
    # actually launched in this test -- that's fine/normal, not an error.
    non_ok = _titles_with_status(results, diagnostics.STATUS_WARNING)
    assert non_ok == ["Running state"]


def test_diagnostics_never_writes_to_disk(tmp_path):
    """Read-only guarantee: running diagnostics shouldn't create,
    delete, or modify anything -- that's preflight's job, not this."""
    server = _make_installed_server(tmp_path)
    server.backup_destination = str(tmp_path / "should-not-be-created")
    before = set(os.listdir(tmp_path))

    diagnostics.run_diagnostics(server)

    after = set(os.listdir(tmp_path))
    assert before == after


def test_missing_world_save_folder_flagged_as_error(tmp_path, monkeypatch):
    """This is the check that was missing entirely before: a backup
    failing with "couldn't find the server's Saved folder" had no
    corresponding diagnostic, so there was no way to see why from
    the Diagnostics tab."""
    server = _make_installed_server(tmp_path)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    server.backup_destination = str(backup_dir)
    monkeypatch.setattr("network_utils.list_local_ipv4s", lambda: {"127.0.0.1"})
    shutil.rmtree(os.path.join(server.install_dir, "ConanSandbox", "Saved"))

    results = diagnostics.run_diagnostics(server)

    world_save = next(r for r in results if r.title == "World save")
    assert world_save.status == diagnostics.STATUS_ERROR
    assert server.install_dir in world_save.message


# ------------------------------------------------------- administrator rights --

def test_admin_check_ok_when_elevated(tmp_path, monkeypatch):
    import proc_utils
    server = _make_installed_server(tmp_path)
    monkeypatch.setattr("network_utils.list_local_ipv4s", lambda: {"127.0.0.1"})
    monkeypatch.setattr(proc_utils, "is_admin", lambda: True)

    results = diagnostics.run_diagnostics(server)

    admin_result = next(r for r in results if r.title == "Administrator rights")
    assert admin_result.status == diagnostics.STATUS_OK


def test_admin_check_warns_when_not_elevated(tmp_path, monkeypatch):
    import proc_utils
    server = _make_installed_server(tmp_path)
    monkeypatch.setattr("network_utils.list_local_ipv4s", lambda: {"127.0.0.1"})
    monkeypatch.setattr(proc_utils, "is_admin", lambda: False)

    results = diagnostics.run_diagnostics(server)

    admin_result = next(r for r in results if r.title == "Administrator rights")
    assert admin_result.status == diagnostics.STATUS_WARNING
    assert "permission prompt" in admin_result.message.lower()


# ------------------------------------------------------------- firewall rules --

def test_firewall_rules_reported_live_per_port(tmp_path, monkeypatch):
    """These must reflect the CURRENT state of Windows Firewall, not
    whether add_firewall_rules() reported success at setup time."""
    import network_setup
    server = _make_installed_server(tmp_path)
    monkeypatch.setattr("network_utils.list_local_ipv4s", lambda: {"127.0.0.1"})

    def fake_status(name, gp, qp):
        return [
            network_setup.FirewallResult(True, f"Game port {gp}"),
            network_setup.FirewallResult(False, f"Game+1 port {gp + 1}"),
            network_setup.FirewallResult(True, f"Query port {qp}"),
        ]
    monkeypatch.setattr(network_setup, "firewall_status", fake_status)

    results = diagnostics.run_diagnostics(server)

    fw_results = [r for r in results if r.title.startswith("Firewall rule:")]
    assert len(fw_results) == 3
    by_status = {r.status for r in fw_results}
    assert diagnostics.STATUS_OK in by_status
    assert diagnostics.STATUS_ERROR in by_status
    missing = next(r for r in fw_results if "Game+1" in r.title)
    assert missing.status == diagnostics.STATUS_ERROR
    assert "repair networking" in missing.message.lower()


# ----------------------------------------------------------------------- upnp --

def test_upnp_ok_when_router_discovered(tmp_path, monkeypatch):
    import network_setup
    server = _make_installed_server(tmp_path)
    monkeypatch.setattr("network_utils.list_local_ipv4s", lambda: {"127.0.0.1"})
    monkeypatch.setattr(network_setup, "discover_igd", lambda timeout=3.0, local_ip=None: network_setup.UpnpDevice(
        control_url="http://192.168.1.1/control", service_type="urn:schemas-upnp-org:service:WANIPConnection:1",
    ))

    results = diagnostics.run_diagnostics(server)

    upnp_result = next(r for r in results if r.title == "Router UPnP")
    assert upnp_result.status == diagnostics.STATUS_OK


def test_upnp_warns_with_manual_forwarding_instructions_when_unavailable(tmp_path, monkeypatch):
    import network_setup
    server = _make_installed_server(tmp_path)
    monkeypatch.setattr("network_utils.list_local_ipv4s", lambda: {"127.0.0.1"})
    monkeypatch.setattr(network_setup, "discover_igd", lambda timeout=3.0, local_ip=None: None)
    monkeypatch.setattr(network_setup, "get_public_ip", lambda timeout=4.0: "198.51.100.7")

    results = diagnostics.run_diagnostics(server)

    upnp_result = next(r for r in results if r.title == "Router UPnP")
    assert upnp_result.status == diagnostics.STATUS_WARNING
    assert "198.51.100.7" in upnp_result.message
    assert str(server.game_port) in upnp_result.message
    assert str(server.query_port) in upnp_result.message
    assert "manually" in upnp_result.message.lower()


def test_upnp_handles_undetectable_public_ip_gracefully(tmp_path, monkeypatch):
    import network_setup
    server = _make_installed_server(tmp_path)
    monkeypatch.setattr("network_utils.list_local_ipv4s", lambda: {"127.0.0.1"})
    monkeypatch.setattr(network_setup, "discover_igd", lambda timeout=3.0, local_ip=None: None)
    monkeypatch.setattr(network_setup, "get_public_ip", lambda timeout=4.0: None)

    results = diagnostics.run_diagnostics(server)  # must not raise

    upnp_result = next(r for r in results if r.title == "Router UPnP")
    assert upnp_result.status == diagnostics.STATUS_WARNING
    assert "couldn't detect" in upnp_result.message.lower()


# ------------------------------------------------------------------ RCON --

def test_rcon_not_checked_when_disabled(tmp_path, monkeypatch):
    server = _make_installed_server(tmp_path)
    server.rcon_enabled = False
    monkeypatch.setattr("network_utils.list_local_ipv4s", lambda: {"127.0.0.1"})

    results = diagnostics.run_diagnostics(server)

    assert not any(r.title == "RCON" for r in results)


def test_rcon_warns_when_enabled_but_server_not_running(tmp_path, monkeypatch):
    server = _make_installed_server(tmp_path)
    server.rcon_enabled = True
    monkeypatch.setattr("network_utils.list_local_ipv4s", lambda: {"127.0.0.1"})

    results = diagnostics.run_diagnostics(server)

    rcon_result = next(r for r in results if r.title == "RCON")
    assert rcon_result.status == diagnostics.STATUS_WARNING
    assert "isn't currently running" in rcon_result.message


def test_rcon_ok_when_connects_and_authenticates(tmp_path, monkeypatch):
    import process_manager
    import rcon
    server = _make_installed_server(tmp_path)
    server.rcon_enabled = True
    server.rcon_port = 25575
    monkeypatch.setattr("network_utils.list_local_ipv4s", lambda: {"127.0.0.1"})
    monkeypatch.setattr(process_manager, "find_running_pid", lambda install_dir: 4242)
    monkeypatch.setattr("network_utils.query_a2s_info", lambda ip, port, timeout=2.0: {"name": "s", "players": 0, "max_players": 10})

    class _FakeClient:
        def __init__(self, *a, **k):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
    monkeypatch.setattr(rcon, "RconClient", _FakeClient)

    results = diagnostics.run_diagnostics(server)

    rcon_result = next(r for r in results if r.title == "RCON")
    assert rcon_result.status == diagnostics.STATUS_OK


def test_rcon_error_distinguishes_wrong_password_from_unreachable(tmp_path, monkeypatch):
    import process_manager
    import rcon
    server = _make_installed_server(tmp_path)
    server.rcon_enabled = True
    monkeypatch.setattr("network_utils.list_local_ipv4s", lambda: {"127.0.0.1"})
    monkeypatch.setattr(process_manager, "find_running_pid", lambda install_dir: 4242)
    monkeypatch.setattr("network_utils.query_a2s_info", lambda ip, port, timeout=2.0: {"name": "s", "players": 0, "max_players": 10})

    class _FakeAuthFailClient:
        def __init__(self, *a, **k):
            pass
        def __enter__(self):
            raise rcon.RconAuthError("bad password")
        def __exit__(self, *exc):
            return False
    monkeypatch.setattr(rcon, "RconClient", _FakeAuthFailClient)

    results = diagnostics.run_diagnostics(server)

    rcon_result = next(r for r in results if r.title == "RCON")
    assert rcon_result.status == diagnostics.STATUS_ERROR
    assert "password" in rcon_result.message.lower()


def test_rcon_error_when_connection_fails_outright(tmp_path, monkeypatch):
    import process_manager
    import rcon
    server = _make_installed_server(tmp_path)
    server.rcon_enabled = True
    monkeypatch.setattr("network_utils.list_local_ipv4s", lambda: {"127.0.0.1"})
    monkeypatch.setattr(process_manager, "find_running_pid", lambda install_dir: 4242)
    monkeypatch.setattr("network_utils.query_a2s_info", lambda ip, port, timeout=2.0: {"name": "s", "players": 0, "max_players": 10})

    class _FakeUnreachableClient:
        def __init__(self, *a, **k):
            pass
        def __enter__(self):
            raise rcon.RconError("connection refused")
        def __exit__(self, *exc):
            return False
    monkeypatch.setattr(rcon, "RconClient", _FakeUnreachableClient)

    results = diagnostics.run_diagnostics(server)

    rcon_result = next(r for r in results if r.title == "RCON")
    assert rcon_result.status == diagnostics.STATUS_ERROR
    assert "connect" in rcon_result.message.lower()


# ------------------------------------------------------------ disk space --

def test_disk_space_ok_when_plenty_free(tmp_path, monkeypatch):
    import shutil as shutil_mod
    server = _make_installed_server(tmp_path)
    monkeypatch.setattr("network_utils.list_local_ipv4s", lambda: {"127.0.0.1"})
    fake_usage = shutil_mod._ntuple_diskusage(total=500 * 1024**3, used=100 * 1024**3, free=400 * 1024**3)
    monkeypatch.setattr(diagnostics.shutil, "disk_usage", lambda path: fake_usage)

    results = diagnostics.run_diagnostics(server)

    disk_results = [r for r in results if r.title == "Disk space"]
    assert disk_results
    assert all(r.status == diagnostics.STATUS_OK for r in disk_results)


def test_disk_space_error_when_critically_low(tmp_path, monkeypatch):
    import shutil as shutil_mod
    server = _make_installed_server(tmp_path)
    monkeypatch.setattr("network_utils.list_local_ipv4s", lambda: {"127.0.0.1"})
    fake_usage = shutil_mod._ntuple_diskusage(total=500 * 1024**3, used=499 * 1024**3, free=int(0.2 * 1024**3))
    monkeypatch.setattr(diagnostics.shutil, "disk_usage", lambda path: fake_usage)

    results = diagnostics.run_diagnostics(server)

    disk_result = next(r for r in results if r.title == "Disk space")
    assert disk_result.status == diagnostics.STATUS_ERROR


def test_disk_space_warning_when_getting_low(tmp_path, monkeypatch):
    import shutil as shutil_mod
    server = _make_installed_server(tmp_path)
    monkeypatch.setattr("network_utils.list_local_ipv4s", lambda: {"127.0.0.1"})
    fake_usage = shutil_mod._ntuple_diskusage(total=500 * 1024**3, used=497 * 1024**3, free=int(3.0 * 1024**3))
    monkeypatch.setattr(diagnostics.shutil, "disk_usage", lambda path: fake_usage)

    results = diagnostics.run_diagnostics(server)

    disk_result = next(r for r in results if r.title == "Disk space")
    assert disk_result.status == diagnostics.STATUS_WARNING


# ------------------------------------------------------------- permissions --

def test_install_folder_permissions_error_when_not_writable(tmp_path, monkeypatch):
    server = _make_installed_server(tmp_path)
    monkeypatch.setattr("network_utils.list_local_ipv4s", lambda: {"127.0.0.1"})
    monkeypatch.setattr(os, "access", lambda path, mode: str(path) != server.install_dir)

    results = diagnostics.run_diagnostics(server)

    perm_result = next(r for r in results if r.title == "Install folder permissions")
    assert perm_result.status == diagnostics.STATUS_ERROR


def test_backup_folder_writability_still_checked_independently(tmp_path, monkeypatch):
    """Existing behavior, unaffected by the install-folder check being
    added alongside it -- each folder gets its own verdict."""
    server = _make_installed_server(tmp_path)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    server.backup_destination = str(backup_dir)
    monkeypatch.setattr("network_utils.list_local_ipv4s", lambda: {"127.0.0.1"})
    monkeypatch.setattr(os, "access", lambda path, mode: str(path) != str(backup_dir))

    results = diagnostics.run_diagnostics(server)

    backup_result = next(r for r in results if r.title == "Backup folder")
    assert backup_result.status == diagnostics.STATUS_ERROR
    install_perm_result = next(r for r in results if r.title == "Install folder permissions")
    assert install_perm_result.status == diagnostics.STATUS_OK


def test_upnp_warns_when_router_found_but_ports_not_forwarded(tmp_path, monkeypatch):
    import network_setup
    server = _make_installed_server(tmp_path)
    monkeypatch.setattr("network_utils.list_local_ipv4s", lambda: {"127.0.0.1"})
    monkeypatch.setattr(network_setup, "discover_igd", lambda timeout=3.0, local_ip=None: network_setup.UpnpDevice(
        control_url="http://192.168.1.1/control", service_type="urn:schemas-upnp-org:service:WANIPConnection:1",
    ))
    monkeypatch.setattr(network_setup, "get_port_mapping", lambda device, port, proto="UDP", timeout=5.0: None)

    results = diagnostics.run_diagnostics(server)

    upnp_result = next(r for r in results if r.title == "Router UPnP")
    assert upnp_result.status == diagnostics.STATUS_WARNING
    assert str(server.game_port) in upnp_result.message


def test_double_nat_reported_when_router_wan_ip_is_private(tmp_path, monkeypatch):
    import network_setup
    server = _make_installed_server(tmp_path)
    monkeypatch.setattr("network_utils.list_local_ipv4s", lambda: {"127.0.0.1"})
    monkeypatch.setattr(network_setup, "discover_igd", lambda timeout=3.0, local_ip=None: network_setup.UpnpDevice(
        control_url="http://192.168.1.1/control", service_type="urn:schemas-upnp-org:service:WANIPConnection:1",
    ))
    monkeypatch.setattr(network_setup, "get_external_ip", lambda device, timeout=5.0: "100.72.1.9")

    results = diagnostics.run_diagnostics(server)

    nat = next(r for r in results if r.title == "Double NAT / CGNAT")
    assert nat.status == diagnostics.STATUS_ERROR


def test_firewall_unreadable_is_a_warning_not_an_error(tmp_path, monkeypatch):
    import network_setup
    server = _make_installed_server(tmp_path)
    monkeypatch.setattr("network_utils.list_local_ipv4s", lambda: {"127.0.0.1"})
    monkeypatch.setattr(network_setup, "firewall_status", lambda sid, gp, qp: [
        network_setup.FirewallResult(False, f"{label} port {port}", checked=False)
        for label, port in (("Game", gp), ("Game+1", gp + 1), ("Query", qp))
    ])

    results = diagnostics.run_diagnostics(server)

    fw = [r for r in results if r.title.startswith("Firewall rule:")]
    assert fw and all(r.status == diagnostics.STATUS_WARNING for r in fw)


def test_query_port_right_above_game_port_is_an_error(tmp_path, monkeypatch):
    server = _make_installed_server(tmp_path)
    server.game_port = 7777
    server.query_port = 7778
    monkeypatch.setattr("network_utils.list_local_ipv4s", lambda: {"127.0.0.1"})

    results = diagnostics.run_diagnostics(server)

    ports = next(r for r in results if r.title == "Ports")
    assert ports.status == diagnostics.STATUS_ERROR


def test_exe_block_rule_is_an_error_and_third_party_firewall_a_warning(tmp_path, monkeypatch):
    import network_setup
    server = _make_installed_server(tmp_path)
    monkeypatch.setattr("network_utils.list_local_ipv4s", lambda: {"127.0.0.1"})
    monkeypatch.setattr(network_setup, "firewall_environment", lambda exe_path="": {
        "checked": True, "block_rules": ["conansandboxserver-win64-shipping.exe"], "third_party": ["Norton Firewall"],
    })
    results = diagnostics.run_diagnostics(server)
    blocked = next(r for r in results if r.title == "Firewall: server program blocked")
    assert blocked.status == diagnostics.STATUS_ERROR
    third = next(r for r in results if r.title == "Third-party firewall")
    assert third.status == diagnostics.STATUS_WARNING and "Norton Firewall" in third.message


@pytest.mark.parametrize("state,expected", [("off", "warning"), ("blocked", "warning"), ("unknown", "warning"), ("on", "ok")])
def test_auto_sign_in_row(tmp_path, monkeypatch, state, expected):
    import windows_update
    server = _make_installed_server(tmp_path)
    monkeypatch.setattr("network_utils.list_local_ipv4s", lambda: {"127.0.0.1"})
    monkeypatch.setattr(windows_update, "auto_sign_in_status", lambda: state)
    r = next(x for x in diagnostics.run_diagnostics(server) if x.title == diagnostics.AUTO_SIGN_IN_TITLE)
    assert r.status == expected
