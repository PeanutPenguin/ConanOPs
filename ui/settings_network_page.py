from __future__ import annotations

from PySide6.QtWidgets import (
    QLabel, QLineEdit, QSpinBox, QHBoxLayout, QVBoxLayout, QPushButton,
    QFrame, QCheckBox,
)

import network_utils
from ui.base_settings_page import SettingsPageBase


def _row(label_text: str, tooltip: str, widget) -> QVBoxLayout:
    col = QVBoxLayout()
    col.setSpacing(6)
    lbl = QLabel(label_text)
    lbl.setToolTip(tooltip)
    lbl.setObjectName("Muted")
    col.addWidget(lbl)
    col.addWidget(widget)
    return col


class SettingsNetworkPage(SettingsPageBase):
    def __init__(self, get_reserved_ports, parent=None):
        super().__init__("Network & Ports", parent, card_form=True)
        self.get_reserved_ports = get_reserved_ports  # callable -> set(ports) used by OTHER servers
        self._has_conflict = False
        self._bind_error = False
        self._name_error = False
        # Set by MainWindow: re-creates firewall rules and router forwards.
        self.on_repair_network = None
        self._build_form()

    def can_apply(self) -> bool:
        # Block Apply on conflicts so they're fixed now, not at launch.
        return not (self._has_conflict or self._bind_error or self._name_error)

    def load_committed(self, values: dict) -> None:
        super().load_committed(values)
        self._check_bind_ip()
        self._check_name()
        # Re-check on load so saved conflicts show immediately.
        self._check_port_conflict()

    def _build_form(self) -> None:
        self.name_edit = QLineEdit()
        self.form_layout.addLayout(_row("Server Name", "The name shown in the server browser.", self.name_edit))
        self.register_field("name", self.name_edit, lambda w: " ".join(w.text().split()), lambda w, v: w.setText(v), self.name_edit.textChanged)
        self.name_error_label = QLabel("")
        self.name_error_label.setObjectName("ErrorText")
        self.name_error_label.hide()
        self.form_layout.addWidget(self.name_error_label)
        self.name_edit.textChanged.connect(self._check_name)

        pw_row = QHBoxLayout()
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)
        show_btn = QPushButton("Show")
        show_btn.setCheckable(True)
        show_btn.toggled.connect(
            lambda on: self.password_edit.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password)
        )
        pw_row.addWidget(self.password_edit, 1)
        pw_row.addWidget(show_btn)
        pw_wrap = QFrame()
        pw_wrap.setLayout(pw_row)
        self.form_layout.addLayout(_row("Server Password", "Leave blank for a public server.", pw_wrap))
        self.register_field("password", self.password_edit, lambda w: w.text(), lambda w, v: w.setText(v), self.password_edit.textChanged)

        self.game_port_spin = QSpinBox()
        self.game_port_spin.setRange(1024, 65534)  # game port + 1 must also be a valid port
        self.form_layout.addLayout(_row(
            "Game Port",
            "The UDP port players connect to directly. Changing it disconnects everyone until they reconnect on the new port.",
            self.game_port_spin,
        ))
        self.register_field("game_port", self.game_port_spin, lambda w: w.value(), lambda w, v: w.setValue(v), self.game_port_spin.valueChanged)

        self.query_port_spin = QSpinBox()
        self.query_port_spin.setRange(1024, 65535)
        self.conflict_label = QLabel("")
        self.conflict_label.setObjectName("ErrorText")
        self.conflict_label.hide()
        self.fix_port_btn = QPushButton("Use suggested port")
        self.fix_port_btn.hide()
        self.fix_port_btn.clicked.connect(self._fix_port_conflict)
        query_col = _row(
            "Query Port",
            "The port used for server-info queries (player count, ping). ConanOps checks it's free across all your configured servers and offers a fix if not.",
            self.query_port_spin,
        )
        query_col.addWidget(self.conflict_label)
        query_col.addWidget(self.fix_port_btn)
        self.form_layout.addLayout(query_col)
        self.register_field("query_port", self.query_port_spin, lambda w: w.value(), lambda w, v: w.setValue(v), self.query_port_spin.valueChanged)

        self.game_port_spin.valueChanged.connect(self._check_port_conflict)
        self.query_port_spin.valueChanged.connect(self._check_port_conflict)

        ip_row = QHBoxLayout()
        self.ip_edit = QLineEdit()
        detect_btn = QPushButton("Auto-detect")
        detect_btn.clicked.connect(self._auto_detect_ip)
        ip_row.addWidget(self.ip_edit, 1)
        ip_row.addWidget(detect_btn)
        ip_wrap = QFrame()
        ip_wrap.setLayout(ip_row)
        self.form_layout.addLayout(_row(
            "Bind Address (Multihome)",
            "The local network interface the server binds to. Must be a real IP on this machine, not the public/router IP.",
            ip_wrap,
        ))
        self.register_field("bind_ip", self.ip_edit, lambda w: w.text().strip(), lambda w, v: w.setText(v), self.ip_edit.textChanged)
        self.bind_error_label = QLabel("")
        self.bind_error_label.setObjectName("ErrorText")
        self.bind_error_label.setWordWrap(True)
        self.bind_error_label.hide()
        self.form_layout.addWidget(self.bind_error_label)
        self.ip_edit.textChanged.connect(self._check_bind_ip)

        self.max_players_spin = QSpinBox()
        self.max_players_spin.setRange(1, 250)
        self.form_layout.addLayout(_row("Max Players", "Hard cap on concurrent connections.", self.max_players_spin))
        self.register_field("max_players", self.max_players_spin, lambda w: w.value(), lambda w, v: w.setValue(v), self.max_players_spin.valueChanged)

        repair_col = QVBoxLayout()
        repair_col.setSpacing(6)
        self.repair_btn = QPushButton("Repair Networking")
        self.repair_btn.setToolTip(
            "Re-creates this server's Windows Firewall rules and router port forwards (UPnP) for its "
            "current ports and bind address. Windows may ask for permission."
        )
        self.repair_btn.clicked.connect(self._on_repair_clicked)
        repair_note = QLabel(
            "Re-adds the firewall rules and router forwarding for the ports above. Use this if "
            "Diagnostics reports a missing rule, or after changing routers."
        )
        repair_note.setObjectName("Dim")
        repair_note.setWordWrap(True)
        repair_col.addWidget(self.repair_btn)
        repair_col.addWidget(repair_note)
        self.form_layout.addLayout(repair_col)

        self.form_layout.addStretch(1)

    def _on_repair_clicked(self) -> None:
        if self.dirty_count() > 0:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(
                self, "Apply first",
                "Apply (or Discard) your pending changes on this page first -- Repair uses the saved settings.",
            )
            return
        if callable(self.on_repair_network):
            self.repair_btn.setEnabled(False)
            self.repair_btn.setText("Repairing…")
            self.on_repair_network()

    def repair_finished(self) -> None:
        self.repair_btn.setEnabled(True)
        self.repair_btn.setText("Repair Networking")

    def _check_name(self) -> None:
        text = self.name_edit.text()
        bad = ""
        cleaned = " ".join(text.split())
        if not cleaned:
            bad = "The server needs a name."
        elif any(ord(ch) < 32 or ord(ch) == 127 for ch in text.strip()):
            bad = "The name can't contain tabs or line breaks."
        elif len(cleaned) > 80:
            bad = "Keep the name to 80 characters or fewer."
        self._name_error = bool(bad)
        self.name_error_label.setText(bad)
        self.name_error_label.setVisible(bool(bad))
        self._update_pending_ui()

    def _check_bind_ip(self) -> None:
        ip = self.ip_edit.text().strip()
        msg = ""
        if ip:
            if not network_utils.is_valid_ipv4(ip):
                msg = "Enter an IPv4 address like 192.168.1.50 (or leave it empty to auto-detect)."
            else:
                local = network_utils.list_local_ipv4s()
                if local and ip not in local:
                    msg = (f"{ip} isn't an address on this PC. Use this PC's local address (click "
                           f"Auto-detect) -- not your public/router IP.")
        self._bind_error = bool(msg)
        self.bind_error_label.setText(msg)
        self.bind_error_label.setVisible(bool(msg))
        self._update_pending_ui()

    def _auto_detect_ip(self) -> None:
        self.ip_edit.setText(network_utils.get_local_ip())

    def _check_port_conflict(self) -> None:
        game = self.game_port_spin.value()
        query = self.query_port_spin.value()
        reserved = self.get_reserved_ports()

        # A running server binds its own committed ports, so skip the live
        # bind test for those (game, game+1, query).
        committed_game = self._committed.get("game_port")
        committed_query = self._committed.get("query_port")
        own_ports = set()
        if isinstance(committed_game, int):
            own_ports |= {committed_game, committed_game + 1}
        if isinstance(committed_query, int):
            own_ports.add(committed_query)

        def live_free(port: int) -> bool:
            return port in own_ports or network_utils.is_udp_port_free(port)

        game_plus_one = game + 1
        overlap = query in (game, game_plus_one)
        game_conflict = (
            overlap or game > 65534 or game in reserved or game_plus_one in reserved
            or not live_free(game) or not live_free(game_plus_one)
        )
        query_conflict = overlap or query in reserved or not live_free(query)
        conflict = game_conflict or query_conflict
        self._has_conflict = conflict

        self.conflict_label.setVisible(conflict)
        self.fix_port_btn.setVisible(conflict)
        if conflict:
            if query == game:
                self.conflict_label.setText(f"Game and Query ports can't both be {game}.")
            elif query == game_plus_one:
                self.conflict_label.setText(
                    f"Query port can't be {query} -- the server also uses the port right above the game port."
                )
            elif game_conflict and query_conflict:
                self.conflict_label.setText(
                    f"Ports {game}/{game_plus_one} and {query} conflict with another server or program."
                )
            elif game_conflict:
                self.conflict_label.setText(
                    f"Game port {game} (or {game_plus_one}, which the server also uses) conflicts with "
                    f"another server or program."
                )
            else:
                self.conflict_label.setText(f"Port {query} conflicts with another server or program.")
        # The base class's dirty-tracking handler may run before this one;
        # refresh the Apply button now that _has_conflict is current.
        self._update_pending_ui()

    def _fix_port_conflict(self) -> None:
        reserved = set(self.get_reserved_ports())
        try:
            game, query = network_utils.find_free_port_pair(
                self.game_port_spin.value(), reserved, start_query_port=self.query_port_spin.value(),
            )
        except RuntimeError:
            try:
                game, query = network_utils.find_free_port_pair(7777, reserved, max_attempts=500)
            except RuntimeError:
                self.conflict_label.setText("Couldn't find free ports automatically -- pick different ports by hand.")
                return
        self.game_port_spin.setValue(game)
        self.query_port_spin.setValue(query)

    def on_apply(self, values: dict) -> None:
        # Set by main_window via `page.on_apply = ...`.
        pass
