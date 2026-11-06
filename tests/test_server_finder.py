"""Finding Conan servers already on this PC and taking them over as-is."""
import json
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
    res = finder.run_scan_real({"m1": str(a)}, [], [rivals[3].key], deep=False)
    assert [f.install_dir for f in res.found] == [str(b)]
    assert sorted(r.name for r in res.found[0].rivals) == ["generic", "tb"]
    assert [r.name for r in res.managed_rivals["m1"]] == ["ta"]  # specific ones only, minus ignored


def test_a_scan_finishing_after_close_opens_nothing(monkeypatch):
    cfg = models.AppConfig()
    win = _window(monkeypatch, cfg)
    from ui.window import finder
    offered = []
    monkeypatch.setattr(win, "_offer_browse_or_new", lambda *a, **k: offered.append(1))
    monkeypatch.setattr(finder, "run_scan", lambda *a, **k: finder.ScanResult())
    win._closed = True
    win._scan_for_servers(from_add_button=True)
    from PySide6.QtWidgets import QApplication
    import time
    for _ in range(200):
        QApplication.processEvents()
        if win._find_worker is None:
            break
        time.sleep(0.01)
    assert win._find_worker is None and offered == []
    win.close()


def test_every_diagnostics_fix_has_a_handler_and_a_label(monkeypatch):
    import diagnostics
    from ui.diagnostics_page import FIX_LABELS
    fixes = {v for k, v in vars(diagnostics).items() if k.startswith("FIX_")}
    assert fixes == set(FIX_LABELS)
    cfg = models.AppConfig()
    win = _window(monkeypatch, cfg)
    try:
        page_side = {diagnostics.FIX_PORT_GUIDE, diagnostics.FIX_SIGN_IN, diagnostics.FIX_INSTALL_VC}
        for fix in fixes - page_side:
            assert win.diagnostics_page._fix_callback(fix) is not None
        unknown = []
        win._diagnostics_fix("nonsense", cfg.add_server("x"), lambda rerun=True: unknown.append(rerun))
        assert unknown == [False]
    finally:
        win.close()


def test_diagnostics_fixes_use_free_ports_and_backup_folder(tmp_path, monkeypatch):
    import network_utils
    from PySide6.QtWidgets import QMessageBox
    cfg = models.AppConfig()
    a = cfg.add_server("A")
    b = cfg.add_server("B")
    a.install_dir, b.install_dir = str(tmp_path / "a" / "server"), str(tmp_path / "b" / "server")
    b.game_port, b.query_port = a.game_port, a.query_port
    win = _window(monkeypatch, cfg)
    monkeypatch.setattr(network_utils, "is_udp_port_free", lambda p, host="0.0.0.0": True)
    monkeypatch.setattr(QMessageBox, "question", lambda *x, **k: QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *x, **k: None)
    dones = []
    try:
        win._diagnostics_fix("free_port", b, lambda rerun=True: dones.append(rerun))
        assert {b.game_port, b.game_port + 1, b.query_port}.isdisjoint({a.game_port, a.game_port + 1, a.query_port})
        assert b.query_port not in (b.game_port, b.game_port + 1)
        b.backup_destination = ""
        b.adopted = False
        win._diagnostics_fix("backup_folder", b, lambda rerun=True: dones.append(rerun))
        assert b.backup_destination == str(tmp_path / "b" / "backups")
        assert dones == [True, True]
    finally:
        win.close()


# ------------------------------------------- settings found wherever they are --

def _bare_install(root, **files):
    exe = process_manager.server_exe_path(str(root))
    os.makedirs(os.path.dirname(exe))
    open(exe, "w").close()
    for rel, body in files.items():
        path = os.path.join(str(root), "ConanSandbox", *rel.split("__"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(body)
    return str(root)


SAVED = "Saved__Config__WindowsServer__"


def test_admin_password_is_found_outside_the_usual_place(tmp_path):
    cases = {
        "default": {"Config__DefaultServerSettings.ini": "[ServerSettings]\nAdminPassword=s3\n"},
        "blank_hides": {SAVED + "ServerSettings.ini": "[ServerSettings]\nAdminPassword=\n",
                        "Config__DefaultServerSettings.ini": "[ServerSettings]\nAdminPassword=s3\n"},
        "game": {SAVED + "Game.ini": "[ServerSettings]\nAdminPassword=s3\n"},
        "engine": {SAVED + "Engine.ini": "[ServerSettings]\nAdminPassword=s3\n"},
        "section": {SAVED + "ServerSettings.ini": "[Other]\nAdminPassword=s3\n"},
        "quoted": {SAVED + "ServerSettings.ini": '[ServerSettings]\nAdminPassword="s3"\n'},
    }
    for name, files in cases.items():
        found = server_finder.read_server(_bare_install(tmp_path / name, **files))
        assert found.gameplay["AdminPassword"] == "s3", name
        assert found.admin_password_source and "s3" not in found.admin_password_source


def test_admin_password_from_launch_options_and_no_warning_when_found(tmp_path):
    root = _bare_install(tmp_path / "a")
    for line in (["x.exe", "-AdminPassword=s3"], ["x.exe", "ConanSandbox?listen?AdminPassword=s3"]):
        found = server_finder.read_server(root, None, line)
        assert found.gameplay["AdminPassword"] == "s3"
        assert not any("admin password" in w for w in found.warnings)
        assert not any("s3" in a for a in found.extra_args)


def test_says_so_when_there_is_no_admin_password(tmp_path):
    found = server_finder.read_server(_bare_install(tmp_path / "a"))
    assert any("No admin password" in w for w in found.warnings)


def test_launch_options_in_the_map_argument_are_read_and_not_kept(tmp_path):
    found = server_finder.read_server(_bare_install(tmp_path / "a"), None, [
        "x.exe", "ConanSandbox?listen?Port=7800?QueryPort=27100?MaxPlayers=12?ServerPassword=sp", "-RconPort=25999"])
    assert found.values == {"game_port": 7800, "query_port": 27100, "max_players": 12, "rcon_port": 25999,
                            "password": "sp"}
    assert found.extra_args == ["ConanSandbox?listen"]


def test_blank_section_doesnt_hide_the_join_password_and_quotes_go(tmp_path):
    root = _bare_install(tmp_path / "a", **{SAVED + "Engine.ini":
                         '[OnlineSubsystem]\nServerPassword="real"\nServerName="My Srv"\n'
                         "[OnlineSubsystemSteam]\nServerPassword=\nServerName=\n"})
    found = server_finder.read_server(root)
    assert found.name == "My Srv" and found.values["password"] == "real"


def test_join_password_and_name_in_server_settings(tmp_path):
    root = _bare_install(tmp_path / "a", **{SAVED + "ServerSettings.ini":
                         "[ServerSettings]\nServerPassword=sp\nServerName=N\nMaxPlayers=9\n"})
    found = server_finder.read_server(root)
    assert found.name == "N" and found.values["password"] == "sp" and found.values["max_players"] == 9


def test_placeholder_bind_address_and_script_variables_are_dropped(tmp_path):
    root = _bare_install(tmp_path / "a")
    for bad in ("YOUR_PUBLIC_IP", "%IP%"):
        found = server_finder.read_server(root, None, ["x.exe", f"-MULTIHOME={bad}", "-Foo=%BAR%", "-nosteam"])
        assert "bind_ip" not in found.values and found.extra_args == ["-nosteam"]
    assert any("YOUR_PUBLIC_IP" in w for w in server_finder.read_server(
        root, None, ["x.exe", "-MULTIHOME=YOUR_PUBLIC_IP"]).warnings)


def test_gameplay_comes_from_the_default_file_with_saved_on_top(tmp_path):
    root = _bare_install(tmp_path / "a", **{
        "Config__DefaultServerSettings.ini": "[ServerSettings]\nMaxNudity=1\nPVPEnabled=False\n",
        SAVED + "ServerSettings.ini": "[ServerSettings]\nMaxNudity=2\n"})
    found = server_finder.read_server(root)
    assert found.gameplay["MaxNudity"] == 2 and found.gameplay["PVPEnabled"] is False


def test_whitelist_and_bans_are_kept(tmp_path):
    root = _bare_install(tmp_path / "a", **{
        "Saved__whitelist.txt": "76561198000000001\n\n", "Saved__blacklist.txt": "76561198000000002\n",
        SAVED + "ServerSettings.ini": "[ServerSettings]\nEnableWhitelist=True\n"})
    v = server_finder.read_server(root).values
    assert v["whitelist_ids"] == ["76561198000000001"] and v["banned_ids"] == ["76561198000000002"]
    assert v["whitelist_enabled"] is True


def test_warns_when_saved_exists_but_has_no_world(tmp_path):
    root = _bare_install(tmp_path / "a", **{SAVED + "Engine.ini": "[URL]\nPort=7777\n"})
    assert any("no world save" in w for w in server_finder.read_server(root).warnings)
    open(os.path.join(root, "ConanSandbox", "Saved", "game.db"), "w").close()
    assert not any("no world save" in w for w in server_finder.read_server(root).warnings)


def test_launch_line_reads_the_starter_exe_comments_and_redirects():
    text = ('rem "C:\\S\\ConanSandboxServer.exe" -old\n'
            '"C:\\S\\ConanSandboxServer.exe" -log -nosteam > out.txt 2>&1\n')
    line = server_finder.launch_line_in(text, "C:\\S")
    assert line == ["C:\\S\\ConanSandboxServer.exe", "-log", "-nosteam"]


def test_running_server_keeps_its_real_folder_spelling(tmp_path, monkeypatch):
    root = tmp_path / "RealCase"
    _make_install(root)
    monkeypatch.setattr(server_finder, "candidate_dirs", lambda *a, **k: [])
    key = "/some/normalized/key"
    running = server_finder.RunningScan(by_install={key: ["x.exe"]}, paths={key: str(root)})
    assert [f.install_dir for f in server_finder.find_unmanaged([], running=running)] == [str(root)]


def test_launch_keeps_a_found_admin_password_in_the_settings_file(tmp_path):
    root = _bare_install(tmp_path / "a", **{SAVED + "Engine.ini": "[URL]\nPort=7777\n"})
    s = models.ServerConfig(name="x", install_dir=root, adopted=True)
    s.gameplay["AdminPassword"] = "s3"
    process_manager.sync_server_ini(s)
    path = os.path.join(root, "ConanSandbox", "Saved", "Config", "WindowsServer", "ServerSettings.ini")
    assert "AdminPassword=s3" in open(path).read()


def test_applying_a_taken_over_server_only_writes_what_changed(tmp_path, monkeypatch):
    root = tmp_path / "S"
    _make_install(root)
    cfg = models.AppConfig()
    win = _window(monkeypatch, cfg)
    try:
        win._adopt_server(server_finder.read_server(str(root)))
        s = cfg.get_active()
        path = os.path.join(str(root), "ConanSandbox", "Saved", "Config", "WindowsServer", "ServerSettings.ini")
        win._apply_gameplay(dict(s.gameplay, ServerMessageOfTheDay="hi"), s)
        text = open(path).read()
        assert "ServerMessageOfTheDay=hi" in text and "AdminPassword" not in text and "clanMaxSize" not in text
    finally:
        win.close()


# ---------------------------------------------- one server, not two (or a copy) --

def _scan_only(monkeypatch):
    monkeypatch.setattr(server_finder, "_steam_libraries", lambda: [])
    monkeypatch.setattr(server_finder, "_fixed_drives", lambda: [])
    return server_finder.RunningScan()


def test_one_install_reached_by_two_paths_is_one_server(tmp_path, monkeypatch):
    real = tmp_path / "real" / "Server"
    _make_install(real)
    try:
        (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    except OSError:
        import pytest
        pytest.skip("no symlink rights")
    alias = str(tmp_path / "link" / "Server")
    running = _scan_only(monkeypatch)
    assert len(server_finder.find_unmanaged([], hints=[str(real), alias], running=running)) == 1
    assert server_finder.find_unmanaged([alias], hints=[str(real)], running=running) == []


def _world(root, name="game.db"):
    with open(os.path.join(str(root), "ConanSandbox", "Saved", name), "w") as f:
        f.write("w" * 10)


def test_folder_steam_no_longer_uses_isnt_ticked(tmp_path, monkeypatch):
    common = tmp_path / "steamapps" / "common"
    old, new = common / "Conan Exiles Dedicated Server", common / "Conan Exiles Enhanced Dedicated Server"
    _make_install(old, port=7001, query=27001)
    _make_install(new, port=7002, query=27002)
    _world(old)
    _world(new)
    (tmp_path / "steamapps" / "appmanifest_443030.acf").write_text(
        '"AppState"\n{\n"installdir" "Conan Exiles Enhanced Dedicated Server"\n"buildid" "555"\n}\n')
    found = server_finder.find_unmanaged([], hints=[str(old), str(new)], running=_scan_only(monkeypatch))
    assert found[0].install_dir == str(new) and not found[0].likely_leftover
    assert "old folder" in found[1].likely_leftover
    assert found[0].values["installed_buildid"] == "555"


def test_copy_without_a_world_or_on_the_same_ports_isnt_ticked(tmp_path, monkeypatch):
    live, spare, old = tmp_path / "live", tmp_path / "spare", tmp_path / "old"
    for r in (live, spare, old):
        _make_install(r)
    _world(live)
    _world(old)
    os.utime(os.path.join(str(old), "ConanSandbox", "Saved", "game.db"), (1000, 1000))
    running = _scan_only(monkeypatch)
    running.by_install[server_finder._norm(str(live))] = ["x.exe"]
    found = {os.path.basename(f.install_dir): f for f in server_finder.find_unmanaged(
        [], hints=[str(spare), str(old)], running=running)}
    assert not found["live"].likely_leftover
    assert "no world save" in found["spare"].likely_leftover
    assert "same ports" in found["old"].likely_leftover


def test_separate_servers_on_their_own_ports_are_both_ticked(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    _make_install(a, port=7001, query=27001)
    _make_install(b, port=7005, query=27005)
    _world(a)
    _world(b)
    found = server_finder.find_unmanaged([], hints=[str(a), str(b)], running=_scan_only(monkeypatch))
    assert len(found) == 2 and not any(f.likely_leftover for f in found)


def test_commented_out_mod_stays_off_and_utf16_modlist_reads(tmp_path):
    root = tmp_path / "S"
    _make_install(root)
    path = os.path.join(str(root), "ConanSandbox", "Mods", "modlist.txt")
    with open(path, "w", encoding="utf-16") as f:
        f.write("C:\\s\\workshop\\content\\440900\\111\\A.pak\n#C:\\s\\workshop\\content\\440900\\222\\B.pak\n")
    mods = server_finder.read_server(str(root)).mods
    assert [(m["id"], m["enabled"]) for m in mods] == [("111", True), ("222", False)]


def test_a_launcher_naming_no_server_only_autostarts_a_lone_server(tmp_path, monkeypatch):
    import takeover
    a, b = tmp_path / "a", tmp_path / "b"
    _make_install(a)
    _make_install(b, name="B", port=7005, query=27005)
    cfg = models.AppConfig()
    win = _window(monkeypatch, cfg)
    generic = takeover.Rival(kind="task", name="t", detail="", ident={"path": "\\", "name": "t"})
    try:
        fa, fb = server_finder.read_server(str(a)), server_finder.read_server(str(b))
        fa.rivals = fb.rivals = [generic]
        assert not win._adopt_server(fa, turned_off=True, sole=False).desired_running
        assert win._adopt_server(fb, turned_off=True, sole=True).desired_running
    finally:
        win.close()


def test_found_dialog_leaves_leftovers_unticked_and_remembers_them(tmp_path, monkeypatch):
    from ui.window import finder as finder_ui
    a, b = tmp_path / "a", tmp_path / "b"
    _make_install(a)
    _make_install(b)
    _world(a)
    fa, fb = server_finder.read_server(str(a)), server_finder.read_server(str(b))
    server_finder.mark_leftovers([fa, fb])
    win = _window(monkeypatch, models.AppConfig())
    try:
        dialog = finder_ui._FoundDialog([fa, fb], room=5)
        assert dialog.chosen() == [fa] and dialog.passed_over() == [fb]
        monkeypatch.setattr(finder_ui, "_FoundDialog", lambda *x, **k: type("D", (), {
            "skip_btn": dialog.skip_btn, "exec": lambda s: finder_ui.QDialog.Accepted, "chosen": lambda s: [],
            "passed_over": lambda s: [fb], "turn_off_rivals": False})())
        win._offer_found_servers([fa, fb], False)
        assert str(b) in win.config.ignored_server_dirs
    finally:
        win.close()


# ---------------------------------------------- review of the finder's own fixes --

def test_passwords_with_percent_and_bang_survive_in_ini_files(tmp_path):
    for i, pw in enumerate(("100%sure%", "a!b!", "!x!", "%TEMP%")):
        root = _bare_install(tmp_path / str(i), **{
            SAVED + "ServerSettings.ini": f"[ServerSettings]\nAdminPassword={pw}\n",
            SAVED + "Engine.ini": f"[OnlineSubsystemSteam]\nServerPassword={pw}\n"})
        found = server_finder.read_server(root)
        assert found.gameplay["AdminPassword"] == pw and found.values["password"] == pw


def test_right_section_beats_a_stale_value_in_the_wrong_one(tmp_path):
    root = _bare_install(tmp_path / "a", **{
        SAVED + "Engine.ini": "[Other]\nAdminPassword=stale\n",
        SAVED + "ServerSettings.ini": "[Other]\nAdminPassword=stale2\n[ServerSettings]\nAdminPassword=real\n"})
    found = server_finder.read_server(root)
    assert found.gameplay["AdminPassword"] == "real"
    assert not any("not under" in w for w in found.warnings)
    root = _bare_install(tmp_path / "b", **{
        SAVED + "Game.ini": "[Other]\nAdminPassword=stale\n",
        "Config__DefaultServerSettings.ini": "[ServerSettings]\nAdminPassword=real\n"})
    assert server_finder.read_server(root).gameplay["AdminPassword"] == "real"


def test_warns_about_a_misplaced_admin_password(tmp_path):
    root = _bare_install(tmp_path / "a", **{SAVED + "Game.ini": "[Other]\nAdminPassword=s3\n"})
    found = server_finder.read_server(root)
    assert any("not under [ServerSettings]" in w and "Game.ini" in w and "s3" not in w for w in found.warnings)


def test_launch_doesnt_overwrite_a_newer_admin_password_in_the_file(tmp_path):
    root = _bare_install(tmp_path / "a", **{SAVED + "ServerSettings.ini": "[ServerSettings]\nAdminPassword=newer\n"})
    s = models.ServerConfig(name="x", install_dir=root, adopted=True)
    s.gameplay["AdminPassword"] = "older"
    process_manager.sync_server_ini(s)
    assert "AdminPassword=newer" in open(process_manager.server_settings_path(root)).read()


def test_powershell_launch_line_and_a_comment_like_quote():
    for rest in ('-ArgumentList "-log -Port=7800" -WindowStyle Hidden -Wait',
                 "-ArgumentList '-log','-Port=7800' -NoNewWindow"):
        line = server_finder.launch_line_in(f'Start-Process "C:\\S\\ConanSandboxServer.exe" {rest}', "C:\\S")
        assert line == ["C:\\S\\ConanSandboxServer.exe", "-log", "-Port=7800"], rest
    line = server_finder.launch_line_in("& 'C:\\S\\ConanSandboxServer.exe' -log", "C:\\S")
    assert line[1:] == ["-log"]
    assert server_finder.launch_line_in("'C:\\S\\ConanSandboxServer.exe' -log", "C:\\S")[1:] == ["-log"]


def test_old_folder_doesnt_claim_the_librarys_build(tmp_path):
    common = tmp_path / "steamapps" / "common"
    _make_install(common / "Old")
    _make_install(common / "New")
    (tmp_path / "steamapps" / "appmanifest_443030.acf").write_text('"installdir" "New"\n"buildid" "9"\n')
    assert server_finder.installed_build(str(common / "New")) == "9"
    assert server_finder.installed_build(str(common / "Old")) == ""


def test_running_server_without_rcon_gets_the_first_restart_warning(tmp_path):
    root = _bare_install(tmp_path / "a")
    assert any("first restart" in w for w in server_finder.read_server(root, ["x.exe"]).warnings)
    assert not any("first restart" in w for w in server_finder.read_server(root).warnings)
    root = _bare_install(tmp_path / "b", **{SAVED + "Game.ini": "[RconPlugin]\nRconEnabled=1\nRconPassword=r\nRconPort=25600\n"})
    assert not any("first restart" in w for w in server_finder.read_server(root, ["x.exe"]).warnings)


def test_adopting_moves_a_launch_option_admin_password_into_the_settings_file(tmp_path, monkeypatch):
    root = _bare_install(tmp_path / "a", **{SAVED + "ServerSettings.ini": "[ServerSettings]\nAdminPassword=other\n"})
    win = _window(monkeypatch, models.AppConfig())
    try:
        found = server_finder.read_server(root, None, ["x.exe", "-AdminPassword=fromarg"])
        server = win._adopt_server(found)
        assert server.extra_launch_args == []
        assert "AdminPassword=fromarg" in open(process_manager.server_settings_path(root)).read()
    finally:
        win.close()


def test_adopting_says_when_it_had_to_rename(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    for r in (a, b):
        _bare_install(r, **{SAVED + "Engine.ini": "[OnlineSubsystemSteam]\nServerName=Chud\n"})
    win = _window(monkeypatch, models.AppConfig())
    said = []
    monkeypatch.setattr(win, "_notify", lambda s, m, title="": said.append(m))
    try:
        win._adopt_server(server_finder.read_server(str(a)))
        assert win._adopt_server(server_finder.read_server(str(b))).name == "Chud 2"
        assert any('already called "Chud"' in m for m in said)
    finally:
        win.close()


def test_dialog_wont_take_more_than_there_is_room_for(tmp_path, monkeypatch):
    from ui.window import finder as finder_ui
    found = []
    for name in ("a", "b", "c"):
        root = tmp_path / name
        _make_install(root)
        _world(root)
        found.append(server_finder.read_server(str(root)))
    server_finder.mark_leftovers(found)
    win = _window(monkeypatch, models.AppConfig())
    try:
        dialog = finder_ui._FoundDialog(found, room=2)
        for cb, _f in dialog.checks:
            cb.setChecked(cb.isEnabled())
        assert len(dialog.chosen()) == 3 and not dialog.add_btn.isEnabled()
    finally:
        win.close()


def test_not_now_waits_a_week_and_then_asks_again(tmp_path, monkeypatch):
    from ui.window import finder as finder_ui
    cfg = models.AppConfig()
    cfg.save = lambda *a, **k: None
    monkeypatch.setattr(finder_ui.time, "time", lambda: 1000.0)
    finder_ui._snooze(cfg, ["C:\\S", ""])
    assert finder_ui._still_snoozed(cfg) == ["C:\\S"]
    monkeypatch.setattr(finder_ui.time, "time", lambda: 1000.0 + finder_ui.SNOOZE_SECONDS + 1)
    assert finder_ui._still_snoozed(cfg) == [] and cfg.snoozed_until == {}


def test_not_now_survives_saving_and_bad_values_are_ignored(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"servers": [], "snoozed_until": {"C:\\S": 5.5, "bad": "x", "worse": True}}))
    cfg = models.AppConfig.load(str(path))
    assert cfg.snoozed_until == {"C:\\S": 5.5}
    cfg.save(str(path))
    assert models.AppConfig.load(str(path)).snoozed_until == {"C:\\S": 5.5}


def test_old_takeovers_get_their_build_filled_in(tmp_path, monkeypatch):
    root = tmp_path / "S"
    _make_install(root)
    os.makedirs(os.path.join(str(root), "steamapps"))
    with open(os.path.join(str(root), "steamapps", "appmanifest_443030.acf"), "w") as f:
        f.write('"buildid" "77"\n')
    cfg = models.AppConfig()
    win = _window(monkeypatch, cfg)
    try:
        s = cfg.add_server("old")
        s.adopted, s.install_dir = True, str(root)
        win._fix_adopted_servers()
        assert s.installed_buildid == "77"
    finally:
        win.close()


def test_unknown_build_doesnt_trigger_an_update(tmp_path, monkeypatch):
    root = tmp_path / "S"
    _make_install(root)
    cfg = models.AppConfig()
    win = _window(monkeypatch, cfg)
    started = []
    monkeypatch.setattr(win, "_start_update_apply", lambda *a, **k: started.append(a))
    monkeypatch.setattr(win, "_notify", lambda *a, **k: None)
    try:
        s = cfg.add_server("old")
        s.adopted, s.install_dir = True, str(root)
        win._on_periodic_update_check(s, "100")
        assert not started and s.installed_buildid == "100"
        win._on_periodic_update_check(s, "101")
        assert started
        fresh = cfg.add_server("new")  # a server ConanOps installed isn't guessed at
        win._settle_unknown_build(fresh, "100")
        assert fresh.installed_buildid == ""
    finally:
        win.close()


# ------------------------------------------------------- never one install twice --

def test_the_same_install_is_never_managed_twice(tmp_path, monkeypatch):
    root = tmp_path / "S"
    _make_install(root)
    cfg = models.AppConfig()
    win = _window(monkeypatch, cfg)
    monkeypatch.setattr(win, "_notify", lambda *a, **k: None)
    try:
        assert win._adopt_server(server_finder.read_server(str(root))) is not None
        assert win._adopt_server(server_finder.read_server(str(root))) is None  # a stale scan's copy
        assert win._adopt_server(server_finder.read_server(str(root) + os.sep)) is None
        assert len(cfg.servers) == 1
    finally:
        win.close()


def test_a_link_to_a_managed_install_is_the_same_server(tmp_path, monkeypatch):
    import pytest
    real = tmp_path / "real" / "S"
    _make_install(real)
    try:
        (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    except OSError:
        pytest.skip("no symlink rights")
    cfg = models.AppConfig()
    win = _window(monkeypatch, cfg)
    monkeypatch.setattr(win, "_notify", lambda *a, **k: None)
    try:
        win._adopt_server(server_finder.read_server(str(real)))
        assert win._adopt_server(server_finder.read_server(str(tmp_path / "link" / "S"))) is None
        assert len(cfg.servers) == 1
    finally:
        win.close()


def test_found_list_drops_servers_added_since_the_scan_and_duplicates(tmp_path, monkeypatch):
    from ui.window import finder as finder_ui
    a, b = tmp_path / "a", tmp_path / "b"
    _make_install(a)
    _make_install(b)
    cfg = models.AppConfig()
    win = _window(monkeypatch, cfg)
    monkeypatch.setattr(win, "_notify", lambda *a, **k: None)
    shown = []
    monkeypatch.setattr(finder_ui, "_FoundDialog", lambda found, *x, **k: shown.append(list(found)) or type(
        "D", (), {"skip_btn": type("B", (), {"setText": lambda s, t: None})(), "exec": lambda s: 0})())
    try:
        fa, fb = server_finder.read_server(str(a)), server_finder.read_server(str(b))
        win._adopt_server(fa)  # added while the scan ran (or in another dialog)
        win._offer_found_servers([fa, fb, server_finder.read_server(str(b))], False)
        assert [f.install_dir for f in shown[0]] == [str(b)]
        shown.clear()
        offered = []
        monkeypatch.setattr(win, "_offer_browse_or_new", lambda *x: offered.append(1))
        win._offer_found_servers([fa], True)
        assert not shown and offered  # nothing left to offer: straight to "add a server"
    finally:
        win.close()


def test_add_server_during_the_startup_scan_isnt_ignored(monkeypatch):
    from ui.window import finder as finder_ui
    cfg = models.AppConfig()
    win = _window(monkeypatch, cfg)
    calls = []
    try:
        class Pending:
            def isRunning(self):
                return True
        win._find_worker = Pending()
        real = finder_ui.FinderMixin._scan_for_servers
        win._scan_for_servers(from_add_button=True)
        assert win._find_wants_add
        win._find_worker = None
        monkeypatch.setattr(finder_ui, "_FindWorker", lambda *a, **k: type("W", (), {
            "found": type("S", (), {"connect": lambda s, fn: calls.append(fn)})(),
            "start": lambda s: None, "finished": type("S", (), {"connect": lambda s, *a: None})(),
            "isRunning": lambda s: False, "wait": lambda s, *a: True})())
        monkeypatch.setattr(win, "_offer_found_servers", lambda *a, **k: calls.append(("offered", a[1])))
        win.RUN_OPS_INLINE = True
        real(win)  # the startup scan
        calls[0](finder_ui.ScanResult())  # it lands...
        assert win._find_wants_add is False
        assert len([c for c in calls if callable(c)]) == 2  # ...and the Add server scan starts
    finally:
        win._find_worker = None
        win.close()
