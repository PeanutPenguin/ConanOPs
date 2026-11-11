"""Scheduled and pre-update backups, restores, and the low-disk-space check.

Part of MainWindow (see ui/main_window.py); split out to keep each file focused."""
from __future__ import annotations

import os
import shutil
import time
from datetime import datetime


from models import ServerConfig
import process_manager
import backup_manager
import diagnostics
import game_log_manager
import backup_runner
import applog



_log = applog.get_logger('ui.main_window')


# A failed scheduled backup is tried again after this long, not every minute.
BACKUP_RETRY_SECONDS = 30 * 60
# While one of these runs, a backup would copy a world that's being replaced.
_BLOCKS_BACKUP = ("Restoring a backup", "Finding a broken mod", "Testing mods (in the app)")


class BackupsMixin:
    def start_backup(self, server: ServerConfig, trigger: str, label: str = "") -> str:
        """Starts a backup in the background. Returns "" once it's started, or
        why it can't be. The result arrives as an alert, and the list updates."""
        if not server.install_dir:
            return "This server isn't set up yet."
        if not server.backup_destination:
            return "Set a backup folder for this server first (Server Settings → Backups)."
        if server.id in self._backup_workers:
            return "A backup of this server is already being made."
        busy = self.server_busy(server)
        if busy in _BLOCKS_BACKUP:
            return f"Wait until this finishes: {busy}."
        if getattr(self, "RUN_OPS_INLINE", False):  # tests: run it here and report the result
            out = backup_manager.run_backup_job(server, trigger, label)
            self._on_backup_finished(server, trigger, out)
            return out.error
        worker = backup_runner.BackupWorker(server, trigger, label)
        worker.finished_backup.connect(lambda out, srv=server, t=trigger: self._on_backup_finished(srv, t, out))
        self._backup_workers[server.id] = worker
        self._retire_worker(worker)
        worker.start()
        self._refresh_backup_status(server)
        return ""

    def _handle_scheduled_backup(self, server: ServerConfig) -> None:
        if not (server.backup_destination and server.install_dir) or server.id in self._backup_workers:
            return
        if time.monotonic() < self._backup_retry_at.get(server.id, 0.0):
            return
        why = self.start_backup(server, backup_manager.TRIGGER_SCHEDULED)
        if why:
            _log.info(f"Scheduled backup of {server.name} put off: {why}")

    def _on_backup_finished(self, server: ServerConfig, trigger: str, out) -> None:
        self._backup_workers.pop(server.id, None)
        scheduled = trigger == backup_manager.TRIGGER_SCHEDULED
        if out.entry is None:
            if scheduled:
                self._backup_retry_at[server.id] = time.monotonic() + BACKUP_RETRY_SECONDS
            self._record_event(server, "backup_failed", out.error)
            self._notify(server, f"{'Scheduled backup' if scheduled else 'Backup'} failed: {out.error}"
                                 f"{' It will be tried again in 30 minutes.' if scheduled else ''}",
                         title="Backup Failed")
        else:
            self._backup_retry_at.pop(server.id, None)
            if scheduled:
                # Stamped only on success, so a failed backup is tried again.
                server.last_backup_at = datetime.now().isoformat()
                self.config.save()
            self._record_event(server, "backup", f"{out.entry.size_label}"
                               f"{'' if out.entry.verified else ' (failed its check)'}")
            if not out.entry.verified:
                self._notify(server, "A backup was made, but a copy of the world database in it didn't pass "
                                     "SQLite's check, even on a second try. The world itself may be damaged: "
                                     "keep your older backups, and check Check for Problems.",
                             title="Backup Needs Checking")
            elif not scheduled:
                self._notify(server, f"Backup made ({out.entry.size_label}).", title="Backup Made")
        if out.copy_error:
            key = (server.id, "copy")
            if key not in self._backup_copy_alerted:
                self._backup_copy_alerted.add(key)
                self._notify(server, out.copy_error + " The backup itself is fine; the copy is tried again with "
                                                      "each backup.", title="Second Backup Copy Failed")
        elif out.entry is not None:
            self._backup_copy_alerted.discard((server.id, "copy"))
        self._refresh_backup_status(server)
        self.backups_changed(server)

    def _refresh_backup_status(self, server: ServerConfig) -> None:
        if self.config.active_server_id == server.id:
            self.settings_backups_page.backups_widget.set_busy(server.id in self._backup_workers)

    def _handle_restore(self, server: ServerConfig, entry) -> None:
        if server.id in self._restore_workers:
            return  # a restore for this server is already in flight
        if server.id in self._backup_workers:
            self._notify(server, "A backup is being made right now. Restore once it's finished.",
                         title="Restore Not Started")
            return
        self._expected_stop.add(server.id)
        self._tracker_for(server).close_all_active()
        self._clear_online(server.id)
        was_running = process_manager.is_running(server.install_dir) if server.install_dir else False

        self._restoring_name[server.id] = os.path.basename(entry.path)
        worker = backup_runner.RestoreWorker(server, entry, was_running)
        worker.finished_restore.connect(lambda ok, err, srv=server: self._on_restore_finished(srv, ok, err))
        self._restore_workers[server.id] = worker
        self._retire_worker(worker)
        worker.start()

    def _on_restore_finished(self, server: ServerConfig, ok: bool, err: str) -> None:
        self._restore_workers.pop(server.id, None)
        self._expected_stop.discard(server.id)
        if ok:
            self._known_running[server.id] = process_manager.is_running(server.install_dir) if server.install_dir else False
            self._record_event(server, "restore", self._restoring_name.pop(server.id, ""))
            self._notify(server, "Backup restored" + (" and the server started again." if self._known_running.get(server.id)
                                                      else "."), title="Restore Complete")
            self.backups_changed(server)
        else:
            _log.error(f"Restore failed for {server.name}: {err}")
            self._restoring_name.pop(server.id, None)
            self._record_event(server, "restore_failed", err)
            self._notify(server, f"Restore failed: {err}", title="Restore Failed")

    def _check_disk_space_all(self) -> None:
        for server in self.config.servers:
            if server.install_dir:
                self._check_one_disk_space(server, server.install_dir, "install")
                game_log_manager.prune_old_logs(server.install_dir)
            if server.backup_destination:
                self._check_one_disk_space(server, server.backup_destination, "backup")

    def _check_one_disk_space(self, server: ServerConfig, path: str, kind: str) -> None:
        if not os.path.isdir(path):
            return
        try:
            free_gb = shutil.disk_usage(path).free / (1024 ** 3)
        except OSError:
            return
        key = (server.id, kind)
        if free_gb < diagnostics.LOW_DISK_SPACE_ERROR_GB:
            if key not in self._disk_space_low_alerted:
                self._disk_space_low_alerted.add(key)
                label = "install folder's" if kind == "install" else "backup folder's"
                self._notify(
                    server,
                    f"Only {free_gb:.1f} GB free on the {label} drive ({path}) -- updates and backups "
                    f"will likely start failing soon. Free up space to avoid that.",
                    title="Low Disk Space",
                )
        elif free_gb > diagnostics.LOW_DISK_SPACE_WARNING_GB:
            self._disk_space_low_alerted.discard(key)  # recovered well clear of the threshold -- a later drop should alert again

    def _backup_sources_for_wizard(self, exclude_id: str) -> list:
        """Backups from every other server, newest first, as (label, path)
        tuples for the setup wizard's restore step."""
        candidates = []
        for s in self.config.servers:
            if s.id == exclude_id or not s.backup_destination:
                continue
            for backup in backup_manager.list_backups(s.backup_destination):
                label = f"{s.name} — {backup.when.strftime('%Y-%m-%d %H:%M')} ({backup.trigger}, {backup.size_label})"
                candidates.append((backup.when, label, backup.path))
        candidates.sort(key=lambda c: c[0], reverse=True)
        return [(label, path) for _when, label, path in candidates]
