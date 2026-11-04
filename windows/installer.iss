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
CloseApplications=yes
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked
Name: "startup"; Description: "Start Hide My Email when I sign in to Windows (tray icon and Ctrl+Alt+Shift+H)"; Flags: unchecked

[Files]
Source: "..\dist\HideMyEmail.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{userprograms}\Hide My Email"; Filename: "{app}\HideMyEmail.exe"
Name: "{userdesktop}\Hide My Email"; Filename: "{app}\HideMyEmail.exe"; Tasks: desktopicon
Name: "{userstartup}\Hide My Email"; Filename: "{app}\HideMyEmail.exe"; Tasks: startup

[Run]
Filename: "{app}\HideMyEmail.exe"; Description: "Open Hide My Email"; Flags: nowait postinstall skipifsilent
