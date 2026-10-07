"""Firewall rules, router forwards, Dynamic DNS, and changes waiting for someone at the PC.

Part of MainWindow (see ui/main_window.py); split out to keep each file focused."""
from __future__ import annotations

from typing import List

from PySide6.QtWidgets import (
    QMessageBox, QSystemTrayIcon,
)

from models import ServerConfig
import process_manager
import dynamic_dns_runner
import network_setup
import network_setup_runner
import powershell
import applog

from ui.workers import keep_until_finished


_log = applog.get_logger('ui.main_window')


class NetworkMixin:
    def queue_for_pc(self, label: str, fn) -> None:
        """Defers a web action needing a Windows permission prompt until someone
        uses the PC. A newer entry with the same label replaces the older."""
        self._needs_pc_seq = getattr(self, "_needs_pc_seq", 0) + 1
        self._needs_pc = [x for x in self._needs_pc if x[0] != label] + [(label, fn, self._needs_pc_seq)]
        _log.info(f"Waiting for someone at the PC: {label}")

    def needs_pc_labels(self) -> List[str]:
        return [x[0] for x in self._needs_pc]

    def needs_pc_counter(self) -> int:
        return getattr(self, "_needs_pc_seq", 0)

    def needs_pc_queued_since(self, counter: int) -> List[str]:
        """Changes queued (or re-queued) after needs_pc_counter() was `counter`."""
        return [x[0] for x in self._needs_pc if x[2] > counter]

    def _run_needs_pc(self) -> None:
        if not self._needs_pc or powershell.prompts_blocked() or getattr(self, "_asking_needs_pc", False):
            return
        self._asking_needs_pc = True
        try:
            items = list(self._needs_pc)
            text = ("Changes made from the web version need Windows' permission, which can only be given here:\n\n"
                    + "\n".join(f"•  {x[0]}" for x in items)
                    + "\n\nFinish them now? Windows will ask for permission.")
            answer = QMessageBox.question(self, "Finish Changes From the Web", text,
                                          QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            self._needs_pc = [x for x in self._needs_pc if x not in items]
            if answer != QMessageBox.Yes:
                _log.info("Changes from the web that needed permission were dismissed at the PC.")
                return
            for label, fn, _seq in items:
                try:
                    fn()
                except Exception as e:  # noqa: BLE001
                    _log.error(f"Couldn't finish \"{label}\": {e}")
        finally:
            self._asking_needs_pc = False

    def _start_network_reconcile(
        self, server: ServerConfig, remove: bool = False, legacy_names=(), old_ports=(),
        do_firewall: bool = True, do_upnp: bool = True, on_done=None,
    ) -> None:
        """Fire-and-forget: re-creates (or with remove=True deletes) a server's
        firewall rules and UPnP forwards in the background, one UAC prompt at
        most. From the web without admin rights, the firewall part waits for the PC."""
        if do_firewall and powershell.prompts_blocked():
            args = dict(remove=remove, legacy_names=list(legacy_names), old_ports=list(old_ports))
            self.queue_for_pc(f"Windows Firewall rules for {server.name}",
                              lambda s=server, a=args: self._start_network_reconcile(s, do_upnp=False, **a))
            do_firewall = False
        ports = list(old_ports)
        if remove:
            ports += [server.game_port, server.game_port + 1, server.query_port]
        worker = network_setup_runner.FirewallReconcileWorker(
            server.id, display_name=server.name, game_port=server.game_port, query_port=server.query_port,
            bind_ip=server.bind_ip, remove=remove,
            legacy_names=list(legacy_names) or [server.name], old_ports=ports,
            do_firewall=do_firewall, do_upnp=do_upnp,
            exe_path=process_manager.server_exe_path(server.install_dir) if server.install_dir else "",
        )
        worker.finished_reconcile.connect(
            lambda *_a, w=worker: self._firewall_workers.remove(w) if w in self._firewall_workers else None
        )
        if on_done is not None:
            worker.finished_reconcile.connect(on_done)
        self._firewall_workers.append(worker)
        self._retire_worker(worker)
        worker.start()

    def _start_network_cleanup_for_wizard(self, server_id: str, ports, bind_ip: str) -> None:
        """On wizard cancel: removes rules/forwards it created, via a tracked worker."""
        worker = network_setup_runner.FirewallReconcileWorker(
            server_id, remove=True, old_ports=list(ports), bind_ip=bind_ip,
        )
        worker.finished_reconcile.connect(
            lambda *_a, w=worker: self._firewall_workers.remove(w) if w in self._firewall_workers else None
        )
        self._firewall_workers.append(worker)
        self._retire_worker(worker)
        worker.start()

    def _repair_network_for_active(self) -> None:
        """Network & Ports page's "Repair Networking" button."""
        server = self.config.get_active()
        if not server:
            self.settings_network_page.repair_finished()
            return
        self.prepare_network_repair(server)

        def done(result, srv=server):
            self.settings_network_page.repair_finished()
            QMessageBox.information(self, f"Repair Networking — {srv.name}",
                                    "\n".join(self.network_repair_lines(result)) or "Done.")

        self._start_network_reconcile(server, legacy_names=[server.name], on_done=done)

    def prepare_network_repair(self, server: ServerConfig) -> None:
        """Fills in this PC's address if the server has no bind IP yet."""
        if not server.bind_ip:
            import network_utils
            ip = network_utils.get_local_ip()
            if network_utils.is_usable_lan_ipv4(ip):
                server.bind_ip = ip
                self.config.save()
                self.settings_saved_elsewhere(server, "network")

    @staticmethod
    def network_repair_lines(result: dict) -> List[str]:
        """What a Repair Networking run did, line by line."""
        lines = []
        if result.get("error"):
            lines.append(f"✗ Something went wrong: {result['error']}")
        for r in result.get("fw_results") or []:
            lines.append(("✓ " if r.success else "✗ ") + "Firewall: " + r.message)
        upnp = result.get("upnp")
        if upnp is None:
            pass
        elif not upnp.get("upnp_available"):
            lines.append("✗ Router: automatic forwarding (UPnP) isn't available -- forward the ports "
                         "manually (Diagnostics has a step-by-step guide).")
        else:
            ok = all(upnp.get(k) for k in ("game_port_forwarded", "game_port_plus_one_forwarded",
                                            "query_port_forwarded"))
            lines.append("✓ Router: all three ports forwarded." if ok
                         else "✗ Router: some ports couldn't be forwarded automatically.")
            if upnp.get("double_nat"):
                lines.append("⚠ Your router's internet address isn't public (double NAT or carrier-grade "
                             "NAT) -- see Diagnostics.")
        return lines

    def _refresh_router_forwards(self) -> None:
        if not network_setup.UPNP_REFRESH_ENABLED:
            return
        for server in self.config.servers:
            if self._known_running.get(server.id) and server.bind_ip:
                try:
                    network_setup.refresh_upnp_async(server)
                except Exception as e:  # noqa: BLE001 - a timer slot must never raise
                    _log.warning(f"Router forward refresh for {server.name} failed: {e}")

    def _check_duckdns(self) -> None:
        if not (self.config.duckdns_domain and self.config.duckdns_token):
            return
        if self._duckdns_worker is not None:
            return  # previous check still running -- skip this tick rather than overlap
        self._duckdns_worker = dynamic_dns_runner.DuckDnsWorker(
            self.config.duckdns_domain, self.config.duckdns_token, parent=self,
        )
        self._duckdns_worker.finished_update.connect(self._on_duckdns_finished)
        worker = self._duckdns_worker
        keep_until_finished(self._retiring_workers, worker)
        self._duckdns_worker.start()

    def _on_duckdns_finished(self, result) -> None:
        self._duckdns_worker = None
        if result.ok:
            return  # silent on success -- most checks find no change; a toast every 30 minutes would just be noise
        _log.warning(f"DuckDNS update failed: {result.message}")
        if self.tray_icon and self.tray_icon.isVisible():
            # Tray only: this isn't tied to any one server's webhook.
            self.tray_icon.showMessage(
                "DuckDNS Update Failed", result.message, QSystemTrayIcon.Warning, 8000,
            )
