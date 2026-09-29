; Meridian 윈도우 설치 프로그램 (Inno Setup 6)
;
; 화면(WinUI)과 엔진을 담은 publish 폴더를 통째로 설치한다. 부품 파일이 수백 개라
; zip 으로 주면 받는 사람이 무엇을 눌러야 할지 모른다. 설치하면 시작 메뉴와
; 바탕화면에 아이콘 하나만 보인다.
;
; 관리자 권한을 묻지 않도록 사용자 폴더(%LOCALAPPDATA%\Programs)에 설치한다.
; 캐시와 프로젝트는 엔진이 %LOCALAPPDATA%\Meridian 에 따로 두므로 설치 위치와 무관하다.
;
;   iscc /DAppVersion=1.0.0 /DSourceDir=..\publish windows\meridian.iss
;   결과: dist\Meridian-Setup.exe

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\publish"
#endif

[Setup]
; AppId 는 바꾸면 안 된다. 새 버전이 옛 버전을 같은 앱으로 알아보고 덮어 설치하는 기준이다.
AppId={{06CE5A8E-65FF-49E1-BE14-1DF2D39C4FFF}
AppName=Meridian
AppVersion={#AppVersion}
AppVerName=Meridian {#AppVersion}
AppPublisher=doozymalen
AppPublisherURL=https://github.com/doozymalen/meridian
AppSupportURL=https://github.com/doozymalen/meridian/issues
DefaultDirName={autopf}\Meridian
DefaultGroupName=Meridian
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir=..\dist
OutputBaseFilename=Meridian-Setup
SetupIconFile=..\icon\meridian.ico
UninstallDisplayIcon={app}\Meridian.exe
UninstallDisplayName=Meridian
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; 켜져 있는 Meridian 은 설치 전에 닫는다 (엔진도 화면이 닫히면 따라 끝난다)
CloseApplications=yes

[Languages]
; 한국어 번역 파일은 Inno Setup 버전에 따라 없을 수 있다. 있으면 쓰고 없으면 영어만.
#if FileExists(AddBackslash(CompilerPath) + "Languages\Korean.isl")
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
#endif
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Meridian"; Filename: "{app}\Meridian.exe"
Name: "{autodesktop}\Meridian"; Filename: "{app}\Meridian.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Meridian.exe"; Description: "{cm:LaunchProgram,Meridian}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 사진 축소본과 내보내기 임시 파일. 수십 GB 가 될 수 있다.
; 사용자가 저장한 프로젝트(%LOCALAPPDATA%\Meridian\projects)는 지우지 않는다.
Type: filesandordirs; Name: "{localappdata}\Meridian\cache"
