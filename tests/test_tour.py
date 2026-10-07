from __future__ import annotations

import sys

import pytest
from PySide6.QtWidgets import QApplication

import models


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


@pytest.fixture
def win(tmp_path):
    from ui.main_window import MainWindow
    cfg = models.AppConfig()
    s = cfg.add_server("Tour")
    s.install_dir = str(tmp_path / "srv")
    w = MainWindow(config=cfg)
    w.resize(1200, 800)
    w.show()
    QApplication.processEvents()
    yield w
    w._really_quit = True
    w.close()


def test_tour_walks_every_step_then_marks_done(win):
    win.start_tour()
    tour = win._tour
    assert tour is not None and tour.isVisible()
    seen = []
    for _ in range(20):
        if win._tour is None:
            break
        seen.append(tour.title_label.text())
        tour.next_btn.click()
        QApplication.processEvents()
    assert win._tour is None and win.config.tour_done is True
    assert seen[0] == "Welcome to ConanOps" and "App Settings" in seen


def test_spotlight_covers_the_start_button_and_card_stays_on_screen(win):
    win.start_tour()
    tour = win._tour
    tour.next_btn.click()  # -> start/stop step
    QApplication.processEvents()
    assert tour.title_label.text() == "Start, stop and restart"
    assert not tour._hole.isNull()
    assert tour.rect().contains(tour.card.geometry())
    tour.skip_btn.click()
    assert win._tour is None and win.config.tour_done


def test_steps_without_visible_targets_are_skipped(win):
    from ui.tour import TourOverlay, TourStep
    from PySide6.QtWidgets import QWidget
    hidden = QWidget(win)
    hidden.hide()
    steps = [TourStep("A", "a", lambda: []), TourStep("B", "b", lambda: [hidden]), TourStep("C", "c", lambda: [])]
    t = TourOverlay(win, steps)
    t.start()
    t.next_btn.click()
    assert t.title_label.text() == "C"
