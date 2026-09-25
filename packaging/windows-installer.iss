; Per-user installer for the Windows desktop build.
;
;   iscc /DProductVersion=2.2.1 /DOutDir=..\dist packaging\windows-installer.iss
;
; PrivilegesRequired=lowest is the whole point: the app writes to per-user
; folders, so it never needs an administrator, and it never installs anything
; into Program Files where the data would become read-only.

#ifndef ProductVersion
  #error ProductVersion is required: pass /DProductVersion=<version>
#endif
#ifndef OutDir
  #define OutDir "..\dist"
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
OutputDir={#OutDir}
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
Source: "..\dist\proxy-workbench-gui.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\proxy-workbench-cli.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\portable-README.txt"; DestDir: "{app}"; Flags: ignoreversion

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
