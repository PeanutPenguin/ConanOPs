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
        self._mod_alerted: set = set()  # (server id, what, mod id[, version]) already told about this session
        self._recent_mod_change: Dict[str, Tuple[float, List[str]]] = {}  # server id -> (when, mods just updated)

    def _handle_mod_watch_due(self, server: ServerConfig) -> None:
        if server.id in self._mod_watch_workers or self.server_busy(server):
            return
        ids = [m["id"] for m in server.mods if m.get("enabled", True)]
        if not ids:
            return
        from workshop_search_runner import ModStatusWorker
        import steam_workshop_api as swa
        worker = ModStatusWorker(ids, api_key=self.config.steam_api_key,
                                 cutoff_ts=swa.cutoff_timestamp(self.config.mod_cutoff()))
        worker.finished_status.connect(lambda result, srv=server: self._on_mod_watch_result(srv, result))
        self._mod_watch_workers[server.id] = worker
        self._retire_worker(worker)
        worker.start()

    def _on_mod_watch_result(self, server: ServerConfig, result) -> None:
        self._mod_watch_workers.pop(server.id, None)
        if server not in self.config.servers:
            return
        if not getattr(result, "ok", False):
            _log.info(f"Mod update check for {server.name} didn't get an answer: {getattr(result, 'error', '')}")
            return
        ids = [m["id"] for m in server.mods if m.get("enabled", True)]
        self._alert_removed_mods(server, ids, result.items or {})
        newer = mod_versions.newer_on_workshop(server.steamcmd_dir, ids, result.items or {})
        if not newer:
            return
        if not server.mod_auto_update:
            self._tell_about_newer_mods(server, newer)
            return
        if time.monotonic() < self._mod_update_backoff.get(server.id, 0.0):
            return
        self._mod_update_pending[server.id] = newer
        if self.server_busy(server) or server.id in self._countdowns:
            return  # tried again next hour
        self._with_warning(server, "mod update", lambda srv=server: self._mod_update_now(srv))

    def _alert_removed_mods(self, server: ServerConfig, ids: List[str], items: dict) -> None:
        """A mod the Workshop no longer lists can't be downloaded by players,
        so they can't join: say so once, not every few minutes."""
        gone = [wid for wid in ids if wid not in items and (server.id, "gone", wid) not in self._mod_alerted]
        if not gone:
            return
        self._mod_alerted.update((server.id, "gone", wid) for wid in gone)
        self._notify(server, f"No longer on the Steam Workshop (removed or made private): {mod_names(server, gone)}. "
                             "Players can't download it any more, so new players can't join while it's enabled. "
                             "Don't just switch it off: anything it added is deleted from the world when the "
                             "server saves without it.", title="Mod Removed From Workshop")

    def _tell_about_newer_mods(self, server: ServerConfig, newer: List[Tuple[str, int]]) -> None:
        """Automatic mod updates are off: no restart, but the owner should know."""
        fresh = [(wid, ts) for wid, ts in newer if (server.id, "newer", wid, ts) not in self._mod_alerted]
        if not fresh:
            return
        self._mod_alerted.update((server.id, "newer", wid, ts) for wid, ts in fresh)
        self._notify(server, f"New Workshop version of {mod_names(server, [w for w, _t in fresh])}. Players' games "
                             "take it straight away, and can't join until the server has it too: restart the "
                             "server (it downloads mods as it starts), or turn on \"Update Workshop mods "
                             "automatically\".", title="Mod Update Waiting")

    def _record_mod_proof(self, server: ServerConfig) -> None:
        """The server has started and answered: note, for each enabled mod,
        that this version ran on this game build (mod_compat.py)."""
        import mod_compat
        build = server.installed_buildid
        ids = [m["id"] for m in server.mods if m.get("enabled", True)]
        if not build or not ids or not server.steamcmd_dir:
            return

        def store(times, srv=server):
            if srv not in self.config.servers:
                return
            listed = {m["id"] for m in srv.mods}
            proof = {wid: p for wid, p in srv.mod_proof.items() if wid in listed}
            for wid, t in (times or {}).items():
                if t:
                    proof[wid] = mod_compat.proof_for(build, t, True)
            if proof != srv.mod_proof:
                srv.mod_proof = proof
                self.config.save()
                page = getattr(self, "mods_page", None)
                if page is not None and self.config.active_server_id == srv.id:
                    page._refresh()
        self._run_op(lambda d=server.steamcmd_dir: mod_versions.installed_times(d, ids), on_done=store,
                     on_error=lambda e: _log.info(f"Couldn't note which mod versions {server.name} started with: {e}"))

    # How long after a mod update a failing server is put down to that update.
    MOD_CHANGE_BLAME_SECONDS = 3600

    def _blame_recent_mod_change(self, server: ServerConfig, what: str) -> str:
        """Called when a server keeps failing right after an update. If mods
        had just been updated, those are the likeliest cause: record that
        they failed on this build and name them. Returns their names ("" if
        no mod had just changed)."""
        import mod_compat
        when, changed = self._recent_mod_change.pop(server.id, (0.0, []))
        if not changed or time.monotonic() - when > self.MOD_CHANGE_BLAME_SECONDS:
            return ""
        for wid in changed:
            server.mod_proof[wid] = mod_compat.proof_for(server.installed_buildid, server.mod_versions.get(wid), False)
        self.config.save()
        names = mod_names(server, changed)
        self._notify(server, f"{server.name} {what} repeatedly right after a mod update: {names}. That update is "
                             "the most likely cause. The mod is marked \"Failed on this build\" on the Mods "
                             "page. Don't just switch it off: anything it added is deleted from the world when "
                             "the server saves without it. Check the mod's Workshop page for a fix, or use "
                             "Mods → Find all bad mods.", title="Mod Update Broke the Server")
        return names

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
            if pending:
                self._recent_mod_change[server.id] = (time.monotonic(), [w for w, _t in pending])
            self._record_event(server, "mod_update", mod_names(server, [w for w, _t in pending or []]))
        else:
            self._mod_update_backoff[server.id] = time.monotonic() + MOD_UPDATE_BACKOFF_SECONDS
            self._record_event(server, "update_failed", "Mod download failed")
