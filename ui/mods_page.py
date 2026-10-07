from __future__ import annotations

import re
from typing import Optional

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QListWidget,
    QListWidgetItem, QLineEdit, QDialog, QMessageBox, QAbstractItemView, QCheckBox,
)
from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QGuiApplication, QCursor, QColor

import mod_manager
import process_manager
import steam_workshop_api as swa
from theme_config import load_theme
from ui import assets
from ui.ops import run_responsive
from models import ServerConfig
from update_runner import ModDownloadWorker
from ui.workshop_browser_dialog import WorkshopBrowserDialog
from workshop_search_runner import ModStatusWorker
from ui.auto_bisect_dialog import AutoBisectDialog


class ModsPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.server: Optional[ServerConfig] = None
        self.on_changed = None  # callable(server) -- persist + rewrite modlist.txt
        self.get_api_key = None  # callable() -> str -- set by main_window; AppConfig.steam_api_key
        self.is_server_online = None  # callable() -> bool -- set by main_window; whether anyone's currently connected
        # Both set by main_window: claims/releases this server for the
        # DURATION of an auto-bisect run (not just around each
        # individual stop/restart it does internally) -- what keeps
        # the health check, watchdog, and scheduled restarts from
        # fighting a bisect that's mid-test. See _open_auto_bisect().
        self.lock_server_for_automation = None  # callable(server_id) -> None
        self.unlock_server_for_automation = None  # callable(server_id) -> None
        self.restart_server = None  # callable(ServerConfig) -> None -- set by main_window; used by AutoBisectDialog's "Re-enable Anyway"
        self._active_bisect_dialog = None  # set while a Quick Mod Check/Find All dialog is open -- see MainWindow.closeEvent
        self._download_worker: Optional[ModDownloadWorker] = None
        self._download_worker_server_id: Optional[str] = None  # which server _download_worker belongs to -- see _refresh()
        # Both set by main_window, sharing its _mod_refresh_workers
        # registry with the periodic daily mod-refresh check AND an
        # update-triggered mod refresh -- without this, a manual
        # download here could run a second SteamCMD process against
        # the same Workshop content folder at the same time as either
        # of those.
        self.claim_mod_refresh_slot = None    # callable(server_id, worker) -> bool (False if already claimed)
        self.release_mod_refresh_slot = None  # callable(server_id) -> None
        # Set by main_window: whether ANY mod download/refresh (manual,
        # daily, or update-triggered) is in flight for a server. An
        # auto-bisect must not start while one is: when it finishes it
        # rewrites modlist.txt from the full mod list (and an update-
        # triggered one can relaunch the server), right in the middle
        # of a test round.
        self.is_mod_refresh_busy = None       # callable(server_id) -> bool
        # Download workers that have reported back but whose QThread
        # hasn't fully exited yet. finished_download is emitted from
        # INSIDE run(), so dropping the last reference in its handler
        # can destroy a QThread that's still running (a hard crash) --
        # same reason main_window keeps _retiring_workers.
        self._retiring_download_workers: list = []
        # Workshop update status of each server's mods -- see
        # _check_mod_status. Set by main_window: the cutoff date from
        # App Settings, and whether to check automatically the first
        # time each server's mods are shown this session (off by
        # default so nothing here touches the network unless wired up).
        self.get_update_cutoff = None  # callable() -> "YYYY-MM-DD"
        self.auto_check_mod_status = False
        self._palette = load_theme()         # row colors for outdated/broken mods
        self._downloaded: dict = {}          # mod id -> .pak present, refreshed by _refresh()
        self._mod_status: dict = {}          # server_id -> {workshop_id: swa.WorkshopItem}
        self._mod_status_error: dict = {}    # server_id -> error text from the last check
        self._status_worker: Optional[ModStatusWorker] = None
        self._status_worker_server_id: Optional[str] = None

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(12)

        top = QHBoxLayout()
        self.title_label = title = QLabel("Mods")
        title.setObjectName("PageTitle")
        top.addWidget(title)
        browse_btn = QPushButton("Browse Workshop…")
        browse_btn.clicked.connect(self._open_workshop_browser)
        top.addWidget(browse_btn)
        self.check_status_btn = QPushButton("Check for Outdated Mods")
        self.check_status_btn.setToolTip(
            "Looks up every mod on this server on the Steam Workshop and flags any that haven't "
            "been updated for the current game patch (cutoff date in App Settings), are Legacy, or "
            "need another Workshop item that isn't in this list. Works without an API key; with one, "
            "missing required mods are checked too."
        )
        self.check_status_btn.clicked.connect(self._check_mod_status)
        top.addWidget(self.check_status_btn)
        self.status_spinner = assets.LoadingSpinner(22)
        self.status_spinner.hide()
        top.addWidget(self.status_spinner)
        self.download_btn = QPushButton("Download Mods")
        self.download_btn.setToolTip(
            "Fetches the .pak file for every mod (enabled or not), so what's listed here actually "
            "exists on disk for the server to load -- adding a mod alone only records its Workshop ID."
        )
        self.download_btn.clicked.connect(self._download_mods)
        top.addWidget(self.download_btn)
        top.addStretch(1)
        bisect_btn = QPushButton("Bisect (Manual)")
        bisect_btn.setToolTip("Find a broken mod yourself: ConanOps halves the list each round and you report whether the problem is still there.")
        bisect_btn.clicked.connect(self._open_bisect)
        auto_bisect_btn = QPushButton("Quick Mod Check…")
        auto_bisect_btn.setToolTip(
            "ConanOps restarts the server once per mod, testing each one alone, to find every mod "
            "that's independently causing a problem. Won't catch a mod that needs another mod "
            "present, or two mods that only break things together -- use Find All Bad Mods for "
            "that. Can take a while -- one restart per mod."
        )
        auto_bisect_btn.clicked.connect(self._open_auto_bisect)
        top.addWidget(auto_bisect_btn)
        find_all_btn = QPushButton("Find All Bad Mods…")
        find_all_btn.setToolTip(
            "Like Quick Mod Check, but also catches a pair that only breaks things when both are "
            "enabled together. Takes meaningfully longer."
        )
        find_all_btn.clicked.connect(self._open_find_all_bisect)
        top.addWidget(find_all_btn)
        top.addWidget(bisect_btn)
        root.addLayout(top)

        self.mod_status_label = QLabel("")
        self.mod_status_label.setWordWrap(True)
        self.mod_status_label.hide()
        root.addWidget(self.mod_status_label)

        add_row = QHBoxLayout()
        add_row.setSpacing(10)
        self.id_edit = QLineEdit()
        self.id_edit.setPlaceholderText("1234567890")
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Shown in this list")
        add_btn = QPushButton("Add Mod")
        add_btn.setObjectName("PrimaryButton")
        add_btn.clicked.connect(self._add_mod)
        for text, edit, stretch in (("Workshop ID or URL", self.id_edit, 1), ("Display name (optional)", self.name_edit, 2)):
            col = QVBoxLayout()
            col.setSpacing(6)
            lbl = QLabel(text)
            lbl.setObjectName("StatLabel")
            col.addWidget(lbl)
            col.addWidget(edit)
            add_row.addLayout(col, stretch)
        add_col = QVBoxLayout()
        add_col.addStretch(1)
        add_col.addWidget(add_btn)
        add_row.addLayout(add_col)
        root.addLayout(add_row)

        self.list_widget = QListWidget()
        # InternalMove: reordering only, by dragging a row up or down
        # within this same list -- no dropping onto other widgets, no
        # dragging items out. A single-column vertical QListWidget has
        # no "x axis" to reorder along in the first place, so this is
        # inherently vertical-only: a row can only ever land above or
        # below another row, never beside one.
        self.list_widget.setDragDropMode(QAbstractItemView.InternalMove)
        self.list_widget.setDefaultDropAction(Qt.MoveAction)
        self.list_widget.model().rowsMoved.connect(self._on_rows_moved)
        self.list_widget.setObjectName("ModList")
        self.list_widget.setSpacing(0)
        root.addWidget(self.list_widget, 1)

        note = QLabel(
            "Load order matters -- mods higher in the list load first. Drag a mod up or down to "
            "reorder it, or use the arrows on its row. Disabling a mod here removes it from modlist.txt "
            "without deleting its files. None of this takes effect until the server is next "
            "restarted -- the running server keeps using whatever mod list it already loaded."
        )
        note.setObjectName("Dim")
        note.setWordWrap(True)
        root.addWidget(note)

    def set_server(self, server: ServerConfig) -> None:
        self.server = server
        self._refresh()
        if (self.auto_check_mod_status and server is not None and server.mods
                and server.id not in self._mod_status and server.id not in self._mod_status_error):
            self._check_mod_status()

    # ------------------------------------------------------- mod status --
    def _cutoff_ts(self) -> int:
        return swa.cutoff_timestamp(self.get_update_cutoff() if self.get_update_cutoff else swa.DEFAULT_CUTOFF_DATE)

    def _check_mod_status(self) -> None:
        if not self.server or not self.server.mods or self._status_worker is not None:
            return
        server = self.server
        api_key = self.get_api_key() if self.get_api_key else ""
        worker = ModStatusWorker([m["id"] for m in server.mods], api_key=api_key, cutoff_ts=self._cutoff_ts())
        self._status_worker = worker
        self._status_worker_server_id = server.id
        self.check_status_btn.setEnabled(False)
        self.check_status_btn.setText("Checking…")
        self.status_spinner.show()
        worker.finished_status.connect(lambda result, sid=server.id: self._on_mod_status(sid, result))
        self._retiring_download_workers.append(worker)  # same keep-alive as the download worker
        worker.finished.connect(
            lambda w=worker: self._retiring_download_workers.remove(w) if w in self._retiring_download_workers else None
        )
        worker.start()

    def _on_mod_status(self, server_id: str, result) -> None:
        self._status_worker = None
        self._status_worker_server_id = None
        self.check_status_btn.setEnabled(True)
        self.check_status_btn.setText("Check for Outdated Mods")
        self.status_spinner.hide()
        if result.ok:
            self._mod_status[server_id] = dict(result.items)
            self._mod_status_error.pop(server_id, None)
        else:
            self._mod_status_error[server_id] = result.error
        self._refresh()

    def _row_color(self, mod: dict, known, server_ids: set) -> str:
        """Theme color for a mod row: dim when disabled, red for a
        Legacy / missing / needs-a-mod problem, amber when it's only
        out of date, default otherwise."""
        pal = self._palette
        if not mod.get("enabled", True):
            return pal.dim
        if known is None:
            return ""
        info = known.get(mod["id"])
        if info is None or info.status == swa.STATUS_LEGACY or any(c not in server_ids for c in info.children):
            return pal.red
        if info.status == swa.STATUS_STALE:
            return pal.yellow
        return ""

    def _status_note(self, mod: dict, known: dict, server_ids: set) -> str:
        """Short per-row note from the last status check ("" if none)."""
        info = known.get(mod["id"])
        if info is None:
            return "  —  ⚠ not found on the Workshop (removed or private?)"
        parts = []
        if info.status == swa.STATUS_STALE:
            parts.append(f"⚠ not updated for current patch (last {info.time_updated and self._fmt_day(info.time_updated)})")
        elif info.status == swa.STATUS_LEGACY:
            parts.append("⚠ Legacy mod")
        elif info.status == swa.STATUS_UNKNOWN:
            parts.append("no version tag")
        missing = [c for c in info.children if c not in server_ids]
        if missing:
            names = ", ".join((known[c].title if c in known else c) for c in missing)
            parts.append(f"⚠ needs: {names}")
        return ("  —  " + "  ·  ".join(parts)) if parts else ""

    def _set_status_note(self, text: str, kind: str) -> None:
        self.mod_status_label.setText(text)
        self.mod_status_label.setVisible(bool(text))
        self.mod_status_label.setObjectName("WarnNote" if kind == "warn" else "OkNote")
        self.mod_status_label.style().unpolish(self.mod_status_label)
        self.mod_status_label.style().polish(self.mod_status_label)

    @staticmethod
    def _fmt_day(ts: int) -> str:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %d, %Y").replace(" 0", " ")

    def _status_summary(self, known: dict) -> str:
        if not self.server:
            return ""
        server_ids = {m["id"] for m in self.server.mods}
        enabled = [m for m in self.server.mods if m.get("enabled", True)]
        stale = [m for m in enabled if m["id"] in known and known[m["id"]].status == swa.STATUS_STALE]
        legacy = [m for m in enabled if m["id"] in known and known[m["id"]].status == swa.STATUS_LEGACY]
        gone = [m for m in enabled if m["id"] not in known]
        needs = [m for m in enabled if m["id"] in known and any(c not in server_ids for c in known[m["id"]].children)]
        bits = []
        if stale:
            bits.append(f"{len(stale)} not updated for the current patch")
        if legacy:
            bits.append(f"{len(legacy)} Legacy")
        if gone:
            bits.append(f"{len(gone)} not found on the Workshop")
        if needs:
            bits.append(f"{len(needs)} missing a required mod")
        if not bits:
            return "✓ Every enabled mod is up to date for the current patch."
        return "⚠ Enabled mods: " + ", ".join(bits) + ". Outdated mods are a common cause of a server that won't start."

    def _refresh(self) -> None:
        # Remember which mod was selected so it's still selected after
        # the rebuild -- otherwise every Move Up / Move Down / toggle
        # dropped the selection and the next click did nothing.
        selected_id = self._selected_id()
        self.list_widget.clear()
        # Sync the Download button to whether THIS server actually has
        # a download in flight -- without this, switching away from a
        # server mid-download and back again left the button stuck on
        # "Downloading…" forever, since the finished-download handler
        # only touched the button when the page was still showing that
        # same server at the moment it fired.
        downloading_this_server = (
            self._download_worker is not None and self._download_worker_server_id == (self.server.id if self.server else None)
        )
        downloading_other_server = self._download_worker is not None and not downloading_this_server
        # Only one manual download runs at a time (see _download_mods).
        # While another server's is in flight, say so and disable the
        # button, rather than leaving it clickable and silently doing
        # nothing when clicked.
        self.download_btn.setEnabled(self._download_worker is None)
        if downloading_this_server:
            self.download_btn.setText("Downloading…")
        elif downloading_other_server:
            self.download_btn.setText("Busy (another server)…")
        else:
            self.download_btn.setText("Download Mods")
        if not self.server:
            self.mod_status_label.setText("")
            return
        known = self._mod_status.get(self.server.id)
        error = self._mod_status_error.get(self.server.id)
        server_ids = {m["id"] for m in self.server.mods}
        if error:
            self._set_status_note(f"Couldn't check mod status: {error}", "warn")
        elif known is not None:
            summary = self._status_summary(known)
            self._set_status_note(summary, "ok" if summary.startswith("✓") else "warn")
        else:
            self._set_status_note("", "")
        # One disk check per mod per refresh (the label, the row's pills
        # and the in-place toggle all reuse it).
        self._downloaded = {
            m["id"]: (mod_manager.find_workshop_pak(self.server.steamcmd_dir, m["id"]) is not None)
            for m in self.server.mods
        } if self.server.steamcmd_dir else {}
        self.list_widget.setUpdatesEnabled(False)
        for index, m in enumerate(self.server.mods):
            label = self._row_label(m, known, server_ids)
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, m["id"])
            # The row widget below draws the row; the item's own text is
            # kept for screen readers and search, but never painted (see
            # the ModList rules in theme.py) -- no tooltip either, since
            # that popped this raw text up over the row on hover.
            item.setForeground(QColor(0, 0, 0, 0))
            self.list_widget.addItem(item)
            row = self._make_row(index, m, known, server_ids)
            item.setSizeHint(QSize(row.sizeHint().width(), max(58, row.sizeHint().height())))
            self.list_widget.setItemWidget(item, row)
            if m["id"] == selected_id:
                self.list_widget.setCurrentItem(item)
        self.list_widget.setUpdatesEnabled(True)

    def _row_label(self, m: dict, known, server_ids: set) -> str:
        label = f"{'✓' if m.get('enabled', True) else '✗'}  {m.get('name') or m['id']}  ({m['id']})"
        if self.server and self.server.steamcmd_dir and not self._downloaded.get(m["id"], True):
            label += "  —  not downloaded"
        if known is not None:
            label += self._status_note(m, known, server_ids)
        return label

    def _row_pills(self, mod: dict, known, server_ids: set) -> list:
        """(object name, text) pills for a mod row."""
        pills = []
        if self.server and self.server.steamcmd_dir and not self._downloaded.get(mod["id"], True):
            pills.append(("PillOff", "Not downloaded"))
        if known is None:
            return pills
        info = known.get(mod["id"])
        if info is None:
            return pills + [("PillBad", "Not on the Workshop")]
        if info.status == swa.STATUS_UPDATED:
            pills.insert(0, ("PillOn", "Updated for current patch"))
        elif info.status == swa.STATUS_STALE:
            pills.insert(0, ("PillWarn", "Not updated since cutoff"))
        elif info.status == swa.STATUS_LEGACY:
            pills.insert(0, ("PillBad", "Legacy"))
        missing = [c for c in info.children if c not in server_ids]
        if missing:
            names = ", ".join((known[c].title if c in known else c) for c in missing)
            pills.append(("PillBad", f"Needs: {names}"))
        return pills

    def _make_row(self, index: int, mod: dict, known, server_ids: set) -> QWidget:
        row = QWidget()
        row.setObjectName("TransparentRow")
        line = QHBoxLayout(row)
        line.setContentsMargins(10, 6, 8, 6)
        line.setSpacing(12)
        num = QLabel(str(index + 1))
        num.setObjectName("SettingValue")
        num.setFixedWidth(22)
        line.addWidget(num)
        switch = QCheckBox()
        switch.setChecked(mod.get("enabled", True))
        switch.setAccessibleName(f"Enable {mod.get('name') or mod['id']}")
        switch.toggled.connect(lambda on, mid=mod["id"]: self._set_enabled_in_place(mid, on))
        line.addWidget(switch)
        text = QVBoxLayout()
        text.setSpacing(2)
        name = QLabel(mod.get("name") or mod["id"])
        name.setObjectName("ModName")
        name.setProperty("tone", self._row_tone(mod, known, server_ids))
        row.name_label = name  # for _set_enabled_in_place
        text.addWidget(name)
        sub = mod["id"]
        info = known.get(mod["id"]) if known else None
        if info is not None and info.time_updated:
            sub += f" · updated {self._fmt_day(info.time_updated)}"
        sub_lbl = QLabel(sub)
        sub_lbl.setObjectName("SettingValue")
        text.addWidget(sub_lbl)
        line.addLayout(text, 1)
        for obj, pill_text in self._row_pills(mod, known, server_ids):
            pill = QLabel(f"● {pill_text}")
            pill.setObjectName(obj)
            line.addWidget(pill, 0, Qt.AlignVCenter)
        for icon, tip, action in (("up", "Move up", lambda: self._move(-1)), ("down", "Move down", lambda: self._move(1)),
                                  ("trash", "Remove", self._remove_selected)):
            btn = QPushButton()
            btn.setObjectName("IconButton")
            btn.setIcon(assets.line_icon(icon, "#e8e8e8"))
            btn.setToolTip(f"{tip}: {mod.get('name') or mod['id']}")
            btn.setAccessibleName(btn.toolTip())
            btn.clicked.connect(lambda _=False, mid=mod["id"], a=action: self._row_action(mid, a))
            line.addWidget(btn)
        return row

    def _row_tone(self, mod: dict, known, server_ids: set) -> str:
        """Name color for a row, as a style property the theme maps to
        a color -- NOT a per-widget stylesheet, which made Qt recompute
        styles for the whole list on every rebuild (the lag spike)."""
        color = self._row_color(mod, known, server_ids)
        pal = self._palette
        return {pal.dim: "dim", pal.red: "bad", pal.yellow: "warn"}.get(color, "normal") if color else "normal"

    def _set_enabled_in_place(self, mod_id: str, on: bool) -> None:
        """A row's on/off switch: saves the change and updates only that
        row and the summary note -- no full list rebuild, no selection
        change, so nothing flickers or lags."""
        if not self.server:
            return
        self.server.mods = mod_manager.set_enabled(self.server.mods, mod_id, on)
        if self.on_changed:
            # Saving (config + modlist.txt) is disk work -- let the switch
            # and row repaint first, then save on the next event-loop pass.
            QTimer.singleShot(0, lambda srv=self.server: self.on_changed(srv))
        known = self._mod_status.get(self.server.id)
        server_ids = {m["id"] for m in self.server.mods}
        mod = next((m for m in self.server.mods if m["id"] == mod_id), None)
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.data(Qt.UserRole) != mod_id or mod is None:
                continue
            item.setText(self._row_label(mod, known, server_ids))
            row = self.list_widget.itemWidget(item)
            name = getattr(row, "name_label", None)
            if name is not None:
                name.setProperty("tone", self._row_tone(mod, known, server_ids))
                name.style().unpolish(name)
                name.style().polish(name)
            break
        if known is not None and not self._mod_status_error.get(self.server.id):
            summary = self._status_summary(known)
            self._set_status_note(summary, "ok" if summary.startswith("✓") else "warn")

    def _row_action(self, mod_id: str, action) -> None:
        """Selects the row for `mod_id`, then runs one of the existing
        selection-based actions (toggle, move, remove) -- so the row
        buttons share exactly the same code paths as before."""
        for i in range(self.list_widget.count()):
            if self.list_widget.item(i).data(Qt.UserRole) == mod_id:
                self.list_widget.setCurrentRow(i)
                break
        # Deferred: the action rebuilds the list, which deletes the very
        # widget whose signal is being handled right now.
        QTimer.singleShot(0, action)

    def _selected_id(self) -> Optional[str]:
        item = self.list_widget.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _persist(self) -> None:
        if self.on_changed and self.server:
            self.on_changed(self.server)
        self._refresh()

    def _add_mod(self) -> None:
        raw = self.id_edit.text().strip()
        if not raw or not self.server:
            return
        wid = self._extract_workshop_id(raw)
        if wid is None:
            QMessageBox.warning(
                self, "Not a Workshop ID",
                f"\"{raw}\" doesn't look like a Steam Workshop item ID. Paste the numeric ID (the one "
                f"in the mod's Workshop URL, e.g. the 1234567890 in "
                f"steamcommunity.com/sharedfiles/filedetails/?id=1234567890), or use Browse Workshop "
                f"instead of typing one in by hand.",
            )
            return
        self.server.mods = mod_manager.add_mod(self.server.mods, wid, self.name_edit.text().strip())
        self.id_edit.clear()
        self.name_edit.clear()
        self._persist()

    @staticmethod
    def _extract_workshop_id(raw: str) -> Optional[str]:
        """Accepts a bare numeric Workshop ID, or a full Workshop URL
        with one -- anything else (a mod's display name, a garbled
        paste) is rejected outright rather than silently saved as an
        ID that will never actually resolve to a real download."""
        raw = raw.strip()
        if raw.isdigit():
            return raw
        match = re.search(r"[?&]id=(\d+)", raw)
        if match:
            return match.group(1)
        return None

    def _remove_selected(self) -> None:
        wid = self._selected_id()
        if not wid or not self.server:
            return
        mod = next((m for m in self.server.mods if m["id"] == wid), None)
        display = (mod.get("name") or wid) if mod else wid
        reply = QMessageBox.question(
            self, "Remove Mod",
            f'Remove "{display}" from this server\'s mod list? Its downloaded files aren\'t deleted, '
            f"only the entry here -- but the server will drop any of its content from the world the "
            f"next time it starts, which can include placed items or buildings.",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        )
        if reply != QMessageBox.Yes:
            return
        self.server.mods = mod_manager.remove_mod(self.server.mods, wid)
        self._persist()

    def _toggle_selected(self) -> None:
        wid = self._selected_id()
        if not wid or not self.server:
            return
        current = next((m.get("enabled", True) for m in self.server.mods if m["id"] == wid), True)
        self.server.mods = mod_manager.set_enabled(self.server.mods, wid, not current)
        self._persist()

    def _move(self, direction: int) -> None:
        wid = self._selected_id()
        if not wid or not self.server:
            return
        self.server.mods = mod_manager.move_mod(self.server.mods, wid, direction)
        self._persist()

    def _on_rows_moved(self, *args) -> None:
        """Fires after a drag-to-reorder finishes (QListWidget's
        InternalMove already rearranged the widget's own rows by the
        time this signal fires -- this just syncs server.mods to match
        that new visual order and persists it, same as the Up/Down
        buttons already do for a button-driven reorder)."""
        if not self.server:
            return
        id_order = [self.list_widget.item(i).data(Qt.UserRole) for i in range(self.list_widget.count())]
        by_id = {m["id"]: m for m in self.server.mods}
        reordered = [by_id[i] for i in id_order if i in by_id]
        if reordered == self.server.mods:
            return  # nothing actually changed order -- avoid a redundant save+refresh
        self.server.mods = reordered
        # Deferred: rowsMoved fires from inside QListView's own drop
        # handling, which keeps using the model's indexes after this
        # returns. _persist() -> _refresh() clears and rebuilds the
        # whole list, so doing that synchronously here pulls the rows
        # out from under Qt mid-drop. server.mods is already updated
        # above, so nothing reads a stale order in the meantime.
        QTimer.singleShot(0, self._persist)

    def _open_workshop_browser(self) -> None:
        if not self.server:
            return
        api_key = self.get_api_key() if self.get_api_key else ""
        dlg = WorkshopBrowserDialog(self.server, api_key, on_changed=self.on_changed, parent=self, cutoff_ts=self._cutoff_ts())
        dlg.exec()
        self._dispose_dialog(dlg)
        self._refresh()

    @staticmethod
    def _dispose_dialog(dlg, worker=None) -> None:
        """Deletes a finished dialog instead of leaving it parented to
        this page forever (one leaked dialog per open). If `worker` --
        a QThread the dialog owns -- is still running, waits for it to
        finish first, since deleting the dialog would delete the
        running thread with it."""
        if not hasattr(dlg, "deleteLater"):
            return  # test doubles
        if worker is None:
            worker = getattr(dlg, "_worker", None)
        if worker is not None and worker.isRunning():
            worker.finished.connect(dlg.deleteLater)
        else:
            dlg.deleteLater()

    def _open_auto_bisect(self) -> None:
        self._run_auto_bisect_dialog(find_all=False)

    def _open_find_all_bisect(self) -> None:
        self._run_auto_bisect_dialog(find_all=True)

    def _run_auto_bisect_dialog(self, find_all: bool) -> None:
        if not self.server or not self.server.mods:
            QMessageBox.information(self, "No mods", "Add some mods first.")
            return
        if not self.server.install_dir:
            QMessageBox.warning(self, "Server not set up", "Set up this server's install folder first (the setup wizard).")
            return
        if self.is_server_online and self.is_server_online():
            QMessageBox.warning(
                self, "Players Online",
                "Auto-bisect restarts the server repeatedly, which would disconnect anyone currently "
                "playing. Wait until nobody's online, or use the manual bisect instead if you need to "
                "run this right now.",
            )
            return
        if self.is_mod_refresh_busy and self.is_mod_refresh_busy(self.server.id):
            QMessageBox.information(
                self, "Mods are downloading",
                "ConanOps is downloading or refreshing this server's mods right now. Wait for that "
                "to finish first -- when it does, it rewrites the mod list, which would interfere "
                "with the testing.",
            )
            return
        server = self.server
        was_running = process_manager.is_running(server.install_dir)
        if self.lock_server_for_automation:
            self.lock_server_for_automation(server.id)
        dlg = None
        try:
            dlg = AutoBisectDialog(
                server, list(server.mods), was_running, find_all=find_all,
                on_changed=self.on_changed, is_online=self.is_server_online,
                restart_server=self.restart_server, parent=self,
            )
            self._active_bisect_dialog = dlg
            dlg.exec()
        finally:
            self._active_bisect_dialog = None
            self._release_after_bisect(dlg, server.id)
        self._refresh()

    def _release_after_bisect(self, dlg, server_id: str) -> None:
        """Releases the automation lock -- but only once the bisect
        worker has ACTUALLY finished. If closing the dialog outlasted
        its wait for the worker's cancel-and-restore sequence, the
        worker is still stopping/relaunching the server; unlocking now
        would let the watchdog and scheduler act on it mid-sequence."""
        worker = getattr(dlg, "_worker", None) if dlg is not None else None
        still_running = worker is not None and worker.isRunning()

        def unlock(_=None, sid=server_id):
            if self.unlock_server_for_automation:
                self.unlock_server_for_automation(sid)

        if still_running:
            worker.finished.connect(unlock)
        else:
            unlock()
        if dlg is not None:
            self._dispose_dialog(dlg, worker)

    def _open_bisect(self) -> None:
        if not self.server or not self.server.mods:
            QMessageBox.information(self, "No mods", "Add some mods first.")
            return
        if not any(m.get("enabled", True) for m in self.server.mods):
            QMessageBox.information(
                self, "No enabled mods",
                "Every mod is already disabled, so there's nothing for a bisect to narrow down. "
                "Enable the mods you want to check first.",
            )
            return
        server = self.server
        # Locked for the dialog's whole lifetime, same as auto-bisect:
        # otherwise the watchdog would "rescue" a test configuration
        # that doesn't start, and a scheduled restart could fire
        # between the person's own test restarts.
        if self.lock_server_for_automation:
            self.lock_server_for_automation(server.id)
        dlg = None
        try:
            dlg = BisectDialog(server, self.on_changed, self, restart_server=self.restart_server)
            dlg.exec()
        finally:
            if self.unlock_server_for_automation:
                self.unlock_server_for_automation(server.id)
            if dlg is not None:
                self._dispose_dialog(dlg)
        self._refresh()

    def _download_mods(self) -> None:
        if not self.server or self._download_worker:
            return
        if not self.server.steamcmd_dir:
            QMessageBox.warning(self, "SteamCMD not configured", "Set the SteamCMD folder for this server first (Updates page).")
            return
        # Every mod, not just currently-enabled ones -- a disabled mod
        # re-enabled later should already be sitting there ready to
        # go, not silently missing because it was off the one time
        # anyone clicked Download.
        ids = [m["id"] for m in self.server.mods]
        if not ids:
            QMessageBox.information(self, "No mods", "There are no mods to download.")
            return
        server = self.server
        worker = ModDownloadWorker(server.steamcmd_dir, ids)
        if self.claim_mod_refresh_slot and not self.claim_mod_refresh_slot(server.id, worker):
            QMessageBox.information(
                self, "Already checking mods",
                "ConanOps is already refreshing this server's mods in the background (the daily "
                "automatic check, most likely). Try again in a moment.",
            )
            return
        self._download_worker = worker
        self._download_worker_server_id = server.id
        self.download_btn.setEnabled(False)
        self.download_btn.setText("Downloading…")
        self._download_worker.finished_download.connect(lambda result, srv=server: self._on_download_finished(srv, result))
        # Keep a reference until the QThread itself has exited -- see
        # _retiring_download_workers in __init__.
        self._retiring_download_workers.append(worker)
        worker.finished.connect(
            lambda w=worker: self._retiring_download_workers.remove(w) if w in self._retiring_download_workers else None
        )
        self._download_worker.start()

    def _on_download_finished(self, server: ServerConfig, result) -> None:
        self._download_worker = None
        self._download_worker_server_id = None
        if self.release_mod_refresh_slot:
            self.release_mod_refresh_slot(server.id)
        # Rewrite modlist.txt now that the real .pak files exist (or may
        # have been renamed by an update). Until a mod is downloaded,
        # write_modlist can only guess its path, and that guess is
        # always wrong -- without this, a freshly added mod stayed
        # pointed at a file that doesn't exist until some unrelated mod
        # edit happened to rewrite the file. Done for whichever server
        # this download was for, even if another one is showing now.
        if self.on_changed:
            self.on_changed(server)
        # Refresh for whatever's showing: this server's per-mod status
        # may have changed, and the Download button is free again either way.
        self._refresh()
        # Report the result even if the person has switched to another
        # server in the meantime -- a failed download used to go
        # completely unreported in that case.
        which = "" if self.server is server else f" for {server.name or 'another server'}"
        if result.success:
            QMessageBox.information(self, "Mods downloaded", f"All mods{which} were downloaded/updated successfully.")
        else:
            QMessageBox.warning(
                self, "Download had problems",
                f"One or more mods{which} failed to download:\n\n" + (result.output[-2000:] if result.output else "(no details)") +
                "\n\nThe server may fail to start, or start without those mods, until this is resolved.",
            )


class BisectDialog(QDialog):
    """Walks the person through disabling half the remaining candidate
    mods, restarting to test, and reporting back -- narrowing down to
    the one broken mod in log2(n) rounds instead of testing one at a
    time.

    Unlike auto_bisect_runner.py's automated version, this dialog
    never restarts the server itself -- the PERSON does, by hand,
    between rounds -- so it can't reset the world save before every
    individual test the way the automated one does. What it CAN do,
    and does: snapshot the world save once when the dialog opens, and
    restore that snapshot once the dialog is dismissed (Restore, or
    closing before reaching a conclusion), so the real world ends up
    exactly as it was before this bisect ever started rather than
    reflecting whatever state the person's own manual restarts left it
    in. A concluded bisect (a culprit found, or ruled out) is treated
    as a real result and left as-is, same as the mod list already was
    -- the world-save reset only ever discards an ABANDONED run's
    test-related changes, never a completed one's."""

    def __init__(self, server: ServerConfig, on_changed=None, parent=None, restart_server=None):
        super().__init__(parent)
        self.server = server
        # callable(ServerConfig) -> None, MainWindow's normal restart.
        # This dialog is modal, so the Dashboard's own Restart button
        # can't be reached while it's open -- the person needs a way
        # to do the restart each round asks for from right here.
        self.restart_server = restart_server
        # Taken as a constructor argument (not set on the instance after
        # the fact) specifically so it's in place BEFORE
        # _apply_current_test() runs at the end of __init__ below.
        # Previously it was assigned by the caller right after
        # construction (dlg = BisectDialog(...); dlg.on_changed = ...),
        # which is too late: __init__ had already run its first
        # _apply_current_test() against on_changed=None, so round 1's
        # disables were computed and shown in the UI but never actually
        # written to modlist.txt -- the whole bisect was silently
        # testing against the wrong (previous) mod list until the
        # second round.
        self.on_changed = on_changed
        self.setWindowTitle("Bisect: Find the Broken Mod")
        self.resize(480, 340)

        enabled_ids = [m["id"] for m in server.mods if m.get("enabled", True)]
        self.state = mod_manager.start_bisect(enabled_ids)
        self._original = {m["id"]: m.get("enabled", True) for m in server.mods}
        # Only meaningful if the server happens to be stopped right
        # when this dialog opens -- see the class docstring for why
        # this can't be taken at the "ideal" moment (just before each
        # restart) the way the automated bisect's own snapshot is.
        self._saved_snapshot_dir = None
        snapshot_problem = ""
        server_running = bool(server.install_dir) and process_manager.is_running(server.install_dir)
        if server.install_dir and not server_running:
            try:
                self._saved_snapshot_dir = mod_manager.snapshot_world_save(server.install_dir)
            except mod_manager.WorldSaveError as e:
                snapshot_problem = str(e)

        layout = QVBoxLayout(self)
        self.info_label = QLabel("")
        self.info_label.setWordWrap(True)
        layout.addWidget(self.info_label)

        if self._saved_snapshot_dir:
            note_text = (
                "The world save has been snapshotted -- if you close this without reaching a "
                "conclusion, or use Restore below, it'll be reset to exactly how it is right now."
            )
        elif snapshot_problem:
            note_text = (
                f"{snapshot_problem}. Any items or buildings a mod removes during testing won't be "
                f"automatically restored if you abandon this bisect -- consider making a backup first."
            )
        elif server_running:
            note_text = (
                "The server was running when this opened, so its world save couldn't be safely "
                "snapshotted -- any items or buildings a mod removes during testing won't be "
                "automatically restored if you abandon this bisect."
            )
        else:
            note_text = "There's no world save yet, so there's nothing to snapshot or restore."
        snapshot_note = QLabel(note_text)
        snapshot_note.setObjectName("Dim")
        snapshot_note.setWordWrap(True)
        layout.addWidget(snapshot_note)

        btn_row = QHBoxLayout()
        self.still_broken_btn = QPushButton("Still broken after restart")
        self.still_broken_btn.clicked.connect(lambda: self._report(True))
        self.fixed_btn = QPushButton("Fixed after restart")
        self.fixed_btn.clicked.connect(lambda: self._report(False))
        btn_row.addWidget(self.still_broken_btn)
        btn_row.addWidget(self.fixed_btn)
        layout.addLayout(btn_row)

        self.restart_btn = QPushButton("Restart Server Now")
        self.restart_btn.setToolTip("Restarts the server with this round's mod list.")
        self.restart_btn.setEnabled(self.restart_server is not None)
        self.restart_btn.clicked.connect(self._restart_now)
        layout.addWidget(self.restart_btn)

        self.restore_btn = QPushButton("Restore original mod list and close")
        self.restore_btn.clicked.connect(self._restore_and_close)
        layout.addWidget(self.restore_btn)

        self._keep_snapshot = False  # True if a world-save restore failed -- see _restore_world_if_wanted
        self._resolved = False  # set True once the person explicitly restores or the bisect finishes
        self._apply_current_test()

    def _handle_dismiss(self) -> None:
        """Shared by every way this dialog can close without the
        Restore button: the window's X (closeEvent) AND Escape
        (reject()). Escape used to skip this entirely -- QDialog's
        default reject() just hides the dialog -- leaving half the
        mods disabled and the world-save snapshot orphaned in %TEMP%.
        If the bisect never reached a conclusion, treat it the same
        as the explicit restore button; if it DID conclude (culprit
        found, or ruled out), that's a real result, so leave it as-is.
        Safe to call more than once (Qt's closeEvent itself calls
        reject(), so the X button reaches this twice)."""
        if not self._resolved and not self.state.done:
            self._restore_and_close(already_closing=True)
        elif self._saved_snapshot_dir and not self._keep_snapshot:
            mod_manager.cleanup_world_save_snapshot(self._saved_snapshot_dir)
            self._saved_snapshot_dir = None

    def closeEvent(self, event) -> None:  # noqa: N802 -- Qt's naming
        self._handle_dismiss()
        super().closeEvent(event)

    def reject(self) -> None:
        self._handle_dismiss()
        super().reject()

    def _names(self, mod_ids) -> str:
        by_id = {m["id"]: m for m in self.server.mods}
        out = []
        for mid in mod_ids:
            name = (by_id.get(mid) or {}).get("name")
            out.append(f"{name} ({mid})" if name and name != mid else mid)
        return ", ".join(out)

    def _restart_now(self) -> None:
        if self.restart_server is None:
            return
        QGuiApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        try:
            self.restart_server(self.server)
        finally:
            QGuiApplication.restoreOverrideCursor()

    def _apply_current_test(self) -> None:
        to_disable = set(self.state.current_test_disabled)
        self.server.mods = mod_manager.apply_bisect_test(self.server.mods, self.state)
        if self.on_changed:
            self.on_changed(self.server)

        self.info_label.setText(
            f"Disabled for this test: {self._names(sorted(to_disable)) if to_disable else '(none -- testing the single remaining candidate)'}\n\n"
            f"Restart the server, let it run for a bit, then report below whether the problem "
            f"is still happening.\n\nCandidates remaining: {len(self.state.remaining)}"
        )

    def _report(self, still_broken: bool) -> None:
        self.state = mod_manager.report_result(self.state, still_broken)
        if self.state.done:
            # Settle the mod list on the actual RESULT, not whatever the
            # last test round left it at: every cleared mod back on, only
            # the culprit (if any) off. Without this, a "ruled out" finish
            # left the final, now-cleared candidate disabled for good.
            self.server.mods = mod_manager.apply_bisect_result(self.server.mods, self.state)
            if self.on_changed:
                self.on_changed(self.server)
            if self.state.culprit:
                self.info_label.setText(
                    f"Found it: mod {self._names([self.state.culprit])} appears to be the cause. It's currently "
                    f"disabled -- every other mod is back on. Restart the server to apply."
                )
            else:
                self.info_label.setText(
                    "Narrowed down to no remaining candidates -- the problem likely isn't one of these mods. "
                    "Every mod that was part of this check is enabled again."
                )
            self.still_broken_btn.setEnabled(False)
            self.fixed_btn.setEnabled(False)
            self.restart_btn.setText("Restart Server Now (apply result)")
            # Concluded -- a real result, not an abandoned run. Leave
            # the world save as it currently is (whatever the person's
            # own manual restarts left it at) rather than resetting it
            # out from under a result they just reached.
            mod_manager.cleanup_world_save_snapshot(self._saved_snapshot_dir)
            self._saved_snapshot_dir = None
        else:
            self._apply_current_test()

    def _restore_and_close(self, already_closing: bool = False) -> None:
        if self._resolved:
            return  # already handled (e.g. X then Qt's own reject() right after)
        self._resolved = True
        for m in self.server.mods:
            if m["id"] in self._original:
                m["enabled"] = self._original[m["id"]]
        if self.on_changed:
            self.on_changed(self.server)
        if self._saved_snapshot_dir:
            self._restore_world_if_wanted()
        if not already_closing:
            self.accept()

    def _restore_world_if_wanted(self) -> None:
        """Puts the world save back to the snapshot, but only after
        asking -- this also undoes anything real players did since the
        bisect started, since manual bisect doesn't keep people off --
        and only with the server actually stopped. Between rounds the
        PERSON restarts the server by hand, so it's usually running by
        the time they close this; on Windows the live .db can't even be
        removed then, and the restore used to fail with nothing but a
        log line after the dialog had already promised a reset."""
        reply = QMessageBox.question(
            self, "Reset World Save?",
            "Put the world save back to exactly how it was when this bisect started?\n\n"
            "This undoes any items or buildings a mod removed during testing -- but also "
            "anything players did on the server since then.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            mod_manager.cleanup_world_save_snapshot(self._saved_snapshot_dir)
            self._saved_snapshot_dir = None
            return

        stopped_for_restore = False
        if process_manager.is_running(self.server.install_dir):
            QGuiApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
            try:
                # Waits for the world to finish saving (up to ~2 min on a
                # big world) without freezing the window.
                srv = self.server
                run_responsive(lambda: process_manager.graceful_stop(srv))
            finally:
                QGuiApplication.restoreOverrideCursor()
            stopped_for_restore = True

        restored = (
            not process_manager.is_running(self.server.install_dir)
            and mod_manager.restore_world_save(self.server.install_dir, self._saved_snapshot_dir)
        )
        if restored:
            mod_manager.cleanup_world_save_snapshot(self._saved_snapshot_dir)
            self._saved_snapshot_dir = None
            if stopped_for_restore:
                QMessageBox.information(
                    self, "World Save Reset",
                    "The server was stopped so the world save could be reset. Start it again when you're ready.",
                )
        else:
            # Keep the snapshot rather than deleting the only good copy.
            self._keep_snapshot = True
            QMessageBox.warning(
                self, "World Save Not Reset",
                "The world save couldn't be reset (the server may still be running). The original "
                f"copy has been kept here so nothing is lost:\n\n{self._saved_snapshot_dir}\n\n"
                "Stop the server and copy those files back into ConanSandbox\\Saved to restore it.",
            )
