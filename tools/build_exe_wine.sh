#!/usr/bin/env bash
# Builds ConanOps.exe on LINUX, using Wine + a standalone Windows Python
# (PyInstaller can't cross-compile, so the Windows toolchain runs under
# Wine). This is how the release .exe was produced.
#
#   sudo apt install wine gcc-mingw-w64-x86-64
#   tools/build_exe_wine.sh            -> dist/ConanOps.exe
#
# Two Wine quirks this handles:
#  * Wine has no ICU (icuuc.dll), which Qt6Core imports. Without it
#    PySide6 can't load at build time and PyInstaller silently leaves
#    out Qt's plugins -- the .exe would then fail on Windows with "no Qt
#    platform plugin". A tiny stand-in DLL is compiled into Wine's
#    System32 for the BUILD only; it is never bundled (real Windows
#    10/11 ships the genuine icuuc.dll).
#  * Python under Wine needs a terminal for its standard streams, so
#    the build runs under `script`.
set -euo pipefail
cd "$(dirname "$0")/.."
WORK="${WORK:-$HOME/.conanops-winbuild}"
PYTAG="${PYTAG:-20261003}"
PYVER="${PYVER:-3.12.15}"
export WINEPREFIX="$WORK/prefix" WINEDEBUG=-all WINEARCH=win64
mkdir -p "$WORK"

if [ ! -x "$WORK/python/python.exe" ]; then
  curl -sL -o "$WORK/py.tgz" "https://github.com/astral-sh/python-build-standalone/releases/download/${PYTAG}/cpython-${PYVER}%2B${PYTAG}-x86_64-pc-windows-msvc-install_only.tar.gz"
  tar xzf "$WORK/py.tgz" -C "$WORK"
fi
[ -d "$WINEPREFIX" ] || wineboot -i >/dev/null 2>&1

cat > "$WORK/icuuc.c" <<'C'
#include <stddef.h>
#define FAIL(n) __declspec(dllexport) int n() { return 0; }
__declspec(dllexport) void* ucnv_open(const char* n, int* e) { if (e) *e = 1; return NULL; }
FAIL(ucnv_close) FAIL(ucnv_reset) FAIL(ucnv_getMaxCharSize) FAIL(ucnv_getName)
FAIL(ucnv_getToUCallBack) FAIL(ucnv_getFromUCallBack) FAIL(ucnv_setToUCallBack) FAIL(ucnv_setFromUCallBack)
FAIL(ucnv_cbToUWriteUChars) FAIL(ucnv_toUnicode) FAIL(ucnv_cbFromUWriteUChars) FAIL(ucnv_toUCountPending)
FAIL(ucnv_fromUCountPending) FAIL(ucnv_getStandardName) FAIL(ucnv_getAvailableName) FAIL(ucnv_countAvailable)
FAIL(ucnv_fromUnicode) FAIL(UCNV_FROM_U_CALLBACK_SUBSTITUTE) FAIL(UCNV_TO_U_CALLBACK_SUBSTITUTE)
C
x86_64-w64-mingw32-gcc -shared -O2 -o "$WINEPREFIX/drive_c/windows/system32/icuuc.dll" "$WORK/icuuc.c"

PY="$WORK/python/python.exe"
script -qc "wine '$PY' -m pip install -q --upgrade pyinstaller -r requirements.txt" /dev/null
script -qc "wine '$PY' -m PyInstaller --noconfirm --onefile --windowed --name ConanOps --icon 'assets\\conanops.ico' --add-data 'assets;assets' main.py" /dev/null
echo "Built: dist/ConanOps.exe"

# Optional signing on Linux with osslsigncode (apt install osslsigncode):
#   CONANOPS_SIGN_PFX=/path/cert.pfx CONANOPS_SIGN_PASSWORD=... tools/build_exe_wine.sh
sign() {
  [ -n "${CONANOPS_SIGN_PFX:-}" ] || return 0
  echo "Signing $1"
  osslsigncode sign -pkcs12 "$CONANOPS_SIGN_PFX" -pass "${CONANOPS_SIGN_PASSWORD:-}" -h sha256 \
    -ts http://timestamp.digicert.com -in "$1" -out "$1.signed"
  mv "$1.signed" "$1"
}
sign dist/ConanOps.exe
(cd dist && rm -f ConanOps-update.zip && zip -q ConanOps-update.zip ConanOps.exe)

# Optional installer: put Inno Setup 6 in $WORK/inno (install it once under
# this Wine prefix, or copy an existing "Inno Setup 6" folder there).
VER="$(python3 -c 'import version; print(version.VERSION)')"
if [ -x "$WORK/inno/ISCC.exe" ] || [ -f "$WORK/inno/ISCC.exe" ]; then
  wine "$WORK/inno/ISCC.exe" /Q "/DMyAppVersion=$VER" 'installer\ConanOps.iss'
  sign "dist/ConanOps-Setup-$VER.exe"
  echo "Built: dist/ConanOps-Setup-$VER.exe"
else
  echo "Inno Setup not found at $WORK/inno/ISCC.exe -- skipped the installer."
fi
