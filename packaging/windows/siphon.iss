; Siphon's Windows installer: per user (no admin rights), Start-menu shortcut, optional desktop
; shortcut, uninstaller. .github/workflows/windows.yml compiles it with
;   ISCC /DAppVersion=<__version__ of siphon/__init__.py> /DSourceDir=<PyInstaller's Siphon folder> /DOutputDir=<dir> siphon.iss
; Settings (%APPDATA%\Siphon), engine updates and the cache (%LOCALAPPDATA%\Siphon) and the music
; itself are the user's, so the uninstaller leaves them.

#ifndef AppVersion
  #error Pass /DAppVersion=<the version in siphon/__init__.py>
#endif
#ifndef SourceDir
  #error Pass /DSourceDir=<the Siphon folder PyInstaller built>
#endif
#ifndef OutputDir
  #define OutputDir "."
#endif

[Setup]
; Never change AppId: it is how an update finds the installed Siphon.
AppId={{A7D6F0BC-783B-4BCF-AAD1-CC4448D09A32}
AppName=Siphon
AppVersion={#AppVersion}
AppVerName=Siphon {#AppVersion}
AppPublisher=GGRRK
VersionInfoVersion={#AppVersion}
PrivilegesRequired=lowest
; {autopf} is %LOCALAPPDATA%\Programs for a per-user install
DefaultDirName={autopf}\Siphon
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern
SetupIconFile=siphon.ico
UninstallDisplayIcon={app}\Siphon.exe
OutputDir={#OutputDir}
OutputBaseFilename=Siphon-{#AppVersion}-Setup
Compression=lzma2/ultra64
SolidCompression=yes
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; An update replaces bin\ as a whole, so no DLL from an older version is left behind.
Type: filesandordirs; Name: "{app}\bin"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Siphon"; Filename: "{app}\Siphon.exe"
Name: "{autodesktop}\Siphon"; Filename: "{app}\Siphon.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Siphon.exe"; Description: "{cm:LaunchProgram,Siphon}"; Flags: nowait postinstall skipifsilent
