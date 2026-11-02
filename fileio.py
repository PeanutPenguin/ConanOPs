"""
Small file helpers shared by the modules that keep state on disk.

write_text_atomic: a power cut or crash mid-write can't leave a truncated
file. The text goes to a temporary file next to the target, is flushed to
disk, then replaces the target in one step. If Windows won't allow the
replace because another program has the file open without sharing (the
Conan server holding a config file, an editor, antivirus), it falls back to
writing the file in place, as before.

read_text_detect / ENCODINGS: Unreal writes .ini files as UTF-16 when they
contain characters outside ASCII, and some tools add a UTF-8 byte-order
mark. These read a file in whatever encoding it uses and report it, so it
can be written back the same way.
"""
from __future__ import annotations

import os
import tempfile
from typing import Tuple


def write_text_atomic(path: str, text: str, encoding: str = "utf-8", newline: str = None) -> None:
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            pass  # target held open by another program: write in place instead
        with open(path, "w", encoding=encoding, newline=newline) as f:
            f.write(text)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def detect_encoding(data: bytes) -> str:
    """The encoding to read and write `data` with, keeping any byte-order mark."""
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return "utf-16"            # Python reads the BOM and writes it back
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    return "utf-8"


def read_text_detect(path: str) -> Tuple[str, str]:
    """(text, encoding). Undecodable bytes become U+FFFD rather than failing."""
    with open(path, "rb") as f:
        data = f.read()
    encoding = detect_encoding(data)
    return data.decode(encoding, errors="replace"), encoding
