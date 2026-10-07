"""QThread workers for firewall and UPnP changes, so netsh/PowerShell, UAC
prompts and router round-trips never run on the GUI thread."""
from __future__ import annotations

from typing import Iterable, Optional

from PySide6.QtCore import QThread, Signal

import applog
import network_setup

_log = applog.get_logger(__name__)


class FirewallReconcileWorker(QThread):
    """Reconcile one server's firewall rules and UPnP forwards with its
    settings (one UAC prompt), or remove them all when remove=True.
    Always emits finished_reconcile({"fw_results", "upnp", "error"})."""
    finished_reconcile = Signal(object)

    def __init__(self, server_id: str, display_name: str = "", game_port: int = 0, query_port: int = 0,
                 bind_ip: str = "", remove: bool = False, legacy_names: Iterable[str] = (),
                 old_ports: Iterable[int] = (), do_firewall: bool = True, do_upnp: bool = True,
                 exe_path: str = "", parent=None):
        super().__init__(parent)
        self.exe_path = exe_path
        self.server_id = server_id
        self.display_name = display_name
        self.game_port = game_port
        self.query_port = query_port
        self.bind_ip = bind_ip
        self.remove = remove
        self.legacy_names = [n for n in legacy_names if n]
        self.old_ports = [int(p) for p in old_ports if p]
        self.do_firewall = do_firewall
        self.do_upnp = do_upnp

    def run(self) -> None:
        result = {"fw_results": [], "upnp": None, "error": None}
        try:
            if self.remove:
                if self.do_firewall:
                    network_setup.remove_firewall_rules([self.server_id], legacy_names=self.legacy_names)
                if self.do_upnp:
                    network_setup.remove_upnp_mappings(self.server_id, ports=self.old_ports,
                                                       local_ip=self.bind_ip or None)
            else:
                if self.do_firewall:
                    result["fw_results"] = network_setup.add_firewall_rules(
                        self.server_id, self.game_port, self.query_port,
                        display=self.display_name, legacy_names=self.legacy_names, exe_path=self.exe_path,
                    )
                if self.do_upnp and self.bind_ip:
                    result["upnp"] = network_setup.reconcile_upnp(
                        self.server_id, self.bind_ip, self.game_port, self.query_port, old_ports=self.old_ports,
                    )
        except Exception as e:  # noqa: BLE001 - always emit
            _log.error(f"Network reconcile failed unexpectedly: {e}")
            result["error"] = str(e)
        self.finished_reconcile.emit(result)


class WizardNetworkSetupWorker(QThread):
    """The setup wizard's whole "detect IP/ports, open firewall, try
    UPnP, check for double NAT" pass, in one background call."""
    finished_setup = Signal(object)  # dict -- see run()

    def __init__(self, server_id: str, server_name: str, reserved_ports: set,
                 preferred_game_port: int = 7777, preferred_query_port: int = 27015,
                 legacy_names: Iterable[str] = (), exe_path: str = "", parent=None):
        super().__init__(parent)
        self.exe_path = exe_path
        self.server_id = server_id
        self.server_name = server_name
        self.reserved_ports = set(reserved_ports or ())
        self.preferred_game_port = preferred_game_port or 7777
        self.preferred_query_port = preferred_query_port or 27015
        self.legacy_names = [n for n in legacy_names if n]

    def run(self) -> None:
        import network_utils
        try:
            detected_ip = network_utils.get_local_ip()
            if not network_utils.is_usable_lan_ipv4(detected_ip):
                raise RuntimeError(
                    "Couldn't find this PC's network address -- check that it's connected to your "
                    "router (Wi-Fi or cable), then click the button to try again."
                )
            game_port, query_port = network_utils.find_free_port_pair(
                self.preferred_game_port, self.reserved_ports, start_query_port=self.preferred_query_port,
            )
            fw_results = network_setup.add_firewall_rules(
                self.server_id, game_port, query_port,
                display=self.server_name, legacy_names=self.legacy_names, exe_path=self.exe_path,
            )
            upnp = network_setup.reconcile_upnp(self.server_id, detected_ip, game_port, query_port)
            public_ip = network_setup.get_public_ip()
            router_ip = network_utils.get_default_gateway(detected_ip)
            external_ip = upnp.get("external_ip")
            # Double NAT / CGNAT: router WAN address is private or differs
            # from the public address.
            double_nat = bool(upnp.get("double_nat")) or bool(
                external_ip and public_ip and external_ip != public_ip
            )
        except Exception as e:  # noqa: BLE001 - always emit so the page doesn't hang
            _log.error(f"Wizard network setup failed: {e}")
            self.finished_setup.emit({"error": str(e)})
            return
        self.finished_setup.emit({
            "error": None,
            "detected_ip": detected_ip,
            "game_port": game_port,
            "query_port": query_port,
            "fw_results": fw_results,
            "upnp": upnp,
            "public_ip": public_ip,
            "router_ip": router_ip,
            "double_nat": double_nat,
        })


class GuideInfoWorker(QThread):
    """Router and public IP lookups for the port-forwarding guide."""
    finished_info = Signal(object)  # (router_ip, public_ip)

    def __init__(self, local_ip: Optional[str], parent=None):
        super().__init__(parent)
        self.local_ip = local_ip

    def run(self) -> None:
        import network_utils
        router_ip = public_ip = None
        try:
            router_ip = network_utils.get_default_gateway(self.local_ip or None)
            public_ip = network_setup.get_public_ip(timeout=3.0)
        except Exception as e:  # noqa: BLE001
            _log.warning(f"Guide info lookup failed: {e}")
        self.finished_info.emit((router_ip, public_ip))
