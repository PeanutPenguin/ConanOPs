"""
App-level settings (not per-server): the color theme, and the optional
startup PIN lock. Lives at its own "App Settings" nav entry rather than
under the per-server Settings sub-nav, since neither of these belongs
to any one server.
"""
from __future__ import annotations

import os
from typing import Callable, Optional

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QScrollArea, QFrame, QColorDialog, QCheckBox, QMessageBox, QDialog,
    QFileDialog, QApplication, QInputDialog,
)
from PySide6.QtGui import QColor
from PySide6.QtCore import QThread, Signal, QTime
from PySide6.QtWidgets import QTimeEdit, QTextBrowser, QProgressBar, QComboBox

from models import AppConfig
from theme_config import ThemePalette, DEFAULT_PALETTE, COLOR_FIELD_LABELS, FONT_FIELD_LABELS, load_theme, save_theme, theme_file_path
from ui.app_lock_dialog import SetPinDialog
import self_update
from update_runner import SelfUpdateWorker
import startup_registration
import background_mode
import powershell
import windows_update
import keep_alive
import app_updates
import update_runner
import version
import applog
import web_control

_log = applog.get_logger(__name__)


class _CallWorker(QThread):
    """Runs one blocking call (usually an elevated PowerShell script that
    waits on a Windows permission prompt) off the GUI thread."""
    finished_call = Signal(object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            result = self._fn()
        except Exception as e:  # noqa: BLE001
            _log.error(f"Background call failed: {e}")
            result = None
        self.finished_call.emit(result)


def _color_row(label_text: str, initial_hex: str):
    row = QWidget()
    row.setObjectName("TransparentRow")
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    label = QLabel(label_text)
    label.setFixedWidth(140)
    edit = QLineEdit(initial_hex)
    edit.setFixedWidth(90)
    swatch = QPushButton()
    swatch.setFixedSize(28, 28)
    swatch.setObjectName("ColorSwatch")

    def _refresh_swatch():
        color = edit.text().strip()
        swatch.setStyleSheet(f"background-color: {color}; border: 1px solid #555; border-radius: 6px;")

    def _pick():
        current = QColor(edit.text().strip())
        if not current.isValid():
            current = QColor(initial_hex)
        chosen = QColorDialog.getColor(current, row, f"Choose {label_text} color")
        if chosen.isValid():
            edit.setText(chosen.name())
            _refresh_swatch()

    edit.textChanged.connect(_refresh_swatch)
    swatch.clicked.connect(_pick)
    _refresh_swatch()

    layout.addWidget(label)
    layout.addWidget(edit)
    layout.addWidget(swatch)
    layout.addStretch(1)
    return row, edit


def _font_row(label_text: str, initial_value: str):
    row = QWidget()
    row.setObjectName("TransparentRow")
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    label = QLabel(label_text)
    label.setFixedWidth(200)
    edit = QLineEdit(initial_value)
    layout.addWidget(label)
    layout.addWidget(edit, 1)
    return row, edit


class AppSettingsPage(QWidget):
    def __init__(self, config: AppConfig, save_config: Callable[[], None],
                 on_theme_changed: Callable[[ThemePalette], None],
                 on_lock_changed: Callable[[], None],
                 install_dir: str,
                 on_update_installed: Callable[[], None],
                 web_control_server: "web_control.WebControlServer",
                 on_delete_app_only: Optional[Callable[[], None]] = None,
                 on_delete_everything: Optional[Callable[[], None]] = None,
                 parent=None):
        super().__init__(parent)
        self.config = config
        self.save_config = save_config
        self.on_theme_changed = on_theme_changed
        self.on_lock_changed = on_lock_changed
        self.install_dir = install_dir
        self.on_update_installed = on_update_installed
        self.web_control_server = web_control_server
        self.on_delete_app_only = on_delete_app_only
        self.on_delete_everything = on_delete_everything
        self._theme = load_theme()
        self._color_edits: dict = {}
        self._font_edits: dict = {}
        self._update_zip_path: Optional[str] = None
        self._self_update_worker: Optional[SelfUpdateWorker] = None
        # Keeps a finished-but-not-yet-wound-down worker referenced
        # until Qt's own QThread.finished fires, same pattern (and same
        # reason -- "QThread: Destroyed while thread is still running")
        # as MainWindow._retiring_workers.
        self._retiring_workers: list = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QHBoxLayout()
        header.setContentsMargins(24, 20, 24, 12)
        self.title_label = title = QLabel("App Settings")
        title.setObjectName("PageTitle")
        header.addWidget(title)
        header.addStretch(1)
        root.addLayout(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        container = QWidget()
        form = QVBoxLayout(container)
        form.setContentsMargins(24, 8, 24, 24)
        form.setSpacing(18)
        scroll.setWidget(container)
        root.addWidget(scroll, 1)

        # ----------------------------------------------------------- theme --
        theme_card = QFrame()
        theme_card.setObjectName("Card")
        theme_layout = QVBoxLayout(theme_card)
        theme_layout.setContentsMargins(20, 16, 20, 16)
        theme_layout.setSpacing(10)

        theme_title = QLabel("Theme")
        theme_title.setObjectName("PageTitle")
        theme_layout.addWidget(theme_title)

        theme_note = QLabel(
            f"Colors and fonts are stored in a plain JSON file at:\n{theme_file_path()}\n"
            "Edit it by hand (or share/drop in someone else's file) and restart ConanOps, "
            "or use the controls below and hit Apply."
        )
        theme_note.setObjectName("Dim")
        theme_note.setWordWrap(True)
        theme_layout.addWidget(theme_note)

        for name, label_text in COLOR_FIELD_LABELS:
            row, edit = _color_row(label_text, getattr(self._theme, name))
            self._color_edits[name] = edit
            theme_layout.addWidget(row)

        for name, label_text in FONT_FIELD_LABELS:
            row, edit = _font_row(label_text, getattr(self._theme, name))
            self._font_edits[name] = edit
            theme_layout.addWidget(row)

        theme_btn_row = QHBoxLayout()
        reset_btn = QPushButton("Reset to Defaults")
        reset_btn.clicked.connect(self._reset_theme_fields)
        apply_btn = QPushButton("Apply Theme")
        apply_btn.setObjectName("PrimaryButton")
        apply_btn.clicked.connect(self._apply_theme)
        theme_btn_row.addWidget(reset_btn)
        theme_btn_row.addStretch(1)
        theme_btn_row.addWidget(apply_btn)
        theme_layout.addLayout(theme_btn_row)

        form.addWidget(theme_card)

        # --------------------------------------------------------- startup --
        startup_card = QFrame()
        startup_card.setObjectName("Card")
        startup_layout = QVBoxLayout(startup_card)
        startup_layout.setContentsMargins(20, 16, 20, 16)
        startup_layout.setSpacing(10)

        startup_title = QLabel("Startup")
        startup_title.setObjectName("PageTitle")
        startup_layout.addWidget(startup_title)

        startup_note = QLabel(
            "Open ConanOps automatically when you sign into Windows, so your servers come back after a restart."
        )
        startup_note.setObjectName("Dim")
        startup_note.setWordWrap(True)
        startup_layout.addWidget(startup_note)

        self.start_with_windows_checkbox = QCheckBox("Start ConanOps when I sign into Windows")
        self.start_with_windows_checkbox.setChecked(self.config.start_with_windows)
        self.start_with_windows_checkbox.toggled.connect(self._on_start_with_windows_toggled)
        startup_layout.addWidget(self.start_with_windows_checkbox)

        self.start_minimized_checkbox = QCheckBox("Start minimized to the tray")
        self.start_minimized_checkbox.setChecked(self.config.start_minimized_to_tray)
        self.start_minimized_checkbox.toggled.connect(self._on_start_minimized_toggled)
        startup_layout.addWidget(self.start_minimized_checkbox)

        form.addWidget(startup_card)

        # --------------------------------------------- unattended mode --
        unattended_card = QFrame()
        unattended_card.setObjectName("Card")
        ul = QVBoxLayout(unattended_card)
        ul.setContentsMargins(20, 16, 20, 16)
        ul.setSpacing(10)
        ut = QLabel("Unattended Operation")
        ut.setObjectName("PageTitle")
        ul.addWidget(ut)

        self.background_checkbox = QCheckBox("Keep my servers running when nobody is signed into Windows")
        self.background_checkbox.setChecked(self.config.background_mode_enabled)
        self.background_checkbox.toggled.connect(self._on_background_toggled)
        ul.addWidget(self.background_checkbox)
        bg_note = QLabel(
            "After a power cut, a Windows Update restart, or signing out, ConanOps starts by itself in the "
            "background (no window) and brings your servers back -- nobody has to sign in. Opening ConanOps "
            "normally takes over from the background copy without stopping anything. Turning this on or off "
            "asks for Windows permission once."
        )
        bg_note.setObjectName("Dim")
        bg_note.setWordWrap(True)
        ul.addWidget(bg_note)
        show_bg = background_mode.AVAILABLE or self.config.background_mode_enabled
        self.background_checkbox.setVisible(show_bg)
        bg_note.setVisible(show_bg)
        if not background_mode.AVAILABLE and self.config.background_mode_enabled:
            bg_note.setText(
                "This option is being retired: it can interfere with how Windows protects saved passwords. "
                "Untick it to remove it. Turn on \"Start ConanOps when I sign into Windows\" instead."
            )
        self.background_status_label = QLabel("")
        self.background_status_label.setObjectName("ErrorText")
        self.background_status_label.setWordWrap(True)
        self.background_status_label.hide()
        ul.addWidget(self.background_status_label)
        self.background_fix_btn = QPushButton("Fix Background Task")
        self.background_fix_btn.hide()
        self.background_fix_btn.clicked.connect(lambda: self._apply_background(True))
        ul.addWidget(self.background_fix_btn)

        self.keep_alive_checkbox = QCheckBox("Reopen ConanOps if it closes or crashes")
        self.keep_alive_checkbox.setChecked(self.config.keep_alive_enabled)
        self.keep_alive_checkbox.toggled.connect(self._on_keep_alive_toggled)
        ul.addWidget(self.keep_alive_checkbox)
        ka_note = QLabel(
            "While you're signed in, a small Windows task checks every minute and reopens ConanOps (quietly, "
            "in the tray) if it stopped, so backups, updates and crash restarts keep happening. Choosing Quit "
            "from the tray icon still closes it for good until you open it again."
        )
        ka_note.setObjectName("Dim")
        ka_note.setWordWrap(True)
        ul.addWidget(ka_note)

        self.keep_awake_checkbox = QCheckBox("Keep this PC awake while a server is running")
        self.keep_awake_checkbox.setChecked(self.config.keep_pc_awake)
        self.keep_awake_checkbox.toggled.connect(self._on_keep_awake_toggled)
        ul.addWidget(self.keep_awake_checkbox)
        awake_note = QLabel(
            "Stops Windows from going to sleep on its own while a server runs (your power settings aren't "
            "changed). It can't stop the sleep button or a laptop lid closing."
        )
        awake_note.setObjectName("Dim")
        awake_note.setWordWrap(True)
        ul.addWidget(awake_note)

        self.update_restart_checkbox = QCheckBox("Only restart for Windows updates during this window:")
        self.update_restart_checkbox.setChecked(self.config.handle_update_restarts)
        self.update_restart_checkbox.toggled.connect(self._on_update_restart_toggled)
        ul.addWidget(self.update_restart_checkbox)
        window_row = QHBoxLayout()
        self.update_start_edit = QTimeEdit(QTime.fromString(self.config.update_restart_start, "HH:mm"))
        self.update_start_edit.setDisplayFormat("HH:mm")
        self.update_end_edit = QTimeEdit(QTime.fromString(self.config.update_restart_end, "HH:mm"))
        self.update_end_edit.setDisplayFormat("HH:mm")
        window_row.addWidget(QLabel("From"))
        window_row.addWidget(self.update_start_edit)
        window_row.addWidget(QLabel("to"))
        window_row.addWidget(self.update_end_edit)
        window_row.addStretch(1)
        ul.addLayout(window_row)
        self.update_start_edit.editingFinished.connect(self._on_update_window_changed)
        self.update_end_edit.editingFinished.connect(self._on_update_window_changed)
        upd_note = QLabel(
            "When Windows needs to restart after updates, ConanOps waits for this window and for everyone to "
            "log off, saves and stops your servers, restarts the PC, and starts the servers again afterwards. "
            "It also sets Windows' \"active hours\" so Windows itself avoids restarting outside this window "
            "(Windows allows at most 18 active hours, so with a short window it may still pick a nearby hour). "
            "Servers come back after the restart once ConanOps starts again -- turn on starting with Windows, "
            "and Windows' \"Use my sign-in info to automatically finish setting up after an update\" so it "
            "signs back in by itself."
        )
        upd_note.setObjectName("Dim")
        upd_note.setWordWrap(True)
        ul.addWidget(upd_note)

        sign_in_row = QHBoxLayout()
        self.sign_in_status_label = QLabel("")
        self.sign_in_status_label.setWordWrap(True)
        self.sign_in_settings_btn = QPushButton("Open Sign-in Settings")
        self.sign_in_settings_btn.clicked.connect(windows_update.open_sign_in_settings)
        sign_in_row.addWidget(self.sign_in_status_label, 1)
        sign_in_row.addWidget(self.sign_in_settings_btn)
        ul.addLayout(sign_in_row)
        self.sign_in_status_label.hide()
        self.sign_in_settings_btn.hide()

        mod_row = QHBoxLayout()
        mod_row.addWidget(QLabel("If a mod stops a server from starting:"))
        self.mod_recovery_combo = QComboBox()
        self.mod_recovery_combo.addItem("Find it, keep the server stopped and wait for a fix", "wait")
        self.mod_recovery_combo.addItem("Find it and start without it (its items are removed)", "start_without")
        self.mod_recovery_combo.addItem("Just tell me", "alert")
        idx = self.mod_recovery_combo.findData(self.config.mod_recovery_mode)
        self.mod_recovery_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.mod_recovery_combo.currentIndexChanged.connect(self._on_mod_recovery_changed)
        mod_row.addWidget(self.mod_recovery_combo, 1)
        ul.addLayout(mod_row)
        mod_note = QLabel(
            "ConanOps finds the broken mod by itself with your world protected. Waiting keeps everything that "
            "mod added to your world -- the server starts again on its own once the mod's author updates it. "
            "Starting without it gets players back in sooner, but its buildings and items are deleted (a backup "
            "is made first)."
        )
        mod_note.setObjectName("Dim")
        mod_note.setWordWrap(True)
        ul.addWidget(mod_note)

        tour_row = QHBoxLayout()
        self.tour_btn = QPushButton("Show the Dashboard Tour")
        self.tour_btn.clicked.connect(lambda: self.on_show_tour() if callable(self.on_show_tour) else None)
        tour_row.addWidget(self.tour_btn)
        tour_row.addStretch(1)
        ul.addLayout(tour_row)
        self.on_show_tour = None

        form.addWidget(unattended_card)
        self._call_workers = []
        if self.config.background_mode_enabled:
            self._run_call(background_mode.status, self._on_background_status)
        self.refresh_sign_in_status()

        # ----------------------------------------------- steam workshop --
        workshop_card = QFrame()
        workshop_card.setObjectName("Card")
        workshop_layout = QVBoxLayout(workshop_card)
        workshop_layout.setContentsMargins(20, 16, 20, 16)
        workshop_layout.setSpacing(10)

        workshop_title = QLabel("Steam Workshop")
        workshop_title.setObjectName("PageTitle")
        workshop_layout.addWidget(workshop_title)

        workshop_note = QLabel(
            'Lets the Mods tab search/browse the Workshop instead of needing a mod\'s ID typed in by '
            'hand. Needs your own free Steam Web API key -- ConanOps can\'t ship one built in (sharing '
            'one key across everyone who has ConanOps would blow through Steam\'s rate limit almost '
            'immediately, and publishing a personal key like that is against Steam\'s own terms). Get '
            'one at <a href="https://steamcommunity.com/dev/apikey">steamcommunity.com/dev/apikey</a> '
            '-- takes under a minute, just needs you to be signed into Steam.'
        )
        workshop_note.setObjectName("Dim")
        workshop_note.setWordWrap(True)
        workshop_note.setOpenExternalLinks(True)
        workshop_layout.addWidget(workshop_note)

        self.steam_api_key_edit = QLineEdit()
        self.steam_api_key_edit.setEchoMode(QLineEdit.Password)
        self.steam_api_key_edit.setText(self.config.steam_api_key)
        self.steam_api_key_edit.setPlaceholderText("Paste your Steam Web API key here")
        self.steam_api_key_edit.editingFinished.connect(self._on_steam_api_key_changed)
        workshop_layout.addWidget(self.steam_api_key_edit)

        cutoff_label = QLabel(
            "Mods count as updated for the current game patch if they were updated on or after this "
            "date (YYYY-MM-DD). Change it when a new patch requires mods to be rebuilt."
        )
        cutoff_label.setObjectName("Dim")
        cutoff_label.setWordWrap(True)
        workshop_layout.addWidget(cutoff_label)
        self.workshop_cutoff_edit = QLineEdit()
        self.workshop_cutoff_edit.setText(self.config.workshop_update_cutoff)
        self.workshop_cutoff_edit.setPlaceholderText("2026-09-01")
        self.workshop_cutoff_edit.setMaxLength(10)
        self.workshop_cutoff_edit.editingFinished.connect(self._on_workshop_cutoff_changed)
        workshop_layout.addWidget(self.workshop_cutoff_edit)

        form.addWidget(workshop_card)

        # -------------------------------------------------------- dynamic dns --
        duckdns_card = QFrame()
        duckdns_card.setObjectName("Card")
        duckdns_layout = QVBoxLayout(duckdns_card)
        duckdns_layout.setContentsMargins(20, 16, 20, 16)
        duckdns_layout.setSpacing(10)

        duckdns_title = QLabel("Dynamic DNS")
        duckdns_title.setObjectName("PageTitle")
        duckdns_layout.addWidget(duckdns_title)

        duckdns_note = QLabel(
            'Keeps a DuckDNS domain pointed at this PC\'s current public IP -- useful if your '
            'internet provider doesn\'t give you a static one, since players\' saved server entries '
            'would otherwise silently break whenever it changes. Free account at '
            '<a href="https://www.duckdns.org">duckdns.org</a> -- sign in, add a domain, and copy '
            'your token from the same page.'
        )
        duckdns_note.setObjectName("Dim")
        duckdns_note.setWordWrap(True)
        duckdns_note.setOpenExternalLinks(True)
        duckdns_layout.addWidget(duckdns_note)

        duckdns_row = QHBoxLayout()
        self.duckdns_domain_edit = QLineEdit()
        self.duckdns_domain_edit.setText(self.config.duckdns_domain)
        self.duckdns_domain_edit.setPlaceholderText("yourdomain (without .duckdns.org)")
        self.duckdns_domain_edit.editingFinished.connect(self._on_duckdns_settings_changed)
        duckdns_row.addWidget(self.duckdns_domain_edit)
        duckdns_layout.addLayout(duckdns_row)

        self.duckdns_token_edit = QLineEdit()
        self.duckdns_token_edit.setEchoMode(QLineEdit.Password)
        self.duckdns_token_edit.setText(self.config.duckdns_token)
        self.duckdns_token_edit.setPlaceholderText("Paste your DuckDNS token here")
        self.duckdns_token_edit.editingFinished.connect(self._on_duckdns_settings_changed)
        duckdns_layout.addWidget(self.duckdns_token_edit)

        form.addWidget(duckdns_card)

        # -------------------------------------------------------- app lock --
        lock_card = QFrame()
        lock_card.setObjectName("Card")
        lock_layout = QVBoxLayout(lock_card)
        lock_layout.setContentsMargins(20, 16, 20, 16)
        lock_layout.setSpacing(10)

        lock_title = QLabel("Startup PIN Lock")
        lock_title.setObjectName("PageTitle")
        lock_layout.addWidget(lock_title)

        lock_note = QLabel(
            "Optional. When on, ConanOps asks for this PIN before it opens -- it has full "
            "RCON access and can ban players, force restarts, and apply updates, so this is "
            "meant to stop a glance at an unlocked screen, not a determined attacker. "
            "Off by default; you can turn it on or off at any time, here or from the tray icon."
        )
        lock_note.setObjectName("Dim")
        lock_note.setWordWrap(True)
        lock_layout.addWidget(lock_note)

        self.lock_checkbox = QCheckBox("Require a PIN when ConanOps starts")
        self.lock_checkbox.setChecked(self.config.app_lock_enabled)
        self.lock_checkbox.toggled.connect(self._on_lock_toggled)
        lock_layout.addWidget(self.lock_checkbox)

        self.change_pin_btn = QPushButton("Change PIN…")
        self.change_pin_btn.setEnabled(self.config.app_lock_enabled)
        self.change_pin_btn.clicked.connect(self._change_pin)
        lock_layout.addWidget(self.change_pin_btn)

        form.addWidget(lock_card)

        # ------------------------------------------------------ web control --
        web_card = QFrame()
        web_card.setObjectName("Card")
        web_layout = QVBoxLayout(web_card)
        web_layout.setContentsMargins(20, 16, 20, 16)
        web_layout.setSpacing(10)

        web_title = QLabel("Web Control")
        web_title.setObjectName("PageTitle")
        web_layout.addWidget(web_title)

        web_note = QLabel(
            "Opens a small status/control page in a browser (Chrome, Firefox, etc.) for "
            "whichever server is active in ConanOps -- view status, and start, stop, or "
            "restart it. Off by default. Reachable from other devices on your network "
            "(like your phone), not just this PC. No password is required, so anyone with "
            "the link can control your server -- Windows may ask to allow this through your "
            "firewall the first time."
        )
        web_note.setObjectName("Dim")
        web_note.setWordWrap(True)
        web_layout.addWidget(web_note)

        self.web_control_checkbox = QCheckBox("Enable local web control")
        self.web_control_checkbox.setChecked(self.config.web_control_enabled)
        self.web_control_checkbox.toggled.connect(self._on_web_control_toggled)
        web_layout.addWidget(self.web_control_checkbox)

        link_row = QHBoxLayout()
        self.web_control_link_label = QLabel("Not currently running.")
        self.web_control_link_label.setObjectName("Muted")
        self.copy_link_btn = QPushButton("Copy Link")
        self.copy_link_btn.setEnabled(False)
        self.copy_link_btn.clicked.connect(self._copy_web_control_link)
        link_row.addWidget(self.web_control_link_label, 1)
        link_row.addWidget(self.copy_link_btn)
        web_layout.addLayout(link_row)

        form.addWidget(web_card)

        # ------------------------------------------------------------ update --
        update_card = QFrame()
        update_card.setObjectName("Card")
        update_layout = QVBoxLayout(update_card)
        update_layout.setContentsMargins(20, 16, 20, 16)
        update_layout.setSpacing(10)

        update_title = QLabel("Update ConanOps")
        update_title.setObjectName("PageTitle")
        update_layout.addWidget(update_title)

        self.current_version_label = QLabel(f"Current version: {version.VERSION}")
        self.current_version_label.setObjectName("Dim")
        update_layout.addWidget(self.current_version_label)

        online = bool(app_updates.update_repo())

        self.app_update_status = QLabel(
            "Checking for updates…" if online else
            "Pick an update file you've already downloaded, then click Update Now."
        )
        self.app_update_status.setWordWrap(True)
        update_layout.addWidget(self.app_update_status)

        self.app_update_notes = QTextBrowser()
        self.app_update_notes.setOpenExternalLinks(True)
        self.app_update_notes.setMaximumHeight(170)
        self.app_update_notes.hide()
        update_layout.addWidget(self.app_update_notes)

        self.app_update_progress = QProgressBar()
        self.app_update_progress.setTextVisible(False)
        self.app_update_progress.hide()
        update_layout.addWidget(self.app_update_progress)

        online_row = QHBoxLayout()
        self.check_updates_btn = QPushButton("Check for Updates")
        self.check_updates_btn.clicked.connect(lambda: self.check_for_updates(automatic=False))
        self.install_online_btn = QPushButton("Download and Install")
        self.install_online_btn.setObjectName("PrimaryButton")
        self.install_online_btn.clicked.connect(lambda: self._install_online_update(confirm=True))
        self.install_online_btn.hide()
        online_row.addWidget(self.check_updates_btn)
        online_row.addWidget(self.install_online_btn)
        online_row.addStretch(1)
        update_layout.addLayout(online_row)

        self.auto_check_updates_checkbox = QCheckBox("Check for updates automatically")
        self.auto_check_updates_checkbox.setChecked(self.config.auto_check_app_updates)
        self.auto_check_updates_checkbox.toggled.connect(self._on_auto_check_updates_toggled)
        update_layout.addWidget(self.auto_check_updates_checkbox)
        self.auto_install_updates_checkbox = QCheckBox(
            "Install updates automatically (ConanOps restarts itself -- your servers keep running)"
        )
        self.auto_install_updates_checkbox.setChecked(self.config.auto_install_app_updates)
        self.auto_install_updates_checkbox.toggled.connect(self._on_auto_install_updates_toggled)
        update_layout.addWidget(self.auto_install_updates_checkbox)
        for w in (self.check_updates_btn, self.auto_check_updates_checkbox, self.auto_install_updates_checkbox):
            w.setVisible(online)

        file_note = QLabel(
            "Or install from an update file you've downloaded yourself. Either way, ConanOps backs up the "
            "current version first, closes and reopens itself to finish, and puts the old version back "
            "automatically if the new one won't start."
        )
        file_note.setObjectName("Dim")
        file_note.setWordWrap(True)
        update_layout.addWidget(file_note)

        picker_row = QHBoxLayout()
        choose_btn = QPushButton("Choose Update File…")
        choose_btn.clicked.connect(self._choose_update_file)
        self.update_file_label = QLabel("No file selected")
        self.update_file_label.setObjectName("Muted")
        picker_row.addWidget(choose_btn)
        picker_row.addWidget(self.update_file_label, 1)
        update_layout.addLayout(picker_row)

        self.update_now_btn = QPushButton("Update Now")
        self.update_now_btn.setEnabled(False)
        self.update_now_btn.clicked.connect(self._confirm_and_update)
        update_layout.addWidget(self.update_now_btn)

        # Hooks MainWindow sets: tell the person (tray toast) an update is
        # out, and whether it's safe to restart ConanOps right now.
        self.on_app_update_available = None
        self.is_busy_for_app_update = None
        self._available_release = None
        self._app_update_check_worker = None
        self._app_update_install_worker = None

        form.addWidget(update_card)

        # ------------------------------------------------------- delete --
        delete_card = QFrame()
        delete_card.setObjectName("Card")
        delete_layout = QVBoxLayout(delete_card)
        delete_layout.setContentsMargins(20, 16, 20, 16)
        delete_layout.setSpacing(10)

        delete_title = QLabel("Delete ConanOps")
        delete_title.setObjectName("PageTitle")
        delete_layout.addWidget(delete_title)

        delete_note = QLabel(
            "Both options close ConanOps immediately once confirmed and can't be undone."
        )
        delete_note.setObjectName("Dim")
        delete_note.setWordWrap(True)
        delete_layout.addWidget(delete_note)

        self.delete_app_only_btn = QPushButton("Uninstall ConanOps (Keep My Data)")
        self.delete_app_only_btn.clicked.connect(self._confirm_delete_app_only)
        delete_layout.addWidget(self.delete_app_only_btn)

        keep_note = QLabel(
            "Removes ConanOps' own program files only. Your servers' install folders, "
            "SteamCMD folders, backups, and Windows Firewall rules are left exactly as they "
            "are -- reinstalling ConanOps later will find them again."
        )
        keep_note.setObjectName("Dim")
        keep_note.setWordWrap(True)
        delete_layout.addWidget(keep_note)

        self.delete_everything_btn = QPushButton("Delete Everything (App + All Server Data)")
        self.delete_everything_btn.setObjectName("DangerButton")
        self.delete_everything_btn.clicked.connect(self._confirm_delete_everything)
        delete_layout.addWidget(self.delete_everything_btn)

        everything_note = QLabel(
            "Puts this PC back the way it was before ConanOps: every server and its files, SteamCMD, "
            "backups, firewall rules, router port forwards, the startup entry, Windows Update hours, "
            "temporary files and ConanOps itself. This deletes your world saves -- there is no undo."
        )
        everything_note.setObjectName("Dim")
        everything_note.setWordWrap(True)
        delete_layout.addWidget(everything_note)

        form.addWidget(delete_card)
        form.addStretch(1)

        self._refresh_web_control_ui()

    # ------------------------------------------------------------- theme --
    def _reset_theme_fields(self) -> None:
        for name, edit in self._color_edits.items():
            edit.setText(getattr(DEFAULT_PALETTE, name))
        for name, edit in self._font_edits.items():
            edit.setText(getattr(DEFAULT_PALETTE, name))

    def _apply_theme(self) -> None:
        values = {name: edit.text().strip() for name, edit in self._color_edits.items()}
        values.update({name: edit.text().strip() for name, edit in self._font_edits.items()})

        for name in self._color_edits:
            if not QColor(values[name]).isValid():
                QMessageBox.warning(self, "Invalid color", f"'{values[name]}' isn't a valid color (use #rrggbb).")
                return
            # Normalize whatever QColor accepted (a color NAME like
            # "red", 3-digit shorthand like "#fff", etc.) down to the
            # strict #rrggbb form theme_config.load_theme() requires.
            # QColor is intentionally more permissive than the loader's
            # own validation -- that's what lets someone type "red" or
            # pick a color from the picker at all -- but saving it
            # un-normalized used to mean Apply visibly worked for the
            # rest of THIS session (on_theme_changed applies the
            # in-memory ThemePalette either way) while silently
            # reverting to the default on the next app restart, once
            # load_theme() re-read the same value from theme.json and
            # rejected it.
            values[name] = QColor(values[name]).name()

        palette = ThemePalette(**values)
        save_theme(palette)
        self._theme = palette
        self.on_theme_changed(palette)

    # --------------------------------------------------------------- lock --
    def _on_start_with_windows_toggled(self, checked: bool) -> None:
        try:
            if checked:
                startup_registration.register()
            else:
                startup_registration.unregister()
        except OSError as e:
            _log.error(f"Couldn't update Windows startup registration: {e}")
            QMessageBox.critical(
                self, "Couldn't Update Startup Setting",
                f"ConanOps couldn't {'register for' if checked else 'unregister from'} Windows startup: {e}",
            )
            self.start_with_windows_checkbox.blockSignals(True)
            self.start_with_windows_checkbox.setChecked(not checked)
            self.start_with_windows_checkbox.blockSignals(False)
            return
        self.config.start_with_windows = checked
        self.save_config()

    # ------------------------------------------------------- unattended --
    def _run_call(self, fn, on_done) -> None:
        worker = _CallWorker(fn, parent=self)
        self._call_workers.append(worker)
        worker.finished_call.connect(on_done)
        worker.finished.connect(lambda w=worker: self._call_workers.remove(w) if w in self._call_workers else None)
        worker.start()

    def _set_checked_quietly(self, box: QCheckBox, value: bool) -> None:
        box.blockSignals(True)
        box.setChecked(value)
        box.blockSignals(False)

    def _on_background_toggled(self, checked: bool) -> None:
        self._apply_background(checked)

    def _apply_background(self, enable: bool) -> None:
        self.background_checkbox.setEnabled(False)
        self.background_fix_btn.setEnabled(False)

        def done(outcome, enable=enable):
            self.background_checkbox.setEnabled(True)
            self.background_fix_btn.setEnabled(True)
            if outcome == powershell.RUN_OK:
                self.config.background_mode_enabled = enable
                self.save_config()
                self.background_status_label.hide()
                self.background_fix_btn.hide()
                if enable and not self.config.start_with_windows:
                    _log.info("Background mode on; sign-in startup is off (the background copy covers boot).")
                return
            self._set_checked_quietly(self.background_checkbox, self.config.background_mode_enabled)
            if outcome == powershell.RUN_DECLINED:
                return  # a declined prompt is a normal choice -- nothing to explain
            QMessageBox.warning(
                self, "Couldn't Change Background Mode",
                "Windows didn't accept the change -- see conanops.log for details.",
            )

        self._run_call(background_mode.register if enable else background_mode.unregister, done)

    def _on_background_status(self, status) -> None:
        if status is None or not self.config.background_mode_enabled:
            return  # couldn't check (the task may not be readable by this account) -- stay quiet
        if not status.get("exists"):
            msg = "The background task is missing (it may have been removed) -- servers won't come back by themselves."
        elif not status.get("matches"):
            msg = "The background task points at an old copy of ConanOps (it was moved or reinstalled)."
        else:
            return
        self.background_status_label.setText(msg)
        self.background_status_label.show()
        self.background_fix_btn.show()

    def _on_keep_alive_toggled(self, checked: bool) -> None:
        self.keep_alive_checkbox.setEnabled(False)

        def done(ok, want=checked):
            self.keep_alive_checkbox.setEnabled(True)
            if ok:
                self.config.keep_alive_enabled = want
                self.save_config()
                return
            self._set_checked_quietly(self.keep_alive_checkbox, self.config.keep_alive_enabled)
            QMessageBox.warning(self, "Couldn't Change This",
                                "Windows didn't accept the change -- see conanops.log for details.")

        self._run_call(keep_alive.enable if checked else keep_alive.disable, done)

    def refresh_sign_in_status(self) -> None:
        """Checks (off the UI thread) whether Windows signs back in after
        update restarts, and shows a warning + shortcut when it doesn't."""
        self._run_call(windows_update.auto_sign_in_status, self._on_sign_in_status)

    def _on_sign_in_status(self, status) -> None:
        texts = {
            windows_update.AUTO_SIGN_IN_OFF:
                "⚠ Windows won't sign you back in after an update restart, so servers stay down until someone "
                "signs in. Turn on \"Use my sign-in info to automatically finish setting up after an update\".",
            windows_update.AUTO_SIGN_IN_BLOCKED:
                "⚠ A policy on this PC stops Windows signing you back in after update restarts -- servers stay "
                "down after one until someone signs in.",
            windows_update.AUTO_SIGN_IN_UNKNOWN:
                "Make sure \"Use my sign-in info to automatically finish setting up after an update\" is on in "
                "Windows, so servers come back after update restarts without anyone signing in.",
        }
        text = texts.get(status)
        self.sign_in_status_label.setText(text or "")
        self.sign_in_status_label.setVisible(bool(text))
        self.sign_in_settings_btn.setVisible(bool(text) and status != windows_update.AUTO_SIGN_IN_BLOCKED)

    def reload_unattended_from_config(self) -> None:
        """Re-reads the start/keep-running settings after something other
        than this page changed them (the setup wizard's last step)."""
        for box, value in ((self.start_with_windows_checkbox, self.config.start_with_windows),
                           (self.keep_alive_checkbox, self.config.keep_alive_enabled),
                           (self.keep_awake_checkbox, self.config.keep_pc_awake),
                           (self.update_restart_checkbox, self.config.handle_update_restarts)):
            self._set_checked_quietly(box, value)
        self.refresh_sign_in_status()

    def _on_mod_recovery_changed(self, _index: int) -> None:
        self.config.mod_recovery_mode = self.mod_recovery_combo.currentData() or "wait"
        self.save_config()

    def _on_keep_awake_toggled(self, checked: bool) -> None:
        self.config.keep_pc_awake = checked
        self.save_config()

    def _apply_active_hours(self, on_done) -> None:
        hours = windows_update.compute_active_hours(
            self.update_start_edit.time().toString("HH:mm"), self.update_end_edit.time().toString("HH:mm"),
        )
        if hours is None:
            QMessageBox.warning(self, "Window Too Long", "Leave at least one hour outside the restart window.")
            on_done(powershell.RUN_FAILED)
            return
        if not self.config.original_active_hours:
            # First change: remember what Windows had, so it can be put back.
            self.config.original_active_hours = windows_update.read_active_hours()
            self.save_config()
        self._run_call(lambda h=hours: windows_update.set_active_hours(*h), on_done)

    def _on_update_restart_toggled(self, checked: bool) -> None:
        if not checked:
            self.config.handle_update_restarts = False
            self.save_config()
            if self.config.original_active_hours:
                original = dict(self.config.original_active_hours)

                def restored(outcome):
                    if outcome == powershell.RUN_OK:
                        self.config.original_active_hours = {}
                        self.save_config()

                self._run_call(lambda: windows_update.restore_active_hours(original), restored)
            return
        self.update_restart_checkbox.setEnabled(False)

        def done(outcome):
            self.update_restart_checkbox.setEnabled(True)
            if outcome == powershell.RUN_OK:
                self.config.handle_update_restarts = True
                self._save_update_window()
            else:
                self._set_checked_quietly(self.update_restart_checkbox, False)

        self._apply_active_hours(done)

    def _save_update_window(self) -> None:
        self.config.update_restart_start = self.update_start_edit.time().toString("HH:mm")
        self.config.update_restart_end = self.update_end_edit.time().toString("HH:mm")
        self.save_config()

    def _on_update_window_changed(self) -> None:
        start = self.update_start_edit.time().toString("HH:mm")
        end = self.update_end_edit.time().toString("HH:mm")
        if (start, end) == (self.config.update_restart_start, self.config.update_restart_end):
            return
        if not self.config.handle_update_restarts:
            self._save_update_window()
            return
        self._apply_active_hours(lambda outcome: self._save_update_window() if outcome == powershell.RUN_OK else None)

    def _on_start_minimized_toggled(self, checked: bool) -> None:
        self.config.start_minimized_to_tray = checked
        self.save_config()

    def _on_steam_api_key_changed(self) -> None:
        self.config.steam_api_key = self.steam_api_key_edit.text().strip()
        self.save_config()

    def _on_workshop_cutoff_changed(self) -> None:
        from datetime import datetime
        text = self.workshop_cutoff_edit.text().strip()
        try:
            datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            # Put the last good value back rather than saving something
            # the Workshop filter can't use.
            self.workshop_cutoff_edit.setText(self.config.workshop_update_cutoff)
            return
        self.config.workshop_update_cutoff = text
        self.save_config()

    def _on_duckdns_settings_changed(self) -> None:
        self.config.duckdns_domain = self.duckdns_domain_edit.text().strip()
        self.config.duckdns_token = self.duckdns_token_edit.text().strip()
        self.save_config()

    def _on_lock_toggled(self, checked: bool) -> None:
        if checked and not self.config.app_lock_enabled:
            dialog = SetPinDialog(current_pin_set=False, verify_fn=self.config.verify_app_lock_pin, parent=self)
            if dialog.exec() != QDialog.Accepted or not dialog.new_pin:
                self.lock_checkbox.blockSignals(True)
                self.lock_checkbox.setChecked(False)
                self.lock_checkbox.blockSignals(False)
                return
            self.config.set_app_lock_pin(dialog.new_pin)
            self.save_config()
            self.change_pin_btn.setEnabled(True)
            self.on_lock_changed()
        elif not checked and self.config.app_lock_enabled:
            reply = QMessageBox.warning(
                self, "Remove PIN lock?",
                "ConanOps will no longer ask for a PIN on startup.",
                QMessageBox.Cancel | QMessageBox.Yes, QMessageBox.Cancel,
            )
            if reply != QMessageBox.Yes:
                self.lock_checkbox.blockSignals(True)
                self.lock_checkbox.setChecked(True)
                self.lock_checkbox.blockSignals(False)
                return
            self.config.clear_app_lock_pin()
            self.save_config()
            self.change_pin_btn.setEnabled(False)
            self.on_lock_changed()

    def _change_pin(self) -> None:
        dialog = SetPinDialog(current_pin_set=True, verify_fn=self.config.verify_app_lock_pin, parent=self)
        if dialog.exec() != QDialog.Accepted or dialog.new_pin is None:
            return
        if dialog.new_pin:
            self.config.set_app_lock_pin(dialog.new_pin)
        else:
            self.config.clear_app_lock_pin()
            self.lock_checkbox.blockSignals(True)
            self.lock_checkbox.setChecked(False)
            self.lock_checkbox.blockSignals(False)
            self.change_pin_btn.setEnabled(False)
        self.save_config()
        self.on_lock_changed()

    # ------------------------------------------------------------- update --
    # ------------------------------------------------- online updates --
    def check_for_updates(self, automatic: bool = False) -> None:
        """Asks GitHub for a newer release (off the GUI thread). automatic
        checks stay quiet on errors and can install by themselves when
        that's turned on."""
        if not app_updates.update_repo() or self._app_update_check_worker is not None \
                or self._app_update_install_worker is not None:
            return
        self.check_updates_btn.setEnabled(False)
        if not automatic or self._available_release is None:
            self.app_update_status.setText("Checking for updates…")
        worker = update_runner.AppUpdateCheckWorker(self)
        self._app_update_check_worker = worker
        self._retiring_workers.append(worker)
        worker.finished.connect(lambda w=worker: self._retiring_workers.remove(w) if w in self._retiring_workers else None)
        worker.finished_check.connect(lambda info, err, a=automatic: self._on_update_checked(info, err, a))
        worker.start()

    def _on_update_checked(self, info, error: str, automatic: bool) -> None:
        self._app_update_check_worker = None
        self.check_updates_btn.setEnabled(True)
        if error:
            if automatic and self._available_release is None:
                self.app_update_status.setText(f"Current version {version.VERSION}. Couldn't check for updates just now.")
            elif not automatic:
                self.app_update_status.setText(error)
            return
        self._available_release = info
        if info is None:
            self.install_online_btn.hide()
            self.app_update_notes.hide()
            self.app_update_status.setText(f"You're up to date (version {version.VERSION}).")
            return
        self.app_update_status.setText(f"Version {info.version} is available (you have {version.VERSION}).")
        notes = info.notes or "No release notes."
        if info.page_url:
            notes += f"\n\n[Full release page]({info.page_url})"
        self.app_update_notes.setMarkdown(notes)
        self.app_update_notes.show()
        self.install_online_btn.show()
        if automatic:
            if callable(self.on_app_update_available):
                self.on_app_update_available(info)
            if self.config.auto_install_app_updates:
                self._install_online_update(confirm=False)

    def _install_online_update(self, confirm: bool) -> None:
        info = self._available_release
        if info is None or self._app_update_install_worker is not None:
            return
        busy = self.is_busy_for_app_update() if callable(self.is_busy_for_app_update) else ""
        if busy:
            if confirm:
                QMessageBox.information(self, "Not right now", f"Wait until {busy} finishes, then try again.")
            else:
                _log.info(f"Automatic update to {info.version} postponed: {busy}.")
            return
        if confirm:
            reply = QMessageBox.question(
                self, "Update ConanOps?",
                f"Download and install ConanOps {info.version}?\n\nYour current version is backed up first. "
                f"ConanOps closes and reopens itself to finish -- your servers keep running.",
                QMessageBox.Cancel | QMessageBox.Yes, QMessageBox.Yes,
            )
            if reply != QMessageBox.Yes:
                return
        self.install_online_btn.setEnabled(False)
        self.check_updates_btn.setEnabled(False)
        self.update_now_btn.setEnabled(False)
        self.app_update_progress.setRange(0, 0)
        self.app_update_progress.show()
        self.app_update_status.setText(f"Downloading ConanOps {info.version}…")
        worker = update_runner.AppUpdateInstallWorker(info, self.install_dir, parent=self)
        self._app_update_install_worker = worker
        self._retiring_workers.append(worker)
        worker.finished.connect(lambda w=worker: self._retiring_workers.remove(w) if w in self._retiring_workers else None)
        worker.progress.connect(self._on_online_update_progress)
        worker.finished_update.connect(self._on_online_update_finished)
        worker.start()

    def _on_online_update_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.app_update_progress.setRange(0, 1000)
            self.app_update_progress.setValue(int(done * 1000 / total))
            self.app_update_status.setText(
                f"Downloading… {done / 1024 ** 2:.0f} of {total / 1024 ** 2:.0f} MB"
            )
        if total > 0 and done >= total:
            self.app_update_progress.setRange(0, 0)
            self.app_update_status.setText("Installing…")

    def _on_online_update_finished(self, result) -> None:
        self._app_update_install_worker = None
        self.app_update_progress.hide()
        self.install_online_btn.setEnabled(True)
        self.check_updates_btn.setEnabled(True)
        self.update_now_btn.setEnabled(bool(self._update_zip_path))
        if not result.success:
            _log.error(f"Online update failed: {result.message}")
            self.app_update_status.setText(f"Update failed: {result.message}")
            return
        _log.info("Online update installed -- relaunching.")
        self.on_update_installed()

    def _on_auto_check_updates_toggled(self, checked: bool) -> None:
        self.config.auto_check_app_updates = checked
        self.save_config()

    def _on_auto_install_updates_toggled(self, checked: bool) -> None:
        self.config.auto_install_app_updates = checked
        self.save_config()

    def _choose_update_file(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "Choose ConanOps update file", "", "ConanOps update (*.zip)"
        )
        if not path:
            return
        try:
            self_update.validate_update_zip(path)
        except self_update.UpdateValidationError as e:
            QMessageBox.warning(self, "Not a valid update file", str(e))
            self._update_zip_path = None
            self.update_file_label.setText("No file selected")
            self.update_now_btn.setEnabled(False)
            return
        self._update_zip_path = path
        self.update_file_label.setText(os.path.basename(path))
        self.update_now_btn.setEnabled(True)

    def _confirm_and_update(self) -> None:
        if not self._update_zip_path:
            return
        reply = QMessageBox.question(
            self, "Update ConanOps?",
            f"Install the update from \"{os.path.basename(self._update_zip_path)}\"?\n\n"
            f"Your current version is backed up first. ConanOps will close and reopen "
            f"itself automatically once it's done.",
            QMessageBox.Cancel | QMessageBox.Yes, QMessageBox.Cancel,
        )
        if reply != QMessageBox.Yes:
            return

        self.update_now_btn.setEnabled(False)
        self.update_now_btn.setText("Updating…")

        # Off the UI thread: a big copy (or a locked-file rename swap
        # on Windows -- see self_update._copy_file_with_swap) can take
        # a while, and freezing the window during it just invites a
        # force-close partway through, which is the one thing the
        # update process is designed to survive cleanly.
        self._self_update_worker = SelfUpdateWorker(self._update_zip_path, self.install_dir, parent=self)
        self._self_update_worker.finished_update.connect(self._on_self_update_finished)
        worker = self._self_update_worker
        self._retiring_workers.append(worker)
        worker.finished.connect(lambda w=worker: self._retiring_workers.remove(w) if w in self._retiring_workers else None)
        self._self_update_worker.start()

    def _on_self_update_finished(self, result) -> None:
        self._self_update_worker = None
        if not result.success:
            _log.error(f"Update failed: {result.message}")
            QMessageBox.critical(self, "Update Failed", result.message)
            self.update_now_btn.setText("Update Now")
            self.update_now_btn.setEnabled(True)
            return

        _log.info("Update installed successfully -- relaunching.")
        self.on_update_installed()

    # ------------------------------------------------------- web control --
    def _refresh_web_control_ui(self) -> None:
        if self.web_control_server.is_running:
            url = self.web_control_server.url_for()
            self.web_control_link_label.setText(url)
            self.copy_link_btn.setEnabled(True)
        else:
            self.web_control_link_label.setText("Not currently running.")
            self.copy_link_btn.setEnabled(False)

    def _on_web_control_toggled(self, checked: bool) -> None:
        if checked:
            ok, message = self.web_control_server.start()
            if not ok:
                QMessageBox.warning(self, "Couldn't Start Web Control", message)
                self.web_control_checkbox.blockSignals(True)
                self.web_control_checkbox.setChecked(False)
                self.web_control_checkbox.blockSignals(False)
                self.config.web_control_enabled = False
                self.save_config()
                self._refresh_web_control_ui()
                return
        else:
            self.web_control_server.stop()

        self.config.web_control_enabled = checked
        self.save_config()
        self._refresh_web_control_ui()

    def _copy_web_control_link(self) -> None:
        url = self.web_control_server.url_for()
        if url:
            QApplication.clipboard().setText(url)

    # ------------------------------------------------------------ delete --
    def _confirm_delete_app_only(self) -> None:
        reply = QMessageBox.warning(
            self, "Uninstall ConanOps?",
            "This removes ConanOps' own program files and closes the app immediately. Your "
            "servers' install folders, SteamCMD folders, backups, and firewall rules are left "
            "untouched -- reinstalling ConanOps later will find them again.\n\n"
            "This can't be undone. Continue?",
            QMessageBox.Cancel | QMessageBox.Yes, QMessageBox.Cancel,
        )
        if reply == QMessageBox.Yes and self.on_delete_app_only:
            self.on_delete_app_only()

    def _confirm_delete_everything(self) -> None:
        # A single Yes/No isn't enough friction for something this
        # destructive (every configured server's actual world save,
        # gone, with no undo) -- requiring the person to type a literal
        # word makes it much harder to click through on autopilot the
        # way a dialog's default button invites.
        text, ok = QInputDialog.getText(
            self, "Delete Everything?",
            "This stops and deletes every server, its files and backups (your world saves), "
            "SteamCMD, firewall rules, router port forwards and ConanOps itself, and undoes the "
            "Windows changes ConanOps made. There is no undo.\n\n"
            "Type DELETE to confirm:",
        )
        if ok and text.strip() == "DELETE" and self.on_delete_everything:
            self.on_delete_everything()
