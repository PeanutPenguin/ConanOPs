from __future__ import annotations

from datetime import datetime

from PySide6.QtWidgets import (
    QLabel, QLineEdit, QCheckBox, QHBoxLayout, QVBoxLayout, QPushButton,
    QFrame, QComboBox,
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


def parse_minutes(text) -> "list | None":
    """ "15, 5, 1" -> [15, 5, 1] (largest first, no repeats); None if it isn't valid."""
    if isinstance(text, (list, tuple)):
        parts = [str(p) for p in text]
    else:
        parts = [p for p in str(text or "").replace(";", ",").replace(" ", ",").split(",") if p.strip()]
    try:
        values = sorted({int(p.strip()) for p in parts}, reverse=True)
    except ValueError:
        return None
    if not values or len(values) > 6 or any(v < 1 or v > 60 for v in values):
        return None
    return values


def format_minutes(value) -> str:
    vals = parse_minutes(value)
    return ", ".join(str(v) for v in vals) if vals else str(value or "")


def normalize_minutes(text: str) -> str:
    """The text as it's saved, so "1,5,15" and "15, 5, 1" count as the same."""
    vals = parse_minutes(text)
    return ", ".join(str(v) for v in vals) if vals else text.strip()


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

        note = QLabel("The restart happens once a day inside this window. What happens if people are "
                      "playing is set below.")
        note.setObjectName("Dim")
        note.setWordWrap(True)
        panel_layout.addWidget(note)
        card_layout.addWidget(self.panel)
        self.form_layout.addWidget(schedule_card)
        self.form_layout.addWidget(self._build_warning_card())

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

    def _build_warning_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("Card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(12)
        title = QLabel("When players are online")
        title.setObjectName("SectionTitle")
        lay.addWidget(title)
        intro = QLabel("Applies to the daily restart, game and mod updates, and scheduled events.")
        intro.setObjectName("Dim")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        self.players_combo = QComboBox()
        self.players_combo.addItem("Warn them in game chat, then restart", "warn")
        self.players_combo.addItem("Wait until everyone has left", "wait")
        self.players_combo.setToolTip("Warnings are sent with RCON. With RCON off, ConanOps always waits.")
        lay.addLayout(_row("If people are playing", self.players_combo))
        self.register_field("when_players_online", self.players_combo, lambda w: w.currentData(),
                            lambda w, v: w.setCurrentIndex(max(0, w.findData(v))), self.players_combo.currentIndexChanged)

        self.warning_edit = QLineEdit()
        self.warning_edit.setPlaceholderText("15, 5, 1")
        self.warning_edit.setFixedWidth(200)
        self.warning_edit.setToolTip("Minutes before the restart to post a warning, largest first, e.g. 15, 5, 1. "
                                     "The first one is how long the countdown takes.")
        lay.addLayout(_row("Warn at (minutes before)", self.warning_edit))
        self.register_field("warning_minutes", self.warning_edit, lambda w: normalize_minutes(w.text()),
                            lambda w, v: w.setText(format_minutes(v)), self.warning_edit.textChanged)
        self.warning_edit.textChanged.connect(self._check_times)
        self.players_combo.currentIndexChanged.connect(lambda _i: self._sync_warning_enabled())

        self.rcon_note = QLabel("")
        self.rcon_note.setObjectName("Dim")
        self.rcon_note.setWordWrap(True)
        lay.addWidget(self.rcon_note)
        self.warning_note = QLabel("If everyone leaves during the countdown, the restart goes ahead straight away. "
                                   "Starting, stopping or restarting the server yourself calls it off.")
        self.warning_note.setObjectName("Dim")
        self.warning_note.setWordWrap(True)
        lay.addWidget(self.warning_note)

        self.mod_update_check = QCheckBox("Update Workshop mods automatically")
        self.mod_update_check.setToolTip("ConanOps checks the Workshop every hour. When a mod you use is updated, "
                                         "it warns players, then restarts the server with the new version.")
        lay.addWidget(self.mod_update_check)
        self.register_field("mod_auto_update", self.mod_update_check, lambda w: w.isChecked(),
                            lambda w, v: w.setChecked(bool(v)), self.mod_update_check.toggled)
        mod_note = QLabel("Checks the Steam Workshop every hour. When one of this server's mods is updated, "
                          "players get the same warning, then the server restarts with the new version so "
                          "people can keep joining.")
        mod_note.setObjectName("Dim")
        mod_note.setWordWrap(True)
        lay.addWidget(mod_note)
        self.rcon_enabled = True
        self._sync_warning_enabled()
        return card

    def set_rcon_enabled(self, on: bool) -> None:
        """Warnings need RCON; shown so "warn" isn't a silent wait."""
        self.rcon_enabled = bool(on)
        self._sync_warning_enabled()

    def _sync_warning_enabled(self) -> None:
        warn = self.players_combo.currentData() == "warn"
        self.warning_edit.setEnabled(warn)
        if warn and not self.rcon_enabled:
            self.rcon_note.setText("RCON is off for this server, so ConanOps can't post warnings and will wait "
                                   "until everyone has left. Turn RCON on under Alerts & RCON.")
            self.rcon_note.show()
        else:
            self.rcon_note.hide()

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
        minutes_ok = parse_minutes(self.warning_edit.text()) is not None
        self._times_valid = start_ok and end_ok and minutes_ok
        self._sync_chart_window()
        self.time_error_label.setVisible(not self._times_valid)
        if not self._times_valid:
            bad = []
            if not start_ok:
                bad.append("start")
            if not end_ok:
                bad.append("end")
            problems = []
            if bad:
                problems.append(f"Quiet hours {' and '.join(bad)} time must be in HH:MM (24-hour) format.")
            if not minutes_ok:
                problems.append("Warn at: enter up to 6 whole minutes between 1 and 60, separated by commas, "
                                "like 15, 5, 1.")
            self.time_error_label.setText(" ".join(problems))
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
