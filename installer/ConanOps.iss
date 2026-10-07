; ConanOps installer (Inno Setup 6 -- https://jrsoftware.org/isinfo.php)
; Built by BUILD_EXE.bat / tools/build_exe_wine.sh after PyInstaller:
;   ISCC.exe /DMyAppVersion=1.0.0 installer\ConanOps.iss  ->  dist\ConanOps-Setup-1.0.0.exe
;
; Why these choices:
;  * Per-user install, no admin prompt (PrivilegesRequired=lowest) into
;    C:\ConanOps by default: a folder the person owns, so ConanOps' own
;    self-updater can replace its files, and with no spaces in the path,
;    so servers/SteamCMD can live in ConanOps' "data" subfolder (SteamCMD
;    fails on paths with spaces -- see conanops_paths.py). Program Files
;    would break both.
;  * Uninstall removes only the program. Servers, worlds, backups and
;    settings in the "data" subfolder are never installer files, so
;    they're left in place. ConanOps.exe --uninstall-cleanup removes what
;    the app registered with Windows (sign-in entry, background task).

#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#define MyAppName "ConanOps"
#define MyAppExe "ConanOps.exe"

[Setup]
AppId={{6F1E3C52-8D4B-4C8E-9B57-2C1A0F4E7D31}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher=ConanOps
DefaultDirName={sd}\ConanOps
DisableProgramGroupPage=yes
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=ConanOps-Setup-{#MyAppVersion}
SetupIconFile=..\assets\conanops.ico
UninstallDisplayIcon={app}\{#MyAppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=yes
RestartApplications=no
DirExistsWarning=no
; Signing (optional): build with /DSignTool=1 and define a sign tool named
; "signtool" in the Inno Setup IDE (Tools > Configure Sign Tools) or with
; ISCC /S"signtool=signtool.exe sign /fd sha256 /tr http://timestamp.digicert.com /td sha256 /a $f"
#ifdef SignTool
SignTool=signtool
SignedUninstaller=yes
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\{#MyAppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\THIRD_PARTY_NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\{#MyAppExe}"; Parameters: "--uninstall-cleanup"; Flags: waituntilterminated; RunOnceId: "ConanOpsCleanup"

[Code]
// A path with a space still works for ConanOps itself, but its server
// data then goes to C:\Users\Public\ConanOps instead of the install
// folder (SteamCMD can't handle spaces) -- say so up front.
function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = wpSelectDir) and (Pos(' ', WizardDirValue) > 0) then
    Result := MsgBox('This folder has a space in its path. ConanOps will work, but SteamCMD can''t ' +
      'use folders with spaces, so your servers will be stored in C:\Users\Public\ConanOps instead of ' +
      'inside this folder.' + #13#10#13#10 + 'Use this folder anyway?', mbConfirmation, MB_YESNO) = IDYES;
end;

// Uninstalling never deletes servers or backups -- remind people where
// their data is, since the folder will be left behind on purpose.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and DirExists(ExpandConstant('{app}\data')) then
    MsgBox('ConanOps has been removed. Your servers, worlds, backups and settings were kept in:' + #13#10 +
      ExpandConstant('{app}\data') + #13#10#13#10 +
      'Delete that folder yourself if you no longer need them.', mbInformation, MB_OK);
end;
