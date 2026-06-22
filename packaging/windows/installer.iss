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
SetupIconFile=app.ico
UninstallDisplayIcon={app}\app.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
; Warn up front that the final step downloads components and may look stuck.
WelcomeLabel2=This will install %1 on your computer.%n%nThe final step downloads the app from the internet, so it needs a connection and can take a few minutes - the progress bar may appear to pause near the end. Please be patient and do not close the window.

[Tasks]
Name: "ai"; Description: "Install AI Cleanup (adds the local/cloud AI features; larger download)"; GroupDescription: "Optional components:"
Name: "addtopath"; Description: "Add the imap-cleanup-tool command to PATH"; GroupDescription: "Optional components:"
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
; The bundled, relocatable Python (built by build.ps1 into .\python).
Source: "python\*"; DestDir: "{app}\python"; Flags: recursesubdirs createallsubdirs ignoreversion
; The launcher + the pinned-version constraints used by the pip step.
Source: "launcher.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "launcher.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "app.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\constraints.txt"; DestDir: "{app}"; Flags: ignoreversion

[Run]
; --- pip install at install time (online) ---
; Web-only when the AI task is NOT selected...
Filename: "{app}\python\python.exe"; \
  Parameters: "-m pip install --no-warn-script-location ""imap-cleanup-tool[web]"" -c ""{app}\constraints.txt"""; \
  StatusMsg: "Downloading and installing components (web UI) - this can take a few minutes, please wait..."; \
  Flags: runhidden; Check: not WizardIsTaskSelected('ai')
; ...web + AI when the AI task IS selected.
Filename: "{app}\python\python.exe"; \
  Parameters: "-m pip install --no-warn-script-location ""imap-cleanup-tool[web,ai]"" -c ""{app}\constraints.txt"""; \
  StatusMsg: "Downloading and installing components (web UI + AI Cleanup) - this can take several minutes, please wait..."; \
  Flags: runhidden; Check: WizardIsTaskSelected('ai')
; Offer to launch at the end.
Filename: "{app}\launcher.exe"; \
  Description: "Launch {#MyAppName}"; Flags: postinstall nowait skipifsilent

[Icons]
; Start-menu + (optional) desktop shortcut -> the branded launcher.exe (opens the
; web UI). launcher.exe carries the app icon, so MSIX tiles are generated from it.
Name: "{group}\IMAP Cleanup Tool"; Filename: "{app}\launcher.exe"; \
  WorkingDir: "{app}"; IconFilename: "{app}\app.ico"
Name: "{group}\Uninstall IMAP Cleanup Tool"; Filename: "{uninstallexe}"
Name: "{autodesktop}\IMAP Cleanup Tool"; Filename: "{app}\launcher.exe"; \
  WorkingDir: "{app}"; IconFilename: "{app}\app.ico"; Tasks: desktopicon

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

// Remove the {app}\python\Scripts entry from the user PATH on uninstall, so no
// stale (dangling) path is left behind once the files are gone.
procedure RemoveFromPath();
var
  Scripts, Cur: string;
begin
  Scripts := ExpandConstant('{app}\python\Scripts');
  if RegQueryStringValue(HKCU, EnvKey, 'Path', Cur) then
  begin
    StringChangeEx(Cur, ';' + Scripts, '', True);
    StringChangeEx(Cur, Scripts + ';', '', True);
    StringChangeEx(Cur, Scripts, '', True);
    RegWriteStringValue(HKCU, EnvKey, 'Path', Cur);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    RemoveFromPath();
end;

[UninstallDelete]
; Remove anything pip wrote into the bundled Python (site-packages, scripts).
Type: filesandordirs; Name: "{app}\python"
