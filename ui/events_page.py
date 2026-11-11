"""
The Events page: a server's scheduled events (scheduled_events.py), such as
double XP at weekends or a message in game chat every couple of hours.
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

import scheduled_events as se

DAY_FULL = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
REPEAT_LABELS = {0: "Just once", 15: "Every 15 minutes", 30: "Every 30 minutes", 60: "Every hour",
                 120: "Every 2 hours", 180: "Every 3 hours", 240: "Every 4 hours", 360: "Every 6 hours"}


def status_text(ev: dict) -> str:
    state = ev.get("state") or {}
    if state.get("active"):
        return "On now" + (" (restart pending)" if state.get("needs_restart") else "")
    return "Scheduled" if ev.get("enabled", True) else "Paused"


def _value_text(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


class _ChangeRow(QWidget):
    def __init__(self, specs, key: str = "", value="", on_remove=None, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.combo = QComboBox()
        for spec in specs:
            self.combo.addItem(spec.label, spec.key)
        if key:
            self.combo.setCurrentIndex(max(0, self.combo.findData(key)))
        self.specs = {s.key: s for s in specs}
        self.value = QLineEdit()
        self.value.setFixedWidth(140)
        self.combo.currentIndexChanged.connect(lambda _i: self._placeholder())
        lay.addWidget(self.combo, 1)
        lay.addWidget(QLabel("→"))
        lay.addWidget(self.value)
        remove = QPushButton("Remove")
        remove.clicked.connect(lambda: on_remove(self) if on_remove else None)
        lay.addWidget(remove)
        self._placeholder()
        if value != "":
            self.value.setText(_value_text(value))

    def _placeholder(self) -> None:
        spec = self.specs.get(self.combo.currentData())
        if spec is None:
            return
        if spec.kind == "bool":
            hint = "true or false"
        elif spec.kind == "choice":
            hint = " / ".join(str(v) for _l, v in (spec.choices or []))
        elif spec.min is not None and spec.max is not None:
            hint = f"{spec.min:g} to {spec.max:g}"
        else:
            hint = f"normally {_value_text(spec.default)}"
        self.value.setPlaceholderText(hint)
        self.value.setToolTip(spec.tooltip or "")

    def pair(self):
        return self.combo.currentData(), self.value.text().strip()


class EventDialog(QDialog):
    def __init__(self, ev: dict, save: Callable[[dict], None], parent=None):
        super().__init__(parent)
        self.ev = dict(ev)
        self._save = save
        kind = ev.get("kind", se.KIND_SETTINGS)
        self.setWindowTitle(("Edit" if ev.get("name") else "New") +
                            (" Settings Event" if kind == se.KIND_SETTINGS else " Scheduled Message"))
        self.setMinimumWidth(560)
        root = QVBoxLayout(self)
        root.setSpacing(10)

        root.addWidget(QLabel("Name"))
        self.name_edit = QLineEdit(ev.get("name", ""))
        self.name_edit.setPlaceholderText("e.g. Double XP weekend" if kind == se.KIND_SETTINGS else "e.g. Discord link")
        root.addWidget(self.name_edit)
        self.enabled_check = QCheckBox("Turned on")
        self.enabled_check.setChecked(bool(ev.get("enabled", True)))
        root.addWidget(self.enabled_check)

        if kind == se.KIND_SETTINGS:
            self._build_settings(root, ev)
        else:
            self._build_message(root, ev)

        self.error = QLabel("")
        self.error.setObjectName("ErrorText")
        self.error.setWordWrap(True)
        self.error.hide()
        root.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    @staticmethod
    def _time_edit(text: str) -> QLineEdit:
        e = QLineEdit(text)
        e.setPlaceholderText("HH:MM")
        e.setFixedWidth(80)
        return e

    @staticmethod
    def _day_combo(day: int) -> QComboBox:
        c = QComboBox()
        for i, d in enumerate(DAY_FULL):
            c.addItem(d, i)
        c.setCurrentIndex(day)
        return c

    def _build_settings(self, root, ev) -> None:
        self.daily_check = QCheckBox("Every day (instead of once a week)")
        self.daily_check.setChecked(bool(ev.get("daily")))
        root.addWidget(self.daily_check)
        row = QHBoxLayout()
        self.start_day = self._day_combo(ev.get("start_day", 4))
        self.start_time = self._time_edit(ev.get("start", "18:00"))
        self.end_day = self._day_combo(ev.get("end_day", 6))
        self.end_time = self._time_edit(ev.get("end", "23:00"))
        row.addWidget(QLabel("From"))
        row.addWidget(self.start_day)
        row.addWidget(self.start_time)
        row.addWidget(QLabel("until"))
        row.addWidget(self.end_day)
        row.addWidget(self.end_time)
        row.addStretch(1)
        root.addLayout(row)
        self.daily_check.toggled.connect(self._sync_daily)
        self._sync_daily()

        root.addWidget(QLabel("Settings to change while it's on"))
        self.specs = se.event_settings()
        self.rows_box = QVBoxLayout()
        root.addLayout(self.rows_box)
        self.rows = []
        for key, value in (ev.get("changes") or {}).items():
            self._add_row(key, value)
        add = QPushButton("Add a Setting")
        add.clicked.connect(lambda: self._add_row())
        root.addWidget(add, 0, Qt.AlignLeft)
        root.addWidget(QLabel("Message of the day while it's on (optional)"))
        self.motd_edit = QLineEdit(ev.get("motd", ""))
        self.motd_edit.setPlaceholderText("e.g. Double XP until Sunday night!")
        root.addWidget(self.motd_edit)
        note = QLabel("The server restarts when the event starts and again when it ends, with in-game warnings "
                      "if people are on (see Restart Schedule). A setting you change by hand during the event "
                      "is kept when it ends.")
        note.setObjectName("Dim")
        note.setWordWrap(True)
        root.addWidget(note)
        if not self.rows:
            self._add_row("PlayerXPRateMultiplier", "2")

    def _sync_daily(self) -> None:
        daily = self.daily_check.isChecked()
        self.start_day.setVisible(not daily)
        self.end_day.setVisible(not daily)

    def _add_row(self, key: str = "", value="") -> None:
        row = _ChangeRow(self.specs, key, value, on_remove=self._remove_row)
        self.rows.append(row)
        self.rows_box.addWidget(row)

    def _remove_row(self, row) -> None:
        self.rows.remove(row)
        row.setParent(None)
        row.deleteLater()

    def _build_message(self, root, ev) -> None:
        root.addWidget(QLabel("Days"))
        days = QHBoxLayout()
        self.day_checks = []
        for i, d in enumerate(se.DAY_NAMES):
            c = QCheckBox(d)
            c.setChecked(i in ev.get("days", list(range(7))))
            self.day_checks.append(c)
            days.addWidget(c)
        days.addStretch(1)
        root.addLayout(days)
        row = QHBoxLayout()
        row.addWidget(QLabel("At"))
        self.time_edit = self._time_edit(ev.get("time", "20:00"))
        row.addWidget(self.time_edit)
        self.repeat_combo = QComboBox()
        for minutes in se.REPEAT_CHOICES:
            self.repeat_combo.addItem(REPEAT_LABELS[minutes] + ("" if not minutes else " after that, until midnight"),
                                      minutes)
        self.repeat_combo.setCurrentIndex(max(0, self.repeat_combo.findData(int(ev.get("repeat_minutes") or 0))))
        row.addWidget(self.repeat_combo)
        row.addStretch(1)
        root.addLayout(row)
        root.addWidget(QLabel("Message"))
        self.text_edit = QLineEdit(ev.get("text", ""))
        self.text_edit.setMaxLength(se.MAX_MESSAGE)
        self.text_edit.setPlaceholderText("e.g. Join our Discord: discord.gg/...")
        root.addWidget(self.text_edit)
        note = QLabel("Posted in game chat with RCON while the server is running.")
        note.setObjectName("Dim")
        root.addWidget(note)

    def values(self) -> dict:
        raw = {"id": self.ev.get("id"), "kind": self.ev.get("kind", se.KIND_SETTINGS),
               "name": self.name_edit.text(), "enabled": self.enabled_check.isChecked()}
        if raw["kind"] == se.KIND_SETTINGS:
            changes = {}
            for row in self.rows:
                key, value = row.pair()
                if key and value != "":
                    changes[key] = value
            raw.update(daily=self.daily_check.isChecked(), start_day=self.start_day.currentData(),
                       start=self.start_time.text(), end_day=self.end_day.currentData(),
                       end=self.end_time.text(), changes=changes, motd=self.motd_edit.text())
        else:
            raw.update(days=[i for i, c in enumerate(self.day_checks) if c.isChecked()], time=self.time_edit.text(),
                       repeat_minutes=self.repeat_combo.currentData(), text=self.text_edit.text())
        return raw

    def _accept(self) -> None:
        try:
            self._save(self.values())
        except ValueError as e:
            self.error.setText(str(e))
            self.error.show()
            return
        self.accept()


class EventsPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.server = None
        # Set by MainWindow: (server, raw) -> event (ValueError if it doesn't check out), (server, id) -> bool.
        self.on_save: Optional[Callable] = None
        self.on_delete: Optional[Callable] = None

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 20, 28, 24)
        root.setSpacing(12)
        intro = QLabel("Change settings on a schedule, such as double XP at weekends, or post a message in game "
                       "chat at set times. Times are this PC's time.")
        intro.setObjectName("Dim")
        intro.setWordWrap(True)
        root.addWidget(intro)
        bar = QHBoxLayout()
        self.add_settings_btn = QPushButton("New Settings Event…")
        self.add_settings_btn.setObjectName("PrimaryButton")
        self.add_settings_btn.clicked.connect(lambda: self._edit(se.new_event(se.KIND_SETTINGS)))
        self.add_message_btn = QPushButton("New Scheduled Message…")
        self.add_message_btn.clicked.connect(lambda: self._edit(se.new_event(se.KIND_MESSAGE)))
        self.edit_btn = QPushButton("Edit…")
        self.edit_btn.clicked.connect(self._edit_selected)
        self.delete_btn = QPushButton("Delete")
        self.delete_btn.clicked.connect(self._delete_selected)
        for b in (self.add_settings_btn, self.add_message_btn, self.edit_btn, self.delete_btn):
            bar.addWidget(b)
        bar.addStretch(1)
        root.addLayout(bar)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Event", "When", "What", "Status"])
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.Stretch)
        h.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._sync_buttons)
        self.table.cellDoubleClicked.connect(lambda _r, _c: self._edit_selected())
        root.addWidget(self.table, 1)
        self.empty = QFrame()
        el = QVBoxLayout(self.empty)
        msg = QLabel("No events yet. Try a settings event for a weekend of double XP, or a message that "
                     "reminds players of the rules every few hours.")
        msg.setObjectName("Dim")
        msg.setWordWrap(True)
        el.addWidget(msg)
        root.addWidget(self.empty)
        self._sync_buttons()

    def set_server(self, server) -> None:
        self.server = server
        self.refresh()

    def _events(self) -> list:
        return list(self.server.scheduled_events) if self.server else []

    def refresh(self) -> None:
        events = self._events()
        selected = self._selected_id()
        self.table.setRowCount(len(events))
        for i, ev in enumerate(events):
            for col, text in enumerate((ev.get("name", ""), se.describe_when(ev), se.describe_what(ev),
                                        status_text(ev))):
                item = QTableWidgetItem(text)
                item.setData(Qt.UserRole, ev.get("id"))
                self.table.setItem(i, col, item)
            if ev.get("id") == selected:
                self.table.selectRow(i)
        self.table.setVisible(bool(events))
        self.empty.setVisible(not events)
        self._sync_buttons()

    def _selected_id(self) -> str:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            item = self.table.currentItem()
            return item.data(Qt.UserRole) if item and self.table.selectedItems() else ""
        item = self.table.item(rows[0].row(), 0)
        return item.data(Qt.UserRole) if item else ""

    def _sync_buttons(self) -> None:
        has = bool(self._selected_id())
        self.edit_btn.setEnabled(has)
        self.delete_btn.setEnabled(has)
        full = len(self._events()) >= se.MAX_EVENTS
        self.add_settings_btn.setEnabled(self.server is not None and not full)
        self.add_message_btn.setEnabled(self.server is not None and not full)

    def _edit(self, ev: dict) -> None:
        if self.server is None or self.on_save is None:
            return
        server = self.server
        EventDialog(ev, lambda raw: self.on_save(server, raw), self).exec()
        self.refresh()

    def _edit_selected(self) -> None:
        eid = self._selected_id()
        ev = next((e for e in self._events() if e.get("id") == eid), None)
        if ev is not None:
            self._edit(ev)

    def _delete_selected(self) -> None:
        eid = self._selected_id()
        ev = next((e for e in self._events() if e.get("id") == eid), None)
        if ev is None or self.on_delete is None:
            return
        extra = " Its settings are put back now (with a restart if the server's running)." \
            if (ev.get("state") or {}).get("active") else ""
        if QMessageBox.question(self, "Delete event", f"Delete “{ev.get('name')}”?{extra}") != QMessageBox.Yes:
            return
        self.on_delete(self.server, eid)
        self.refresh()
