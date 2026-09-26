; Siphon's Windows installer: per user (no admin rights), Start-menu shortcut, optional desktop
; shortcut, uninstaller. .github/workflows/windows.yml compiles it with
;   ISCC /DAppVersion=<__version__ of siphon/__init__.py> /DSourceDir=<PyInstaller's Siphon folder> /DOutputDir=<dir> siphon.iss
; Settings (%APPDATA%\Siphon), engine updates and the cache (%LOCALAPPDATA%\Siphon) and the music
; itself are the user's, so the uninstaller leaves them.
; Siphon updates itself by running a newer installer silently as it quits (siphon/updater.py) with two
; switches of this script's own: /WAITPID=<pid> waits for that Siphon process to end before anything
; is replaced, and /RELAUNCH starts Siphon again when the silent install is done.

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
Filename: "{app}\Siphon.exe"; Flags: nowait; Check: Relaunch

[Code]
const
  SYNCHRONIZE = $00100000;

function OpenProcess(Access: DWORD; InheritHandle: BOOL; ProcessId: DWORD): THandle;
  external 'OpenProcess@kernel32.dll stdcall';
function WaitForSingleObject(Handle: THandle; Milliseconds: DWORD): DWORD;
  external 'WaitForSingleObject@kernel32.dll stdcall';
function CloseHandle(Handle: THandle): BOOL;
  external 'CloseHandle@kernel32.dll stdcall';

function HasSwitch(const Name: String): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), Name) = 0 then
      Result := True;
end;

function InitializeSetup(): Boolean;
var
  Siphon: THandle;
begin
  { /WAITPID: the Siphon that started this update is still quitting; its files are free once it has gone.
    A process that has already ended, or a wrong number, opens nothing and nothing is waited for. }
  Siphon := OpenProcess(SYNCHRONIZE, False, StrToIntDef(ExpandConstant('{param:WAITPID|0}'), 0));
  if Siphon <> 0 then
  begin
    WaitForSingleObject(Siphon, 30000);
    CloseHandle(Siphon);
  end;
  Result := True;
end;

function Relaunch(): Boolean;
begin
  { Only silent: an install with its wizard offers to start Siphon on its last page instead. }
  Result := WizardSilent and HasSwitch('/RELAUNCH');
end;
