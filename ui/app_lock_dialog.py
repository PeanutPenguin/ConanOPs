"""
Optional startup PIN lock and the tray dialog to set/change it. A single
salted PIN hash, not real multi-user auth.
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton


class UnlockDialog(QDialog):
    """Startup PIN prompt with only Unlock or Quit (a skippable lock isn't a
    lock). Failed attempts get a growing delay to slow brute-forcing."""

    _FREE_ATTEMPTS = 2       # typos happen
    _MAX_DELAY_SECONDS = 30

    def __init__(self, verify_fn: Callable[[str], bool], parent=None):
        super().__init__(parent)
        self.setWindowTitle("ConanOps Locked")
        self.setModal(True)
        self._verify_fn = verify_fn
        self._fail_count = 0

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Enter your ConanOps PIN:"))
        self.pin_edit = QLineEdit()
        self.pin_edit.setEchoMode(QLineEdit.Password)
        self.pin_edit.returnPressed.connect(self._try_unlock)
        layout.addWidget(self.pin_edit)
        self.error_label = QLabel("")
        self.error_label.setObjectName("ErrorText")
        layout.addWidget(self.error_label)

        btn_row = QHBoxLayout()
        quit_btn = QPushButton("Quit")
        quit_btn.clicked.connect(self.reject)
        self.unlock_btn = QPushButton("Unlock")
        self.unlock_btn.setObjectName("PrimaryButton")
        self.unlock_btn.clicked.connect(self._try_unlock)
        btn_row.addStretch(1)
        btn_row.addWidget(quit_btn)
        btn_row.addWidget(self.unlock_btn)
        layout.addLayout(btn_row)

    def _try_unlock(self) -> None:
        if self._verify_fn(self.pin_edit.text()):
            self.accept()
            return

        self._fail_count += 1
        self.pin_edit.clear()
        over = self._fail_count - self._FREE_ATTEMPTS
        if over <= 0:
            self.error_label.setText("Incorrect PIN.")
            self.pin_edit.setFocus()
            return

        delay = min(2 ** (over - 1), self._MAX_DELAY_SECONDS)
        self._lock_input_for(delay)

    def _lock_input_for(self, seconds: int) -> None:
        self.pin_edit.setEnabled(False)
        self.unlock_btn.setEnabled(False)
        self._remaining = seconds
        self._tick_lockout_label()
        self._lockout_timer = QTimer(self)
        self._lockout_timer.timeout.connect(self._tick_lockout_label)
        self._lockout_timer.start(1000)

    def _tick_lockout_label(self) -> None:
        if self._remaining <= 0:
            self._lockout_timer.stop()
            self.pin_edit.setEnabled(True)
            self.unlock_btn.setEnabled(True)
            self.error_label.setText("Incorrect PIN.")
            self.pin_edit.setFocus()
            return
        self.error_label.setText(f"Too many incorrect attempts -- try again in {self._remaining}s.")
        self._remaining -= 1


class SetPinDialog(QDialog):
    """New PIN entered twice; changing needs the current PIN first.
    Blank new-PIN fields remove the lock."""

    def __init__(self, current_pin_set: bool, verify_fn: Callable[[str], bool], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Change App PIN" if current_pin_set else "Set App PIN")
        self.setModal(True)
        self._verify_fn = verify_fn
        self._current_pin_set = current_pin_set
        self.new_pin: Optional[str] = None  # None on cancel; "" on accept means "remove the lock"

        layout = QVBoxLayout(self)

        self.current_edit = None
        if current_pin_set:
            layout.addWidget(QLabel("Current PIN:"))
            self.current_edit = QLineEdit()
            self.current_edit.setEchoMode(QLineEdit.Password)
            layout.addWidget(self.current_edit)

        layout.addWidget(QLabel("New PIN (leave both fields below blank to remove the lock):"))
        self.new_edit = QLineEdit()
        self.new_edit.setEchoMode(QLineEdit.Password)
        layout.addWidget(self.new_edit)
        layout.addWidget(QLabel("Confirm new PIN:"))
        self.confirm_edit = QLineEdit()
        self.confirm_edit.setEchoMode(QLineEdit.Password)
        layout.addWidget(self.confirm_edit)

        self.error_label = QLabel("")
        self.error_label.setObjectName("ErrorText")
        layout.addWidget(self.error_label)

        btn_row = QHBoxLayout()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        save_btn = QPushButton("Save")
        save_btn.setObjectName("PrimaryButton")
        save_btn.clicked.connect(self._try_save)
        btn_row.addStretch(1)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

    def _try_save(self) -> None:
        if self._current_pin_set and self.current_edit is not None:
            if not self._verify_fn(self.current_edit.text()):
                self.error_label.setText("Current PIN is incorrect.")
                return

        new_pin = self.new_edit.text()
        confirm = self.confirm_edit.text()
        if new_pin != confirm:
            self.error_label.setText("New PIN and confirmation don't match.")
            return
        if new_pin and len(new_pin) < 4:
            self.error_label.setText("PIN must be at least 4 characters (or blank to remove it).")
            return

        self.new_pin = new_pin  # "" means "remove the lock"
        self.accept()
