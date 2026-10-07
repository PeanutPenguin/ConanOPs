from __future__ import annotations

import sys

import pytest

pyside6 = pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

from ui.port_forwarding_guide_dialog import PortForwardingGuideDialog


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


def test_dialog_constructs_with_full_info():
    dialog = PortForwardingGuideDialog(7777, 27015, "192.168.1.50", "192.168.1.1", "203.0.113.9")
    assert dialog.windowTitle() == "Port Forwarding Guide"


def test_dialog_handles_missing_router_and_public_ip_gracefully():
    """None for router_ip/public_ip is a normal, expected input (both
    are best-effort detections that can legitimately fail) -- must not
    raise or show "None" literally anywhere."""
    dialog = PortForwardingGuideDialog(7777, 27015, "192.168.1.50", None, None)
    # Walk every QLabel and confirm none of them literally say "None"
    from PySide6.QtWidgets import QLabel
    for label in dialog.findChildren(QLabel):
        assert "None" not in label.text()


def test_dialog_mentions_all_three_ports():
    dialog = PortForwardingGuideDialog(7777, 27015, "192.168.1.50", "192.168.1.1", "203.0.113.9")
    from PySide6.QtWidgets import QLabel
    all_text = " ".join(label.text() for label in dialog.findChildren(QLabel))
    assert "7777" in all_text
    assert "7778" in all_text  # game+1
    assert "27015" in all_text


def test_dialog_mentions_the_local_ip_as_the_forwarding_target():
    dialog = PortForwardingGuideDialog(7777, 27015, "192.168.1.50", "192.168.1.1", "203.0.113.9")
    from PySide6.QtWidgets import QLabel
    all_text = " ".join(label.text() for label in dialog.findChildren(QLabel))
    assert "192.168.1.50" in all_text
