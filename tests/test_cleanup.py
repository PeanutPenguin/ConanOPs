from __future__ import annotations

import os

import cleanup
import models


def test_never_deletes_home_or_drive_roots(tmp_path):
    home = os.path.expanduser("~")
    for p in (home, os.path.join(home, "Downloads"), os.path.join(home, "Documents"), os.path.abspath(os.sep),
              os.path.dirname(home)):
        assert cleanup.is_safe_to_delete(p) is False, p
    assert cleanup.is_safe_to_delete(str(tmp_path / "x")) is True


def test_remove_path_refuses_protected(tmp_path):
    assert "won't delete" in cleanup.remove_path(os.path.expanduser("~"))


def test_remove_path_handles_read_only_files(tmp_path):
    d = tmp_path / "ro"
    d.mkdir()
    f = d / "file.txt"
    f.write_text("x")
    os.chmod(f, 0o444)
    assert cleanup.remove_path(str(d)) is None and not d.exists()


def test_backups_in_a_shared_folder_only_lose_conanops_zips(tmp_path):
    folder = tmp_path / "MyStuff"
    folder.mkdir()
    (folder / "20261005-040000_scheduled.zip").write_bytes(b"x")
    (folder / "taxes.pdf").write_bytes(b"x")
    cleanup.remove_backups(str(folder), owned=False)
    assert (folder / "taxes.pdf").exists() and not (folder / "20261005-040000_scheduled.zip").exists()


def test_server_paths_includes_per_server_folder_but_not_shared_steamcmd(tmp_path, monkeypatch):
    monkeypatch.setattr(cleanup, "owned_roots", lambda: [str(tmp_path / "data")])
    cfg = models.AppConfig()
    a = cfg.add_server("A")
    b = cfg.add_server("B")
    a.install_dir = str(tmp_path / "data" / a.id / "server")
    a.steamcmd_dir = str(tmp_path / "data" / a.id / "steamcmd")
    b.install_dir = str(tmp_path / "data" / b.id / "server")
    b.steamcmd_dir = a.steamcmd_dir
    plan = cleanup.server_paths(a, [b])
    assert a.install_dir in plan["files"]
    assert a.steamcmd_dir not in plan["files"]
    assert os.path.dirname(a.install_dir) not in plan["files"]  # B's SteamCMD lives inside it
    plan_b = cleanup.server_paths(b, [])
    assert a.steamcmd_dir in plan_b["files"]


def test_cleanup_script_covers_every_windows_change():
    s = cleanup.cleanup_script(["ab12"], ["Chud"], [r"C:\ConanOps\data\ab12\server"], remove_task=True,
                               restore_active_hours={"ActiveHoursStart": None, "ActiveHoursEnd": 17,
                                                     "SmartActiveHoursState": 1},
                               uninstall_vcredist=True)
    assert "Remove-NetFirewallRule" in s and "Get-NetFirewallApplicationFilter" in s
    assert "Unregister-ScheduledTask" in s
    assert "Remove-ItemProperty -Path $k -Name ActiveHoursStart" in s
    assert "ActiveHoursEnd -Value 17" in s
    assert "Redistributable" in s
    assert s.rstrip().endswith("exit 0")


def test_generic_data_folder_only_owned_if_it_holds_conanops_files(tmp_path, monkeypatch):
    import conanops_paths
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(conanops_paths, "no_space_root", lambda: str(data))
    monkeypatch.setattr(conanops_paths, "public_fallback_root", lambda: str(tmp_path / "nope"))
    assert str(data) not in cleanup.owned_roots()
    (data / "config.json").write_text("{}")
    assert str(data) in cleanup.owned_roots()
