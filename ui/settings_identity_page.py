from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QLabel, QMessageBox, QPushButton

import proc_utils
from ui.generic_settings_page import GenericSettingsPage
from ini_field_specs import IDENTITY_FIELDS


class SettingsIdentityPage(GenericSettingsPage):
    """Everything from Funcom's own 'General' settings section, except
    ServerName/ServerPassword (those live on Network & Ports) and
    MaxPlayers (treated as a network setting in ConanOps). Includes one
    ConanOps-only field, __description, which is never written to any
    .ini -- MainWindow filters '__'-prefixed keys out before writing.

    Also carries one non-.ini control, "Open Server Folder" -- opens
    the server's own install_dir in Explorer. This is deliberately
    outside the staged-edit form above it (it doesn't belong to
    _fields/_getters/_setters and has nothing to do with Apply/
    Discard): it's here because being able to actually SEE what's on
    disk is the fastest way to sort out "why does ConanOps say it
    can't find X" -- e.g. the World save check on the Diagnostics tab,
    or a backup failing because ConanSandbox/Saved isn't where it's
    expected to be."""

    def __init__(self, parent=None):
        super().__init__("Server Identity", IDENTITY_FIELDS, parent)
        self._install_dir = ""

        row = QHBoxLayout()
        self.open_folder_btn = QPushButton("Open Server Folder")
        self.open_folder_btn.clicked.connect(self._open_server_folder)
        row.addWidget(self.open_folder_btn)
        hint = QLabel("Opens this server's install folder in Explorer.")
        hint.setObjectName("Muted")
        row.addWidget(hint)
        row.addStretch(1)
        self.form_layout.insertLayout(0, row)

    def set_install_dir(self, install_dir: str) -> None:
        """Called by MainWindow._load_active_server() whenever the
        active server changes -- this page only otherwise ever sees
        server.gameplay (an ini-values dict with no install_dir in
        it), via load_committed()."""
        self._install_dir = install_dir or ""

    def _open_server_folder(self) -> None:
        if not self._install_dir:
            QMessageBox.information(
                self, "No Install Folder",
                "This server doesn't have an install folder configured yet -- run the setup "
                "wizard first (Dashboard, \"Set Up Server\").",
            )
            return
        try:
            proc_utils.open_in_explorer(self._install_dir)
        except FileNotFoundError:
            QMessageBox.warning(
                self, "Folder Not Found",
                f"The configured install folder doesn't exist: {self._install_dir}\n\n"
                f"It may have been moved or deleted -- check the Diagnostics settings tab, "
                f"or re-run the setup wizard to reinstall.",
            )
        except OSError as e:
            QMessageBox.warning(self, "Couldn't Open Folder", f"Couldn't open {self._install_dir}: {e}")
