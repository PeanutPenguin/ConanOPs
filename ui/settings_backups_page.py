from __future__ import annotations

from PySide6.QtWidgets import (
    QLabel, QSpinBox, QLineEdit, QHBoxLayout, QVBoxLayout, QPushButton,
    QFileDialog, QCheckBox, QFrame,
)

from ui.base_settings_page import SettingsPageBase
from ui.backups_page import BackupsPage


def _row(label_text: str, widget) -> QVBoxLayout:
    col = QVBoxLayout()
    col.setSpacing(6)
    lbl = QLabel(label_text)
    lbl.setObjectName("Muted")
    col.addWidget(lbl)
    col.addWidget(widget)
    return col


class SettingsBackupsPage(SettingsPageBase):
    """Backup schedule/retention/destination settings (normal Apply/Discard)
    plus an embedded BackupsPage whose actions take effect immediately."""

    def __init__(self, parent=None):
        # These are ConanOps' own scheduler settings, not server .ini keys.
        super().__init__("Backups", parent, requires_restart=False, card_form=True)
        self._build_form()

    def _build_form(self) -> None:
        section = QLabel("Schedule & Retention")
        section.setObjectName("Muted")
        self.form_layout.addWidget(section)

        row = QHBoxLayout()
        self.daily_spin = QSpinBox()
        self.daily_spin.setRange(0, 60)
        self.weekly_spin = QSpinBox()
        self.weekly_spin.setRange(0, 52)
        row.addLayout(_row("Daily backups kept", self.daily_spin))
        row.addLayout(_row("Weekly backups kept", self.weekly_spin))
        self.form_layout.addLayout(row)
        self.register_field("backup_daily_keep", self.daily_spin, lambda w: w.value(), lambda w, v: w.setValue(v), self.daily_spin.valueChanged)
        self.register_field("backup_weekly_keep", self.weekly_spin, lambda w: w.value(), lambda w, v: w.setValue(v), self.weekly_spin.valueChanged)

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 48)
        self.form_layout.addLayout(_row("Backup every (hours)", self.interval_spin))
        self.register_field("backup_interval_hours", self.interval_spin, lambda w: w.value(), lambda w, v: w.setValue(v), self.interval_spin.valueChanged)

        dest_row = QHBoxLayout()
        self.dest_edit = QLineEdit()
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse)
        dest_row.addWidget(self.dest_edit, 1)
        dest_row.addWidget(browse_btn)
        dest_wrap = QFrame()
        dest_wrap.setLayout(dest_row)
        self.form_layout.addLayout(_row("Destination folder", dest_wrap))
        dest_note = QLabel(
            "Pre-filled automatically inside this server's own ConanOps folder the first time "
            "a server is set up -- change it any time."
        )
        dest_note.setObjectName("Dim")
        dest_note.setWordWrap(True)
        self.form_layout.addWidget(dest_note)
        self.register_field("backup_destination", self.dest_edit, lambda w: w.text(), lambda w, v: w.setText(v), self.dest_edit.textChanged)

        self.pre_update_check = QCheckBox("Always back up before applying an update")
        self.form_layout.addWidget(self.pre_update_check)
        self.register_field("backup_before_update", self.pre_update_check, lambda w: w.isChecked(), lambda w, v: w.setChecked(v), self.pre_update_check.toggled)

        divider = QFrame()
        divider.setFrameShape(QFrame.HLine)
        self.form_layout.addWidget(divider)

        self.backups_widget = BackupsPage(embedded=True)
        self.form_layout.addWidget(self.backups_widget)

        self.form_layout.addStretch(1)

    def set_server(self, server) -> None:
        """Feeds the embedded backup list; load_committed() handles the settings fields."""
        self.backups_widget.set_server(server)

    def refresh(self) -> None:
        self.backups_widget.refresh()

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose backup destination")
        if path:
            self.dest_edit.setText(path)

    def on_apply(self, values: dict) -> None:
        pass
