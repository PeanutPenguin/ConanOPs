from __future__ import annotations

import time
from typing import Optional

import psutil
from PySide6.QtCore import Qt, QTimer, QThread, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QGridLayout, QPlainTextEdit, QListWidget, QListWidgetItem,
)

import network_utils
import process_manager
from models import ServerConfig
from ui.workers import keep_until_finished


class _DashboardPollWorker(QThread):
    """One dashboard poll (process scan, CPU/memory/uptime, A2S query) off the
    GUI thread, since the scan and query can stall the UI."""
    finished_poll = Signal(object)  # dict -- see run()'s local `result`

    def __init__(self, install_dir: str, bind_ip: str, query_port: int,
                 cached_pid: Optional[int], cached_proc, parent=None):
        super().__init__(parent)
        self.install_dir = install_dir
        self.bind_ip = bind_ip
        self.query_port = query_port
        self.cached_pid = cached_pid
        self.cached_proc = cached_proc

    def run(self) -> None:
        result = {"pid": None, "proc": None, "cpu": None, "mem_mb": None, "uptime_seconds": None, "max_players": None}
        try:
            pid = process_manager.find_running_pid(self.install_dir)
            result["pid"] = pid
            if pid is not None:
                try:
                    if self.cached_proc is not None and self.cached_pid == pid:
                        proc = self.cached_proc
                        result["cpu"] = proc.cpu_percent(interval=None)
                    else:
                        # cpu_percent() measures since the last call, so a new
                        # Process only primes the baseline; cpu stays None
                        # until the next poll.
                        proc = psutil.Process(pid)
                        proc.cpu_percent(interval=None)
                        result["cpu"] = None
                    result["proc"] = proc
                    result["mem_mb"] = proc.memory_info().rss / (1024 * 1024)
                    result["uptime_seconds"] = time.time() - proc.create_time()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    result["proc"] = None

                info = network_utils.query_a2s_info(self.bind_ip or "127.0.0.1", self.query_port, timeout=0.5)
                if info:
                    result["max_players"] = info["max_players"]
        except Exception:  # noqa: BLE001 - always emit; a stuck poll worker blocks every future poll (see _poll_process_stats)
            pass

        self.finished_poll.emit(result)


def _format_uptime(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"Up {days}d {hours}h"
    if hours:
        return f"Up {hours}h {minutes}m"
    return f"Up {minutes}m"


def _stat_card(title: str) -> tuple[QFrame, QLabel, QLabel]:
    card = QFrame()
    card.setObjectName("Card")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(6)
    title_lbl = QLabel(title)
    title_lbl.setObjectName("StatLabel")
    value_lbl = QLabel("—")
    value_lbl.setObjectName("StatValue")
    sub_lbl = QLabel("")
    sub_lbl.setObjectName("Dim")
    layout.addWidget(title_lbl)
    layout.addWidget(value_lbl)
    layout.addWidget(sub_lbl)
    return card, value_lbl, sub_lbl


def _section_title(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("SectionTitle")
    return lbl


def _repolish(widget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class AutomationTile(QFrame):
    """One Automation tile: badge, name, On/Off pill, and a Change button."""

    def __init__(self, key: str, name: str, mark: str, tint: str, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.key = key
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 12, 12, 12)
        row.setSpacing(12)
        badge = QLabel(mark)
        badge.setFixedSize(36, 36)
        badge.setAlignment(Qt.AlignCenter)
        badge.setStyleSheet(f"background-color: #2f2f2f; border-radius: 8px; color: {tint}; font-weight: 700; font-size: 12px;")
        row.addWidget(badge)
        col = QVBoxLayout()
        col.setSpacing(4)
        self.name_label = QLabel(name)
        self.name_label.setObjectName("TileTitle")
        col.addWidget(self.name_label)
        self.state_label = QLabel("")
        self.state_label.setObjectName("PillOff")
        col.addWidget(self.state_label, 0, Qt.AlignLeft)
        row.addLayout(col, 1)
        self.open_btn = QPushButton("Change")
        self.open_btn.setToolTip(f"Open the settings for {name.lower()}")
        row.addWidget(self.open_btn)

    def set_state(self, on: bool, detail: str) -> None:
        if getattr(self, "_state", None) == (on, detail):
            return  # refreshed on a timer; restyling an unchanged pill forces a relayout
        self._state = (on, detail)
        self.state_label.setObjectName("PillOn" if on else "PillOff")
        self.state_label.setText(f"● {detail}")
        _repolish(self.state_label)


# (key, name, badge letters, badge tint) -- key is what set_automation()
# and on_open_automation use.
AUTOMATION_TILES = [
    ("restart", "Scheduled restarts", "RS", "#f0a66a"),
    ("updates", "Auto-update", "UP", "#7fb2ff"),
    ("backups", "Backups", "BK", "#5fd38a"),
    ("watchdog", "Crash watchdog", "WD", "#f28b80"),
    ("alerts", "Discord & ntfy alerts", "DC", "#a99cff"),
    ("ddns", "Dynamic DNS", "DN", "#f0b54a"),
]


class NoticeBar(QFrame):
    """An amber warning strip with one action button, hidden until set_notice()."""

    def __init__(self, button_text: str, parent=None):
        super().__init__(parent)
        self.setObjectName("WarnBox")
        row = QHBoxLayout(self)
        row.setContentsMargins(16, 10, 12, 10)
        row.setSpacing(12)
        self.label = QLabel("")
        self.label.setWordWrap(True)
        row.addWidget(self.label, 1)
        self.button = QPushButton(button_text)
        row.addWidget(self.button, 0, Qt.AlignVCenter)
        self._text = None
        self.hide()

    def set_notice(self, text: str) -> None:
        if text == self._text:
            return  # refreshed on a timer -- leave an unchanged notice alone
        self._text = text
        self.label.setText(text)
        self.setVisible(bool(text))


class DashboardPage(QWidget):
    """Display and control surface. MainWindow owns the per-server LogMonitors
    and pushes updates here for the displayed server."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.server: Optional[ServerConfig] = None
        self.on_restart = None       # set by main_window: callable(server)
        self.on_start = None         # set by main_window: callable(server)
        self.on_stop = None          # set by main_window: callable(server)
        self.on_setup = None         # set by main_window: callable(server) -> opens setup wizard
        self.on_add_server = None    # set by main_window: callable() -> add a first server, from the empty state
        self.on_open_automation = None  # set by main_window: callable(tile_key) -> open that feature's settings
        self.on_start_anyway = None     # set by main_window: callable() -> start a held server anyway
        self.on_enable_rcon = None      # set by main_window: callable() -> turn RCON on for this server
        self._proc_cache: Optional[tuple[int, psutil.Process]] = None  # reused so cpu_percent() has a baseline
        self._poll_worker: Optional[_DashboardPollWorker] = None
        self._poll_worker_server: Optional[ServerConfig] = None
        # Keeps finished workers referenced until QThread.finished fires.
        self._retiring_workers: list = []

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(18)

        top = QHBoxLayout()
        top.setSpacing(10)
        self.title_label = QLabel("Dashboard")
        self.title_label.setObjectName("PageTitle")
        self.status_label = QLabel("UNKNOWN")
        self.status_label.setObjectName("PillOff")
        self.uptime_label = QLabel("")
        self.uptime_label.setObjectName("Dim")
        top.addWidget(self.title_label)
        top.addWidget(self.status_label, 0, Qt.AlignVCenter)
        top.addWidget(self.uptime_label)
        top.addStretch(1)

        self.connect_label = QLabel("")
        self.connect_label.setObjectName("Muted")
        self.setup_btn = QPushButton("Set Up Server…")
        self.setup_btn.setObjectName("PrimaryButton")
        self.setup_btn.clicked.connect(self._handle_setup)
        # Visibility set by _apply_running_state().
        self.start_btn = QPushButton("Start")
        self.start_btn.clicked.connect(self._handle_start)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self._handle_stop)
        self.restart_btn = QPushButton("Restart")
        self.restart_btn.setObjectName("PrimaryButton")  # the mockup's main action
        self.restart_btn.clicked.connect(self._handle_restart)
        top.addWidget(self.connect_label)
        top.addWidget(self.setup_btn)
        top.addWidget(self.start_btn)
        top.addWidget(self.stop_btn)
        top.addWidget(self.restart_btn)
        root.addLayout(top)

        # Hidden as one unit by set_empty().
        self.content = QWidget()
        content_layout = QVBoxLayout(self.content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(16)
        root.addWidget(self.content, 1)

        grid = QGridLayout()
        self.hold_notice = NoticeBar("Start Anyway")
        self.hold_notice.button.clicked.connect(lambda: self.on_start_anyway and self.on_start_anyway())
        content_layout.addWidget(self.hold_notice)
        self.rcon_notice = NoticeBar("Turn On RCON")
        self.rcon_notice.button.clicked.connect(lambda: self.on_enable_rcon and self.on_enable_rcon())
        content_layout.addWidget(self.rcon_notice)
        # Server builds moved into Server Settings; a new one is flagged here.
        self.update_notice = NoticeBar("Go to Server Updates")
        self.update_notice.button.clicked.connect(
            lambda: self.on_open_automation and self.on_open_automation("updates"))
        content_layout.addWidget(self.update_notice)

        grid.setSpacing(12)
        self.cpu_card, self.cpu_value, self.cpu_sub = _stat_card("CPU")
        self.mem_card, self.mem_value, self.mem_sub = _stat_card("Memory")
        self.fps_card, self.fps_value, self.fps_sub = _stat_card("Server FPS")
        self.players_card, self.players_value, self.players_sub = _stat_card("Players")
        for i, card in enumerate((self.cpu_card, self.mem_card, self.fps_card, self.players_card)):
            grid.addWidget(card, 0, i)
        content_layout.addLayout(grid)

        content_layout.addWidget(_section_title("Automation"))
        auto_grid = QGridLayout()
        auto_grid.setSpacing(12)
        self.automation_tiles: dict = {}
        for i, (key, name, mark, tint) in enumerate(AUTOMATION_TILES):
            tile = AutomationTile(key, name, mark, tint)
            tile.open_btn.clicked.connect(lambda _=False, k=key: self._open_automation(k))
            auto_grid.addWidget(tile, i // 3, i % 3)
            self.automation_tiles[key] = tile
        content_layout.addLayout(auto_grid)

        log_frame = QFrame()
        log_frame.setObjectName("Card")
        log_layout = QVBoxLayout(log_frame)
        log_layout.setContentsMargins(16, 14, 16, 16)
        log_layout.setSpacing(10)
        log_layout.addWidget(_section_title("Server Log"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        log_layout.addWidget(self.log_view)
        content_layout.addWidget(log_frame, 2)

        players_frame = QFrame()
        players_frame.setObjectName("Card")
        players_frame.setMaximumHeight(160)
        players_layout = QVBoxLayout(players_frame)
        players_layout.setContentsMargins(16, 14, 16, 16)
        players_layout.setSpacing(10)
        players_layout.addWidget(_section_title("Online Players"))
        self.players_list = QListWidget()
        players_layout.addWidget(self.players_list)
        content_layout.addWidget(players_frame)

        # Shown instead of `content` when no server is configured.
        self.empty_frame = QFrame()
        self.empty_frame.setObjectName("Card")
        empty_layout = QVBoxLayout(self.empty_frame)
        empty_layout.addStretch(1)
        empty_title = QLabel("No servers configured yet")
        empty_title.setObjectName("PageTitle")
        empty_title.setAlignment(Qt.AlignCenter)
        empty_layout.addWidget(empty_title)
        empty_note = QLabel("Add a server to get started -- ConanOps will walk you through the rest.")
        empty_note.setObjectName("Dim")
        empty_note.setAlignment(Qt.AlignCenter)
        empty_layout.addWidget(empty_note)
        self.empty_add_btn = QPushButton("+ Add Your First Server")
        self.empty_add_btn.setObjectName("PrimaryButton")
        self.empty_add_btn.clicked.connect(self._handle_add_server)
        add_btn_row = QHBoxLayout()
        add_btn_row.addStretch(1)
        add_btn_row.addWidget(self.empty_add_btn)
        add_btn_row.addStretch(1)
        empty_layout.addLayout(add_btn_row)
        empty_layout.addStretch(1)
        root.addWidget(self.empty_frame, 1)
        self.empty_frame.hide()

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_process_stats)
        self._poll_timer.start(2000)

    def set_automation(self, states: dict) -> None:
        """states: tile key -> (on, detail text)."""
        for key, (on, detail) in states.items():
            tile = self.automation_tiles.get(key)
            if tile is not None:
                tile.set_state(on, detail)

    def _open_automation(self, key: str) -> None:
        if self.on_open_automation:
            self.on_open_automation(key)

    def _set_status(self, text: str, kind: str) -> None:
        if self.status_label.text() == text:
            return  # polled every 2 s -- don't restyle when nothing changed
        self.status_label.setText(text)
        self.status_label.setObjectName({"on": "PillOn", "warn": "PillWarn"}.get(kind, "PillOff"))
        _repolish(self.status_label)

    def _retire_worker(self, worker: Optional[_DashboardPollWorker]) -> None:
        keep_until_finished(self._retiring_workers, worker)

    def set_server(self, server: ServerConfig, online_players: set) -> None:
        self.empty_frame.hide()
        self.content.show()
        self.server = server
        self.title_label.setText(server.name)
        self.connect_label.setText(f"{server.bind_ip or '(no ip)'}:{server.game_port}")
        self.log_view.clear()
        self.setup_btn.setVisible(not bool(server.install_dir))
        # Starting guess; the next poll sets the real state.
        self._apply_running_state(running=False)
        self._proc_cache = None  # reset so a new server gets its own psutil.Process baseline
        self.set_online_players(online_players)
        self._poll_process_stats()

    def set_empty(self) -> None:
        """Empty state when no servers are configured."""
        self.server = None
        self.content.hide()
        self.empty_frame.show()
        self.title_label.setText("Dashboard")
        self.status_label.setText("")
        self.uptime_label.setText("")
        self.connect_label.setText("")

    def _apply_running_state(self, running: bool) -> None:
        has_install = bool(self.server and self.server.install_dir)
        self.start_btn.setVisible(has_install and not running)
        self.stop_btn.setVisible(has_install and running)
        self.restart_btn.setVisible(has_install)

    def _handle_restart(self) -> None:
        if self.server and self.on_restart:
            self.on_restart(self.server)

    def _handle_start(self) -> None:
        if self.server and self.on_start:
            self.on_start(self.server)

    def _handle_stop(self) -> None:
        if self.server and self.on_stop:
            self.on_stop(self.server)

    def _handle_setup(self) -> None:
        if self.server and self.on_setup:
            self.on_setup(self.server)

    def _handle_add_server(self) -> None:
        if self.on_add_server:
            self.on_add_server()

    def set_online_players(self, names: set) -> None:
        self.players_list.clear()
        for name in sorted(names):
            self.players_list.addItem(QListWidgetItem(name))
        self.players_value.setText(str(len(names)))

    def on_status_update(self, data: dict) -> None:
        self.fps_value.setText(f"{data['fps']:.1f}")
        self.cpu_value.setText(f"{data['cpu']:.0f}%")

    def on_log_line(self, line: str) -> None:
        self.log_view.appendPlainText(line)

    def _poll_process_stats(self) -> None:
        if not self.server:
            return  # no server configured at all -- set_empty()'s state stands, nothing to poll
        if not self.server.install_dir:
            self._set_status("● Not set up", "warn")
            return
        if self._poll_worker:
            return  # previous poll still in flight -- skip this tick rather than overlap
        cached_pid, cached_proc = self._proc_cache if self._proc_cache else (None, None)
        self._poll_worker_server = self.server
        self._poll_worker = _DashboardPollWorker(
            self.server.install_dir, self.server.bind_ip, self.server.query_port, cached_pid, cached_proc,
        )
        self._poll_worker.finished_poll.connect(self._on_poll_finished)
        self._poll_worker.start()

    def _on_poll_finished(self, result: dict) -> None:
        polled_server = self._poll_worker_server
        self._retire_worker(self._poll_worker)
        self._poll_worker = None
        if self.server is not polled_server:
            return  # switched servers while this poll was running -- its result is stale, drop it
        pid = result["pid"]
        if pid is None:
            self._set_status("● Offline", "off")
            self.uptime_label.setText("")
            self.cpu_value.setText("—")
            self.mem_value.setText("—")
            self._proc_cache = None
            self._apply_running_state(running=False)
            return

        self._set_status("● Online", "on")
        self._apply_running_state(running=True)
        if result["proc"] is not None:
            self._proc_cache = (pid, result["proc"])
            # cpu is None on a new process's first poll.
            self.cpu_value.setText(f"{result['cpu']:.0f}%" if result["cpu"] is not None else "—")
            self.mem_value.setText(f"{result['mem_mb']:.0f} MB")
            self.uptime_label.setText(_format_uptime(result["uptime_seconds"]))
        else:
            self._proc_cache = None
            self.uptime_label.setText("")

        if result["max_players"] is not None:
            self.players_sub.setText(f"of {result['max_players']} max (query confirmed)")

