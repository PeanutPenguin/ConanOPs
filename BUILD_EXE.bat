@echo off
setlocal EnableExtensions EnableDelayedExpansion
rem Builds dist\ConanOps.exe -- and, if Inno Setup 6 is installed,
rem dist\ConanOps-Setup-<version>.exe -- from this folder on Windows.
rem Needs Python 3.10+ (python.org) on PATH. Takes a few minutes.
rem
rem Optional code signing (see SIGNING.md). Set ONE of these first:
rem   set CONANOPS_SIGN_PFX=C:\path\cert.pfx  and  set CONANOPS_SIGN_PASSWORD=...
rem   set CONANOPS_SIGN_THUMBPRINT=<certificate SHA1 thumbprint in your cert store / USB token>
rem   set CONANOPS_SIGN_COMMAND=<full custom signing command; %%FILE%% is replaced with the file>
cd /d "%~dp0"
python --version >nul 2>&1 || (echo Python isn't installed or isn't on PATH. Get it from python.org. & pause & exit /b 1)
echo Installing build tools...
python -m pip install --upgrade pyinstaller -r requirements.txt || (pause & exit /b 1)
echo Building ConanOps.exe...
python -m PyInstaller --noconfirm --onefile --windowed --name ConanOps --icon assets\conanops.ico --add-data "assets;assets" main.py || (pause & exit /b 1)

for /f "usebackq delims=" %%v in (`python -c "import version; print(version.VERSION)"`) do set "APPVER=%%v"

call :sign "%~dp0dist\ConanOps.exe" || (pause & exit /b 1)
echo Making the update file...
if exist "%~dp0dist\ConanOps-update.zip" del "%~dp0dist\ConanOps-update.zip"
powershell -NoProfile -Command "Compress-Archive -LiteralPath '%~dp0dist\ConanOps.exe' -DestinationPath '%~dp0dist\ConanOps-update.zip'" || (pause & exit /b 1)

set "ISCC="
for %%p in ("%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe" "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 7\ISCC.exe" "%LOCALAPPDATA%\Programs\Inno Setup 7\ISCC.exe") do (
  if not defined ISCC if exist "%%~p" set "ISCC=%%~p"
)
if not defined ISCC (
  where ISCC.exe >nul 2>&1 && set "ISCC=ISCC.exe"
)
if defined ISCC (
  echo Building installer...
  "!ISCC!" /Q /DMyAppVersion=%APPVER% installer\ConanOps.iss || (pause & exit /b 1)
  call :sign "%~dp0dist\ConanOps-Setup-%APPVER%.exe" || (pause & exit /b 1)
) else (
  echo Inno Setup 6 not found -- skipping the installer. Get it from https://jrsoftware.org/isinfo.php
)

echo.
echo Done: %~dp0dist
explorer "%~dp0dist"
exit /b 0

:sign
rem %1 = file to sign. Does nothing (successfully) if no signing is configured.
if defined CONANOPS_SIGN_COMMAND (
  set "CMD=!CONANOPS_SIGN_COMMAND:%%FILE%%=%~1!"
  echo Signing %~nx1...
  call !CMD! || (echo Signing failed. & exit /b 1)
  exit /b 0
)
if not defined CONANOPS_SIGN_PFX if not defined CONANOPS_SIGN_THUMBPRINT exit /b 0
set "SIGNTOOL="
where signtool.exe >nul 2>&1 && set "SIGNTOOL=signtool.exe"
if not defined SIGNTOOL (
  for /f "delims=" %%s in ('dir /b /s "%ProgramFiles(x86)%\Windows Kits\10\bin\*signtool.exe" 2^>nul ^| findstr /i "\\x64\\"') do set "SIGNTOOL=%%s"
)
if not defined SIGNTOOL (echo signtool.exe not found -- install the Windows SDK, or unset the CONANOPS_SIGN_* variables. & exit /b 1)
echo Signing %~nx1...
if defined CONANOPS_SIGN_PFX (
  "!SIGNTOOL!" sign /fd sha256 /tr http://timestamp.digicert.com /td sha256 /f "%CONANOPS_SIGN_PFX%" /p "%CONANOPS_SIGN_PASSWORD%" "%~1" || exit /b 1
) else (
  "!SIGNTOOL!" sign /fd sha256 /tr http://timestamp.digicert.com /td sha256 /sha1 %CONANOPS_SIGN_THUMBPRINT% "%~1" || exit /b 1
)
exit /b 0
