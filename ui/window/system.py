"""ConanOps itself: app updates, Windows update restarts, admin rights, the web link, deleting ConanOps.

Part of MainWindow (see ui/main_window.py); split out to keep each file focused."""
from __future__ import annotations

import proc_utils
import os
import subprocess
import sys
import time
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QMessageBox, QSystemTrayIcon, QApplication,
)

import conanops_paths
import process_manager
import powershell
import startup_registration
import keep_alive
import cleanup
import removal_runner
import vcredist
import power
import windows_update
import webhooks
import applog
import self_delete


from ui.window.common import APP_INSTALL_DIR

_log = applog.get_logger('ui.main_window')


class SystemMixin:
    def _maybe_check_app_update(self) -> None:
        cfg = self.config
        if not cfg.auto_check_app_updates:
            return
        if time.time() - cfg.last_app_update_check < self._APP_UPDATE_CHECK_INTERVAL_SECONDS:
            return
        cfg.last_app_update_check = time.time()
        cfg.save()
        self.app_settings_page.check_for_updates(automatic=True)

    def _on_app_update_available(self, info) -> None:
        if self.tray_icon and self.tray_icon.isVisible():
            self.tray_icon.showMessage(
                "ConanOps update available",
                f"Version {info.version} is out. Open App Settings to install it.",
                QSystemTrayIcon.Information, 8000,
            )

    def _busy_reason_for_app_update(self) -> str:
        """Why restarting ConanOps now would interrupt something, or "".
        Running servers don't count; closing ConanOps never stops them."""
        if self._update_apply_workers:
            return "a server update"
        if self._restore_workers:
            return "a backup restore"
        if self._mod_refresh_workers:
            return "a mod download"
        if getattr(self.mods_page, "_active_bisect_dialog", None) is not None:
            return "mod troubleshooting"
        if self._update_restart_started:
            return "the Windows Update restart"
        if self._recovery_workers:
            return "the automatic mod check"
        return ""

    def _unattended_tick(self) -> None:
        try:
            any_running = any(self._known_running.get(s.id) for s in self.config.servers)
            power.set_keep_awake(bool(self.config.keep_pc_awake and any_running))
            self._check_windows_update_restart()
        except Exception as e:  # noqa: BLE001 - a timer slot must never raise
            _log.error(f"Unattended check failed: {e}")

    def _check_windows_update_restart(self) -> None:
        """Restarts the PC for pending Windows updates: only in the chosen
        window, with nobody online and nothing mid-flight, at most daily, and
        not within 2 h of boot (the pending flag can linger and cause a loop)."""
        cfg = self.config
        if not cfg.handle_update_restarts or self._update_restart_started:
            return
        now = datetime.now()
        try:
            from scheduler import _parse_hhmm, is_within_window
            if not is_within_window(now.time(), _parse_hhmm(cfg.update_restart_start),
                                    _parse_hhmm(cfg.update_restart_end)):
                return
        except (ValueError, AttributeError):
            return
        if cfg.last_update_restart == now.strftime("%Y-%m-%d"):
            return
        try:
            import psutil
            if time.time() - psutil.boot_time() < self._UPDATE_RESTART_MIN_UPTIME_SECONDS:
                return
        except Exception:  # noqa: BLE001
            return
        if not windows_update.reboot_pending():
            return
        running = [s for s in cfg.servers if self._known_running.get(s.id)]
        if any(self._online_by_server.get(s.id) for s in running):
            return  # wait for everyone to leave (the window is checked again next minute)
        if (self._update_apply_workers or self._restore_workers or self._mod_refresh_workers
                or getattr(self.mods_page, "_active_bisect_dialog", None) is not None):
            return

        self._update_restart_started = True
        cfg.last_update_restart = now.strftime("%Y-%m-%d")
        cfg.save()
        for s in running:
            self._expected_stop.add(s.id)
            self._tracker_for(s).close_all_active()
            self._notify(s, "Saving and stopping for a Windows Update restart. ConanOps will start it again "
                            "after the PC comes back.", title="Windows Update Restart")
        if not running and cfg.servers:
            self._notify(cfg.servers[0], "Restarting the PC to finish Windows updates.", title="Windows Update Restart")

        def work(servers=tuple(running)):
            for srv in servers:
                try:
                    process_manager.graceful_stop(srv)
                except Exception as e:  # noqa: BLE001 - still restart; the server would be killed anyway
                    _log.error(f"Couldn't stop {srv.name} cleanly before the update restart: {e}")
            if not windows_update.restart_pc(delay_seconds=60):
                raise RuntimeError("Windows refused the restart request (see conanops.log).")

        def failed(e, servers=tuple(running)):
            self._update_restart_started = False
            for srv in servers:
                self._expected_stop.discard(srv.id)
            target = servers[0] if servers else (cfg.servers[0] if cfg.servers else None)
            if target is not None:
                self._notify(target, f"Windows Update restart didn't happen: {e}. The watchdog will bring "
                                     f"stopped servers back.", title="Windows Update Restart Failed")

        self._run_op(work, on_error=failed)

    def _report_app_update_problem(self, text: str) -> None:
        """Reports a failed post-update restart without blocking (nobody may be
        at the PC): a non-modal message plus server alerts."""
        box = QMessageBox(QMessageBox.Warning, "Update Installed", text, QMessageBox.Ok, self)
        box.setModal(False)
        box.setAttribute(Qt.WA_DeleteOnClose)
        box.show()
        target = self.config.get_active()
        if target is not None:
            self._notify(target, text, title="ConanOps Update")

    def _restart_with_admin_rights(self) -> None:
        """"Run with admin rights" was just turned on: start the elevated
        copy (no prompt) and close this one. Servers keep running."""
        import admin_mode
        if not admin_mode.restart_elevated():
            QMessageBox.warning(self, "Couldn't Restart",
                                "ConanOps couldn't start itself with admin rights. Close and reopen it to switch.")
            return
        self._really_quit = True
        self.close()
        lock = getattr(QApplication.instance(), "_conanops_lock", None)
        if lock is not None:
            lock.unlock()  # after closing, so the web port is free for the new copy
        QApplication.instance().quit()

    def _relaunch_after_update(self) -> None:
        """After a successful self-update: spawns a fresh process to load the
        new code, then closes this one."""
        popen_kwargs = {"cwd": APP_INSTALL_DIR, "close_fds": True, "env": proc_utils.child_env()}
        if os.name == "nt":
            # Detach so the new process survives this one exiting.
            popen_kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
            )
        # Frozen: sys.executable is ConanOps itself (no main.py to pass).
        if getattr(sys, "frozen", False):
            relaunch_args = [sys.executable]
        else:
            relaunch_args = [sys.executable, os.path.join(APP_INSTALL_DIR, "main.py")]
        try:
            proc = subprocess.Popen(relaunch_args, **popen_kwargs)
        except OSError as e:
            _log.error(f"Update installed, but couldn't relaunch automatically: {e}")
            self._report_app_update_problem(
                f"The update installed, but ConanOps couldn't restart itself automatically "
                f"({e}). Please close and reopen it yourself.")
            return

        # Release the single-instance lock early so the new process gets it quickly.
        lock = getattr(QApplication.instance(), "_conanops_lock", None)
        if lock is not None:
            lock.unlock()

        # If the new process dies at once, stay open rather than leave none running.
        time.sleep(0.4)
        if proc.poll() is not None:
            _log.error(f"Relaunched process exited immediately (code {proc.returncode}); staying open.")
            self._report_app_update_problem(
                "The update installed, but the new version didn't start up successfully. This window "
                "will stay open so nothing you're running gets interrupted -- please try closing and "
                "reopening ConanOps yourself, or check the update file if that keeps happening.")
            if lock is not None:
                lock.tryLock(1_000)  # we're staying open after all -- reclaim it
            return

        self._really_quit = True
        self.close()

    def _handle_delete_app_only(self, preserve_data: bool = True) -> None:
        """Uninstalls ConanOps via a detached helper (see self_delete.py),
        leaving server data untouched. preserve_data: skip the data folder,
        which may be inside APP_INSTALL_DIR; False from _handle_delete_everything."""
        self._spawn_self_removal(preserve_data)

    def _handle_delete_everything(self) -> None:
        """Removes everything ConanOps added (servers stopped first so worlds
        save), then the program itself after exit. See cleanup.py."""
        uninstall_vc = False
        if vcredist.installed_by_conanops():
            uninstall_vc = QMessageBox.question(
                self, "Remove the Visual C++ runtime too?",
                "ConanOps installed the Microsoft Visual C++ runtime on this PC. Remove it as well?\n\n"
                "Choose No if you've installed other games or programs since then -- some of them may "
                "need it.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
            ) == QMessageBox.Yes

        from PySide6.QtWidgets import QProgressDialog
        progress = QProgressDialog("Getting started…", None, 0, 0, self)
        progress.setWindowTitle("Deleting Everything")
        progress.setCancelButton(None)
        progress.setMinimumDuration(0)
        progress.setWindowModality(Qt.ApplicationModal)
        progress.show()

        for s in self.config.servers:
            self._expected_stop.add(s.id)
            s.desired_running = False
        if hasattr(self.scheduler, "stop"):
            self.scheduler.stop()
        for monitor in list(self._log_monitors.values()):
            monitor.stop()

        worker = removal_runner.DeleteEverythingWorker(self.config, uninstall_vc)
        self._firewall_workers.append(worker)
        self._retire_worker(worker)
        worker.progress.connect(progress.setLabelText)

        def finished(problems, w=worker):
            if w in self._firewall_workers:
                self._firewall_workers.remove(w)
            progress.close()
            if problems:
                QMessageBox.warning(
                    self, "Almost Everything Was Removed",
                    "ConanOps will now close and remove itself, but these couldn't be removed:\n\n" +
                    "\n".join(problems[:15]) + "\n\nYou can delete anything listed above by hand.",
                )
            self._spawn_self_removal(preserve_data=False)

        worker.finished_cleanup.connect(finished)
        self._start_or_run(worker)

    def _apply_keep_running(self) -> None:
        """The setup wizard's "Keep my servers running by themselves" option.
        At most one Windows permission prompt (active hours)."""
        cfg = self.config
        if not cfg.start_with_windows:
            try:
                startup_registration.register()
                cfg.start_with_windows = True
            except OSError as e:
                _log.warning(f"Couldn't turn on start with Windows: {e}")
        cfg.keep_pc_awake = True
        cfg.save()
        want_keep_alive = not cfg.keep_alive_enabled
        want_update_window = not cfg.handle_update_restarts

        def work():
            done = {"keep_alive": False, "update_window": False, "original": None}
            if want_keep_alive:
                done["keep_alive"] = keep_alive.enable()
            if want_update_window:
                hours = windows_update.compute_active_hours(cfg.update_restart_start, cfg.update_restart_end)
                if hours:
                    if not cfg.original_active_hours:
                        done["original"] = windows_update.read_active_hours()
                    done["update_window"] = windows_update.set_active_hours(*hours) == powershell.RUN_OK
            return done

        def finished(done):
            if done.get("original") is not None and not cfg.original_active_hours:
                cfg.original_active_hours = done["original"]
            if done.get("keep_alive"):
                cfg.keep_alive_enabled = True
            if done.get("update_window"):
                cfg.handle_update_restarts = True
            cfg.save()
            if hasattr(self, "app_settings_page"):
                self.app_settings_page.reload_unattended_from_config()

        self._run_op(work, on_done=finished,
                     on_error=lambda e: _log.error(f"Keep-it-running setup failed: {e}"))

    def _sync_web_tunnel(self) -> None:
        """The remote link runs only while the web version is on, a
        password is set and "from anywhere" is ticked."""
        want = (self.config.web_control_enabled and self.web_control.is_running
                and bool(self.config.web_password_hash) and self.config.web_remote_enabled and not self.background)
        if want:
            self.web_tunnel.start(self.web_control.actual_port)
        elif self.web_tunnel.running:
            self.web_tunnel.stop()

    def _on_web_tunnel_changed(self, url: str, status: str) -> None:
        page = getattr(self, "app_settings_page", None)
        if page is not None and hasattr(page, "show_web_remote_status"):
            page.show_web_remote_status(url, status)
        if url and url != getattr(self, "_last_web_url", ""):
            self._last_web_url = url
            # Send the link only to ntfy, never Discord (players can often read it).
            topics = [self.config.alert_ntfy_url] if self.config.alert_ntfy_url else []
            if self.tray_icon and self.tray_icon.isVisible():
                self.tray_icon.showMessage("Web Link", "The from-anywhere web link changed -- see App Settings → "
                                                       "Web Version.", QSystemTrayIcon.Information, 5000)
            if topics:
                import threading
                text = f"New from-anywhere link for the ConanOps web version (password needed): {url}"
                threading.Thread(target=lambda: [webhooks.send_ntfy(t, text, title="ConanOps web link")
                                                 for t in topics], daemon=True).start()

    def _spawn_self_removal(self, preserve_data: bool) -> None:
        """Hands the last step to self_delete.py's helper (ConanOps can't
        delete its own running files) and quits."""
        preserve_name = conanops_paths.APP_DATA_DIRNAME if preserve_data else None
        extra = [] if preserve_data else cleanup.owned_roots() + cleanup.temp_leftovers()
        try:
            keep_alive.disable()  # or the watcher would try to reopen ConanOps
        except Exception as e:  # noqa: BLE001
            _log.warning(f"Couldn't remove the keep-alive task: {e}")
        try:
            self_delete.spawn_self_delete_helper(APP_INSTALL_DIR, preserve_name=preserve_name, extra_paths=extra)
        except OSError as e:
            _log.error(f"Couldn't start the uninstall helper: {e}")
            QMessageBox.critical(self, "Uninstall Failed", f"Couldn't start the uninstall process: {e}")
            return
        self._really_quit = True
        self.close()
