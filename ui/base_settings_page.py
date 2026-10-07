"""
Shared base for Settings sub-pages: header with a pending-changes badge and
Apply/Discard, plus dirty tracking. Subclasses write the real files in
on_apply(). A container may show the cross-tab pending total and make Apply
apply every page; Discard only affects this page.
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
    dirty_changed = Signal()  # this page's own dirty count changed

    def __init__(self, page_title: str, parent=None, requires_restart: bool = True, card_form: bool = False):
        super().__init__(parent)
        self._fields: Dict[str, Any] = {}          # name -> widget
        self._getters: Dict[str, Callable] = {}     # name -> widget -> value
        self._setters: Dict[str, Callable] = {}     # name -> (widget, value) -> None
        self._committed: Dict[str, Any] = {}
        self._total_pending = 0  # what the badge shows
        # Set by a container so Apply applies every page at once.
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
        # The server reads its .ini files only at startup, so most pages
        # apply on the next server restart. requires_restart=False is for
        # ConanOps-only settings, and for mixed pages that note the restart
        # inline on the affected fields.
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
            # Wrap the whole form in one card; the outer stretch keeps the
            # card only as tall as its contents.
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
        """This page's own pending-edit count (the badge may show more)."""
        return self._dirty_count()

    def set_total_pending(self, total: int) -> None:
        """Called by a container so the badge shows the cross-tab total."""
        self._total_pending = total
        self._refresh_pending_label()
        self._refresh_apply_enabled()

    def _refresh_pending_label(self) -> None:
        n = self._total_pending
        self.pending_label.setText(
            "" if n == 0 else f"{n} pending change" + ("" if n == 1 else "s")
        )

    def _refresh_apply_enabled(self) -> None:
        # Enabled if any tab has changes, since Apply may apply them all.
        self.apply_btn.setEnabled(self._total_pending > 0 and self.can_apply())

    def _update_pending_ui(self) -> None:
        own = self._dirty_count()
        self.discard_btn.setEnabled(own > 0)
        # Default to this page's count; a container overrides it right away
        # via dirty_changed (same call stack, so no flicker).
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
        """Set by a container so this page's Apply applies every page."""
        self._apply_all_hook = fn

    def _on_apply_clicked(self) -> None:
        if self._apply_all_hook:
            self._apply_all_hook()
        else:
            self.apply_own()

    def apply_own(self) -> None:
        """Apply and commit this page's own pending changes."""
        if not self.can_apply():
            return
        values = self._current_values()
        self.on_apply(values)
        self._committed = values
        self._update_pending_ui()
        self.applied.emit()

    def can_apply(self) -> bool:
        """Override to block Apply, e.g. on a known port conflict."""
        return True

    def on_apply(self, values: Dict[str, Any]) -> None:
        """Override to persist `values` to the ServerConfig and .ini files."""
        raise NotImplementedError
