from __future__ import annotations

from PySide6.QtWidgets import QCheckBox, QLabel, QLineEdit, QSpinBox, QVBoxLayout

from ui.base_settings_page import SettingsPageBase
from ui.more_options import MoreOptions


def _row(label_text: str, widget) -> QVBoxLayout:
    col = QVBoxLayout()
    col.setSpacing(6)
    lbl = QLabel(label_text)
    lbl.setObjectName("Muted")
    col.addWidget(lbl)
    col.addWidget(widget)
    return col


class SettingsAlertsPage(SettingsPageBase):
    def __init__(self, parent=None):
        # RCON settings live in Game.ini, read at server startup.
        super().__init__("RCON", parent, requires_restart=True, card_form=True)
        self._build_form()

    def _build_form(self) -> None:
        self.rcon_enabled_check = QCheckBox("Enable RCON")
        self.form_layout.addWidget(self.rcon_enabled_check)
        self.register_field("rcon_enabled", self.rcon_enabled_check, lambda w: w.isChecked(), lambda w, v: w.setChecked(v), self.rcon_enabled_check.toggled)

        self.more = MoreOptions("More options", 2, card=False)
        more = self.more.body_layout
        more.setSpacing(12)
        more.setContentsMargins(0, 8, 0, 4)
        self.form_layout.addWidget(self.more)

        self.rcon_port_spin = QSpinBox()
        self.rcon_port_spin.setRange(1024, 65535)
        more.addLayout(_row("RCON Port", self.rcon_port_spin))
        self.register_field("rcon_port", self.rcon_port_spin, lambda w: w.value(), lambda w, v: w.setValue(v), self.rcon_port_spin.valueChanged)

        self.rcon_password_edit = QLineEdit()
        self.rcon_password_edit.setEchoMode(QLineEdit.Password)
        more.addLayout(_row("RCON Password (usually the admin password)", self.rcon_password_edit))
        self.register_field("rcon_password", self.rcon_password_edit, lambda w: w.text(), lambda w, v: w.setText(v), self.rcon_password_edit.textChanged)

        alerts_note = QLabel(
            "Discord and ntfy alerts are set once for all servers, in App Settings → Alerts."
        )
        alerts_note.setObjectName("Dim")
        alerts_note.setWordWrap(True)
        self.form_layout.addWidget(alerts_note)

        self.form_layout.addStretch(1)

    def on_apply(self, values: dict) -> None:
        pass
