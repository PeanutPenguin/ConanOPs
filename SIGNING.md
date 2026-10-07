# Code signing ConanOps

Unsigned `.exe` files get the "Windows protected your PC" (SmartScreen) warning, and
PyInstaller-built apps are a common antivirus false positive. Signing fixes the
"unknown publisher" part and is what lets SmartScreen build reputation for you.
The build scripts already know how to sign -- you only need a certificate.

## 1. Get a certificate

Prices and programs change; check current terms before buying.

- **Azure Trusted Signing** (Microsoft's cloud signing service) -- usually the cheapest
  ongoing option (a low monthly fee), no USB token to manage. Eligibility has been
  limited by country and by organization vs. individual; check whether you qualify.
- **OV (Organization/Individual Validation) certificate** from a CA such as Sectigo,
  DigiCert, SSL.com or Certum. Since 2023 the private key must live on a hardware token
  or a cloud HSM, so expect a USB token or a cloud-signing add-on.
- **EV certificates** cost more and no longer give instant SmartScreen reputation, so
  they're rarely worth it for a project like this.
- **Open-source projects** can apply to SignPath Foundation for free signing.

SmartScreen reputation still builds over time (downloads without bad reports), even
when signed. Keep signing every release with the same certificate.

## 2. Sign during the build

`BUILD_EXE.bat` signs `dist\ConanOps.exe` and the installer when one of these is set:

```bat
rem A .pfx file (only possible with older/exportable certificates):
set CONANOPS_SIGN_PFX=C:\certs\conanops.pfx
set CONANOPS_SIGN_PASSWORD=your-password

rem A certificate in your Windows certificate store or on a USB token:
set CONANOPS_SIGN_THUMBPRINT=0123456789ABCDEF0123456789ABCDEF01234567

rem Anything else (e.g. Azure Trusted Signing's signtool dlib setup):
set CONANOPS_SIGN_COMMAND=signtool sign /fd sha256 /tr http://timestamp.acs.microsoft.com /td sha256 /dlib "C:\path\Azure.CodeSigning.Dlib.dll" /dmdf "C:\path\metadata.json" "%FILE%"

BUILD_EXE.bat
```

`signtool.exe` comes with the Windows SDK. On Linux, `tools/build_exe_wine.sh` signs with
`osslsigncode` when `CONANOPS_SIGN_PFX` is set.

## 3. If antivirus still flags a release

- Submit the file as a false positive: Microsoft at
  https://www.microsoft.com/wdsi/filesubmission, and to whichever other vendor flagged
  it (VirusTotal shows which).
- ConanOps no longer uses PowerShell's `-EncodedCommand` (a common malware pattern);
  scripts are written to a temp file and integrity-checked before an elevated run --
  see `powershell.py`.
- Rebuilding PyInstaller's bootloader from source is another known way to shed
  generic PyInstaller detections, if it ever becomes a recurring problem.
