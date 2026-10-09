"""Starting, stopping and restarting servers; health checks, the crash watchdog and resuming servers after a reboot.

Part of MainWindow (see ui/main_window.py); split out to keep each file focused."""
from __future__ import annotations

import os
import time
from typing import Optional

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QMessageBox,
)

from models import ServerConfig
import process_manager
import banlist_manager
import health_check_runner
import preflight
import applog
from update_runner import CheckWorker

from ui.workers import keep_until_finished

from ui.window.common import _OpWorker

_log = applog.get_logger('ui.main_window')


class PowerMixin:
    def _handle_restart(self, server: ServerConfig) -> None:
        """Manual restart from the Dashboard: failures show as dialogs."""
        result = preflight.run_preflight(server)
        if not result.ok:
            if self._offer_reinstall_if_files_missing(server) or self._offer_file_fixes(server, result, "restart"):
                return
            QMessageBox.warning(self, "Can't restart", "Pre-flight checks failed:\n\n" + "\n".join(result.problems))
            return
        if result.repairs:
            self._notify(server, "Auto-repaired before restart:\n" + "\n".join(result.repairs), title="Auto-Repair")
        self._warn_then_restart(server, manual=True)

    def _restart_unattended(self, server: ServerConfig) -> None:
        """Scheduled restart: failures go to alerts, never a blocking dialog
        that would sit unattended."""
        result = preflight.run_preflight(server)
        if getattr(result, "files_missing", False) and self._repair_game_files(server, why="game files were missing before a scheduled restart",
                                                             then_start=True, stop_first=True):
            return
        if not result.ok:
            self._notify(server, "Scheduled restart skipped -- pre-flight checks failed:\n" + "\n".join(result.problems), title="Restart Failed")
            return
        if result.repairs:
            self._notify(server, "Auto-repaired before restart:\n" + "\n".join(result.repairs), title="Auto-Repair")
        self._warn_then_restart(server, manual=False)

    def _warn_then_restart(self, server: ServerConfig, manual: bool) -> None:
        """Broadcasts a restart warning to online players, then restarts after
        a short non-blocking grace period. Restarts at once if RCON is off
        or nobody is online."""
        online = bool(self._online_by_server.get(server.id))
        if server.rcon_enabled and online:
            banlist_manager.broadcast_message(
                server, f"Server restarting in {self._RESTART_WARNING_GRACE_SECONDS} seconds...",
            )
            QTimer.singleShot(
                self._RESTART_WARNING_GRACE_SECONDS * 1000,
                lambda: self._finish_restart(server, manual),
            )
        else:
            self._finish_restart(server, manual)

    def _finish_restart(self, server: ServerConfig, manual) -> None:
        """manual: True (a click in the app: show a dialog), "web" (from
        the web version: alert), False (scheduled: alert)."""
        def report(e):
            if manual is True:
                QMessageBox.critical(self, "Restart failed", str(e))
            elif manual == "web":
                self._notify(server, f"Restart failed: {e}", title="Restart Failed")
            else:
                self._notify(server, f"Scheduled restart failed: {e}", title="Restart Failed")
        self._do_restart(server, on_error=report)

    def _do_restart(self, server: ServerConfig, on_error=None) -> None:
        """Shared restart, run off the UI thread. A FileNotFoundError goes to
        on_error; without one it's raised (inline mode) or logged."""
        self._expected_stop.add(server.id)
        self._tracker_for(server).close_all_active()
        self._clear_online(server.id)

        def done(_result, sid=server.id):
            self._known_running[sid] = True
            self._expected_stop.discard(sid)
            self._refresh_chrome()

        def failed(e, sid=server.id):
            self._expected_stop.discard(sid)
            if on_error is not None:
                on_error(e)
            else:
                raise e

        self._run_op(lambda: process_manager.restart(server), on_done=done,
                     on_error=failed if (on_error is not None or not self.RUN_OPS_INLINE) else None)

    def _handle_start(self, server: ServerConfig) -> None:
        """Manual start from the Dashboard: preflight, then launch."""
        if process_manager.is_running(server.install_dir):
            return  # Start button shouldn't even be visible in this case, but don't double-launch
        result = preflight.run_preflight(server)
        if not result.ok:
            if self._offer_reinstall_if_files_missing(server) or self._offer_file_fixes(server, result, "start"):
                return
            QMessageBox.warning(self, "Can't start", "Pre-flight checks failed:\n\n" + "\n".join(result.problems))
            return
        if result.repairs:
            self._notify(server, "Auto-repaired before start:\n" + "\n".join(result.repairs), title="Auto-Repair")
        try:
            self._launch_by_request(server)
        except FileNotFoundError as e:
            QMessageBox.critical(self, "Start failed", str(e))

    def _handle_kick_player(self, server: ServerConfig, name: str) -> None:
        message = banlist_manager.kick_player(server, name)
        self._notify(server, message, title="Player Kicked")

    def _handle_ban_player(self, server: ServerConfig, player_name: str, steam_id: str) -> None:
        message = banlist_manager.ban_player(server, steam_id)
        self._notify(server, f"{player_name} ({steam_id}): {message}", title="Player Banned")
        if self.config.active_server_id == server.id:
            self.access_page.set_server(server)  # refresh the ban list shown there too

    def _handle_stop(self, server: ServerConfig) -> None:
        """Manual stop from the Dashboard (graceful, then hard kill)."""
        if not process_manager.is_running(server.install_dir):
            return  # Stop button shouldn't even be visible in this case
        self._expected_stop.add(server.id)
        self._tracker_for(server).close_all_active()
        self._clear_online(server.id)
        # Keeps it stopped through reboots; set before the off-thread stop so
        # the watchdog can't race it.
        server.desired_running = False
        self.config.save()
        self._watchdog_attempts.pop(server.id, None)
        self._post_update_watch.pop(server.id, None)

        def done(_result, sid=server.id):
            self._known_running[sid] = False
            self._expected_stop.discard(sid)
            self._refresh_chrome()

        self._run_op(lambda: process_manager.graceful_stop(server), on_done=done,
                     on_error=lambda e, sid=server.id: (self._expected_stop.discard(sid),
                                                        _log.error(f"Stop failed: {e}")))

    def _handle_restart_skipped_online(self, server: ServerConfig) -> None:
        self._notify(
            server,
            "Today's scheduled restart window is open, but players are still "
            "online -- skipping today and trying again next time.",
            title="Restart Skipped",
        )

    def _handle_scheduled_restart(self, server: ServerConfig) -> None:
        """A scheduled restart checks for a server update first and updates
        instead of a plain restart if one is available."""
        if server.id in self._update_check_workers or server.id in self._update_apply_workers:
            self._restart_unattended(server)  # an update is already in flight; just restart plainly
            return
        self._notify(server, "Scheduled restart starting -- checking for updates first.", title="Scheduled Restart")
        worker = CheckWorker(server.steamcmd_dir)
        worker.finished_check.connect(
            lambda latest, info, srv=server: (self.updates_page.record_check(srv, latest, info),
                                              self._on_restart_update_check(srv, latest))
        )
        self._update_check_workers[server.id] = worker
        self._retire_worker(worker)
        worker.start()

    def _run_op(self, fn, on_done=None, on_error=None) -> None:
        if self.RUN_OPS_INLINE:
            try:
                result = fn()
            except Exception as e:  # noqa: BLE001
                if on_error is None:
                    raise
                on_error(e)
                return
            if on_done:
                on_done(result)
            return
        worker = _OpWorker(fn)
        if on_done:
            worker.done.connect(on_done)
        worker.failed.connect(on_error if on_error else (lambda e: _log.error(f"Server operation failed: {e}")))
        self._retire_worker(worker)
        worker.start()

    def _start_health_check(self) -> None:
        if self._health_worker is not None:
            return  # a previous check is still running -- skip this tick rather than overlap
        self._health_worker = health_check_runner.HealthCheckWorker(list(self.config.servers), parent=self)
        self._health_worker.finished_check.connect(self._on_health_check_finished)
        worker = self._health_worker
        keep_until_finished(self._retiring_workers, worker)
        self._health_worker.start()

    def _on_health_check_finished(self, results: list) -> None:
        self._health_worker = None
        by_id = {sid: (running, info) for sid, running, info in results}
        for server in self.config.servers:
            if server.id not in by_id:
                continue  # removed from config, or added after this check started -- picked up next tick
            running_now, info = by_id[server.id]
            was_running = self._known_running.get(server.id, False)

            if was_running and not running_now and server.id not in self._expected_stop and server.id not in self._automation_locked:
                self._tracker_for(server).close_all_active()
                self._clear_online(server.id)
                self._notify(server, "The server process stopped unexpectedly (crash or external kill).", title="Server Crashed")
                self._unresponsive_since.pop(server.id, None)
                self._known_running[server.id] = False
                if self._post_update_failure(server, "crashed"):
                    continue
                if server.desired_running and not server.update_hold:
                    self._attempt_watchdog_restart(server, reason="it crashed", need_stop_first=False)
                continue

            if running_now and server.id not in self._expected_stop and server.id not in self._automation_locked:
                self._known_running[server.id] = True
                self._track_hung_state(server, info)
                continue

            self._unresponsive_since.pop(server.id, None)
            self._known_running[server.id] = running_now

    def _track_hung_state(self, server: ServerConfig, info: Optional[dict]) -> None:
        """Bookkeeping only; no I/O (info was fetched by HealthCheckWorker)."""
        if info is not None:
            self._unresponsive_since.pop(server.id, None)
            return
        now = time.monotonic()
        since = self._unresponsive_since.get(server.id)
        if since is None:
            self._unresponsive_since[server.id] = now
            return
        if now - since < self._HUNG_THRESHOLD_SECONDS:
            return
        self._unresponsive_since.pop(server.id, None)
        if self._post_update_failure(server, "stopped responding"):
            return
        if server.desired_running and not server.update_hold:
            self._attempt_watchdog_restart(server, reason="it stopped responding to queries", need_stop_first=True)

    def _attempt_watchdog_restart(self, server: ServerConfig, reason: str, need_stop_first: bool) -> None:
        """Recovery for a server with desired_running. need_stop_first is True
        for a hang (stop before relaunch), False for a crash."""
        now = time.monotonic()
        last_attempt = self._watchdog_last_attempt.get(server.id)
        if last_attempt is not None and now - last_attempt > self._WATCHDOG_STREAK_RESET_SECONDS:
            self._watchdog_attempts[server.id] = 0
            self._files_repaired.discard(server.id)
        attempts = self._watchdog_attempts.get(server.id, 0)
        if attempts >= self._WATCHDOG_MAX_ATTEMPTS:
            # Out of restarts: if it has mods, test whether one is the cause.
            if any(m.get("enabled", True) for m in server.mods):
                self._start_mod_recovery(server, reason.replace("it ", "", 1) if reason.startswith("it ") else reason)
            return  # already gave up and alerted this streak -- see the bottom of this method
        if last_attempt is not None:
            delay = self._WATCHDOG_BACKOFF_SECONDS[min(attempts, len(self._WATCHDOG_BACKOFF_SECONDS) - 1)]
            if now - last_attempt < delay:
                return  # still waiting out the backoff before trying again

        self._watchdog_last_attempt[server.id] = now
        self._watchdog_attempts[server.id] = attempts + 1
        attempt_num = attempts + 1

        result = preflight.run_preflight(server)
        # Game files missing, or two plain restarts didn't help: have Steam check
        # and re-download damaged or missing game files, once per streak.
        if (getattr(result, "files_missing", False) or (result.ok and attempt_num >= 3)) and server.id not in self._files_repaired:
            if need_stop_first:
                self._expected_stop.add(server.id)
                self._tracker_for(server).close_all_active()
                self._clear_online(server.id)
            self._repair_game_files(server, why=reason, then_start=True, stop_first=need_stop_first)
            return
        if not result.ok:
            self._notify(
                server,
                f"Watchdog couldn't restart this server ({reason}) -- pre-flight checks failed:\n" + "\n".join(result.problems),
                title="Watchdog Restart Failed",
            )
            return
        if result.repairs:
            self._notify(server, "Auto-repaired before watchdog restart:\n" + "\n".join(result.repairs), title="Auto-Repair")

        if need_stop_first:
            self._expected_stop.add(server.id)
            self._tracker_for(server).close_all_active()
            self._clear_online(server.id)

        def work():
            if need_stop_first:
                process_manager.graceful_stop(server)
            process_manager.launch(server)

        def done(_result, sid=server.id):
            self._known_running[sid] = True
            if need_stop_first:
                self._expected_stop.discard(sid)
            self._notify(
                server,
                f"Watchdog restarted this server ({reason}) -- attempt {attempt_num} of "
                f"{self._WATCHDOG_MAX_ATTEMPTS} this session.",
                title="Watchdog Restart",
            )

        def failed(e, sid=server.id):
            if need_stop_first:
                self._expected_stop.discard(sid)
            self._notify(server, f"Watchdog couldn't restart this server ({reason}): {e}", title="Watchdog Restart Failed")

        # Off the UI thread: a hang restart waits for the world to save first.
        self._run_op(work, on_done=done, on_error=failed)

        if self._watchdog_attempts.get(server.id, 0) >= self._WATCHDOG_MAX_ATTEMPTS:
            self._notify(
                server,
                f"This server has needed restarting {self._WATCHDOG_MAX_ATTEMPTS} times in a row -- "
                f"ConanOps will stop trying to restart it automatically. Check the Console and "
                f"Diagnostics tabs for what's actually wrong, then start it manually from the "
                f"Dashboard once that's fixed (which also resets this counter).",
                title="Watchdog Giving Up",
            )

    def _network_not_ready_for_resume(self) -> bool:
        """True shortly after boot if a saved bind IP isn't on any adapter yet
        or no real (non-APIPA) address exists."""
        import network_utils
        try:
            import psutil
            if time.time() - psutil.boot_time() > self._RESUME_BOOT_WINDOW_SECONDS:
                return False
        except Exception:  # noqa: BLE001
            return False
        resuming = [s for s in self.config.servers if s.desired_running and s.install_dir and not s.update_hold]
        if not resuming:
            return False
        local = {ip for ip in network_utils.list_local_ipv4s() if network_utils.is_usable_lan_ipv4(ip)}
        if not local:
            return True
        return any(s.bind_ip and s.bind_ip not in local for s in resuming)

    def _auto_resume_servers(self) -> None:
        """At startup, starts every server with desired_running that isn't
        running. Failures go to alerts, not dialogs (nobody may be there)."""
        if self._network_not_ready_for_resume():
            # DHCP still pending: wait (up to ~3 min) so preflight doesn't
            # overwrite a good bind IP as "stale".
            self._resume_network_waits = getattr(self, "_resume_network_waits", 0) + 1
            if self._resume_network_waits <= self._RESUME_NETWORK_MAX_WAITS:
                QTimer.singleShot(self._RESUME_NETWORK_WAIT_MS, self._auto_resume_servers)
                return
            _log.warning("Network still not ready after waiting; auto-resuming anyway.")
        for server in self.config.servers:
            if not server.desired_running or not server.install_dir:
                continue
            if server.update_hold:
                self._notify(server, f"Not starting this server: it's being held after a failed update. "
                                     f"{server.update_hold} Click Start to run it anyway.", title="Server Held")
                continue
            if process_manager.is_running(server.install_dir):
                continue  # already running somehow (e.g. survived from before this ConanOps session)
            result = preflight.run_preflight(server)
            if getattr(result, "files_missing", False) and self._repair_game_files(
                    server, why="game files were missing when starting it after a reboot", then_start=True):
                continue
            if not result.ok:
                self._notify(
                    server,
                    "Auto-resume couldn't start this server -- pre-flight checks failed:\n" + "\n".join(result.problems),
                    title="Auto-Resume Failed",
                )
                continue
            if result.repairs:
                self._notify(server, "Auto-repaired before auto-resume:\n" + "\n".join(result.repairs), title="Auto-Repair")
            try:
                process_manager.launch(server)
                self._known_running[server.id] = True
            except FileNotFoundError as e:
                self._notify(server, f"Auto-resume couldn't start this server: {e}", title="Auto-Resume Failed")

    # ------------------------------------------------ missing-file repair --
    def _repair_game_files(self, server: ServerConfig, why: str, then_start: bool, stop_first: bool = False) -> bool:
        """Has Steam check the server's game files and re-download anything
        missing or damaged (SteamCMD validate). Never touches the world,
        settings or mods. Starts the server afterwards if then_start."""
        from update_runner import UpdateWorker
        if self._is_update_busy(server) or server.id in self._automation_locked:
            return False
        self._files_repaired.add(server.id)
        self._automation_locked.add(server.id)
        self._notify(server, f"Checking the server's game files with Steam ({why}) -- anything missing or damaged "
                             f"is downloaded again. Your world, settings and mods aren't touched.", title="Repairing Files")
        worker = UpdateWorker(server.steamcmd_dir, server.install_dir, stop_server=server if stop_first else None)
        self._update_apply_workers[server.id] = worker

        def finished(res, srv=server):
            self._update_apply_workers.pop(srv.id, None)
            self._automation_locked.discard(srv.id)
            self._expected_stop.discard(srv.id)
            if not res.success:
                self._notify(srv, "Couldn't repair the game files: " + (res.output or "SteamCMD failed").strip()[-300:],
                             title="Repair Failed")
                return
            if not then_start:
                self._notify(srv, "Game files checked and repaired.", title="Files Repaired")
                return
            check = preflight.run_preflight(srv)
            if not check.ok:
                self._notify(srv, "Repaired the game files, but the server still can't start:\n" + "\n".join(check.problems),
                             title="Repair Done -- Still Not Starting")
                return

            def started(_r, sid=srv.id):
                self._known_running[sid] = True
                self._notify(srv, "Game files checked and repaired, and the server was started again.",
                             title="Files Repaired")
            self._run_op(lambda: process_manager.launch(srv), on_done=started,
                         on_error=lambda e: self._notify(srv, f"Repaired the files, but starting failed: {e}",
                                                         title="Start Failed"))
        worker.finished_update.connect(finished)
        self._retire_worker(worker)
        worker.start()
        return True

    def _offer_file_fixes(self, server: ServerConfig, result, action: str) -> bool:
        """Manual start/restart: offers the fix for missing game files or a
        missing world save. True if it handled the situation."""
        if getattr(result, "world_missing", False):
            import backup_manager
            backups = backup_manager.list_backups(server.backup_destination)
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle("World save missing")
            box.setText(f"The world save for \"{server.name}\" is missing. Starting now would create a brand-new, "
                        f"empty world.\n\nThe latest backup is from {backups[0].when:%b %d, %I:%M %p}.")
            restore = box.addButton("Restore Latest Backup", QMessageBox.AcceptRole)
            fresh = box.addButton("Start a New World", QMessageBox.DestructiveRole)
            box.addButton(QMessageBox.Cancel)
            box.exec()
            if box.clickedButton() is restore:
                self._handle_restore(server, backups[0])
                self._notify(server, "Restoring the latest backup. Start the server once it's done.", title="Restoring")
            elif box.clickedButton() is fresh:
                server._allow_new_world = True
                try:
                    (self._handle_start if action == "start" else self._handle_restart)(server)
                finally:
                    server._allow_new_world = False
            return True
        if getattr(result, "files_missing", False):
            answer = QMessageBox.question(
                self, "Game files missing",
                f"Some of \"{server.name}\"'s game files are missing or damaged.\n\nHave Steam check them and download "
                f"what's missing, then start the server? Your world, settings and mods aren't touched.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if answer == QMessageBox.Yes:
                self._files_repaired.discard(server.id)
                if not self._repair_game_files(server, why="requested", then_start=True):
                    QMessageBox.information(self, "Busy", "An update or another job is running for this server -- try again in a minute.")
            return True
        return False

    def _offer_reinstall_if_files_missing(self, server: ServerConfig) -> bool:
        """If a server's files are gone, offers to reinstall via the setup
        wizard. True if the offer was shown."""
        if not server.install_dir or os.path.isdir(server.install_dir):
            return False
        answer = QMessageBox.question(
            self, "Server files are missing",
            f"The files for \"{server.name}\" aren't on this PC anymore -- they were deleted or moved.\n\n"
            f"Download them again now? Your settings for this server are kept.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if answer == QMessageBox.Yes:
            self._open_setup_wizard(server)
        return True
