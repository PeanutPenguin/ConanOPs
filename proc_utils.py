"""Helpers for launching external programs (steamcmd, netsh, the game
server) from ConanOps' windowed process, plus elevation helpers."""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional


def hidden_window_kwargs() -> dict:
    """subprocess kwargs that stop a console child from flashing a window.
    Piping output alone doesn't prevent it; CREATE_NO_WINDOW does.
    No-op off Windows."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def hidden_console_kwargs() -> dict:
    """Give the child a real but hidden console (CREATE_NEW_CONSOLE +
    SW_HIDE). SteamCMD's app_update failed with "Missing configuration"
    under CREATE_NO_WINDOW but works this way. No-op off Windows."""
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0  # SW_HIDE
    return {"creationflags": subprocess.CREATE_NEW_CONSOLE, "startupinfo": startupinfo}


def open_in_explorer(path: str) -> None:
    """Open `path` in Explorer. Raises FileNotFoundError/OSError so callers
    can word the error. No-op off Windows."""
    if not os.path.isdir(path):
        raise FileNotFoundError(path)
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 - Windows-only stdlib call, exactly what this function is for


def hidden_gui_window_kwargs() -> dict:
    """Ask a GUI child (the Conan server opens a render window) to start
    hidden via SW_HIDE. Only works if the child honors nCmdShow.
    No-op off Windows."""
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0  # SW_HIDE
    return {"startupinfo": startupinfo}


def is_admin() -> bool:
    """Whether this process is elevated. False off Windows or if the check
    fails ("not elevated" is the safe default)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001 - see docstring: "not elevated" is always the safe fallback
        return False


def shell_execute_runas(exe: str, params: str, cwd: Optional[str] = None) -> bool:
    """Launch `exe params` elevated via ShellExecuteW "runas" (UAC prompt).
    Returns whether the launch succeeded (declined prompts also return
    False). Gives no process handle, so it can't be waited on."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        # SW_HIDE for consistency; the UAC prompt still shows.
        result = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, cwd, 0)
        return result > 32
    except Exception:  # noqa: BLE001 - this module's whole contract is "best-effort, never raises"
        return False


# Returned when the UAC prompt is declined; not a real exit code.
ELEVATION_DECLINED = -1223
ELEVATION_FAILED = -1


def run_elevated_and_wait(exe: str, params: str, timeout: float = 60.0) -> int:
    """Run `exe params` elevated and wait, via ShellExecuteExW (which gives
    a process handle, so no marker file in a user-writable folder).
    Returns the exit code, ELEVATION_DECLINED, or ELEVATION_FAILED (also on
    timeout or off Windows). Never raises."""
    if sys.platform != "win32":
        return ELEVATION_FAILED
    try:
        import ctypes
        from ctypes import wintypes

        class SHELLEXECUTEINFOW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("fMask", wintypes.ULONG),
                ("hwnd", wintypes.HWND),
                ("lpVerb", wintypes.LPCWSTR),
                ("lpFile", wintypes.LPCWSTR),
                ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR),
                ("nShow", ctypes.c_int),
                ("hInstApp", wintypes.HINSTANCE),
                ("lpIDList", ctypes.c_void_p),
                ("lpClass", wintypes.LPCWSTR),
                ("hkeyClass", wintypes.HKEY),
                ("dwHotKey", wintypes.DWORD),
                ("hIconOrMonitor", wintypes.HANDLE),
                ("hProcess", wintypes.HANDLE),
            ]

        SEE_MASK_NOCLOSEPROCESS = 0x00000040
        SEE_MASK_NOASYNC = 0x00000100
        ERROR_CANCELLED = 1223
        WAIT_OBJECT_0 = 0

        info = SHELLEXECUTEINFOW()
        info.cbSize = ctypes.sizeof(info)
        info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
        info.hwnd = None
        info.lpVerb = "runas"
        info.lpFile = exe
        info.lpParameters = params
        info.lpDirectory = None
        info.nShow = 0  # SW_HIDE

        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
        shell32.ShellExecuteExW.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        if not shell32.ShellExecuteExW(ctypes.byref(info)):
            err = ctypes.get_last_error()
            return ELEVATION_DECLINED if err == ERROR_CANCELLED else ELEVATION_FAILED
        if not info.hProcess:
            return ELEVATION_FAILED
        try:
            if kernel32.WaitForSingleObject(info.hProcess, int(timeout * 1000)) != WAIT_OBJECT_0:
                return ELEVATION_FAILED
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code)):
                return ELEVATION_FAILED
            return int(code.value)
        finally:
            kernel32.CloseHandle(info.hProcess)
    except Exception:  # noqa: BLE001 - best-effort, never raises
        return ELEVATION_FAILED


def child_env() -> dict:
    """Environment for every program ConanOps starts. Strips PyInstaller's
    private variables and _MEI temp PATH entries, and sets
    PYINSTALLER_RESET_ENVIRONMENT=1 so a relaunched ConanOps unpacks its own
    temp folder instead of reusing (and blocking deletion of) the old one."""
    env = dict(os.environ)
    meipass = getattr(sys, "_MEIPASS", None)
    for key in list(env):
        if key.startswith("_PYI_") or key in ("_MEIPASS2",):
            env.pop(key, None)
    if meipass and env.get("PATH"):
        norm = os.path.normcase(os.path.abspath(meipass))
        env["PATH"] = os.pathsep.join(
            p for p in env["PATH"].split(os.pathsep)
            if p and not os.path.normcase(os.path.abspath(p)).startswith(norm)
        )
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env
