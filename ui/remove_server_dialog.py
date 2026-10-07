"""
Confirmation dialog for removing a server from ConanOps (Sidebar's
per-server remove button). A plain QMessageBox can't hold more than
one checkbox, and this needs two -- delete the server's files, delete
its backups, independently -- so it's its own small QDialog instead,
same pattern as UnlockDialog/SetPinDialog in app_lock_dialog.py.

The base action (forgetting the server) is unconditional and always
described up front; the checkboxes are purely additive, both default
CHECKED: removing a server leaves nothing behind unless the person
deliberately unticks a box to keep its files or backups.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QPushButton, QFrame,
)


class RemoveServerDialog(QDialog):
    def __init__(
        self,
        server_name: str,
        install_dir: str,
        backup_destination: str,
        is_running: bool,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Remove Server")
        self.setModal(True)
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)

        intro = QLabel(
            f"This removes \"{server_name}\" from ConanOps -- its settings, session history, "
            f"firewall rules and router port forwards. Untick a box below to keep those files. "
            f"This can't be undone."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        layout.addWidget(_divider())

        self.delete_files_cb: Optional[QCheckBox] = None
        self.delete_backups_cb: Optional[QCheckBox] = None

        if install_dir:
            running_note = (
                " It's running -- it will be saved and stopped first."
                if is_running else ""
            )
            self.delete_files_cb = QCheckBox("Delete the server's files (the server, its SteamCMD and its folder)")
            self.delete_files_cb.setChecked(True)
            layout.addWidget(self.delete_files_cb)
            files_detail = QLabel(f"{install_dir}{running_note}")
            files_detail.setObjectName("Muted")
            files_detail.setWordWrap(True)
            files_detail.setContentsMargins(24, 0, 0, 8)
            layout.addWidget(files_detail)
        else:
            no_files_note = QLabel("No install folder is configured, so there are no server files to delete.")
            no_files_note.setObjectName("Muted")
            no_files_note.setWordWrap(True)
            layout.addWidget(no_files_note)

        if backup_destination:
            self.delete_backups_cb = QCheckBox("Delete this server's backups (its world saves)")
            self.delete_backups_cb.setChecked(True)
            layout.addWidget(self.delete_backups_cb)
            backups_detail = QLabel(backup_destination)
            backups_detail.setObjectName("Muted")
            backups_detail.setWordWrap(True)
            backups_detail.setContentsMargins(24, 0, 0, 8)
            layout.addWidget(backups_detail)

        steamcmd_note = QLabel(
            "Anything another server still uses (like a shared SteamCMD folder) is kept."
        )
        steamcmd_note.setObjectName("Muted")
        steamcmd_note.setWordWrap(True)
        layout.addWidget(steamcmd_note)

        btn_row = QHBoxLayout()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.clicked.connect(self.accept)
        btn_row.addStretch(1)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(self.remove_btn)
        layout.addLayout(btn_row)

    @property
    def delete_files(self) -> bool:
        return self.delete_files_cb is not None and self.delete_files_cb.isChecked()

    @property
    def delete_backups(self) -> bool:
        return self.delete_backups_cb is not None and self.delete_backups_cb.isChecked()


def _divider() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setFrameShadow(QFrame.Sunken)
    return line
