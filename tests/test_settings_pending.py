from __future__ import annotations

import sys

import pytest

pyside6 = pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

from ini_field_specs import FieldSpec
from ui.generic_settings_page import GenericSettingsPage
from ui.settings_container import SettingsContainer


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _make_page(title, key):
    specs = [FieldSpec(key, key, "float", 1.0, "", min=0.0, max=10.0, step=0.1, decimals=1)]
    page = GenericSettingsPage(title, specs)
    page.on_apply = lambda values: None
    page.load_committed({key: 1.0})
    return page


def _edit(page, key, new_value):
    slider, scale = page._sliders[key]
    slider.setValue(int(round(new_value * scale)))


def test_editing_one_tab_shows_pending_on_every_tab():
    page_a = _make_page("A", "KeyA")
    page_b = _make_page("B", "KeyB")
    container = SettingsContainer([("a", "A", page_a), ("b", "B", page_b)])

    assert page_a.pending_label.text() == ""
    assert page_b.pending_label.text() == ""

    _edit(page_a, "KeyA", 5.0)

    assert page_a.pending_label.text() == "1 pending change"
    # The clean tab must show the SAME total, not "0" -- this is the
    # exact bug being guarded against: switching to an unrelated tab
    # should never make a pending edit elsewhere look like it vanished.
    assert page_b.pending_label.text() == "1 pending change"


def test_total_accumulates_across_multiple_tabs():
    page_a = _make_page("A", "KeyA")
    page_b = _make_page("B", "KeyB")
    container = SettingsContainer([("a", "A", page_a), ("b", "B", page_b)])

    _edit(page_a, "KeyA", 5.0)
    _edit(page_b, "KeyB", 3.0)

    assert page_a.pending_label.text() == "2 pending changes"
    assert page_b.pending_label.text() == "2 pending changes"


def test_applying_one_tab_applies_every_tabs_pending_changes():
    """Clicking Apply on ANY tab now applies every tab's pending
    changes at once, not just the one you clicked -- that's the whole
    point of the cross-tab queue: you shouldn't have to visit each
    tab separately to actually save what you changed on it."""
    page_a = _make_page("A", "KeyA")
    page_b = _make_page("B", "KeyB")
    container = SettingsContainer([("a", "A", page_a), ("b", "B", page_b)])

    _edit(page_a, "KeyA", 5.0)
    _edit(page_b, "KeyB", 3.0)
    assert page_a.pending_label.text() == "2 pending changes"

    page_a._on_apply_clicked()

    assert page_a.pending_label.text() == ""
    assert page_b.pending_label.text() == ""
    assert page_a.dirty_count() == 0
    assert page_b.dirty_count() == 0


def test_apply_skips_a_page_that_vetoes_its_own_apply():
    """A page that refuses to apply (can_apply() False, e.g. Network's
    port-conflict guard) must not block the REST from being applied --
    its own pending change just stays pending."""
    page_a = _make_page("A", "KeyA")
    page_b = _make_page("B", "KeyB")
    page_b.can_apply = lambda: False
    container = SettingsContainer([("a", "A", page_a), ("b", "B", page_b)])

    _edit(page_a, "KeyA", 5.0)
    _edit(page_b, "KeyB", 3.0)

    container.apply_all()

    assert page_a.dirty_count() == 0  # applied
    assert page_b.dirty_count() == 1  # skipped, still pending


def test_discarding_one_tab_reduces_total_for_all():
    page_a = _make_page("A", "KeyA")
    page_b = _make_page("B", "KeyB")
    container = SettingsContainer([("a", "A", page_a), ("b", "B", page_b)])

    _edit(page_a, "KeyA", 5.0)
    _edit(page_b, "KeyB", 3.0)

    page_b.discard()

    assert page_a.pending_label.text() == "1 pending change"
    assert page_b.pending_label.text() == "1 pending change"


def test_discard_stays_scoped_to_own_tab_but_apply_reflects_the_whole_queue():
    """Discard only ever acts on this page's own fields. Apply is
    different now: it applies EVERY tab's pending changes (see
    test_applying_one_tab_applies_every_tabs_pending_changes), so its
    enabled state reflects the cross-tab total, not just this page's
    own edits -- otherwise you couldn't use page B's Apply button to
    flush a change you made on page A."""
    page_a = _make_page("A", "KeyA")
    page_b = _make_page("B", "KeyB")
    container = SettingsContainer([("a", "A", page_a), ("b", "B", page_b)])

    _edit(page_a, "KeyA", 5.0)
    # Page B has no edits of its own -- Discard stays disabled, but
    # Apply is enabled since there's something pending overall.
    assert page_b.apply_btn.isEnabled() is True
    assert page_b.discard_btn.isEnabled() is False
    assert page_a.apply_btn.isEnabled() is True
    assert page_a.discard_btn.isEnabled() is True


def test_zero_pending_shows_empty_label():
    page_a = _make_page("A", "KeyA")
    page_b = _make_page("B", "KeyB")
    container = SettingsContainer([("a", "A", page_a), ("b", "B", page_b)])

    assert page_a.pending_label.text() == ""
    assert page_b.pending_label.text() == ""
