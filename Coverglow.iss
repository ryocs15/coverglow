; Installer for dist\Coverglow.exe (build the exe first). Inno Setup 6:
;   ISCC Coverglow.iss  ->  dist\CoverglowSetup.exe
; Settings and stats live in %APPDATA%\Coverglow, not in the install folder.

#define AppVersion "1.2.0"

[Setup]
AppId={{52846197-9AA4-4AF0-962C-FC389C3B927B}
AppName=Coverglow
AppVersion={#AppVersion}
AppPublisher=ryocs15
AppPublisherURL=https://github.com/ryocs15/coverglow
DefaultDirName={autopf}\Coverglow
DisableDirPage=no
DisableProgramGroupPage=yes
; per-user by default (no admin prompt); the user can still pick an all-users install
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=dist
OutputBaseFilename=CoverglowSetup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\Coverglow.exe
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
LicenseFile=LICENSE

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "tr"; MessagesFile: "compiler:Languages\Turkish.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "dist\Coverglow.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Coverglow"; Filename: "{app}\Coverglow.exe"
Name: "{autodesktop}\Coverglow"; Filename: "{app}\Coverglow.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Coverglow.exe"; Description: "{cm:LaunchProgram,Coverglow}"; Flags: nowait postinstall skipifsilent

[Code]
// the widget has no window to close, so stop a running copy before files are replaced or removed
procedure KillApp;
var R: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/f /im Coverglow.exe', '', SW_HIDE, ewWaitUntilTerminated, R);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  KillApp;
  Result := '';
end;

function InitializeUninstall: Boolean;
begin
  KillApp;
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  // the app writes its own startup entry; remove it so Windows doesn't try to launch a deleted exe
  if CurUninstallStep = usPostUninstall then
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'Coverglow');
end;
