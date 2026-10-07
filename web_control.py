"""
Stdlib HTTP server for the web version: serves assets/web and the JSON API
(webui/api.py) on the home network, and remotely via a Cloudflare tunnel.

Security: all but login needs an HttpOnly SameSite=Strict session cookie;
no password set = only the setup screen works; POSTs need a custom header
and matching Origin (CSRF); wrong passwords are rate-limited.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import tempfile
import socket
import sys
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingMixIn, TCPServer
from typing import Callable, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

import applog
import network_utils

_log = applog.get_logger(__name__)

DEFAULT_PORT = 8787
PORT_FALLBACK_ATTEMPTS = 5
COOKIE = "conanops_session"
MAX_BODY = 64 * 1024

_STATIC = {
    "/": ("web", "index.html"),
    "/index.html": ("web", "index.html"),
    "/app.js": ("web", "app.js"),
    "/app.css": ("web", "app.css"),
    "/manifest.webmanifest": ("web", "manifest.webmanifest"),
    "/icon.svg": ("conanops-icon-small.svg",),
    "/icon-512.png": ("conanops-icon-512.png",),
    "/fonts/Geist-Regular.ttf": ("fonts", "Geist-Regular.ttf"),
    "/fonts/Geist-Medium.ttf": ("fonts", "Geist-Medium.ttf"),
    "/fonts/Geist-SemiBold.ttf": ("fonts", "Geist-SemiBold.ttf"),
    "/fonts/GeistMono-Regular.ttf": ("fonts", "GeistMono-Regular.ttf"),
}

_SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' data: https://steamuserimages-a.akamaihd.net "
                                "https://images.steamusercontent.com; style-src 'self'; script-src 'self'; "
                                "connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
                                "form-action 'self'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


def _asset(*parts: str) -> str:
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "assets", *parts)


def _make_handler(owner: "WebControlServer"):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ConanOps"
        sys_version = ""

        def log_message(self, fmt, *args):  # noqa: A003
            if not self.path.startswith("/api/servers/") or self.command != "GET":
                _log.info("web: " + (fmt % args))

        def _client_ip(self) -> str:
            ip = self.client_address[0]
            if ip in ("127.0.0.1", "::1") and self.headers.get("Cf-Connecting-Ip"):
                return self.headers["Cf-Connecting-Ip"].strip()  # arrived through the tunnel
            return ip

        def _https(self) -> bool:
            return (self.client_address[0] in ("127.0.0.1", "::1")
                    and self.headers.get("X-Forwarded-Proto", "").lower() == "https")

        def _token(self) -> Optional[str]:
            raw = self.headers.get("Cookie")
            if not raw:
                return None
            try:
                c = SimpleCookie(raw)
            except Exception:  # noqa: BLE001
                return None
            return c[COOKIE].value if COOKIE in c else None

        def _authed(self) -> bool:
            return bool(owner.password_hash()) and owner.sessions.valid(self._token())

        def _send(self, status: int, body: bytes, ctype: str, extra: Optional[dict] = None,
                  cache: str = "no-store") -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            for k, v in _SECURITY_HEADERS.items():
                self.send_header(k, v)
            if self._https():
                self.send_header("Strict-Transport-Security", "max-age=31536000")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, status: int = 200, extra: Optional[dict] = None) -> None:
            self._send(status, json.dumps(obj, default=str).encode("utf-8"), "application/json", extra)

        def _cookie(self, token: str, max_age: int) -> str:
            parts = [f"{COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Strict", f"Max-Age={max_age}"]
            if self._https():
                parts.append("Secure")
            return "; ".join(parts)

        def _chunked(self) -> bool:
            return "chunked" in (self.headers.get("Transfer-Encoding") or "").lower()

        def _read_chunks(self, limit: int, sink) -> int:
            """Reads a chunked body into sink(bytes); returns its size, or -1 if
            over `limit` or malformed."""
            total = 0
            while True:
                line = self.rfile.readline(1024)
                try:
                    size = int(line.split(b";")[0].strip() or b"0", 16)
                except ValueError:
                    return -1
                if size == 0:
                    while self.rfile.readline(1024) not in (b"\r\n", b"\n", b""):
                        pass  # trailers
                    return total
                total += size
                if total > limit:
                    return -1
                remaining = size
                while remaining:
                    chunk = self.rfile.read(min(remaining, 1024 * 1024))
                    if not chunk:
                        return -1
                    sink(chunk)
                    remaining -= len(chunk)
                self.rfile.readline(8)  # the CRLF after each chunk

        def _read_body(self) -> Optional[dict]:
            if self._chunked():
                parts: list = []
                if self._read_chunks(MAX_BODY, parts.append) < 0:
                    return None
                raw = b"".join(parts)
                length = len(raw)
            else:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    return None
                if length > MAX_BODY:
                    return None
                raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return None
            return data if isinstance(data, dict) else None

        def _same_origin_post(self) -> bool:
            if self.headers.get("X-ConanOps") != "1":
                return False
            origin = self.headers.get("Origin")
            if not origin:
                return True
            host = self.headers.get("Host", "")
            return urlsplit(origin).netloc == host

        def do_HEAD(self):  # noqa: N802
            self.do_GET()

        def do_GET(self):  # noqa: N802
            url = urlsplit(self.path)
            if url.path in _STATIC:
                return self._static(url.path)
            if url.path == "/api/session":
                return self._json({"authed": self._authed(), "password_set": bool(owner.password_hash()),
                                   "remote": owner.is_remote_request(self)})
            if url.path.startswith("/api/"):
                return self._api("GET", url)
            self._json({"error": "Not found"}, 404)

        def do_POST(self):  # noqa: N802
            url = urlsplit(self.path)
            if not self._same_origin_post():
                return self._json({"error": "Forbidden"}, 403)
            if url.path == "/api/login":
                return self._login()
            if url.path == "/api/logout":
                owner.sessions.revoke(self._token())
                return self._json({"ok": True}, extra={"Set-Cookie": self._cookie("", 0)})
            m = re.fullmatch(r"/api/servers/([^/]+)/backups/import", url.path)
            if m:
                return self._import_backup(m.group(1))
            if url.path.startswith("/api/"):
                return self._api("POST", url)
            self._json({"error": "Not found"}, 404)

        def _import_backup(self, sid: str) -> None:
            """Streams an uploaded backup zip to a temp file, then imports it."""
            if not owner.password_hash() or not self._authed():
                return self._json({"error": "Please sign in.", "needs_login": True}, 401)
            if owner.api is None:
                return self._json({"error": "ConanOps is still starting."}, 503)
            from webui.api import MAX_IMPORT_BYTES
            from webui.bridge import WebActionError
            chunked = self._chunked()
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 and not chunked:
                return self._json({"ok": False, "message": "Choose a backup file first."}, 400)
            if length > MAX_IMPORT_BYTES:
                self.close_connection = True
                return self._json({"ok": False, "message": "That file is too big to be a server backup."}, 413)
            folder = tempfile.mkdtemp(prefix="conanops-import-")
            path = os.path.join(folder, "upload.zip")
            try:
                remaining = 0 if chunked else length
                with open(path, "wb") as f:
                    if chunked and self._read_chunks(MAX_IMPORT_BYTES, f.write) <= 0:
                        raise OSError("the upload was empty, too big or broken")
                    while remaining > 0:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise OSError("the upload stopped before it finished")
                        f.write(chunk)
                        remaining -= len(chunk)
                name = unquote(self.headers.get("X-Filename", "") or "")
                result = owner.api.import_backup(sid, path, name)
                self._json(result)
            except WebActionError as e:
                self._json({"ok": False, "error": str(e), "message": str(e)}, 400)
            except OSError as e:
                self.close_connection = True
                self._json({"ok": False, "message": f"The upload failed: {e}"}, 400)
            except Exception as e:  # noqa: BLE001
                _log.exception(f"Backup import failed: {e}")
                self._json({"ok": False, "message": "Something went wrong -- see conanops.log on the PC."}, 500)
            finally:
                shutil.rmtree(folder, ignore_errors=True)

        def _static(self, path: str) -> None:
            file_path = _asset(*_STATIC[path])
            try:
                with open(file_path, "rb") as f:
                    body = f.read()
            except OSError:
                return self._json({"error": "Not found"}, 404)
            ctype = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
            if path.endswith(".webmanifest"):
                ctype = "application/manifest+json"
            if ctype.startswith("text/") or ctype in ("application/javascript",):
                ctype += "; charset=utf-8"
            cache = "no-cache" if path in ("/", "/index.html", "/app.js", "/app.css") else "max-age=86400"
            self._send(200, body, ctype, cache=cache)

        def _login(self) -> None:
            ip = self._client_ip()
            wait = owner.limiter.wait_seconds(ip)
            if wait:
                return self._json({"error": f"Too many wrong passwords. Try again in {wait} seconds."}, 429)
            stored = owner.password_hash()
            if not stored:
                return self._json({"error": "Set a web password in ConanOps (App Settings) first."}, 403)
            body = self._read_body() or {}
            from webui import auth
            if not auth.verify_password(str(body.get("password", "")), stored):
                owner.limiter.failed(ip)
                _log.warning(f"Web login: wrong password from {ip}")
                return self._json({"error": "Wrong password."}, 401)
            owner.limiter.succeeded(ip)
            token, max_age = owner.sessions.create(bool(body.get("remember", True)))
            _log.info(f"Web login from {ip}")
            self._json({"ok": True}, extra={"Set-Cookie": self._cookie(token, max_age)})

        def _api(self, method: str, url) -> None:
            if not owner.password_hash():
                return self._json({"error": "Set a web password in ConanOps (App Settings) first.",
                                   "needs_password": True}, 403)
            if not self._authed():
                return self._json({"error": "Please sign in.", "needs_login": True}, 401)
            if owner.api is None:
                return self._json({"error": "ConanOps is still starting."}, 503)
            fn, params = owner.api.match(method, url.path)
            if fn is None:
                return self._json({"error": "Not found"}, 404)
            body = self._read_body() if method == "POST" else {}
            if body is None:
                return self._json({"error": "Bad request"}, 400)
            from webui.api import Request
            from webui.bridge import WebActionError
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            req = Request(method, url.path, params, query, body, self._client_ip())
            try:
                result = fn(req)
                self._json(result if isinstance(result, dict) else {"ok": True})
            except WebActionError as e:
                self._json({"ok": False, "error": str(e), "message": str(e)}, 400)
            except TimeoutError:
                msg = ("ConanOps on the PC is busy and didn't answer in time. It may still finish this -- check "
                       "before trying again.")
                self._json({"ok": False, "error": msg, "message": msg}, 504)
            except Exception as e:  # noqa: BLE001 - never leak a traceback to the browser
                _log.exception(f"Web API {method} {url.path} failed: {e}")
                self._json({"ok": False, "error": "Something went wrong -- see conanops.log on the PC.",
                            "message": "Something went wrong -- see conanops.log on the PC."}, 500)

    return Handler


class _ThreadingHTTPServer(ThreadingMixIn, TCPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets two programs bind the same port;
    # SO_EXCLUSIVEADDRUSE makes a taken port fail so we fall back to the next.
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self):
        opt = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if sys.platform == "win32" and opt is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, opt, 1)
        super().server_bind()


class WebControlServer:
    """`api` (a webui.api.WebApi) is attached by MainWindow later."""

    def __init__(self, get_active_server: Callable = None, save_config: Optional[Callable[[], None]] = None,
                 get_password_hash: Optional[Callable[[], str]] = None, sessions_path: Optional[str] = None):
        from webui.auth import LoginLimiter, SessionStore
        self.get_active_server = get_active_server
        self.save_config = save_config
        self._get_password_hash = get_password_hash or (lambda: "")
        self.sessions = SessionStore(sessions_path)
        self.limiter = LoginLimiter()
        self.api = None
        self.tunnel_port_hint: Optional[int] = None
        self._httpd: Optional[_ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.actual_port: Optional[int] = None

    def password_hash(self) -> str:
        try:
            return self._get_password_hash() or ""
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def is_remote_request(handler) -> bool:
        return handler.client_address[0] in ("127.0.0.1", "::1") and bool(handler.headers.get("Cf-Connecting-Ip"))

    @property
    def is_running(self) -> bool:
        return self._httpd is not None

    def start(self, preferred_port: int = DEFAULT_PORT) -> Tuple[bool, str]:
        """Listens on all adapters, trying a few ports past `preferred_port`."""
        if self.is_running:
            return True, f"Already running on port {self.actual_port}."
        handler_cls = _make_handler(self)
        last_error: Optional[Exception] = None
        for port in range(preferred_port, preferred_port + PORT_FALLBACK_ATTEMPTS):
            try:
                httpd = _ThreadingHTTPServer(("0.0.0.0", port), handler_cls)
            except OSError as e:
                last_error = e
                continue
            self._httpd = httpd
            self.actual_port = port
            self._thread = threading.Thread(target=httpd.serve_forever, daemon=True, name="web-control")
            self._thread.start()
            _log.info(f"web: listening on 0.0.0.0:{port}")
            return True, f"Listening on port {port}."
        _log.error(f"web: couldn't bind any port starting from {preferred_port}: {last_error}")
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
        _log.info("web: stopped.")

    def url_for(self, ip: Optional[str] = None) -> Optional[str]:
        """The home-network link."""
        if not self.is_running:
            return None
        ip = ip or network_utils.get_local_ip()
        return f"http://{ip}:{self.actual_port}"
