"""
Server Settings: a search box over every setting, a short list of sections,
and one save bar for all of them.

Sections come either as (key, label, heading, description, widget) -- the
app's layout from settings_layout.py -- or as plain (key, label, widget).
Every settings page keeps its own values and its own save function; this
only arranges them, finds settings, and saves or discards them all at once.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QPushButton, QStackedWidget,
    QButtonGroup, QScrollArea, QFrame, QLabel, QLineEdit,
)

import settings_layout
from ui.more_options import MoreOptions, open_folds_around

# Sub-nav group headings for plain (key, label, widget) entries.
GROUP_HEADINGS = {"diagnostics": "Server", "progression": "Gameplay", "backups": "Automation"}


def _norm(text: str) -> str:
    return " ".join((text or "").lower().replace("&&", "&").split())


class SettingsContainer(QWidget):
    def __init__(self, entries: List[Tuple], parent=None, pages: Optional[List[Tuple[str, QWidget]]] = None,
                 extras: Optional[List[Tuple[str, str, str, QWidget]]] = None):
        super().__init__(parent)
        sections = []
        for e in entries:
            if len(e) == 5:
                sections.append(e)
            else:
                key, label, widget = e
                sections.append((key, label, GROUP_HEADINGS.get(key, ""), "", widget))
        # Settings pages tracked for saving: given, or the section widgets themselves.
        page_list = pages if pages is not None else [(k, w) for k, _l, _h, _d, w in sections]
        self.pages: Dict[str, QWidget] = dict(page_list)
        self._extras = list(extras or [])
        self._index: Optional[list] = None
        self._section_labels = {k: label for k, label, _h, _d, _w in sections}

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 12, 24, 16)
        root.setSpacing(12)

        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("SettingsSearch")
        self.search_edit.setPlaceholderText("Search every setting, e.g. \"xp\", \"port\", \"decay\" or \"backup\"")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setAccessibleName("Search settings")
        self.search_edit.textChanged.connect(self._on_search)
        root.addWidget(self.search_edit)

        self.save_bar = QFrame()
        self.save_bar.setObjectName("SaveBar")
        bar = QHBoxLayout(self.save_bar)
        bar.setContentsMargins(16, 8, 10, 8)
        self.save_label = QLabel("")
        self.save_label.setWordWrap(True)
        bar.addWidget(self.save_label, 1)
        self.discard_btn = QPushButton("Discard")
        self.discard_btn.clicked.connect(self.discard_all)
        bar.addWidget(self.discard_btn)
        self.save_btn = QPushButton("Save")
        self.save_btn.setObjectName("PrimaryButton")
        self.save_btn.clicked.connect(self.apply_all)
        bar.addWidget(self.save_btn)
        self.save_bar.hide()
        root.addWidget(self.save_bar)

        body = QHBoxLayout()
        body.setSpacing(24)
        root.addLayout(body, 1)

        subnav_scroll = QScrollArea()
        subnav_scroll.setWidgetResizable(True)
        subnav_scroll.setFrameShape(QFrame.NoFrame)
        subnav_scroll.setFixedWidth(210)
        subnav_container = QWidget()
        subnav = QVBoxLayout(subnav_container)
        subnav.setSpacing(2)
        subnav.setContentsMargins(0, 0, 0, 0)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self.stack = QStackedWidget()
        self.sections: Dict[str, QWidget] = {}
        self._section_widgets: Dict[str, QWidget] = {}
        last_heading = None
        for key, label, heading, blurb, widget in sections:
            if heading and heading != last_heading:
                h = QLabel(heading)
                h.setObjectName("SectionLabel")
                subnav.addWidget(h)
            last_heading = heading or last_heading
            # Qt treats a single & as a shortcut marker.
            btn = QPushButton(label.replace("&", "&&"))
            btn.setObjectName("NavButton")
            btn.setCheckable(True)
            btn.setProperty("sub_key", key)
            btn.clicked.connect(lambda _=False, k=key: self._select(k))
            self._group.addButton(btn)
            subnav.addWidget(btn)
            holder = self._wrap(label, blurb, widget) if len(entries[0]) == 5 else widget
            self.stack.addWidget(holder)
            self.sections[key] = holder
            self._section_widgets[key] = widget
        subnav.addStretch(1)
        subnav_scroll.setWidget(subnav_container)
        body.addWidget(subnav_scroll)
        body.addWidget(self.stack, 1)

        self._results = QWidget()
        rl = QVBoxLayout(self._results)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(8)
        self._results_title = QLabel("")
        self._results_title.setObjectName("SectionTitle")
        rl.addWidget(self._results_title)
        rscroll = QScrollArea()
        rscroll.setWidgetResizable(True)
        rscroll.setFrameShape(QFrame.NoFrame)
        self._results_card = QFrame()
        self._results_card.setObjectName("Card")
        self._results_layout = QVBoxLayout(self._results_card)
        self._results_layout.setContentsMargins(0, 4, 0, 4)
        self._results_layout.setSpacing(0)
        inner = QWidget()
        il = QVBoxLayout(inner)
        il.setContentsMargins(0, 0, 8, 0)
        il.addWidget(self._results_card)
        il.addStretch(1)
        rscroll.setWidget(inner)
        rl.addWidget(rscroll, 1)
        self.stack.addWidget(self._results)
        self._current = sections[0][0] if sections else ""

        for p in self.pages.values():
            # One save bar for every page: the pages' own Apply/Discard stay
            # hidden, and Apply on any of them saves them all.
            if hasattr(p, "dirty_changed"):
                p.dirty_changed.connect(self._recompute_total_pending)
            if hasattr(p, "set_apply_all_hook"):
                p.set_apply_all_hook(self.apply_all)
            if hasattr(p, "header_widget") and len(entries[0]) == 5:
                p.header_widget.hide()

        if sections:
            self._select(sections[0][0])

    def _wrap(self, label: str, blurb: str, widget: QWidget) -> QWidget:
        holder = QWidget()
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        title = QLabel(label)
        title.setObjectName("SectionTitle")
        lay.addWidget(title)
        if blurb:
            note = QLabel(blurb)
            note.setObjectName("Dim")
            note.setWordWrap(True)
            lay.addWidget(note)
        lay.addSpacing(6)
        lay.addWidget(widget, 1)
        # Pages built for a full window carry their own outer margins and title.
        for attr in ("title_label", "page_title_label"):
            lbl = getattr(widget, attr, None)
            if isinstance(lbl, QLabel):
                lbl.hide()
        if getattr(widget, "embedded", None) is False:  # a SettingsPageBase page
            if getattr(widget, "form_card", None) is not None:
                widget.form_container.layout().setContentsMargins(0, 0, 8, 16)
            else:
                widget.form_layout.setContentsMargins(0, 0, 8, 16)
        elif widget.layout() is not None and not hasattr(widget, "embedded"):
            widget.layout().setContentsMargins(0, 0, 0, 0)
        return holder

    # ------------------------------------------------------------ saving --
    def _pending_pages(self) -> list:
        return [p for p in self.pages.values() if hasattr(p, "dirty_count") and p.dirty_count() > 0]

    def _recompute_total_pending(self) -> None:
        total = sum(p.dirty_count() for p in self.pages.values() if hasattr(p, "dirty_count"))
        for p in self.pages.values():
            if hasattr(p, "set_total_pending"):
                p.set_total_pending(total)
        blocked = [p for p in self._pending_pages() if hasattr(p, "can_apply") and not p.can_apply()]
        self.save_bar.setVisible(total > 0)
        if blocked:
            where = ", ".join(dict.fromkeys(self._section_of_page(p) for p in blocked))
            self.save_label.setText(f"{total} unsaved change{'' if total == 1 else 's'} -- fix the problem "
                                    f"shown on {where} first.")
        else:
            self.save_label.setText(f"{total} unsaved change{'' if total == 1 else 's'}. Game settings take "
                                    f"effect the next time the server restarts.")
        self.save_btn.setEnabled(total > 0 and not blocked)

    def _section_of_page(self, page) -> str:
        key = next((k for k, p in self.pages.items() if p is page), "")
        return self._section_labels.get(settings_layout.section_for(key), getattr(page, "page_title", key))

    def apply_all(self) -> None:
        """Applies every page's pending changes. A page whose can_apply() is
        False is skipped and its changes stay pending."""
        for p in self.pages.values():
            if not hasattr(p, "dirty_count") or not hasattr(p, "apply_own"):
                continue
            if p.dirty_count() <= 0:
                continue
            if hasattr(p, "can_apply") and not p.can_apply():
                continue
            p.apply_own()

    def discard_all(self) -> None:
        for p in self._pending_pages():
            p.discard()

    # -------------------------------------------------------- navigation --
    def _select(self, key: str) -> None:
        if key not in self.sections:
            key = settings_layout.section_for(key)
        if key not in self.sections:
            return
        self._current = key
        if self.search_edit.text():
            self.search_edit.blockSignals(True)
            self.search_edit.clear()
            self.search_edit.blockSignals(False)
        for btn in self._group.buttons():
            btn.setChecked(btn.property("sub_key") == key)
        self.stack.setCurrentWidget(self.sections[key])

    def current_section(self) -> str:
        return self._current

    def show_setting(self, section_key: str, widget: QWidget) -> None:
        """Opens a section at one setting: unfolds it, scrolls to it, focuses it."""
        self._select(section_key)
        open_folds_around(widget)

        def reveal():
            w = widget.parentWidget()
            while w is not None and not isinstance(w, QScrollArea):
                w = w.parentWidget()
            if w is not None:
                w.ensureWidgetVisible(widget, 0, 120)
            widget.setFocus(Qt.OtherFocusReason)
        QTimer.singleShot(0, reveal)

    # ------------------------------------------------------------ search --
    def _build_index(self) -> list:
        from webui.fields import _help_for, _label_for
        index = []
        for page_key, page in self.pages.items():
            section = settings_layout.section_for(page_key)
            for name, w in getattr(page, "_fields", {}).items():
                label = _label_for(w) or name
                where = [self._section_labels.get(section, "")]
                if page_key in settings_layout.GAMEPLAY_PAGES:
                    where.append("Most changed" if name in settings_layout.GAMEPLAY_COMMON
                                 else settings_layout.category_title(page_key))
                else:
                    p = w.parentWidget()
                    while p is not None and p is not page:
                        if isinstance(p, MoreOptions):
                            where.append(p.toggle.text())
                            break
                        p = p.parentWidget()
                index.append((section, label, _help_for(w), " › ".join(x for x in where if x), w))
        for section, label, help_text, w in self._extras:
            index.append((section, label, help_text, self._section_labels.get(section, ""), w))
        return index

    def _on_search(self, text: str) -> None:
        q = _norm(text)
        if not q:
            self._select(self._current)
            return
        if self._index is None:
            self._index = self._build_index()
        while self._results_layout.count():
            item = self._results_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        words = q.split()

        def matches(e) -> bool:
            # Name or place anywhere; the description only by whole-word start.
            name = _norm(f"{e[1]} {e[3]}")
            help_words = _norm(e[2]).replace("-", " ").split()
            return all(wd in name or any(h.startswith(wd) for h in help_words) for wd in words)
        hits = [e for e in self._index if matches(e)]
        hits.sort(key=lambda e: (not _norm(e[1]).startswith(words[0]), q not in _norm(e[1])))
        self._results_title.setText(
            f"{len(hits)} setting{'' if len(hits) == 1 else 's'} match \"{text.strip()}\"" if hits
            else f"Nothing matches \"{text.strip()}\". Try a shorter word.")
        self._results_card.setVisible(bool(hits))
        for section, label, help_text, where, w in hits[:60]:
            btn = QPushButton()
            btn.setObjectName("SearchResult")
            btn.setAccessibleName(f"{label}, in {where}")
            btn.setToolTip(help_text)
            lines = QVBoxLayout(btn)
            lines.setContentsMargins(16, 8, 16, 8)
            lines.setSpacing(2)
            name_label = QLabel(label)
            name_label.setObjectName("SearchResultName")
            where_label = QLabel(where)
            where_label.setObjectName("Dim")
            for lbl in (name_label, where_label):
                lbl.setAttribute(Qt.WA_TransparentForMouseEvents)
                lines.addWidget(lbl)
            btn.setMinimumHeight(name_label.sizeHint().height() + where_label.sizeHint().height() + 20)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _=False, s=section, w=w: self.show_setting(s, w))
            self._results_layout.addWidget(btn)
        self._group.setExclusive(False)
        for b in self._group.buttons():
            b.setChecked(False)
        self._group.setExclusive(True)
        self.stack.setCurrentWidget(self._results)
