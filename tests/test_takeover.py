"""Switching off the other things that start, restart or update a server
ConanOps takes over (takeover.py), and the finder pieces that use them."""
import os

import powershell
import server_finder
import takeover

EXE = r"ConanSandbox\Binaries\Win64\ConanSandboxServer-Win64-Shipping.exe"
SRV = r"D:\Games\Conan\Server"
OTHER = r"E:\Conan2"


def _bat(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return str(p)


def _proc(pid, name, cmdline, ppid=1, cwd=""):
    return {"pid": pid, "name": name, "cmdline": cmdline, "ppid": ppid, "cwd": cwd, "create_time": 100.0 + pid}


def test_finds_tasks_services_startup_and_loops(tmp_path, monkeypatch):
    monkeypatch.setattr(takeover.conanops_paths, "app_install_dir", lambda: r"C:\ConanOps")
    loop = _bat(tmp_path, "start_conan.bat",
                f':loop\r\n"{SRV}\\{EXE}" -log -MULTIHOME=192.168.1.9 -nosteam\r\ntimeout 10\r\ngoto loop\r\n')
    updater = _bat(tmp_path, "update.bat",
                   f'steamcmd.exe +force_install_dir "{SRV}" +login anonymous +app_update 443030 validate +quit\r\n')
    inventory = {
        "tasks": [
            {"path": "\\", "name": "Conan Updater", "state": "Ready",
             "actions": [{"exe": "cmd.exe", "args": f'/c "{updater}"', "wd": ""}]},
            {"path": "\\", "name": "ConanOps Keep-Alive", "state": "Ready",
             "actions": [{"exe": r"C:\ConanOps\ConanOps.exe", "args": "--watch", "wd": ""}]},
            {"path": "\\", "name": "Backup photos", "state": "Ready",
             "actions": [{"exe": "robocopy.exe", "args": "C:\\Pics D:\\Pics", "wd": ""}]},
            {"path": "\\", "name": "Old disabled", "state": "Disabled",
             "actions": [{"exe": "cmd.exe", "args": f'/c "{loop}"', "wd": ""}]},
        ],
        "services": [
            {"name": "ConanSvc", "display": "Conan Server (NSSM)", "state": "Running", "start": "Auto",
             "path": rf"C:\nssm\nssm.exe  {OTHER}\{EXE} -log {OTHER}"},
        ],
        "run": [
            {"key": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Run", "name": "ConanOps", "value": r'"C:\ConanOps\ConanOps.exe"'},
            {"key": r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Run", "name": "Discord", "value": "discord.exe"},
        ],
        "startup": [{"file": r"C:\Users\me\Startup\conan.lnk", "common": False, "target": f'cmd.exe /c "{loop}"'}],
    }
    procs = [
        _proc(10, "cmd.exe", ["cmd.exe", "/c", loop]),
        _proc(11, server_finder.EXE_NAME, [f"{SRV}\\{EXE}", "-log"], ppid=10),
        _proc(12, "python.exe", ["python.exe", r"C:\tools\conan_monitor.py"]),
        _proc(13, "cmd.exe", ["cmd.exe", "/c", "echo hi"]),
        _proc(14, "steamcmd.exe", ["steamcmd.exe", "+app_update", "443030"]),
        _proc(15, "WindowsGSM.exe", ["WindowsGSM.exe"]),  # manages other games too: only counts if it started a server
        _proc(16, "ConanServerManager.exe", ["ConanServerManager.exe"]),
        _proc(17, "ConanSandbox.exe", ["ConanSandbox.exe", "-BattlEye"]),  # someone playing the game: never touched
        _proc(18, "ConanOps.exe", ["ConanOps.exe"]),
    ]
    rivals = takeover.scan([SRV], inventory=inventory, processes=procs)
    by_kind = {}
    for r in rivals:
        by_kind.setdefault(r.kind, []).append(r)
    names = sorted(r.name for r in rivals)
    assert "ConanOps Keep-Alive" not in names and "ConanOps" not in names and "Discord" not in names
    assert "Backup photos" not in names and "Old disabled" not in names
    assert [r.name for r in by_kind["task"]] == ["Conan Updater"]
    assert by_kind["task"][0].install_dirs == [server_finder.path_key(SRV)]  # found through the .bat it runs
    svc = by_kind["service"][0]
    assert svc.stops_server and svc.install_dirs == [server_finder.path_key(OTHER)]
    assert by_kind["startup"][0].applies_to(SRV) and not by_kind["startup"][0].applies_to(OTHER)
    pids = sorted(r.ident["pid"] for r in by_kind["process"])
    assert pids == [10, 12, 16]  # the loop, the monitor, the Conan manager; not echo, steamcmd, the server or WindowsGSM
    loop_rival = next(r for r in by_kind["process"] if r.ident["pid"] == 10)
    assert loop_rival.name.startswith("start_conan.bat")


def test_hints_and_launch_lines_come_from_scripts(tmp_path):
    loop = _bat(tmp_path, "run.bat", f'cd /d "{SRV}"\r\n"{SRV}\\{EXE}" -log -usecheats -MaxPlayers=20\r\n')
    upd = _bat(tmp_path, "upd.bat", f'steamcmd +force_install_dir {OTHER} +app_update 443030 +quit\r\n')
    text = takeover.expand_text(f'cmd /c "{loop}" & "{upd}"')
    paths = takeover._paths_named(text)
    assert SRV in paths and OTHER in paths
    rival = takeover.Rival(kind="task", name="t", detail="", text=text)
    line = takeover.launch_lines([rival], [SRV])[server_finder.path_key(SRV)]
    extra, blocker = server_finder.split_args(line)
    assert extra == ["-usecheats"] and not blocker


def test_script_reading_follows_relative_scripts_and_stops(tmp_path):
    (tmp_path / "inner.cmd").write_text(f'"{SRV}\\{EXE}"\r\n', encoding="utf-8")
    (tmp_path / "outer.bat").write_text("call inner.cmd\r\ncall outer.bat\r\n", encoding="utf-8")
    text = takeover.expand_text("cmd /c outer.bat", cwd=str(tmp_path))
    assert "ConanSandboxServer" in text  # outer -> inner, and the self-call doesn't loop


def _rival(kind, **ident):
    return takeover.Rival(kind=kind, name=kind, detail="", ident=ident, install_dirs=[server_finder.path_key(SRV)])


def test_turn_off_does_what_it_can_unelevated_then_one_prompt(monkeypatch):
    order, off, scripts = [], set(), []
    rivals = [_rival("service", name="Svc", start="Auto"), _rival("task", path="\\", name="T"),
              _rival("process", pid=1, cmd="cmd", create=1.0),
              _rival("run", key=r"HKLM:\Software\Run", name="R", value="x.bat")]
    rivals[0].stops_server = True

    def unelevated(r, backup):
        order.append(r.kind)
        if r.kind == "process":
            off.add(r.key)
    monkeypatch.setattr(takeover, "_try_unelevated", unelevated)
    monkeypatch.setattr(takeover, "is_off", lambda r: r.key in off)

    def privileged(script, timeout=0):
        scripts.append(script)
        for r in rivals:
            if r.kind != "task":  # say the task wouldn't turn off
                off.add(r.key)
        return powershell.RUN_OK
    monkeypatch.setattr(takeover.powershell, "run_privileged", privileged)
    up = {server_finder.path_key(SRV): True}
    res = takeover.turn_off(rivals, server_running=lambda inst: up.pop(inst, False))
    assert order == ["process", "task", "run"]  # processes first; services only elevated
    assert len(scripts) == 1 and "Set-Service -Name 'Svc' -StartupType Disabled" in scripts[0]
    assert "Remove-ItemProperty -Path 'HKLM:\\Software\\Run' -Name 'R'" in scripts[0]
    assert "Stop-Process" not in scripts[0]  # the process was already gone
    assert sorted(r.kind for r in res.done) == ["process", "run", "service"]
    assert [r.kind for r, _w in res.failed] == ["task"]
    assert sorted(rec["kind"] for rec in res.records) == ["run", "service"]  # processes have nothing to undo
    assert res.stopped_servers == [server_finder.path_key(SRV)]
    assert not res.declined


def test_declined_prompt_is_reported(monkeypatch):
    monkeypatch.setattr(takeover, "_try_unelevated", lambda r, b: None)
    monkeypatch.setattr(takeover, "is_off", lambda r: False)
    monkeypatch.setattr(takeover.powershell, "run_privileged", lambda s, timeout=0: powershell.RUN_DECLINED)
    res = takeover.turn_off([_rival("task", path="\\", name="T")])
    assert res.declined and res.failed and "declined" in res.failed[0][1] and not res.records


def test_startup_file_moves_and_restores(tmp_path, monkeypatch):
    monkeypatch.setattr(takeover, "backup_dir", lambda: str(tmp_path / "off"))
    start = tmp_path / "Startup"
    start.mkdir()
    f = start / "conan.bat"
    f.write_text("x", encoding="utf-8")
    r = takeover.Rival(kind="startup", name="conan.bat", detail="", ident={"file": str(f), "common": False})
    monkeypatch.setattr(takeover.powershell, "run_privileged", lambda *a, **k: powershell.RUN_OK)
    res = takeover.turn_off([r])
    assert res.done and not f.exists()
    rec = res.records[0]
    assert os.path.isfile(rec["moved_to"])
    assert takeover.restore(res.records) == [] and f.exists()


def test_restore_builds_one_elevated_script(monkeypatch):
    seen = []
    monkeypatch.setattr(takeover.powershell, "run_privileged", lambda s, timeout=0: seen.append(s) or powershell.RUN_OK)
    recs = [{"kind": "task", "path": "\\", "task": "Conan's Updater"},
            {"kind": "service", "service": "Svc", "start": "Auto"},
            {"kind": "run", "key": r"HKLM:\Software\Run", "value_name": "R", "value": "x.bat"}]
    assert takeover.restore(recs) == []
    assert len(seen) == 1
    s = seen[0]
    assert "Enable-ScheduledTask -TaskPath '\\' -TaskName 'Conan''s Updater'" in s  # quoted safely
    assert "Set-Service -Name 'Svc' -StartupType Automatic" in s and "New-ItemProperty" in s


def test_rival_keys_are_stable():
    a = takeover.Rival(kind="task", name="x", detail="", ident={"path": "\\", "name": "Conan"})
    b = takeover.Rival(kind="task", name="y", detail="", ident={"path": "\\", "name": "CONAN"})
    assert a.key == b.key
    assert "Scheduled task" in a.describe()
