#define AppName "AkremMobile Installment Manager"
#define AppVersion "0.1.0"
#define AppExeName "AkremMobile.exe"

[Setup]
AppId={{74B61613-3CD0-46C5-97B3-5D7D22BF4119}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=AkremMobile
DefaultDirName={localappdata}\Programs\AkremMobile
DefaultGroupName=AkremMobile
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64
OutputDir=..\dist\installer
OutputBaseFilename=AkremMobile-Setup-{#AppVersion}
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
SetupIconFile=..\app\resources\icon.ico
WizardSmallImageFile=..\app\resources\installer_small.bmp
WizardImageFile=..\app\resources\installer_large.bmp

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked
Name: "startupicon"; Description: "Start AkremMobile when I sign in to Windows"; GroupDescription: "Background:"; Flags: unchecked

[Files]
Source: "..\dist\AkremMobile\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; AppUserModelID must match WINDOWS_APP_ID in app/ui/tray.py so Windows
; attributes the app's toast notifications to this shortcut.
Name: "{group}\AkremMobile"; Filename: "{app}\{#AppExeName}"; AppUserModelID: "AkremMobile.InstallmentManager"
Name: "{autodesktop}\AkremMobile"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon; AppUserModelID: "AkremMobile.InstallmentManager"
Name: "{userstartup}\AkremMobile"; Filename: "{app}\{#AppExeName}"; Tasks: startupicon; AppUserModelID: "AkremMobile.InstallmentManager"

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch AkremMobile"; Flags: nowait postinstall skipifsilent
