"""Detects and installs the MSVC 2015-2022 x64 runtime the server needs.

Since the UE5 update the server won't start without it, and SteamCMD (unlike the
Steam client) doesn't run Microsoft's installer. install() verifies the Microsoft
signature from an admin-only folder before running it (one UAC prompt).
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

import applog
import powershell

_log = applog.get_logger(__name__)

# Microsoft's permanent "latest x64 redistributable" links, tried in order.
DOWNLOAD_URLS = (
    "https://aka.ms/vc14/vc_redist.x64.exe",
    "https://aka.ms/vs/17/release/vc_redist.x64.exe",
)

# Oldest runtime treated as current (UE5 uses the VS 2022 toolset). Newer
# 2015-2022 runtimes are backward compatible, so upgrading is always safe.
MIN_VERSION: Tuple[int, int] = (14, 40)

_REG_PATHS = (
    r"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64",
    r"SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64",
)
_DLLS = ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll")

STATUS_OK = "ok"
STATUS_OUTDATED = "outdated"
STATUS_MISSING = "missing"
STATUS_UNKNOWN = "unknown"  # not on Windows / couldn't tell


@dataclass
class RuntimeStatus:
    state: str
    version: Optional[Tuple[int, ...]] = None

    @property
    def version_text(self) -> str:
        return ".".join(str(p) for p in self.version) if self.version else "unknown version"


def _registry_version() -> Optional[Tuple[int, ...]]:
    import winreg
    for path in _REG_PATHS:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                installed, _ = winreg.QueryValueEx(key, "Installed")
                if int(installed) != 1:
                    continue
                parts = []
                for name in ("Major", "Minor", "Bld", "Rbld"):
                    try:
                        parts.append(int(winreg.QueryValueEx(key, name)[0]))
                    except OSError:
                        break
                if len(parts) >= 2:
                    return tuple(parts)
                return (14, 0)
        except OSError:
            continue
    return None


def _dlls_present() -> bool:
    system32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    return all(os.path.isfile(os.path.join(system32, dll)) for dll in _DLLS)


def status() -> RuntimeStatus:
    if sys.platform != "win32":
        return RuntimeStatus(STATUS_UNKNOWN)
    try:
        version = _registry_version()
    except Exception as e:  # noqa: BLE001
        _log.warning(f"Couldn't read the Visual C++ runtime version: {e}")
        version = None
    if version is not None:
        if tuple(version[:2]) < MIN_VERSION:
            return RuntimeStatus(STATUS_OUTDATED, version)
        return RuntimeStatus(STATUS_OK, version)
    # No registry entry, but another program may have installed the DLLs; trust them.
    if _dlls_present():
        return RuntimeStatus(STATUS_OK, None)
    return RuntimeStatus(STATUS_MISSING, None)


def needs_install() -> bool:
    return status().state in (STATUS_MISSING, STATUS_OUTDATED)


INSTALL_OK = "ok"
INSTALL_OK_RESTART = "ok_restart"     # installed; Windows wants a restart for it to fully apply
INSTALL_DECLINED = "declined"
INSTALL_DOWNLOAD_FAILED = "download_failed"
INSTALL_FAILED = "failed"


def _download(progress: Callable[[str], None]) -> Optional[str]:
    folder = tempfile.mkdtemp(prefix="conanops-vcredist-")
    target = os.path.join(folder, "vc_redist.x64.exe")
    for url in DOWNLOAD_URLS:
        try:
            progress(f"Downloading the Microsoft Visual C++ runtime from {url} ...")
            with urllib.request.urlopen(url, timeout=60) as resp, open(target, "wb") as f:
                shutil.copyfileobj(resp, f)
            if os.path.getsize(target) > 1024 * 1024:  # a real installer is ~25 MB
                return target
        except Exception as e:  # noqa: BLE001 - try the next link
            _log.warning(f"Visual C++ runtime download from {url} failed: {e}")
    shutil.rmtree(folder, ignore_errors=True)
    return None


def _install_script(downloaded: str) -> str:
    """Elevated script. Copies the installer to an admin-only folder before checking
    its signature, so nothing unprivileged can swap it after the check.
    Installer codes 3010 (restart) and 1638 (newer present) map to 0; 95 = bad signature."""
    q = powershell.ps_str
    return (
        f"$src = {q(downloaded)}\n"
        "$dir = Join-Path $env:SystemRoot 'Temp\\ConanOps-vcredist'\n"
        "New-Item -ItemType Directory -Path $dir -Force | Out-Null\n"
        "$exe = Join-Path $dir 'vc_redist.x64.exe'\n"
        "Copy-Item -LiteralPath $src -Destination $exe -Force\n"
        "$sig = Get-AuthenticodeSignature -LiteralPath $exe\n"
        "if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') {\n"
        "  Remove-Item -LiteralPath $exe -Force -ErrorAction SilentlyContinue\n"
        "  exit 95\n"
        "}\n"
        "$p = Start-Process -FilePath $exe -ArgumentList '/install','/quiet','/norestart' -Wait -PassThru\n"
        "$code = $p.ExitCode\n"
        "Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue\n"
        "if ($code -eq 3010) { exit 0 }\n"
        "if ($code -eq 1638) { exit 0 }\n"
        "exit $code\n"
    )


def install(progress: Optional[Callable[[str], None]] = None) -> str:
    """Downloads and installs the runtime. Blocking; call from a worker thread."""
    progress = progress or (lambda _m: None)
    if sys.platform != "win32":
        return INSTALL_FAILED
    was_missing = status().state == STATUS_MISSING
    downloaded = _download(progress)
    if not downloaded:
        progress("Couldn't download the Visual C++ runtime -- check the internet connection.")
        return INSTALL_DOWNLOAD_FAILED
    try:
        progress("Installing the Microsoft Visual C++ runtime (Windows will ask for permission)...")
        outcome = powershell.run_privileged(_install_script(downloaded), timeout=600)
    finally:
        shutil.rmtree(os.path.dirname(downloaded), ignore_errors=True)
    if outcome == powershell.RUN_DECLINED:
        progress("The Visual C++ runtime wasn't installed -- the Windows permission prompt was declined.")
        return INSTALL_DECLINED
    after = status()
    if outcome == powershell.RUN_OK and was_missing:
        _record_installed_by_conanops()
    if after.state == STATUS_OK:
        progress(f"Visual C++ runtime installed ({after.version_text}).")
        return INSTALL_OK
    if outcome == powershell.RUN_OK:
        # Success, but the new version isn't visible yet (usually pending a restart).
        progress("Visual C++ runtime installed; Windows may need a restart before it's fully in place.")
        return INSTALL_OK_RESTART
    progress("Installing the Visual C++ runtime failed -- see conanops.log.")
    return INSTALL_FAILED


# Marker for "ConanOps installed the runtime where none existed" (not upgrades),
# so Delete Everything can offer to remove it.
def _marker_path() -> str:
    return os.path.join(os.path.expanduser("~"), "ConanOps", "vcredist-installed-by-conanops")


def installed_by_conanops() -> bool:
    return os.path.exists(_marker_path())


def _record_installed_by_conanops() -> None:
    try:
        os.makedirs(os.path.dirname(_marker_path()), exist_ok=True)
        with open(_marker_path(), "w", encoding="utf-8") as f:
            f.write("ConanOps installed the Microsoft Visual C++ x64 runtime on this PC.\n")
    except OSError as e:
        _log.warning(f"Couldn't record the Visual C++ install: {e}")


def uninstall_script() -> str:
    """Elevated PowerShell that silently uninstalls the x64 v14 runtime."""
    return (
        "try {\n"
        "$roots = 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall', "
        "'HKLM:\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall'\n"
        "$entries = Get-ChildItem $roots -ErrorAction SilentlyContinue | Get-ItemProperty -ErrorAction SilentlyContinue | "
        "Where-Object { $_.DisplayName -match 'Visual C\\+\\+ (2015-20\\d\\d|v14) Redistributable \\(x64\\)' -and $_.BundleProviderKey }\n"
        "foreach ($e in $entries) {\n"
        "  $cmd = [string]$e.QuietUninstallString\n"
        "  if (-not $cmd) { $cmd = [string]$e.UninstallString + ' /quiet' }\n"
        "  if ($cmd) { Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', ($cmd + ' /norestart') -Wait -WindowStyle Hidden }\n"
        "}\n"
        "} catch {}\n"
    )
