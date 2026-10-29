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
    assert f.mods == [{"id": "880454836", "name": "Pippi", "enabled": True,
                       "pak": "C:\\steamcmd\\steamapps\\workshop\\content\\440900\\880454836\\Pippi.pak"}]


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


# ------------------------------------------------- complete takeover (1.0.11) --

def test_reads_name_and_password_from_the_section_conan_uses(tmp_path):
    root = tmp_path / "S"
    _make_install(root)
    eng = os.path.join(str(root), "ConanSandbox", "Saved", "Config", "WindowsServer", "Engine.ini")
    with open(eng, "a") as f:
        f.write("[OnlineSubsystemSteam]\nServerName=Real Name\nServerPassword=steam-pw\n"
                "[URL]\nMultiHome=10.0.0.5\n")
    found = server_finder.read_server(str(root))
    assert found.name == "Real Name" and found.values["password"] == "steam-pw"
    assert found.values["bind_ip"] == "10.0.0.5"  # from Engine.ini when it isn't running


def test_keeps_launch_options_and_refuses_moved_save_folders():
    extra, blocker = server_finder.split_args(
        ["C:\\S\\x.exe", "ConanSandbox?listen", "-log", "-Port=7777", "-MULTIHOME=1.2.3.4", "-nosteam", "-usecheats"])
    assert extra == ["ConanSandbox?listen", "-nosteam", "-usecheats"] and not blocker
    _extra, blocker = server_finder.split_args(["x.exe", "-usedir=D:\\Saves"])
    assert "another folder" in blocker


def test_launch_args_keep_the_old_options_in_the_right_places():
    s = models.ServerConfig(install_dir="C:\\S", game_port=7779, query_port=27017, max_players=10, bind_ip="1.2.3.4")
    s.extra_launch_args = ["ConanSandbox?listen", "-nosteam", "-Port=1", "-log"]
    args = process_manager.build_launch_args(s)
    assert args[1] == "ConanSandbox?listen"  # a map/URL argument goes straight after the exe
    assert args[-1] == "-nosteam" and args.count("-log") == 1 and "-Port=1" not in args


def test_local_mods_and_other_paks_survive_rewrites(tmp_path):
    import mod_manager
    root = tmp_path / "S"
    _make_install(root)
    other_pak = tmp_path / "steam" / "steamapps" / "workshop" / "content" / "440900" / "222" / "Two.pak"
    other_pak.parent.mkdir(parents=True)
    other_pak.write_text("x")
    (root / "ConanSandbox" / "Mods" / "MyLocal.pak").write_text("x")
    with open(mod_manager.modlist_path(str(root)), "w") as f:
        f.write(f"MyLocal.pak\n*{other_pak}\nFirstAfter.pak\n")
    found = server_finder.read_server(str(root))
    assert [m["id"] for m in found.mods] == ["222"]
    assert found.local_mods == [{"line": "MyLocal.pak", "after": ""}, {"line": "FirstAfter.pak", "after": "222"}]
    assert any("FirstAfter.pak" in w for w in found.warnings)  # listed but missing
    steamcmd = tmp_path / "conanops-steamcmd"
    mod_manager.write_modlist(str(root), str(steamcmd), found.mods, local=found.local_mods)
    lines = open(mod_manager.modlist_path(str(root))).read().splitlines()
    # Same order, and the Workshop mod still loads from where it was until ConanOps downloads its own copy.
    assert lines == ["MyLocal.pak", f"*{other_pak}", "FirstAfter.pak"]
    assert mod_manager.has_pak(str(steamcmd), found.mods[0])


def test_install_root_from_any_folder_inside_or_the_exe(tmp_path):
    root = tmp_path / "Srv"
    _make_install(root)
    exe = process_manager.server_exe_path(str(root))
    assert server_finder.install_root_for(exe) == str(root)
    assert server_finder.install_root_for(os.path.dirname(exe)) == str(root)
    assert server_finder.install_root_for(str(tmp_path)) == str(root)  # one install below the pick
    assert server_finder.install_root_for(str(tmp_path / "nothing")) is None


def test_deep_walk_finds_nested_installs_and_skips_junk(tmp_path):
    deep = tmp_path / "Games" / "Servers" / "Conan" / "Live"
    _make_install(deep)
    hidden = tmp_path / "Users" / "me" / "AppData" / "Conan"
    _make_install(hidden)
    found = server_finder.deep_walk([str(tmp_path)])
    assert found == [str(deep)]
    assert server_finder.deep_walk([str(tmp_path)], max_depth=2) == []


def test_library_reads_the_appmanifest_folder(tmp_path):
    lib = tmp_path / "steamcmd"
    inst = lib / "steamapps" / "common" / "Conan Exiles Enhanced Dedicated Server"
    _make_install(inst)
    (lib / "steamapps" / f"appmanifest_{server_finder.APP_ID}.acf").write_text(
        '"AppState"\n{\n\t"installdir"\t\t"Conan Exiles Enhanced Dedicated Server"\n}\n')
    assert str(inst) in server_finder._library_installs(str(lib))


def test_steamcmd_detected_from_where_mods_live(tmp_path):
    root = tmp_path / "S"
    _make_install(root)
    sc = tmp_path / "steamcmd"
    pak = sc / "steamapps" / "workshop" / "content" / "440900" / "5" / "M.pak"
    pak.parent.mkdir(parents=True)
    pak.write_text("x")
    (sc / "steamcmd.exe").write_text("")
    import mod_manager
    with open(mod_manager.modlist_path(str(root)), "w") as f:
        f.write(f"*{pak}\n")
    assert server_finder.read_server(str(root)).steamcmd_dir == str(sc)


def test_adopted_servers_are_never_deleted_by_default(tmp_path, monkeypatch):
    import cleanup
    monkeypatch.setattr(cleanup, "owned_roots", lambda: [str(tmp_path / "ConanOps")])
    s = models.ServerConfig(install_dir=str(tmp_path / "steamcmd" / "steamapps" / "common" / "Conan"),
                            steamcmd_dir=str(tmp_path / "steamcmd"))
    s.adopted = True
    assert cleanup.server_paths(s, [])["files"] == []
    plan = cleanup.server_paths(s, [], include_adopted_files=True)
    assert plan["files"] == [s.install_dir]  # their SteamCMD (holding the server) is never ours to delete
    legacy = models.ServerConfig(install_dir=str(tmp_path / "Elsewhere"))
    assert legacy.adopted is None and cleanup.is_adopted(legacy)
    ours = models.ServerConfig(install_dir=str(tmp_path / "ConanOps" / "abc" / "server"))
    assert not cleanup.is_adopted(ours)
    assert models.AppConfig().add_server("x").adopted is False


def _window(monkeypatch, cfg):
    import sys
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(sys.argv)
    import mod_manager
    import steamcmd
    from ui.main_window import MainWindow
    monkeypatch.setattr(mod_manager, "write_modlist", lambda *a, **k: None)
    monkeypatch.setattr(steamcmd, "is_steamcmd_installed", lambda d: True)
    monkeypatch.setattr(cfg, "save", lambda *a, **k: None)
    win = MainWindow(config=cfg)
    monkeypatch.setattr(win, "_start_network_reconcile", lambda *a, **k: None)
    win.RUN_OPS_INLINE = True
    return win


def test_take_over_backs_up_switches_off_and_restarts(tmp_path, monkeypatch):
    import backup_manager
    import takeover
    from PySide6.QtWidgets import QMessageBox
    root = tmp_path / "Chud"
    _make_install(root)
    (root / "ConanSandbox" / "Saved" / "game.db").write_text("w")
    with open(os.path.join(str(root), "ConanSandbox", "Saved", "Config", "WindowsServer", "Game.ini"), "w") as f:
        f.write("[RconPlugin]\nRconEnabled=False\n")
    cfg = models.AppConfig()
    other = cfg.add_server("Other")
    other.rcon_port = 25575
    win = _window(monkeypatch, cfg)
    backups, offs, launched, reports = [], [], [], []
    monkeypatch.setattr(backup_manager, "create_backup_for_server",
                        lambda s, d, t, label="": backups.append((s.name, d, label)) or object())
    svc = takeover.Rival(kind="service", name="svc", detail="", ident={"name": "S", "start": "Auto"},
                         install_dirs=[server_finder.path_key(str(root))], stops_server=True)

    def turn_off(rivals, server_running=None):
        offs.append([r.key for r in rivals])
        return takeover.TakeoverResult(done=list(rivals), records=[{"kind": "service", "service": "S",
                                       "install_dirs": [server_finder.path_key(str(root))]}],
                                       stopped_servers=[server_finder.path_key(str(root))])
    monkeypatch.setattr(takeover, "turn_off", turn_off)
    monkeypatch.setattr(win, "_launch_by_request", lambda s: launched.append(s.name))
    import preflight
    monkeypatch.setattr(preflight, "run_preflight", lambda s: preflight.CheckResult(ok=True, problems=[], repairs=[]))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: reports.append(a[2]))
    try:
        found = server_finder.read_server(str(root), ["x.exe", "-nosteam"])
        found.rivals = [svc]
        win._take_over([found], turn_off=True)
        s = next(x for x in cfg.servers if x.name == "Chudville")
        assert s.adopted and s.extra_launch_args == ["-nosteam"] and s.desired_running
        assert s.rcon_enabled and s.rcon_password and s.rcon_port != other.rcon_port
        assert s.backup_destination.endswith(os.path.join(s.id, "backups"))
        assert backups == [("Chudville", s.backup_destination, "Before ConanOps took over")]
        assert offs == [[svc.key]] and launched == ["Chudville"]
        assert cfg.takeover_records and reports and "svc" in reports[0]
        assert s.id not in win._automation_locked
    finally:
        win.close()


def test_not_switching_off_remembers_the_choice(tmp_path, monkeypatch):
    import takeover
    root = tmp_path / "Chud"
    _make_install(root)
    cfg = models.AppConfig()
    win = _window(monkeypatch, cfg)
    monkeypatch.setattr(takeover, "turn_off", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no")))
    try:
        found = server_finder.read_server(str(root))
        found.rivals = [takeover.Rival(kind="task", name="t", detail="", ident={"path": "\\", "name": "T"})]
        win._take_over([found], turn_off=False)
        assert cfg.servers and cfg.ignored_rivals == [found.rivals[0].key]
        assert not cfg.servers[0].desired_running
    finally:
        win.close()


def test_scan_attaches_rivals_and_reports_managed_ones(tmp_path, monkeypatch):
    import takeover
    from ui.window import finder
    a, b = tmp_path / "A", tmp_path / "B"
    _make_install(a)
    _make_install(b)
    pa, pb = server_finder.path_key(str(a)), server_finder.path_key(str(b))
    rivals = [takeover.Rival(kind="task", name="ta", detail="", ident={"path": "\\", "name": "ta"}, install_dirs=[pa]),
              takeover.Rival(kind="task", name="tb", detail="", ident={"path": "\\", "name": "tb"}, install_dirs=[pb]),
              takeover.Rival(kind="process", name="generic", detail="", ident={"cmd": "g"}),
              takeover.Rival(kind="task", name="ignored", detail="", ident={"path": "\\", "name": "x"}, install_dirs=[pa])]
    monkeypatch.setattr(takeover, "scan", lambda known: rivals)
    monkeypatch.setattr(server_finder, "scan_running", lambda: server_finder.RunningScan())
    monkeypatch.setattr(server_finder, "candidate_dirs", lambda *a, **k: [str(a), str(b)])
    res = finder.run_scan({"m1": str(a)}, [], [rivals[3].key], deep=False)
    assert [f.install_dir for f in res.found] == [str(b)]
    assert sorted(r.name for r in res.found[0].rivals) == ["generic", "tb"]
    assert [r.name for r in res.managed_rivals["m1"]] == ["ta"]  # specific ones only, minus ignored
