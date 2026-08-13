; Vlocalhost.AI — Windows installer (Inno Setup 6)
;
; Packages the bundle produced by tools/build_bundle.py into one .exe that
; needs nothing else on the machine: no Python, no pip, no network.
;
; Build:
;     python tools\build_bundle.py --target windows-x64
;     iscc installer\windows\vlocalhost.iss /DAppVersion=1.1.0
;
; Two rules this file exists to enforce:
;
;   1. NO ADMINISTRATOR. PrivilegesRequired=lowest means no UAC prompt, which
;      also means it installs on a locked-down work laptop. Nothing here writes
;      outside the user's own profile unless they choose another drive.
;
;   2. UNINSTALLING NEVER DELETES NOTES. The app directory is disposable and
;      is removed; meeting notes live in %LOCALAPPDATA%\Vlocalhost and are left
;      exactly where they are. That folder is deliberately absent from
;      [UninstallDelete] — do not "tidy" it in.

#ifndef AppVersion
  #define AppVersion "1.1.0"
#endif

#define AppName      "Vlocalhost.AI"
#define AppPublisher "Vlocalhost"
#define AppURL       "https://antigravitysoham-eng.github.io/vlocalhost-ai/"
#define SupportURL   "https://antigravitysoham-eng.github.io/vlocalhost-ai/support/"
; Relative to SourceDir (the repo root), NOT to this file. Writing it as
; "..\..\build\..." instead adds 19 characters to every one of the ~30,000
; paths Inno handles, which is enough to cross Windows' 260-character limit
; and abort the compile with "The system cannot find the path specified".
#define StageDir     "build\stage-windows-x64"

[Setup]
AppId={{7F3C1A62-9B4E-4D8A-9C21-0E5A7B6D4F10}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#SupportURL}
AppUpdatesURL={#AppURL}
VersionInfoVersion={#AppVersion}

; Per-user install: no UAC, works without admin rights.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

; {autopf} resolves to %LOCALAPPDATA%\Programs for a per-user install. The
; directory page stays enabled so any drive can be chosen — D:\Apps\Vlocalhost
; is a supported answer.
DefaultDirName={autopf}\Vlocalhost
DisableDirPage=no
UsePreviousAppDir=yes
DefaultGroupName={#AppName}
AllowNoIcons=yes

ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; Every relative path below is resolved from the repo root. Keeping it short
; is what keeps the deepest site-packages file under MAX_PATH.
SourceDir=..\..
OutputDir=dist
OutputBaseFilename=vlocalhost-{#AppVersion}-windows-x64-setup
SetupIconFile=assets\vlocalhost.ico
UninstallDisplayIcon={app}\app\assets\vlocalhost.ico
UninstallDisplayName={#AppName}

; LZMA2/max on an already-compressed payload still wins ~15%, and the bundle
; is big enough that it is worth the build minutes.
Compression=lzma2/max
SolidCompression=yes

WizardStyle=modern
DisableWelcomePage=no
LicenseFile=LICENSE
DisableProgramGroupPage=yes

; No ExtraDiskSpaceRequired: Inno already totals the [Files] entries, so adding
; the unpacked size on top double-counts it. It made Add/Remove Programs report
; 840 MB for a 393 MB install and demanded that much free space before starting.

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; \
  GroupDescription: "Shortcuts:"

[Files]
; The whole bundle: the interpreter, its installed dependencies, and the app.
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; Point at pythonw.exe so no console window sits behind the app. WorkingDir is
; the app folder so relative imports resolve the way they do from source.
Name: "{group}\{#AppName}"; Filename: "{app}\runtime\pythonw.exe"; \
  Parameters: """{app}\app\vlocalhost.py"""; WorkingDir: "{app}\app"; \
  IconFilename: "{app}\app\assets\vlocalhost.ico"; \
  Comment: "Meeting notes that never leave your machine"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\runtime\pythonw.exe"; \
  Parameters: """{app}\app\vlocalhost.py"""; WorkingDir: "{app}\app"; \
  IconFilename: "{app}\app\assets\vlocalhost.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\app\vlocalhost.py"""; \
  WorkingDir: "{app}\app"; Description: "Open {#AppName} now"; \
  Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Python writes __pycache__ throughout runtime\Lib the first time the app runs
; — thousands of directories Inno never installed and therefore will not
; remove, leaving ~390 MB behind after an "successful" uninstall. Both of these
; directories are entirely ours, so removing them wholesale is safe.
;
; Deliberately NOT "{app}": the user picks that path, and it may be a folder
; they already had.
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\app"
; NOTE: %LOCALAPPDATA%\Vlocalhost is NOT listed here, on purpose. See the header.

[Code]
// The directory page accepts anything the user types, including the folder
// their notes are in (%LOCALAPPDATA%\Vlocalhost, per integrations\store.py).
// Installing there mixes program files into their data and makes an uninstall
// look like it ate their meetings. Refuse it.
function NextButtonClick(CurPageID: Integer): Boolean;
var
  DataDir, Chosen: String;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    DataDir := Lowercase(ExpandConstant('{localappdata}\Vlocalhost'));
    Chosen := Lowercase(RemoveBackslashUnlessRoot(WizardDirValue));
    if (Chosen = DataDir) or (Pos(DataDir + '\', Chosen + '\') = 1) then
    begin
      MsgBox('That folder is where Vlocalhost keeps your meeting notes.' + #13#10#13#10 +
             ExpandConstant('{localappdata}\Vlocalhost') + #13#10#13#10 +
             'Installing the program there would mix it in with your notes. ' +
             'Please choose a different folder.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

// Refuse to install over a running copy: replacing files under a live
// interpreter produces a half-updated install that fails in obscure ways.
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  Result := True;
  if Exec('cmd.exe', '/c tasklist /FI "IMAGENAME eq pythonw.exe" | find /I "pythonw.exe"',
          '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    if ResultCode = 0 then
    begin
      if MsgBox('Vlocalhost may still be running.' + #13#10#13#10 +
                'Close it first, then continue. Installing over a running ' +
                'copy can leave it half-updated.' + #13#10#13#10 +
                'Continue anyway?', mbConfirmation, MB_YESNO) = IDNO then
        Result := False;
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    MsgBox('Vlocalhost has been removed.' + #13#10#13#10 +
           'Your meeting notes and settings were kept, in:' + #13#10 +
           ExpandConstant('{localappdata}\Vlocalhost') + #13#10#13#10 +
           'Delete that folder yourself if you want them gone.',
           mbInformation, MB_OK);
end;
