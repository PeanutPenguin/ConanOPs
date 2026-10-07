from __future__ import annotations

import pytest

import secrets_store


def test_empty_string_stays_empty():
    assert secrets_store.protect("") == ""
    assert secrets_store.unprotect("") == ""


def test_protect_unprotect_round_trip():
    protected = secrets_store.protect("hunter2")
    assert protected != "hunter2"
    assert "hunter2" not in protected
    assert secrets_store.unprotect(protected) == "hunter2"


def test_protected_value_is_labeled():
    """Whatever protection tier is available (DPAPI on Windows, the
    obfuscated fallback elsewhere), the stored string must be
    honestly prefixed so it's never mistaken for real ciphertext."""
    protected = secrets_store.protect("hunter2")
    assert protected.startswith(secrets_store._DPAPI_PREFIX) or protected.startswith(secrets_store._PLAIN_PREFIX)


def test_unprefixed_value_treated_as_legacy_plaintext():
    assert secrets_store.unprotect("plain-old-value") == "plain-old-value"


def test_dpapi_prefixed_value_raises_decryption_error_when_dpapi_unavailable(monkeypatch):
    """On a non-Windows box (or a build without pywin32/ctypes DPAPI
    access), a "dpapi:"-prefixed value genuinely can't be recovered.
    This must raise DecryptionError -- not silently return "" -- so
    callers (models.py) can tell "never had a secret" apart from
    "have one but can't read it right now" and avoid overwriting the
    real stored value with a blank on the next save."""
    monkeypatch.setattr(secrets_store, "_dpapi_available", lambda: False)
    with pytest.raises(secrets_store.DecryptionError):
        secrets_store.unprotect("dpapi:c29tZWNpcGhlcnRleHQ=")


def test_corrupted_obfuscated_value_raises_decryption_error():
    """Malformed base64 after the "obfuscated:" prefix (e.g. a
    truncated config.json from a crash mid-write) must raise
    DecryptionError rather than let a binascii/UnicodeDecodeError
    propagate out and crash config loading entirely."""
    with pytest.raises(secrets_store.DecryptionError):
        secrets_store.unprotect("obfuscated:not-valid-base64!!!")
