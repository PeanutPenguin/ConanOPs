from __future__ import annotations

from PySide6.QtWidgets import QLabel, QLineEdit, QSpinBox, QCheckBox, QVBoxLayout

from ui.base_settings_page import SettingsPageBase


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
        # requires_restart=False at the PAGE level: this page is a mix
        # -- RCON settings are written into the server's Game.ini and
        # need a restart to take effect, but the webhook URLs are
        # ConanOps' own settings and take effect immediately. Labeling
        # the whole page's button as restart-gated would wrongly imply
        # a webhook change needs one too, so the caveat is scoped to
        # just the RCON fields instead, right where they are below.
        super().__init__("RCON & Alerts", parent, requires_restart=False, card_form=True)
        self._build_form()

    def _build_form(self) -> None:
        self.rcon_enabled_check = QCheckBox("Enable RCON")
        self.form_layout.addWidget(self.rcon_enabled_check)
        self.register_field("rcon_enabled", self.rcon_enabled_check, lambda w: w.isChecked(), lambda w, v: w.setChecked(v), self.rcon_enabled_check.toggled)

        self.rcon_port_spin = QSpinBox()
        self.rcon_port_spin.setRange(1024, 65535)
        self.form_layout.addLayout(_row("RCON Port", self.rcon_port_spin))
        self.register_field("rcon_port", self.rcon_port_spin, lambda w: w.value(), lambda w, v: w.setValue(v), self.rcon_port_spin.valueChanged)

        self.rcon_password_edit = QLineEdit()
        self.rcon_password_edit.setEchoMode(QLineEdit.Password)
        self.form_layout.addLayout(_row("RCON Password (usually the admin password)", self.rcon_password_edit))
        self.register_field("rcon_password", self.rcon_password_edit, lambda w: w.text(), lambda w, v: w.setText(v), self.rcon_password_edit.textChanged)

        rcon_restart_note = QLabel("Takes effect the next time the server restarts.")
        rcon_restart_note.setObjectName("Dim")
        self.form_layout.addWidget(rcon_restart_note)

        self.form_layout.addWidget(QLabel(""))  # spacer

        self.discord_edit = QLineEdit()
        self.discord_edit.setPlaceholderText("https://discord.com/api/webhooks/...")
        self.form_layout.addLayout(_row(
            "Discord Webhook URL — alerts on crash, update, backup failure, and auto-repair",
            self.discord_edit,
        ))
        self.register_field("webhook_discord_url", self.discord_edit, lambda w: w.text(), lambda w, v: w.setText(v), self.discord_edit.textChanged)

        self.discord_status_check = QCheckBox("Also show a live status message in Discord (updates every ~5 minutes)")
        self.form_layout.addWidget(self.discord_status_check)
        self.register_field(
            "discord_status_enabled", self.discord_status_check,
            lambda w: w.isChecked(), lambda w, v: w.setChecked(v), self.discord_status_check.toggled,
        )
        discord_status_note = QLabel(
            "Edits one message in place (online/offline, player count) rather than posting a new "
            "one each time -- needs the Discord Webhook URL above to be set."
        )
        discord_status_note.setObjectName("Dim")
        discord_status_note.setWordWrap(True)
        self.form_layout.addWidget(discord_status_note)

        self.ntfy_edit = QLineEdit()
        self.ntfy_edit.setPlaceholderText("https://ntfy.sh/your-topic-name")
        self.form_layout.addLayout(_row("ntfy.sh Topic URL (alternative or additional alert channel)", self.ntfy_edit))
        self.register_field("webhook_ntfy_url", self.ntfy_edit, lambda w: w.text(), lambda w, v: w.setText(v), self.ntfy_edit.textChanged)

        webhook_note = QLabel("Takes effect immediately -- no server restart needed.")
        webhook_note.setObjectName("Dim")
        self.form_layout.addWidget(webhook_note)

        self.form_layout.addStretch(1)

    def on_apply(self, values: dict) -> None:
        pass
