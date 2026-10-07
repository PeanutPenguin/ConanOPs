"""
Encrypts sensitive ServerConfig fields (passwords, webhook URLs, which are
bearer tokens) in config.json using Windows DPAPI via ctypes, tied to the
current Windows user so no key file is needed. Off Windows, values are only
base64-obfuscated under an explicit "obfuscated:" prefix, with a warning.
"""
from __future__ import annotations

import base64
import platform

import applog

_log = applog.get_logger(__name__)

_DPAPI_PREFIX = "dpapi:"
_PLAIN_PREFIX = "obfuscated:"  # non-Windows fallback -- NOT real encryption, see module docstring

_warned_fallback = False


class DecryptionError(Exception):
    """A stored value exists but can't be recovered. Distinct from "never set"
    ("") so callers don't save "" over it and destroy it permanently."""


def _dpapi_available() -> bool:
    return platform.system() == "Windows"


def _dpapi_encrypt(data: bytes) -> bytes:
    import ctypes
    import ctypes.wintypes as wintypes

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _to_blob(payload: bytes) -> _DATA_BLOB:
        buf = ctypes.create_string_buffer(payload, len(payload))
        return _DATA_BLOB(len(payload), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in = _to_blob(data)
    blob_out = _DATA_BLOB()
    ok = crypt32.CryptProtectData(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise OSError("CryptProtectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _dpapi_decrypt(data: bytes) -> bytes:
    import ctypes
    import ctypes.wintypes as wintypes

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _to_blob(payload: bytes) -> _DATA_BLOB:
        buf = ctypes.create_string_buffer(payload, len(payload))
        return _DATA_BLOB(len(payload), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in = _to_blob(data)
    blob_out = _DATA_BLOB()
    ok = crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise OSError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def protect(plaintext: str) -> str:
    """Returns a string safe to write to config.json. Empty stays empty."""
    global _warned_fallback
    if not plaintext:
        return ""
    if _dpapi_available():
        encrypted = _dpapi_encrypt(plaintext.encode("utf-8"))
        return _DPAPI_PREFIX + base64.b64encode(encrypted).decode("ascii")
    if not _warned_fallback:
        _log.warning(
            "DPAPI isn't available on this platform -- secrets in config.json are "
            "obfuscated (base64) only, not encrypted. Expected in a non-Windows dev/test "
            "environment; a packaged Windows build uses real DPAPI encryption."
        )
        _warned_fallback = True
    return _PLAIN_PREFIX + base64.b64encode(plaintext.encode("utf-8")).decode("ascii")


def unprotect(stored: str) -> str:
    """Reverses protect(). Unprefixed values are legacy plain text and are
    returned as-is. Raises DecryptionError (never returns "") when a value
    can't be recovered, e.g. config copied to another Windows user."""
    if not stored:
        return ""
    if stored.startswith(_DPAPI_PREFIX):
        if not _dpapi_available():
            _log.error("Found a DPAPI-protected value but DPAPI isn't available here -- can't decrypt it.")
            raise DecryptionError("DPAPI isn't available on this platform.")
        try:
            payload = base64.b64decode(stored[len(_DPAPI_PREFIX):])
            return _dpapi_decrypt(payload).decode("utf-8")
        except (OSError, ValueError, UnicodeDecodeError) as e:
            _log.error(
                "Failed to decrypt a stored secret (DPAPI error) -- it may belong to a "
                "different Windows user account than the one running ConanOps now."
            )
            raise DecryptionError(str(e)) from e
    if stored.startswith(_PLAIN_PREFIX):
        try:
            return base64.b64decode(stored[len(_PLAIN_PREFIX):]).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as e:
            _log.error(f"Failed to decode an 'obfuscated:' stored value: {e}")
            raise DecryptionError(str(e)) from e
    return stored
