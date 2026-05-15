; Fry Hub — Inno Setup wrapper for the PyInstaller installer exe.
; Built via: build_cli.py --inno  (see build_inno() in build_cli.py)
; Direct compile:  ISCC.exe /DAppVersion=4.0.16 packaging/fryhub.iss

#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif

#define AppName        "Fry Hub"
#define AppPublisher   "Fry Networks"
#define AppExeName     "frynetworks_installer.exe"
#define AppGroupName   "Fry Hub"

[Setup]
AppId={{B8E3F1A2-7C4D-4E9B-9F1A-3D5C8E2F1B7A}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={commonpf}\FryNetworks
DefaultGroupName={#AppGroupName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=FryHubSetup-{#AppVersion}
SetupIconFile=..\resources\fryhub.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ShowLanguageDialog=no
CloseApplications=yes
RestartApplications=no
AppMutex=FryNetworksHubInstanceMutex_v1

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "..\dist\frynetworks_installer_v{#AppVersion}.exe"; \
  DestDir: "{app}"; DestName: "{#AppExeName}"; Flags: ignoreversion
Source: "..\tools\register_updater_task.ps1"; \
  DestDir: "{app}\tools"; Flags: ignoreversion
; Updater exe — built by build_cli.py separately.
Source: "..\dist\frynetworks_updater.exe"; \
  DestDir: "{app}"; DestName: "frynetworks_updater.exe"; \
  Flags: ignoreversion

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; \
  GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{commondesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; \
  Tasks: desktopicon

[Registry]
Root: HKLM; Subkey: "Software\FryNetworks"; ValueType: string; \
  ValueName: "InstallDir"; ValueData: "{app}"; Flags: uninsdeletevalue
Root: HKLM; Subkey: "Software\FryNetworks"; ValueType: string; \
  ValueName: "Version";    ValueData: "{#AppVersion}"; Flags: uninsdeletevalue

[Run]
; Register the FryNetworksUpdater scheduled task. Idempotent per recon.
Filename: "powershell.exe"; \
  Parameters: "-ExecutionPolicy Bypass -File ""{app}\tools\register_updater_task.ps1"" -UpdaterPath ""{app}\frynetworks_updater.exe"""; \
  Flags: runhidden waituntilterminated; \
  StatusMsg: "Registering updater scheduled task..."

[UninstallRun]
; CLI-driven full uninstall of all miners + data
Filename: "{app}\{#AppExeName}"; Parameters: "uninstall --all --remove-data -y"; \
  Flags: runhidden waituntilterminated; RunOnceId: "FryNetworksUninstallAll"
; Remove the FryNetworksUpdater scheduled task on uninstall (redundant safety net).
Filename: "schtasks.exe"; Parameters: "/delete /tn \FryNetworks\FryNetworksUpdater /f"; \
  Flags: runhidden; RunOnceId: "DelUpdaterTask"
