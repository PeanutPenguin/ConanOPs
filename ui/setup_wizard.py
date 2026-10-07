"""
First-run setup wizard for a server: installs SteamCMD, downloads the
dedicated server, picks a local IP and free ports, adds firewall rules,
tries UPnP, and reports anything (e.g. router forwarding) left to do by hand.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QFileDialog, QPlainTextEdit, QWizard, QWizardPage, QProgressBar, QMessageBox,
    QRadioButton, QComboBox, QCheckBox,
)

import backup_manager
from ui import assets
import conanops_paths
import network_setup_runner
import steamcmd as steamcmd_mod
import vcredist
from models import ServerConfig


class _InstallWorker(QThread):
    def _ensure_vc_runtime(self) -> None:
        """Installs the VC++ runtime the server needs (SteamCMD doesn't).
        Never fails setup; pre-launch checks offer it again."""
        try:
            if self.isInterruptionRequested() or not vcredist.needs_install():
                return
            vcredist.install(progress=self.progress.emit)
        except Exception as e:  # noqa: BLE001 - optional step
            self.progress.emit(f"Couldn't set up the Visual C++ runtime: {e}")

    progress = Signal(str)
    progress_percent = Signal(float)  # SteamCMD download percentage
    finished_ok = Signal(bool)

    def __init__(self, steamcmd_dir: str, install_dir: str, restore_backup_path: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.steamcmd_dir = steamcmd_dir
        self.install_dir = install_dir
        self.restore_backup_path = restore_backup_path

    def run(self) -> None:
        try:
            ok = steamcmd_mod.install_steamcmd(
                self.steamcmd_dir, progress=self.progress.emit, should_cancel=self.isInterruptionRequested
            )
            if not ok or self.isInterruptionRequested():
                self.finished_ok.emit(False)
                return
            self.progress.emit("Downloading Conan Exiles Dedicated Server (this can take a while)...")
            result = steamcmd_mod.update_server(
                self.steamcmd_dir, self.install_dir, progress=self.progress.emit,
                should_cancel=self.isInterruptionRequested,
                on_progress_percent=self.progress_percent.emit,
            )
            if self.isInterruptionRequested():
                self.progress.emit("Cancelled.")
                self.finished_ok.emit(False)
                return
            if result.success:
                self.progress.emit("Server download complete.")
                self._ensure_vc_runtime()
            else:
                self.progress.emit("Server download did not finish cleanly -- check the log above.")
                self.finished_ok.emit(False)
                return

            if self.restore_backup_path:
                self.progress.emit("Restoring world save from the chosen backup...")
                try:
                    entry = backup_manager.BackupEntry(
                        path=self.restore_backup_path, when=datetime.now(),
                        trigger="import", size_bytes=os.path.getsize(self.restore_backup_path),
                    )
                    backup_manager.restore_backup(self.install_dir, entry)
                    self.progress.emit("Backup restored.")
                except Exception as e:  # noqa: BLE001 - a failed restore shouldn't be reported as a successful setup
                    self.progress.emit(f"Restoring the backup failed: {e}")
                    self.finished_ok.emit(False)
                    return

            self.finished_ok.emit(True)
        except Exception as e:  # report instead of silently killing the thread
            self.progress.emit(f"Unexpected error: {e}")
            self.finished_ok.emit(False)


def _no_space_default_base() -> str:
    """Default base folder for new servers (conanops_paths.no_space_root())."""
    return conanops_paths.no_space_root()


def _path_has_spaces(path: str) -> bool:
    return " " in path


def _path_is_ascii(path: str) -> bool:
    try:
        path.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _is_within(child: str, parent: str) -> bool:
    """True if `child` is `parent` or anywhere inside it (both normalized)."""
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:  # different drives
        return False


def _nearest_existing_dir(path: str) -> Optional[str]:
    p = path
    while p and not os.path.isdir(p):
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent
    return p or None


def _dir_has_contents(path: str) -> bool:
    try:
        return os.path.isdir(path) and any(os.scandir(path))
    except OSError:
        return False


def _clean_server_name(name: str) -> str:
    return " ".join(name.split())


_MAX_NAME_LEN = 80

# Install-drive free space: below the first setup refuses, below the second it asks.
_MIN_FREE_FOR_INSTALL = steamcmd_mod.MIN_FREE_BYTES_FOR_UPDATE
_COMFORTABLE_FREE_FOR_INSTALL = 40 * 1024 ** 3


def _free_bytes(path: str) -> Optional[int]:
    import shutil
    base = _nearest_existing_dir(path)
    if base is None:
        return None
    try:
        return shutil.disk_usage(base).free
    except OSError:
        return None


class PathsPage(QWizardPage):
    def __init__(self, server: ServerConfig, get_reserved_dirs, get_default_steamcmd_dir=None,
                 get_reserved_names=None, parent=None):
        super().__init__(parent)
        self.server = server
        self.get_reserved_dirs = get_reserved_dirs
        self.get_reserved_names = get_reserved_names or (lambda: set())
        self.install_dir_preexisting = True   # until validatePage() learns otherwise
        self.steamcmd_dir_preexisting = True
        self.setTitle("Name this server, and where should it live?")

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Server name:"))
        self.name_edit = QLineEdit(server.name)
        layout.addWidget(self.name_edit)
        name_note = QLabel(
            "Shown in ConanOps' own sidebar and in the in-game server browser. You can "
            "rename it later from Network & Ports settings."
        )
        name_note.setObjectName("Dim")
        name_note.setWordWrap(True)
        layout.addWidget(name_note)

        layout.addWidget(QLabel("SteamCMD folder:"))
        row1 = QHBoxLayout()
        # Default to an existing server's SteamCMD folder; it and its mod cache are safe to share.
        default_steamcmd = (get_default_steamcmd_dir() if get_default_steamcmd_dir else "") or \
            os.path.join(_no_space_default_base(), server.id, "steamcmd")
        self.steamcmd_edit = QLineEdit(server.steamcmd_dir or default_steamcmd)
        browse1 = QPushButton("Browse…")
        browse1.clicked.connect(lambda: self._browse(self.steamcmd_edit))
        row1.addWidget(self.steamcmd_edit, 1)
        row1.addWidget(browse1)
        layout.addLayout(row1)

        layout.addWidget(QLabel("Server install folder:"))
        row2 = QHBoxLayout()
        self.install_edit = QLineEdit(server.install_dir or os.path.join(_no_space_default_base(), server.id, "server"))
        browse2 = QPushButton("Browse…")
        browse2.clicked.connect(lambda: self._browse(self.install_edit))
        row2.addWidget(self.install_edit, 1)
        row2.addWidget(browse2)
        layout.addLayout(row2)

        note = QLabel(
            "Both folders will be created automatically if they don't exist yet. The SteamCMD "
            "folder can be shared across servers (it's reused above if you already have one); "
            "the server install folder must be different for each server."
        )
        note.setObjectName("Dim")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.registerField("name*", self.name_edit)
        self.registerField("steamcmd_dir*", self.steamcmd_edit)
        self.registerField("install_dir*", self.install_edit)
        # PySide6's "*" mandatory-field check misses text set from code and can
        # leave Next disabled, so isComplete() checks the widgets directly.
        self.name_edit.textChanged.connect(self.completeChanged)
        self.steamcmd_edit.textChanged.connect(self.completeChanged)
        self.install_edit.textChanged.connect(self.completeChanged)

    def isComplete(self) -> bool:
        return (
            bool(self.name_edit.text().strip())
            and bool(self.steamcmd_edit.text().strip())
            and bool(self.install_edit.text().strip())
        )

    def _browse(self, edit: QLineEdit) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose folder")
        if path:
            edit.setText(path)

    def validatePage(self) -> bool:
        """Rejects bad names/folders and writes the cleaned values back into
        the fields, since later steps read the fields."""
        raw_name = self.name_edit.text()
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw_name.strip()):
            QMessageBox.warning(self, "Name not allowed", "The server name can't contain tabs or line breaks.")
            return False
        name = _clean_server_name(raw_name)
        if not name:
            QMessageBox.warning(self, "Name needed", "Give this server a name.")
            return False
        if len(name) > _MAX_NAME_LEN:
            QMessageBox.warning(self, "Name too long", f"Keep the server name to {_MAX_NAME_LEN} characters or fewer.")
            return False
        reserved_names = {_clean_server_name(n).casefold() for n in self.get_reserved_names()}
        if name.casefold() in reserved_names:
            QMessageBox.warning(
                self, "Name already in use",
                f"Another configured server is already named \"{name}\". Pick a different name.",
            )
            return False

        install_raw = self.install_edit.text().strip()
        steamcmd_raw = self.steamcmd_edit.text().strip()
        install_dir = _norm(install_raw)
        steamcmd_dir = _norm(steamcmd_raw)

        # SteamCMD's app_update can fail with "Missing configuration" on paths with spaces.
        for label, path in (("SteamCMD folder", steamcmd_dir), ("server install folder", install_dir)):
            if _path_has_spaces(path):
                QMessageBox.warning(
                    self, "Path contains a space",
                    f"The {label} can't contain a space -- SteamCMD fails to download the server "
                    f"with a confusing \"Missing configuration\" error when it does.\n\n"
                    f"Pick a folder without spaces in its path, e.g. C:\\ConanOps\\... or "
                    f"C:\\Users\\Public\\ConanOps\\...",
                )
                return False

        if install_dir == steamcmd_dir:
            QMessageBox.warning(
                self, "Folder conflict",
                "The SteamCMD folder and the server install folder can't be the same folder.",
            )
            return False
        if _is_within(install_dir, steamcmd_dir) or _is_within(steamcmd_dir, install_dir):
            QMessageBox.warning(
                self, "Folder conflict",
                "The server install folder and the SteamCMD folder can't be inside each other -- "
                "use two separate folders (e.g. ...\\steamcmd and ...\\server side by side).",
            )
            return False

        for other in self.get_reserved_dirs():
            other_n = _norm(other)
            if install_dir == other_n:
                QMessageBox.warning(
                    self, "Folder already in use",
                    "Another configured server already uses this install folder. Pick a different one.",
                )
                return False
            if _is_within(install_dir, other_n) or _is_within(other_n, install_dir):
                QMessageBox.warning(
                    self, "Folder already in use",
                    "This install folder is inside (or contains) another server's install folder. "
                    "Each server needs its own separate folder.",
                )
                return False

        for label, path in (("SteamCMD folder", steamcmd_dir), ("server install folder", install_dir)):
            base = _nearest_existing_dir(path)
            if base is None:
                QMessageBox.warning(self, "Folder not available",
                                    f"The drive for the {label} doesn't exist:\n{path}")
                return False
            if not os.access(base, os.W_OK):
                QMessageBox.warning(
                    self, "Folder not writable",
                    f"ConanOps can't create the {label} here -- Windows won't allow writing to\n{base}\n\n"
                    "Pick a folder you own, e.g. C:\\ConanOps\\...",
                )
                return False

        free = _free_bytes(install_dir)
        if free is not None and not _dir_has_contents(os.path.join(install_dir, "ConanSandbox")):
            gb = free / 1024 ** 3
            if free < _MIN_FREE_FOR_INSTALL:
                QMessageBox.warning(
                    self, "Not enough disk space",
                    f"Only {gb:.1f} GB is free on the drive for the server install folder. The Conan "
                    f"Exiles server download is large -- free up space or choose a folder on another "
                    f"drive (at least {_MIN_FREE_FOR_INSTALL / 1024 ** 3:.0f} GB free, more is better).",
                )
                return False
            if free < _COMFORTABLE_FREE_FOR_INSTALL:
                answer = QMessageBox.question(
                    self, "Low disk space",
                    f"Only {gb:.1f} GB is free on that drive. The server, its mods, backups and future "
                    f"updates can need more than that over time.\n\nInstall here anyway?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
                )
                if answer != QMessageBox.Yes:
                    return False

        if not (_path_is_ascii(install_dir) and _path_is_ascii(steamcmd_dir)):
            answer = QMessageBox.question(
                self, "Unusual characters in folder path",
                "One of these folder paths contains non-English characters (accents, symbols, other "
                "alphabets). SteamCMD can have trouble with those. A plain path like C:\\ConanOps\\... "
                "is safest.\n\nUse these folders anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return False

        if _dir_has_contents(install_dir) and not os.path.isdir(os.path.join(install_dir, "ConanSandbox")):
            answer = QMessageBox.question(
                self, "Folder isn't empty",
                f"The server install folder already has files in it and doesn't look like a Conan "
                f"Exiles server:\n{install_dir}\n\nThe server will be downloaded into it alongside "
                f"whatever's there. Continue?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return False

        # So cancelling only offers to delete folders this setup created.
        self.install_dir_preexisting = _dir_has_contents(install_dir)
        self.steamcmd_dir_preexisting = _dir_has_contents(steamcmd_dir)

        self.name_edit.setText(name)
        self.install_edit.setText(os.path.abspath(install_raw))
        self.steamcmd_edit.setText(os.path.abspath(steamcmd_raw))
        return True


class RestorePage(QWizardPage):
    """Optional: seed the world from an existing backup. The restore runs
    in _InstallWorker right after the download."""

    def __init__(self, get_backup_sources=None, parent=None):
        super().__init__(parent)
        self.setTitle("Start fresh, or restore from a backup?")
        self.selected_backup_path: Optional[str] = None
        self._external_path: Optional[str] = None
        self._candidates = list(get_backup_sources()) if get_backup_sources else []

        layout = QVBoxLayout(self)
        self.fresh_radio = QRadioButton("Start with a fresh, empty world")
        self.fresh_radio.setChecked(True)
        self.restore_radio = QRadioButton("Restore the world from an existing backup")
        layout.addWidget(self.fresh_radio)
        layout.addWidget(self.restore_radio)

        layout.addWidget(QLabel("One of your other servers' backups:"))
        self.source_combo = QComboBox()
        self.source_combo.setEnabled(False)
        if self._candidates:
            for label, path in self._candidates:
                self.source_combo.addItem(label, path)
        else:
            self.source_combo.addItem("(no backups from other servers found)", None)
            self.source_combo.setEnabled(False)
        layout.addWidget(self.source_combo)

        browse_row = QHBoxLayout()
        self.external_path_label = QLabel("(no file chosen)")
        self.external_path_label.setObjectName("Dim")
        self.browse_btn = QPushButton("Or Browse for a Backup File…")
        self.browse_btn.setEnabled(False)
        self.browse_btn.clicked.connect(self._browse_external)
        browse_row.addWidget(self.external_path_label, 1)
        browse_row.addWidget(self.browse_btn)
        layout.addLayout(browse_row)

        note = QLabel(
            "This brings over the world save (and its ServerSettings/Engine.ini) -- not this "
            "server's ConanOps settings (rates, mods, schedules, etc.), which stay separate and "
            "default to normal until you set them up on the Settings pages."
        )
        note.setObjectName("Dim")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.fresh_radio.toggled.connect(self._on_mode_toggled)
        self.restore_radio.toggled.connect(self._on_mode_toggled)

    def _on_mode_toggled(self) -> None:
        restoring = self.restore_radio.isChecked()
        self.source_combo.setEnabled(restoring and bool(self._candidates))
        self.browse_btn.setEnabled(restoring)

    def _browse_external(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(self, "Choose a backup file", "", "Backup zip (*.zip)")
        if path:
            self._external_path = path
            self.external_path_label.setText(os.path.basename(path))

    def validatePage(self) -> bool:
        if not self.restore_radio.isChecked():
            self.selected_backup_path = None
            return True

        # A browsed-to file wins over the combo's default.
        path = self._external_path or self.source_combo.currentData()
        if not path:
            QMessageBox.warning(
                self, "No backup chosen",
                "Pick a backup from the list, browse for a backup file, or choose to start fresh instead.",
            )
            return False

        try:
            backup_manager.validate_backup_zip(path)
        except backup_manager.ImportValidationError as e:
            QMessageBox.warning(self, "Not a valid backup", str(e))
            return False

        self.selected_backup_path = path
        return True


class InstallPage(QWizardPage):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle("Installing")
        self.setCommitPage(True)
        self._done = False
        self._ok = False
        self._worker = None
        self.started = False  # for wizard-cancel cleanup

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        status_row = QHBoxLayout()
        status_row.setSpacing(12)
        self.spinner = assets.LoadingSpinner(40)
        status_row.addWidget(self.spinner)
        self.status_label = QLabel("Getting SteamCMD ready…")
        self.status_label.setObjectName("SectionTitle")
        status_row.addWidget(self.status_label, 1)
        layout.addLayout(status_row)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)  # indeterminate until a percentage arrives
        self.progress_bar.setTextVisible(False)
        layout.addWidget(self.progress_bar)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view)
        self.retry_btn = QPushButton("Try Again")
        self.retry_btn.clicked.connect(self._start)
        self.retry_btn.hide()
        layout.addWidget(self.retry_btn)
        self.failure_hint = QLabel(
            "The install didn't finish. Check the log above, then click Try Again -- or go Back to "
            "pick different folders."
        )
        self.failure_hint.setObjectName("Dim")
        self.failure_hint.setWordWrap(True)
        self.failure_hint.hide()
        layout.addWidget(self.failure_hint)

    def initializePage(self) -> None:
        self._start()

    def _start(self) -> None:
        if self._worker and self._worker.isRunning():
            return
        self._done = False
        self._ok = False
        self.started = True
        self.retry_btn.hide()
        self.failure_hint.hide()
        self.completeChanged.emit()
        self.progress_bar.setRange(0, 0)  # the SteamCMD bootstrap has no percentage
        self.spinner.show()
        self.status_label.setText("Getting SteamCMD ready…")
        steamcmd_dir = self.field("steamcmd_dir")
        install_dir = self.field("install_dir")
        restore_page = getattr(self.wizard(), "restore_page", None)
        restore_backup_path = restore_page.selected_backup_path if restore_page else None
        self._worker = _InstallWorker(steamcmd_dir, install_dir, restore_backup_path=restore_backup_path, parent=self)
        self._worker.progress.connect(self.log_view.appendPlainText)
        self._worker.progress_percent.connect(self._on_progress_percent)
        self._worker.finished_ok.connect(self._on_finished)
        self._worker.start()

    def cleanupPage(self) -> None:
        """Back clicked: stop the download so going forward again can't start a
        second install into the same folder."""
        self.stop_worker()
        self._done = False
        self._ok = False
        self.retry_btn.hide()
        self.failure_hint.hide()
        super().cleanupPage()

    def _on_progress_percent(self, percent: float) -> None:
        if self.progress_bar.maximum() == 0:
            self.progress_bar.setRange(0, 100)
        pct = min(100, max(0, int(percent)))
        self.progress_bar.setValue(pct)
        self.status_label.setText(f"Downloading the Conan Exiles server… {pct}%")

    def stop_worker(self) -> None:
        """Stops the install thread on cancel/close/Back."""
        if self._worker and self._worker.isRunning():
            self._worker.requestInterruption()
            # Cancel is polled every ~0.25s; terminate gets 5s before a kill.
            self._worker.wait(7000)

    def _on_finished(self, ok: bool) -> None:
        self._done = True
        self._ok = bool(ok)
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1 if ok else 0)
        self.spinner.hide()
        self.status_label.setText("Installed." if ok else "Install failed -- see the log below.")
        self.log_view.appendPlainText("Done." if ok else "Finished with errors -- see log above.")
        self.retry_btn.setVisible(not ok)
        self.failure_hint.setVisible(not ok)
        self.completeChanged.emit()

    def isComplete(self) -> bool:
        # Only a successful install unlocks Continue.
        return self._done and self._ok


def _yes_no(flag) -> str:
    return "done" if flag else "not done"


class NetworkPage(QWizardPage):
    def __init__(self, server: ServerConfig, get_reserved_ports, parent=None):
        super().__init__(parent)
        self.server = server
        self.get_reserved_ports = get_reserved_ports
        self.setTitle("Networking")

        layout = QVBoxLayout(self)
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(self.status_label.textInteractionFlags() | Qt.TextSelectableByMouse)
        layout.addWidget(self.status_label)

        self.run_btn = QPushButton("Detect IP, ports, and set up networking")
        self.run_btn.clicked.connect(self._run)
        layout.addWidget(self.run_btn)

        self.guide_btn = QPushButton("View Port Forwarding Guide")
        self.guide_btn.clicked.connect(self._open_guide)
        self.guide_btn.setVisible(False)
        layout.addWidget(self.guide_btn)

        self.detected_ip = ""
        self.detected_game_port = 7777
        self.detected_query_port = 27015
        self._ran = False          # have a usable result
        self.touched = False       # rules/forwards may exist, so clean up on cancel
        self._auto_ran = False
        self._worker: Optional[network_setup_runner.WizardNetworkSetupWorker] = None
        self._last_public_ip: Optional[str] = None
        self._last_router_ip: Optional[str] = None
        self._last_double_nat = False
        self.on_worker_finished = None  # set by SetupWizard.reject() mid-run

    def initializePage(self) -> None:
        # QWizard calls initializePage() on every visit; auto-run only once.
        if not self._auto_ran:
            self._auto_ran = True
            self._run()

    def is_running(self) -> bool:
        return self._worker is not None

    def _run(self) -> None:
        if self._worker:
            return
        self.status_label.setText("Working... (Windows may ask for permission to add firewall rules -- click Yes.)")
        self.run_btn.setEnabled(False)
        self._ran = False
        self.completeChanged.emit()
        self.touched = True
        name = _clean_server_name(self.field("name") or self.server.name)
        install_dir = (self.field("install_dir") or "").strip()
        exe_path = ""
        if install_dir:
            import process_manager
            exe_path = process_manager.server_exe_path(install_dir)
        self._worker = network_setup_runner.WizardNetworkSetupWorker(
            self.server.id, name, self.get_reserved_ports(), exe_path=exe_path, parent=self,
        )
        self._worker.finished_setup.connect(self._on_setup_finished)
        self._worker.start()

    def _on_setup_finished(self, result: dict) -> None:
        self._worker = None
        self.run_btn.setEnabled(True)
        if self.on_worker_finished is not None:
            callback, self.on_worker_finished = self.on_worker_finished, None
            callback(result)
            return

        if result.get("error"):
            self._ran = False
            self.guide_btn.setVisible(False)
            self.status_label.setText(
                f"Networking setup didn't finish:\n{result['error']}\n\nClick the button above to try again."
            )
            self.completeChanged.emit()
            return

        self.detected_ip = result["detected_ip"]
        self.detected_game_port = result["game_port"]
        self.detected_query_port = result["query_port"]
        self._last_public_ip = result.get("public_ip")
        self._last_router_ip = result.get("router_ip")
        self._last_double_nat = bool(result.get("double_nat"))

        lines = [
            f"This PC's local address: {self.detected_ip}",
            f"Game port: {self.detected_game_port} (plus {self.detected_game_port + 1}), "
            f"Query port: {self.detected_query_port}",
            "",
        ]

        fw_results = result.get("fw_results") or []
        for r in fw_results:
            lines.append(("✓ " if r.success else "✗ ") + "Firewall: " + r.message)
        if any(not r.success for r in fw_results):
            lines.append(
                "Some firewall rules weren't added. Click the button above to try again, and click Yes "
                "if Windows asks for permission. (You can also fix this later with \"Repair Networking\" "
                "on the Network & Ports settings page.)"
            )

        upnp = result.get("upnp") or {}
        needs_manual_forwarding = not upnp.get("upnp_available")
        lines.append("")
        if not upnp.get("upnp_available"):
            lines.append("Your router didn't respond to automatic port forwarding (UPnP).")
            lines.append(
                f"Manual step needed: forward UDP ports {self.detected_game_port}, "
                f"{self.detected_game_port + 1}, and {self.detected_query_port} to {self.detected_ip} "
                f"in your router's settings -- the guide below walks through it."
            )
        else:
            lines.append(f"Router forwarding, game port {self.detected_game_port}: {_yes_no(upnp.get('game_port_forwarded'))}")
            lines.append(f"Router forwarding, port {self.detected_game_port + 1}: {_yes_no(upnp.get('game_port_plus_one_forwarded'))}")
            lines.append(f"Router forwarding, query port {self.detected_query_port}: {_yes_no(upnp.get('query_port_forwarded'))}")
            if not (upnp.get("game_port_forwarded") and upnp.get("game_port_plus_one_forwarded")
                    and upnp.get("query_port_forwarded")):
                needs_manual_forwarding = True
                if upnp.get("conflicts"):
                    ports = ", ".join(str(p) for p in upnp["conflicts"])
                    lines.append(f"Port(s) {ports} are already forwarded to another device on your network.")
                lines.append("Some ports weren't forwarded automatically -- forward those manually (see the guide).")

        if self._last_public_ip:
            lines.append(f"Your public IP (what friends connect to): {self._last_public_ip}")
        if self._last_double_nat:
            lines.append("")
            lines.append(
                "⚠ Your router's own internet address isn't a public one. That usually means a second "
                "router/modem in front of it (double NAT) or that your ISP shares one public address "
                "between customers (carrier-grade NAT). Port forwarding on this router alone won't make "
                "the server reachable: put the ISP modem in bridge mode, forward the same ports on it too, "
                "or ask your ISP for a public IP."
            )

        self.guide_btn.setVisible(needs_manual_forwarding)
        self.status_label.setText("\n".join(lines))
        self._ran = True
        self.completeChanged.emit()

    def _open_guide(self) -> None:
        from ui.port_forwarding_guide_dialog import PortForwardingGuideDialog
        dialog = PortForwardingGuideDialog(
            self.detected_game_port, self.detected_query_port, self.detected_ip,
            self._last_router_ip, self._last_public_ip, parent=self,
        )
        dialog.exec()

    def isComplete(self) -> bool:
        return self._ran and self._worker is None


class KeepRunningPage(QWizardPage):
    """Last step: one switch to set ConanOps up to run unattended."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle("Keep it running")
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        intro = QLabel("Want ConanOps to look after this server by itself, even when you're not around?")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.keep_running_checkbox = QCheckBox("Keep my servers running by themselves (recommended)")
        self.keep_running_checkbox.setChecked(True)
        self.keep_running_checkbox.setProperty("primary", True)
        layout.addWidget(self.keep_running_checkbox)

        details = QLabel(
            "This turns on:\n"
            "  •  Start ConanOps when you sign into Windows\n"
            "  •  Reopen ConanOps if it closes or crashes\n"
            "  •  Keep the PC from going to sleep while a server is running\n"
            "  •  Only restart for Windows updates between 4 and 6 AM (Windows will ask for permission once)\n\n"
            "You can change any of these later in App Settings."
        )
        details.setObjectName("Dim")
        details.setWordWrap(True)
        layout.addWidget(details)

        self.sign_in_note = QLabel(
            "One thing ConanOps can't switch on for you: in Windows, turn on \"Use my sign-in info to "
            "automatically finish setting up after an update\". Without it, the PC waits at the sign-in screen "
            "after an update restart and your servers stay down until someone signs in."
        )
        self.sign_in_note.setWordWrap(True)
        self.sign_in_btn = QPushButton("Open Sign-in Settings")
        self.sign_in_btn.clicked.connect(self._open_sign_in_settings)
        layout.addWidget(self.sign_in_note)
        row = QHBoxLayout()
        row.addWidget(self.sign_in_btn)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(1)

        self.keep_running_checkbox.toggled.connect(self._sync)
        self._sync(True)

    def _sync(self, on: bool) -> None:
        self.sign_in_note.setVisible(on)
        self.sign_in_btn.setVisible(on)

    @staticmethod
    def _open_sign_in_settings() -> None:
        import windows_update
        windows_update.open_sign_in_settings()

    @property
    def keep_running(self) -> bool:
        return self.keep_running_checkbox.isChecked()


# Left-hand step list: (label, page attribute) in page order.
_WIZARD_STEPS = [
    ("Name & folders", "paths_page"),
    ("World", "restore_page"),
    ("Install", "install_page"),
    ("Networking", "network_page"),
    ("Keep it running", "keep_running_page"),
]


class _StepsPanel(QFrame):
    """Left column: numbered steps, current highlighted, finished ticked."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("WizardSteps")
        self.setFixedWidth(210)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 18, 16, 18)
        layout.setSpacing(6)
        brand = QHBoxLayout()
        brand.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(assets.app_icon_pixmap(32))
        logo.setFixedSize(32, 32)
        brand.addWidget(logo)
        title = QLabel("Set up a server")
        title.setObjectName("AppTitle")
        brand.addWidget(title, 1)
        layout.addLayout(brand)
        layout.addSpacing(14)
        self._rows = []
        for i, (label, _attr) in enumerate(_WIZARD_STEPS):
            row = QHBoxLayout()
            row.setSpacing(10)
            badge = QLabel(str(i + 1))
            badge.setObjectName("StepBadge")
            badge.setFixedSize(24, 24)
            badge.setAlignment(Qt.AlignCenter)
            text = QLabel(label)
            text.setObjectName("StepLabel")
            row.addWidget(badge)
            row.addWidget(text, 1)
            layout.addLayout(row)
            self._rows.append((badge, text))
        layout.addStretch(1)

    def set_current(self, index: int) -> None:
        for i, (badge, text) in enumerate(self._rows):
            state = "done" if i < index else ("current" if i == index else "todo")
            badge.setText("✓" if state == "done" else str(i + 1))
            for w in (badge, text):
                w.setProperty("state", state)
                w.style().unpolish(w)
                w.style().polish(w)


class SetupWizard(QWizard):
    def __init__(self, server: ServerConfig, get_reserved_ports, get_reserved_dirs=None,
                 get_default_steamcmd_dir=None, get_reserved_names=None, get_backup_sources=None,
                 start_network_cleanup=None, parent=None):
        super().__init__(parent)
        self.server = server
        # Callable(server_id, ports, bind_ip) removing firewall rules and
        # router forwards in the background; falls back to our own worker.
        self._start_network_cleanup_cb = start_network_cleanup
        self._cleanup_workers = []
        self.setWindowTitle(f"Set Up Server — {server.name}")
        self.setWindowIcon(assets.app_icon())
        self.setMinimumSize(820, 560)
        # The Aero wizard style paints its own header and button bar, ignoring our theme.
        self.setWizardStyle(QWizard.ClassicStyle)
        self.setOption(QWizard.NoBackButtonOnStartPage, True)
        self.setTitleFormat(Qt.RichText)
        self.setButtonText(QWizard.CommitButton, "Continue")
        self.setButtonText(QWizard.FinishButton, "Finish Setup")
        self.setButtonText(QWizard.BackButton, "Back")
        self.setButtonText(QWizard.NextButton, "Next")
        # Property, not object name: QWizard owns its buttons' object names.
        for which in (QWizard.NextButton, QWizard.CommitButton, QWizard.FinishButton):
            self.button(which).setProperty("primary", True)
        self.steps_panel = _StepsPanel()
        self.setSideWidget(self.steps_panel)

        self.paths_page = PathsPage(
            server,
            get_reserved_dirs=get_reserved_dirs or (lambda: set()),
            get_default_steamcmd_dir=get_default_steamcmd_dir,
            get_reserved_names=get_reserved_names,
        )
        # Keep the title bar's server name in sync with the name field.
        self.paths_page.name_edit.textChanged.connect(
            lambda text: self.setWindowTitle(f"Set Up Server — {text.strip() or server.name}")
        )
        self.restore_page = RestorePage(get_backup_sources)
        self.install_page = InstallPage()
        self.network_page = NetworkPage(server, get_reserved_ports)
        self.keep_running_page = KeepRunningPage()

        self.addPage(self.paths_page)
        self.addPage(self.restore_page)
        self.addPage(self.install_page)
        self.addPage(self.network_page)
        self.addPage(self.keep_running_page)

        for page in (self.paths_page, self.restore_page, self.install_page, self.network_page,
                     self.keep_running_page):
            page.setTitle(f'<span style="font-size: 20px; font-weight: 600;">{page.title()}</span>')
            page.setContentsMargins(20, 6, 20, 6)
        self.currentIdChanged.connect(self._on_page_changed)
        self.steps_panel.set_current(0)

    def showEvent(self, event) -> None:  # noqa: N802 -- Qt's naming
        # Re-polish once styled so the "primary" property takes effect.
        super().showEvent(event)
        for which in (QWizard.NextButton, QWizard.CommitButton, QWizard.FinishButton):
            btn = self.button(which)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _on_page_changed(self, page_id: int) -> None:
        pages = [getattr(self, attr) for _label, attr in _WIZARD_STEPS]
        page = self.page(page_id)
        if page in pages:
            self.steps_panel.set_current(pages.index(page))

    def _start_network_cleanup(self, ports, bind_ip: str) -> None:
        if self._start_network_cleanup_cb is not None:
            self._start_network_cleanup_cb(self.server.id, ports, bind_ip)
            return
        worker = network_setup_runner.FirewallReconcileWorker(
            self.server.id, remove=True, old_ports=ports, bind_ip=bind_ip, parent=self,
        )
        self._cleanup_workers.append(worker)
        worker.finished_reconcile.connect(
            lambda *_a, w=worker: self._cleanup_workers.remove(w) if w in self._cleanup_workers else None
        )
        worker.start()

    def reject(self) -> None:
        self.install_page.stop_worker()
        net = self.network_page
        is_new_server = not self.server.install_dir
        # Never tear down an existing server's live rules.
        if is_new_server and net.touched:
            gp, qp = net.detected_game_port, net.detected_query_port
            ports = [gp, gp + 1, qp]
            if net.is_running():
                # Remove after the in-flight run finishes, rather than racing it.
                net.on_worker_finished = lambda result: self._start_network_cleanup(
                    [result.get("game_port", gp), result.get("game_port", gp) + 1, result.get("query_port", qp)],
                    result.get("detected_ip") or "",
                )
            else:
                self._start_network_cleanup(ports, net.detected_ip)

        if is_new_server and self.install_page.started:
            self._offer_to_delete_downloaded_files()
        super().reject()

    def _offer_to_delete_downloaded_files(self) -> None:
        """Offers to delete folders this setup created/filled, never a
        pre-existing or shared SteamCMD folder."""
        targets = []
        install_dir = self.field("install_dir") or ""
        steamcmd_dir = self.field("steamcmd_dir") or ""
        if install_dir and os.path.isdir(install_dir) and not self.paths_page.install_dir_preexisting:
            targets.append(install_dir)
        if steamcmd_dir and os.path.isdir(steamcmd_dir) and not self.paths_page.steamcmd_dir_preexisting:
            targets.append(steamcmd_dir)
        if not targets:
            return
        answer = QMessageBox.question(
            self, "Delete downloaded files?",
            "Setup was cancelled. Delete the files it downloaded?\n\n" + "\n".join(targets),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            return
        import shutil
        import threading

        def work(paths=tuple(targets)):
            for path in paths:
                shutil.rmtree(path, ignore_errors=True)

        threading.Thread(target=work, name="wizard-cancel-cleanup", daemon=True).start()

    def closeEvent(self, event) -> None:
        self.install_page.stop_worker()
        super().closeEvent(event)

    def apply_to_server(self) -> None:
        """Call after exec() returns Accepted to write results into the ServerConfig."""
        self.server.name = _clean_server_name(self.field("name") or "") or self.server.name
        self.server.steamcmd_dir = (self.field("steamcmd_dir") or "").strip()
        self.server.install_dir = (self.field("install_dir") or "").strip()
        if not self.server.backup_destination:
            # Without a destination, scheduled backups would never run.
            self.server.backup_destination = backup_manager.default_backup_destination(self.server.install_dir)
        self.server.bind_ip = self.network_page.detected_ip
        self.server.game_port = self.network_page.detected_game_port
        self.server.query_port = self.network_page.detected_query_port
        buildid = steamcmd_mod.get_installed_buildid(self.server.install_dir)
        if buildid:
            self.server.installed_buildid = buildid
