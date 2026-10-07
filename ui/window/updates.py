"""Server build updates (checks, applying, failures and holds) and keeping mods up to date.

Part of MainWindow (see ui/main_window.py); split out to keep each file focused."""
from __future__ import annotations

import time
from datetime import datetime

from PySide6.QtCore import QTimer

from models import ServerConfig
import process_manager
import mod_manager
import applog
from update_runner import CheckWorker, UpdateWorker, ModDownloadWorker



_log = applog.get_logger('ui.main_window')


class UpdatesMixin:
    def _handle_update_applied(self, server: ServerConfig, buildid: str) -> None:
        self.config.save()
        self._notify(server, f"Updated to build {buildid}.", title="Update Applied")
        self._update_failure_streak.pop(server.id, None)  # a success (manual or automatic) clears any building streak

    def _on_restart_update_check(self, server: ServerConfig, latest_buildid) -> None:
        self._update_check_workers.pop(server.id, None)
        if latest_buildid and latest_buildid != server.installed_buildid:
            self._notify(server, f"Update {latest_buildid} found during scheduled restart -- updating now.", title="Scheduled Restart")
            self._start_update_apply(server)
        else:
            self._restart_unattended(server)

    def _is_update_busy(self, server: ServerConfig) -> bool:
        """True if any update check or apply (automatic or manual) is running
        for this server, so they can't race."""
        return (
            server.id in self._update_check_workers
            or server.id in self._update_apply_workers
            or server.id in self._manual_update_in_progress
        )

    def _handle_update_check_due(self, server: ServerConfig) -> None:
        """Periodic auto-update check. A new build is applied at once if
        nobody is online, else left for the next scheduled restart."""
        if self._is_update_busy(server):
            return
        worker = CheckWorker(server.steamcmd_dir)
        worker.finished_check.connect(
            lambda latest, info, srv=server: (self.updates_page.record_check(srv, latest, info),
                                              self._on_periodic_update_check(srv, latest))
        )
        self._update_check_workers[server.id] = worker
        self._retire_worker(worker)
        worker.start()

    def _on_periodic_update_check(self, server: ServerConfig, latest_buildid) -> None:
        self._update_check_workers.pop(server.id, None)
        if not latest_buildid:
            return  # not stamped, so a failed check retries soon
        server.last_update_check_at = datetime.now().isoformat()
        self.config.save()
        if latest_buildid == server.installed_buildid:
            return
        online = bool(self._online_by_server.get(server.id))
        if online:
            return  # leave it pending -- next check, or the next scheduled restart, will catch it
        self._notify(server, f"Update {latest_buildid} detected and nobody's online -- updating now.", title="Auto-Update")
        self._start_update_apply(server)

    def _start_update_apply(self, server: ServerConfig, relaunch_after: bool = False) -> None:
        """Automatic update: UpdateWorker stops the server, backs up the closed
        world, checks disk space and runs SteamCMD off the UI thread.
        relaunch_after: start it afterwards even if it isn't running now."""
        if self._is_update_busy(server):
            return
        running = process_manager.is_running(server.install_dir) if server.install_dir else False
        was_running = running or relaunch_after
        if running:
            self._expected_stop.add(server.id)
            self._tracker_for(server).close_all_active()
            self._clear_online(server.id)
            self._known_running[server.id] = False

        backup = bool(server.backup_before_update and server.backup_destination and server.install_dir)
        worker = UpdateWorker(
            server.steamcmd_dir, server.install_dir,
            stop_server=server if running else None,
            backup_server=server if backup else None,
            backup_destination=server.backup_destination if backup else "",
        )
        worker.finished_update.connect(
            lambda result, srv=server, wr=was_running: self._on_update_apply_finished(srv, result, wr)
        )
        self._update_apply_workers[server.id] = worker
        self._retire_worker(worker)
        worker.start()

    def _on_update_apply_finished(self, server: ServerConfig, result, was_running: bool) -> None:
        self._update_apply_workers.pop(server.id, None)
        self._expected_stop.discard(server.id)

        if result.success:
            server.installed_buildid = result.installed_buildid or server.installed_buildid
            server.update_hold = ""
            self._update_retry_pending.discard(server.id)
            self.config.save()
            self._notify(server, f"Updated to build {server.installed_buildid}.", title="Update Applied")
            self._update_failure_streak.pop(server.id, None)
            self.updates_page.record_result(server, True, automatic=True)
            self._update_mods_then_relaunch(server, was_running, watch=True)
            return

        self.updates_page.record_result(server, False, automatic=True)
        streak = self._update_failure_streak.get(server.id, 0) + 1
        self._update_failure_streak[server.id] = streak
        self._handle_update_failure(server, result, was_running)
        if streak >= self._UPDATE_FAILURE_ALERT_THRESHOLD:
            self._notify(
                server,
                f"Automatic updates have failed {streak} times in a row for this server. ConanOps "
                f"will keep trying, but something likely needs attention -- check SteamCMD, disk "
                f"space, and network connectivity.",
                title="Repeated Update Failures",
            )

    def _handle_update_failure(self, server: ServerConfig, result, was_running: bool) -> None:
        """A failed update never relaunches the old build (players' clients are
        already updated, and files may be half-replaced). update_hold keeps it
        stopped until the retry in _UPDATE_RETRY_MINUTES or a manual Start."""
        reasons = {
            "disk": result.output,
            "backup": result.output,
            "steamcmd": "SteamCMD couldn't finish the update (Steam busy or unreachable, or files "
                        "locked). See the app's console output for SteamCMD's log.",
        }
        why = reasons.get(getattr(result, "reason", ""), "The update didn't finish -- see the app's console output.")
        if not was_running:
            self._notify(server, f"Automatic update failed. {why}", title="Update Failed")
            return
        server.update_hold = why
        self.config.save()
        self._notify(
            server,
            f"Update failed, so the server was left stopped rather than started on the old version "
            f"(players with the updated game couldn't join it). {why} Retrying in "
            f"{self._UPDATE_RETRY_MINUTES} minutes -- or click Start to run the old version anyway.",
            title="Update Failed -- Server Held",
        )
        self._schedule_update_retry(server)

    def _schedule_update_retry(self, server: ServerConfig) -> None:
        if server.id in self._update_retry_pending:
            return
        self._update_retry_pending.add(server.id)

        def retry(sid=server.id):
            self._update_retry_pending.discard(sid)
            srv = next((x for x in self.config.servers if x.id == sid), None)
            if srv is None or not srv.update_hold or self._is_update_busy(srv):
                return  # removed, already resolved (manual Start or update), or busy
            self._start_update_apply(srv, relaunch_after=True)

        QTimer.singleShot(self._UPDATE_RETRY_MINUTES * 60 * 1000, retry)

    def _update_mods_then_relaunch(self, server: ServerConfig, was_running: bool, watch: bool = False) -> None:
        """After any server update, refreshes mods (a server update never
        updates them) and then relaunches."""
        # All mods, so a disabled one isn't stale when re-enabled.
        mod_ids = [m["id"] for m in server.mods]
        if not mod_ids or not server.steamcmd_dir:
            self._relaunch_if_was_running(server, was_running, watch=watch)
            return
        existing = self._mod_refresh_workers.get(server.id)
        if existing is not None:
            # Two downloads into the same Workshop folder could corrupt it;
            # reuse the running one's result.
            existing.finished_download.connect(
                lambda result, srv=server, wr=was_running, w=watch: self._on_post_update_mod_refresh_finished(srv, result, wr, w)
            )
            return
        worker = ModDownloadWorker(server.steamcmd_dir, mod_ids)
        worker.finished_download.connect(
            lambda result, srv=server, wr=was_running, w=watch: self._on_post_update_mod_refresh_finished(srv, result, wr, w)
        )
        self._mod_refresh_workers[server.id] = worker
        self._retire_worker(worker)
        worker.start()

    def _on_post_update_mod_refresh_finished(self, server: ServerConfig, result, was_running: bool, watch: bool = False) -> None:
        self._mod_refresh_workers.pop(server.id, None)
        # Rewrite modlist.txt before relaunch: .pak paths may have changed.
        self._handle_mods_changed(server)
        self.mods_changed_elsewhere(server)
        if not result.success:
            self._notify(
                server,
                f"Mods couldn't be refreshed after the update: {result.output}. The server will still "
                f"restart with whatever mod files it already has -- check the Mods tab.",
                title="Mod Update Failed",
            )
        self._relaunch_if_was_running(server, was_running, watch=watch)

    def _relaunch_if_was_running(self, server: ServerConfig, was_running: bool, watch: bool = False) -> None:
        if not was_running:
            return
        try:
            process_manager.launch(server)
            self._known_running[server.id] = True
            if watch:
                self._begin_post_update_watch(server)
        except FileNotFoundError as e:
            self._notify(server, f"Update finished but relaunch failed: {e}", title="Restart Failed")

    def _begin_post_update_watch(self, server: ServerConfig) -> None:
        self._post_update_watch[server.id] = {"until": time.monotonic() + self._POST_UPDATE_WATCH_SECONDS, "failures": 0}

    def _post_update_failure(self, server: ServerConfig, what: str) -> bool:
        """On a crash or hang. True means the post-update watch took over and
        the caller must NOT run the normal watchdog restart."""
        watch = self._post_update_watch.get(server.id)
        if watch is None:
            return False
        if time.monotonic() > watch["until"]:
            self._post_update_watch.pop(server.id, None)
            return False
        watch["failures"] += 1
        if watch["failures"] < self._POST_UPDATE_FAILURES_BEFORE_HOLD:
            return False  # first one: let the watchdog try once, could be a fluke
        self._post_update_watch.pop(server.id, None)
        self._known_running[server.id] = False
        if self._start_mod_recovery(server, what):
            return True  # the mod check stops the server itself, safely, before testing
        if server.install_dir and process_manager.is_running(server.install_dir):
            self._expected_stop.add(server.id)
            self._run_op(lambda: process_manager.graceful_stop(server),
                         on_done=lambda _r, sid=server.id: self._expected_stop.discard(sid),
                         on_error=lambda _e, sid=server.id: self._expected_stop.discard(sid))
        self._known_running[server.id] = False
        server.update_hold = (f"It {what} repeatedly right after the game update -- most likely a mod that "
                              f"hasn't been updated for the new version.")
        self.config.save()
        self._diagnose_post_update_mods(server, what)
        return True

    def _diagnose_post_update_mods(self, server: ServerConfig, what: str) -> None:
        enabled = [m["id"] for m in server.mods if m.get("enabled", True)]
        base = (f"{server.name} {what} repeatedly right after the game update, so it's been left stopped "
                f"to protect the world. ")
        tail = ("Find the culprit with Mods → Find All Bad Mods (it protects your world while testing). "
                "Don't just switch mods off and start the server: anything a disabled mod added is deleted "
                "from the world when it saves. Click Start to try again anyway.")
        if not enabled:
            self._notify(server, base + "There are no mods enabled, so it may be a problem with the update "
                                        "itself -- try Updates → Back Up & Update Now to re-validate the files. "
                                        "Click Start to try again.", title="Update Broke the Server")
            return
        from workshop_search_runner import ModStatusWorker
        import steam_workshop_api as swa
        worker = ModStatusWorker(enabled, api_key=self.config.steam_api_key,
                                 cutoff_ts=swa.cutoff_timestamp(self.config.workshop_update_cutoff))

        def report(result, srv=server):
            names = {m["id"]: (m.get("name") or m["id"]) for m in srv.mods}
            suspects = []
            if result.ok:
                for mid in enabled:
                    info = result.items.get(mid)
                    if info is None or info.status in (swa.STATUS_STALE, swa.STATUS_LEGACY, swa.STATUS_UNKNOWN):
                        suspects.append(names.get(mid, mid))
            lead = (f"Mods not updated for the current game version: {', '.join(suspects)}. "
                    if suspects else "Couldn't tell which mod from the Workshop alone. ")
            self._notify(srv, base + lead + tail, title="Update Broke the Server")

        worker.finished_status.connect(report)
        self._retire_worker(worker)
        worker.start()

    def _claim_mod_refresh_worker(self, server_id: str, worker) -> bool:
        """Ensures at most one mod download per server's Workshop folder.
        Returns False (registering nothing) if one is already running."""
        if server_id in self._mod_refresh_workers:
            return False
        self._mod_refresh_workers[server_id] = worker
        return True

    def _release_mod_refresh_worker(self, server_id: str) -> None:
        self._mod_refresh_workers.pop(server_id, None)

    def _handle_mod_refresh_due(self, server: ServerConfig) -> None:
        """Periodic mod refresh. Never restarts the server; new files load on
        its next restart."""
        if server.id in self._mod_refresh_workers:
            return  # already busy (an update-triggered refresh, most likely) -- try again next time this fires
        mod_ids = [m["id"] for m in server.mods]  # all mods -- see _update_mods_then_relaunch
        if not mod_ids or not server.steamcmd_dir:
            server.last_mod_check_at = datetime.now().isoformat()
            self.config.save()
            return
        worker = ModDownloadWorker(server.steamcmd_dir, mod_ids)
        worker.finished_download.connect(
            lambda result, srv=server: self._on_periodic_mod_refresh_finished(srv, result)
        )
        self._mod_refresh_workers[server.id] = worker
        self._retire_worker(worker)
        worker.start()

    def _on_periodic_mod_refresh_finished(self, server: ServerConfig, result) -> None:
        self._mod_refresh_workers.pop(server.id, None)
        # Stamped even on failure: this low-urgency check just retries in ~24h.
        server.last_mod_check_at = datetime.now().isoformat()
        self._handle_mods_changed(server)
        self.mods_changed_elsewhere(server)
        if not result.success:
            self._notify(
                server,
                f"Periodic mod-update check failed: {result.output}. Will check again in about a day.",
                title="Mod Check Failed",
            )

    def _stop_before_manual_update(self, server: ServerConfig) -> bool:
        """Before a manual update: marks the server busy and prepares the stop.
        Returns whether it was running, so the caller knows to relaunch."""
        self._manual_update_in_progress.add(server.id)
        was_running = process_manager.is_running(server.install_dir) if server.install_dir else False
        if was_running:
            # UpdateWorker does the actual stop off the UI thread.
            self._expected_stop.add(server.id)
            self._tracker_for(server).close_all_active()
            self._clear_online(server.id)
            self._known_running[server.id] = False
        return was_running

    def _relaunch_after_manual_update(self, server: ServerConfig, was_running: bool, result=None) -> None:
        self._manual_update_in_progress.discard(server.id)
        self._expected_stop.discard(server.id)
        if result is not None and not result.success:
            self._handle_update_failure(server, result, was_running)
            return
        if result is not None and result.success and server.update_hold:
            server.update_hold = ""
            self.config.save()
        self._update_mods_then_relaunch(server, was_running, watch=result is not None)

    def _handle_mods_changed(self, server: ServerConfig) -> None:
        self.config.save()
        if server.install_dir and server.steamcmd_dir:
            mod_manager.write_modlist(server.install_dir, server.steamcmd_dir, server.mods)
