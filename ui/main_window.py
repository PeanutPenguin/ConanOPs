"""
Main application window: sidebar navigation, the per-server dashboard
and settings pages, background monitors/schedulers, and the tray icon.
"""
from __future__ import annotations

import os
from collections import deque
from typing import Dict, List, Optional

from PySide6.QtCore import QEvent, QTimer, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QStackedWidget, QMessageBox,
    QSystemTrayIcon, QMenu, QDialog, QFrame, QLabel, QPushButton,
)

from models import AppConfig, ServerConfig
import conanops_paths
import ini_utils
import process_manager
import backup_manager
import dynamic_dns_runner
import discord_status_runner
import backup_runner
import health_check_runner
import network_setup_runner
import keep_alive
import removal_runner
import applog
import theme_config
import web_control
from session_tracker import SessionTracker
from log_monitor import LogMonitor
from scheduler import Scheduler
from update_runner import CheckWorker, UpdateWorker, ModDownloadWorker
from ui.mod_recovery import ModRecoveryMixin
from ui.web_actions import WebActionsMixin
from ui.window.alerts import AlertsMixin
from ui.window.backups import BackupsMixin
from ui.window.common import APP_INSTALL_DIR, _NotifyWorker, _sessions_path_for
from ui.window.network import NetworkMixin
from ui.window.power import PowerMixin
from ui.window.system import SystemMixin
from ui.window.updates import UpdatesMixin

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
from ui.workers import keep_until_finished
from ui.generic_settings_page import GenericSettingsPage
from ui.setup_wizard import SetupWizard
import ini_field_specs

_log = applog.get_logger(__name__)

# The real install folder (not PyInstaller's _MEIPASS temp dir), used by the
# self-updater to back up, overwrite and relaunch the app.


def _make_tray_icon(accent: str = "") -> QIcon:
    """The bundled ConanOps icon. `accent` is unused, kept for callers."""
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
    "app": ("App Settings", "ConanOps itself: startup, remote access, integrations and appearance"),
}


class MainWindow(QMainWindow, PowerMixin, UpdatesMixin, BackupsMixin, NetworkMixin, AlertsMixin, SystemMixin,
                 ModRecoveryMixin, WebActionsMixin):
    # From the Cloudflare tunnel's thread: (link or "", status text).
    web_tunnel_changed = Signal(str, str)
    admin_mode_problem = Signal(str)

    def __init__(self, config: Optional[AppConfig] = None, background: bool = False):
        # background=True: windowless instance from the scheduled task.
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
        # Separate from _expected_stop, which other flows clear when they
        # finish; only ModsPage's lock/unlock touches this, so nothing can
        # unlock a running bisect.
        self._automation_locked: set = set()
        # Recent log lines per server, for the web version.
        self._web_logs: Dict[str, deque] = {}
        self._web_status: Dict[str, dict] = {}
        # Changes made from the web version that need a Windows permission
        # prompt, waiting for someone to use the PC (see queue_for_pc()).
        self._needs_pc: List[tuple] = []
        self._known_running: Dict[str, bool] = {}  # server id -> was it running last health check
        # Watchdog crash-restart state (per session, not persisted).
        self._watchdog_attempts: Dict[str, int] = {}       # server id -> consecutive crash-restarts this session
        self._watchdog_last_attempt: Dict[str, float] = {} # server id -> time.monotonic() of the last attempt
        self._unresponsive_since: Dict[str, float] = {}    # server id -> time.monotonic() it first stopped answering queries
        self._mod_refresh_workers: Dict[str, ModDownloadWorker] = {}
        self._update_failure_streak: Dict[str, int] = {}   # server id -> consecutive AUTOMATIC update failures
        self._update_retry_pending: set = set()            # server ids with a failed-update retry timer running
        self._post_update_watch: Dict[str, dict] = {}      # server id -> {"until": monotonic, "failures": n}
        # (server_id, "install"|"backup") already alerted for low disk space;
        # cleared once the drive recovers so a later drop alerts again.
        self._disk_space_low_alerted: set = set()
        self._update_check_workers: Dict[str, CheckWorker] = {}
        self._update_apply_workers: Dict[str, UpdateWorker] = {}
        self._manual_update_in_progress: set = set()  # server ids with a manual update (Updates page) running
        self._restore_workers: Dict[str, backup_runner.RestoreWorker] = {}
        self._firewall_workers: List[network_setup_runner.FirewallReconcileWorker] = []
        self._notify_workers: List[_NotifyWorker] = []
        # Workers leave the dicts above when their result signal fires, which
        # can precede QThread.finished; this keeps a reference until then to
        # avoid "QThread: Destroyed while thread is still running".
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

        # One GenericSettingsPage per remaining ini_field_specs category.
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
            ("alerts", "RCON", self.settings_alerts_page),
        ]

        self.settings_container = SettingsContainer(settings_entries)
        self.settings_container_entries = settings_entries  # the web version reads these pages too

        self.web_control = web_control.WebControlServer(
            get_active_server=self._active, save_config=self.config.save,
            get_password_hash=lambda: self.config.web_password_hash,
            sessions_path=os.path.join(conanops_paths.no_space_root(), "web_sessions.json"),
        )
        from webui.tunnel import Tunnel
        self.web_tunnel = Tunnel(on_change=lambda url, status: self.web_tunnel_changed.emit(url, status))
        self.web_tunnel_changed.connect(self._on_web_tunnel_changed)
        if self.config.web_control_enabled and not self.background:
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
        # The shared header shows titles; hide the pages' own (kept for state text).
        for p in self.pages.values():
            for attr in ("title_label", "page_title_label"):
                lbl = getattr(p, attr, None)
                if lbl is not None:
                    lbl.hide()

        from PySide6.QtWidgets import QTableWidget, QAbstractItemView
        # self.stack, not self: the stack isn't parented to the window yet.
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

        # Monitor every server, not just the active one, so scheduling and
        # crash detection work for all of them.
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

        # Background low-disk-space watch, so a filling drive is flagged
        # before it breaks a backup or update.
        self._disk_space_timer = QTimer(self)
        self._disk_space_timer.timeout.connect(self._check_disk_space_all)
        self._disk_space_timer.start(30 * 60 * 1000)

        # Prevent idle sleep while a server runs, and do Windows Update
        # restarts inside the chosen window (power.py / windows_update.py).
        self._update_restart_started = False
        self._unattended_timer = QTimer(self)
        self._unattended_timer.timeout.connect(self._unattended_tick)
        self._unattended_timer.start(60_000)
        QTimer.singleShot(3_000, self._unattended_tick)

        # ConanOps self-updates: checked after start, then every 20 h.
        self.app_settings_page.on_app_update_available = self._on_app_update_available
        self.app_settings_page.on_show_tour = self._show_tour_from_settings
        self.app_settings_page.on_restart_elevated = self._restart_with_admin_rights
        self.app_settings_page.on_alerts_changed = self.alerts_changed
        self.admin_mode_problem.connect(self.app_settings_page.show_admin_mode_problem)
        self.app_settings_page.web_tunnel = self.web_tunnel
        self.app_settings_page.on_web_remote_changed = self._sync_web_tunnel
        self.app_settings_page.on_web_password_changed = self.web_control.sessions.revoke_all
        from webui.api import WebApi
        from webui.bridge import GuiBridge
        self._web_bridge = GuiBridge(self)
        self.web_control.api = WebApi(self, self._web_bridge)
        QTimer.singleShot(2_000, self._sync_web_tunnel)
        self.app_settings_page.is_busy_for_app_update = self._busy_reason_for_app_update
        self._app_update_timer = QTimer(self)
        self._app_update_timer.timeout.connect(self._maybe_check_app_update)
        self._app_update_timer.start(60 * 60 * 1000)
        QTimer.singleShot(45_000, self._maybe_check_app_update)

        self._init_mod_recovery()

        # Routers often forget UPnP forwards on reboot; refresh hourly.
        self._upnp_refresh_timer = QTimer(self)
        self._upnp_refresh_timer.timeout.connect(self._refresh_router_forwards)
        self._upnp_refresh_timer.start(self._UPNP_REFRESH_MS)

        # Sync the keep-alive task off the UI thread (PowerShell is slow).
        if not self.background:
            import threading
            threading.Thread(target=keep_alive.ensure, args=(self.config.keep_alive_enabled,),
                             name="keep-alive-ensure", daemon=True).start()

            def check_admin(enabled=self.config.admin_mode_enabled):
                import admin_mode
                problem = admin_mode.ensure(enabled)
                if problem:
                    self.admin_mode_problem.emit(problem)
            threading.Thread(target=check_admin, name="admin-mode-ensure", daemon=True).start()
        # Parented timer (not bare singleShot) so it can't fire after the window closes.
        startup_check_timer = QTimer(self)
        startup_check_timer.setSingleShot(True)
        startup_check_timer.timeout.connect(self._check_disk_space_all)
        startup_check_timer.start(5_000)

        # DuckDNS update every 30 minutes (app-level: it's this PC's public IP).
        self._duckdns_timer = QTimer(self)
        self._duckdns_timer.timeout.connect(self._check_duckdns)
        self._duckdns_timer.start(30 * 60 * 1000)
        duckdns_startup_timer = QTimer(self)
        duckdns_startup_timer.setSingleShot(True)
        duckdns_startup_timer.timeout.connect(self._check_duckdns)
        duckdns_startup_timer.start(5_000)
        self._duckdns_worker: Optional[dynamic_dns_runner.DuckDnsWorker] = None

        # Opt-in live Discord status message per server, edited in place every
        # 5 minutes to stay within rate limits.
        self._discord_status_timer = QTimer(self)
        self._discord_status_timer.timeout.connect(self._check_discord_status_all)
        self._discord_status_timer.start(5 * 60 * 1000)
        discord_status_startup_timer = QTimer(self)
        discord_status_startup_timer.setSingleShot(True)
        discord_status_startup_timer.timeout.connect(self._check_discord_status_all)
        discord_status_startup_timer.start(5_000)
        self._discord_status_workers: Dict[str, discord_status_runner.DiscordStatusWorker] = {}
        # Consecutive edit failures per server, so a blip keeps the message id
        # but a deleted message is eventually replaced.
        self._discord_status_failures: Dict[str, int] = {}

        self._setup_tray()

        # Needs the tray icon (for _notify) and _known_running to be set.
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
            # Re-polish only on change; this runs every few seconds.
            self.header_status.setObjectName(name)
            self.header_status.style().unpolish(self.header_status)
            self.header_status.style().polish(self.header_status)

    def _refresh_chrome(self) -> None:
        """Timer refresh of sidebar status, Start/Stop button and header pill
        from cached state; never polls processes itself."""
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
        alerts = [name for name, url in (("Discord", self.config.alert_discord_url), ("ntfy", self.config.alert_ntfy_url)) if url]
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
            self.app_settings_page.show_section("ddns")
        elif key == "alerts":
            self._on_nav_selected("app")
            self.app_settings_page.show_section("alerts")
        elif key in ("restart", "backups"):
            self._on_nav_selected("settings")
            self.settings_container._select(key)

    def _enable_rcon_for_active(self) -> None:
        """Dashboard's "Turn On RCON" (see WebActionsMixin.enable_rcon)."""
        server = self.config.get_active()
        if server:
            self.enable_rcon(server)

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

        # The worker stops the server (saving the world) before deleting anything.
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
        """Players already online when a monitor attaches to a running log.
        Not fed to the session tracker (join times unknown); only fixes the
        online count used by the never-restart-while-online check."""
        self._online_by_server[server_id] = set(names)
        if self.config.active_server_id == server_id:
            self.dashboard_page.set_online_players(self._online_by_server[server_id])
            self.players_page.set_online_players(self._online_by_server[server_id])

    def _on_status_update(self, server_id: str, data: dict) -> None:
        self._web_status[server_id] = data
        if self.config.active_server_id == server_id:
            self.dashboard_page.on_status_update(data)

    def _on_log_line(self, server_id: str, line: str) -> None:
        buf = self._web_logs.get(server_id)
        if buf is None:
            buf = self._web_logs[server_id] = deque(maxlen=400)
        buf.append(line)
        if self.config.active_server_id == server_id:
            self.dashboard_page.on_log_line(line)

    # ------------------------------------------------------------ server --
    def _load_active_server(self) -> None:
        server = self.config.get_active()
        if not server:
            self.dashboard_page.set_empty()
            return

        if not server.backup_destination and server.install_dir:
            # An empty destination silently disables backups; fill and persist a default.
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

        for key, values in self.settings_values(server).items():
            page = self.settings_page_for(key)
            if page is not None:
                page.load_committed(values)
        self.settings_identity_page.set_install_dir(server.install_dir)
        self.settings_restart_page._refresh_suggestion()
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


    # ------------------------------------------- waiting for the PC --


    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.ActivationChange and self.isActiveWindow() and self._needs_pc:
            QTimer.singleShot(300, self._run_needs_pc)


    # ------------------------------------------------------- settings io --
    def _retire_worker(self, worker) -> None:
        keep_until_finished(self._retiring_workers, worker)

    def _active(self) -> ServerConfig:
        return self.config.get_active()

    def _handle_config_changed(self, server: ServerConfig) -> None:
        self.config.save()

    def _apply_gameplay(self, values: dict, server: Optional[ServerConfig] = None) -> None:
        """Apply handler for every gameplay/identity page. Keys are Conan ini
        names; "__"-prefixed keys are stored but never written to an .ini."""
        s = server or self._active()
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

    def _apply_network(self, values: dict, server: Optional[ServerConfig] = None) -> None:
        s = server or self._active()
        old_name = s.name
        old_game_port = s.game_port
        old_query_port = s.query_port
        old_bind_ip = s.bind_ip
        for k, v in values.items():
            setattr(s, k, v)
        self.config.save()

        # Firewall rules are keyed by server id + port; router forwards also
        # depend on the bind IP. Old-port rules are removed in the same pass.
        ports_changed = s.game_port != old_game_port or s.query_port != old_query_port
        bind_changed = s.bind_ip != old_bind_ip
        if ports_changed or bind_changed:
            self._start_network_reconcile(
                s, legacy_names=[old_name, s.name],
                old_ports=[old_game_port, old_game_port + 1, old_query_port],
                do_firewall=ports_changed,
            )

        if s.install_dir:
            # Per Funcom: Engine.ini [OnlineSubsystemSteam] holds name, password
            # and query port; [URL] holds the game port. Conan ignores a plain
            # "OnlineSubsystem" section. Launch args override these if they differ.
            engine_ini = os.path.join(s.install_dir, "ConanSandbox", "Saved", "Config", "WindowsServer", "Engine.ini")
            ini_utils.apply_known_keys(engine_ini, {
                "ServerName": ("OnlineSubsystemSteam", s.name),
                "ServerPassword": ("OnlineSubsystemSteam", s.password),
                "Port": ("URL", str(s.game_port)),
                "GameServerQueryPort": ("OnlineSubsystemSteam", str(s.query_port)),
                "MultiHome": ("URL", s.bind_ip),
            })
            # MaxPlayers belongs in Game.ini's gamesession section, not ServerSettings.ini.
            game_ini = os.path.join(s.install_dir, "ConanSandbox", "Saved", "Config", "WindowsServer", "Game.ini")
            ini_utils.apply_known_keys(game_ini, {
                "MaxPlayers": ("/script/engine.gamesession", str(s.max_players)),
            })
        self._refresh_sidebar()
        if self.config.active_server_id == s.id:
            self.console_page.set_server(s)  # picks up any RCON port/enabled change

    def _apply_backups_settings(self, values: dict, server: Optional[ServerConfig] = None) -> None:
        s = server or self._active()
        for k, v in values.items():
            setattr(s, k, v)
        self.config.save()

    def _apply_restart(self, values: dict, server: Optional[ServerConfig] = None) -> None:
        s = server or self._active()
        for k, v in values.items():
            setattr(s, k, v)
        self.config.save()

    def _apply_alerts(self, values: dict, server: Optional[ServerConfig] = None) -> None:
        s = server or self._active()
        for k, v in values.items():
            setattr(s, k, v)
        self.config.save()
        if s.install_dir:
            # RCON lives in Game.ini [RconPlugin] (not Engine.ini), and
            # RconEnabled takes "1"/"0", not "True"/"False".
            game_ini = os.path.join(s.install_dir, "ConanSandbox", "Saved", "Config", "WindowsServer", "Game.ini")
            ini_utils.apply_known_keys(game_ini, {
                "RconEnabled": ("RconPlugin", "1" if s.rcon_enabled else "0"),
                "RconPassword": ("RconPlugin", s.rcon_password),
                "RconPort": ("RconPlugin", str(s.rcon_port)),
            })
        if self.config.active_server_id == s.id:
            self.console_page.set_server(s)


    # ---------------------------------------------------------- actions --
    # Delay between the RCON restart warning and the restart.
    _RESTART_WARNING_GRACE_SECONDS = 10


    _UPDATE_RETRY_MINUTES = 30


    # ------------------------------------------------- post-update watch --
    # Within this window after an update, repeated crashes/hangs (usually an
    # outdated mod) hold the server stopped after one retry and list suspect mods.
    _POST_UPDATE_WATCH_SECONDS = 15 * 60
    _POST_UPDATE_FAILURES_BEFORE_HOLD = 2


    # ------------------------------------------------------- off-thread ops --
    # Tests set this so stop/restart run synchronously.
    RUN_OPS_INLINE = False


        # Success is silent on purpose.


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
            # Cancelling a new server's wizard removes its saved placeholder;
            # the install_dir guard leaves configured servers untouched.
            self.config.remove_server(server.id)
            self.config.save()
            self._refresh_sidebar()
            self._load_active_server()


    # --------------------------------------------------- health / crash --
    # Delays between crash-restarts in a streak (last value repeats); after
    # _WATCHDOG_MAX_ATTEMPTS it alerts instead of looping forever.
    _WATCHDOG_BACKOFF_SECONDS = [30, 60, 120, 300, 300]
    _WATCHDOG_MAX_ATTEMPTS = 5
    # A crash this long after the last attempt starts a new streak, so rare
    # crashes never use up the attempts.
    _WATCHDOG_STREAK_RESET_SECONDS = 3600
    # Unanswered queries for this long mean hung. Generous, so a slow-loading
    # server isn't restarted just before it comes up.
    _HUNG_THRESHOLD_SECONDS = 300

    # Consecutive automatic update failures before an extra "needs attention" alert.
    _UPDATE_FAILURE_ALERT_THRESHOLD = 3


    _RESUME_NETWORK_WAIT_MS = 10_000
    _RESUME_NETWORK_MAX_WAITS = 18
    _RESUME_BOOT_WINDOW_SECONDS = 15 * 60


    # -------------------------------------------------------- disk space --
    _UPNP_REFRESH_MS = 60 * 60 * 1000


    # ------------------------------------------------------ app updates --
    _APP_UPDATE_CHECK_INTERVAL_SECONDS = 20 * 3600


    # ------------------------------------------------------- unattended --
    _UPDATE_RESTART_MIN_UPTIME_SECONDS = 2 * 3600


    # ------------------------------------------------------ dynamic dns --


    # --------------------------------------------------- discord status --
    _DISCORD_STATUS_FAILURE_RESET_THRESHOLD = 5


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
        """Re-applies the stylesheet and tray icon after a theme change."""
        self.theme = palette
        self.setStyleSheet(build_stylesheet(self.theme))
        if self.tray_icon:
            self.tray_icon.setIcon(_make_tray_icon(self.theme.accent))
        self._refresh_chrome()

    def _on_lock_changed(self) -> None:
        """Keeps the tray lock label in sync after a PIN lock change."""
        if hasattr(self, "_lock_action"):
            self._lock_action.setText("Change App PIN…" if self.config.app_lock_enabled else "Set App PIN…")


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


    def _start_or_run(self, worker) -> None:
        """Tests (RUN_OPS_INLINE) run the worker synchronously."""
        if self.RUN_OPS_INLINE:
            worker.run()
        else:
            worker.start()


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
        # Stop keep-alive from reopening ConanOps after a deliberate quit.
        keep_alive.mark_user_quit()
        self._really_quit = True
        self.close()

    def closeEvent(self, event) -> None:
        if getattr(self, "tray_icon", None) and self.tray_icon.isVisible() and not getattr(self, "_really_quit", False):
            event.ignore()
            self.hide()
            self.tray_icon.showMessage("ConanOps", "Still running in the background.", QSystemTrayIcon.Information, 3000)
            return

        # Backstop (e.g. tray Exit): cancel a running bisect and wait for it to
        # restore modlist.txt and the world snapshot.
        active_bisect = getattr(self.mods_page, "_active_bisect_dialog", None)
        if active_bisect is not None:
            active_bisect.reject()

        for monitor in self._log_monitors.values():
            monitor.stop()
        for monitor in self._log_monitors.values():
            # Wait so the QThread isn't destroyed while still running.
            monitor.wait(1000)

        # One-shot workers can't be aborted safely (a half-done SteamCMD update
        # corrupts the install); just wait briefly for them.
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
        self.web_tunnel.stop()
        super().closeEvent(event)
