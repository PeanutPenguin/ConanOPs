"""Finding Conan servers already on this PC and taking them over.

Part of MainWindow (see ui/main_window.py)."""
from __future__ import annotations

import os
from typing import List

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QLabel, QVBoxLayout

import applog
import server_finder
from models import MAX_SERVERS

_log = applog.get_logger('ui.main_window')


class _FindWorker(QThread):
    found = Signal(object)  # List[server_finder.FoundServer]

    def __init__(self, managed: List[str], ignored: List[str], parent=None):
        super().__init__(parent)
        self.managed, self.ignored = managed, ignored

    def run(self) -> None:
        try:
            result = server_finder.find_unmanaged(self.managed, self.ignored)
        except Exception as e:  # noqa: BLE001 - always emit
            _log.warning(f"Server scan failed: {e}")
            result = []
        self.found.emit(result)


class _FoundDialog(QDialog):
    """Lists the servers found; each ticked one gets managed."""

    def __init__(self, found, room: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Conan servers found on this PC")
        lay = QVBoxLayout(self)
        intro = QLabel(f"ConanOps found {len(found)} Conan Exiles server{'s' if len(found) != 1 else ''} on this PC. "
                       "Manage them here? Nothing is reinstalled or reset: their world, settings, ports and mods "
                       "are kept exactly as they are.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.checks = []
        for i, f in enumerate(found):
            text = f"{f.name}{'  (running now)' if f.running else ''}\n{f.install_dir}"
            cb = QCheckBox(text)
            cb.setChecked(i < room)
            cb.setEnabled(i < room)
            lay.addWidget(cb)
            self.checks.append((cb, f))
        if len(found) > room:
            note = QLabel(f"ConanOps manages up to {MAX_SERVERS} servers, so only {room} more can be added.")
            note.setObjectName("Dim")
            lay.addWidget(note)
        buttons = QDialogButtonBox()
        self.add_btn = buttons.addButton("Manage These", QDialogButtonBox.AcceptRole)
        self.add_btn.setObjectName("PrimaryButton")
        self.skip_btn = buttons.addButton("Not Now", QDialogButtonBox.RejectRole)
        self.never_btn = buttons.addButton("Don't Ask Again", QDialogButtonBox.DestructiveRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.never_btn.clicked.connect(lambda: self.done(2))
        lay.addWidget(buttons)

    def chosen(self):
        return [f for cb, f in self.checks if cb.isChecked() and cb.isEnabled()]


class FinderMixin:
    def _scan_for_servers(self, from_add_button: bool = False) -> None:
        """Looks for unmanaged servers in the background; offers any it finds.
        From "Add server", finding none goes straight to the setup wizard."""
        if getattr(self, "_find_worker", None) is not None:
            return
        managed = [s.install_dir for s in self.config.servers if s.install_dir]
        ignored = [] if from_add_button else list(self.config.ignored_server_dirs)
        worker = _FindWorker(managed, ignored, parent=self)
        self._find_worker = worker

        def done(found, add=from_add_button):
            self._find_worker = None
            if found:
                self._offer_found_servers(found, add)
            elif add:
                self._add_new_server()
        worker.found.connect(done)
        self._retire_worker(worker)
        worker.start()

    def _offer_found_servers(self, found, from_add_button: bool) -> None:
        room = MAX_SERVERS - len(self.config.servers)
        if room <= 0:
            if from_add_button:
                self._add_new_server()  # shows the limit message
            return
        dialog = _FoundDialog(found, room, parent=self)
        if from_add_button:
            dialog.skip_btn.setText("Set Up a New Server Instead")
        result = dialog.exec()
        if result == QDialog.Accepted:
            for f in dialog.chosen():
                self._adopt_server(f)
        elif result == 2:
            self.config.ignored_server_dirs = sorted(set(self.config.ignored_server_dirs) |
                                                     {f.install_dir for f in found})
            self.config.save()
        elif from_add_button:
            self._add_new_server()

    def _adopt_server(self, found) -> None:
        """Manages an existing install as-is, keeping its world and settings."""
        import steamcmd
        from ui.setup_wizard import _no_space_default_base
        server = self.config.add_server(found.name)
        if server is None:
            return
        server.install_dir = found.install_dir
        server.steamcmd_dir = (found.steamcmd_dir or self.config.default_steamcmd_dir(exclude_id=server.id)
                               or os.path.join(_no_space_default_base(), server.id, "steamcmd"))
        for attr, value in found.values.items():
            if hasattr(server, attr):
                setattr(server, attr, value)
        server.gameplay.update(found.gameplay)
        server.mods = list(found.mods)
        server.desired_running = found.running
        self.config.active_server_id = server.id
        self.config.save()
        self._ensure_monitor(server)
        self._known_running[server.id] = found.running
        self._refresh_sidebar()
        self._load_active_server()
        self._notify(server, f"Now managed by ConanOps: {found.install_dir}. Its world, settings and mods were "
                             f"kept as they were.", title="Server Added")
        if not steamcmd.is_steamcmd_installed(server.steamcmd_dir):
            # Needed for updates and mods; installs in the background.
            self._run_op(lambda d=server.steamcmd_dir: steamcmd.install_steamcmd(d),
                         on_done=lambda _r: None,
                         on_error=lambda e, srv=server: self._notify(
                             srv, f"Couldn't install SteamCMD (needed for updates and mods): {e}",
                             title="SteamCMD Install Failed"))
        # Firewall rules and router forwarding for its ports, like a new server gets.
        self._start_network_reconcile(server)
