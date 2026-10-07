"""
Removes ConanOps' own program -- the last step of App Settings'
"Uninstall ConanOps (Keep My Data)" and "Delete Everything".

The running ConanOps (and, for a packaged build, its .exe and its
unpacked temp copy) can't delete itself, so a small detached PowerShell
helper does it after ConanOps exits:

  1. waits until every ConanOps process involved has exited (a packaged
     build runs as two: the launcher and the app itself);
  2. if ConanOps was installed with the installer, runs its uninstaller
     silently -- so the Start menu entry, desktop shortcut and the
     "Installed apps" entry go too, exactly as uninstalling from Windows
     Settings would -- and waits for it to finish;
  3. deletes the program folder (all of it, or everything except the
     data subfolder for "keep my data");
  4. deletes any extra paths it was given -- ConanOps' own data folders
     and log, which ConanOps itself can't delete while it has them open;
  5. deletes itself.

PowerShell rather than a .bat: batch files mangle paths with non-English
characters or a "%" in them, which would leave things behind.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from typing import Iterable, List, Optional

import powershell
import proc_utils


def _pids_to_wait_for(pid: Optional[int]) -> List[int]:
    pids = [pid if pid is not None else os.getpid()]
    if pid is None and getattr(sys, "frozen", False):
        try:
            import psutil
            parent = psutil.Process(os.getpid()).parent()
            if parent is not None and os.path.basename(parent.exe()).lower() == os.path.basename(sys.executable).lower():
                pids.append(parent.pid)  # the one-file launcher
        except Exception:  # noqa: BLE001
            pass
    return pids


_REMOVE_HARD = r'''function Remove-Hard([string]$p) {
  for ($i = 0; $i -lt 5 -and (Test-Path -LiteralPath $p); $i++) {
    Get-ChildItem -LiteralPath $p -Recurse -Force | ForEach-Object { try { $_.Attributes = 'Normal' } catch {} }
    try { (Get-Item -LiteralPath $p -Force).Attributes = 'Normal' } catch {}
    Remove-Item -LiteralPath $p -Recurse -Force
    if (Test-Path -LiteralPath $p) {
      if ((Get-Item -LiteralPath $p -Force).PSIsContainer) { cmd.exe /c rmdir /s /q ('\\?\' + $p) | Out-Null }
      Start-Sleep -Seconds 2
    }
  }
}
'''

_RUN_UNINSTALLER = r'''$unins = Get-ChildItem -LiteralPath $app -Filter 'unins*.exe' -File | Select-Object -First 1
if ($unins) {
  Start-Process -FilePath $unins.FullName -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART'
  $d = (Get-Date).AddMinutes(3)
  while ((Test-Path -LiteralPath $unins.FullName) -and (Get-Date) -lt $d) { Start-Sleep -Seconds 1 }
  Start-Sleep -Seconds 2
}
'''


def own_files_if_shared_folder(install_dir: str) -> Optional[List[str]]:
    """None if install_dir is ConanOps' own folder (safe to delete whole);
    otherwise the list of ConanOps' own files in it.

    ConanOps.exe is often just run from wherever it was downloaded --
    Downloads, the Desktop. Deleting "the program folder" there would
    take everything else in it, so in that case only ConanOps' own
    files go: the exe, its update manifest, files the manifest lists,
    and leftovers from past updates."""
    import cleanup
    import self_update
    folder = os.path.abspath(install_dir)
    has_uninstaller = any(n.lower().startswith("unins") and n.lower().endswith(".exe")
                          for n in (os.listdir(folder) if os.path.isdir(folder) else []))
    dedicated = has_uninstaller or "conanops" in os.path.basename(folder).lower()
    if dedicated and cleanup.is_safe_to_delete(folder):
        return None
    files = set()
    if getattr(sys, "frozen", False):
        files.add(os.path.basename(sys.executable))
    for rel in (self_update._read_manifest(folder) or set()):
        files.add(rel.split("/")[0])
    files.add(self_update._MANIFEST_NAME)
    if os.path.isdir(folder):
        for n in os.listdir(folder):
            if n.endswith(".conanops-old"):
                files.add(n)
    return sorted(os.path.join(folder, f) for f in files)


def build_uninstall_script(install_dir: str, pids, preserve_name: Optional[str] = None,
                           extra_paths: Iterable[str] = (), script_dir: str = "",
                           own_files: Optional[List[str]] = None) -> str:
    q = powershell.ps_str
    pids = [int(p) for p in (pids if isinstance(pids, (list, tuple)) else [pids])]
    lines = [
        "$ErrorActionPreference = 'SilentlyContinue'\n",
        "$ProgressPreference = 'SilentlyContinue'\n",
        f"$pids = @({','.join(str(p) for p in pids)})\n",
        "$deadline = (Get-Date).AddMinutes(5)\n",
        "while ((Get-Date) -lt $deadline -and ($pids | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })) "
        "{ Start-Sleep -Milliseconds 500 }\n",
        "Start-Sleep -Seconds 2\n",
        _REMOVE_HARD,
        f"$app = {q(os.path.abspath(install_dir))}\n",
        _RUN_UNINSTALLER,
    ]
    if own_files is not None:
        # Shared folder (Downloads, Desktop...): only ConanOps' own files.
        for f in own_files:
            lines.append(f"Remove-Hard {q(f)}\n")
    elif preserve_name is None:
        lines.append("Remove-Hard $app\n")
    else:
        lines.append(
            f"Get-ChildItem -LiteralPath $app -Force | Where-Object {{ $_.Name -ne {q(preserve_name)} }} | "
            "ForEach-Object { Remove-Hard $_.FullName }\n"
        )
    for p in extra_paths:
        if p:
            lines.append(f"Remove-Hard {q(os.path.abspath(p))}\n")
    if script_dir:
        lines.append(f"Remove-Item -LiteralPath {q(script_dir)} -Recurse -Force\n")
    return "".join(lines)


def spawn_self_delete_helper(install_dir: str, pid: Optional[int] = None, preserve_name: Optional[str] = None,
                             extra_paths: Iterable[str] = ()) -> str:
    """Writes the helper script to a temp folder (outside everything it
    deletes) and launches it detached. The caller must exit right after.
    Returns the script's path."""
    folder = tempfile.mkdtemp(prefix="conanops-cleanup-")
    script_path = os.path.join(folder, "cleanup.ps1")
    text = build_uninstall_script(install_dir, _pids_to_wait_for(pid), preserve_name, extra_paths, folder,
                                  own_files=own_files_if_shared_folder(install_dir))
    with open(script_path, "w", encoding="utf-8-sig") as f:
        f.write(text)

    runner = (
        f"$s = [IO.File]::ReadAllText({powershell.ps_str(script_path)}, [Text.Encoding]::UTF8).TrimStart([char]0xFEFF); "
        "Invoke-Expression $s"
    )
    creationflags = 0
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    subprocess.Popen(
        [powershell.powershell_exe(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-WindowStyle", "Hidden", "-Command", runner],
        creationflags=creationflags, close_fds=True, env=proc_utils.child_env(),
    )
    return script_path
