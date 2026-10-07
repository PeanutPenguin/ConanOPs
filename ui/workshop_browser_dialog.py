"""Steam Workshop search dialog for the Mods tab. Uses steam_workshop_api.py (needs a
Steam Web API key) via a background worker so the GUI thread never blocks on HTTP."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QScrollArea, QWidget, QFrame, QComboBox, QCheckBox, QMessageBox,
)

import mod_manager
import steam_workshop_api as swa
from ui import assets
from models import ServerConfig
from workshop_search_runner import WorkshopSearchWorker

# Debounce so typing doesn't fire a request per letter.
_TYPING_DEBOUNCE_MS = 600

_SORT_CHOICES = [
    ("Best match", swa.SORT_BEST_MATCH),
    ("Most popular", swa.SORT_POPULAR),
    ("Recently updated", swa.SORT_RECENTLY_UPDATED),
]

# Readable on both light and dark themes.
_STATUS_COLORS = {
    swa.STATUS_UPDATED: "#3a9a4f",
    swa.STATUS_STALE: "#c9902f",
    swa.STATUS_LEGACY: "#c0483f",
    swa.STATUS_UNKNOWN: "#8a8a8a",
}


def _fmt_date(ts: int) -> str:
    if not ts:
        return "unknown date"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %d, %Y").replace(" 0", " ")


class WorkshopBrowserDialog(QDialog):
    def __init__(self, server: ServerConfig, api_key: str, on_changed=None, parent=None,
                 cutoff_ts: int = swa.IRIS_CUTOFF_TIMESTAMP):
        super().__init__(parent)
        self.server = server
        self.api_key = api_key
        self.on_changed = on_changed
        self.cutoff_ts = cutoff_ts
        self._worker: Optional[WorkshopSearchWorker] = None
        self._pending_search = False   # a new search was asked for while one was running
        self._loading_more = False     # the in-flight search appends instead of replacing
        self._next_cursor = ""
        self._shown_ids: set = set()
        # So items added in this dialog show "Added" immediately.
        self._added_this_session: set = set()

        self.setWindowTitle("Browse Steam Workshop")
        self.setModal(True)
        self.resize(640, 680)

        root = QVBoxLayout(self)

        filter_note = QLabel(
            f"Each mod is labelled by its Workshop version tag and whether it's been updated since "
            f"{_fmt_date(cutoff_ts)} (the cutoff set in App Settings). By default only mods updated "
            f"for the current patch are shown -- tick the boxes below to see the rest, labelled. An "
            f"update date is a strong hint, not proof: a description edit also counts as an update."
        )
        filter_note.setObjectName("Dim")
        filter_note.setWordWrap(True)
        root.addWidget(filter_note)

        search_row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search the Workshop, or leave blank to browse…")
        self.search_edit.returnPressed.connect(self._run_search)
        self.search_edit.textEdited.connect(self._on_text_edited)
        search_row.addWidget(self.search_edit, 1)
        self.sort_combo = QComboBox()
        for label, value in _SORT_CHOICES:
            self.sort_combo.addItem(label, value)
        self.sort_combo.setCurrentIndex(1)  # popular, until something is typed
        self.sort_combo.currentIndexChanged.connect(lambda _i: self._run_search())
        search_row.addWidget(self.sort_combo)
        self.search_btn = QPushButton("Search")
        self.search_btn.clicked.connect(self._run_search)
        search_row.addWidget(self.search_btn)
        root.addLayout(search_row)

        show_row = QHBoxLayout()
        show_row.addWidget(QLabel("Show:"))
        self.status_checks = {}
        for status in swa.ALL_STATUSES:
            cb = QCheckBox(swa.STATUS_LABELS[status])
            cb.setChecked(status == swa.STATUS_UPDATED)
            cb.toggled.connect(lambda _c: self._run_search())
            self.status_checks[status] = cb
            show_row.addWidget(cb)
        show_row.addStretch(1)
        root.addLayout(show_row)

        status_row = QHBoxLayout()
        self.spinner = assets.LoadingSpinner(22)
        self.spinner.hide()
        status_row.addWidget(self.spinner)
        self.status_label = QLabel("")
        self.status_label.setObjectName("Dim")
        self.status_label.setWordWrap(True)
        status_row.addWidget(self.status_label, 1)
        root.addLayout(status_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        self.results_container = QWidget()
        self.results_layout = QVBoxLayout(self.results_container)
        self.results_layout.addStretch(1)
        scroll.setWidget(self.results_container)
        root.addWidget(scroll, 1)

        close_row = QHBoxLayout()
        self.load_more_btn = QPushButton("Load More")
        self.load_more_btn.setVisible(False)
        self.load_more_btn.clicked.connect(self._load_more)
        close_row.addWidget(self.load_more_btn)
        close_row.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.setObjectName("PrimaryButton")
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        root.addLayout(close_row)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(_TYPING_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._run_search)

        if not self.api_key:
            self.status_label.setText(
                "No Steam Web API key configured -- add one on App Settings to search or browse the Workshop."
            )
            for w in (self.search_edit, self.search_btn, self.sort_combo, *self.status_checks.values()):
                w.setEnabled(False)
        else:
            self._run_search()

    # ------------------------------------------------------------ lifetime --
    def _wait_for_worker_if_running(self) -> None:
        """Called from every close path so the owned QThread is never destroyed while running."""
        self._debounce.stop()
        if self._worker is not None:
            self._worker.wait(2000)

    def accept(self) -> None:
        self._wait_for_worker_if_running()
        super().accept()

    def reject(self) -> None:
        self._wait_for_worker_if_running()
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802 -- Qt's naming
        self._wait_for_worker_if_running()
        super().closeEvent(event)

    # -------------------------------------------------------------- search --
    def _selected_statuses(self) -> set:
        chosen = {s for s, cb in self.status_checks.items() if cb.isChecked()}
        return chosen or {swa.STATUS_UPDATED}

    def _on_text_edited(self, text: str) -> None:
        # Query -> best match, empty -> popular. Signals blocked; the debounce timer searches.
        want = swa.SORT_BEST_MATCH if text.strip() else swa.SORT_POPULAR
        current = self.sort_combo.currentData()
        if current in (swa.SORT_BEST_MATCH, swa.SORT_POPULAR) and current != want:
            self.sort_combo.blockSignals(True)
            self.sort_combo.setCurrentIndex(self.sort_combo.findData(want))
            self.sort_combo.blockSignals(False)
        self._debounce.start()

    def _clear_results(self) -> None:
        while self.results_layout.count() > 1:  # leave the trailing stretch in place
            item = self.results_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._shown_ids = set()

    def _run_search(self) -> None:
        if not self.api_key:
            return
        self._debounce.stop()
        if self._worker is not None:
            self._pending_search = True  # run again with the latest settings once this one's done
            return
        self._loading_more = False
        self._clear_results()
        self.load_more_btn.setVisible(False)
        self.status_label.setText("Searching…")
        self._start_worker(cursor="*")

    def _load_more(self) -> None:
        if self._worker is not None or not self._next_cursor:
            return
        self._loading_more = True
        self.load_more_btn.setEnabled(False)
        self.status_label.setText("Loading more…")
        self._start_worker(cursor=self._next_cursor)

    def _start_worker(self, cursor: str) -> None:
        self.search_btn.setEnabled(False)
        self.spinner.show()
        query = self.search_edit.text().strip()
        self._worker = WorkshopSearchWorker(
            self.api_key, query, parent=self, sort=self.sort_combo.currentData() or "",
            show=self._selected_statuses(), cutoff_ts=self.cutoff_ts, cursor=cursor,
            skip_ids=set(self._shown_ids) if self._loading_more else None,
        )
        self._worker.finished_search.connect(self._on_search_finished)
        self._worker.start()

    def _on_search_finished(self, result) -> None:
        self._worker = None
        self.spinner.hide()
        self.search_btn.setEnabled(True)
        self.load_more_btn.setEnabled(True)
        if self._pending_search:
            # Settings changed mid-search; this result is stale.
            self._pending_search = False
            self._run_search()
            return
        appending = self._loading_more
        self._loading_more = False
        if not result.ok:
            self.status_label.setText(result.error)
            return
        self._next_cursor = result.next_cursor
        self.load_more_btn.setVisible(bool(result.next_cursor))
        if not result.items and not appending:
            self.status_label.setText(
                "No results with the current filters -- try a different search, tick more of the "
                "Show boxes, or check back later as more mod authors update."
            )
            return
        existing_ids = {m["id"] for m in self.server.mods}
        for item in result.items:
            if item.id in self._shown_ids:
                continue
            self._shown_ids.add(item.id)
            self.results_layout.insertWidget(
                self.results_layout.count() - 1, self._make_result_row(item, existing_ids),
            )
        more = " More are available." if result.next_cursor else ""
        self.status_label.setText(f"Showing {len(self._shown_ids)} mod(s).{more}")

    # ---------------------------------------------------------------- rows --
    def _make_result_row(self, item, existing_ids: set) -> QFrame:
        row = QFrame()
        row.setObjectName("Card")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(12, 10, 12, 10)

        text_col = QVBoxLayout()
        title = QLabel(item.title)
        title.setStyleSheet("font-weight: 600;")
        title.setWordWrap(True)
        text_col.addWidget(title)

        status = getattr(item, "status", swa.STATUS_UNKNOWN)
        badge = QLabel(f"{swa.STATUS_LABELS.get(status, status)} · updated {_fmt_date(item.time_updated)}")
        badge.setStyleSheet(f"color: {_STATUS_COLORS.get(status, '#8a8a8a')}; font-weight: 600;")
        text_col.addWidget(badge)

        snippet = f" — {item.description[:140]}" if item.description else ""
        meta = QLabel(f"{item.subscriptions:,} subscribers{snippet}")
        meta.setObjectName("Dim")
        meta.setWordWrap(True)
        text_col.addWidget(meta)

        children = list(getattr(item, "children", []) or [])
        if children:
            titles = getattr(item, "child_titles", {}) or {}
            needs = ", ".join(titles.get(c, c) for c in children)
            req = QLabel(f"Requires: {needs}")
            req.setObjectName("Dim")
            req.setWordWrap(True)
            text_col.addWidget(req)
        layout.addLayout(text_col, 1)

        already_added = item.id in existing_ids or item.id in self._added_this_session
        add_btn = QPushButton("Added" if already_added else "Add")
        add_btn.setEnabled(not already_added)
        add_btn.clicked.connect(lambda _=False, it=item, btn=add_btn: self._add_item(it, btn))
        layout.addWidget(add_btn)

        return row

    def _missing_children(self, item) -> List[str]:
        have = {m["id"] for m in self.server.mods} | self._added_this_session
        return [c for c in (getattr(item, "children", []) or []) if c not in have]

    def _add_item(self, item, button: QPushButton) -> None:
        if getattr(item, "status", None) == swa.STATUS_LEGACY:
            reply = QMessageBox.question(
                self, "Legacy Mod",
                f'"{item.title}" is tagged Legacy -- built for the old version of the game. It most '
                f"likely won't load on a current server. Add it anyway?",
                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
            )
            if reply != QMessageBox.Yes:
                return

        to_add = [(item.id, item.title)]
        missing = self._missing_children(item)
        if missing:
            titles = getattr(item, "child_titles", {}) or {}
            names = "\n".join(f"• {titles.get(c, c)}" for c in missing)
            reply = QMessageBox.question(
                self, "Required Mods",
                f'"{item.title}" needs these other Workshop items to work:\n\n{names}\n\n'
                f"Add them too? (They'll be placed before it in the load order.)",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
            )
            if reply == QMessageBox.Yes:
                to_add = [(c, titles.get(c, "")) for c in missing] + to_add

        mods = self.server.mods
        for wid, name in to_add:
            mods = mod_manager.add_mod(mods, wid, name)
            self._added_this_session.add(wid)
        self.server.mods = mods
        button.setText("Added")
        button.setEnabled(False)
        if self.on_changed:
            self.on_changed(self.server)
