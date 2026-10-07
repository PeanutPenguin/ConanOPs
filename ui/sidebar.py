from __future__ import annotations

from typing import Callable, List, Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QButtonGroup,
    QFrame,
)

from models import ServerConfig, MAX_SERVERS
import version
from ui import assets

# (key, label, icon) in display order, grouped into sidebar sections.
NAV_SECTIONS = [
    ("Manage", [
        ("dashboard", "Dashboard", "dashboard"),
        ("players", "Players", "players"),
        ("mods", "Mods", "mods"),
        ("console", "Console", "console"),
        ("settings", "Server Settings", "settings"),
    ]),
    ("System", [
        ("app", "App Settings", "app"),
    ]),
]
NAV_ITEMS = [(key, label) for _section, items in NAV_SECTIONS for key, label, _icon in items]


def _section_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("SectionLabel")
    return lbl


class Sidebar(QWidget):
    server_selected = Signal(str)     # server id
    server_remove_requested = Signal(str)  # server id
    add_server_clicked = Signal()
    nav_selected = Signal(str)        # nav key
    power_clicked = Signal()          # Start/Stop Server button at the bottom

    def __init__(self, parent=None, muted_color: str = "#a3a3a3", accent_text_color: str = "#e2914f"):
        super().__init__(parent)
        self.setObjectName("Sidebar")
        # A QWidget subclass doesn't paint its stylesheet background without this.
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedWidth(256)
        self._muted = muted_color
        self._accent_text = accent_text_color

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 18, 12, 16)
        root.setSpacing(2)

        brand = QHBoxLayout()
        brand.setContentsMargins(8, 2, 8, 14)
        brand.setSpacing(12)
        logo = QLabel()
        logo.setPixmap(assets.app_icon_pixmap(38))
        logo.setFixedSize(38, 38)
        brand.addWidget(logo)
        names = QVBoxLayout()
        names.setSpacing(0)
        title = QLabel("ConanOps")
        title.setObjectName("AppTitle")
        names.addWidget(title)
        ver = QLabel(f"v{version.VERSION}")
        ver.setObjectName("AppVersion")
        names.addWidget(ver)
        brand.addLayout(names, 1)
        root.addLayout(brand)

        root.addWidget(_section_label("Servers"))
        self.server_list_layout = QVBoxLayout()
        self.server_list_layout.setContentsMargins(0, 0, 0, 0)
        self.server_list_layout.setSpacing(2)
        root.addLayout(self.server_list_layout)
        self._server_group = QButtonGroup(self)
        self._server_group.setExclusive(True)

        self.add_server_btn = QPushButton("Add server")
        self.add_server_btn.setObjectName("NavButton")
        self.add_server_btn.setIcon(assets.line_icon("plus", self._muted))
        self.add_server_btn.clicked.connect(self.add_server_clicked.emit)
        root.addWidget(self.add_server_btn)

        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        for section, items in NAV_SECTIONS:
            root.addSpacing(8)
            root.addWidget(_section_label(section))
            for key, label, icon in items:
                btn = QPushButton(label)
                btn.setObjectName("NavButton")
                btn.setCheckable(True)
                btn.setProperty("nav_key", key)
                btn.setIcon(assets.line_icon(icon, self._muted, self._accent_text))
                btn.setIconSize(QSize(18, 18))
                btn.clicked.connect(lambda _=False, k=key: self.nav_selected.emit(k))
                self._nav_group.addButton(btn)
                root.addWidget(btn)
        root.addStretch(1)

        info = QFrame()
        info.setObjectName("InfoBox")
        info_layout = QHBoxLayout(info)
        info_layout.setContentsMargins(12, 10, 12, 10)
        info_layout.setSpacing(10)
        info_icon = QLabel()
        info_icon.setPixmap(assets.line_icon("info", "#7fb2ff").pixmap(18, 18))
        info_icon.setAlignment(Qt.AlignTop)
        info_layout.addWidget(info_icon)
        self.info_label = QLabel(
            "ConanOps keeps running in the tray when this window closes. Scheduled restarts, "
            "backups and updates carry on."
        )
        self.info_label.setObjectName("SidebarNote")
        self.info_label.setWordWrap(True)
        info_layout.addWidget(self.info_label, 1)
        root.addSpacing(12)
        root.addWidget(info)

        self.power_btn = QPushButton("Stop Server")
        self.power_btn.setIcon(assets.line_icon("power", "#ececec"))
        self.power_btn.clicked.connect(self.power_clicked.emit)
        self.power_btn.setEnabled(False)
        root.addSpacing(6)
        root.addWidget(self.power_btn)

        self._server_buttons: dict[str, QPushButton] = {}
        self._server_rows: dict[str, QWidget] = {}
        self._server_dots: dict[str, QLabel] = {}
        self._server_state: dict = {}
        self._power_state = "unset"
        self._server_players: dict[str, QLabel] = {}

    def set_power_state(self, running: Optional[bool]) -> None:
        """Bottom button: Stop when running, Start when not, disabled with no server."""
        if running == self._power_state:
            return  # timer-driven: skip when nothing changed
        self._power_state = running
        if running is None:
            self.power_btn.setEnabled(False)
            self.power_btn.setText("Stop Server")
            return
        self.power_btn.setEnabled(True)
        self.power_btn.setText("Stop Server" if running else "Start Server")
        self.power_btn.setIcon(assets.line_icon("power" if running else "play", "#ececec"))

    def set_server_status(self, server_id: str, running: bool, players: Optional[int] = None) -> None:
        """Status dot + player count next to a server's name."""
        # Timer-driven: only restyle when something changed.
        state = (running, players if running else None)
        if self._server_state.get(server_id) == state:
            return
        self._server_state[server_id] = state
        dot = self._server_dots.get(server_id)
        if dot is not None:
            dot.setProperty("running", running)
            dot.style().unpolish(dot)
            dot.style().polish(dot)
            dot.setToolTip("Running" if running else "Stopped")
        count = self._server_players.get(server_id)
        if count is not None:
            count.setText(str(players) if running and players is not None else "")

    def set_servers(self, servers: List[ServerConfig], active_id: str) -> None:
        for row in list(self._server_rows.values()):
            self.server_list_layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        for btn in self._server_group.buttons():
            self._server_group.removeButton(btn)
        self._server_rows.clear()
        self._server_buttons.clear()
        self._server_dots.clear()
        self._server_players.clear()
        self._server_state.clear()

        for s in servers:
            row = QWidget()
            row.setObjectName("TransparentRow")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(2)

            btn = QPushButton(s.name)
            btn.setObjectName("ServerRow")
            btn.setCheckable(True)
            btn.setChecked(s.id == active_id)
            btn.clicked.connect(lambda _=False, sid=s.id: self.server_selected.emit(sid))
            self._server_group.addButton(btn)
            # Child layout on the button keeps the whole row one click target.
            inner = QHBoxLayout(btn)
            inner.setContentsMargins(12, 0, 10, 0)
            dot = QLabel()
            dot.setObjectName("ServerDot")
            dot.setFixedSize(8, 8)
            inner.addWidget(dot)
            inner.addStretch(1)
            players = QLabel("")
            players.setObjectName("ServerPlayers")
            inner.addWidget(players)
            row_layout.addWidget(btn, 1)

            remove_btn = QPushButton()
            remove_btn.setObjectName("ServerRemoveButton")
            remove_btn.setIcon(assets.line_icon("close", self._muted))
            remove_btn.setIconSize(QSize(14, 14))
            remove_btn.setFixedSize(26, 26)
            remove_btn.setToolTip(f"Remove {s.name}")
            remove_btn.clicked.connect(lambda _=False, sid=s.id: self.server_remove_requested.emit(sid))
            row_layout.addWidget(remove_btn)

            self.server_list_layout.addWidget(row)
            self._server_buttons[s.id] = btn
            self._server_rows[s.id] = row
            self._server_dots[s.id] = dot
            self._server_players[s.id] = players
            self.set_server_status(s.id, False)

        self.add_server_btn.setEnabled(len(servers) < MAX_SERVERS)
        self.add_server_btn.setText(
            f"Add server ({len(servers)} of {MAX_SERVERS})" if servers else "Add server"
        )
        if not servers:
            self.set_power_state(None)

    def set_active_nav(self, key: str) -> None:
        for btn in self._nav_group.buttons():
            btn.setChecked(btn.property("nav_key") == key)

    # Nav entries that need an active server.
    _PER_SERVER_NAV_KEYS = {"players", "updates", "mods", "access", "console", "settings"}

    def set_per_server_nav_enabled(self, enabled: bool) -> None:
        """Enables/disables the nav entries that need an active server."""
        for btn in self._nav_group.buttons():
            if btn.property("nav_key") in self._PER_SERVER_NAV_KEYS:
                btn.setEnabled(enabled)
