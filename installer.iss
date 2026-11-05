; Inno Setup script for Tenant Launcher. Built by build.ps1 (or CI) after PyInstaller.
; Per-user install (no admin needed): %LOCALAPPDATA%\Programs\Tenant Launcher

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{7B1C2E0A-5D7F-4C1B-9B7E-TENANTLAUNCH}
AppName=Tenant Launcher
AppVersion={#AppVersion}
AppPublisher=Xenex Systems
DefaultDirName={localappdata}\Programs\Tenant Launcher
DefaultGroupName=Tenant Launcher
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
OutputDir=installer
OutputBaseFilename=TenantLauncher-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\TenantLauncher.exe
SetupIconFile=assets\icon.ico
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"

[Files]
Source: "dist\TenantLauncher\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Tenant Launcher"; Filename: "{app}\TenantLauncher.exe"
Name: "{autodesktop}\Tenant Launcher"; Filename: "{app}\TenantLauncher.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\TenantLauncher.exe"; Description: "Start Tenant Launcher"; Flags: nowait postinstall skipifsilent
