"""Explaining why a server won't start or keeps crashing from its own logs."""
import os
import time

import log_doctor


def _install(tmp_path, log_text=None, modlist=None):
    root = tmp_path / "srv"
    (root / "ConanSandbox" / "Saved" / "Logs").mkdir(parents=True)
    (root / "ConanSandbox" / "Mods").mkdir(parents=True)
    if log_text is not None:
        (root / "ConanSandbox" / "Saved" / "Logs" / "ConanSandbox.log").write_text(log_text, encoding="utf-8")
    if modlist is not None:
        (root / "ConanSandbox" / "Mods" / "modlist.txt").write_text(modlist, encoding="utf-8")
    return str(root)


def _keys(findings):
    return [f.key for f in findings]


def test_known_causes_are_explained_with_their_fix(tmp_path):
    cases = {
        "LogNet: Error: Unable to bind socket: address already in use": ("port_in_use", log_doctor.FIX_FREE_PORT),
        "LogMemory: Fatal error: Ran out of memory allocating 1048576 bytes": ("out_of_memory", ""),
        "LogDatabase: Error: database disk image is malformed": ("world_corrupt", log_doctor.FIX_RESTORE_BACKUP),
        "LogOnline: Warning: SteamAPI_Init() failed": ("steam_init", log_doctor.FIX_REPAIR_FILES),
        "LogPakFile: Warning: Failed to mount pak D:/x/Pippi.pak": ("mod_missing", log_doctor.FIX_DOWNLOAD_MODS),
        "LogWindows: Error: There is not enough space on the disk.": ("disk_full", ""),
        "LogBattlEye: Error: BattlEye Server failed to initialize": ("battleye", log_doctor.FIX_DISABLE_BATTLEYE),
    }
    for line, (key, fix) in cases.items():
        found = log_doctor.examine(_install(tmp_path / key, f"[2026.10.30-01.00.00:000][  0]{line}\n"))
        assert found and found[0].key == key and found[0].fix == fix, line
        assert line[-20:] in found[0].detail


def test_crash_inside_a_mod_names_the_mod(tmp_path):
    log = ("LogWindows: Error: === Critical error: ===\n"
           "LogWindows: Error: Fatal error!\n"
           "LogWindows: Error: [Callstack] 0x00 ConanSandboxServer!UObject /Game/Mods/Pippi/Blueprints/Thing.Thing\n")
    found = log_doctor.examine(_install(tmp_path, log), mods=[{"id": "1", "name": "Pippi - User & Server Management"}])
    assert found[0].key == "mod_crash" and "Pippi" in found[0].message and found[0].fix == log_doctor.FIX_FIND_BAD_MOD
    assert "fatal" not in _keys(found)  # one explanation, not two


def test_unrecognised_crash_quotes_the_server(tmp_path):
    found = log_doctor.examine(_install(tmp_path, "LogWindows: Error: Assertion failed: Foo != nullptr\n"))
    assert found[0].key == "fatal" and "Assertion failed: Foo != nullptr" in found[0].message


def test_missing_mod_files_from_modlist(tmp_path):
    present = tmp_path / "present.pak"
    present.write_text("x")
    install = _install(tmp_path, "", modlist=f"*{present}\n*C:\\steam\\steamapps\\workshop\\content\\440900\\42\\Gone.pak\n")
    found = log_doctor.examine(install, mods=[{"id": "42", "name": "Better Thralls"}])
    assert found[0].key == "mod_missing" and "Better Thralls" in found[0].message
    assert "present" not in found[0].detail


def test_clean_shutdown_isnt_reported_as_a_problem(tmp_path):
    log = "LogBattlEye: Warning: BattlEye could not connect\nLogExit: Exiting.\nLog file closed\n"
    assert log_doctor.examine(_install(tmp_path, log)) == []


def test_healthy_log_has_no_findings(tmp_path):
    assert log_doctor.examine(_install(tmp_path, "LogInit: Display: Engine is initialized.\n"), running=True) == []


def _crash(install, name, age_s, message="Fatal error! Unhandled Exception: EXCEPTION_ACCESS_VIOLATION"):
    d = os.path.join(log_doctor.crashes_dir(install), name)
    os.makedirs(d)
    with open(os.path.join(d, "CrashContext.runtime-xml"), "w") as f:
        f.write(f"<FGenericCrashContext><ErrorMessage>{message}</ErrorMessage></FGenericCrashContext>")
    t = time.time() - age_s
    os.utime(d, (t, t))


def test_crash_reports_and_repeated_crashes(tmp_path):
    install = _install(tmp_path, "LogInit: fine\n")
    _crash(install, "c1", 3600, "Ran out of memory allocating 4096 bytes")
    _crash(install, "c2", 7200)
    _crash(install, "c3", 9000)
    _crash(install, "old", 3 * 24 * 3600)
    found = log_doctor.examine(install)
    assert _keys(found) == ["out_of_memory", "crash_count"]
    assert "3 times" in found[1].message and found[1].status == log_doctor.STATUS_ERROR


def test_a_running_server_ignores_crashes_from_before_it_started(tmp_path):
    install = _install(tmp_path, "LogInit: fine\n")
    _crash(install, "c1", 3600)
    assert log_doctor.examine(install, running=True, started_at=time.time() - 60) == []


def test_explain_gives_one_line_for_alerts(tmp_path):
    install = _install(tmp_path, "LogNet: Error: Failed to listen on port 7777\n")
    assert log_doctor.explain(install).startswith("Likely cause: port already in use.")
    assert log_doctor.explain(str(tmp_path / "nothing")) == ""


def test_only_reads_the_end_of_a_huge_log(tmp_path):
    install = _install(tmp_path, "LogNet: Error: address already in use\n" + "LogX: filler\n" * 10)
    path = log_doctor.latest_log(install)
    assert len(log_doctor._tail_lines(path, max_bytes=40)) < 5


def test_crash_alert_includes_the_likely_cause(tmp_path, monkeypatch):
    import sys
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(sys.argv)
    import models
    from ui.main_window import MainWindow
    cfg = models.AppConfig()
    monkeypatch.setattr(cfg, "save", lambda *a, **k: None)
    s = cfg.add_server("Chud")
    s.install_dir = _install(tmp_path, "LogMemory: Error: Ran out of memory allocating 64 bytes\n")
    win = MainWindow(config=cfg)
    win.RUN_OPS_INLINE = True
    sent = []
    monkeypatch.setattr(win, "_notify", lambda srv, msg, title="": sent.append((title, msg)))
    try:
        win._notify_with_cause(s, "The server process stopped unexpectedly.", title="Server Crashed")
        assert sent[0][0] == "Server Crashed" and "Likely cause: ran out of memory." in sent[0][1]
    finally:
        win.close()
