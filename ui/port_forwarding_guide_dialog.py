"""
Manual port-forwarding guide: shown wherever ConanOps already knows
UPnP couldn't do this automatically (the setup wizard's Networking
step, and the Diagnostics tab's "Router UPnP" check) -- since that's
exactly the moment someone needs this and has no other way to find
out what to actually do about it.

Deliberately self-contained in the app rather than linking out to a
third-party site: it needs to work even when ConanOps' own diagnosis
is "your network is part of the problem," and it can be filled in
with this server's OWN ports and IPs instead of generic placeholders.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame, QScrollArea, QWidget,
)


def _step(number: int, title: str, body: str) -> QFrame:
    card = QFrame()
    card.setObjectName("Card")
    layout = QHBoxLayout(card)
    layout.setContentsMargins(16, 12, 16, 12)
    layout.setSpacing(12)

    badge = QLabel(str(number))
    badge.setFixedWidth(24)
    badge.setStyleSheet("font-weight: 700; font-size: 14px;")
    layout.addWidget(badge)

    text_col = QVBoxLayout()
    text_col.setSpacing(2)
    title_label = QLabel(title)
    title_label.setStyleSheet("font-weight: 600;")
    title_label.setWordWrap(True)
    body_label = QLabel(body)
    body_label.setObjectName("Muted")
    body_label.setWordWrap(True)
    body_label.setTextInteractionFlags(body_label.textInteractionFlags() | Qt.TextSelectableByMouse)  # addresses/ports are meant to be copied
    text_col.addWidget(title_label)
    text_col.addWidget(body_label)
    layout.addLayout(text_col, 1)

    return card


class PortForwardingGuideDialog(QDialog):
    def __init__(
        self,
        game_port: int,
        query_port: int,
        local_ip: str,
        router_ip: Optional[str],
        public_ip: Optional[str],
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Port Forwarding Guide")
        self.setModal(True)
        self.setMinimumSize(520, 560)

        ports_line = f"UDP {game_port}, {game_port + 1} (Conan's own second port, right above the game port), and {query_port}"
        local_ip_text = local_ip or "(not yet detected -- run the Networking step or Diagnostics first)"
        public_ip_text = public_ip or "(couldn't detect)"
        if router_ip:
            router_step = (
                f"In a web browser, go to http://{router_ip} -- this is ConanOps' best guess at your router's "
                f"address. If that doesn't load anything, open a Command Prompt, run \"ipconfig\", and use the "
                f"\"Default Gateway\" address listed for your network adapter instead."
            )
        else:
            router_step = (
                "ConanOps couldn't detect your router's address. Open a Command Prompt, run \"ipconfig\", and "
                "use the \"Default Gateway\" address listed for your network adapter (often 192.168.0.1 or "
                "192.168.1.1) -- type it into a web browser's address bar. A sticker on the router usually "
                "shows it too."
            )

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 16)
        root.setSpacing(12)

        intro = QLabel(
            "Every router is a little different, so exact menu names vary -- but the steps below "
            "are the same shape everywhere. This only needs doing once per router; ConanOps has no "
            "way to do it for you, since it would need your router's own admin login, which only you have."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        steps_container = QWidget()
        steps_layout = QVBoxLayout(steps_container)
        steps_layout.setSpacing(10)

        steps_layout.addWidget(_step(
            1, "Open your router's admin page", router_step,
        ))
        steps_layout.addWidget(_step(
            2, "Log in",
            "Often admin/admin or admin/password if it's never been changed -- check a sticker on the "
            "router itself first, since many ship with a unique password printed there. Your ISP's "
            "setup guide or the router's own manual (searchable by its model number, also usually on "
            "that sticker) will have it if not.",
        ))
        steps_layout.addWidget(_step(
            3, "Find port forwarding",
            "Look for \"Port Forwarding,\" \"Virtual Server,\" \"NAT Forwarding,\" or sometimes a "
            "\"Gaming\" section -- usually under an \"Advanced\" menu. The exact name depends on the "
            "router's brand, but it's almost always one of these.",
        ))
        steps_layout.addWidget(_step(
            4, "Give this PC a fixed local address",
            f"Find \"DHCP Reservation\", \"Address Reservation\" or \"Static Lease\" (often under LAN or "
            f"DHCP settings) and reserve {local_ip_text} for this PC. Without this, the router can hand "
            f"the PC a different address after a restart, and the forwarding rules below would point at "
            f"nothing.",
        ))
        steps_layout.addWidget(_step(
            5, "Add three UDP forwarding rules",
            f"Forward {ports_line} -- all pointed at this PC's local IP address: {local_ip_text}. Choose "
            f"UDP as the protocol. If the router only offers TCP or \"Both\", pick \"Both\". Keep the "
            f"external and internal port numbers the same.",
        ))
        steps_layout.addWidget(_step(
            6, "Save, and reboot the router if it asks",
            "Some routers apply port-forwarding rules immediately; others need a restart first. If "
            "the router's own interface doesn't say either way, restarting it after saving is the "
            "safe default.",
        ))
        steps_layout.addWidget(_step(
            7, "Check it actually worked",
            f"Have a friend outside your home network connect to {public_ip_text}:{game_port}. Testing "
            f"your own public IP from inside your home often fails even when everything is set up "
            f"correctly (many routers don't support \"NAT loopback\"), and most online \"open port\" "
            f"checkers only test TCP, not UDP -- so a friend actually connecting is the check that can't "
            f"be wrong.",
        ))
        steps_layout.addWidget(_step(
            8, "Still not reachable?",
            "If your router's status page shows its own internet/WAN address starting with 10., 172.16-31., "
            "192.168. or 100.64-127., there's another router or modem in front of it (double NAT), or your "
            "ISP shares one public address between customers (carrier-grade NAT). Forward the same ports "
            "on the ISP's modem too (or put it in bridge mode), or ask your ISP for a public IP address.",
        ))

        steps_layout.addStretch(1)
        scroll.setWidget(steps_container)
        root.addWidget(scroll, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.setObjectName("PrimaryButton")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)
