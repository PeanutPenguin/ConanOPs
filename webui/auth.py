"""
Login for the web version: a password (PBKDF2-SHA256, never stored in
plain text), long-lived sessions so a phone stays signed in, and a
lockout that slows down password guessing.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from typing import Dict, Optional, Tuple

import applog

_log = applog.get_logger(__name__)

PBKDF2_ITERATIONS = 310_000
SESSION_DAYS = 30
SHORT_SESSION_HOURS = 12
MIN_PASSWORD_LENGTH = 8


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, digest_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, AttributeError):
        return False


_COMMON = {"12345678", "123456789", "1234567890", "qwertyui", "qwertyuiop", "iloveyou", "abcdefgh",
           "sunshine", "princess", "football", "baseball", "welcome1", "trustno1", "passw0rd"}


def password_problem(password: str) -> str:
    """"" if acceptable, else why not."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters."
    if (password.lower() in _COMMON or len(set(password)) <= 2
            or password.lower().rstrip("0123456789!") in ("password", "conanops", "qwerty", "conan", "letmein")):
        return "That password is too easy to guess."
    return ""


class SessionStore:
    """Session tokens are random 256-bit values held by the browser in an
    HttpOnly cookie. Only their SHA-256 is kept (in memory and in a small
    file, so a restart of ConanOps doesn't sign everyone out)."""

    def __init__(self, path: Optional[str]):
        self._path = path
        self._lock = threading.Lock()
        self._sessions: Dict[str, float] = {}
        self._load()

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _load(self) -> None:
        if not self._path or not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            now = time.time()
            self._sessions = {k: float(v) for k, v in data.items() if float(v) > now}
        except (OSError, ValueError, AttributeError):
            self._sessions = {}

    def _save(self) -> None:
        if not self._path:
            return
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._sessions, f)
            os.replace(tmp, self._path)
        except OSError as e:
            _log.warning(f"Couldn't save web sessions: {e}")

    def create(self, remember: bool) -> Tuple[str, int]:
        """(token, max_age_seconds)."""
        token = secrets.token_urlsafe(32)
        max_age = SESSION_DAYS * 86400 if remember else SHORT_SESSION_HOURS * 3600
        with self._lock:
            self._sessions[self._key(token)] = time.time() + max_age
            self._save()
        return token, max_age

    def valid(self, token: Optional[str]) -> bool:
        if not token:
            return False
        with self._lock:
            exp = self._sessions.get(self._key(token))
            if exp is None:
                return False
            if exp < time.time():
                self._sessions.pop(self._key(token), None)
                self._save()
                return False
            return True

    def revoke(self, token: Optional[str]) -> None:
        if not token:
            return
        with self._lock:
            if self._sessions.pop(self._key(token), None) is not None:
                self._save()

    def revoke_all(self) -> None:
        with self._lock:
            self._sessions.clear()
            self._save()

    def count(self) -> int:
        now = time.time()
        with self._lock:
            return sum(1 for e in self._sessions.values() if e > now)


class LoginLimiter:
    """After 5 wrong passwords from one address, that address waits 1
    minute, then 2, 4... up to an hour. Separately, more than 30 wrong
    passwords in an hour from anywhere locks logins for everyone for 15
    minutes -- addresses are easy to change, the hourly cap isn't."""
    FREE_TRIES = 5
    MAX_LOCK = 3600
    GLOBAL_LIMIT = 30
    GLOBAL_LOCK = 900

    def __init__(self):
        self._lock = threading.Lock()
        self._fails: Dict[str, Tuple[int, float]] = {}  # ip -> (count, locked_until)
        self._recent: list = []
        self._global_until = 0.0

    def wait_seconds(self, ip: str) -> int:
        now = time.time()
        with self._lock:
            if self._global_until > now:
                return int(self._global_until - now) + 1
            count, until = self._fails.get(ip, (0, 0.0))
            return int(until - now) + 1 if until > now else 0

    def failed(self, ip: str) -> None:
        now = time.time()
        with self._lock:
            count, _until = self._fails.get(ip, (0, 0.0))
            count += 1
            lock = 0.0
            if count >= self.FREE_TRIES:
                lock = min(self.MAX_LOCK, 60 * (2 ** (count - self.FREE_TRIES)))
            self._fails[ip] = (count, now + lock)
            self._recent = [t for t in self._recent if t > now - 3600] + [now]
            if len(self._recent) > self.GLOBAL_LIMIT:
                self._global_until = now + self.GLOBAL_LOCK
                _log.warning("Web login: too many wrong passwords this hour; logins paused for 15 minutes.")

    def succeeded(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)
