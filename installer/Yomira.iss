#ifndef AppVersion
  #error "Compile through build_installer.ps1 so AppVersion stays aligned with the backend."
#endif

#define BundleDir GetEnv("YOMIRA_BUNDLE_DIR")
#if BundleDir == ""
  #error "YOMIRA_BUNDLE_DIR must point to the completed K258 onedir bundle."
#endif

[Setup]
AppId={{10ECACF2-F35D-4E4E-A962-6F4E7B11E01E}
AppName=Yomira
AppVersion={#AppVersion}
AppPublisher=Yomira
DefaultDirName={localappdata}\Programs\Yomira
DisableDirPage=yes
UsePreviousAppDir=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
AppMutex=Local\Yomira.V1.Backend
CloseApplications=no
Uninstallable=yes
UninstallDisplayIcon={app}\Yomira.exe
DisableProgramGroupPage=yes
OutputBaseFilename=Yomira-Setup-{#AppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Files]
; The K258 onedir tree is the only installed payload. DataRoot and library_root stay external.
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Yomira"; Filename: "{app}\Yomira.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\Yomira"; Filename: "{app}\Yomira.exe"; WorkingDir: "{app}"

[Run]
Filename: "{app}\Yomira.exe"; WorkingDir: "{app}"; Description: "Abrir Yomira"; Flags: postinstall nowait skipifsilent
