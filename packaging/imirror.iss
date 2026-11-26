; iMirror installer (Inno Setup 6). Built by .github/workflows/build.yml:
;   iscc /DAppVersion=2.0.0 packaging\imirror.iss
#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif

[Setup]
AppId={{6F3B8E2A-9C4D-4E71-B2A5-1D7C3F9E0A41}
AppName=iMirror
AppVersion={#AppVersion}
AppVerName=iMirror {#AppVersion}
AppPublisher=Ernomoon
AppPublisherURL=https://github.com/Ernomoon/iMirror
AppSupportURL=https://github.com/Ernomoon/iMirror/issues
DefaultDirName={autopf}\iMirror
DefaultGroupName=iMirror
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=iMirror-Setup-{#AppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\iMirror.exe
Compression=lzma2/ultra64
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
PrivilegesRequired=admin
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Ярлык на рабочем столе"; GroupDescription: "Дополнительно:"

[Files]
Source: "..\dist\iMirror\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\dist-receiver\*"; DestDir: "{app}\receiver"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\firmware_bin\*.bin"; DestDir: "{app}\firmware_bin"; Flags: ignoreversion

[Icons]
Name: "{group}\iMirror"; Filename: "{app}\iMirror.exe"
Name: "{group}\Удалить iMirror"; Filename: "{uninstallexe}"
Name: "{autodesktop}\iMirror"; Filename: "{app}\iMirror.exe"; Tasks: desktopicon

[Run]
; Firewall: the receiver accepts the iPhone's connection, iMirror answers Bonjour (UDP 5353).
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""iMirror"""; Flags: runhidden waituntilterminated
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""iMirror AirPlay"""; Flags: runhidden waituntilterminated
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""iMirror"" dir=in action=allow program=""{app}\iMirror.exe"" enable=yes profile=any"; Flags: runhidden waituntilterminated
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""iMirror AirPlay"" dir=in action=allow program=""{app}\receiver\bin\uxplay.exe"" enable=yes profile=any"; Flags: runhidden waituntilterminated
Filename: "{app}\iMirror.exe"; Description: "Запустить iMirror"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""iMirror"""; Flags: runhidden; RunOnceId: "fw1"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""iMirror AirPlay"""; Flags: runhidden; RunOnceId: "fw2"

[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\iMirror"

[Code]
const
  WebView2Key = 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WebView2UserKey = 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function WebView2Installed: Boolean;
var v: String;
begin
  Result := (RegQueryStringValue(HKLM, WebView2Key, 'pv', v) and (v <> '') and (v <> '0.0.0.0')) or
            (RegQueryStringValue(HKCU, WebView2UserKey, 'pv', v) and (v <> '') and (v <> '0.0.0.0'));
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  code: Integer;
begin
  if (CurStep = ssPostInstall) and not WebView2Installed then
  begin
    WizardForm.StatusLabel.Caption := 'Установка Microsoft Edge WebView2 (нужен для окна iMirror)...';
    try
      DownloadTemporaryFile('https://go.microsoft.com/fwlink/p/?LinkId=2124703', 'MicrosoftEdgeWebview2Setup.exe', '', nil);
      Exec(ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe'), '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, code);
    except
      MsgBox('Не удалось скачать Microsoft Edge WebView2. Установите его с сайта Microsoft, иначе окно iMirror не откроется.', mbError, MB_OK);
    end;
  end;
end;
