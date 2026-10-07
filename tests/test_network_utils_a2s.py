from __future__ import annotations

import socket

import network_utils


class _FakeSocket:
    """Simulates a UDP socket returning scripted (data, addr) tuples
    or raising scripted exceptions, one call at a time, across
    potentially multiple socket() instances (the challenge-response
    retry opens a second socket)."""
    _script = []  # shared class-level queue across instances, set per test

    def __init__(self, *a, **k):
        pass

    def settimeout(self, t):
        pass

    def sendto(self, data, addr):
        pass

    def recvfrom(self, bufsize):
        action = _FakeSocket._script.pop(0)
        if isinstance(action, Exception):
            raise action
        return action, ("1.2.3.4", 27015)

    def close(self):
        pass


def _install_fake_socket(monkeypatch, script):
    _FakeSocket._script = list(script)
    monkeypatch.setattr(socket, "socket", _FakeSocket)


def _real_info_response(name="Chudville", map_name="ExiledLands"):
    def cstr(s):
        return s.encode("utf-8") + b"\x00"
    buf = b"\xff\xff\xff\xffI" + b"\x11" + cstr(name) + cstr(map_name) + cstr("folder") + cstr("game")
    buf += b"\x00\x00"  # app id
    buf += bytes([5])   # players
    buf += bytes([40])  # max_players
    return buf


def test_query_a2s_info_returns_none_on_timeout(monkeypatch):
    _install_fake_socket(monkeypatch, [socket.timeout()])
    assert network_utils.query_a2s_info("1.2.3.4", 27015) is None


def test_query_a2s_info_parses_a_direct_response(monkeypatch):
    _install_fake_socket(monkeypatch, [_real_info_response("Chudville", "ExiledLands")])
    result = network_utils.query_a2s_info("1.2.3.4", 27015)
    assert result["name"] == "Chudville"
    assert result["map"] == "ExiledLands"
    assert result["players"] == 5
    assert result["max_players"] == 40


def test_query_a2s_info_follows_a_challenge_response(monkeypatch):
    challenge_reply = b"\xff\xff\xff\xff\x41\x01\x02\x03\x04"
    _install_fake_socket(monkeypatch, [challenge_reply, _real_info_response()])
    result = network_utils.query_a2s_info("1.2.3.4", 27015)
    assert result is not None
    assert result["name"] == "Chudville"


def test_query_a2s_info_handles_challenge_response_timing_out(monkeypatch):
    """The actual bug: the second (challenge) reply timing out used to
    raise socket.timeout straight out of this function uncaught,
    crashing whatever called it -- the auto-bisect worker and the
    background health check both use this."""
    challenge_reply = b"\xff\xff\xff\xff\x41\x01\x02\x03\x04"
    _install_fake_socket(monkeypatch, [challenge_reply, socket.timeout()])
    result = network_utils.query_a2s_info("1.2.3.4", 27015)  # must not raise
    assert result is None


def test_query_a2s_info_handles_challenge_response_os_error(monkeypatch):
    challenge_reply = b"\xff\xff\xff\xff\x41\x01\x02\x03\x04"
    _install_fake_socket(monkeypatch, [challenge_reply, OSError("network unreachable")])
    result = network_utils.query_a2s_info("1.2.3.4", 27015)  # must not raise
    assert result is None


def test_query_a2s_info_returns_none_for_wrong_response_type(monkeypatch):
    wrong_type = b"\xff\xff\xff\xffZ" + b"\x00" * 10
    _install_fake_socket(monkeypatch, [wrong_type])
    assert network_utils.query_a2s_info("1.2.3.4", 27015) is None


def test_query_a2s_info_returns_none_for_truncated_garbage(monkeypatch):
    _install_fake_socket(monkeypatch, [b"\xff\xff\xff\xffI\x11short"])
    assert network_utils.query_a2s_info("1.2.3.4", 27015) is None
