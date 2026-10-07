"""
First-run tour: a short walkthrough of the dashboard, shown once after
the first server is set up (and on demand from App Settings).

An overlay dims the window, cuts a highlighted "spotlight" around the
part being explained, and shows a small card next to it with Back /
Next / Skip. Steps whose target isn't on screen are skipped, so the
tour never points at nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional

from PySide6.QtCore import QEvent, QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget


@dataclass
class TourStep:
    title: str
    text: str
    # Returns the widgets to highlight (their combined area), or [] for
    # a centered card with no spotlight.
    targets: Callable[[], List[QWidget]]
    before: Optional[Callable[[], None]] = None  # e.g. switch to the page being shown


def _visible(widgets) -> List[QWidget]:
    return [w for w in widgets if w is not None and w.isVisible() and w.width() > 0]


class TourOverlay(QWidget):
    finished = Signal(bool)  # True = went through to the end, False = skipped

    _PAD = 8
    _GAP = 14
    _CARD_WIDTH = 360

    def __init__(self, window: QWidget, steps: List[TourStep], accent: str = "#c9752f"):
        super().__init__(window)
        self._window = window
        self._steps = steps
        self._accent = QColor(accent)
        self._index = -1
        self._hole = QRect()
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.setFocusPolicy(Qt.StrongFocus)

        self.card = QFrame(self)
        self.card.setObjectName("Card")
        self.card.setAutoFillBackground(True)
        self.card.setFixedWidth(self._CARD_WIDTH)
        lay = QVBoxLayout(self.card)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(8)
        self.step_label = QLabel("")
        self.step_label.setObjectName("Dim")
        self.title_label = QLabel("")
        self.title_label.setObjectName("SectionTitle")
        self.title_label.setWordWrap(True)
        self.text_label = QLabel("")
        self.text_label.setWordWrap(True)
        lay.addWidget(self.step_label)
        lay.addWidget(self.title_label)
        lay.addWidget(self.text_label)
        row = QHBoxLayout()
        self.skip_btn = QPushButton("Skip tour")
        self.back_btn = QPushButton("Back")
        self.next_btn = QPushButton("Next")
        self.next_btn.setProperty("primary", True)
        self.skip_btn.clicked.connect(lambda: self._end(False))
        self.back_btn.clicked.connect(lambda: self._go(-1))
        self.next_btn.clicked.connect(lambda: self._go(+1))
        row.addWidget(self.skip_btn)
        row.addStretch(1)
        row.addWidget(self.back_btn)
        row.addWidget(self.next_btn)
        lay.addLayout(row)

        window.installEventFilter(self)

    # ------------------------------------------------------------- flow --
    def start(self) -> None:
        self.setGeometry(self._window.rect())
        self.show()
        self.raise_()
        self.setFocus()
        self._index = -1
        self._go(+1)

    def _go(self, direction: int) -> None:
        i = self._index + direction
        while 0 <= i < len(self._steps):
            step = self._steps[i]
            if step.before:
                step.before()
            targets = _visible(step.targets())
            if targets or not step.targets():
                self._index = i
                self._show_step(step, targets)
                return
            i += direction  # nothing to point at for this one -- skip it
        if direction > 0:
            self._end(True)

    def _shown_numbers(self) -> tuple:
        return self._index + 1, len(self._steps)

    def _show_step(self, step: TourStep, targets: List[QWidget]) -> None:
        n, total = self._shown_numbers()
        self.step_label.setText(f"{n} of {total}")
        self.title_label.setText(step.title)
        self.text_label.setText(step.text)
        self.back_btn.setEnabled(self._index > 0)
        self.next_btn.setText("Done" if self._index == len(self._steps) - 1 else "Next")
        for b in (self.next_btn,):
            b.style().unpolish(b)
            b.style().polish(b)
        self._targets = targets
        self._layout_step()
        self.next_btn.setFocus()

    def _layout_step(self) -> None:
        self.setGeometry(self._window.rect())
        targets = getattr(self, "_targets", [])
        if targets:
            rect = QRect()
            for w in targets:
                top_left = w.mapTo(self._window, QPoint(0, 0))
                rect = rect.united(QRect(top_left, w.size()))
            self._hole = rect.adjusted(-self._PAD, -self._PAD, self._PAD, self._PAD).intersected(self.rect())
        else:
            self._hole = QRect()
        self.card.adjustSize()
        self.card.move(self._card_position())
        self.card.raise_()
        self.update()

    def _card_position(self) -> QPoint:
        area = self.rect()
        cw, ch = self.card.width(), self.card.height()
        if self._hole.isNull():
            return QPoint(area.center().x() - cw // 2, area.center().y() - ch // 2)
        h = self._hole

        def clamp_x(x):
            return max(8, min(x, area.width() - cw - 8))

        def clamp_y(y):
            return max(8, min(y, area.height() - ch - 8))

        candidates = [
            QPoint(clamp_x(h.left()), h.bottom() + self._GAP),        # below
            QPoint(clamp_x(h.left()), h.top() - self._GAP - ch),      # above
            QPoint(h.right() + self._GAP, clamp_y(h.top())),          # right
            QPoint(h.left() - self._GAP - cw, clamp_y(h.top())),      # left
        ]
        for p in candidates:
            if area.contains(QRect(p, self.card.size())):
                return p
        # Nothing fits cleanly (huge target): clamp the first choice on screen.
        p = candidates[0]
        x = max(8, min(p.x(), area.width() - cw - 8))
        y = max(8, min(p.y(), area.height() - ch - 8))
        return QPoint(x, y)

    def _end(self, completed: bool) -> None:
        self._window.removeEventFilter(self)
        self.hide()
        self.finished.emit(completed)
        self.deleteLater()

    # ------------------------------------------------------------ events --
    def eventFilter(self, obj, event) -> bool:  # noqa: N802 -- Qt's naming
        if obj is self._window and event.type() in (QEvent.Resize, QEvent.Show):
            if self.isVisible():
                self._layout_step()
        return False

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self._end(False)
        elif event.key() in (Qt.Key_Right, Qt.Key_Return, Qt.Key_Enter):
            self._go(+1)
        elif event.key() == Qt.Key_Left and self._index > 0:
            self._go(-1)
        else:
            super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        event.accept()  # clicks outside the card don't reach the app underneath

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        shade = QPainterPath()
        shade.setFillRule(Qt.OddEvenFill)
        shade.addRect(self.rect())
        if not self._hole.isNull():
            shade.addRoundedRect(self._hole, 10, 10)
        p.fillPath(shade, QColor(0, 0, 0, 165))
        if not self._hole.isNull():
            pen = QPen(self._accent)
            pen.setWidth(2)
            p.setPen(pen)
            p.drawRoundedRect(self._hole, 10, 10)


def dashboard_steps(main_window) -> List[TourStep]:
    """The tour's content, pointing at MainWindow's real widgets."""
    dash = main_window.dashboard_page
    side = main_window.sidebar

    def nav(key):
        return [b for b in side._nav_group.buttons() if b.property("nav_key") == key]

    def show_dashboard():
        side.set_active_nav("dashboard")
        main_window._on_nav_selected("dashboard") if hasattr(main_window, "_on_nav_selected") else None

    return [
        TourStep("Welcome to ConanOps",
                 "Here's a one-minute look around. You can skip this, or see it again any time from App Settings.",
                 lambda: [], before=show_dashboard),
        TourStep("Start, stop and restart",
                 "Your server's controls. Stopping or restarting always saves the world first. The address next to "
                 "them is what friends type into Conan Exiles to join.",
                 lambda: [dash.connect_label, dash.start_btn, dash.stop_btn, dash.restart_btn]),
        TourStep("How it's doing",
                 "CPU, memory, server FPS and who's online, updated live while it runs.",
                 lambda: [dash.cpu_card, dash.mem_card, dash.fps_card, dash.players_card]),
        TourStep("It runs itself",
                 "Restarts when nobody's online, backups, crash recovery and updates all happen on their own. "
                 "Click Change on any of these to adjust it.",
                 lambda: list(dash.automation_tiles.values())),
        TourStep("Players and mods",
                 "See who's played and for how long, kick or ban, and add Steam Workshop mods. If a mod ever breaks "
                 "the server, ConanOps finds it for you.",
                 lambda: nav("players") + nav("mods")),
        TourStep("When something's wrong",
                 "Server Settings has Diagnostics, which explains in plain English why friends can't connect and "
                 "fixes most of it with a click.",
                 lambda: nav("settings")),
        TourStep("App Settings",
                 "ConanOps' own options: starting with Windows, Windows Update restart times, alerts to your phone "
                 "through Discord, and ConanOps updates. That's it -- have fun!",
                 lambda: nav("app")),
    ]
