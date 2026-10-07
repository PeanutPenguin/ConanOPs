"""QThread workers for removing a server and for "Delete Everything" (see cleanup.py)."""
from __future__ import annotations

import time
from typing import List

from PySide6.QtCore import QThread, Signal

import applog
import cleanup
import network_setup
import process_manager

_log = applog.get_logger(__name__)


def _stop_and_wait(server, timeout: float = 60.0) -> bool:
    """Stops the server and waits for its process to exit so files aren't locked."""
    if not server.install_dir or not process_manager.is_running(server.install_dir):
        return True
    try:
        process_manager.graceful_stop(server)
    except Exception as e:  # noqa: BLE001
        _log.error(f"Stopping {server.name} before removal failed: {e}")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_manager.is_running(server.install_dir):
            return True
        time.sleep(0.5)
    return False


class ServerRemovalWorker(QThread):
    """Removes one server's firewall rules, router forwards and chosen files.
    Emits finished_removal with a list of problems (empty = clean)."""
    finished_removal = Signal(list)

    def __init__(self, server, other_servers, delete_files: bool, delete_backups: bool, parent=None):
        super().__init__(parent)
        self.server = server
        self.others = list(other_servers)
        self.delete_files = delete_files
        self.delete_backups = delete_backups

    def run(self) -> None:
        problems: List[str] = []
        s = self.server
        try:
            if (self.delete_files or self.delete_backups) and not _stop_and_wait(s):
                problems.append(f"{s.name} didn't stop in time, so some of its files may still be in use.")
            plan = cleanup.server_paths(s, self.others)
            program_dirs = [s.install_dir] if s.install_dir else []
            if not network_setup.remove_firewall_rules([s.id], legacy_names=[s.name], program_dirs=program_dirs):
                problems.append("Its Windows Firewall rules weren't removed (the permission prompt was declined "
                                "or failed).")
            try:
                network_setup.remove_upnp_mappings(
                    s.id, ports=[s.game_port, s.game_port + 1, s.query_port], local_ip=s.bind_ip or None,
                )
            except Exception as e:  # noqa: BLE001
                _log.warning(f"Router forward removal failed: {e}")
            if self.delete_files:
                for path in plan["files"]:
                    err = cleanup.remove_path(path)
                    if err:
                        problems.append(err)
            if self.delete_backups and plan["backups"]:
                problems.extend(cleanup.remove_backups(*plan["backups"]))
            for path in plan["always"]:
                err = cleanup.remove_path(path)
                if err:
                    problems.append(err)
        except Exception as e:  # noqa: BLE001 - always report back
            _log.error(f"Server removal failed unexpectedly: {e}")
            problems.append(f"Unexpected error: {e}")
        self.finished_removal.emit(problems)


class DeleteEverythingWorker(QThread):
    """Removes everything except the program folder and open files (self_delete.py does those)."""
    progress = Signal(str)
    finished_cleanup = Signal(list)

    def __init__(self, config, uninstall_vcredist: bool, parent=None):
        super().__init__(parent)
        self.config = config
        self.uninstall_vcredist = uninstall_vcredist

    def run(self) -> None:
        problems: List[str] = []
        servers = list(self.config.servers)
        try:
            for s in servers:
                self.progress.emit(f"Stopping {s.name}…")
                if not _stop_and_wait(s):
                    problems.append(f"{s.name} didn't stop in time; some of its files may remain.")

            self.progress.emit("Removing Windows settings (Windows will ask for permission)…")
            import background_mode
            import powershell
            script = cleanup.cleanup_script(
                server_ids=[s.id for s in servers], legacy_names=[s.name for s in servers],
                program_dirs=[s.install_dir for s in servers if s.install_dir],
                remove_task=True,
                restore_active_hours=self.config.original_active_hours or None,
                uninstall_vcredist=self.uninstall_vcredist,
            )
            if powershell.run_privileged(script, timeout=600) != powershell.RUN_OK:
                problems.append("Windows settings (firewall rules, update hours) weren't removed -- the "
                                "permission prompt was declined or failed.")

            self.progress.emit("Removing router port forwards…")
            try:
                import network_utils
                local_ip = network_utils.get_local_ip()
                device = network_setup.discover_igd(timeout=2.0, local_ip=local_ip)
                if device is not None:
                    for s in servers:
                        network_setup.remove_upnp_mappings(
                            s.id, ports=[s.game_port, s.game_port + 1, s.query_port],
                            local_ip=s.bind_ip or local_ip, device=device,
                        )
            except Exception as e:  # noqa: BLE001
                _log.warning(f"Router forward removal failed: {e}")

            try:
                import startup_registration
                startup_registration.unregister()
            except OSError as e:
                problems.append(f"The sign-in startup entry wasn't removed: {e}")

            self.progress.emit("Deleting server files and backups…")
            for i, s in enumerate(servers):
                plan = cleanup.server_paths(s, servers[i + 1:])
                for path in plan["files"] + plan["always"]:
                    err = cleanup.remove_path(path)
                    if err:
                        problems.append(err)
                if plan["backups"]:
                    problems.extend(cleanup.remove_backups(*plan["backups"]))
            for path in cleanup.temp_leftovers():
                cleanup.remove_path(path)
        except Exception as e:  # noqa: BLE001
            _log.error(f"Delete Everything failed unexpectedly: {e}")
            problems.append(f"Unexpected error: {e}")
        self.finished_cleanup.emit(problems)
