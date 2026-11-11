from __future__ import annotations

import os

from PySide6.QtWidgets import (
    QLabel, QSpinBox, QLineEdit, QHBoxLayout, QVBoxLayout, QPushButton,
    QFileDialog, QCheckBox, QFrame,
)

from ui.base_settings_page import SettingsPageBase
from ui.backups_page import BackupsPage
from ui.more_options import MoreOptions


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(path.strip()))).rstrip("\\/")


def copy_folder_problem(main: str, copy: str) -> str:
    """Why the second copy folder can't be used, or ""."""
    if not copy.strip():
        return ""
    if not os.path.isabs(os.path.expanduser(copy.strip())):
        return "The second copy folder needs a full path, like D:\\ConanOps Backups."
    if not main.strip():
        return ""
    a, b = _norm(main), _norm(copy)
    if a == b:
        return "The second copy folder is the main backup folder. Pick a different one, ideally on another drive."
    if b.startswith(a + os.sep) or a.startswith(b + os.sep):
        return "The second copy folder can't be inside the main backup folder, or the other way round."
    return ""


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
        self.daily_spin.setSuffix(" days")
        self.weekly_spin.setSuffix(" weeks")
        self.daily_spin.setToolTip("For this many days, the newest backup of each day is kept.")
        self.weekly_spin.setToolTip("After that, the newest backup of each week is kept for this many weeks.")
        row.addLayout(_row("Days of daily backups", self.daily_spin))
        row.addLayout(_row("Weeks of weekly backups", self.weekly_spin))
        self.form_layout.addLayout(row)
        keep_note = QLabel(
            "Every backup from the last 24 hours is kept. After that, the newest of each day is kept for the "
            "days above, then the newest of each week for the weeks above. Backups you make yourself or import "
            "are never deleted, and the last 3 safety backups (made before an update or a restore) are always "
            "kept, as is anything from the last 14 days.")
        keep_note.setObjectName("Dim")
        keep_note.setWordWrap(True)
        self.form_layout.addWidget(keep_note)
        self.register_field("backup_daily_keep", self.daily_spin, lambda w: w.value(), lambda w, v: w.setValue(v), self.daily_spin.valueChanged)
        self.register_field("backup_weekly_keep", self.weekly_spin, lambda w: w.value(), lambda w, v: w.setValue(v), self.weekly_spin.valueChanged)

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 48)
        self.form_layout.addLayout(_row("Backup every (hours)", self.interval_spin))
        self.register_field("backup_interval_hours", self.interval_spin, lambda w: w.value(), lambda w, v: w.setValue(v), self.interval_spin.valueChanged)

        # Less-used: where backups go, and the pre-update backup switch.
        self.more = MoreOptions("More options", 3, card=False)
        more = self.more.body_layout
        more.setSpacing(12)
        more.setContentsMargins(0, 8, 0, 4)
        self.form_layout.addWidget(self.more)

        dest_row = QHBoxLayout()
        self.dest_edit = QLineEdit()
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse)
        dest_row.addWidget(self.dest_edit, 1)
        dest_row.addWidget(browse_btn)
        dest_wrap = QFrame()
        dest_wrap.setLayout(dest_row)
        more.addLayout(_row("Destination folder", dest_wrap))
        dest_note = QLabel(
            "Pre-filled automatically inside this server's own ConanOps folder the first time "
            "a server is set up -- change it any time."
        )
        dest_note.setObjectName("Dim")
        dest_note.setWordWrap(True)
        more.addWidget(dest_note)
        self.register_field("backup_destination", self.dest_edit, lambda w: w.text(), lambda w, v: w.setText(v), self.dest_edit.textChanged)

        copy_row = QHBoxLayout()
        self.copy_edit = QLineEdit()
        self.copy_edit.setPlaceholderText("Optional: another drive, a USB disk or a synced folder")
        self.copy_edit.setToolTip("After each backup, a copy goes here too, kept to the same days and weeks. "
                                  "If the main backup drive fails, the copy is still there.")
        copy_btn = QPushButton("Browse…")
        copy_btn.clicked.connect(self._browse_copy)
        copy_row.addWidget(self.copy_edit, 1)
        copy_row.addWidget(copy_btn)
        copy_wrap = QFrame()
        copy_wrap.setLayout(copy_row)
        more.addLayout(_row("Second copy folder", copy_wrap))
        copy_note = QLabel("After each backup, ConanOps copies it here as well, best on a different drive, and "
                           "keeps it to the same days and weeks. Leave it empty for no second copy.")
        copy_note.setObjectName("Dim")
        copy_note.setWordWrap(True)
        more.addWidget(copy_note)
        self.copy_error_label = QLabel("")
        self.copy_error_label.setObjectName("ErrorText")
        self.copy_error_label.setWordWrap(True)
        self.copy_error_label.hide()
        more.addWidget(self.copy_error_label)
        self.register_field("backup_copy_destination", self.copy_edit, lambda w: w.text().strip(),
                            lambda w, v: w.setText(v or ""), self.copy_edit.textChanged)
        self.copy_edit.textChanged.connect(self._check_copy)
        self.dest_edit.textChanged.connect(self._check_copy)

        self.pre_update_check = QCheckBox("Always back up before applying an update")
        more.addWidget(self.pre_update_check)
        self.register_field("backup_before_update", self.pre_update_check, lambda w: w.isChecked(), lambda w, v: w.setChecked(v), self.pre_update_check.toggled)

        divider = QFrame()
        divider.setFrameShape(QFrame.HLine)
        self.form_layout.addWidget(divider)
        self.divider = divider

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

    def _browse_copy(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose a folder for the second copy")
        if path:
            self.copy_edit.setText(path)

    def copy_problem(self) -> str:
        return copy_folder_problem(self.dest_edit.text(), self.copy_edit.text())

    def _check_copy(self, *_args) -> None:
        problem = self.copy_problem()
        self.copy_error_label.setText(problem)
        self.copy_error_label.setVisible(bool(problem))
        if problem:
            self.more.set_open(True)
        self._update_pending_ui()

    def can_apply(self) -> bool:
        return not self.copy_problem()

    def load_committed(self, values: dict) -> None:
        super().load_committed(values)
        self._check_copy()

    def on_apply(self, values: dict) -> None:
        pass
