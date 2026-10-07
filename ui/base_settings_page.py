"""
Shared plumbing for every Settings sub-page: a header with a pending-
changes badge and Apply/Discard buttons, plus the dirty-tracking that
decides when those buttons are enabled. Nothing here writes to the real
.ini files -- that's each page's on_apply() -- this just manages the
draft-vs-committed UI state.

The pending-changes badge shows the TOTAL pending count across every
settings sub-tab, not just this page's own -- a container that holds
several of these pages (SettingsContainer) calls set_total_pending()
on all of them whenever any one page's dirty_changed signal fires, so
switching to a tab with no edits of its own doesn't make it look like
a change you made on another tab was lost. Apply/Discard themselves
still only ever act on this page's own fields -- only the displayed
number is cross-tab.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QFrame,
)


class SettingsPageBase(QWidget):
    applied = Signal()
    dirty_changed = Signal()  # this page's OWN dirty count changed

    def __init__(self, page_title: str, parent=None, requires_restart: bool = True, card_form: bool = False):
        super().__init__(parent)
        self._fields: Dict[str, Any] = {}          # name -> widget
        self._getters: Dict[str, Callable] = {}     # name -> widget -> value
        self._setters: Dict[str, Callable] = {}     # name -> (widget, value) -> None
        self._committed: Dict[str, Any] = {}
        self._total_pending = 0  # what the badge shows; defaults to own count, see set_total_pending
        # If a container holding multiple settings pages sets this (see
        # set_apply_all_hook), clicking Apply on ANY page applies every
        # page's pending changes at once instead of just this one's --
        # otherwise (a page used standalone) Apply just applies its own.
        self._apply_all_hook: Optional[Callable[[], None]] = None
        self.page_title = page_title

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QHBoxLayout()
        header.setContentsMargins(24, 20, 24, 12)
        self.title_label = QLabel(page_title)
        self.title_label.setObjectName("PageTitle")
        header.addWidget(self.title_label)
        header.addSpacing(16)

        self.pending_label = QLabel("")
        self.pending_label.setObjectName("Muted")
        header.addWidget(self.pending_label)
        header.addStretch(1)

        self.discard_btn = QPushButton("Discard")
        self.discard_btn.clicked.connect(self.discard)
        # Most settings pages write into the Conan Exiles server's own
        # .ini files, which it only reads at startup -- Apply saves the
        # change right away, but it won't actually take effect on a
        # currently-running server until the NEXT time it restarts
        # (scheduled, watchdog, or manual). "Restart" specifically
        # means the SERVER process, not Windows or the PC -- a previous
        # wording ("Apply on Next Reset") used "Reset" for this, which
        # reads as ambiguous next to ConanOps' own PC-restart features.
        #
        # requires_restart=False is for pages that DON'T touch the
        # server's .ini files at all -- Backups and Restart Schedule
        # are pure ConanOps-side scheduler settings, read live by
        # ConanOps' own code, so labeling them with a restart caveat
        # that doesn't apply to them would be actively misleading, not
        # just imprecise. A page with genuinely MIXED fields (some
        # restart-gated, some not -- see SettingsAlertsPage) also
        # passes False here rather than overclaiming for its immediate
        # fields, and instead notes the restart requirement inline,
        # scoped to just the fields it actually applies to.
        self.apply_btn = QPushButton("Apply on Next Restart" if requires_restart else "Apply")
        self.apply_btn.setObjectName("PrimaryButton")
        self.apply_btn.clicked.connect(self._on_apply_clicked)
        header.addWidget(self.discard_btn)
        header.addSpacing(8)
        header.addWidget(self.apply_btn)
        root.addLayout(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        self.form_container = QWidget()
        self.form_layout = QVBoxLayout(self.form_container)
        self.form_layout.setContentsMargins(24, 8, 24, 24)
        self.form_layout.setSpacing(18)
        if card_form:
            # The whole form sits in one card (the design's tile look).
            # Subclasses keep adding to self.form_layout as before -- it
            # just lives inside the card now; the stretch outside the
            # card keeps the card only as tall as its contents.
            outer = self.form_layout
            self.form_card = QFrame()
            self.form_card.setObjectName("Card")
            outer.addWidget(self.form_card)
            outer.addStretch(1)
            self.form_layout = QVBoxLayout(self.form_card)
            self.form_layout.setContentsMargins(20, 18, 20, 20)
            self.form_layout.setSpacing(16)
        scroll.setWidget(self.form_container)
        root.addWidget(scroll, 1)

        self._update_pending_ui()

    # ------------------------------------------------------------ fields --
    def register_field(self, name: str, widget, getter: Callable, setter: Callable, change_signal) -> None:
        self._fields[name] = widget
        self._getters[name] = getter
        self._setters[name] = setter
        change_signal.connect(lambda *_: self._on_field_changed())

    def _on_field_changed(self) -> None:
        self._update_pending_ui()

    def _current_values(self) -> Dict[str, Any]:
        return {name: self._getters[name](self._fields[name]) for name in self._fields}

    def _dirty_count(self) -> int:
        current = self._current_values()
        return sum(1 for k in current if current.get(k) != self._committed.get(k))

    def dirty_count(self) -> int:
        """This page's own pending-edit count, independent of whatever
        the badge is currently displaying -- used by a container to
        compute the cross-tab total."""
        return self._dirty_count()

    def set_total_pending(self, total: int) -> None:
        """Called by a container holding multiple settings pages (e.g.
        SettingsContainer) to make the badge show the sum across every
        tab rather than just this page's own count. A page used on its
        own, with no container calling this, just shows its own count
        (set in _update_pending_ui below) as before."""
        self._total_pending = total
        self._refresh_pending_label()
        self._refresh_apply_enabled()

    def _refresh_pending_label(self) -> None:
        n = self._total_pending
        self.pending_label.setText(
            "" if n == 0 else f"{n} pending change" + ("" if n == 1 else "s")
        )

    def _refresh_apply_enabled(self) -> None:
        # Apply reflects whether there's anything to apply ACROSS ALL
        # tabs (via _apply_all_hook, see the constructor comment), not
        # just this page's own edits -- otherwise this page's own
        # button would stay disabled while a different tab still had
        # an unsaved change, even though clicking it would apply that
        # change too.
        self.apply_btn.setEnabled(self._total_pending > 0 and self.can_apply())

    def _update_pending_ui(self) -> None:
        own = self._dirty_count()
        self.discard_btn.setEnabled(own > 0)
        # Default the badge to this page's own count; a connected
        # container overrides it with the cross-tab total immediately
        # after, via the dirty_changed signal below (same call stack,
        # so there's no visible flash of the wrong number). Also
        # refreshes Apply's enabled state at each step, since a
        # standalone page (no container) never gets set_total_pending()
        # called on it at all.
        self._total_pending = own
        self._refresh_pending_label()
        self._refresh_apply_enabled()
        self.dirty_changed.emit()

    # -------------------------------------------------------- lifecycle --
    def load_committed(self, values: Dict[str, Any]) -> None:
        """Call after registering fields, with the server's saved values."""
        self._committed = dict(values)
        for name, value in values.items():
            if name in self._setters:
                self._setters[name](self._fields[name], value)
        self._update_pending_ui()

    def discard(self) -> None:
        for name, value in self._committed.items():
            if name in self._setters:
                self._setters[name](self._fields[name], value)
        self._update_pending_ui()

    def set_apply_all_hook(self, fn: Optional[Callable[[], None]]) -> None:
        """Called by a container holding multiple settings pages (e.g.
        SettingsContainer) so this page's own Apply button applies
        every page's pending changes at once instead of just this
        one's -- see the constructor comment on _apply_all_hook."""
        self._apply_all_hook = fn

    def _on_apply_clicked(self) -> None:
        if self._apply_all_hook:
            self._apply_all_hook()
        else:
            self.apply_own()

    def apply_own(self) -> None:
        """Applies and commits THIS page's own pending changes only.
        A container's apply-all hook calls this on each page in turn;
        a standalone page (no hook set) reaches it via its own Apply
        button through _on_apply_clicked()."""
        if not self.can_apply():
            return  # shouldn't normally be reachable (button is disabled), but guard against it directly too
        values = self._current_values()
        self.on_apply(values)
        self._committed = values
        self._update_pending_ui()
        self.applied.emit()

    def can_apply(self) -> bool:
        """Subclasses can override to veto Apply even when there are
        pending changes -- e.g. SettingsNetworkPage refuses to apply a
        port configuration it's already flagged as conflicting, since
        the conflict warning shown on that page used to be purely
        informational and Apply would go through anyway."""
        return True

    def on_apply(self, values: Dict[str, Any]) -> None:
        """Subclasses override to actually persist `values` (write to the
        ServerConfig object + real .ini files)."""
        raise NotImplementedError
