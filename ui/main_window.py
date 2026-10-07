"""
Main application window: sidebar navigation, the per-server dashboard
and settings pages, background monitors/schedulers, and the tray icon.
"""
from __future__ import annotations

import proc_utils
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional

from PySide6.QtCore import QTimer, Qt, QThread, Signal
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QFont
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QStackedWidget, QMessageBox,
    QSystemTrayIcon, QMenu, QDialog, QApplication, QFrame, QLabel, QPushButton,
)

from models import AppConfig, ServerConfig
import conanops_paths
import ini_utils
import process_manager
import backup_manager
import banlist_manager
import diagnostics
import game_log_manager
import dynamic_dns_runner
import discord_status_runner
import backup_runner
import health_check_runner
import network_setup
import network_setup_runner
import preflight
import powershell
import startup_registration
import keep_alive
import cleanup
import removal_runner
import vcredist
import power
import windows_update
import mod_manager
import webhooks
import applog
import theme_config
import web_control
import self_delete
from session_tracker import SessionTracker
from log_monitor import LogMonitor
from scheduler import Scheduler
from update_runner import CheckWorker, UpdateWorker, ModDownloadWorker
from ui.mod_recovery import ModRecoveryMixin

from ui.theme import build_stylesheet
from ui.sidebar import Sidebar
from ui import assets
from ui.theme import button_colors, _mix as _theme_mix
from ui.app_lock_dialog import SetPinDialog
from ui.remove_server_dialog import RemoveServerDialog
from ui.app_settings_page import AppSettingsPage
from ui.dashboard_page import DashboardPage
from ui.players_page import PlayersPage
from ui.updates_page import UpdatesPage
from ui.mods_page import ModsPage
from ui.access_page import AccessPage
from ui.console_page import ConsolePage
from ui.settings_identity_page import SettingsIdentityPage
from ui.settings_network_page import SettingsNetworkPage
from ui.settings_rates_page import SettingsRatesPage
from ui.settings_backups_page import SettingsBackupsPage
from ui.settings_restart_page import SettingsRestartPage
from ui.settings_alerts_page import SettingsAlertsPage
from ui.settings_container import SettingsContainer
from ui.diagnostics_page import DiagnosticsPage
from ui.generic_settings_page import GenericSettingsPage
from ui.setup_wizard import SetupWizard
import ini_field_specs

_log = applog.get_logger(__name__)

# The folder the running app lives in. Under a source install this is
# computed from this file's own location (ui/main_window.py's parent's
# parent) rather than assumed from cwd or sys.argv[0], so it's correct
# regardless of how ConanOps was launched. Under a PyInstaller-frozen
# build, __file__ points inside PyInstaller's own temporary extraction
# directory (sys._MEIPASS), NOT the folder the real .exe sits in --
# using it there would make the self-updater back up and overwrite a
# throwaway temp copy instead of the actual install, so that case uses
# sys.executable's own directory instead. Used by the self-updater to
# know what to back up and overwrite, and to relaunch the (now
# updated) app afterward.
APP_INSTALL_DIR = conanops_paths.app_install_dir()


def _sessions_path_for(server: ServerConfig) -> str:
    base = os.path.join(os.path.expanduser("~"), "ConanOps", "sessions")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, f"{server.id}.json")


def _make_tray_icon(accent: str = "") -> QIcon:
    """The bundled ConanOps icon (see ui/assets.py). `accent` is kept
    for call compatibility -- the icon no longer changes with the
    theme, since it's the app's actual brand mark now."""
    return assets.app_icon()


# Header title + one-line description for each top-level page.
PAGE_HEADERS = {
    "dashboard": ("Dashboard", "{server} at a glance: status, automation and who's online"),
    "players": ("Players", "Everyone who has joined {server}"),
    "mods": ("Mods", "Workshop mods, load order and update status"),
    "updates": ("Updates", "Conan Exiles dedicated server builds from Steam"),
    "access": ("Access", "Whitelist and bans"),
    "console": ("Console", "Send RCON commands to the running server"),
    "settings": ("Server Settings", "Everything ConanOps writes to {server}'s settings files"),
    "app": ("App Settings", "ConanOps itself: theme, startup and integrations"),
}


class _OpWorker(QThread):
    """Runs one blocking server operation (a graceful stop, a restart)
    off the UI thread. graceful_stop can legitimately wait up to ~2
    minutes for a big world to finish saving; doing that on the UI
    thread froze the whole window."""
    done = Signal(object)
    failed = Signal(object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            result = self._fn()
        except Exception as e:  # noqa: BLE001 - handed to the caller's on_error
            self.failed.emit(e)
            return
        self.done.emit(result)


class _NotifyWorker(QThread):
    """Sends a server's webhook alert(s) off the GUI thread.
    webhooks.notify() does up to two blocking HTTP POSTs (Discord,
    ntfy), each with its own 5s timeout -- _notify() used to call it
    directly, so any alert (a restart, a failed backup, a scheduler
    event) could freeze the whole app for up to ~10s, worse if the
    webhook endpoint was slow or unreachable rather than failing fast."""
    finished_notify = Signal(bool, str, str, str)  # ok, server_name, title, message

    def __init__(self, discord_url: str, ntfy_url: str, message: str, title: str, server_name: str, parent=None):
        super().__init__(parent)
        self.discord_url = discord_url
        self.ntfy_url = ntfy_url
        self.message = message
        self.title = title
        self.server_name = server_name

    def run(self) -> None:
        try:
            ok = webhooks.notify(self.discord_url, self.ntfy_url, self.message, title=self.title)
        except Exception as e:  # noqa: BLE001 - always emit so this worker gets cleared from _notify_workers
            _log.error(f"Alert delivery raised unexpectedly: {e}")
            ok = False
        self.finished_notify.emit(ok, self.server_name, self.title, self.message)


class MainWindow(QMainWindow, ModRecoveryMixin):
    def __init__(self, config: Optional[AppConfig] = None, background: bool = False):
        # background=True: the windowless instance started by the
        # unattended-mode scheduled task (see background_mode.py). Same
        # logic, never shown.
        self.background = background
        super().__init__()
        self.setWindowTitle("ConanOps")
        self.setWindowIcon(assets.app_icon())
        self.resize(1440, 900)

        self.config = config or AppConfig.load()
        self.theme = theme_config.load_theme()

        self._trackers: Dict[str, SessionTracker] = {}
        self._log_monitors: Dict[str, LogMonitor] = {}
        self._online_by_server: Dict[str, set] = {}
        self._expected_stop: set = set()          # server ids we're intentionally stopping
        # Separate from _expected_stop on purpose: that set is touched
        # by many independent short-lived flows (updates, restores,
        # restarts), each adding then unconditionally discarding a
        # server's id when THEY finish -- which used to also silently
        # clear an auto-bisect's lock on the same server if one of
        # those other flows happened to run (or finish) while a bisect
        # was mid-run, letting the watchdog and scheduler fight it.
        # This set is only ever touched by ModsPage's
        # lock/unlock_server_for_automation, so nothing else can clear
        # it out from under a running bisect.
        self._automation_locked: set = set()
        self._known_running: Dict[str, bool] = {}  # server id -> was it running last health check
        # Watchdog crash-restart state -- see _attempt_watchdog_restart().
        # Neither dict is persisted: a fresh app session starts every
        # server's crash count at zero, same as a person manually
        # restarting it would.
        self._watchdog_attempts: Dict[str, int] = {}       # server id -> consecutive crash-restarts this session
        self._watchdog_last_attempt: Dict[str, float] = {} # server id -> time.monotonic() of the last attempt
        self._unresponsive_since: Dict[str, float] = {}    # server id -> time.monotonic() it first stopped answering queries
        self._mod_refresh_workers: Dict[str, ModDownloadWorker] = {}
        self._update_failure_streak: Dict[str, int] = {}   # server id -> consecutive AUTOMATIC update failures
        self._update_retry_pending: set = set()            # server ids with a failed-update retry timer running
        self._post_update_watch: Dict[str, dict] = {}      # server id -> {"until": monotonic, "failures": n}
        # (server_id, "install"|"backup") pairs already alerted for low
        # disk space -- cleared once that folder's drive recovers well
        # above the warning threshold, so a later drop alerts again
        # instead of either going silent forever after the first alert
        # or re-alerting every single timer tick while it stays low.
        self._disk_space_low_alerted: set = set()
        self._update_check_workers: Dict[str, CheckWorker] = {}
        self._update_apply_workers: Dict[str, UpdateWorker] = {}
        self._manual_update_in_progress: set = set()  # server ids with a manual update (Updates page) running
        self._restore_workers: Dict[str, backup_runner.RestoreWorker] = {}
        self._firewall_workers: List[network_setup_runner.FirewallReconcileWorker] = []
        self._notify_workers: List[_NotifyWorker] = []
        # Workers get dropped from the dicts/lists above as soon as their
        # OWN result signal fires (that's what the busy-checks and UI
        # updates need), but that can happen slightly before Qt's own
        # QThread.finished -- dropping the last Python reference to a
        # QThread before the underlying OS thread has actually wound
        # down is a real crash risk ("QThread: Destroyed while thread
        # is still running"). Every worker below is also handed to
        # self._retire_worker(), which holds a reference here until
        # QThread.finished actually fires.
        self._retiring_workers: list = []

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.setCentralWidget(central)

        self.sidebar = Sidebar(
            muted_color=_theme_mix(self.theme.muted, self.theme.ivory, 0.55),
            accent_text_color=button_colors(self.theme.accent, self.theme.red, self.theme.bg)["accent_text"],
        )
        self.sidebar.power_clicked.connect(self._on_sidebar_power)
        self.sidebar.server_selected.connect(self._on_server_selected)
        self.sidebar.add_server_clicked.connect(self._on_add_server)
        self.sidebar.server_remove_requested.connect(self._on_remove_server_requested)
        self.sidebar.nav_selected.connect(self._on_nav_selected)
        layout.addWidget(self.sidebar)

        self.dashboard_page = DashboardPage()
        self.dashboard_page.on_restart = self._handle_restart
        self.dashboard_page.on_start = self._handle_start
        self.dashboard_page.on_stop = self._handle_stop
        self.dashboard_page.on_setup = self._open_setup_wizard
        self.dashboard_page.on_add_server = self._on_add_server
        self.dashboard_page.on_open_automation = self._open_automation_settings
        self.dashboard_page.on_start_anyway = lambda: (self.config.get_active() and self._handle_start(self.config.get_active()))
        self.dashboard_page.on_enable_rcon = self._enable_rcon_for_active
        self.dashboard_page.automation_tiles["watchdog"].open_btn.hide()  # follows Start/Stop, nothing to configure

        self.players_page = PlayersPage()
        self.players_page.on_kick = self._handle_kick_player
        self.players_page.on_ban = self._handle_ban_player

        self.updates_page = UpdatesPage()
        self.updates_page.on_update_applied = self._handle_update_applied
        self.updates_page.on_auto_update_setting_changed = self._handle_config_changed
        self.updates_page.on_update_now_guard = self._is_update_busy
        self.updates_page.on_stop_before_update = self._stop_before_manual_update
        self.updates_page.on_relaunch_after_update = self._relaunch_after_manual_update

        self.mods_page = ModsPage()
        self.mods_page.get_api_key = lambda: self.config.steam_api_key
        self.mods_page.get_update_cutoff = lambda: self.config.workshop_update_cutoff
        self.mods_page.auto_check_mod_status = True
        self.mods_page.is_server_online = lambda: bool(self._online_by_server.get(self.config.active_server_id))
        self.mods_page.lock_server_for_automation = lambda sid: self._automation_locked.add(sid)
        self.mods_page.unlock_server_for_automation = lambda sid: self._automation_locked.discard(sid)
        self.mods_page.restart_server = self._handle_restart
        self.mods_page.claim_mod_refresh_slot = self._claim_mod_refresh_worker
        self.mods_page.release_mod_refresh_slot = self._release_mod_refresh_worker
        self.mods_page.is_mod_refresh_busy = lambda sid: sid in self._mod_refresh_workers
        self.mods_page.on_changed = self._handle_mods_changed

        self.access_page = AccessPage()
        self.access_page.on_changed = self._handle_config_changed

        self.console_page = ConsolePage()

        self.settings_identity_page = SettingsIdentityPage()
        self.settings_network_page = SettingsNetworkPage(get_reserved_ports=self._reserved_ports_for_active)
        self.settings_rates_page = SettingsRatesPage()  # Progression category
        self.settings_backups_page = SettingsBackupsPage()
        self.settings_restart_page = SettingsRestartPage(get_hourly_activity=self._hourly_activity_for_active)
        self.settings_alerts_page = SettingsAlertsPage()
        self.diagnostics_page = DiagnosticsPage(
            get_active_server=self._active, get_reserved_ports=self._reserved_ports_for_active,
        )

        # One GenericSettingsPage per remaining category from
        # ini_field_specs.CATEGORIES (Day/Night, Survival, Combat,
        # Harvesting, Crafting, Building & Decay, Chat, Purge, Pets &
        # Hunger) -- Progression above is handled by settings_rates_page
        # since it's the original bespoke page, kept for continuity.
        self.gameplay_pages: dict = {}
        for key, label, specs in ini_field_specs.CATEGORIES:
            if key == "progression":
                continue  # already have settings_rates_page for this one
            self.gameplay_pages[key] = GenericSettingsPage(label, specs)

        gameplay_apply = self._apply_gameplay
        for page in [self.settings_identity_page, self.settings_rates_page, *self.gameplay_pages.values()]:
            page.on_apply = gameplay_apply

        self.settings_network_page.on_apply = self._apply_network
        self.settings_network_page.on_repair_network = self._repair_network_for_active
        self.settings_backups_page.on_apply = self._apply_backups_settings
        self.settings_backups_page.backups_widget.on_restore_confirmed = self._handle_restore
        self.settings_restart_page.on_apply = self._apply_restart
        self.settings_alerts_page.on_apply = self._apply_alerts

        settings_entries = [
            ("diagnostics", "Diagnostics", self.diagnostics_page),
            ("identity", "Server Identity", self.settings_identity_page),
            ("network", "Network & Ports", self.settings_network_page),
            ("progression", "Progression", self.settings_rates_page),
        ]
        for key, label, _specs in ini_field_specs.CATEGORIES:
            if key == "progression":
                continue
            settings_entries.append((key, label, self.gameplay_pages[key]))
        settings_entries += [
            ("backups", "Backups", self.settings_backups_page),
            ("restart", "Restart Schedule", self.settings_restart_page),
            ("alerts", "RCON & Alerts", self.settings_alerts_page),
        ]

        self.settings_container = SettingsContainer(settings_entries)

        self.web_control = web_control.WebControlServer(get_active_server=self._active, save_config=self.config.save)
        if self.config.web_control_enabled:
            ok, message = self.web_control.start()
            if not ok:
                _log.error(f"Couldn't auto-start web control on launch: {message}")

        self.app_settings_page = AppSettingsPage(
            self.config, self.config.save, self._on_theme_changed, self._on_lock_changed,
            install_dir=APP_INSTALL_DIR, on_update_installed=self._relaunch_after_update,
            web_control_server=self.web_control,
            on_delete_app_only=self._handle_delete_app_only,
            on_delete_everything=self._handle_delete_everything,
        )

        self.pages = {
            "dashboard": self.dashboard_page,
            "players": self.players_page,
            "updates": self.updates_page,
            "mods": self.mods_page,
            "access": self.access_page,
            "console": self.console_page,
            "settings": self.settings_container,
            "app": self.app_settings_page,
        }
        self.stack = QStackedWidget()
        for p in self.pages.values():
            self.stack.addWidget(p)
        # Each page used to draw its own big title; the shared header
        # above the stack does that now, so the pages' own copies hide
        # (they're kept, not deleted -- some still carry state text).
        for p in self.pages.values():
            for attr in ("title_label", "page_title_label"):
                lbl = getattr(p, attr, None)
                if lbl is not None:
                    lbl.hide()

        # Tables everywhere get the mockup's look: no grid, roomy rows,
        # left-aligned headers, whole-row selection.
        from PySide6.QtWidgets import QTableWidget, QAbstractItemView
        # self.stack, not self: the stack isn't parented to the window
        # yet at this point, so self.findChildren() found no tables and
        # this styling silently never applied.
        for table in self.stack.findChildren(QTableWidget):
            table.setShowGrid(False)
            table.verticalHeader().setDefaultSectionSize(52)
            table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            table.horizontalHeader().setHighlightSections(False)
            for c in range(table.columnCount()):
                header_item = table.horizontalHeaderItem(c)
                if header_item is not None:
                    header_item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            table.setSelectionBehavior(QAbstractItemView.SelectRows)
            table.setFocusPolicy(Qt.NoFocus)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        content_layout.addWidget(self._build_header())
        content_layout.addWidget(self.stack, 1)
        layout.addWidget(content, 1)

        self._chrome_timer = QTimer(self)
        self._chrome_timer.timeout.connect(self._refresh_chrome)
        self._chrome_timer.start(3000)

        self.setStyleSheet(build_stylesheet(self.theme))

        # Start a background monitor for every server that has a valid
        # install dir, regardless of which one is "active" -- lets the
        # scheduler and crash-detection work for servers you aren't
        # currently viewing.
        for server in self.config.servers:
            self._ensure_monitor(server)
            self._known_running[server.id] = process_manager.is_running(server.install_dir) if server.install_dir else False

        self.scheduler = Scheduler(
            get_servers=lambda: self.config.servers,
            is_online=lambda s: bool(self._online_by_server.get(s.id)),
            on_state_changed=self.config.save,
            is_running=lambda s: process_manager.is_running(s.install_dir) if s.install_dir else False,
            is_locked=lambda s: s.id in self._automation_locked,
        )
        self.scheduler.backup_due.connect(self._handle_scheduled_backup)
        self.scheduler.restart_due.connect(self._handle_scheduled_restart)
        self.scheduler.restart_skipped_online.connect(self._handle_restart_skipped_online)
        self.scheduler.update_check_due.connect(self._handle_update_check_due)
        self.scheduler.mod_refresh_due.connect(self._handle_mod_refresh_due)
        self.scheduler.start()

        self._health_timer = QTimer(self)
        self._health_timer.timeout.connect(self._start_health_check)
        self._health_timer.start(10_000)
        self._health_worker: Optional[health_check_runner.HealthCheckWorker] = None

        # Proactive, standing watch for low disk space -- distinct from
        # the Diagnostics tab's on-demand check, this runs in the
        # background so a slowly filling drive gets flagged before it
        # actually breaks a backup or update, not only when someone
        # happens to go looking. 30 minutes is frequent enough to catch
        # it well before a typical drive fills from a quiet 5 GB
        # headroom to 0, without adding meaningful overhead (this is
        # just shutil.disk_usage() calls, no network or subprocess).
        self._disk_space_timer = QTimer(self)
        self._disk_space_timer.timeout.connect(self._check_disk_space_all)
        self._disk_space_timer.start(30 * 60 * 1000)

        # Unattended operation: keep the PC from idle-sleeping while a
        # server runs, and do Windows Update restarts inside the chosen
        # window (see power.py / windows_update.py). Both once a minute.
        self._update_restart_started = False
        self._unattended_timer = QTimer(self)
        self._unattended_timer.timeout.connect(self._unattended_tick)
        self._unattended_timer.start(60_000)
        QTimer.singleShot(3_000, self._unattended_tick)

        # ConanOps' own updates (GitHub Releases -- see app_updates.py):
        # once shortly after start, then whenever 20 h have passed.
        self.app_settings_page.on_app_update_available = self._on_app_update_available
        self.app_settings_page.on_show_tour = self._show_tour_from_settings
        self.app_settings_page.is_busy_for_app_update = self._busy_reason_for_app_update
        self._app_update_timer = QTimer(self)
        self._app_update_timer.timeout.connect(self._maybe_check_app_update)
        self._app_update_timer.start(60 * 60 * 1000)
        QTimer.singleShot(45_000, self._maybe_check_app_update)

        # Finds a broken mod by itself and waits for its fix (ui/mod_recovery.py).
        self._init_mod_recovery()

        # Routers often forget automatic port forwards when they restart.
        # Re-check them every hour for running servers, instead of only
        # when a server starts (see network_setup.refresh_upnp_async).
        self._upnp_refresh_timer = QTimer(self)
        self._upnp_refresh_timer.timeout.connect(self._refresh_router_forwards)
        self._upnp_refresh_timer.start(self._UPNP_REFRESH_MS)

        # Make the keep-alive task match the setting (re-points it if
        # ConanOps was moved). Off the UI thread: PowerShell is slow.
        if not self.background:
            import threading
            threading.Thread(target=keep_alive.ensure, args=(self.config.keep_alive_enabled,),
                             name="keep-alive-ensure", daemon=True).start()
        # Also check shortly after startup, not just after the first
        # 30-minute wait -- a real QTimer parented to self (rather than
        # the bare QTimer.singleShot(msec, callable) form) so Qt's own
        # parent/child cleanup prevents this from ever firing against
        # an already-closed window if someone closes ConanOps within
        # the first 5 seconds.
        startup_check_timer = QTimer(self)
        startup_check_timer.setSingleShot(True)
        startup_check_timer.timeout.connect(self._check_disk_space_all)
        startup_check_timer.start(5_000)

        # Same 30-minute cadence as disk space above -- a typical DDNS
        # client update interval (Docker's own duckdns image defaults
        # to 15 minutes; this errs a little gentler). App-level, not
        # per-server, since it's about this machine's own public IP,
        # not any one server.
        self._duckdns_timer = QTimer(self)
        self._duckdns_timer.timeout.connect(self._check_duckdns)
        self._duckdns_timer.start(30 * 60 * 1000)
        duckdns_startup_timer = QTimer(self)
        duckdns_startup_timer.setSingleShot(True)
        duckdns_startup_timer.timeout.connect(self._check_duckdns)
        duckdns_startup_timer.start(5_000)
        self._duckdns_worker: Optional[dynamic_dns_runner.DuckDnsWorker] = None

        # Live Discord status presence: an always-current message per
        # server (opt-in -- discord_status_enabled), edited in place
        # rather than a fresh post each time. 5 minutes is frequent
        # enough to feel "live" without hammering Discord's rate limits
        # across several servers, or making the channel feel spammy
        # from constant edits.
        self._discord_status_timer = QTimer(self)
        self._discord_status_timer.timeout.connect(self._check_discord_status_all)
        self._discord_status_timer.start(5 * 60 * 1000)
        discord_status_startup_timer = QTimer(self)
        discord_status_startup_timer.setSingleShot(True)
        discord_status_startup_timer.timeout.connect(self._check_discord_status_all)
        discord_status_startup_timer.start(5_000)
        self._discord_status_workers: Dict[str, discord_status_runner.DiscordStatusWorker] = {}
        # In-memory only: consecutive edit FAILURES per server (not
        # persisted -- resets each session). See _on_discord_status_finished
        # for why this exists: a transient network blip shouldn't throw
        # away a perfectly good message id and start posting fresh
        # messages, but a message that's genuinely gone (deleted in
        # Discord) should eventually be replaced rather than failing
        # to edit it forever.
        self._discord_status_failures: Dict[str, int] = {}

        self._setup_tray()

        # After the tray icon exists (auto-resume failures are reported
        # via _notify(), which uses it) and after every server's initial
        # _known_running state is known (set in the loop above).
        self._auto_resume_servers()

        self._refresh_sidebar()
        self._load_active_server()
        self.sidebar.set_active_nav("dashboard")

    # ------------------------------------------------------------- nav --
    def _on_nav_selected(self, key: str) -> None:
        self.sidebar.set_active_nav(key)
        self.stack.setCurrentWidget(self.pages[key])
        self._current_nav = key
        self._refresh_header()

    # ---------------------------------------------------------- chrome --
    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName("ContentHeader")
        row = QHBoxLayout(header)
        row.setContentsMargins(24, 20, 24, 18)
        row.setSpacing(16)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.header_title = QLabel("Dashboard")
        self.header_title.setObjectName("HeaderTitle")
        self.header_subtitle = QLabel("")
        self.header_subtitle.setObjectName("HeaderSubtitle")
        titles.addWidget(self.header_title)
        titles.addWidget(self.header_subtitle)
        row.addLayout(titles, 1)
        self.header_spinner = assets.LoadingSpinner(22)
        self.header_spinner.hide()
        row.addWidget(self.header_spinner, 0, Qt.AlignVCenter)
        self.header_status = QLabel("")
        self.header_status.setObjectName("PillOff")
        row.addWidget(self.header_status, 0, Qt.AlignVCenter)
        self.header_lock_btn = QPushButton()
        self.header_lock_btn.setObjectName("IconButton")
        self.header_lock_btn.setIcon(assets.line_icon("lock", "#ececec"))
        self.header_lock_btn.setToolTip("Change or set the app PIN")
        self.header_lock_btn.setAccessibleName("App PIN")
        self.header_lock_btn.clicked.connect(lambda: self._open_set_pin_dialog())
        row.addWidget(self.header_lock_btn, 0, Qt.AlignVCenter)
        self._current_nav = "dashboard"
        return header

    def _refresh_header(self) -> None:
        if not hasattr(self, "header_title"):
            return
        title, subtitle = PAGE_HEADERS.get(getattr(self, "_current_nav", "dashboard"), ("", ""))
        server = self.config.get_active()
        name = server.name if server else "your server"
        self.header_title.setText(title)
        self.header_subtitle.setText(subtitle.format(server=name))
        if not server:
            self.header_status.hide()
            self.header_spinner.hide()
            return
        self.header_status.show()
        running = bool(self._known_running.get(server.id))
        players = len(self._online_by_server.get(server.id, set()) or ())
        busy = server.id in getattr(self, "_expected_stop", set()) or server.id in self._automation_locked
        if self.header_spinner.isVisible() != busy:
            self.header_spinner.setVisible(busy)
        if running:
            name = "PillOn"
            text = f"● Online · {players} / {server.max_players}" if getattr(server, "max_players", 0) else f"● Online · {players}"
        else:
            name, text = "PillOff", "● Stopped"
        if self.header_status.text() != text:
            self.header_status.setText(text)
        if self.header_status.objectName() != name:
            # Re-polish only when the pill's color actually changes --
            # this runs every few seconds.
            self.header_status.setObjectName(name)
            self.header_status.style().unpolish(self.header_status)
            self.header_status.style().polish(self.header_status)

    def _refresh_chrome(self) -> None:
        """Sidebar status dots/player counts, the bottom Start/Stop
        button and the header pill. Runs on a timer from state other
        code already keeps current (_known_running, _online_by_server)
        -- it never polls processes itself."""
        for srv in self.config.servers:
            self.sidebar.set_server_status(
                srv.id, bool(self._known_running.get(srv.id)), len(self._online_by_server.get(srv.id, set()) or ()),
            )
        active = self.config.get_active()
        self.sidebar.set_power_state(bool(self._known_running.get(active.id)) if active else None)
        if active:
            self.dashboard_page.set_automation(self._automation_states(active))
            self.dashboard_page.hold_notice.set_notice(
                f"Held stopped. {active.update_hold}" if active.update_hold else ""
            )
            self.dashboard_page.rcon_notice.set_notice(
                "RCON is off for this server, so ConanOps can't ask it to save before stopping it. "
                "Every stop, restart and update is a hard shutdown that loses anything since the last "
                "autosave, and can damage the world if it lands mid-save." if not active.rcon_enabled else ""
            )
        self._refresh_header()

    def _automation_states(self, server: ServerConfig) -> dict:
        def hours(n: int) -> str:
            return f"every {n} h" if n != 1 else "every hour"
        alerts = [name for name, url in (("Discord", server.webhook_discord_url), ("ntfy", server.webhook_ntfy_url)) if url]
        ddns_on = bool(self.config.duckdns_domain and self.config.duckdns_token)
        return {
            "restart": (server.restart_enabled,
                        f"Daily {server.restart_start}–{server.restart_end}" if server.restart_enabled else "Off"),
            "updates": (server.auto_update,
                        f"Checks {hours(server.auto_update_check_interval_hours)}" if server.auto_update else "Off"),
            "backups": (server.backup_interval_hours > 0,
                        f"{hours(server.backup_interval_hours).capitalize()} · keep {server.backup_daily_keep}"
                        if server.backup_interval_hours > 0 else "Off"),
            "watchdog": (server.desired_running,
                         "Restarts it after a crash" if server.desired_running else "Off while stopped"),
            "alerts": (bool(alerts), " + ".join(alerts) if alerts else "Not set up"),
            "ddns": (ddns_on, f"{self.config.duckdns_domain}.duckdns.org" if ddns_on else "Not set up"),
        }

    def _open_automation_settings(self, key: str) -> None:
        if key == "updates":
            self._on_nav_selected("updates")
        elif key == "ddns":
            self._on_nav_selected("app")
        elif key in ("restart", "backups", "alerts"):
            self._on_nav_selected("settings")
            self.settings_container._select(key)

    def _enable_rcon_for_active(self) -> None:
        """Dashboard's "Turn On RCON": RCON on with a random password and
        a port no other server uses. Written to Game.ini now; the server
        picks it up the next time it starts."""
        import secrets
        server = self.config.get_active()
        if not server:
            return
        server.rcon_enabled = True
        if not server.rcon_password:
            server.rcon_password = secrets.token_urlsafe(12)
        taken = self.config.used_ports(exclude_id=server.id) | {server.game_port, server.game_port + 1, server.query_port}
        while server.rcon_port in taken:
            server.rcon_port += 1
        self.config.save()
        if server.install_dir:
            process_manager.sync_rcon_ini(server)
        self.console_page.set_server(server)
        if self.config.active_server_id == server.id:
            self.settings_alerts_page.load_committed({
                "rcon_enabled": server.rcon_enabled, "rcon_port": server.rcon_port,
                "rcon_password": server.rcon_password,
                "webhook_discord_url": server.webhook_discord_url, "webhook_ntfy_url": server.webhook_ntfy_url,
            })
        running = bool(self._known_running.get(server.id))
        self._notify(server, "RCON turned on." + (" It takes effect the next time the server restarts."
                                                  if running else ""), title="RCON On")
        self._refresh_chrome()

    def _on_sidebar_power(self) -> None:
        server = self.config.get_active()
        if not server:
            return
        if self._known_running.get(server.id):
            self._handle_stop(server)
        else:
            self._handle_start(server)
        self._refresh_chrome()

    def _refresh_sidebar(self) -> None:
        self.sidebar.set_servers(self.config.servers, self.config.active_server_id)
        self.sidebar.set_per_server_nav_enabled(bool(self.config.servers))
        self._refresh_chrome()

    def _on_server_selected(self, server_id: str) -> None:
        self.config.active_server_id = server_id
        self.config.save()
        self._load_active_server()

    def _on_add_server(self) -> None:
        server = self.config.add_server()
        if server is None:
            QMessageBox.information(self, "Server limit reached", "ConanOps supports up to 5 servers.")
            return
        self.config.save()
        self._refresh_sidebar()
        self._load_active_server()
        self._open_setup_wizard(server)

    def _on_remove_server_requested(self, server_id: str) -> None:
        server = next((s for s in self.config.servers if s.id == server_id), None)
        if not server:
            return

        is_running = process_manager.is_running(server.install_dir) if server.install_dir else False
        dialog = RemoveServerDialog(
            server.name, server.install_dir, server.backup_destination, is_running, parent=self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        delete_files = dialog.delete_files
        delete_backups = dialog.delete_backups

        # Stop watching it -- the background log monitor, scheduler, and
        # health check all key off self.config.servers / these dicts, so
        # once the server's gone from both, nothing else touches it again.
        # The worker below stops the server itself (saving the world)
        # before deleting anything, so nothing is locked.
        if is_running:
            self._expected_stop.add(server_id)
            self._tracker_for(server).close_all_active()
        monitor = self._log_monitors.pop(server_id, None)
        if monitor:
            monitor.stop()
            monitor.wait(500)
        self._trackers.pop(server_id, None)
        self._online_by_server.pop(server_id, None)
        self._known_running.pop(server_id, None)
        self._watchdog_attempts.pop(server_id, None)
        self._post_update_watch.pop(server_id, None)
        server.desired_running = False

        others = [s for s in self.config.servers if s.id != server_id]
        self.config.remove_server(server_id)
        self.config.save()
        self._refresh_sidebar()
        self._load_active_server()

        worker = removal_runner.ServerRemovalWorker(server, others, delete_files, delete_backups)
        self._firewall_workers.append(worker)
        self._retire_worker(worker)

        def finished(problems, w=worker, srv=server, files=delete_files, backups=delete_backups):
            if w in self._firewall_workers:
                self._firewall_workers.remove(w)
            self._expected_stop.discard(srv.id)
            if problems:
                QMessageBox.warning(
                    self, "Some Things Weren't Removed",
                    f"\"{srv.name}\" was removed from ConanOps, but:\n\n" + "\n".join(problems) +
                    "\n\nYou can delete anything listed above by hand.",
                )
                return
            done = ["its settings, session history, firewall rules and router port forwards"]
            if files:
                done.append("its server files")
            if backups:
                done.append("its backups")
            QMessageBox.information(
                self, "Server Removed",
                f"\"{srv.name}\" was removed, along with " + ", ".join(done[:-1]) +
                (" and " if len(done) > 1 else "") + done[-1] + ".",
            )

        worker.finished_removal.connect(finished)
        self._start_or_run(worker)

    # --------------------------------------------------- monitor upkeep --
    def _tracker_for(self, server: ServerConfig) -> SessionTracker:
        if server.id not in self._trackers:
            self._trackers[server.id] = SessionTracker(_sessions_path_for(server))
        return self._trackers[server.id]

    def _tracker_for_id(self, server_id: str) -> SessionTracker:
        server = next((s for s in self.config.servers if s.id == server_id), None)
        return self._tracker_for(server) if server else SessionTracker(
            os.path.join(os.path.expanduser("~"), "ConanOps", "sessions", f"{server_id}.json")
        )

    def _ensure_monitor(self, server: ServerConfig) -> None:
        existing = self._log_monitors.get(server.id)
        if existing and existing.install_dir == server.install_dir:
            return
        if existing:
            existing.stop()
            existing.wait(500)
            del self._log_monitors[server.id]

        if not server.install_dir:
            return

        monitor = LogMonitor(server.install_dir)
        self._tracker_for(server)
        self._online_by_server.setdefault(server.id, set())

        monitor.player_joined.connect(lambda name, sid=server.id: self._on_player_joined(sid, name))
        monitor.player_left.connect(lambda name, sid=server.id: self._on_player_left(sid, name))
        monitor.status_update.connect(lambda data, sid=server.id: self._on_status_update(sid, data))
        monitor.log_line.connect(lambda line, sid=server.id: self._on_log_line(sid, line))
        monitor.initial_online_snapshot.connect(lambda names, sid=server.id: self._on_initial_online_snapshot(sid, names))
        monitor.start()

        self._log_monitors[server.id] = monitor

    def _on_player_joined(self, server_id: str, name: str) -> None:
        self._tracker_for_id(server_id).player_joined(name)
        self._online_by_server.setdefault(server_id, set()).add(name)
        if self.config.active_server_id == server_id:
            self.dashboard_page.set_online_players(self._online_by_server[server_id])
            self.players_page.set_online_players(self._online_by_server[server_id])

    def _on_player_left(self, server_id: str, name: str) -> None:
        self._tracker_for_id(server_id).player_left(name)
        self._online_by_server.setdefault(server_id, set()).discard(name)
        if self.config.active_server_id == server_id:
            self.dashboard_page.set_online_players(self._online_by_server[server_id])
            self.players_page.set_online_players(self._online_by_server[server_id])

    def _on_initial_online_snapshot(self, server_id: str, names: set) -> None:
        """Called once, the first time a server's log monitor attaches
        to an already-running log -- see LogMonitor.run()'s comment.
        We deliberately don't feed these into the session tracker (we
        don't know their real join time, so a fabricated one would
        pollute playtime stats); this only fixes the online-COUNT and
        the never-restart-while-online safety check, which is what
        actually mattered for #13."""
        self._online_by_server[server_id] = set(names)
        if self.config.active_server_id == server_id:
            self.dashboard_page.set_online_players(self._online_by_server[server_id])
            self.players_page.set_online_players(self._online_by_server[server_id])

    def _on_status_update(self, server_id: str, data: dict) -> None:
        if self.config.active_server_id == server_id:
            self.dashboard_page.on_status_update(data)

    def _on_log_line(self, server_id: str, line: str) -> None:
        if self.config.active_server_id == server_id:
            self.dashboard_page.on_log_line(line)

    # ------------------------------------------------------------ server --
    def _load_active_server(self) -> None:
        server = self.config.get_active()
        if not server:
            self.dashboard_page.set_empty()
            return

        if not server.backup_destination and server.install_dir:
            # Retroactively fixes a server set up before this default
            # existed (backup_destination stayed "" forever, silently
            # disabling scheduled/pre-update backups) -- see
            # backup_manager.default_backup_destination()'s docstring.
            # Persisted, not just displayed, so it sticks and the next
            # scheduled-backup check actually has somewhere to write to.
            server.backup_destination = backup_manager.default_backup_destination(server.install_dir)
            self.config.save()

        online = self._online_by_server.get(server.id, set())
        self.dashboard_page.set_server(server, online)
        self.players_page.set_tracker(self._tracker_for(server))
        self.players_page.set_server(server)
        self.players_page.set_online_players(online)
        self.settings_backups_page.set_server(server)
        self.updates_page.set_server(server)
        self.mods_page.set_server(server)
        self.access_page.set_server(server)
        self.console_page.set_server(server)

        self.settings_identity_page.load_committed(server.gameplay)
        self.settings_identity_page.set_install_dir(server.install_dir)
        self.settings_rates_page.load_committed(server.gameplay)
        for page in self.gameplay_pages.values():
            page.load_committed(server.gameplay)

        self.settings_network_page.load_committed({
            "name": server.name, "password": server.password,
            "game_port": server.game_port, "query_port": server.query_port,
            "bind_ip": server.bind_ip, "max_players": server.max_players,
        })
        self.settings_backups_page.load_committed({
            "backup_daily_keep": server.backup_daily_keep,
            "backup_weekly_keep": server.backup_weekly_keep,
            "backup_interval_hours": server.backup_interval_hours,
            "backup_destination": server.backup_destination,
            "backup_before_update": server.backup_before_update,
        })
        self.settings_restart_page.load_committed({
            "restart_enabled": server.restart_enabled,
            "restart_start": server.restart_start,
            "restart_end": server.restart_end,
        })
        self.settings_restart_page._refresh_suggestion()
        self.settings_alerts_page.load_committed({
            "rcon_enabled": server.rcon_enabled,
            "rcon_port": server.rcon_port,
            "rcon_password": server.rcon_password,
            "webhook_discord_url": server.webhook_discord_url,
            "webhook_ntfy_url": server.webhook_ntfy_url,
        })
        self.diagnostics_page.on_server_switched()

    def _reserved_ports_for_active(self) -> set:
        active = self.config.get_active()
        exclude = active.id if active else None
        return self.config.used_ports(exclude_id=exclude)

    def _hourly_activity_for_active(self):
        server = self.config.get_active()
        if not server:
            return []
        return self._tracker_for(server).hourly_activity_histogram()

    def _notify(self, server: ServerConfig, message: str, title: str = "ConanOps") -> None:
        full_title = f"{title} — {server.name}"
        # Tray toast is local/instant -- show it right away rather than
        # waiting on the webhook round-trip below.
        if self.tray_icon and self.tray_icon.isVisible():
            self.tray_icon.showMessage(full_title, message, QSystemTrayIcon.Information, 5000)

        worker = _NotifyWorker(server.webhook_discord_url, server.webhook_ntfy_url, message, full_title, server.name)
        worker.finished_notify.connect(self._on_notify_finished)
        self._notify_workers.append(worker)
        worker.finished_notify.connect(lambda *_, w=worker: self._notify_workers.remove(w) if w in self._notify_workers else None)
        self._retire_worker(worker)
        worker.start()

    def _on_notify_finished(self, ok: bool, server_name: str, title: str, message: str) -> None:
        if not ok:
            _log.warning(f"Alert delivery failed for {server_name}: {title} -- {message}")
            if self.tray_icon and self.tray_icon.isVisible():
                self.tray_icon.showMessage(
                    "Alert Delivery Failed",
                    f"Couldn't reach Discord/ntfy for \"{title}\" -- see conanops.log.",
                    QSystemTrayIcon.Warning, 5000,
                )

    def _start_network_reconcile(
        self, server: ServerConfig, remove: bool = False, legacy_names=(), old_ports=(),
        do_firewall: bool = True, do_upnp: bool = True, on_done=None,
    ) -> None:
        """Fire-and-forget: re-creates (or, with remove=True, deletes) a
        server's firewall rules and UPnP router forwards on a background
        thread -- one UAC prompt at most. Keeps a reference to the worker
        until it finishes, since an unreferenced QThread can be garbage
        collected out from under itself mid-run."""
        ports = list(old_ports)
        if remove:
            ports += [server.game_port, server.game_port + 1, server.query_port]
        worker = network_setup_runner.FirewallReconcileWorker(
            server.id, display_name=server.name, game_port=server.game_port, query_port=server.query_port,
            bind_ip=server.bind_ip, remove=remove,
            legacy_names=list(legacy_names) or [server.name], old_ports=ports,
            do_firewall=do_firewall, do_upnp=do_upnp,
            exe_path=process_manager.server_exe_path(server.install_dir) if server.install_dir else "",
        )
        worker.finished_reconcile.connect(
            lambda *_a, w=worker: self._firewall_workers.remove(w) if w in self._firewall_workers else None
        )
        if on_done is not None:
            worker.finished_reconcile.connect(on_done)
        self._firewall_workers.append(worker)
        self._retire_worker(worker)
        worker.start()

    def _start_network_cleanup_for_wizard(self, server_id: str, ports, bind_ip: str) -> None:
        """Used by the setup wizard when it's cancelled: removes whatever
        rules/forwards its Networking step created, via a tracked worker
        (so app shutdown waits for it like any other)."""
        worker = network_setup_runner.FirewallReconcileWorker(
            server_id, remove=True, old_ports=list(ports), bind_ip=bind_ip,
        )
        worker.finished_reconcile.connect(
            lambda *_a, w=worker: self._firewall_workers.remove(w) if w in self._firewall_workers else None
        )
        self._firewall_workers.append(worker)
        self._retire_worker(worker)
        worker.start()

    def _repair_network_for_active(self) -> None:
        """Network & Ports page's "Repair Networking" button."""
        server = self.config.get_active()
        if not server:
            self.settings_network_page.repair_finished()
            return
        if not server.bind_ip:
            import network_utils
            ip = network_utils.get_local_ip()
            if network_utils.is_usable_lan_ipv4(ip):
                server.bind_ip = ip
                self.config.save()

        def done(result, srv=server):
            self.settings_network_page.repair_finished()
            lines = []
            if result.get("error"):
                lines.append(f"Something went wrong: {result['error']}")
            for r in result.get("fw_results") or []:
                lines.append(("✓ " if r.success else "✗ ") + "Firewall: " + r.message)
            upnp = result.get("upnp")
            if upnp is None:
                pass
            elif not upnp.get("upnp_available"):
                lines.append("Router: automatic forwarding (UPnP) isn't available -- forward the ports "
                             "manually (Diagnostics has a step-by-step guide).")
            else:
                ok = all(upnp.get(k) for k in ("game_port_forwarded", "game_port_plus_one_forwarded",
                                                "query_port_forwarded"))
                lines.append("✓ Router: all three ports forwarded." if ok
                             else "✗ Router: some ports couldn't be forwarded automatically.")
                if upnp.get("double_nat"):
                    lines.append("⚠ Your router's internet address isn't public (double NAT or carrier-grade "
                                 "NAT) -- see Diagnostics.")
            QMessageBox.information(self, f"Repair Networking — {srv.name}", "\n".join(lines) or "Done.")

        self._start_network_reconcile(server, legacy_names=[server.name], on_done=done)

    # ------------------------------------------------------- settings io --
    def _retire_worker(self, worker) -> None:
        """Keeps `worker` referenced until Qt's own QThread.finished
        fires -- see self._retiring_workers' comment in __init__."""
        if worker is None:
            return
        self._retiring_workers.append(worker)
        worker.finished.connect(lambda w=worker: self._retiring_workers.remove(w) if w in self._retiring_workers else None)

    def _active(self) -> ServerConfig:
        return self.config.get_active()

    def _handle_config_changed(self, server: ServerConfig) -> None:
        self.config.save()

    def _apply_gameplay(self, values: dict) -> None:
        """Shared apply handler for every gameplay/identity settings
        page (Server Identity, Progression, and the 9 generic
        categories). `values` keys are real Conan ini key names (or a
        __-prefixed ConanOps-only key like __description, which is
        stored but never written to any .ini file)."""
        s = self._active()
        s.gameplay.update(values)
        self.config.save()

        if not s.install_dir:
            return
        settings_ini = os.path.join(s.install_dir, "ConanSandbox", "Saved", "Config", "WindowsServer", "ServerSettings.ini")
        updates = {}
        for key, value in values.items():
            if key.startswith("__"):
                continue
            spec = ini_field_specs.ALL_FIELDS_BY_KEY.get(key)
            section = spec.section if spec else ini_field_specs.DEFAULT_SECTION
            formatted = ini_utils.bool_to_ini(value) if isinstance(value, bool) else str(value)
            updates[key] = (section, formatted)
        if updates:
            ini_utils.apply_known_keys(settings_ini, updates)

    def _apply_network(self, values: dict) -> None:
        s = self._active()
        old_name = s.name
        old_game_port = s.game_port
        old_query_port = s.query_port
        old_bind_ip = s.bind_ip
        for k, v in values.items():
            setattr(s, k, v)
        self.config.save()

        # Firewall rules are keyed by server id + port, so only a PORT
        # change needs them redone (a rename no longer does). Router
        # forwards also depend on the bind address. Stale rules/forwards
        # for the old ports are removed in the same pass.
        ports_changed = s.game_port != old_game_port or s.query_port != old_query_port
        bind_changed = s.bind_ip != old_bind_ip
        if ports_changed or bind_changed:
            self._start_network_reconcile(
                s, legacy_names=[old_name, s.name],
                old_ports=[old_game_port, old_game_port + 1, old_query_port],
                do_firewall=ports_changed,
            )

        if s.install_dir:
            # ServerName/ServerPassword/Port/QueryPort placement below is
            # per Funcom's own dedicated-server dev-tracker post and
            # quickstart guide (Engine.ini, [OnlineSubsystemSteam] for
            # identity + query port, [URL] for the game port) -- an
            # earlier version of this wrote all four to a section called
            # "OnlineSubsystem" (missing "Steam"), which Conan doesn't
            # read at all, so none of these ever actually reached the
            # game. Port/QueryPort/MULTIHOME are also passed as launch
            # command-line args (process_manager.build_launch_args),
            # which take priority if the two ever disagree -- these ini
            # values mainly matter for anyone launching the exe outside
            # ConanOps, or before its first launch.
            engine_ini = os.path.join(s.install_dir, "ConanSandbox", "Saved", "Config", "WindowsServer", "Engine.ini")
            ini_utils.apply_known_keys(engine_ini, {
                "ServerName": ("OnlineSubsystemSteam", s.name),
                "ServerPassword": ("OnlineSubsystemSteam", s.password),
                "Port": ("URL", str(s.game_port)),
                "GameServerQueryPort": ("OnlineSubsystemSteam", str(s.query_port)),
                "MultiHome": ("URL", s.bind_ip),
            })
            # MaxPlayers lives in Game.ini under Unreal's own gamesession
            # section, not ServerSettings.ini -- a previous version wrote
            # it to ServerSettings.ini, where Conan never looks for it.
            game_ini = os.path.join(s.install_dir, "ConanSandbox", "Saved", "Config", "WindowsServer", "Game.ini")
            ini_utils.apply_known_keys(game_ini, {
                "MaxPlayers": ("/script/engine.gamesession", str(s.max_players)),
            })
        self._refresh_sidebar()
        self.console_page.set_server(s)  # picks up any RCON port/enabled change

    def _apply_backups_settings(self, values: dict) -> None:
        s = self._active()
        for k, v in values.items():
            setattr(s, k, v)
        self.config.save()

    def _apply_restart(self, values: dict) -> None:
        s = self._active()
        for k, v in values.items():
            setattr(s, k, v)
        self.config.save()

    def _apply_alerts(self, values: dict) -> None:
        s = self._active()
        for k, v in values.items():
            setattr(s, k, v)
        self.config.save()
        if s.install_dir:
            # RCON's actual home is Game.ini's [RconPlugin] section, per
            # both the community wiki and Funcom's own dev-tracker post
            # -- not Engine.ini. It also takes "1"/"0" for RconEnabled,
            # not "True"/"False". A previous version wrote RCONEnabled/
            # RCONPort to Engine.ini's "OnlineSubsystem" section (which
            # Conan doesn't read), used the wrong booleans, and never
            # wrote the password at all -- so RCON access effectively
            # never worked no matter what was configured here.
            game_ini = os.path.join(s.install_dir, "ConanSandbox", "Saved", "Config", "WindowsServer", "Game.ini")
            ini_utils.apply_known_keys(game_ini, {
                "RconEnabled": ("RconPlugin", "1" if s.rcon_enabled else "0"),
                "RconPassword": ("RconPlugin", s.rcon_password),
                "RconPort": ("RconPlugin", str(s.rcon_port)),
            })
        self.console_page.set_server(s)

    def _handle_mods_changed(self, server: ServerConfig) -> None:
        self.config.save()
        if server.install_dir and server.steamcmd_dir:
            mod_manager.write_modlist(server.install_dir, server.steamcmd_dir, server.mods)

    def _handle_update_applied(self, server: ServerConfig, buildid: str) -> None:
        self.config.save()
        self._notify(server, f"Updated to build {buildid}.", title="Update Applied")
        self._update_failure_streak.pop(server.id, None)  # a success (manual or automatic) clears any building streak

    # ---------------------------------------------------------- actions --
    # Grace period between an RCON broadcast warning and the restart
    # actually happening -- long enough to notice a message on screen,
    # short enough not to meaningfully delay the restart itself.
    _RESTART_WARNING_GRACE_SECONDS = 10

    def _handle_restart(self, server: ServerConfig) -> None:
        """Manual restart, from the Dashboard's Restart button: failures
        are shown as blocking dialogs since a person is sitting right
        there waiting on the click."""
        result = preflight.run_preflight(server)
        if not result.ok:
            if self._offer_reinstall_if_files_missing(server):
                return
            QMessageBox.warning(self, "Can't restart", "Pre-flight checks failed:\n\n" + "\n".join(result.problems))
            return
        if result.repairs:
            self._notify(server, "Auto-repaired before restart:\n" + "\n".join(result.repairs), title="Auto-Repair")
        self._warn_then_restart(server, manual=True)

    def _restart_unattended(self, server: ServerConfig) -> None:
        """Restart triggered by the scheduler, not a person clicking a
        button -- failures go through the normal alert channel
        (_notify: webhook/tray) instead of a blocking QMessageBox,
        which would otherwise pop up unattended (e.g. at 4am) and sit
        there requiring a click before anything else in the app could
        happen."""
        result = preflight.run_preflight(server)
        if not result.ok:
            self._notify(server, "Scheduled restart skipped -- pre-flight checks failed:\n" + "\n".join(result.problems), title="Restart Failed")
            return
        if result.repairs:
            self._notify(server, "Auto-repaired before restart:\n" + "\n".join(result.repairs), title="Auto-Repair")
        self._warn_then_restart(server, manual=False)

    def _warn_then_restart(self, server: ServerConfig, manual: bool) -> None:
        """Sends a short RCON broadcast to anyone currently online
        before the server actually restarts, then waits a brief grace
        period so they have a moment to see it -- rather than just
        disconnecting them with zero notice, which is what a manual
        Restart click did before this existed (the scheduled-restart
        path already refuses to fire at all while anyone's online, so
        this mostly matters here; it's still attempted there too, as a
        defense against the narrow race of someone joining in the gap
        between that check and the restart actually happening).

        Delayed via QTimer.singleShot rather than a blocking sleep, so
        the UI stays responsive during the grace period instead of
        freezing for it. Best-effort throughout: if RCON is disabled,
        or nobody's actually online, this skips straight to
        restarting with no delay at all."""
        online = bool(self._online_by_server.get(server.id))
        if server.rcon_enabled and online:
            banlist_manager.broadcast_message(
                server, f"Server restarting in {self._RESTART_WARNING_GRACE_SECONDS} seconds...",
            )
            QTimer.singleShot(
                self._RESTART_WARNING_GRACE_SECONDS * 1000,
                lambda: self._finish_restart(server, manual),
            )
        else:
            self._finish_restart(server, manual)

    def _finish_restart(self, server: ServerConfig, manual: bool) -> None:
        def report(e):
            if manual:
                QMessageBox.critical(self, "Restart failed", str(e))
            else:
                self._notify(server, f"Scheduled restart failed: {e}", title="Restart Failed")
        self._do_restart(server, on_error=report)

    def _do_restart(self, server: ServerConfig, on_error=None) -> None:
        """Shared restart mechanics for both paths above, run off the UI
        thread (the stop half waits for the world to finish saving).
        A FileNotFoundError (missing server exe) goes to on_error; with
        no on_error it's raised (inline/test mode) or logged."""
        self._expected_stop.add(server.id)
        self._tracker_for(server).close_all_active()
        self._online_by_server[server.id] = set()

        def done(_result, sid=server.id):
            self._known_running[sid] = True
            self._expected_stop.discard(sid)
            self._refresh_chrome()

        def failed(e, sid=server.id):
            self._expected_stop.discard(sid)
            if on_error is not None:
                on_error(e)
            else:
                raise e

        self._run_op(lambda: process_manager.restart(server), on_done=done,
                     on_error=failed if (on_error is not None or not self.RUN_OPS_INLINE) else None)

    def _handle_start(self, server: ServerConfig) -> None:
        """Manual start, from the Dashboard's Start button. Mirrors
        _handle_restart's preflight-then-act shape, but only launches
        -- there's nothing running yet to gracefully stop first."""
        if process_manager.is_running(server.install_dir):
            return  # Start button shouldn't even be visible in this case, but don't double-launch
        result = preflight.run_preflight(server)
        if not result.ok:
            if self._offer_reinstall_if_files_missing(server):
                return
            QMessageBox.warning(self, "Can't start", "Pre-flight checks failed:\n\n" + "\n".join(result.problems))
            return
        if result.repairs:
            self._notify(server, "Auto-repaired before start:\n" + "\n".join(result.repairs), title="Auto-Repair")
        try:
            process_manager.launch(server)
            self._known_running[server.id] = True
            # An explicit Start means "I want this running" -- persisted
            # so auto-resume (_auto_resume_servers(), called on the next
            # app/PC startup) knows to bring it back up without being
            # told again. Not touched by a scheduled/watchdog restart
            # (_do_restart(), _attempt_watchdog_restart()) -- those only
            # ever happen for a server this was already true for.
            server.desired_running = True
            # Clicking Start is the person overriding a failed-update hold
            # (see _handle_update_failure) -- run what's installed.
            server.update_hold = ""
            server.mod_recovery = {}  # starting by hand ends any wait for a mod fix
            self._post_update_watch.pop(server.id, None)
            self.config.save()
            self._watchdog_attempts.pop(server.id, None)  # fresh start -- any prior crash-streak no longer applies
        except FileNotFoundError as e:
            QMessageBox.critical(self, "Start failed", str(e))

    def _handle_kick_player(self, server: ServerConfig, name: str) -> None:
        message = banlist_manager.kick_player(server, name)
        self._notify(server, message, title="Player Kicked")

    def _handle_ban_player(self, server: ServerConfig, player_name: str, steam_id: str) -> None:
        message = banlist_manager.ban_player(server, steam_id)
        self._notify(server, f"{player_name} ({steam_id}): {message}", title="Player Banned")
        if self.config.active_server_id == server.id:
            self.access_page.set_server(server)  # refresh the ban list shown there too

    def _handle_stop(self, server: ServerConfig) -> None:
        """Manual stop, from the Dashboard's Stop button. Uses the same
        graceful-then-hard-kill path as a restart's stop half (see
        process_manager.graceful_stop's docstring), just without the
        relaunch afterward."""
        if not process_manager.is_running(server.install_dir):
            return  # Stop button shouldn't even be visible in this case
        self._expected_stop.add(server.id)
        self._tracker_for(server).close_all_active()
        self._online_by_server[server.id] = set()
        # An explicit Stop means "I don't want this running" -- the
        # other half of the desired_running contract described in
        # _handle_start() above. This is what keeps a deliberately-
        # stopped server down through a reboot instead of auto-
        # resume bringing it back up uninvited. Recorded now, before the
        # (off-thread) stop finishes, so the watchdog can't race it.
        server.desired_running = False
        self.config.save()
        self._watchdog_attempts.pop(server.id, None)
        self._post_update_watch.pop(server.id, None)

        def done(_result, sid=server.id):
            self._known_running[sid] = False
            self._expected_stop.discard(sid)
            self._refresh_chrome()

        self._run_op(lambda: process_manager.graceful_stop(server), on_done=done,
                     on_error=lambda e, sid=server.id: (self._expected_stop.discard(sid),
                                                        _log.error(f"Stop failed: {e}")))

    def _handle_scheduled_backup(self, server: ServerConfig) -> None:
        if not (server.backup_destination and server.install_dir):
            return
        try:
            entry = backup_manager.create_backup_for_server(server, server.backup_destination, backup_manager.TRIGGER_SCHEDULED)
        except backup_manager.BackupSpaceError as e:
            self._notify(server, f"Scheduled backup skipped -- {e}", title="Backup Failed")
            return
        if entry is None:
            saved = backup_manager.saved_dir(server.install_dir)
            if os.path.isdir(saved):
                self._notify(
                    server,
                    "Scheduled backup failed -- the backup file didn't pass its own integrity check "
                    "after being written, and was deleted. Check the app's log for details -- this "
                    "usually means a disk problem on the backup destination's drive.",
                    title="Backup Failed",
                )
            else:
                self._notify(
                    server,
                    f"Scheduled backup failed -- couldn't find the server's Saved folder ({saved}). "
                    f"Check the Diagnostics settings tab for more.",
                    title="Backup Failed",
                )
            return
        # Stamp last_backup_at only on success (see Scheduler._check_backup)
        # so a failed backup is retried on the next tick instead of
        # waiting a full backup_interval_hours.
        server.last_backup_at = datetime.now().isoformat()
        self.config.save()
        backup_manager.prune_backups(server.backup_destination, server.backup_daily_keep, server.backup_weekly_keep)
        if self.config.active_server_id == server.id:
            self.settings_backups_page.refresh()

    def _handle_restart_skipped_online(self, server: ServerConfig) -> None:
        self._notify(
            server,
            "Today's scheduled restart window is open, but players are still "
            "online -- skipping today and trying again next time.",
            title="Restart Skipped",
        )

    def _handle_scheduled_restart(self, server: ServerConfig) -> None:
        """Per the request: a scheduled restart always checks SteamCMD
        for an update first. If one's available, it updates (which
        stops, updates, and relaunches the server) instead of doing a
        plain restart; otherwise it just restarts normally."""
        if server.id in self._update_check_workers or server.id in self._update_apply_workers:
            self._restart_unattended(server)  # an update is already in flight; just restart plainly
            return
        self._notify(server, "Scheduled restart starting -- checking for updates first.", title="Scheduled Restart")
        worker = CheckWorker(server.steamcmd_dir)
        worker.finished_check.connect(
            lambda latest, info, srv=server: self._on_restart_update_check(srv, latest)
        )
        self._update_check_workers[server.id] = worker
        self._retire_worker(worker)
        worker.start()

    def _on_restart_update_check(self, server: ServerConfig, latest_buildid) -> None:
        self._update_check_workers.pop(server.id, None)
        if latest_buildid and latest_buildid != server.installed_buildid:
            self._notify(server, f"Update {latest_buildid} found during scheduled restart -- updating now.", title="Scheduled Restart")
            self._start_update_apply(server)
        else:
            self._restart_unattended(server)

    def _is_update_busy(self, server: ServerConfig) -> bool:
        """True if an update-check or update-apply -- automatic OR
        manual (Updates page) -- is already in flight for this server.
        Shared by every path that starts a new update/check so a
        manual click and an automatic one can't race each other."""
        return (
            server.id in self._update_check_workers
            or server.id in self._update_apply_workers
            or server.id in self._manual_update_in_progress
        )

    def _handle_update_check_due(self, server: ServerConfig) -> None:
        """The independent periodic check (separate from the restart-
        triggered one above): if auto-update is on, this fires every
        `auto_update_check_interval_hours`. If it finds a new build and
        nobody's currently online, it updates immediately rather than
        waiting for the next scheduled restart."""
        if self._is_update_busy(server):
            return
        worker = CheckWorker(server.steamcmd_dir)
        worker.finished_check.connect(
            lambda latest, info, srv=server: self._on_periodic_update_check(srv, latest)
        )
        self._update_check_workers[server.id] = worker
        self._retire_worker(worker)
        worker.start()

    def _on_periodic_update_check(self, server: ServerConfig, latest_buildid) -> None:
        self._update_check_workers.pop(server.id, None)
        if not latest_buildid:
            # The check itself failed (SteamCMD missing/timed out/no
            # network) -- don't stamp last_update_check_at, so the next
            # tick sees the interval as still not satisfied and retries
            # soon instead of waiting a full auto_update_check_interval_hours.
            return
        # The check succeeded, whether or not it found anything new --
        # stamp it now so we don't re-check again until the next
        # interval elapses.
        server.last_update_check_at = datetime.now().isoformat()
        self.config.save()
        if latest_buildid == server.installed_buildid:
            return
        online = bool(self._online_by_server.get(server.id))
        if online:
            return  # leave it pending -- next check, or the next scheduled restart, will catch it
        self._notify(server, f"Update {latest_buildid} detected and nobody's online -- updating now.", title="Auto-Update")
        self._start_update_apply(server)

    def _start_update_apply(self, server: ServerConfig, relaunch_after: bool = False) -> None:
        """Shared update sequence for the automatic paths: UpdateWorker
        stops the server, backs up the (now closed) world, checks disk
        space and runs SteamCMD -- all off the UI thread. Relaunching
        afterwards (or not, after a failure) is _on_update_apply_finished's
        call. relaunch_after: start it afterwards even though it isn't
        running now -- used when retrying an update that left a server
        held stopped. Manual updates from the Updates page build the same
        worker themselves (`ui/updates_page.py`)."""
        if self._is_update_busy(server):
            return
        running = process_manager.is_running(server.install_dir) if server.install_dir else False
        was_running = running or relaunch_after
        if running:
            self._expected_stop.add(server.id)
            self._tracker_for(server).close_all_active()
            self._online_by_server[server.id] = set()
            self._known_running[server.id] = False

        backup = bool(server.backup_before_update and server.backup_destination and server.install_dir)
        worker = UpdateWorker(
            server.steamcmd_dir, server.install_dir,
            stop_server=server if running else None,
            backup_server=server if backup else None,
            backup_destination=server.backup_destination if backup else "",
        )
        worker.finished_update.connect(
            lambda result, srv=server, wr=was_running: self._on_update_apply_finished(srv, result, wr)
        )
        self._update_apply_workers[server.id] = worker
        self._retire_worker(worker)
        worker.start()

    def _on_update_apply_finished(self, server: ServerConfig, result, was_running: bool) -> None:
        self._update_apply_workers.pop(server.id, None)
        self._expected_stop.discard(server.id)

        if result.success:
            server.installed_buildid = result.installed_buildid or server.installed_buildid
            server.update_hold = ""
            self._update_retry_pending.discard(server.id)
            self.config.save()
            self._notify(server, f"Updated to build {server.installed_buildid}.", title="Update Applied")
            self._update_failure_streak.pop(server.id, None)
            if self.config.active_server_id == server.id:
                self.updates_page.set_server(server)
            self._update_mods_then_relaunch(server, was_running, watch=True)
            return

        streak = self._update_failure_streak.get(server.id, 0) + 1
        self._update_failure_streak[server.id] = streak
        self._handle_update_failure(server, result, was_running)
        if streak >= self._UPDATE_FAILURE_ALERT_THRESHOLD:
            self._notify(
                server,
                f"Automatic updates have failed {streak} times in a row for this server. ConanOps "
                f"will keep trying, but something likely needs attention -- check SteamCMD, disk "
                f"space, and network connectivity.",
                title="Repeated Update Failures",
            )

    _UPDATE_RETRY_MINUTES = 30

    def _handle_update_failure(self, server: ServerConfig, result, was_running: bool) -> None:
        """A failed update never relaunches the old build: Steam has
        already updated players' games, so they couldn't join it anyway,
        and a SteamCMD run that died partway may have left a mix of old
        and new files that won't even start. The server stays stopped
        (update_hold keeps auto-resume and the watchdog from starting
        it), the person is told why, and the update is retried in
        _UPDATE_RETRY_MINUTES -- much sooner than the normal check
        interval. Clicking Start overrides the hold."""
        reasons = {
            "disk": result.output,
            "backup": result.output,
            "steamcmd": "SteamCMD couldn't finish the update (Steam busy or unreachable, or files "
                        "locked). See the app's console output for SteamCMD's log.",
        }
        why = reasons.get(getattr(result, "reason", ""), "The update didn't finish -- see the app's console output.")
        if not was_running:
            self._notify(server, f"Automatic update failed. {why}", title="Update Failed")
            return
        server.update_hold = why
        self.config.save()
        self._notify(
            server,
            f"Update failed, so the server was left stopped rather than started on the old version "
            f"(players with the updated game couldn't join it). {why} Retrying in "
            f"{self._UPDATE_RETRY_MINUTES} minutes -- or click Start to run the old version anyway.",
            title="Update Failed -- Server Held",
        )
        self._schedule_update_retry(server)

    def _schedule_update_retry(self, server: ServerConfig) -> None:
        if server.id in self._update_retry_pending:
            return
        self._update_retry_pending.add(server.id)

        def retry(sid=server.id):
            self._update_retry_pending.discard(sid)
            srv = next((x for x in self.config.servers if x.id == sid), None)
            if srv is None or not srv.update_hold or self._is_update_busy(srv):
                return  # removed, already resolved (manual Start or update), or busy
            self._start_update_apply(srv, relaunch_after=True)

        QTimer.singleShot(self._UPDATE_RETRY_MINUTES * 60 * 1000, retry)

    def _update_mods_then_relaunch(self, server: ServerConfig, was_running: bool, watch: bool = False) -> None:
        """Shared tail end of both the automatic and manual server-
        update flows (see _relaunch_after_manual_update below): a
        server update and a mod update are two completely independent
        SteamCMD/Workshop downloads -- finishing one never refreshes
        the other. Before this, a mod that Funcom's latest build
        needed a newer version of (or that had simply been updated on
        the Workshop since ConanOps last downloaded it) would keep
        running its old, possibly now-incompatible .pak file after
        every single automatic or manual server update, silently,
        until someone happened to notice something broken and thought
        to check the Mods tab.

        Only actually does anything when there ARE enabled mods and a
        SteamCMD folder to fetch them with -- a server running with no
        mods at all skips straight to relaunching, same as before this
        existed."""
        # Every mod, not just enabled ones -- same as the Mods tab's own
        # Download button. Refreshing only enabled mods let disabled
        # ones go stale, so re-enabling one later loaded an outdated .pak.
        mod_ids = [m["id"] for m in server.mods]
        if not mod_ids or not server.steamcmd_dir:
            self._relaunch_if_was_running(server, was_running, watch=watch)
            return
        existing = self._mod_refresh_workers.get(server.id)
        if existing is not None:
            # A periodic, independent mod-refresh check (see
            # _handle_mod_refresh_due) is already running for this
            # server -- two ModDownloadWorkers hitting the same
            # SteamCMD Workshop folder at once could corrupt the
            # download, so wait for that one to finish and relaunch
            # off ITS result instead of starting a second, competing
            # worker.
            existing.finished_download.connect(
                lambda result, srv=server, wr=was_running, w=watch: self._on_post_update_mod_refresh_finished(srv, result, wr, w)
            )
            return
        worker = ModDownloadWorker(server.steamcmd_dir, mod_ids)
        worker.finished_download.connect(
            lambda result, srv=server, wr=was_running, w=watch: self._on_post_update_mod_refresh_finished(srv, result, wr, w)
        )
        self._mod_refresh_workers[server.id] = worker
        self._retire_worker(worker)
        worker.start()

    def _on_post_update_mod_refresh_finished(self, server: ServerConfig, result, was_running: bool, watch: bool = False) -> None:
        self._mod_refresh_workers.pop(server.id, None)
        # Rewrite modlist.txt BEFORE relaunching: a mod downloaded for
        # the first time (or whose author renamed its .pak in this
        # update) has a different real path now, and relaunching onto
        # the old guessed/stale path loads the server without it.
        # Done whether or not the refresh fully succeeded -- whatever
        # did land should be pointed at correctly.
        self._handle_mods_changed(server)
        if not result.success:
            self._notify(
                server,
                f"Mods couldn't be refreshed after the update: {result.output}. The server will still "
                f"restart with whatever mod files it already has -- check the Mods tab.",
                title="Mod Update Failed",
            )
        self._relaunch_if_was_running(server, was_running, watch=watch)

    def _relaunch_if_was_running(self, server: ServerConfig, was_running: bool, watch: bool = False) -> None:
        if not was_running:
            return
        try:
            process_manager.launch(server)
            self._known_running[server.id] = True
            if watch:
                self._begin_post_update_watch(server)
        except FileNotFoundError as e:
            self._notify(server, f"Update finished but relaunch failed: {e}", title="Restart Failed")

    # ------------------------------------------------- post-update watch --
    # For this long after an update relaunches a server, a crash or hang
    # is treated as "the update broke something" (almost always a mod
    # built for the old game version) rather than a one-off: one normal
    # watchdog retry, then the server is held stopped instead of being
    # restarted over and over, and the person gets a list of the mods
    # that haven't been updated for the current patch.
    _POST_UPDATE_WATCH_SECONDS = 15 * 60
    _POST_UPDATE_FAILURES_BEFORE_HOLD = 2

    def _begin_post_update_watch(self, server: ServerConfig) -> None:
        self._post_update_watch[server.id] = {"until": time.monotonic() + self._POST_UPDATE_WATCH_SECONDS, "failures": 0}

    def _post_update_failure(self, server: ServerConfig, what: str) -> bool:
        """Called on a crash or hang. Returns True if the post-update
        watch took over (the caller must NOT run the normal watchdog
        restart)."""
        watch = self._post_update_watch.get(server.id)
        if watch is None:
            return False
        if time.monotonic() > watch["until"]:
            self._post_update_watch.pop(server.id, None)
            return False
        watch["failures"] += 1
        if watch["failures"] < self._POST_UPDATE_FAILURES_BEFORE_HOLD:
            return False  # first one: let the watchdog try once, could be a fluke
        self._post_update_watch.pop(server.id, None)
        self._known_running[server.id] = False
        if self._start_mod_recovery(server, what):
            return True  # the mod check stops the server itself, safely, before testing
        if server.install_dir and process_manager.is_running(server.install_dir):
            self._expected_stop.add(server.id)
            self._run_op(lambda: process_manager.graceful_stop(server),
                         on_done=lambda _r, sid=server.id: self._expected_stop.discard(sid),
                         on_error=lambda _e, sid=server.id: self._expected_stop.discard(sid))
        self._known_running[server.id] = False
        server.update_hold = (f"It {what} repeatedly right after the game update -- most likely a mod that "
                              f"hasn't been updated for the new version.")
        self.config.save()
        self._diagnose_post_update_mods(server, what)
        return True

    def _diagnose_post_update_mods(self, server: ServerConfig, what: str) -> None:
        enabled = [m["id"] for m in server.mods if m.get("enabled", True)]
        base = (f"{server.name} {what} repeatedly right after the game update, so it's been left stopped "
                f"to protect the world. ")
        tail = ("Find the culprit with Mods → Find All Bad Mods (it protects your world while testing). "
                "Don't just switch mods off and start the server: anything a disabled mod added is deleted "
                "from the world when it saves. Click Start to try again anyway.")
        if not enabled:
            self._notify(server, base + "There are no mods enabled, so it may be a problem with the update "
                                        "itself -- try Updates → Back Up & Update Now to re-validate the files. "
                                        "Click Start to try again.", title="Update Broke the Server")
            return
        from workshop_search_runner import ModStatusWorker
        import steam_workshop_api as swa
        worker = ModStatusWorker(enabled, api_key=self.config.steam_api_key,
                                 cutoff_ts=swa.cutoff_timestamp(self.config.workshop_update_cutoff))

        def report(result, srv=server):
            names = {m["id"]: (m.get("name") or m["id"]) for m in srv.mods}
            suspects = []
            if result.ok:
                for mid in enabled:
                    info = result.items.get(mid)
                    if info is None or info.status in (swa.STATUS_STALE, swa.STATUS_LEGACY, swa.STATUS_UNKNOWN):
                        suspects.append(names.get(mid, mid))
            lead = (f"Mods not updated for the current game version: {', '.join(suspects)}. "
                    if suspects else "Couldn't tell which mod from the Workshop alone. ")
            self._notify(srv, base + lead + tail, title="Update Broke the Server")

        worker.finished_status.connect(report)
        self._retire_worker(worker)
        worker.start()

    # ------------------------------------------------------- off-thread ops --
    # Tests set this so stop/restart run synchronously, as they did before
    # these moved off the UI thread (see tests/conftest.py).
    RUN_OPS_INLINE = False

    def _run_op(self, fn, on_done=None, on_error=None) -> None:
        if self.RUN_OPS_INLINE:
            try:
                result = fn()
            except Exception as e:  # noqa: BLE001
                if on_error is None:
                    raise
                on_error(e)
                return
            if on_done:
                on_done(result)
            return
        worker = _OpWorker(fn)
        if on_done:
            worker.done.connect(on_done)
        worker.failed.connect(on_error if on_error else (lambda e: _log.error(f"Server operation failed: {e}")))
        self._retire_worker(worker)
        worker.start()

    def _claim_mod_refresh_worker(self, server_id: str, worker) -> bool:
        """Shared registry for anything that downloads mods via
        SteamCMD for a given server -- the periodic daily mod-refresh
        check, a server update's own post-update mod refresh, and a
        manual Download Mods click on the Mods tab all go through this
        same dict, so at most one of them is ever running against a
        given server's Workshop content folder at once. Returns False
        (and registers nothing) if a slot is already taken."""
        if server_id in self._mod_refresh_workers:
            return False
        self._mod_refresh_workers[server_id] = worker
        return True

    def _release_mod_refresh_worker(self, server_id: str) -> None:
        self._mod_refresh_workers.pop(server_id, None)

    def _handle_mod_refresh_due(self, server: ServerConfig) -> None:
        """Independent of any server-build update -- a mod can get its
        own new Workshop version on a completely different schedule
        than Funcom ships a server build (see scheduler.py's
        _check_mod_refresh docstring). Deliberately doesn't restart
        the server even if new mod files land: unlike an update-
        triggered refresh (which already has a restart in flight
        anyway), forcing a restart here would be a second, unasked-for
        way this app disrupts a running server -- the new files just
        sit ready for whenever the server next restarts on its own
        (scheduled, watchdog, or a person clicking Restart)."""
        if server.id in self._mod_refresh_workers:
            return  # already busy (an update-triggered refresh, most likely) -- try again next time this fires
        mod_ids = [m["id"] for m in server.mods]  # all mods -- see _update_mods_then_relaunch
        if not mod_ids or not server.steamcmd_dir:
            server.last_mod_check_at = datetime.now().isoformat()
            self.config.save()
            return
        worker = ModDownloadWorker(server.steamcmd_dir, mod_ids)
        worker.finished_download.connect(
            lambda result, srv=server: self._on_periodic_mod_refresh_finished(srv, result)
        )
        self._mod_refresh_workers[server.id] = worker
        self._retire_worker(worker)
        worker.start()

    def _on_periodic_mod_refresh_finished(self, server: ServerConfig, result) -> None:
        self._mod_refresh_workers.pop(server.id, None)
        # Stamped whether it succeeded or failed -- see
        # scheduler.py's _check_mod_refresh docstring for why this
        # (unlike the update-check's last_update_check_at) doesn't
        # need a short-retry-on-failure backoff: a failed mod check
        # just tries again in another ~24h, gentler than hammering
        # SteamCMD every few minutes for a low-urgency background check.
        server.last_mod_check_at = datetime.now().isoformat()
        # Saves config AND rewrites modlist.txt -- a mod that just
        # finished downloading (or got a renamed .pak) needs its real
        # path in the file for the server's next restart to load it.
        self._handle_mods_changed(server)
        if not result.success:
            self._notify(
                server,
                f"Periodic mod-update check failed: {result.output}. Will check again in about a day.",
                title="Mod Check Failed",
            )
        # Success is deliberately silent -- most checks find nothing
        # new, and "checked, nothing changed" once a day per server
        # would just be noise. If something DID update, the new files
        # are simply in place for whenever the server next restarts.

    def _handle_pre_update_backup(self, server: ServerConfig) -> None:
        if server.backup_before_update and server.backup_destination:
            try:
                backup_manager.create_backup_for_server(server, server.backup_destination, backup_manager.TRIGGER_PRE_UPDATE)
            except backup_manager.BackupSpaceError as e:
                self._notify(server, f"Pre-update backup skipped -- {e}", title="Backup Failed")

    def _stop_before_manual_update(self, server: ServerConfig) -> bool:
        """Called by UpdatesPage right before it starts a manual
        SteamCMD update: stops the server first (a manual update used
        to run against a live server, same as the automatic path was
        already careful to avoid) and marks it busy so an automatic
        update-check can't start a second, overlapping update for the
        same server. Returns whether the server was running, so the
        caller knows whether to relaunch afterward."""
        self._manual_update_in_progress.add(server.id)
        was_running = process_manager.is_running(server.install_dir) if server.install_dir else False
        if was_running:
            # The stop itself now happens inside UpdateWorker, off the UI
            # thread (see ui/updates_page.py) -- just the bookkeeping here.
            self._expected_stop.add(server.id)
            self._tracker_for(server).close_all_active()
            self._online_by_server[server.id] = set()
            self._known_running[server.id] = False
        return was_running

    def _relaunch_after_manual_update(self, server: ServerConfig, was_running: bool, result=None) -> None:
        self._manual_update_in_progress.discard(server.id)
        self._expected_stop.discard(server.id)
        if result is not None and not result.success:
            self._handle_update_failure(server, result, was_running)
            return
        if result is not None and result.success and server.update_hold:
            server.update_hold = ""
            self.config.save()
        self._update_mods_then_relaunch(server, was_running, watch=result is not None)

    def _handle_restore(self, server: ServerConfig, entry) -> None:
        if server.id in self._restore_workers:
            return  # a restore for this server is already in flight
        self._expected_stop.add(server.id)
        self._tracker_for(server).close_all_active()
        self._online_by_server[server.id] = set()
        was_running = process_manager.is_running(server.install_dir) if server.install_dir else False

        worker = backup_runner.RestoreWorker(server, entry, was_running)
        worker.finished_restore.connect(lambda ok, err, srv=server: self._on_restore_finished(srv, ok, err))
        self._restore_workers[server.id] = worker
        self._retire_worker(worker)
        worker.start()

    def _on_restore_finished(self, server: ServerConfig, ok: bool, err: str) -> None:
        self._restore_workers.pop(server.id, None)
        self._expected_stop.discard(server.id)
        if ok:
            self._known_running[server.id] = process_manager.is_running(server.install_dir) if server.install_dir else False
            self._notify(server, "Backup restored and server relaunched.", title="Restore Complete")
        else:
            _log.error(f"Restore failed for {server.name}: {err}")
            self._notify(server, f"Restore failed: {err}", title="Restore Failed")

    def _open_setup_wizard(self, server: ServerConfig) -> None:
        wizard = SetupWizard(
            server,
            get_reserved_ports=lambda: self.config.used_ports(exclude_id=server.id),
            get_reserved_dirs=lambda: self.config.used_install_dirs(exclude_id=server.id),
            get_default_steamcmd_dir=lambda: self.config.default_steamcmd_dir(exclude_id=server.id),
            get_reserved_names=lambda: self.config.used_server_names(exclude_id=server.id),
            get_backup_sources=lambda: self._backup_sources_for_wizard(exclude_id=server.id),
            start_network_cleanup=self._start_network_cleanup_for_wizard,
            parent=self,
        )
        if wizard.exec():
            wizard.apply_to_server()
            self.config.save()
            keep_page = getattr(wizard, "keep_running_page", None)
            if keep_page is not None and keep_page.keep_running:
                self._apply_keep_running()
            if not self.config.tour_done:
                QTimer.singleShot(600, self.start_tour)
            self._ensure_monitor(server)
            self._known_running[server.id] = False
            self._refresh_sidebar()
            if self.config.active_server_id == server.id:
                self._load_active_server()
        elif not server.install_dir:
            # This method is only ever called with a server that has no
            # install_dir yet -- dashboard_page.py's "Set Up Server"
            # button is only visible in that state, and _on_add_server()
            # opens the wizard immediately after creating one. So
            # cancelling here always means "never mind, I didn't want to
            # add this server after all" -- not "I changed my mind about
            # redoing an existing server's setup" (nothing can reach this
            # method for an already-configured server to test that
            # against). Without this, the placeholder _on_add_server()
            # already saved to disk before the wizard even opened would
            # be left behind: an empty, unconfigured server sitting in
            # the sidebar forever. The `not server.install_dir` guard is
            # what keeps this safe even if that invariant ever changes --
            # an already-configured server just gets left untouched.
            self.config.remove_server(server.id)
            self.config.save()
            self._refresh_sidebar()
            self._load_active_server()

    def _backup_sources_for_wizard(self, exclude_id: str) -> list:
        """Every backup from every OTHER configured server, newest
        first overall, for the setup wizard's "restore from a backup"
        step -- lets a new server be seeded from an existing one's
        world instead of always starting fresh. Returns (label, path)
        tuples ready for a combo box."""
        candidates = []
        for s in self.config.servers:
            if s.id == exclude_id or not s.backup_destination:
                continue
            for backup in backup_manager.list_backups(s.backup_destination):
                label = f"{s.name} — {backup.when.strftime('%Y-%m-%d %H:%M')} ({backup.trigger}, {backup.size_label})"
                candidates.append((backup.when, label, backup.path))
        candidates.sort(key=lambda c: c[0], reverse=True)
        return [(label, path) for _when, label, path in candidates]

    # --------------------------------------------------- health / crash --
    # Watchdog backoff: delays (seconds) between successive crash-restart
    # attempts for the SAME server, indexed by how many have happened in
    # the current streak -- the last value repeats once attempts exceed
    # the list's length. After _WATCHDOG_MAX_ATTEMPTS in a row, ConanOps
    # stops trying and alerts instead of looping forever.
    _WATCHDOG_BACKOFF_SECONDS = [30, 60, 120, 300, 300]
    _WATCHDOG_MAX_ATTEMPTS = 5
    # A crash streak this long after the LAST restart attempt counts as
    # "it ran fine for a good while" rather than "still crash-looping" --
    # without this, a server that crashes only rarely (say, once a
    # month) would eventually exhaust its 5 attempts over its whole
    # lifetime and stop being auto-restarted at all, which defeats the
    # entire point of a watchdog for exactly the servers that need it
    # least urgently but still need it eventually.
    _WATCHDOG_STREAK_RESET_SECONDS = 3600
    # How long a RUNNING server can go without answering a query before
    # it's treated as hung (not crashed -- the process is still there)
    # and gets a stop-then-restart. Long enough that a server that's
    # simply slow to finish loading right after starting doesn't get
    # mistaken for hung (worst case here is a few extra minutes of
    # downtime waiting this out; the failure mode on the other side --
    # restarting a server that was about to come up fine on its own --
    # is worse, since it throws away however long it had already been
    # loading and starts that whole process over).
    _HUNG_THRESHOLD_SECONDS = 300

    # Consecutive AUTOMATIC update failures (manual failures are already
    # visible right in the Updates tab to whoever just clicked the
    # button, so this is scoped to the background path specifically)
    # before ConanOps sends a distinct "something needs attention"
    # alert -- separate from the per-attempt "Update Failed" notice,
    # which still fires every time regardless of streak length.
    _UPDATE_FAILURE_ALERT_THRESHOLD = 3

    def _start_health_check(self) -> None:
        if self._health_worker is not None:
            return  # a previous check is still running -- skip this tick rather than overlap
        self._health_worker = health_check_runner.HealthCheckWorker(list(self.config.servers), parent=self)
        self._health_worker.finished_check.connect(self._on_health_check_finished)
        worker = self._health_worker
        self._retiring_workers.append(worker)
        worker.finished.connect(lambda w=worker: self._retiring_workers.remove(w) if w in self._retiring_workers else None)
        self._health_worker.start()

    def _on_health_check_finished(self, results: list) -> None:
        self._health_worker = None
        by_id = {sid: (running, info) for sid, running, info in results}
        for server in self.config.servers:
            if server.id not in by_id:
                continue  # removed from config, or added after this check started -- picked up next tick
            running_now, info = by_id[server.id]
            was_running = self._known_running.get(server.id, False)

            if was_running and not running_now and server.id not in self._expected_stop and server.id not in self._automation_locked:
                self._tracker_for(server).close_all_active()
                self._online_by_server[server.id] = set()
                self._notify(server, "The server process stopped unexpectedly (crash or external kill).", title="Server Crashed")
                self._unresponsive_since.pop(server.id, None)
                self._known_running[server.id] = False
                if self._post_update_failure(server, "crashed"):
                    continue
                if server.desired_running and not server.update_hold:
                    self._attempt_watchdog_restart(server, reason="it crashed", need_stop_first=False)
                continue

            if running_now and server.id not in self._expected_stop and server.id not in self._automation_locked:
                self._known_running[server.id] = True
                self._track_hung_state(server, info)
                continue

            self._unresponsive_since.pop(server.id, None)
            self._known_running[server.id] = running_now

    def _track_hung_state(self, server: ServerConfig, info: Optional[dict]) -> None:
        """Bookkeeping only -- info was already fetched off the UI
        thread by HealthCheckWorker, so nothing here does any I/O."""
        if info is not None:
            self._unresponsive_since.pop(server.id, None)
            return
        now = time.monotonic()
        since = self._unresponsive_since.get(server.id)
        if since is None:
            self._unresponsive_since[server.id] = now
            return
        if now - since < self._HUNG_THRESHOLD_SECONDS:
            return
        self._unresponsive_since.pop(server.id, None)
        if self._post_update_failure(server, "stopped responding"):
            return
        if server.desired_running and not server.update_hold:
            self._attempt_watchdog_restart(server, reason="it stopped responding to queries", need_stop_first=True)

    def _attempt_watchdog_restart(self, server: ServerConfig, reason: str, need_stop_first: bool) -> None:
        """Only called when server.desired_running is True -- the
        person wants this running, so a crash or a hang gets an
        automatic recovery attempt rather than just sitting down until
        someone notices. need_stop_first distinguishes a crash (nothing
        to stop -- process_manager.launch() alone is enough) from a
        hang (the process is still there, just unresponsive, so it
        needs a graceful-then-hard-kill stop before relaunching, same
        as a scheduled restart's stop half)."""
        now = time.monotonic()
        last_attempt = self._watchdog_last_attempt.get(server.id)
        if last_attempt is not None and now - last_attempt > self._WATCHDOG_STREAK_RESET_SECONDS:
            self._watchdog_attempts[server.id] = 0
        attempts = self._watchdog_attempts.get(server.id, 0)
        if attempts >= self._WATCHDOG_MAX_ATTEMPTS:
            # Out of plain restarts. If it has mods, find out whether one
            # of them is the cause (ui/mod_recovery.py) -- it holds the
            # server stopped while testing, so this only happens once.
            if any(m.get("enabled", True) for m in server.mods):
                self._start_mod_recovery(server, reason.replace("it ", "", 1) if reason.startswith("it ") else reason)
            return  # already gave up and alerted this streak -- see the bottom of this method
        if last_attempt is not None:
            delay = self._WATCHDOG_BACKOFF_SECONDS[min(attempts, len(self._WATCHDOG_BACKOFF_SECONDS) - 1)]
            if now - last_attempt < delay:
                return  # still waiting out the backoff before trying again

        self._watchdog_last_attempt[server.id] = now
        self._watchdog_attempts[server.id] = attempts + 1
        attempt_num = attempts + 1

        result = preflight.run_preflight(server)
        if not result.ok:
            self._notify(
                server,
                f"Watchdog couldn't restart this server ({reason}) -- pre-flight checks failed:\n" + "\n".join(result.problems),
                title="Watchdog Restart Failed",
            )
            return
        if result.repairs:
            self._notify(server, "Auto-repaired before watchdog restart:\n" + "\n".join(result.repairs), title="Auto-Repair")

        if need_stop_first:
            self._expected_stop.add(server.id)
            self._tracker_for(server).close_all_active()
            self._online_by_server[server.id] = set()

        def work():
            if need_stop_first:
                process_manager.graceful_stop(server)
            process_manager.launch(server)

        def done(_result, sid=server.id):
            self._known_running[sid] = True
            if need_stop_first:
                self._expected_stop.discard(sid)
            self._notify(
                server,
                f"Watchdog restarted this server ({reason}) -- attempt {attempt_num} of "
                f"{self._WATCHDOG_MAX_ATTEMPTS} this session.",
                title="Watchdog Restart",
            )

        def failed(e, sid=server.id):
            if need_stop_first:
                self._expected_stop.discard(sid)
            self._notify(server, f"Watchdog couldn't restart this server ({reason}): {e}", title="Watchdog Restart Failed")

        # Off the UI thread: a hang restart waits for the world to save first.
        self._run_op(work, on_done=done, on_error=failed)

        if self._watchdog_attempts.get(server.id, 0) >= self._WATCHDOG_MAX_ATTEMPTS:
            self._notify(
                server,
                f"This server has needed restarting {self._WATCHDOG_MAX_ATTEMPTS} times in a row -- "
                f"ConanOps will stop trying to restart it automatically. Check the Console and "
                f"Diagnostics tabs for what's actually wrong, then start it manually from the "
                f"Dashboard once that's fixed (which also resets this counter).",
                title="Watchdog Giving Up",
            )

    _RESUME_NETWORK_WAIT_MS = 10_000
    _RESUME_NETWORK_MAX_WAITS = 18
    _RESUME_BOOT_WINDOW_SECONDS = 15 * 60

    def _network_not_ready_for_resume(self) -> bool:
        """True only shortly after a PC boot, when a server that's about
        to auto-resume has a saved bind IP that isn't on any adapter yet,
        or no real (non-APIPA) address exists at all."""
        import network_utils
        try:
            import psutil
            if time.time() - psutil.boot_time() > self._RESUME_BOOT_WINDOW_SECONDS:
                return False
        except Exception:  # noqa: BLE001
            return False
        resuming = [s for s in self.config.servers if s.desired_running and s.install_dir and not s.update_hold]
        if not resuming:
            return False
        local = {ip for ip in network_utils.list_local_ipv4s() if network_utils.is_usable_lan_ipv4(ip)}
        if not local:
            return True
        return any(s.bind_ip and s.bind_ip not in local for s in resuming)

    def _auto_resume_servers(self) -> None:
        """Called once, during startup (see __init__), after the
        initial _known_running state is known: brings back every
        server the person wants running (desired_running=True) but
        that isn't currently running -- whether ConanOps itself just
        started (a PC reboot, or "Start with Windows"), or a server
        was left down from before a previous session ended. The other
        half of the "click Start once, ConanOps keeps it running from
        here on" contract described in _handle_start()'s comment.

        Unlike a manual Start click, failures here are reported
        through _notify() (tray + webhook) rather than a blocking
        QMessageBox -- nobody's necessarily sitting at the screen
        during an automatic startup pass, least of all right after a
        PC reboot, and a modal dialog nobody's there to dismiss would
        just sit there forever."""
        if self._network_not_ready_for_resume():
            # Started with Windows and the network adapter hasn't got its
            # address yet (DHCP still pending). Preflight would otherwise
            # see the saved bind IP as "stale" and overwrite a perfectly
            # good -- possibly deliberately chosen -- address, or fail.
            # Wait, without blocking the UI, for up to ~3 minutes.
            self._resume_network_waits = getattr(self, "_resume_network_waits", 0) + 1
            if self._resume_network_waits <= self._RESUME_NETWORK_MAX_WAITS:
                QTimer.singleShot(self._RESUME_NETWORK_WAIT_MS, self._auto_resume_servers)
                return
            _log.warning("Network still not ready after waiting; auto-resuming anyway.")
        for server in self.config.servers:
            if not server.desired_running or not server.install_dir:
                continue
            if server.update_hold:
                self._notify(server, f"Not starting this server: it's being held after a failed update. "
                                     f"{server.update_hold} Click Start to run it anyway.", title="Server Held")
                continue
            if process_manager.is_running(server.install_dir):
                continue  # already running somehow (e.g. survived from before this ConanOps session)
            result = preflight.run_preflight(server)
            if not result.ok:
                self._notify(
                    server,
                    "Auto-resume couldn't start this server -- pre-flight checks failed:\n" + "\n".join(result.problems),
                    title="Auto-Resume Failed",
                )
                continue
            if result.repairs:
                self._notify(server, "Auto-repaired before auto-resume:\n" + "\n".join(result.repairs), title="Auto-Repair")
            try:
                process_manager.launch(server)
                self._known_running[server.id] = True
            except FileNotFoundError as e:
                self._notify(server, f"Auto-resume couldn't start this server: {e}", title="Auto-Resume Failed")

    # -------------------------------------------------------- disk space --
    _UPNP_REFRESH_MS = 60 * 60 * 1000

    def _refresh_router_forwards(self) -> None:
        if not network_setup.UPNP_REFRESH_ENABLED:
            return
        for server in self.config.servers:
            if self._known_running.get(server.id) and server.bind_ip:
                try:
                    network_setup.refresh_upnp_async(server)
                except Exception as e:  # noqa: BLE001 - a timer slot must never raise
                    _log.warning(f"Router forward refresh for {server.name} failed: {e}")

    # ------------------------------------------------------ app updates --
    _APP_UPDATE_CHECK_INTERVAL_SECONDS = 20 * 3600

    def _maybe_check_app_update(self) -> None:
        cfg = self.config
        if not cfg.auto_check_app_updates:
            return
        if time.time() - cfg.last_app_update_check < self._APP_UPDATE_CHECK_INTERVAL_SECONDS:
            return
        cfg.last_app_update_check = time.time()
        cfg.save()
        self.app_settings_page.check_for_updates(automatic=True)

    def _on_app_update_available(self, info) -> None:
        if self.tray_icon and self.tray_icon.isVisible():
            self.tray_icon.showMessage(
                "ConanOps update available",
                f"Version {info.version} is out. Open App Settings to install it.",
                QSystemTrayIcon.Information, 8000,
            )

    def _busy_reason_for_app_update(self) -> str:
        """Non-empty (a short description) while restarting ConanOps would
        interrupt something. Servers RUNNING is fine -- closing ConanOps
        never stops them."""
        if self._update_apply_workers:
            return "a server update"
        if self._restore_workers:
            return "a backup restore"
        if self._mod_refresh_workers:
            return "a mod download"
        if getattr(self.mods_page, "_active_bisect_dialog", None) is not None:
            return "mod troubleshooting"
        if self._update_restart_started:
            return "the Windows Update restart"
        if self._recovery_workers:
            return "the automatic mod check"
        return ""

    # ------------------------------------------------------- unattended --
    _UPDATE_RESTART_MIN_UPTIME_SECONDS = 2 * 3600

    def _unattended_tick(self) -> None:
        try:
            any_running = any(self._known_running.get(s.id) for s in self.config.servers)
            power.set_keep_awake(bool(self.config.keep_pc_awake and any_running))
            self._check_windows_update_restart()
        except Exception as e:  # noqa: BLE001 - a timer slot must never raise
            _log.error(f"Unattended check failed: {e}")

    def _check_windows_update_restart(self) -> None:
        """Restarts the PC for pending Windows updates -- only inside the
        chosen window, only with nobody online on any server, never
        while an update/restore/mod download is mid-flight, at most once
        a day, and never within 2 hours of the last boot (Windows can
        leave the 'restart pending' flag set while it finishes up, which
        would otherwise mean a restart loop)."""
        cfg = self.config
        if not cfg.handle_update_restarts or self._update_restart_started:
            return
        now = datetime.now()
        try:
            from scheduler import _parse_hhmm, is_within_window
            if not is_within_window(now.time(), _parse_hhmm(cfg.update_restart_start),
                                    _parse_hhmm(cfg.update_restart_end)):
                return
        except (ValueError, AttributeError):
            return
        if cfg.last_update_restart == now.strftime("%Y-%m-%d"):
            return
        try:
            import psutil
            if time.time() - psutil.boot_time() < self._UPDATE_RESTART_MIN_UPTIME_SECONDS:
                return
        except Exception:  # noqa: BLE001
            return
        if not windows_update.reboot_pending():
            return
        running = [s for s in cfg.servers if self._known_running.get(s.id)]
        if any(self._online_by_server.get(s.id) for s in running):
            return  # wait for everyone to leave (the window is checked again next minute)
        if (self._update_apply_workers or self._restore_workers or self._mod_refresh_workers
                or getattr(self.mods_page, "_active_bisect_dialog", None) is not None):
            return

        self._update_restart_started = True
        cfg.last_update_restart = now.strftime("%Y-%m-%d")
        cfg.save()
        for s in running:
            self._expected_stop.add(s.id)
            self._tracker_for(s).close_all_active()
            self._notify(s, "Saving and stopping for a Windows Update restart. ConanOps will start it again "
                            "after the PC comes back.", title="Windows Update Restart")
        if not running and cfg.servers:
            self._notify(cfg.servers[0], "Restarting the PC to finish Windows updates.", title="Windows Update Restart")

        def work(servers=tuple(running)):
            for srv in servers:
                try:
                    process_manager.graceful_stop(srv)
                except Exception as e:  # noqa: BLE001 - still restart; the server would be killed anyway
                    _log.error(f"Couldn't stop {srv.name} cleanly before the update restart: {e}")
            if not windows_update.restart_pc(delay_seconds=60):
                raise RuntimeError("Windows refused the restart request (see conanops.log).")

        def failed(e, servers=tuple(running)):
            self._update_restart_started = False
            for srv in servers:
                self._expected_stop.discard(srv.id)
            target = servers[0] if servers else (cfg.servers[0] if cfg.servers else None)
            if target is not None:
                self._notify(target, f"Windows Update restart didn't happen: {e}. The watchdog will bring "
                                     f"stopped servers back.", title="Windows Update Restart Failed")

        self._run_op(work, on_error=failed)

    def _check_disk_space_all(self) -> None:
        for server in self.config.servers:
            if server.install_dir:
                self._check_one_disk_space(server, server.install_dir, "install")
                # Same periodic pass as the disk-space check -- both are
                # low-frequency housekeeping over local files, no reason
                # for a separate timer. Best-effort: game_log_manager
                # already handles/logs its own per-file failures, so
                # nothing here needs to react to the result.
                game_log_manager.prune_old_logs(server.install_dir)
            if server.backup_destination:
                self._check_one_disk_space(server, server.backup_destination, "backup")

    def _check_one_disk_space(self, server: ServerConfig, path: str, kind: str) -> None:
        if not os.path.isdir(path):
            return
        try:
            free_gb = shutil.disk_usage(path).free / (1024 ** 3)
        except OSError:
            return
        key = (server.id, kind)
        if free_gb < diagnostics.LOW_DISK_SPACE_ERROR_GB:
            if key not in self._disk_space_low_alerted:
                self._disk_space_low_alerted.add(key)
                label = "install folder's" if kind == "install" else "backup folder's"
                self._notify(
                    server,
                    f"Only {free_gb:.1f} GB free on the {label} drive ({path}) -- updates and backups "
                    f"will likely start failing soon. Free up space to avoid that.",
                    title="Low Disk Space",
                )
        elif free_gb > diagnostics.LOW_DISK_SPACE_WARNING_GB:
            self._disk_space_low_alerted.discard(key)  # recovered well clear of the threshold -- a later drop should alert again

    # ------------------------------------------------------ dynamic dns --
    def _check_duckdns(self) -> None:
        if not (self.config.duckdns_domain and self.config.duckdns_token):
            return
        if self._duckdns_worker is not None:
            return  # previous check still running -- skip this tick rather than overlap
        self._duckdns_worker = dynamic_dns_runner.DuckDnsWorker(
            self.config.duckdns_domain, self.config.duckdns_token, parent=self,
        )
        self._duckdns_worker.finished_update.connect(self._on_duckdns_finished)
        worker = self._duckdns_worker
        self._retiring_workers.append(worker)
        worker.finished.connect(lambda w=worker: self._retiring_workers.remove(w) if w in self._retiring_workers else None)
        self._duckdns_worker.start()

    def _on_duckdns_finished(self, result) -> None:
        self._duckdns_worker = None
        if result.ok:
            return  # silent on success -- most checks find no change; a toast every 30 minutes would just be noise
        _log.warning(f"DuckDNS update failed: {result.message}")
        if self.tray_icon and self.tray_icon.isVisible():
            # Not routed through _notify()/webhooks -- this is an
            # app-level concern (this machine's own public IP), not
            # tied to any one server the way every other alert in this
            # app is, so there's no natural server whose webhook
            # should carry it.
            self.tray_icon.showMessage(
                "DuckDNS Update Failed", result.message, QSystemTrayIcon.Warning, 8000,
            )

    # --------------------------------------------------- discord status --
    _DISCORD_STATUS_FAILURE_RESET_THRESHOLD = 5

    def _check_discord_status_all(self) -> None:
        for server in self.config.servers:
            if not (server.discord_status_enabled and server.webhook_discord_url):
                continue
            if server.id in self._discord_status_workers:
                continue  # previous update still in flight -- skip this tick rather than overlap
            content = self._build_discord_status_message(server)
            worker = discord_status_runner.DiscordStatusWorker(
                server.webhook_discord_url, server.discord_status_message_id, content, parent=self,
            )
            worker.finished_update.connect(lambda message_id, srv=server: self._on_discord_status_finished(srv, message_id))
            self._discord_status_workers[server.id] = worker
            self._retire_worker(worker)
            worker.start()

    def _build_discord_status_message(self, server: ServerConfig) -> str:
        """Built entirely from state this process already has on hand
        (the health check / log monitor's own tracking) -- no fresh
        query of its own, so this adds nothing but the one Discord
        HTTP call itself."""
        now_str = datetime.now().strftime("%H:%M")
        if self._known_running.get(server.id, False):
            online = self._online_by_server.get(server.id, set())
            return f"**{server.name}** — 🟢 Online, {len(online)} player(s) connected. _(updated {now_str})_"
        return f"**{server.name}** — 🔴 Offline. _(updated {now_str})_"

    def _on_discord_status_finished(self, server: ServerConfig, message_id: Optional[str]) -> None:
        self._discord_status_workers.pop(server.id, None)
        if message_id is not None:
            self._discord_status_failures.pop(server.id, None)
            if message_id != server.discord_status_message_id:
                server.discord_status_message_id = message_id
                self.config.save()
            return

        # Failed -- could be a transient network blip (in which case
        # the stored message id is still perfectly good and worth
        # trying again next tick) or the message genuinely no longer
        # existing in Discord (deleted by someone, e.g.), which would
        # otherwise fail forever. Only give up on the stored id -- and
        # let the NEXT successful tick post a fresh message instead --
        # after several consecutive failures, not the first one.
        streak = self._discord_status_failures.get(server.id, 0) + 1
        self._discord_status_failures[server.id] = streak
        if streak >= self._DISCORD_STATUS_FAILURE_RESET_THRESHOLD and server.discord_status_message_id:
            server.discord_status_message_id = ""
            self.config.save()
            self._discord_status_failures[server.id] = 0

    # -------------------------------------------------------- system tray --
    def _setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray_icon = None
            return
        self.tray_icon = QSystemTrayIcon(_make_tray_icon(self.theme.accent), self)
        self.tray_icon.setToolTip("ConanOps")
        menu = QMenu()
        show_action = menu.addAction("Show ConanOps")
        show_action.triggered.connect(self._restore_from_tray)
        menu.addSeparator()
        self._lock_action = menu.addAction("Change App PIN…" if self.config.app_lock_enabled else "Set App PIN…")
        self._lock_action.triggered.connect(self._open_set_pin_dialog)
        menu.addSeparator()
        quit_action = menu.addAction("Quit")
        quit_action.triggered.connect(self._quit_from_tray)
        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()
        self._really_quit = False

    def _on_theme_changed(self, palette) -> None:
        """Called by AppSettingsPage after it's already written the new
        theme.json -- just re-applies the live stylesheet and tray icon
        so the change is visible immediately without restarting."""
        self.theme = palette
        self.setStyleSheet(build_stylesheet(self.theme))
        if self.tray_icon:
            self.tray_icon.setIcon(_make_tray_icon(self.theme.accent))
        self._refresh_chrome()

    def _on_lock_changed(self) -> None:
        """Called after the PIN lock is set/changed/cleared from either
        the tray menu or the App Settings page, so whichever UI didn't
        just make the change (tray label) stays in sync."""
        if hasattr(self, "_lock_action"):
            self._lock_action.setText("Change App PIN…" if self.config.app_lock_enabled else "Set App PIN…")

    def _relaunch_after_update(self) -> None:
        """Called by AppSettingsPage right after self_update.apply_update()
        reports success. Python already has the OLD code loaded in
        memory, so this process can't just start using the new files --
        it has to spawn a fresh process (which will import the new code
        from disk) and then close itself, the same way Quit does."""
        popen_kwargs = {"cwd": APP_INSTALL_DIR, "close_fds": True, "env": proc_utils.child_env()}
        if os.name == "nt":
            # Detach the new process from this one's console/process
            # group so it isn't torn down when this process exits right
            # after -- Windows-only flags, no-ops/absent elsewhere.
            popen_kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
            )
        # Under a PyInstaller-frozen build, sys.executable IS ConanOps
        # itself (there's no separate python.exe to hand a script path
        # to, and no main.py on disk to hand it anyway -- self_update's
        # marker-file check is frozen-aware for the same reason, see
        # self_update._expected_marker_files()). A previous version of
        # this unconditionally built [sys.executable, main.py], which
        # under a frozen build meant re-launching the exe with a
        # nonexistent script path as its argument.
        if getattr(sys, "frozen", False):
            relaunch_args = [sys.executable]
        else:
            relaunch_args = [sys.executable, os.path.join(APP_INSTALL_DIR, "main.py")]
        try:
            proc = subprocess.Popen(relaunch_args, **popen_kwargs)
        except OSError as e:
            _log.error(f"Update installed, but couldn't relaunch automatically: {e}")
            QMessageBox.information(
                self, "Update Installed",
                f"The update installed, but ConanOps couldn't restart itself automatically "
                f"({e}). Please close and reopen it yourself.",
            )
            return

        # Release the single-instance lock (see main.py) as soon as
        # we're committed to closing -- lets the new process's own
        # tryLock() succeed quickly instead of needing its full
        # fallback wait for us to finish tearing down.
        lock = getattr(QApplication.instance(), "_conanops_lock", None)
        if lock is not None:
            lock.unlock()

        # Give the new process a brief window to fail fast -- a
        # missing DLL, a broken import in the update, anything that
        # exits it almost immediately -- before this process commits
        # to closing. Without this check, a relaunch that dies on the
        # spot would silently leave NO ConanOps process running at
        # all (and no error dialog, since Popen() itself succeeded).
        time.sleep(0.4)
        if proc.poll() is not None:
            _log.error(f"Relaunched process exited immediately (code {proc.returncode}); staying open.")
            QMessageBox.warning(
                self, "Update Installed",
                "The update installed, but the new version didn't start up successfully. This window "
                "will stay open so nothing you're running gets interrupted -- please try closing and "
                "reopening ConanOps yourself, or check the update file if that keeps happening.",
            )
            if lock is not None:
                lock.tryLock(1_000)  # we're staying open after all -- reclaim it
            return

        self._really_quit = True
        self.close()

    def _handle_delete_app_only(self, preserve_data: bool = True) -> None:
        """Uninstalls ConanOps itself, leaving every server's data
        (install folders, SteamCMD folders, backups, firewall rules)
        untouched -- see self_delete.py for why deleting the running
        app's own install folder needs a separate detached helper
        rather than a plain shutil.rmtree() from in here.

        preserve_data: when servers default to living alongside the
        app itself (see conanops_paths.app_data_dir()'s docstring),
        that's a SUBFOLDER of APP_INSTALL_DIR -- so "leaving server
        data untouched" requires the uninstall helper to actually skip
        it, not just delete APP_INSTALL_DIR wholesale as it always
        used to when the two were guaranteed separate. Defaults to
        True for this method's own "app only" meaning; passed False
        by _handle_delete_everything() below, since by the time IT
        calls this, data_dir has already been explicitly handled (and
        deleting it again here is exactly what "get rid of all of it"
        means if that first attempt partially failed)."""
        self._spawn_self_removal(preserve_data)

    def _handle_delete_everything(self) -> None:
        """Leaves the PC the way it was before ConanOps: every server
        (stopped first, so worlds are saved and nothing is locked), its
        files, backups and SteamCMD folders, every firewall rule and
        router forward, the sign-in entry, Windows Update active hours
        (put back), the Visual C++ runtime if ConanOps installed it,
        temp leftovers -- then, after ConanOps exits, the program itself
        (via its uninstaller if it was installed) and ConanOps' own
        folders and log. See cleanup.py."""
        uninstall_vc = False
        if vcredist.installed_by_conanops():
            uninstall_vc = QMessageBox.question(
                self, "Remove the Visual C++ runtime too?",
                "ConanOps installed the Microsoft Visual C++ runtime on this PC. Remove it as well?\n\n"
                "Choose No if you've installed other games or programs since then -- some of them may "
                "need it.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
            ) == QMessageBox.Yes

        from PySide6.QtWidgets import QProgressDialog
        progress = QProgressDialog("Getting started…", None, 0, 0, self)
        progress.setWindowTitle("Deleting Everything")
        progress.setCancelButton(None)
        progress.setMinimumDuration(0)
        progress.setWindowModality(Qt.ApplicationModal)
        progress.show()

        for s in self.config.servers:
            self._expected_stop.add(s.id)
            s.desired_running = False
        if hasattr(self.scheduler, "stop"):
            self.scheduler.stop()
        for monitor in list(self._log_monitors.values()):
            monitor.stop()

        worker = removal_runner.DeleteEverythingWorker(self.config, uninstall_vc)
        self._firewall_workers.append(worker)
        self._retire_worker(worker)
        worker.progress.connect(progress.setLabelText)

        def finished(problems, w=worker):
            if w in self._firewall_workers:
                self._firewall_workers.remove(w)
            progress.close()
            if problems:
                QMessageBox.warning(
                    self, "Almost Everything Was Removed",
                    "ConanOps will now close and remove itself, but these couldn't be removed:\n\n" +
                    "\n".join(problems[:15]) + "\n\nYou can delete anything listed above by hand.",
                )
            self._spawn_self_removal(preserve_data=False)

        worker.finished_cleanup.connect(finished)
        self._start_or_run(worker)

    def _offer_reinstall_if_files_missing(self, server: ServerConfig) -> bool:
        """When a server's files are gone (deleted or moved), offer to
        download them again with the setup wizard instead of showing a
        list of missing paths. True if the offer was shown."""
        if not server.install_dir or os.path.isdir(server.install_dir):
            return False
        answer = QMessageBox.question(
            self, "Server files are missing",
            f"The files for \"{server.name}\" aren't on this PC anymore -- they were deleted or moved.\n\n"
            f"Download them again now? Your settings for this server are kept.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if answer == QMessageBox.Yes:
            self._open_setup_wizard(server)
        return True

    def _show_tour_from_settings(self) -> None:
        self.sidebar.set_active_nav("dashboard")
        self._on_nav_selected("dashboard")
        QTimer.singleShot(150, self.start_tour)

    def start_tour(self) -> None:
        """Shows the dashboard walkthrough (ui/tour.py)."""
        if getattr(self, "_tour", None) is not None or self.background or not self.isVisible():
            return
        from ui.tour import TourOverlay, dashboard_steps
        self._tour = TourOverlay(self, dashboard_steps(self), accent=self.config.accent_color or "#c9752f")

        def ended(_completed):
            self._tour = None
            self.config.tour_done = True
            self.config.save()

        self._tour.finished.connect(ended)
        self._tour.start()

    def _apply_keep_running(self) -> None:
        """The setup wizard's "Keep my servers running by themselves":
        start with Windows, reopen if it stops, stay awake, and Windows
        Update restarts only in the 4-6 AM window -- skipping whatever's
        already on. At most one Windows permission prompt (active hours)."""
        cfg = self.config
        if not cfg.start_with_windows:
            try:
                startup_registration.register()
                cfg.start_with_windows = True
            except OSError as e:
                _log.warning(f"Couldn't turn on start with Windows: {e}")
        cfg.keep_pc_awake = True
        cfg.save()
        want_keep_alive = not cfg.keep_alive_enabled
        want_update_window = not cfg.handle_update_restarts

        def work():
            done = {"keep_alive": False, "update_window": False, "original": None}
            if want_keep_alive:
                done["keep_alive"] = keep_alive.enable()
            if want_update_window:
                hours = windows_update.compute_active_hours(cfg.update_restart_start, cfg.update_restart_end)
                if hours:
                    if not cfg.original_active_hours:
                        done["original"] = windows_update.read_active_hours()
                    done["update_window"] = windows_update.set_active_hours(*hours) == powershell.RUN_OK
            return done

        def finished(done):
            if done.get("original") is not None and not cfg.original_active_hours:
                cfg.original_active_hours = done["original"]
            if done.get("keep_alive"):
                cfg.keep_alive_enabled = True
            if done.get("update_window"):
                cfg.handle_update_restarts = True
            cfg.save()
            if hasattr(self, "app_settings_page"):
                self.app_settings_page.reload_unattended_from_config()

        self._run_op(work, on_done=finished,
                     on_error=lambda e: _log.error(f"Keep-it-running setup failed: {e}"))

    def _start_or_run(self, worker) -> None:
        """Tests (RUN_OPS_INLINE) run the worker synchronously."""
        if self.RUN_OPS_INLINE:
            worker.run()
        else:
            worker.start()

    def _spawn_self_removal(self, preserve_data: bool) -> None:
        """Hands the last step to self_delete.py's helper (ConanOps can't
        delete its own running files) and quits."""
        preserve_name = conanops_paths.APP_DATA_DIRNAME if preserve_data else None
        extra = [] if preserve_data else cleanup.owned_roots() + cleanup.temp_leftovers()
        try:
            keep_alive.disable()  # or the watcher would try to reopen ConanOps
        except Exception as e:  # noqa: BLE001
            _log.warning(f"Couldn't remove the keep-alive task: {e}")
        try:
            self_delete.spawn_self_delete_helper(APP_INSTALL_DIR, preserve_name=preserve_name, extra_paths=extra)
        except OSError as e:
            _log.error(f"Couldn't start the uninstall helper: {e}")
            QMessageBox.critical(self, "Uninstall Failed", f"Couldn't start the uninstall process: {e}")
            return
        self._really_quit = True
        self.close()

    def _open_set_pin_dialog(self) -> None:
        dialog = SetPinDialog(
            current_pin_set=self.config.app_lock_enabled,
            verify_fn=self.config.verify_app_lock_pin,
            parent=self,
        )
        if dialog.exec() != QDialog.Accepted or dialog.new_pin is None:
            return
        if dialog.new_pin:
            self.config.set_app_lock_pin(dialog.new_pin)
            QMessageBox.information(self, "PIN Set", "ConanOps will ask for this PIN the next time it starts.")
        else:
            self.config.clear_app_lock_pin()
            QMessageBox.information(self, "PIN Removed", "ConanOps will no longer ask for a PIN on startup.")
        self.config.save()
        self._on_lock_changed()
        if hasattr(self, "app_settings_page"):
            self.app_settings_page.lock_checkbox.blockSignals(True)
            self.app_settings_page.lock_checkbox.setChecked(self.config.app_lock_enabled)
            self.app_settings_page.lock_checkbox.blockSignals(False)
            self.app_settings_page.change_pin_btn.setEnabled(self.config.app_lock_enabled)

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.Trigger:
            self._restore_from_tray()

    def _restore_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit_from_tray(self) -> None:
        # A deliberate Quit: keep-alive must not reopen ConanOps behind
        # the person's back (cleared next time they open it).
        keep_alive.mark_user_quit()
        self._really_quit = True
        self.close()

    def closeEvent(self, event) -> None:
        if getattr(self, "tray_icon", None) and self.tray_icon.isVisible() and not getattr(self, "_really_quit", False):
            event.ignore()
            self.hide()
            self.tray_icon.showMessage("ConanOps", "Still running in the background.", QSystemTrayIcon.Information, 3000)
            return

        # An open Quick Mod Check/Find All Bad Mods dialog runs its own
        # modal event loop (exec()), which normally means MainWindow's
        # closeEvent can't even fire while one is up -- but that dialog
        # itself already handles this: this is only a backstop for the
        # unusual paths that can still reach here anyway (e.g. the tray
        # icon's own Exit action). Without this, actually quitting here
        # would abandon a QThread that's mid-restart of the real server,
        # leaving modlist.txt at whatever partial test subset it was on
        # and the world-save snapshot orphaned in a temp folder --
        # reject() runs the exact same cancel-and-wait-for-restore
        # sequence the dialog's own close button and Escape key do.
        active_bisect = getattr(self.mods_page, "_active_bisect_dialog", None)
        if active_bisect is not None:
            active_bisect.reject()

        for monitor in self._log_monitors.values():
            monitor.stop()
        for monitor in self._log_monitors.values():
            # A previous version called stop() without ever waiting on
            # these threads at all, so quitting the app while a monitor
            # happened to be mid-iteration could tear down its QThread
            # wrapper while the underlying thread was still alive --
            # "QThread: Destroyed while thread is still running", a
            # crash on some Qt builds. log_monitor.py's stop() is now
            # responsive within ~100ms, so this wait is short.
            monitor.wait(1000)

        # One-shot background workers (update checks/applies, restores,
        # firewall reconciliation) don't have their own stop() -- they
        # run a single SteamCMD/netsh call to completion. There's no
        # clean way to abort a SteamCMD update partway through without
        # risking a corrupted install, so this doesn't try to; it just
        # waits briefly for anything that's about to finish anyway, and
        # otherwise lets Qt's own shutdown sequence take it from here
        # rather than silently discarding a QThread that's still alive.
        still_running = [
            *self._update_check_workers.values(),
            *self._update_apply_workers.values(),
            *self._restore_workers.values(),
            *self._firewall_workers,
        ]
        for worker in still_running:
            worker.wait(1000)
        for worker in list(self._notify_workers):
            worker.wait(1000)
        if self.dashboard_page._poll_worker:
            self.dashboard_page._poll_worker.wait(1000)
        if self.access_page._action_worker:
            self.access_page._action_worker.wait(1000)

        self.scheduler.stop()
        self.web_control.stop()
        super().closeEvent(event)
