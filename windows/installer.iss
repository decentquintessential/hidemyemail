; Inno Setup script for Hide My Email. Built by .github/workflows/windows.yml.
; Installs per user (no admin rights) into %LOCALAPPDATA%\Programs.

#define AppVersion GetEnv("APP_VERSION")
#if AppVersion == ""
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{4B40960E-5963-4010-B434-53D0B01FEA77}
AppName=Hide My Email
AppVersion={#AppVersion}
AppPublisher=Hide My Email
DefaultDirName={localappdata}\Programs\Hide My Email
DisableDirPage=yes
DefaultGroupName=Hide My Email
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=HideMyEmail-Setup
SetupIconFile=..\build\icon.ico
UninstallDisplayIcon={app}\HideMyEmail.exe
UninstallDisplayName=Hide My Email
; Force-close a running copy if it doesn't close by itself, instead of cancelling the update.
CloseApplications=force
RestartApplications=no
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked
Name: "startup"; Description: "Start Hide My Email when I sign in to Windows (tray icon and Ctrl+Alt+Shift+H)"; Flags: unchecked

[Files]
; The app is a folder (HideMyEmail.exe plus _internal\), not a single self-unpacking .exe.
Source: "..\dist\HideMyEmail\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; Remove the previous version's libraries so no stale files are left behind.
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{userprograms}\Hide My Email"; Filename: "{app}\HideMyEmail.exe"
Name: "{userdesktop}\Hide My Email"; Filename: "{app}\HideMyEmail.exe"; Tasks: desktopicon
Name: "{userstartup}\Hide My Email"; Filename: "{app}\HideMyEmail.exe"; Tasks: startup

[Run]
Filename: "{app}\HideMyEmail.exe"; Description: "Open Hide My Email"; Flags: nowait postinstall skipifsilent
; The app's updater runs this installer with /VERYSILENT /RELAUNCH=1 and quits; reopen it afterwards.
Filename: "{app}\HideMyEmail.exe"; Flags: nowait; Check: ShouldRelaunch

[Code]
function InitializeSetup: Boolean;
begin
  // When the app starts an update it quits straight away; give it a moment to exit.
  if ExpandConstant('{param:RELAUNCH|0}') = '1' then
    Sleep(3000);
  Result := True;
end;

function ShouldRelaunch: Boolean;
begin
  Result := ExpandConstant('{param:RELAUNCH|0}') = '1';
end;
