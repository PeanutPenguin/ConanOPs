"""Shared helper for shelling out to external console-subsystem tools
(steamcmd.exe, netsh.exe) from ConanOps' own windowed GUI process."""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional


def hidden_window_kwargs() -> dict:
    """Extra kwargs for subprocess.run()/Popen() that stop Windows from
    flashing a visible console window for a console-subsystem child
    process launched from a windowed (GUI) parent process.

    Piping stdout/stderr (capture_output=True, or explicit PIPE args)
    does NOT by itself suppress this window on Windows -- the console
    still gets created and shown for an instant before the child even
    has a chance to write anything to those pipes. CREATE_NO_WINDOW is
    the actual fix. It's a Windows-only subprocess flag (referencing
    subprocess.CREATE_NO_WINDOW on another platform raises
    AttributeError), so this is a no-op everywhere else."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def hidden_console_kwargs() -> dict:
    """Like hidden_window_kwargs(), but gives the child a REAL console
    that's simply never shown, instead of no console at all.

    CREATE_NO_WINDOW runs a console program with no console attached
    whatsoever. That's fine for netsh, but SteamCMD behaved differently
    under it than when run from a normal console: the same
    `+app_update 443030` command that failed with "Missing
    configuration" when launched by ConanOps succeeded from a .bat.
    CREATE_NEW_CONSOLE + SW_HIDE hands SteamCMD a genuine console (so
    it runs exactly as it would from Command Prompt) while still never
    showing a window. stdout/stderr are still piped by the caller, so
    ConanOps keeps reading output live. No-op off Windows."""
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0  # SW_HIDE
    return {"creationflags": subprocess.CREATE_NEW_CONSOLE, "startupinfo": startupinfo}


def open_in_explorer(path: str) -> None:
    """Opens `path` in Windows Explorer. Raises FileNotFoundError if
    path doesn't exist and OSError for anything else that goes wrong --
    deliberately doesn't swallow these itself, since what to tell the
    person differs by call site (e.g. "server not installed yet" reads
    better than a generic error on some pages). No-op off Windows,
    since os.startfile() only exists there at all; every other part
    of this app is Windows-only too (see this module's own docstring),
    so that's expected, not silently degraded functionality."""
    if not os.path.isdir(path):
        raise FileNotFoundError(path)
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 - Windows-only stdlib call, exactly what this function is for


def hidden_gui_window_kwargs() -> dict:
    """Best-effort suppression of a child GUI process's own window --
    for a process that isn't a console app at all, unlike the two
    helpers above. The Conan Exiles dedicated server
    (ConanSandboxServer-Win64-Shipping.exe) opens a real render window
    on Windows by default; every Linux/Wine setup guide for it needs a
    virtual framebuffer (xvfb) specifically because the "dedicated
    server" build still tries to create one.

    This sets STARTF_USESHOWWINDOW + SW_HIDE, which Windows passes to
    the child as its requested initial window state. Whether that
    actually keeps the window hidden depends on the child respecting
    it: a well-behaved app that calls ShowWindow(hwnd, nCmdShow) with
    the value Windows gave it will start hidden, but some apps ignore
    that and show their window regardless. It's the safe, standard
    thing to try -- there's no downside if the app ignores it, only
    upside if it doesn't. No-op off Windows."""
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0  # SW_HIDE
    return {"startupinfo": startupinfo}


def is_admin() -> bool:
    """Whether this process is currently running elevated
    (Administrator). Windows Firewall rule changes (netsh advfirewall
    firewall add/delete) require this -- checking it proactively is
    what lets network_setup.py offer a real UAC prompt up front
    instead of quietly failing every netsh call and only explaining
    why afterward. Always False off Windows, where ctypes.windll
    doesn't exist at all; also False (rather than raising) if the
    Win32 call itself fails for any reason, since "assume not
    elevated" is the safe default either way -- worst case, this
    triggers an elevation prompt that wasn't strictly needed."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001 - see docstring: "not elevated" is always the safe fallback
        return False


def shell_execute_runas(exe: str, params: str, cwd: Optional[str] = None) -> bool:
    """Launches `exe params` elevated -- triggers a real Windows UAC
    consent prompt -- via ShellExecuteW's "runas" verb. Returns
    whether the launch itself succeeded: per the Win32 docs,
    ShellExecuteW returns a value > 32 on success and a small error
    code otherwise (this covers both a declined UAC prompt and a
    launch failure -- Windows doesn't distinguish the two at this
    API). That return value is explicitly NOT a process handle, so it
    can't be waited on here; a caller that needs to know when the
    elevated command actually finishes has to have it signal
    completion some other way (e.g. writing a marker file once done --
    see network_setup.py's elevated firewall-rule helper). No-op
    (returns False) off Windows."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        # SW_HIDE: no visible window for the elevated process, matching
        # every other subprocess this app launches (see the *_kwargs
        # helpers above) -- consistency, not a security measure (the UAC
        # consent prompt itself is what Windows shows regardless).
        result = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, cwd, 0)
        return result > 32
    except Exception:  # noqa: BLE001 - this module's whole contract is "best-effort, never raises"
        return False


# Sentinel exit code run_elevated_and_wait() returns when the UAC prompt
# was declined -- distinct from any real exit code a script would use.
ELEVATION_DECLINED = -1223
ELEVATION_FAILED = -1


def run_elevated_and_wait(exe: str, params: str, timeout: float = 60.0) -> int:
    """Runs `exe params` elevated (one UAC consent prompt) via
    ShellExecuteExW with SEE_MASK_NOCLOSEPROCESS, which -- unlike plain
    ShellExecuteW -- hands back a real process handle. That lets this
    wait for the elevated process to actually finish and read its exit
    code, instead of polling for a marker file in a user-writable temp
    folder (which also meant the script itself sat in a folder any
    unelevated process could rewrite before the elevated run picked it
    up).

    Returns the process exit code, ELEVATION_DECLINED if the person
    declined the UAC prompt, or ELEVATION_FAILED for any other launch
    failure or a timeout. Never raises. Off Windows: ELEVATION_FAILED."""
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
    """Environment for every program ConanOps starts (game servers,
    SteamCMD, its own relaunch after an update, helper scripts).

    A PyInstaller one-file build unpacks itself into a temp folder
    (_MEIxxxxx) and deletes it on exit. Without this, a ConanOps that
    relaunches itself (after an update) is told by the inherited
    environment to REUSE the old instance's temp folder -- so the new
    window would run old code, and the old process can't delete the
    folder on exit ("Failed to remove temporary directory").
    PYINSTALLER_RESET_ENVIRONMENT=1 makes the new copy unpack its own.
    PyInstaller's private variables, and any PATH entry pointing into
    the temp folder, are dropped so nothing else inherits them either."""
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
