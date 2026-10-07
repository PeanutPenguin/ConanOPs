"""
Encrypts sensitive ServerConfig fields (RCON password, server join
password, webhook URLs -- Discord/ntfy webhook URLs are bearer tokens,
not just addresses) before they're written to config.json, and
decrypts them on load.

Uses Windows DPAPI (CryptProtectData / CryptUnprotectData) via ctypes
directly -- no extra dependency (no pywin32) -- since encryption tied
to the current Windows user account is what actually stops "read
config.json, get everyone's RCON password", without ConanOps having
to invent and manage its own key file (which would just move the
secret from one plaintext file to another one right next to it).

DPAPI isn't available off Windows. Rather than silently falling back
to plaintext -- which would defeat the whole point without anyone
noticing -- values are still obfuscated (base64, clearly NOT
encryption) with an honest prefix so this is visible in the stored
file, and a warning is logged once per run. This matters mainly for
running/testing this codebase on non-Windows machines; the packaged
Windows build (see README) always has real DPAPI available.
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
    """Raised by unprotect() when a stored value can't be recovered --
    DPAPI unavailable/failed, or corrupted base64 -- as opposed to
    "no value was ever set" (which returns "", not an error). Callers
    (see models.py) need this distinction: overwriting a value that
    merely failed to decrypt THIS run with "" on the next save would
    destroy it permanently, since every save round-trips through
    protect() again."""


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
    """Returns a string safe to write to config.json. Empty input
    stays empty -- no point tagging/encrypting "no password set"."""
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
    """Reverses protect(). Also transparently handles values saved
    before this feature existed (plain, unprefixed text), so
    upgrading ConanOps doesn't lock anyone out of their own saved
    passwords -- those get protected automatically the next time
    config.json is saved, since every save round-trips through
    protect() again.

    Raises DecryptionError -- rather than returning "" -- when a
    value is present but genuinely couldn't be recovered (DPAPI
    unavailable or failed, or corrupted base64). Returning "" for
    that case used to look identical to "no secret was ever set,"
    and since every config.json save re-protects whatever's
    currently in memory, that "" got written straight back out on
    the very next save -- permanently erasing an RCON password or
    webhook URL just because it failed to decrypt once (e.g. the
    config was copied to a different Windows user account, or a
    transient DPAPI error). See models.py's from_dict()/to_dict()
    for how the caller uses this to avoid that."""
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
    # Legacy: saved before this feature existed -- plain text as-is.
    return stored
