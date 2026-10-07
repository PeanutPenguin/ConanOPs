"""
The Microsoft Visual C++ 2015-2022 (x64) runtime the Conan Exiles
dedicated server needs.

Since the Unreal Engine 5 "Enhanced" update, the server is strict about
this runtime. Installing through the Steam client runs Microsoft's
installer automatically; SteamCMD -- which ConanOps uses -- doesn't run
those extra installers, so a fresh PC can end up with a server that
won't start ("Microsoft Visual C++ ... is required" / missing
VCRUNTIME140_1.dll or MSVCP140.dll).

  status()   -- installed / outdated / missing, from the registry entry
                Microsoft's installer writes, falling back to whether
                the DLLs themselves exist.
  install()  -- downloads Microsoft's official installer, and in ONE
                elevated script copies it to an admin-only folder,
                checks it is validly signed by Microsoft, and runs it
                silently. One Windows permission prompt.
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

# Oldest runtime treated as current. UE5 builds use the VS 2022 toolset;
# older 14.x runtimes are where "missing the right minor version"
# failures come from. Installing a newer runtime over an older one is
# always safe -- every 2015-2022 release is backward compatible.
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
    # No registry entry. The DLLs can still be present (installed by
    # another program's own copy of the redistributable); trust them --
    # a false "missing" would block launches for nothing.
    if _dlls_present():
        return RuntimeStatus(STATUS_OK, None)
    return RuntimeStatus(STATUS_MISSING, None)


def needs_install() -> bool:
    return status().state in (STATUS_MISSING, STATUS_OUTDATED)


# Results of install()
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
    """Runs elevated. The downloaded file sits in a folder any program
    the person runs could overwrite, so it's copied into a folder only
    administrators can write to, its Microsoft signature is checked
    THERE, and only that checked copy runs. Exit codes: 0 / 3010 (needs
    restart) / 1638 (a newer version is already installed) are success;
    95 = signature check failed."""
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
    """Downloads and installs the runtime (one permission prompt).
    Blocking -- call it from a worker thread."""
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
        # Installer reported success but the new version isn't visible
        # yet -- typically pending a restart.
        progress("Visual C++ runtime installed; Windows may need a restart before it's fully in place.")
        return INSTALL_OK_RESTART
    progress("Installing the Visual C++ runtime failed -- see conanops.log.")
    return INSTALL_FAILED


# ConanOps installed the runtime on a PC that didn't have it -> recorded
# here, so a full "Delete Everything" can offer to take it back off.
# (Upgrading an existing, older runtime isn't recorded: something else
# put the runtime there first.)
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
    """PowerShell (elevated) that silently uninstalls the Visual C++
    2015-2022 / v14 x64 runtime via its own registered uninstaller."""
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
