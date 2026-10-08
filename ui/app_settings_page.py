"""App Settings page: settings for ConanOps itself rather than one server (theme,
startup, unattended mode, Workshop key, DuckDNS, lock, web UI, alerts, updates, delete)."""
from __future__ import annotations

import os
from typing import Callable, Optional

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QScrollArea, QFrame, QColorDialog, QCheckBox, QMessageBox, QDialog,
    QFileDialog, QApplication, QInputDialog,
)
from PySide6.QtGui import QColor
from PySide6.QtCore import QThread, Signal, QTime, Qt
from PySide6.QtWidgets import QTimeEdit, QTextBrowser, QProgressBar, QComboBox, QStackedWidget, QButtonGroup

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
import admin_mode
import proc_utils
import app_updates
import update_runner
import version
import applog
import web_control
from ui.workers import keep_until_finished

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


class _CardCollector:
    """Stands in for a layout while the page's cards are built."""

    def __init__(self):
        self.widgets: list = []

    def addWidget(self, widget, *args) -> None:  # noqa: N802 - Qt-style name
        self.widgets.append(widget)


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
        # Holds finished workers until QThread.finished fires, avoiding
        # "QThread: Destroyed while thread is still running".
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

        # Cards are collected and shown one section at a time (see _build_sections).
        form = _CardCollector()
        self._root_layout = root

        # ----------------------------------------------------------- theme --
        theme_card = QFrame()
        theme_card.setObjectName("Card")
        theme_layout = QVBoxLayout(theme_card)
        theme_layout.setContentsMargins(20, 16, 20, 16)
        theme_layout.setSpacing(10)

        theme_title = QLabel("Appearance")
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
        ut = QLabel("Keep Running")
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

        self.admin_mode_checkbox = QCheckBox("Run with admin rights (no Windows permission prompts)")
        self.admin_mode_checkbox.setChecked(self.config.admin_mode_enabled)
        self.admin_mode_checkbox.toggled.connect(self._on_admin_mode_toggled)
        ul.addWidget(self.admin_mode_checkbox)
        self.admin_mode_note = QLabel()
        self.admin_mode_note.setObjectName("Dim")
        self.admin_mode_note.setWordWrap(True)
        ul.addWidget(self.admin_mode_note)
        self.admin_mode_problem_label = QLabel("")
        self.admin_mode_problem_label.setObjectName("ErrorText")
        self.admin_mode_problem_label.setWordWrap(True)
        self.admin_mode_problem_label.hide()
        ul.addWidget(self.admin_mode_problem_label)
        self._refresh_admin_mode_note()

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
        form.addWidget(self._build_alerts_card())

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

        lock_title = QLabel("PIN Lock")
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

        web_title = QLabel("Web Version")
        web_title.setObjectName("PageTitle")
        web_layout.addWidget(web_title)

        web_note = QLabel(
            "Use ConanOps from a browser on your phone or another computer -- dashboards, start/stop, "
            "players, console, backups, updates, mods and server settings. Deleting things and adding "
            "servers stay in this app. Everyone signs in with the password below."
        )
        web_note.setObjectName("Dim")
        web_note.setWordWrap(True)
        web_layout.addWidget(web_note)

        self.web_control_checkbox = QCheckBox("Turn on the web version")
        self.web_control_checkbox.setChecked(self.config.web_control_enabled)
        self.web_control_checkbox.toggled.connect(self._on_web_control_toggled)
        web_layout.addWidget(self.web_control_checkbox)

        pw_row = QHBoxLayout()
        self.web_password_label = QLabel("")
        self.web_password_label.setWordWrap(True)
        self.web_password_btn = QPushButton("Set Password…")
        self.web_password_btn.clicked.connect(self._set_web_password)
        self.web_signout_btn = QPushButton("Sign Everyone Out")
        self.web_signout_btn.clicked.connect(self._sign_out_web_sessions)
        pw_row.addWidget(self.web_password_label, 1)
        pw_row.addWidget(self.web_password_btn)
        pw_row.addWidget(self.web_signout_btn)
        web_layout.addLayout(pw_row)

        link_row = QHBoxLayout()
        link_caption = QLabel("On your Wi-Fi:")
        link_caption.setObjectName("Muted")
        self.web_control_link_label = QLabel("Not currently running.")
        self.web_control_link_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.copy_link_btn = QPushButton("Copy")
        self.copy_link_btn.setEnabled(False)
        self.copy_link_btn.clicked.connect(self._copy_web_control_link)
        self.web_firewall_btn = QPushButton("Allow Through Firewall")
        self.web_firewall_btn.setToolTip("Lets phones and PCs on your home network reach it (asks Windows for "
                                         "permission once). Not needed for the link from anywhere.")
        self.web_firewall_btn.clicked.connect(self._allow_web_through_firewall)
        link_row.addWidget(link_caption)
        link_row.addWidget(self.web_control_link_label, 1)
        link_row.addWidget(self.copy_link_btn)
        link_row.addWidget(self.web_firewall_btn)
        web_layout.addLayout(link_row)

        self.web_remote_checkbox = QCheckBox("Also let me use it from anywhere (secure link through Cloudflare)")
        self.web_remote_checkbox.setChecked(self.config.web_remote_enabled)
        self.web_remote_checkbox.toggled.connect(self._on_web_remote_toggled)
        web_layout.addWidget(self.web_remote_checkbox)
        remote_row = QHBoxLayout()
        self.web_remote_label = QLabel("")
        self.web_remote_label.setWordWrap(True)
        self.web_remote_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.copy_remote_btn = QPushButton("Copy")
        self.copy_remote_btn.clicked.connect(self._copy_remote_link)
        remote_row.addWidget(self.web_remote_label, 1)
        remote_row.addWidget(self.copy_remote_btn)
        web_layout.addLayout(remote_row)
        remote_note = QLabel(
            "Works on mobile data, with no router changes and your home address hidden -- Cloudflare's free "
            "tunnel carries it over HTTPS. The link changes whenever it reconnects; ConanOps sends the new one "
            "to your ntfy phone alerts if you use them (never to Discord, where players might see it). Anyone with the link still needs the password."
        )
        remote_note.setObjectName("Dim")
        remote_note.setWordWrap(True)
        web_layout.addWidget(remote_note)
        self.web_tunnel = None
        self.on_web_remote_changed = None
        self.on_web_password_changed = None
        self._remote_url = ""

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

        # Set by MainWindow: update notification, and whether restarting is safe now.
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
        self._build_sections(form.widgets)

        self._refresh_web_control_ui()

    # ------------------------------------------------------------ alerts --
    def _build_alerts_card(self) -> QFrame:
        """Discord and ntfy alerts, shared by all servers."""
        import alert_guides
        import webhooks
        from ui.fold_out_guide import FoldOutGuide
        card = QFrame()
        card.setObjectName("Card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(10)
        title = QLabel("Alerts")
        title.setObjectName("PageTitle")
        lay.addWidget(title)
        intro = QLabel("Get told when a server crashes, updates, finds a broken mod, a backup fails and more -- in a "
                       "Discord channel, as phone notifications through ntfy, or both. These are for all your "
                       "servers; every alert says which server it's about.")
        intro.setObjectName("Dim")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        def field(label_text: str, placeholder: str, value: str) -> QLineEdit:
            lbl = QLabel(label_text)
            lbl.setObjectName("Muted")
            lay.addWidget(lbl)
            edit = QLineEdit(value)
            edit.setPlaceholderText(placeholder)
            lay.addWidget(edit)
            return edit

        self.alert_discord_edit = field("Discord webhook link", "https://discord.com/api/webhooks/...",
                                        self.config.alert_discord_url)
        self.alert_discord_edit.editingFinished.connect(self._on_alert_links_changed)
        # The test shows the web-version link too, like every Discord alert
        # (get_web_link_line is set by MainWindow; called off the GUI thread).
        self.get_web_link_line = None
        self.alert_discord_test_btn, self.alert_discord_result = self._alert_test_row(
            lay, lambda url, name: webhooks.test_discord(
                url, name, self.get_web_link_line() if callable(self.get_web_link_line) else ""),
            self.alert_discord_edit)
        self.alert_discord_status_check = QCheckBox("Also keep a live status message for each server in that channel "
                                                    "(updates every ~5 minutes)")
        self.alert_discord_status_check.setChecked(self.config.discord_status_enabled)
        self.alert_discord_status_check.toggled.connect(self._on_discord_status_toggled)
        lay.addWidget(self.alert_discord_status_check)
        self.alert_discord_guide = FoldOutGuide(*alert_guides.DISCORD)
        lay.addWidget(self.alert_discord_guide)

        spacer = QLabel("")
        lay.addWidget(spacer)
        self.alert_ntfy_edit = field("ntfy topic link (phone notifications)", "https://ntfy.sh/your-topic-name",
                                     self.config.alert_ntfy_url)
        self.alert_ntfy_edit.editingFinished.connect(self._on_alert_links_changed)
        self.alert_ntfy_test_btn, self.alert_ntfy_result = self._alert_test_row(
            lay, webhooks.test_ntfy, self.alert_ntfy_edit)
        self.alert_ntfy_guide = FoldOutGuide(*alert_guides.NTFY)
        lay.addWidget(self.alert_ntfy_guide)
        self.alert_error_label = QLabel("")
        self.alert_error_label.setObjectName("ErrorText")
        self.alert_error_label.setWordWrap(True)
        self.alert_error_label.hide()
        lay.addWidget(self.alert_error_label)
        return card

    def _alert_test_row(self, lay, fn, edit: QLineEdit):
        row = QHBoxLayout()
        btn = QPushButton("Send Test")
        label = QLabel("")
        label.setObjectName("Dim")
        label.setWordWrap(True)
        row.addWidget(btn)
        row.addWidget(label, 1)
        lay.addLayout(row)

        def run():
            btn.setEnabled(False)
            label.setText("Sending…")
            url = edit.text().strip()

            def done(result):
                ok, text = result if isinstance(result, tuple) else (False, str(result))
                btn.setEnabled(True)
                label.setObjectName("OkNote" if ok else "ErrorText")
                label.style().unpolish(label)
                label.style().polish(label)
                label.setText(text)
            self._run_call(lambda: fn(url, "your servers"), done)
        btn.clicked.connect(run)
        return btn, label

    def set_alert_links(self, discord_url: str, ntfy_url: str, status_on: bool) -> None:
        """Saves the alert settings (from this page or the web version)."""
        import webhooks
        discord_url, ntfy_url = discord_url.strip(), ntfy_url.strip()
        problem = (webhooks.discord_url_problem(discord_url) if discord_url else "") or \
                  (webhooks.ntfy_url_problem(ntfy_url) if ntfy_url else "")
        if problem:
            raise ValueError(problem)
        changed = discord_url != self.config.alert_discord_url
        self.config.alert_discord_url = discord_url
        self.config.alert_ntfy_url = ntfy_url
        self.config.discord_status_enabled = bool(status_on)
        self.save_config()
        for edit, value in ((self.alert_discord_edit, discord_url), (self.alert_ntfy_edit, ntfy_url)):
            if edit.text() != value:
                edit.blockSignals(True)
                edit.setText(value)
                edit.blockSignals(False)
        self._set_checked_quietly(self.alert_discord_status_check, bool(status_on))
        self.alert_error_label.hide()
        if callable(getattr(self, "on_alerts_changed", None)):
            self.on_alerts_changed(changed)

    def _on_alert_links_changed(self) -> None:
        try:
            self.set_alert_links(self.alert_discord_edit.text(), self.alert_ntfy_edit.text(),
                                 self.alert_discord_status_check.isChecked())
        except ValueError as e:
            self.alert_error_label.setText(f"Not saved: {e}")
            self.alert_error_label.show()

    def _on_discord_status_toggled(self, on: bool) -> None:
        self._on_alert_links_changed()

    # ---------------------------------------------------------- sections --
    # (key, label, group heading or "") in the order the cards are built. Shared with the web UI.
    SECTIONS = [
        ("startup", "Startup", "Running"),
        ("keep", "Keep Running", ""),
        ("web", "Web Version", "Remote Access"),
        ("ddns", "Dynamic DNS", ""),
        ("alerts", "Alerts", "Integrations"),
        ("workshop", "Steam Workshop", ""),
        ("appearance", "Appearance", "ConanOps"),
        ("lock", "PIN Lock", ""),
        ("updates", "Updates", ""),
        ("delete", "Delete ConanOps", ""),
    ]
    _CARD_ORDER = ["appearance", "startup", "keep", "workshop", "alerts", "ddns", "lock", "web", "updates", "delete"]

    def _build_sections(self, cards: list) -> None:
        # A search box over every App Settings section, like Server Settings.
        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("SettingsSearch")
        self.search_edit.setPlaceholderText("Search App Settings, e.g. \"discord\", \"startup\", \"pin\" or \"web\"")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setAccessibleName("Search App Settings")
        self.search_edit.textChanged.connect(self._on_search)
        search_row = QHBoxLayout()
        search_row.setContentsMargins(24, 4, 24, 4)
        search_row.addWidget(self.search_edit)
        self._root_layout.addLayout(search_row)
        self._search_index = None

        body = QHBoxLayout()
        body.setContentsMargins(24, 4, 24, 20)
        body.setSpacing(24)
        nav = QVBoxLayout()
        nav.setSpacing(2)
        nav.setContentsMargins(0, 0, 0, 0)
        nav_box = QWidget()
        nav_box.setLayout(nav)
        nav_box.setFixedWidth(210)
        self._section_stack = QStackedWidget()
        self._section_buttons: dict = {}
        self._section_pages: dict = {}
        group = QButtonGroup(self)
        group.setExclusive(True)
        by_key = dict(zip(self._CARD_ORDER, cards))
        for key, label, heading in self.SECTIONS:
            if heading:
                h = QLabel(heading)
                h.setObjectName("SectionLabel")
                nav.addWidget(h)
            btn = QPushButton(label.replace("&", "&&"))
            btn.setObjectName("NavButton")
            btn.setCheckable(True)
            btn.clicked.connect(lambda _=False, k=key: self.show_section(k))
            group.addButton(btn)
            nav.addWidget(btn)
            self._section_buttons[key] = btn
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            inner = QWidget()
            col = QVBoxLayout(inner)
            col.setContentsMargins(0, 0, 8, 0)
            col.addWidget(by_key[key])
            col.addStretch(1)
            scroll.setWidget(inner)
            self._section_stack.addWidget(scroll)
            self._section_pages[key] = scroll
        nav.addStretch(1)
        self._section_group = group
        self._section_cards = by_key
        self._results_page = QWidget()
        rl = QVBoxLayout(self._results_page)
        rl.setContentsMargins(0, 0, 8, 0)
        rl.setSpacing(8)
        self._results_title = QLabel("")
        self._results_title.setObjectName("SectionTitle")
        rl.addWidget(self._results_title)
        self._results_card = QFrame()
        self._results_card.setObjectName("Card")
        self._results_layout = QVBoxLayout(self._results_card)
        self._results_layout.setContentsMargins(0, 4, 0, 4)
        self._results_layout.setSpacing(0)
        rl.addWidget(self._results_card)
        rl.addStretch(1)
        self._section_stack.addWidget(self._results_page)
        body.addWidget(nav_box)
        body.addWidget(self._section_stack, 1)
        self._root_layout.addLayout(body, 1)
        self.show_section("startup")

    def _build_search_index(self) -> list:
        from PySide6.QtWidgets import QAbstractButton
        labels = {k: label for k, label, _h in self.SECTIONS}
        index, seen = [], set()
        for key, card in self._section_cards.items():
            for w in card.findChildren(QWidget):
                if isinstance(w, QAbstractButton):
                    text = w.text()
                elif isinstance(w, QLabel) and w.objectName() not in ("Dim", "ErrorText"):
                    text = w.text()
                else:
                    continue
                text = " ".join(text.replace("&&", "&").split())
                if not text or len(text) > 90 or "<" in text or (key, text) in seen:
                    continue
                seen.add((key, text))
                index.append((key, text, labels.get(key, ""), w))
        return index

    def _on_search(self, text: str) -> None:
        q = " ".join(text.lower().split())
        if not q:
            self.show_section(self.current_section() or "startup")
            return
        if self._search_index is None:
            self._search_index = self._build_search_index()
        while self._results_layout.count():
            item = self._results_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        words = q.split()
        hits = [e for e in self._search_index if all(wd in f"{e[1]} {e[2]}".lower() for wd in words)]
        self._results_title.setText(
            f"{len(hits)} match{'' if len(hits) == 1 else 'es'} for \"{text.strip()}\"" if hits
            else f"Nothing matches \"{text.strip()}\". Try a shorter word.")
        self._results_card.setVisible(bool(hits))
        for key, label, where, w in hits[:60]:
            btn = QPushButton()
            btn.setObjectName("SearchResult")
            btn.setAccessibleName(f"{label}, in {where}")
            btn.setCursor(Qt.PointingHandCursor)
            lines = QVBoxLayout(btn)
            lines.setContentsMargins(16, 8, 16, 8)
            lines.setSpacing(2)
            name_label = QLabel(label)
            name_label.setObjectName("SearchResultName")
            where_label = QLabel(where)
            where_label.setObjectName("Dim")
            for lbl in (name_label, where_label):
                lbl.setAttribute(Qt.WA_TransparentForMouseEvents)
                lines.addWidget(lbl)
            btn.setMinimumHeight(name_label.sizeHint().height() + where_label.sizeHint().height() + 20)
            btn.clicked.connect(lambda _=False, k=key, w=w: self._go_to(k, w))
            self._results_layout.addWidget(btn)
        self._section_group.setExclusive(False)
        for b in self._section_buttons.values():
            b.setChecked(False)
        self._section_group.setExclusive(True)
        self._section_stack.setCurrentWidget(self._results_page)

    def _go_to(self, key: str, widget: QWidget) -> None:
        self.search_edit.blockSignals(True)
        self.search_edit.clear()
        self.search_edit.blockSignals(False)
        self.show_section(key)
        from PySide6.QtCore import QTimer

        def reveal():
            self._section_pages[key].ensureWidgetVisible(widget, 0, 120)
            if widget.focusPolicy() != Qt.NoFocus:
                widget.setFocus(Qt.OtherFocusReason)
        QTimer.singleShot(0, reveal)

    def show_section(self, key: str) -> None:
        if key not in self._section_pages:
            return
        self._last_section = key
        if getattr(self, "search_edit", None) is not None and self.search_edit.text():
            self.search_edit.blockSignals(True)
            self.search_edit.clear()
            self.search_edit.blockSignals(False)
        self._section_buttons[key].setChecked(True)
        self._section_stack.setCurrentWidget(self._section_pages[key])

    def current_section(self) -> str:
        page = self._section_stack.currentWidget()
        found = next((k for k, p in self._section_pages.items() if p is page), "")
        return found or getattr(self, "_last_section", "")

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
            # Save as strict #rrggbb: load_theme() rejects names like "red" that QColor accepts.
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
                return  # declined prompt: nothing to explain
            QMessageBox.warning(
                self, "Couldn't Change Background Mode",
                "Windows didn't accept the change -- see conanops.log for details.",
            )

        self._run_call(background_mode.register if enable else background_mode.unregister, done)

    def _on_background_status(self, status) -> None:
        if status is None or not self.config.background_mode_enabled:
            return  # couldn't check; stay quiet
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

    # ------------------------------------------------- admin rights --
    def _refresh_admin_mode_note(self) -> None:
        state = ""
        if self.config.admin_mode_enabled:
            state = (" ConanOps is running with admin rights now." if proc_utils.is_admin()
                     else " It takes effect the next time ConanOps starts.")
        self.admin_mode_note.setText(
            "Windows asks for permission once. After that ConanOps starts itself with admin rights, so firewall "
            "changes and Windows' update hours never wait for someone to click \"Yes\" -- including from the web "
            "version. Trade-off: anything that can change files in ConanOps' folder could also get admin rights "
            "this way." + state)

    def show_admin_mode_problem(self, text: str) -> None:
        self.admin_mode_problem_label.setText(text)
        self.admin_mode_problem_label.setVisible(bool(text))

    def _on_admin_mode_toggled(self, checked: bool) -> None:
        self.admin_mode_checkbox.setEnabled(False)

        def done(outcome, want=checked):
            self.admin_mode_checkbox.setEnabled(True)
            if want and outcome != powershell.RUN_OK:
                self._set_checked_quietly(self.admin_mode_checkbox, False)
                if outcome != powershell.RUN_DECLINED:
                    QMessageBox.warning(self, "Couldn't Turn This On",
                                        "Windows didn't accept the change -- see conanops.log for details.")
                return
            self.config.admin_mode_enabled = want
            self.save_config()
            self.show_admin_mode_problem("")
            self._refresh_admin_mode_note()
            if not want:
                if outcome != powershell.RUN_OK:
                    QMessageBox.information(
                        self, "Turned Off",
                        "ConanOps won't use admin rights from its next start. Windows' task for it couldn't be "
                        "removed (see conanops.log); it's no longer used.")
                elif proc_utils.is_admin():
                    QMessageBox.information(self, "Turned Off",
                                            "ConanOps keeps admin rights until it next restarts.")
                return
            if proc_utils.is_admin() or not callable(getattr(self, "on_restart_elevated", None)):
                return
            answer = QMessageBox.question(
                self, "Restart ConanOps?",
                "Restart ConanOps now so it has admin rights? Your servers keep running.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if answer == QMessageBox.Yes:
                self.on_restart_elevated()

        self._run_call(admin_mode.enable if checked else admin_mode.disable, done)

    def refresh_sign_in_status(self) -> None:
        """Checks off the GUI thread whether Windows signs back in after update restarts."""
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
        """Re-reads startup settings after another place (the setup wizard) changed them."""
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
            # Restore the last good value instead of saving an unusable one.
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
    def check_for_updates(self, automatic: bool = False) -> None:
        """Checks GitHub for a newer release off the GUI thread. Automatic checks
        stay quiet on errors and may auto-install if enabled."""
        if not app_updates.update_repo() or self._app_update_check_worker is not None \
                or self._app_update_install_worker is not None:
            return
        self.check_updates_btn.setEnabled(False)
        if not automatic or self._available_release is None:
            self.app_update_status.setText("Checking for updates…")
        worker = update_runner.AppUpdateCheckWorker(self)
        self._app_update_check_worker = worker
        keep_until_finished(self._retiring_workers, worker)
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
        keep_until_finished(self._retiring_workers, worker)
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

        # Off the GUI thread: a frozen window invites a force-close mid-copy.
        self._self_update_worker = SelfUpdateWorker(self._update_zip_path, self.install_dir, parent=self)
        self._self_update_worker.finished_update.connect(self._on_self_update_finished)
        worker = self._self_update_worker
        keep_until_finished(self._retiring_workers, worker)
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
        running = self.web_control_server.is_running
        has_pw = bool(self.config.web_password_hash)
        self.web_password_label.setText(
            "Password: set." if has_pw else "⚠ No password yet -- set one so you can sign in.")
        self.web_password_btn.setText("Change Password…" if has_pw else "Set Password…")
        self.web_signout_btn.setVisible(has_pw)
        if running:
            self.web_control_link_label.setText(self.web_control_server.url_for() or "")
        else:
            self.web_control_link_label.setText("Not currently running.")
        self.copy_link_btn.setEnabled(running)
        self.web_firewall_btn.setVisible(running)
        self.web_remote_checkbox.setEnabled(running and has_pw)
        if not (running and has_pw):
            self.web_remote_label.setText("Turn on the web version and set a password first."
                                          if self.config.web_remote_enabled else "")
        self.copy_remote_btn.setVisible(bool(self._remote_url) and self.config.web_remote_enabled)

    def show_web_remote_status(self, url: str, status: str) -> None:
        """Called by MainWindow when the Cloudflare link changes."""
        self._remote_url = url
        if not self.config.web_remote_enabled:
            self.web_remote_label.setText("")
        elif url:
            self.web_remote_label.setText(url)
        else:
            self.web_remote_label.setText(status)
        self.copy_remote_btn.setVisible(bool(url))

    def _on_web_control_toggled(self, checked: bool) -> None:
        if checked:
            ok, message = self.web_control_server.start()
            if not ok:
                QMessageBox.warning(self, "Couldn't Start the Web Version", message)
                self._set_checked_quietly(self.web_control_checkbox, False)
                self.config.web_control_enabled = False
                self.save_config()
                self._refresh_web_control_ui()
                return
        else:
            self.web_control_server.stop()
        self.config.web_control_enabled = checked
        self.save_config()
        self._refresh_web_control_ui()
        if callable(self.on_web_remote_changed):
            self.on_web_remote_changed()

    def _on_web_remote_toggled(self, checked: bool) -> None:
        self.config.web_remote_enabled = checked
        self.save_config()
        if not checked:
            self.web_remote_label.setText("")
            self._remote_url = ""
        self._refresh_web_control_ui()
        if callable(self.on_web_remote_changed):
            self.on_web_remote_changed()

    def _set_web_password(self) -> None:
        from webui import auth
        dlg = QDialog(self)
        dlg.setWindowTitle("Web Version Password")
        lay = QVBoxLayout(dlg)
        info = QLabel(f"Choose a password for the web version (at least {auth.MIN_PASSWORD_LENGTH} characters). "
                      "Changing it signs everyone out.")
        info.setWordWrap(True)
        lay.addWidget(info)
        first = QLineEdit()
        first.setEchoMode(QLineEdit.Password)
        first.setPlaceholderText("New password")
        second = QLineEdit()
        second.setEchoMode(QLineEdit.Password)
        second.setPlaceholderText("Type it again")
        error = QLabel("")
        error.setObjectName("ErrorText")
        lay.addWidget(first)
        lay.addWidget(second)
        lay.addWidget(error)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        ok = QPushButton("Save Password")
        ok.setObjectName("PrimaryButton")
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)
        cancel.clicked.connect(dlg.reject)

        def accept():
            problem = auth.password_problem(first.text())
            if not problem and first.text() != second.text():
                problem = "The two passwords don't match."
            if problem:
                error.setText(problem)
                return
            dlg.accept()
        ok.clicked.connect(accept)
        if dlg.exec() != QDialog.Accepted:
            return
        self.config.web_password_hash = auth.hash_password(first.text())
        self.save_config()
        if callable(self.on_web_password_changed):
            self.on_web_password_changed()
        self._refresh_web_control_ui()
        if callable(self.on_web_remote_changed):
            self.on_web_remote_changed()

    def _sign_out_web_sessions(self) -> None:
        if callable(self.on_web_password_changed):
            self.on_web_password_changed()
        QMessageBox.information(self, "Signed Out", "Every browser signed into the web version was signed out.")

    def _allow_web_through_firewall(self) -> None:
        import network_setup
        port = self.web_control_server.actual_port
        if not port:
            return
        self.web_firewall_btn.setEnabled(False)

        def done(outcome):
            self.web_firewall_btn.setEnabled(True)
            if outcome == powershell.RUN_OK:
                QMessageBox.information(self, "Allowed", "Phones and PCs on your home network can open it now.")
            elif outcome != powershell.RUN_DECLINED:
                QMessageBox.warning(self, "Not Allowed", "Windows didn't accept the change -- see conanops.log.")
        self._run_call(lambda: network_setup.allow_web_port(port), done)

    def _copy_web_control_link(self) -> None:
        url = self.web_control_server.url_for()
        if url:
            QApplication.clipboard().setText(url)

    def _copy_remote_link(self) -> None:
        if self._remote_url:
            QApplication.clipboard().setText(self._remote_url)

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
        # Typing a word, not just Yes/No, guards this irreversible delete of every world save.
        text, ok = QInputDialog.getText(
            self, "Delete Everything?",
            "This stops and deletes every server, its files and backups (your world saves), "
            "SteamCMD, firewall rules, router port forwards and ConanOps itself, and undoes the "
            "Windows changes ConanOps made. There is no undo.\n\n"
            "Type DELETE to confirm:",
        )
        if ok and text.strip() == "DELETE" and self.on_delete_everything:
            self.on_delete_everything()
