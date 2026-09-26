; Per-user installer for the Windows desktop build.
;
;   iscc /DProductVersion=2.3.0 /DOutDir=C:\path\to\dist /DSourceDir=C:\path\to\dist packaging\windows-installer.iss
;
; PrivilegesRequired=lowest is the whole point: the app writes to per-user
; folders, so it never needs an administrator, and it never installs anything
; into Program Files where the data would become read-only.
;
; SourceDir is where the built binaries are, and it is passed rather than
; assumed: a relative path is resolved against the compiler's working
; directory, and every path a user builds in can contain spaces.

#ifndef ProductVersion
  #error ProductVersion is required: pass /DProductVersion=<version>
#endif
#ifndef OutDir
  #define OutDir "..\dist"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist"
#endif

#define AppName "Proxy Workbench"
#define AppPublisher "Proxy Workbench contributors"
#define AppExeName "proxy-workbench-gui.exe"
#define AppCliExeName "proxy-workbench-cli.exe"

[Setup]
AppId={{6C0B2F1E-6C3B-4E3E-9E2B-7C7B1D0E5A21}
AppName={#AppName}
AppVersion={#ProductVersion}
AppVerName={#AppName} {#ProductVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\Proxy Workbench
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir="{#OutDir}"
OutputBaseFilename=proxy-workbench-{#ProductVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Per-user install: no elevation prompt, no machine-wide writes.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#AppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "addtopath"; Description: "Add the command line tool to PATH"; GroupDescription: "Command line:"
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
; The note about where data goes ships with the installed program as well as
; in the portable zip: it is the only place a user is told that a per-user
; folder is used and that portable mode has to be asked for.
Source: "{#SourceDir}\proxy-workbench-gui.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\proxy-workbench-cli.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\portable-README.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Command line ({#AppExeName})"; Filename: "{app}\{#AppExeName}"; Parameters: "--help"; WorkingDir: "{app}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon
Name: "{autodesktop}\Command line"; Filename: "{app}\{#AppExeName}"; Parameters: "--help"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; \
  ValueData: "{olddata};{app}"; Check: NeedsAddPath(ExpandConstant('{app}')); Tasks: addtopath

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Start {#AppName}"; Flags: nowait postinstall skipifsilent

[Code]
function NeedsAddPath(Param: string): boolean;
var
  OrigPath: string;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', OrigPath) then
  begin
    Result := True;
    exit;
  end;
  Result := Pos(';' + Uppercase(Param) + ';', ';' + Uppercase(OrigPath) + ';') = 0;
end;
