#define AppName "Voice2Text"
#ifndef AppVersion
  #define AppVersion "0.2.0"
#endif
#ifndef BuildDir
  #define BuildDir "..\build\voice2text"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif

[Setup]
AppId={{6F65A0AA-BF4D-4C4F-A8B0-64D6798022ED}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Andrew Somerset
AppPublisherURL=https://github.com/andrew-somerset/voice2text
AppSupportURL=https://github.com/andrew-somerset/voice2text/issues
DefaultDirName={localappdata}\Programs\Voice2Text
DefaultGroupName=Voice2Text
UninstallDisplayName=Voice2Text
UninstallDisplayIcon={app}\Voice2Text.exe
OutputDir={#OutputDir}
OutputBaseFilename=Voice2Text-Setup-{#AppVersion}
SetupIconFile=..\assets\voice2text.ico
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
DisableWelcomePage=yes
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
DisableFinishedPage=yes
AllowNoIcons=no
Compression=lzma2/max
SolidCompression=yes
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
SetupMutex=Voice2TextInstaller
VersionInfoVersion={#AppVersion}.0
VersionInfoCompany=Andrew Somerset
VersionInfoDescription=Voice2Text installer
VersionInfoProductName=Voice2Text
VersionInfoProductVersion={#AppVersion}

[Files]
Source: "{#BuildDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Voice2Text Settings"; Filename: "{app}\Voice2Text.exe"; Parameters: "--settings"; IconFilename: "{app}\Voice2Text.exe"
Name: "{group}\Start Voice2Text"; Filename: "{app}\Voice2Text.exe"; Parameters: "--start-background"; IconFilename: "{app}\Voice2Text.exe"

[Run]
Filename: "{app}\Voice2Text.exe"; Parameters: "--first-run"; Description: "Set up Voice2Text"; Flags: nowait runascurrentuser skipifsilent

[UninstallRun]
Filename: "{app}\Voice2Text.exe"; Parameters: "--uninstall-startup"; Flags: runhidden waituntilterminated; RunOnceId: "StopVoice2Text"

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssInstall then
  begin
    if FileExists(ExpandConstant('{app}\Voice2Text.exe')) then
    begin
      Exec(
        ExpandConstant('{app}\Voice2Text.exe'),
        '--stop-background',
        '',
        SW_HIDE,
        ewWaitUntilTerminated,
        ResultCode
      );
      Sleep(750);
    end;
  end;
end;
