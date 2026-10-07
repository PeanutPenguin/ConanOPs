"""Minimal Source RCON client (used by Conan Exiles).

Packet: int32 length | int32 request_id | int32 type | body\x00 | \x00
The server needs RCONEnabled and RCONPort set; AdminPassword is the RCON password.
"""
from __future__ import annotations

import socket
import struct
from dataclasses import dataclass
from typing import Optional

SERVERDATA_AUTH = 3
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_AUTH_RESPONSE = 2
SERVERDATA_RESPONSE_VALUE = 0


class RconError(Exception):
    pass


class RconAuthError(RconError):
    pass


@dataclass
class RconClient:
    host: str
    port: int
    password: str
    timeout: float = 5.0

    def __post_init__(self):
        self._sock: Optional[socket.socket] = None
        self._next_id = 1

    def connect(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(self.timeout)
        try:
            self._sock.connect((self.host, self.port))
        except OSError as e:
            self.close()
            raise RconError(f"Couldn't connect to {self.host}:{self.port}: {e}") from e
        try:
            self._authenticate()
        except OSError as e:
            self.close()
            raise RconError(f"Connection lost during authentication: {e}") from e

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def __enter__(self) -> "RconClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ wire --
    def _send_packet(self, pkt_type: int, body: str) -> int:
        pkt_id = self._next_id
        self._next_id += 1
        payload = struct.pack("<ii", pkt_id, pkt_type) + body.encode("utf-8") + b"\x00\x00"
        packet = struct.pack("<i", len(payload)) + payload
        self._sock.sendall(packet)
        return pkt_id

    def _recv_packet(self) -> tuple:
        raw_len = self._recv_exact(4)
        (length,) = struct.unpack("<i", raw_len)
        data = self._recv_exact(length)
        pkt_id, pkt_type = struct.unpack("<ii", data[:8])
        body = data[8:-2].decode("utf-8", errors="replace")
        return pkt_id, pkt_type, body

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise RconError("Connection closed while reading response")
            buf += chunk
        return buf

    def _authenticate(self) -> None:
        self._send_packet(SERVERDATA_AUTH, self.password)
        # Some servers send an empty RESPONSE_VALUE before the AUTH_RESPONSE.
        for _ in range(2):
            pkt_id, pkt_type, _body = self._recv_packet()
            if pkt_type == SERVERDATA_AUTH_RESPONSE:
                if pkt_id == -1:
                    raise RconAuthError("RCON authentication failed (wrong password).")
                return
        raise RconAuthError("RCON authentication did not complete as expected.")

    # ----------------------------------------------------------- public --
    def command(self, cmd: str) -> str:
        if not self._sock:
            raise RconError("Not connected -- call connect() first.")
        try:
            self._send_packet(SERVERDATA_EXECCOMMAND, cmd)
            _pkt_id, _pkt_type, body = self._recv_packet()
        except OSError as e:
            raise RconError(f"Connection lost while running the command: {e}") from e
        return body


def send_command(host: str, port: int, password: str, cmd: str, timeout: float = 5.0) -> str:
    with RconClient(host, port, password, timeout=timeout) as client:
        return client.command(cmd)
