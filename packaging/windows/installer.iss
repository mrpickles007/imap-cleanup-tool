; Inno Setup script for the IMAP Cleanup Tool Windows installer.
; ---------------------------------------------------------------------------
; This is an ONLINE installer: it bundles a private, relocatable Python (so it
; never touches the user's system Python) and, at install time, pip-installs the
; app with the dependency versions pinned in ..\constraints.txt. The AI Cleanup
; component is an optional task (off by default) because litellm is heavy.
;
; BEFORE COMPILING (see build.ps1, which automates this):
;   1. Put a relocatable python-build-standalone "install_only" build in
;      packaging\windows\python\  (so packaging\windows\python\python.exe exists).
;   2. Have Inno Setup installed (ISCC.exe on PATH).
;   3. Compile, passing the version:  ISCC /DMyAppVersion=0.36.8 installer.iss
;
; Internet is required during installation (the pip step downloads the app and
; its dependencies from PyPI).
; ---------------------------------------------------------------------------

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#define MyAppName "IMAP Cleanup Tool"
#define MyAppPublisher "Giulio Alberello"
#define MyAppURL "https://imapcleanuptool.com"

[Setup]
AppId={{8F3B2A1C-7E54-4D9A-9C2B-IMAPCLEANUP01}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={autopf}\IMAP Cleanup Tool
DefaultGroupName=IMAP Cleanup Tool
DisableProgramGroupPage=yes
; Per-machine install needs admin; use lowest + autopf for per-user if preferred.
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=dist
; Stable (unversioned) name so the website can link to the always-latest asset:
;   .../releases/latest/download/imap-cleanup-tool-windows-setup.exe
OutputBaseFilename=imap-cleanup-tool-windows-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; SetupIconFile=app.ico        ; (optional) add packaging\windows\app.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "ai"; Description: "Install AI Cleanup (adds the local/cloud AI features; larger download)"; GroupDescription: "Optional components:"
Name: "addtopath"; Description: "Add the imap-cleanup-tool command to PATH"; GroupDescription: "Optional components:"
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
; The bundled, relocatable Python (built by build.ps1 into .\python).
Source: "python\*"; DestDir: "{app}\python"; Flags: recursesubdirs createallsubdirs ignoreversion
; The launcher + the pinned-version constraints used by the pip step.
Source: "launcher.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\constraints.txt"; DestDir: "{app}"; Flags: ignoreversion

[Run]
; --- pip install at install time (online) ---
; Web-only when the AI task is NOT selected...
Filename: "{app}\python\python.exe"; \
  Parameters: "-m pip install --no-warn-script-location ""imap-cleanup-tool[web]"" -c ""{app}\constraints.txt"""; \
  StatusMsg: "Installing IMAP Cleanup Tool (web UI)..."; \
  Flags: runhidden; Check: not WizardIsTaskSelected('ai')
; ...web + AI when the AI task IS selected.
Filename: "{app}\python\python.exe"; \
  Parameters: "-m pip install --no-warn-script-location ""imap-cleanup-tool[web,ai]"" -c ""{app}\constraints.txt"""; \
  StatusMsg: "Installing IMAP Cleanup Tool (web UI + AI Cleanup)..."; \
  Flags: runhidden; Check: WizardIsTaskSelected('ai')
; Offer to launch at the end.
Filename: "{app}\python\python.exe"; Parameters: """{app}\launcher.py"""; \
  Description: "Launch {#MyAppName}"; Flags: postinstall nowait skipifsilent

[Icons]
; Start-menu + (optional) desktop shortcut -> the launcher (opens the web UI).
Name: "{group}\IMAP Cleanup Tool"; Filename: "{app}\python\python.exe"; \
  Parameters: """{app}\launcher.py"""; WorkingDir: "{app}"
Name: "{group}\Uninstall IMAP Cleanup Tool"; Filename: "{uninstallexe}"
Name: "{autodesktop}\IMAP Cleanup Tool"; Filename: "{app}\python\python.exe"; \
  Parameters: """{app}\launcher.py"""; WorkingDir: "{app}"; Tasks: desktopicon

[Code]
// Add {app}\python\Scripts to the user PATH when the addtopath task is selected.
const
  EnvKey = 'Environment';

procedure AddToPath();
var
  Scripts, Cur: string;
begin
  Scripts := ExpandConstant('{app}\python\Scripts');
  if not RegQueryStringValue(HKCU, EnvKey, 'Path', Cur) then
    Cur := '';
  if Pos(LowerCase(Scripts), LowerCase(Cur)) = 0 then
  begin
    if (Cur <> '') and (Cur[Length(Cur)] <> ';') then
      Cur := Cur + ';';
    RegWriteStringValue(HKCU, EnvKey, 'Path', Cur + Scripts);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('addtopath') then
    AddToPath();
end;

[UninstallDelete]
; Remove anything pip wrote into the bundled Python (site-packages, scripts).
Type: filesandordirs; Name: "{app}\python"
