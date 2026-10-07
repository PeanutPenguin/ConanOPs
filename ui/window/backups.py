"""Scheduled and pre-update backups, restores, and the low-disk-space check.

Part of MainWindow (see ui/main_window.py); split out to keep each file focused."""
from __future__ import annotations

import os
import shutil
from datetime import datetime


from models import ServerConfig
import process_manager
import backup_manager
import diagnostics
import game_log_manager
import backup_runner
import applog



_log = applog.get_logger('ui.main_window')


class BackupsMixin:
    def _handle_scheduled_backup(self, server: ServerConfig) -> None:
        if not (server.backup_destination and server.install_dir):
            return
        try:
            entry = backup_manager.create_backup_for_server(server, server.backup_destination, backup_manager.TRIGGER_SCHEDULED)
        except backup_manager.BackupSpaceError as e:
            self._notify(server, f"Scheduled backup skipped -- {e}", title="Backup Failed")
            return
        if entry is None:
            saved = backup_manager.saved_dir(server.install_dir)
            if os.path.isdir(saved):
                self._notify(
                    server,
                    "Scheduled backup failed -- the backup file didn't pass its own integrity check "
                    "after being written, and was deleted. Check the app's log for details -- this "
                    "usually means a disk problem on the backup destination's drive.",
                    title="Backup Failed",
                )
            else:
                self._notify(
                    server,
                    f"Scheduled backup failed -- couldn't find the server's Saved folder ({saved}). "
                    f"Check the Diagnostics settings tab for more.",
                    title="Backup Failed",
                )
            return
        # Stamped only on success so a failed backup retries on the next tick.
        server.last_backup_at = datetime.now().isoformat()
        self.config.save()
        backup_manager.prune_backups(server.backup_destination, server.backup_daily_keep, server.backup_weekly_keep)
        if self.config.active_server_id == server.id:
            self.settings_backups_page.refresh()

    def _handle_pre_update_backup(self, server: ServerConfig) -> None:
        if server.backup_before_update and server.backup_destination:
            try:
                backup_manager.create_backup_for_server(server, server.backup_destination, backup_manager.TRIGGER_PRE_UPDATE)
            except backup_manager.BackupSpaceError as e:
                self._notify(server, f"Pre-update backup skipped -- {e}", title="Backup Failed")

    def _handle_restore(self, server: ServerConfig, entry) -> None:
        if server.id in self._restore_workers:
            return  # a restore for this server is already in flight
        self._expected_stop.add(server.id)
        self._tracker_for(server).close_all_active()
        self._clear_online(server.id)
        was_running = process_manager.is_running(server.install_dir) if server.install_dir else False

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
            self._notify(server, "Backup restored and server relaunched.", title="Restore Complete")
            self.backups_changed(server)
        else:
            _log.error(f"Restore failed for {server.name}: {err}")
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
