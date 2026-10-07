from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QLabel, QMessageBox, QPushButton

import proc_utils
from ui.generic_settings_page import GenericSettingsPage
from ini_field_specs import IDENTITY_FIELDS


class SettingsIdentityPage(GenericSettingsPage):
    """Funcom's 'General' settings, minus ServerName/ServerPassword/MaxPlayers
    (on Network & Ports). The ConanOps-only __description field is never
    written to an .ini ('__' keys are filtered out). "Open Server Folder" is
    outside the Apply/Discard form."""

    def __init__(self, parent=None, common_keys=None):
        super().__init__("Server Identity", IDENTITY_FIELDS, parent, common_keys=common_keys)
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
        """Set on server change; load_committed() only gets the ini values."""
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
