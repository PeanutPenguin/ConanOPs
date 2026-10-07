from __future__ import annotations

from datetime import datetime
from typing import List

from PySide6.QtWidgets import (
    QLabel, QLineEdit, QCheckBox, QHBoxLayout, QVBoxLayout, QPushButton,
    QFrame,
)

from theme_config import load_theme
from ui.activity_chart import ActivityChart
from ui.base_settings_page import SettingsPageBase


def _valid_hhmm(s: str) -> bool:
    try:
        datetime.strptime(s.strip(), "%H:%M")
        return True
    except ValueError:
        return False


def _row(label_text: str, widget) -> QVBoxLayout:
    col = QVBoxLayout()
    col.setSpacing(6)
    lbl = QLabel(label_text)
    lbl.setObjectName("Muted")
    col.addWidget(lbl)
    col.addWidget(widget)
    return col


class SettingsRestartPage(SettingsPageBase):
    def __init__(self, get_hourly_activity, parent=None):
        """get_hourly_activity: callable -> 24 per-hour session counts, used to suggest a window."""
        # The window is a ConanOps scheduler setting, not an .ini value, so no restart is needed.
        super().__init__("Restart Schedule", parent, requires_restart=False)
        self.get_hourly_activity = get_hourly_activity
        self._build_form()

    def _build_form(self) -> None:
        schedule_card = QFrame()
        schedule_card.setObjectName("Card")
        card_layout = QVBoxLayout(schedule_card)
        card_layout.setContentsMargins(18, 16, 18, 16)
        card_layout.setSpacing(14)

        self.enabled_check = QCheckBox("Scheduled restarts")
        self.enabled_check.toggled.connect(self._toggle_panel)
        card_layout.addWidget(self.enabled_check)
        self.register_field("restart_enabled", self.enabled_check, lambda w: w.isChecked(), lambda w, v: w.setChecked(v), self.enabled_check.toggled)

        self.panel = QFrame()
        self.panel.setObjectName("TransparentRow")
        panel_layout = QVBoxLayout(self.panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(10)

        time_row = QHBoxLayout()
        time_row.setSpacing(12)
        self.start_edit = QLineEdit()
        self.start_edit.setPlaceholderText("HH:MM")
        self.start_edit.setFixedWidth(120)
        self.end_edit = QLineEdit()
        self.end_edit.setPlaceholderText("HH:MM")
        self.end_edit.setFixedWidth(120)
        time_row.addLayout(_row("Quiet hours start", self.start_edit))
        time_row.addLayout(_row("Quiet hours end", self.end_edit))
        use_suggestion_btn = QPushButton("Use Suggested Window")
        use_suggestion_btn.clicked.connect(self._use_suggestion)
        sugg_col = QVBoxLayout()
        sugg_col.addStretch(1)
        sugg_col.addWidget(use_suggestion_btn)
        time_row.addLayout(sugg_col)
        time_row.addStretch(1)
        panel_layout.addLayout(time_row)

        self.time_error_label = QLabel("")
        self.time_error_label.setObjectName("ErrorText")
        self.time_error_label.hide()
        panel_layout.addWidget(self.time_error_label)

        self.suggestion_label = QLabel("")
        self.suggestion_label.setObjectName("Muted")
        self.suggestion_label.setWordWrap(True)
        panel_layout.addWidget(self.suggestion_label)

        note = QLabel("If anyone's online during this window, ConanOps skips that day and retries next time.")
        note.setObjectName("Dim")
        note.setWordWrap(True)
        panel_layout.addWidget(note)
        card_layout.addWidget(self.panel)
        self.form_layout.addWidget(schedule_card)

        activity_card = QFrame()
        activity_card.setObjectName("Card")
        act = QVBoxLayout(activity_card)
        act.setContentsMargins(18, 16, 18, 16)
        act.setSpacing(12)
        head = QHBoxLayout()
        title = QLabel("Player activity")
        title.setObjectName("SectionTitle")
        head.addWidget(title)
        head.addStretch(1)
        caption = QLabel("Sessions started in each hour of the day, from this server's history")
        caption.setObjectName("Dim")
        head.addWidget(caption)
        act.addLayout(head)

        palette = load_theme()
        self.activity_chart = ActivityChart(accent=palette.accent, text=palette.dim)
        act.addWidget(self.activity_chart)

        legend = QHBoxLayout()
        legend.setSpacing(18)
        for color, text in ((palette.accent, "Restart window"), ("#4a4a4a", "Other hours")):
            item = QHBoxLayout()
            item.setSpacing(8)
            swatch = QLabel()
            swatch.setFixedSize(10, 10)
            swatch.setStyleSheet(f"background-color: {color}; border-radius: 2px;")
            item.addWidget(swatch)
            lbl = QLabel(text)
            lbl.setObjectName("Muted")
            item.addWidget(lbl)
            legend.addLayout(item)
        legend.addStretch(1)
        self.activity_summary = QLabel("")
        self.activity_summary.setObjectName("Muted")
        legend.addWidget(self.activity_summary)
        act.addLayout(legend)
        self.form_layout.addWidget(activity_card)
        self.form_layout.addStretch(1)

        self._times_valid = True
        self.register_field("restart_start", self.start_edit, lambda w: w.text(), lambda w, v: w.setText(v), self.start_edit.textChanged)
        self.register_field("restart_end", self.end_edit, lambda w: w.text(), lambda w, v: w.setText(v), self.end_edit.textChanged)
        self.start_edit.textChanged.connect(self._check_times)
        self.end_edit.textChanged.connect(self._check_times)

        self._refresh_suggestion()

    def _sync_chart_window(self) -> None:
        def hour(text: str):
            return datetime.strptime(text.strip(), "%H:%M").hour if _valid_hhmm(text) else None
        on = self.enabled_check.isChecked()
        self.activity_chart.set_window(hour(self.start_edit.text()) if on else None,
                                       hour(self.end_edit.text()) if on else None)

    def can_apply(self) -> bool:
        # The scheduler would silently read a bad time as 00:00, so block Apply.
        return self._times_valid

    def _check_times(self) -> None:
        start_ok = _valid_hhmm(self.start_edit.text())
        end_ok = _valid_hhmm(self.end_edit.text())
        self._times_valid = start_ok and end_ok
        self._sync_chart_window()
        self.time_error_label.setVisible(not self._times_valid)
        if not self._times_valid:
            bad = []
            if not start_ok:
                bad.append("start")
            if not end_ok:
                bad.append("end")
            self.time_error_label.setText(f"Quiet hours {' and '.join(bad)} time must be in HH:MM (24-hour) format.")
        # Re-check explicitly: signal connection order would leave Apply one keystroke stale.
        self._update_pending_ui()

    def _toggle_panel(self, on: bool) -> None:
        self.panel.setEnabled(on)
        self._sync_chart_window()

    def load_committed(self, values: dict) -> None:
        super().load_committed(values)
        # Runs on server switch, so refresh from the new server's history.
        self._refresh_suggestion()
        self._check_times()

    def _refresh_suggestion(self) -> None:
        hours = self.get_hourly_activity()
        self.activity_chart.set_values(hours or [])
        self.activity_summary.setText(self.activity_chart.summary() if self.activity_chart.has_data() else "")
        if not hours or sum(hours) == 0:
            self.suggestion_label.setText("Not enough session history yet to suggest a window.")
            self._suggested = None
            return
        # Find the quietest contiguous 2-hour block.
        best_start, best_sum = 0, None
        for h in range(24):
            s = hours[h] + hours[(h + 1) % 24]
            if best_sum is None or s < best_sum:
                best_sum, best_start = s, h
        end = (best_start + 2) % 24
        self._suggested = (f"{best_start:02d}:00", f"{end:02d}:00")
        self.suggestion_label.setText(
            f"Quietest window from your history: {self._suggested[0]} – {self._suggested[1]}"
        )

    def _use_suggestion(self) -> None:
        if getattr(self, "_suggested", None):
            self.start_edit.setText(self._suggested[0])
            self.end_edit.setText(self._suggested[1])

    def on_apply(self, values: dict) -> None:
        pass
