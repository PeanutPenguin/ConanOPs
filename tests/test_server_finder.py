"""Finding Conan servers already on this PC and taking them over as-is."""
import os

import models
import process_manager
import server_finder


def _make_install(root, name="Chudville", port=7779, query=27017):
    exe = process_manager.server_exe_path(str(root))
    os.makedirs(os.path.dirname(exe))
    open(exe, "w").close()
    cfg = os.path.join(str(root), "ConanSandbox", "Saved", "Config", "WindowsServer")
    os.makedirs(cfg)
    with open(os.path.join(cfg, "Engine.ini"), "w") as f:
        f.write(f"[OnlineSubsystem]\nServerName={name}\nServerPassword=hunter2\n[URL]\nPort={port}\n"
                f"[OnlineSubsystemSteam]\nGameServerQueryPort={query}\n")
    with open(os.path.join(cfg, "Game.ini"), "w") as f:
        f.write("[RconPlugin]\nRconEnabled=True\nRconPassword=abc\nRconPort=25580\n"
                "[/Script/Engine.GameSession]\nMaxPlayers=30\n")
    with open(os.path.join(cfg, "ServerSettings.ini"), "w") as f:
        f.write("[ServerSettings]\nPlayerXPRateMultiplier=3.5\nPVPEnabled=False\nMaxNudity=1\n")
    mods = os.path.join(str(root), "ConanSandbox", "Mods")
    os.makedirs(mods)
    with open(os.path.join(mods, "modlist.txt"), "w") as f:
        f.write("C:\\steamcmd\\steamapps\\workshop\\content\\440900\\880454836\\Pippi.pak\n")


def test_reads_an_existing_install_without_changing_anything(tmp_path):
    root = tmp_path / "ConanServer"
    _make_install(root)
    f = server_finder.read_server(str(root), ["x.exe", "-log", "-MULTIHOME=192.168.1.5"])
    assert f.name == "Chudville" and f.running
    assert f.values == {"game_port": 7779, "query_port": 27017, "max_players": 30, "rcon_port": 25580,
                        "password": "hunter2", "rcon_enabled": True, "rcon_password": "abc", "bind_ip": "192.168.1.5"}
    assert f.gameplay["PlayerXPRateMultiplier"] == 3.5 and f.gameplay["PVPEnabled"] is False
    assert f.gameplay["MaxNudity"] == 1
    assert f.mods == [{"id": "880454836", "name": "Pippi", "enabled": True}]


def test_finds_unmanaged_and_skips_managed_and_dismissed(tmp_path, monkeypatch):
    a, b, c = tmp_path / "ConanA", tmp_path / "ConanB", tmp_path / "ConanC"
    for r in (a, b, c):
        _make_install(r)
    (tmp_path / "ConanEmpty").mkdir()
    monkeypatch.setattr(server_finder, "_running", lambda: {})
    monkeypatch.setattr(server_finder, "_steam_libraries", lambda: [])
    monkeypatch.setattr(server_finder, "_fixed_drives", lambda: [])
    found = server_finder.find_unmanaged([str(a)], [str(b)], extra_roots=[str(tmp_path)])
    assert [f.install_dir for f in found] == [str(c)]


def test_ignored_dirs_persist(tmp_path):
    c = models.AppConfig()
    c.ignored_server_dirs = ["D:\\Conan"]
    path = str(tmp_path / "c.json")
    c.save(path)
    assert models.AppConfig.load(path).ignored_server_dirs == ["D:\\Conan"]


def test_adopt_keeps_settings(tmp_path, monkeypatch):
    import sys
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(sys.argv)
    import mod_manager
    import steamcmd
    from ui.main_window import MainWindow
    monkeypatch.setattr(mod_manager, "write_modlist", lambda *a, **k: None)
    monkeypatch.setattr(steamcmd, "is_steamcmd_installed", lambda d: True)
    root = tmp_path / "ConanServer"
    _make_install(root)
    cfg = models.AppConfig()
    monkeypatch.setattr(cfg, "save", lambda *a, **k: None)
    win = MainWindow(config=cfg)
    monkeypatch.setattr(win, "_start_network_reconcile", lambda *a, **k: None)
    try:
        win._adopt_server(server_finder.read_server(str(root)))
        s = cfg.get_active()
        assert s.install_dir == str(root) and s.name == "Chudville" and s.game_port == 7779
        assert s.gameplay["PlayerXPRateMultiplier"] == 3.5 and s.mods[0]["id"] == "880454836"
        assert not s.desired_running
    finally:
        win.close()
