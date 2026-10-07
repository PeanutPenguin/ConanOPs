"""QThread workers for Steam Workshop HTTP calls, kept off the UI thread."""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal

import applog
import steam_workshop_api

_log = applog.get_logger(__name__)


class WorkshopSearchWorker(QThread):
    finished_search = Signal(object)  # steam_workshop_api.SearchResult

    def __init__(self, api_key: str, query: str, page: int = 1, parent=None, *, sort: str = "",
                 show=None, cutoff_ts: int = steam_workshop_api.IRIS_CUTOFF_TIMESTAMP,
                 cursor: str = "*", skip_ids=None):
        super().__init__(parent)
        self.api_key = api_key
        self.query = query
        self.page = page
        self.sort = sort
        self.show = set(show) if show else {steam_workshop_api.STATUS_UPDATED}
        self.cutoff_ts = cutoff_ts
        self.cursor = cursor
        self.skip_ids = set(skip_ids or ())

    def run(self) -> None:
        try:
            result = steam_workshop_api.search(
                self.api_key, self.query, sort=self.sort, show=self.show, cutoff_ts=self.cutoff_ts,
                cursor=self.cursor, skip_ids=self.skip_ids, resolve_children=True, use_cache=True,
            )
        except Exception as e:  # noqa: BLE001 - always emit so the dialog doesn't hang waiting
            _log.error(f"Workshop search failed unexpectedly: {e}")
            result = steam_workshop_api.SearchResult(ok=False, error=f"Search failed unexpectedly: {e}")
        self.finished_search.emit(result)


class ModStatusWorker(QThread):
    """Looks up every mod on a server (steam_workshop_api.get_details)
    so the Mods tab can flag outdated mods and missing required items."""
    finished_status = Signal(object)  # steam_workshop_api.DetailsResult

    def __init__(self, workshop_ids, api_key: str = "", cutoff_ts: int = steam_workshop_api.IRIS_CUTOFF_TIMESTAMP,
                 parent=None):
        super().__init__(parent)
        self.workshop_ids = list(workshop_ids)
        self.api_key = api_key
        self.cutoff_ts = cutoff_ts

    def run(self) -> None:
        try:
            result = steam_workshop_api.get_details(
                self.workshop_ids, api_key=self.api_key, cutoff_ts=self.cutoff_ts, use_cache=True,
            )
        except Exception as e:  # noqa: BLE001
            _log.error(f"Mod status check failed unexpectedly: {e}")
            result = steam_workshop_api.DetailsResult(ok=False, error=f"Mod status check failed unexpectedly: {e}")
        self.finished_status.emit(result)
