"""Finding Conan servers already on this PC and taking them over completely.

Taking over means: managing the install exactly as it is (world, settings,
ports, passwords, RCON, mods, launch arguments), backing it up first, and
switching off whatever else starts, restarts or updates it (old .bat loops,
scheduled tasks, startup items, services, monitor scripts), so ConanOps is
the only thing in charge. See server_finder.py and takeover.py.

Part of MainWindow (see ui/main_window.py)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFrame, QLabel, QMessageBox, QProgressBar,
    QScrollArea, QVBoxLayout, QWidget,
)

import applog
import server_finder
import takeover
from models import MAX_SERVERS

_log = applog.get_logger('ui.main_window')

RESULT_NEVER = 2  # "Don't Ask Again"
RESULT_BROWSE = 3


@dataclass
class ScanResult:
    found: List[server_finder.FoundServer] = field(default_factory=list)
    managed_rivals: Dict[str, list] = field(default_factory=dict)  # server id -> [takeover.Rival]
    rivals: list = field(default_factory=list)                     # everything takeover.scan() saw
    unreadable: int = 0


def run_scan(managed: Dict[str, str], ignored_dirs: List[str], ignored_rivals: List[str], deep: bool) -> ScanResult:
    """managed: server id -> install dir. Safe to call off the GUI thread."""
    result = ScanResult()
    try:
        running = server_finder.scan_running()
    except Exception as e:  # noqa: BLE001
        _log.warning(f"Couldn't list running servers: {e}")
        running = server_finder.RunningScan()
    result.unreadable = running.unreadable
    known = [d for d in managed.values() if d] + list(running.by_install)
    try:
        rivals = takeover.scan(known)
    except Exception as e:  # noqa: BLE001 - finding servers must not depend on this
        _log.warning(f"Couldn't look for other server launchers: {e}")
        rivals = []
    try:
        hints = takeover.install_hints(rivals)
    except Exception as e:  # noqa: BLE001
        _log.warning(f"Couldn't read install folders from launchers: {e}")
        hints = []
    lines = takeover.launch_lines(rivals, hints + list(running.by_install))
    found = server_finder.find_unmanaged(list(managed.values()), ignored_dirs, deep=deep, hints=hints,
                                         running=running, launch_lines=lines)
    if rivals:
        # Folders found other ways: a launcher may still name them.
        known_now = known + [f.install_dir for f in found]
        rivals = [r for r in rivals if not r.install_dirs or set(r.install_dirs) & {server_finder.path_key(k) for k in known_now}]
        for i, f in enumerate(found):
            if not f.running and not f.extra_args:
                line = takeover.launch_lines(rivals, [f.install_dir]).get(server_finder.path_key(f.install_dir))
                if line:
                    found[i] = f = server_finder.read_server(f.install_dir, None, line)
            f.rivals = [r for r in takeover.for_install(rivals, f.install_dir) if r.key not in ignored_rivals]
        for sid, inst in managed.items():
            # Only rivals that name this server: a stray Conan script isn't a reason to nag.
            mine = [r for r in rivals if inst and r.install_dirs and r.applies_to(inst) and r.key not in ignored_rivals]
            if mine:
                result.managed_rivals[sid] = mine
    result.found, result.rivals = found, rivals
    return result


class _FindWorker(QThread):
    found = Signal(object)  # ScanResult

    def __init__(self, managed: Dict[str, str], ignored: List[str], ignored_rivals: List[str], deep: bool, parent=None):
        super().__init__(parent)
        self.managed, self.ignored, self.ignored_rivals, self.deep = managed, ignored, ignored_rivals, deep

    def run(self) -> None:
        try:
            result = run_scan(self.managed, self.ignored, self.ignored_rivals, self.deep)
        except Exception as e:  # noqa: BLE001 - always emit
            _log.warning(f"Server scan failed: {e}")
            result = ScanResult()
        self.found.emit(result)


class _ScanningDialog(QDialog):
    """Shown while "Add server" looks around the PC (a deep scan can take a moment)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Looking for servers")
        self.setModal(True)
        lay = QVBoxLayout(self)
        text = QLabel("Looking for Conan Exiles servers already on this PC…")
        text.setWordWrap(True)
        lay.addWidget(text)
        bar = QProgressBar()
        bar.setRange(0, 0)
        lay.addWidget(bar)
        buttons = QDialogButtonBox()
        self.skip_btn = buttons.addButton("Skip — Set Up a New Server", QDialogButtonBox.RejectRole)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)


def _bullets(lines: List[str]) -> str:
    return "\n".join(f"• {line}" for line in lines)


class _FoundDialog(QDialog):
    """Lists the servers found; each ticked one gets managed. Shows what else
    runs them, and whether to switch those off."""

    def __init__(self, found, room: int, unreadable: int = 0, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Conan servers found on this PC")
        self.setMinimumWidth(560)
        self.found = list(found)
        lay = QVBoxLayout(self)
        n = len(self.found)
        intro = QLabel(f"ConanOps found {n} Conan Exiles server{'s' if n != 1 else ''} on this PC. "
                       "Manage them here? Nothing is reinstalled or reset: the world, settings, ports, passwords, "
                       "mods and launch options are kept exactly as they are, and a backup is made first.")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        rows = QVBoxLayout(inner)
        rows.setContentsMargins(0, 0, 0, 0)
        self.checks = []
        any_rivals = False
        allowed = 0
        for f in self.found:
            ok = not f.blocker and allowed < room
            if not f.blocker:
                allowed += 1
            cb = QCheckBox(f"{f.name}{'  (running now)' if f.running else ''}")
            cb.setChecked(ok)
            cb.setEnabled(ok)
            rows.addWidget(cb)
            notes = ([f.blocker] if f.blocker else []) + list(f.warnings)
            text = f.install_dir + ("\n" + _bullets(notes) if notes else "")
            if f.rivals:
                any_rivals = True
                text += "\n• Also started or updated by:\n" + "\n".join(f"      – {r.describe()}" for r in f.rivals)
            detail = QLabel(text)
            detail.setObjectName("Muted")
            detail.setWordWrap(True)
            detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
            detail.setContentsMargins(24, 0, 0, 8)
            rows.addWidget(detail)
            self.checks.append((cb, f))
        rows.addStretch(1)
        area.setWidget(inner)
        area.setMinimumHeight(min(420, 90 * max(1, n)))
        lay.addWidget(area)

        if allowed > room:
            note = QLabel(f"ConanOps manages up to {MAX_SERVERS} servers, so only {room} more can be added.")
            note.setObjectName("Dim")
            note.setWordWrap(True)
            lay.addWidget(note)
        if unreadable:
            note = QLabel(f"{unreadable} more server{' is' if unreadable == 1 else 's are'} running as administrator, "
                          "so Windows won't show ConanOps where it is. Turn on \"Run with admin rights\" in App "
                          "Settings, or use Browse to pick its folder.")
            note.setObjectName("Dim")
            note.setWordWrap(True)
            lay.addWidget(note)

        self.turn_off_cb = None
        if any_rivals:
            self.turn_off_cb = QCheckBox("Switch off the other launchers (recommended)")
            self.turn_off_cb.setChecked(True)
            lay.addWidget(self.turn_off_cb)
            hosted = [f.name for f in self.found for r in f.rivals if r.stops_server]
            note_text = ("So only ConanOps runs these servers: two managers on one server fight, each restarting or "
                         "updating it while the other is using it. "
                         "Nothing is deleted, and removing a server from ConanOps can turn them back on.")
            if hosted:
                note_text += (f" A Windows service runs {', '.join(sorted(set(hosted)))}, so it restarts once under "
                              "ConanOps (players can rejoin in a minute or two).")
            note = QLabel(note_text)
            note.setObjectName("Dim")
            note.setWordWrap(True)
            note.setContentsMargins(24, 0, 0, 0)
            lay.addWidget(note)

        buttons = QDialogButtonBox()
        self.add_btn = buttons.addButton("Manage These", QDialogButtonBox.AcceptRole)
        self.add_btn.setObjectName("PrimaryButton")
        self.browse_btn = buttons.addButton("Browse…", QDialogButtonBox.ActionRole)
        self.skip_btn = buttons.addButton("Not Now", QDialogButtonBox.RejectRole)
        self.never_btn = buttons.addButton("Don't Ask Again", QDialogButtonBox.DestructiveRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.never_btn.clicked.connect(lambda: self.done(RESULT_NEVER))
        self.browse_btn.clicked.connect(lambda: self.done(RESULT_BROWSE))
        for cb, _f in self.checks:
            cb.toggled.connect(self._sync_add_button)
        self._sync_add_button()
        lay.addWidget(buttons)

    def _sync_add_button(self) -> None:
        self.add_btn.setEnabled(bool(self.chosen()))

    def chosen(self):
        return [f for cb, f in self.checks if cb.isChecked() and cb.isEnabled()]

    @property
    def turn_off_rivals(self) -> bool:
        return self.turn_off_cb is not None and self.turn_off_cb.isChecked()


class _RivalsDialog(QDialog):
    """For servers ConanOps already manages that something else also runs."""

    def __init__(self, entries, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Other programs also run your server")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        lines = []
        for name, rivals in entries:
            lines.append(f"{name}:")
            lines += [f"    • {r.describe()}" for r in rivals]
        intro = QLabel("These also start, restart or update servers ConanOps manages. Two managers on one server "
                       "fight: each restarts or updates it while the other is using it.\n\n" + "\n".join(lines) +
                       "\n\nSwitch them off? Nothing is deleted, and removing the server from ConanOps can turn "
                       "them back on.")
        intro.setWordWrap(True)
        intro.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(intro)
        buttons = QDialogButtonBox()
        self.off_btn = buttons.addButton("Switch Them Off", QDialogButtonBox.AcceptRole)
        self.off_btn.setObjectName("PrimaryButton")
        buttons.addButton("Not Now", QDialogButtonBox.RejectRole)
        self.never_btn = buttons.addButton("Leave Them Running", QDialogButtonBox.DestructiveRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.never_btn.clicked.connect(lambda: self.done(RESULT_NEVER))
        lay.addWidget(buttons)


class FinderMixin:
    # ------------------------------------------------------------ scanning --
    def _scan_for_servers(self, from_add_button: bool = False) -> None:
        """Looks for unmanaged servers (and other launchers) in the background
        and offers to take them over. From "Add server", finding none goes
        to the setup wizard (or Browse)."""
        if getattr(self, "_find_worker", None) is not None:
            return
        managed = {s.id: s.install_dir for s in self.config.servers if s.install_dir}
        ignored = [] if from_add_button else list(self.config.ignored_server_dirs)
        ignored_rivals = [] if from_add_button else list(self.config.ignored_rivals)
        # Walking every drive is for when the person asked and is waiting ("Add server").
        deep = from_add_button
        worker = _FindWorker(managed, ignored, ignored_rivals, deep, parent=self)
        self._find_worker = worker
        waiting = None
        if from_add_button and not getattr(self, "RUN_OPS_INLINE", False):
            waiting = _ScanningDialog(self)
            self._find_skipped = False

            def skip():
                self._find_skipped = True  # the scan's result is dropped when it lands
                self._add_new_server()
            waiting.rejected.connect(skip)
            waiting.show()

        def done(result, add=from_add_button, dlg=waiting):
            self._find_worker = None
            skipped = getattr(self, "_find_skipped", False)
            self._find_skipped = False
            if dlg is not None:
                dlg.blockSignals(True)
                dlg.close()
                dlg.deleteLater()
            if skipped:
                return
            self._last_scan = result
            if result.found:
                self._offer_found_servers(result.found, add, unreadable=result.unreadable)
            elif add:
                self._offer_browse_or_new(result.unreadable)
            if result.managed_rivals:
                self._offer_turn_off_managed(result.managed_rivals)
        worker.found.connect(done)
        self._retire_worker(worker)
        worker.start()

    def _offer_browse_or_new(self, unreadable: int = 0) -> None:
        extra = ""
        if unreadable:
            extra = (f"\n\n{unreadable} server{' is' if unreadable == 1 else 's are'} running as administrator, so "
                     "Windows won't show ConanOps where it is. Browse to its folder, or turn on \"Run with admin "
                     "rights\" in App Settings and try again.")
        box = QMessageBox(self)
        box.setWindowTitle("Add a Server")
        box.setText("No other Conan Exiles servers were found on this PC." + extra +
                    "\n\nSet up a new one, or point ConanOps to a server you already have?")
        new_btn = box.addButton("Set Up a New Server", QMessageBox.AcceptRole)
        browse_btn = box.addButton("Find My Existing Server…", QMessageBox.ActionRole)
        box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(new_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is new_btn:
            self._add_new_server()
        elif clicked is browse_btn:
            self._browse_for_server(from_add_button=True)

    def _browse_for_server(self, from_add_button: bool = False, pending=None) -> None:
        """Lets the person point at an install ConanOps didn't find."""
        start = os.path.splitdrive(os.path.expanduser("~"))[0] + os.sep
        folder = QFileDialog.getExistingDirectory(self, "Pick your Conan Exiles server's folder", start)
        if not folder:
            if pending:
                self._offer_found_servers(pending, from_add_button)
            return
        root = server_finder.install_root_for(folder)
        if not root:
            QMessageBox.warning(self, "No Server There",
                                "That folder doesn't hold a Conan Exiles dedicated server (ConanOps looks for "
                                "ConanSandbox\\Binaries\\Win64\\ConanSandboxServer-Win64-Shipping.exe inside it). "
                                "Pick the server's main folder, the one that contains ConanSandbox.")
            if pending:
                self._offer_found_servers(pending, from_add_button)
            return
        mine = server_finder._norm(root)
        managed = next((s for s in self.config.servers if s.install_dir and server_finder._norm(s.install_dir) == mine), None)
        if managed is not None:
            QMessageBox.information(self, "Already Managed", f"ConanOps already manages that server as \"{managed.name}\".")
            return
        running = server_finder.scan_running().by_install.get(mine)
        rivals = list(getattr(getattr(self, "_last_scan", None), "rivals", []) or [])
        line = takeover.launch_lines(rivals, [root]).get(server_finder.path_key(root))
        found = server_finder.read_server(root, running, line)
        found.rivals = [r for r in takeover.for_install(rivals, root) if r.key not in self.config.ignored_rivals]
        others = [f for f in (pending or []) if server_finder._norm(f.install_dir) != mine]
        self._offer_found_servers([found] + others, from_add_button)

    # ------------------------------------------------------------- offering --
    def _offer_found_servers(self, found, from_add_button: bool, unreadable: int = 0) -> None:
        room = MAX_SERVERS - len(self.config.servers)
        if room <= 0:
            if from_add_button:
                self._add_new_server()  # shows the limit message
            return
        dialog = _FoundDialog(found, room, unreadable=unreadable, parent=self)
        if from_add_button:
            dialog.skip_btn.setText("Set Up a New Server Instead")
        result = dialog.exec()
        if result == QDialog.Accepted:
            self._take_over(dialog.chosen(), turn_off=dialog.turn_off_rivals)
        elif result == RESULT_BROWSE:
            self._browse_for_server(from_add_button, pending=found)
        elif result == RESULT_NEVER:
            self.config.ignored_server_dirs = sorted(set(self.config.ignored_server_dirs) |
                                                     {f.install_dir for f in found})
            self.config.save()
        elif from_add_button:
            self._add_new_server()

    def _offer_turn_off_managed(self, managed_rivals: Dict[str, list]) -> None:
        by_id = {s.id: s for s in self.config.servers}
        entries = [(by_id[sid].name, rivals) for sid, rivals in managed_rivals.items() if sid in by_id]
        if not entries:
            return
        dialog = _RivalsDialog(entries, parent=self)
        result = dialog.exec()
        rivals = _dedupe([r for _n, rs in entries for r in rs])
        if result == QDialog.Accepted:
            servers = [by_id[sid] for sid in managed_rivals if sid in by_id]
            self._turn_off_rivals(rivals, servers, backups=[])
        elif result == RESULT_NEVER:
            self.config.ignored_rivals = sorted(set(self.config.ignored_rivals) | {r.key for r in rivals})
            self.config.save()

    # ------------------------------------------------------------ taking over --
    def _take_over(self, chosen, turn_off: bool) -> None:
        servers = []
        for f in chosen:
            server = self._adopt_server(f, turned_off=turn_off and bool(f.rivals))
            if server is not None:
                servers.append(server)
        if not servers:
            return
        rivals = _dedupe([r for f in chosen for r in f.rivals]) if turn_off else []
        if not turn_off:
            keys = {r.key for f in chosen for r in f.rivals}
            if keys:
                self.config.ignored_rivals = sorted(set(self.config.ignored_rivals) | keys)
                self.config.save()
        self._turn_off_rivals(rivals, servers, backups=servers)

    def _adopt_server(self, found, turned_off: bool = False):
        """Manages an existing install as-is, keeping its world and settings."""
        import steamcmd
        from ui.setup_wizard import _no_space_default_base
        server = self.config.add_server(found.name)
        if server is None:
            return None
        server.adopted = True
        server.install_dir = found.install_dir
        base = os.path.join(_no_space_default_base(), server.id)
        server.steamcmd_dir = (found.steamcmd_dir or self.config.default_steamcmd_dir(exclude_id=server.id)
                               or os.path.join(base, "steamcmd"))
        # Backups live in ConanOps' own folder, never inside someone's SteamCMD library.
        server.backup_destination = os.path.join(base, "backups")
        for attr, value in found.values.items():
            if hasattr(server, attr):
                setattr(server, attr, value)
        server.gameplay.update(found.gameplay)
        server.mods = list(found.mods)
        server.local_mods = list(found.local_mods)
        server.extra_launch_args = list(found.extra_args)
        # It used to start by itself (task, service, startup item): ConanOps does that now.
        auto_started = turned_off and any(r.kind != takeover.KIND_PROCESS for r in found.rivals)
        server.desired_running = found.running or auto_started
        notes = []
        if not found.values.get("rcon_enabled"):
            notes.append("RCON was turned on (ConanOps uses it to save the world and warn players before "
                         "restarts); it takes effect the next time the server restarts.")
        # On, with a password, on a port no other server uses (keeps a working one as it is).
        self.enable_rcon(server)
        clash = {server.game_port, server.game_port + 1, server.query_port} & self.config.used_ports(exclude_id=server.id)
        if clash:
            notes.append(f"Its port{'s' if len(clash) > 1 else ''} {', '.join(map(str, sorted(clash)))} "
                         f"{'are' if len(clash) > 1 else 'is'} also used by another server here; change one in "
                         "Settings → Network before running both.")
        self.config.active_server_id = server.id
        self.config.save()
        self._ensure_monitor(server)
        self._known_running[server.id] = found.running
        self._refresh_sidebar()
        self._load_active_server()
        self._notify(server, f"Now managed by ConanOps: {found.install_dir}. Its world, settings and mods were "
                             f"kept as they were." + (" " + " ".join(notes) if notes else ""), title="Server Added")
        if not steamcmd.is_steamcmd_installed(server.steamcmd_dir):
            # Needed for updates and mods; installs in the background.
            self._run_op(lambda d=server.steamcmd_dir: steamcmd.install_steamcmd(d),
                         on_done=lambda _r: None,
                         on_error=lambda e, srv=server: self._notify(
                             srv, f"Couldn't install SteamCMD (needed for updates and mods): {e}",
                             title="SteamCMD Install Failed"))
        # Firewall rules and router forwarding for its ports, like a new server gets.
        self._start_network_reconcile(server)
        return server

    def _turn_off_rivals(self, rivals, servers, backups) -> None:
        """Backs up `backups`, switches off `rivals`, then starts any server a
        service stop took down. Automation waits for it to finish."""
        if not rivals and not backups:
            return
        import backup_manager
        import process_manager
        ids = [s.id for s in servers]
        for sid in ids:
            self._automation_locked.add(sid)

        def work():
            made = []
            for s in backups:
                if not (s.backup_destination and os.path.isdir(backup_manager.saved_dir(s.install_dir))):
                    continue
                try:
                    entry = backup_manager.create_backup_for_server(
                        s, s.backup_destination, backup_manager.TRIGGER_MANUAL, label="Before ConanOps took over")
                    made.append((s.id, entry is not None))
                except Exception as e:  # noqa: BLE001 - never block the takeover on a backup
                    _log.warning(f"Takeover backup failed for {s.name}: {e}")
                    made.append((s.id, False))
            outcome = takeover.turn_off(rivals, server_running=process_manager.is_running) if rivals else None
            return made, outcome

        def finish(res, ids=tuple(ids)):
            made, outcome = res
            for sid in ids:
                self._automation_locked.discard(sid)
            by_id = {s.id: s for s in self.config.servers}
            for sid, ok in made:
                if not ok and sid in by_id:
                    self._notify(by_id[sid], "The backup before taking over couldn't be made. Check Backups "
                                             "settings; nothing else was changed because of it.", title="Backup Failed")
            if outcome is None:
                return
            self.config.takeover_records = list(self.config.takeover_records) + list(outcome.records)
            self.config.save()
            self._restart_stopped_by_takeover(outcome.stopped_servers)
            self._report_takeover(outcome)

        def failed(e, ids=tuple(ids)):
            for sid in ids:
                self._automation_locked.discard(sid)
            _log.error(f"Takeover failed: {e}")
            QMessageBox.warning(self, "Couldn't Switch Everything Off",
                                f"Something went wrong switching off the other launchers: {e}\n\nThey may still "
                                "start or update the server. Check the log for details.")
        self._run_op(work, on_done=finish, on_error=failed)

    def _restart_stopped_by_takeover(self, installs) -> None:
        import preflight
        wanted = {server_finder.path_key(i) for i in installs}
        for s in self.config.servers:
            if not s.install_dir or server_finder.path_key(s.install_dir) not in wanted:
                continue
            self._known_running[s.id] = False
            result = preflight.run_preflight(s)
            if not result.ok:
                self._notify(s, "Couldn't start it after switching off its old service:\n" + "\n".join(result.problems),
                             title="Start Failed")
                continue
            try:
                self._launch_by_request(s)
            except (FileNotFoundError, OSError) as e:
                self._notify(s, f"Couldn't start it after switching off its old service: {e}", title="Start Failed")

    def _report_takeover(self, outcome) -> None:
        if not outcome.done and not outcome.failed:
            return
        parts = []
        if outcome.done:
            parts.append("Switched off:\n" + _bullets([r.describe() for r in outcome.done]))
        if outcome.failed:
            why = outcome.failed[0][1]
            parts.append(f"Still on ({why}):\n" + _bullets([r.describe() for r, _w in outcome.failed]) +
                         "\n\nThese can still start or update the server behind ConanOps' back. Try again from "
                         "\"Add server\", or switch them off yourself.")
        if outcome.done:
            parts.append(f"Moved files are kept in {takeover.backup_dir()}.")
        if outcome.failed:
            QMessageBox.warning(self, "Some Launchers Are Still On", "\n\n".join(parts))
        else:
            QMessageBox.information(self, "ConanOps Is in Charge", "\n\n".join(parts))

    def _restore_launchers(self, records) -> None:
        """Turns back on what a takeover switched off (from Remove server)."""
        keep = [r for r in self.config.takeover_records if r not in records]
        self.config.takeover_records = keep
        self.config.save()

        def failed(e):
            QMessageBox.warning(self, "Old Launchers", f"Couldn't turn the old launchers back on: {e}")

        def done(problems):
            if problems:
                QMessageBox.warning(self, "Old Launchers", "\n".join(problems))
        self._run_op(lambda recs=list(records): takeover.restore(recs), on_done=done, on_error=failed)

    def _fix_adopted_servers(self) -> None:
        """Servers taken over by 1.0.10 got no backup folder; give them one."""
        import cleanup
        from ui.setup_wizard import _no_space_default_base
        changed = False
        for s in self.config.servers:
            if s.install_dir and not s.backup_destination and cleanup.is_adopted(s):
                s.backup_destination = os.path.join(_no_space_default_base(), s.id, "backups")
                changed = True
        if changed:
            self.config.save()


def _dedupe(rivals):
    seen, out = set(), []
    for r in rivals:
        if r.key not in seen:
            seen.add(r.key)
            out.append(r)
    return out
