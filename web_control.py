"""
Local web control: a lightweight HTTP server, stdlib only (no new
dependency), that serves a small status/control page reachable from a
browser on this machine or elsewhere on the local network. Off by
default -- toggled from App Settings, which also owns whether it's
remembered across restarts.

Deliberately unauthenticated, per an explicit choice: whoever has the
link can view status and start/stop/restart the active server. That
trade-off is surfaced in the App Settings UI text, not re-litigated
here.

Scope: controls whichever server is "active" in ConanOps at the
moment a request arrives (via the get_active_server callable), not a
specific server picked at startup -- so switching the active server in
the app changes what the web page controls too.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingMixIn, TCPServer
from typing import Callable, Optional, Tuple

import applog
import network_utils
import preflight
import process_manager
from models import ServerConfig

_log = applog.get_logger(__name__)

DEFAULT_PORT = 8787
PORT_FALLBACK_ATTEMPTS = 5


def _render_page() -> bytes:
    html = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ConanOps Web Control</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { background: #1d1d1d; color: #ececec; font-family: Geist, -apple-system, 'Segoe UI', sans-serif;
         font-size: 14px; max-width: 480px; margin: 40px auto; padding: 0 20px; }
  h1 { font-size: 22px; font-weight: 600; margin: 0 0 4px; }
  .sub { color: #a3a3a3; margin: 0 0 20px; font-size: 13px; }
  .card { background: #252525; border: 1px solid #303030; border-radius: 10px; padding: 18px; margin: 14px 0; }
  #name { font-size: 16px; font-weight: 600; margin-bottom: 8px; }
  #state { display: inline-block; border-radius: 999px; padding: 3px 10px; font-size: 12px; font-weight: 600; }
  #state::before { content: ""; display: inline-block; width: 7px; height: 7px; border-radius: 50%;
                   background: currentColor; margin-right: 6px; vertical-align: 1px; }
  .status-online { color: #5fd38a; background: rgba(95, 211, 138, .12); }
  .status-offline { color: #a3a3a3; background: rgba(163, 163, 163, .12); }
  .players { color: #a3a3a3; margin-top: 10px; }
  button { background: #232323; color: #ececec; border: 1px solid #3a3a3a; border-radius: 8px; min-height: 44px;
           padding: 0 18px; font: inherit; font-weight: 500; margin: 4px 8px 4px 0; cursor: pointer; }
  button:hover { background: #2c2c2c; }
  button.primary { background: #ac6427; border-color: #ac6427; color: #fff; font-weight: 600; }
  button.primary:hover { background: #b8702f; }
  button:disabled { opacity: 0.45; cursor: default; }
  button:focus-visible { outline: 2px solid #e2914f; outline-offset: 2px; }
  #msg { margin-top: 10px; color: #f0b54a; min-height: 20px; }
</style>
</head>
<body>
<h1>ConanOps Web Control</h1>
<p class="sub">Start, stop or restart your server from any device on your network.</p>
<div class="card">
  <div id="name">Loading...</div>
  <div id="state" class="status-offline">-</div>
  <div id="players" class="players"></div>
</div>
<div class="card">
  <button id="startBtn" class="primary" onclick="doAction('start')">Start</button>
  <button id="restartBtn" onclick="doAction('restart')">Restart</button>
  <button id="stopBtn" onclick="doAction('stop')">Stop</button>
  <div id="msg"></div>
</div>
<script>
async function refresh() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    document.getElementById('name').textContent = d.name || '(no server configured)';
    const state = document.getElementById('state');
    if (!d.configured) {
      state.textContent = 'Not configured';
      state.className = 'status-offline';
    } else {
      state.textContent = d.running ? 'Online' : 'Offline';
      state.className = d.running ? 'status-online' : 'status-offline';
    }
    document.getElementById('players').textContent =
      (d.running && d.players !== null) ? `${d.players}/${d.max_players} players` : '';
    document.getElementById('startBtn').disabled = !d.configured || d.running;
    document.getElementById('restartBtn').disabled = !d.configured || !d.running;
    document.getElementById('stopBtn').disabled = !d.configured || !d.running;
  } catch (e) {
    document.getElementById('msg').textContent = 'Lost connection to ConanOps.';
  }
}
async function doAction(action) {
  const msg = document.getElementById('msg');
  msg.textContent = 'Working...';
  try {
    const r = await fetch('/api/' + action, { method: 'POST' });
    const d = await r.json();
    msg.textContent = d.message || '';
  } catch (e) {
    msg.textContent = 'Request failed.';
  }
  refresh();
}
refresh();
setInterval(refresh, 4000);
</script>
</body>
</html>"""
    return html.encode("utf-8")


def _make_handler(get_active_server: Callable[[], Optional[ServerConfig]],
                   save_config: Optional[Callable[[], None]] = None):
    # Shared across every request this handler class serves, so two
    # Start/Restart POSTs that land close together (e.g. an impatient
    # double-click on the web page) are serialized instead of both
    # racing to launch the process -- process_manager.is_running() alone
    # isn't enough since the first launch's process may not show up in
    # a process scan yet by the time the second request checks it.
    action_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: A003 - silence default stderr logging
            _log.info("web_control: " + (fmt % args))

        def _origin_allowed(self) -> bool:
            """Best-effort CSRF guard: this page is meant to be opened
            directly, not embedded/POSTed-to from some other site a
            browser on the LAN happens to have open. A browser sets
            Origin on cross-origin requests (and most same-origin ones
            too); a request with no Origin/Referer header at all (curl,
            a same-origin fetch some browsers omit it for) is let
            through, since this is defense in depth, not real auth --
            the page is unauthenticated by design (see module
            docstring)."""
            origin = self.headers.get("Origin") or self.headers.get("Referer")
            if not origin:
                return True
            host = self.headers.get("Host", "")
            return host and (origin == f"http://{host}" or origin.startswith(f"http://{host}/"))

        def _send_json(self, obj: dict, status: int = 200) -> None:
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - required name by BaseHTTPRequestHandler
            if self.path == "/" or self.path == "/index.html":
                body = _render_page()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/status":
                self._send_json(self._status())
            else:
                self._send_json({"error": "not found"}, status=404)

        def do_POST(self) -> None:  # noqa: N802
            if not self._origin_allowed():
                self._send_json({"error": "forbidden"}, status=403)
                return
            if self.path == "/api/start":
                self._send_json(self._start())
            elif self.path == "/api/stop":
                self._send_json(self._stop())
            elif self.path == "/api/restart":
                self._send_json(self._restart())
            else:
                self._send_json({"error": "not found"}, status=404)

        # ------------------------------------------------------ actions --
        def _status(self) -> dict:
            server = get_active_server()
            if server is None or not server.install_dir:
                return {"configured": False, "name": None, "running": False, "players": None, "max_players": None}
            running = process_manager.is_running(server.install_dir)
            players = max_players = None
            if running:
                info = network_utils.query_a2s_info(server.bind_ip or "127.0.0.1", server.query_port, timeout=2.0)
                if info:
                    players = info.get("players")
                    max_players = info.get("max_players")
            return {
                "configured": True, "name": server.name, "running": running,
                "players": players, "max_players": max_players,
            }

        def _start(self) -> dict:
            with action_lock:
                server = get_active_server()
                if server is None or not server.install_dir:
                    return {"success": False, "message": "No server is configured."}
                if process_manager.is_running(server.install_dir):
                    return {"success": False, "message": "Already running."}
                result = preflight.run_preflight(server)
                if save_config:
                    save_config()
                if not result.ok:
                    return {"success": False, "message": "Pre-flight check failed: " + "; ".join(result.problems)}
                try:
                    process_manager.launch(server)
                except OSError as e:
                    _log.error(f"web_control: start failed: {e}")
                    return {"success": False, "message": f"Failed to start: {e}"}
                return {"success": True, "message": "Starting..."}

        def _stop(self) -> dict:
            with action_lock:
                server = get_active_server()
                if server is None or not server.install_dir:
                    return {"success": False, "message": "No server is configured."}
                if not process_manager.is_running(server.install_dir):
                    return {"success": False, "message": "Not running."}
                ok = process_manager.graceful_stop(server)
                return {"success": ok, "message": "Stopped." if ok else "Stop didn't complete cleanly."}

        def _restart(self) -> dict:
            with action_lock:
                server = get_active_server()
                if server is None or not server.install_dir:
                    return {"success": False, "message": "No server is configured."}
                result = preflight.run_preflight(server)
                if save_config:
                    save_config()
                if not result.ok:
                    return {"success": False, "message": "Pre-flight check failed: " + "; ".join(result.problems)}
                try:
                    process_manager.restart(server)
                except OSError as e:
                    _log.error(f"web_control: restart failed: {e}")
                    return {"success": False, "message": f"Failed to restart: {e}"}
                return {"success": True, "message": "Restarting..."}

    return Handler


class _ThreadingHTTPServer(ThreadingMixIn, TCPServer):
    daemon_threads = True
    allow_reuse_address = True


class WebControlServer:
    def __init__(self, get_active_server: Callable[[], Optional[ServerConfig]],
                 save_config: Optional[Callable[[], None]] = None):
        self.get_active_server = get_active_server
        self.save_config = save_config
        self._httpd: Optional[_ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.actual_port: Optional[int] = None

    @property
    def is_running(self) -> bool:
        return self._httpd is not None

    def start(self, preferred_port: int = DEFAULT_PORT) -> Tuple[bool, str]:
        """Binds to 0.0.0.0 (reachable from other devices on the LAN,
        not just this machine) and serves in a background thread.
        Tries a few ports past `preferred_port` if it's already taken,
        since a fixed single port with no fallback is a common,
        entirely avoidable failure mode."""
        if self.is_running:
            return True, f"Already running on port {self.actual_port}."

        handler_cls = _make_handler(self.get_active_server, self.save_config)
        last_error: Optional[Exception] = None
        for port in range(preferred_port, preferred_port + PORT_FALLBACK_ATTEMPTS):
            try:
                httpd = _ThreadingHTTPServer(("0.0.0.0", port), handler_cls)
            except OSError as e:
                last_error = e
                continue
            self._httpd = httpd
            self.actual_port = port
            self._thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            self._thread.start()
            _log.info(f"web_control: listening on 0.0.0.0:{port}")
            return True, f"Listening on port {port}."

        _log.error(f"web_control: couldn't bind any port starting from {preferred_port}: {last_error}")
        return False, f"Couldn't start: {last_error}"

    def stop(self) -> None:
        if not self.is_running:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2.0)
        self._httpd = None
        self._thread = None
        self.actual_port = None
        _log.info("web_control: stopped.")

    def url_for(self, ip: Optional[str] = None) -> Optional[str]:
        """The link to share/copy. Uses the machine's LAN IP (so it
        works from another device), falling back to localhost if that
        can't be determined."""
        if not self.is_running:
            return None
        ip = ip or network_utils.get_local_ip()
        return f"http://{ip}:{self.actual_port}"
