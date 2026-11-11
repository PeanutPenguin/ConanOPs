"""Workshop mod auto-update: every hour, asks the Workshop whether any of a
server's mods has a newer version than the one downloaded. If so, a stopped
server just gets the new files; a running one warns players (see
countdown.py), stops, downloads and starts again, so people can keep joining
once their game has the new version too.

Part of MainWindow (see ui/main_window.py)."""
from __future__ import annotations

import time
from typing import Dict, List, Tuple

import applog
import mod_versions
import process_manager
from models import ServerConfig

_log = applog.get_logger('ui.main_window')

# After a failed mod update, don't restart for mods again for a while:
# otherwise players would be warned and kicked every hour for nothing.
MOD_UPDATE_BACKOFF_SECONDS = 6 * 3600


def mod_names(server: ServerConfig, ids) -> str:
    names = {m["id"]: (m.get("name") or m["id"]) for m in server.mods}
    shown = [names.get(i, i) for i in ids]
    if len(shown) > 4:
        return ", ".join(shown[:4]) + f" and {len(shown) - 4} more"
    return ", ".join(shown)


class ModWatchMixin:
    def _init_mod_watch(self) -> None:
        self._mod_watch_workers: Dict[str, object] = {}
        self._mod_update_pending: Dict[str, List[Tuple[str, int]]] = {}
        self._mod_update_restarting: set = set()
        self._mod_update_backoff: Dict[str, float] = {}

    def _handle_mod_watch_due(self, server: ServerConfig) -> None:
        if server.id in self._mod_watch_workers or self.server_busy(server):
            return
        ids = [m["id"] for m in server.mods if m.get("enabled", True)]
        if not ids:
            return
        from workshop_search_runner import ModStatusWorker
        import steam_workshop_api as swa
        worker = ModStatusWorker(ids, api_key=self.config.steam_api_key,
                                 cutoff_ts=swa.cutoff_timestamp(self.config.workshop_update_cutoff))
        worker.finished_status.connect(lambda result, srv=server: self._on_mod_watch_result(srv, result))
        self._mod_watch_workers[server.id] = worker
        self._retire_worker(worker)
        worker.start()

    def _on_mod_watch_result(self, server: ServerConfig, result) -> None:
        self._mod_watch_workers.pop(server.id, None)
        if server not in self.config.servers or not server.mod_auto_update:
            return
        if not getattr(result, "ok", False):
            _log.info(f"Mod update check for {server.name} didn't get an answer: {getattr(result, 'error', '')}")
            return
        ids = [m["id"] for m in server.mods if m.get("enabled", True)]
        newer = mod_versions.newer_on_workshop(server.steamcmd_dir, ids, result.items or {})
        if not newer:
            return
        if time.monotonic() < self._mod_update_backoff.get(server.id, 0.0):
            return
        self._mod_update_pending[server.id] = newer
        if self.server_busy(server) or server.id in self._countdowns:
            return  # tried again next hour
        self._with_warning(server, "mod update", lambda srv=server: self._mod_update_now(srv))

    def _mod_update_now(self, server: ServerConfig) -> None:
        newer = self._mod_update_pending.get(server.id)
        if not newer or server not in self.config.servers:
            return
        if self.server_busy(server):
            return  # something else got there first; the next check tries again
        names = mod_names(server, [wid for wid, _t in newer])
        running = bool(server.install_dir) and process_manager.is_running(server.install_dir)
        self._mod_update_restarting.add(server.id)
        if not running:
            self._update_mods_then_relaunch(server, False)
            return
        self._notify(server, f"New Workshop version of {names} -- restarting the server to load it.",
                     title="Mod Update")
        self._expected_stop.add(server.id)
        self._tracker_for(server).close_all_active()
        self._clear_online(server.id)
        self._known_running[server.id] = False

        def stopped(ok, srv=server):
            self._expected_stop.discard(srv.id)
            if ok is False:
                self._mod_update_restarting.discard(srv.id)
                self._known_running[srv.id] = True
                self._notify(srv, f"The mod update didn't happen: {process_manager.STILL_RUNNING_MESSAGE}",
                             title="Mod Update Failed")
                return
            self._update_mods_then_relaunch(srv, True, watch=True)

        def failed(e, srv=server):
            self._expected_stop.discard(srv.id)
            self._mod_update_restarting.discard(srv.id)
            self._known_running[srv.id] = process_manager.is_running(srv.install_dir)
            self._notify(srv, f"The mod update didn't happen: {e}", title="Mod Update Failed")
        self._run_op(lambda: process_manager.graceful_stop(server), on_done=stopped, on_error=failed)
        self._refresh_chrome()

    def _after_mod_download(self, server: ServerConfig, success: bool) -> None:
        """Called by every mod download that finishes (see updates.py)."""
        pending = self._mod_update_pending.pop(server.id, None) if success else None
        if success:
            for wid, ts in pending or []:
                server.mod_versions[wid] = ts
        if server.id not in self._mod_update_restarting:
            return
        self._mod_update_restarting.discard(server.id)
        if success:
            self._mod_update_backoff.pop(server.id, None)
            self._record_event(server, "mod_update", mod_names(server, [w for w, _t in pending or []]))
        else:
            self._mod_update_backoff[server.id] = time.monotonic() + MOD_UPDATE_BACKOFF_SECONDS
            self._record_event(server, "update_failed", "Mod download failed")
