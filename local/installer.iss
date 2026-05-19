; 퀀트 플랫폼 로컬앱 — Inno Setup 인스톨러 스크립트
;
; 빌드 순서:
;   1) python -m PyInstaller QuantPlatformLocal.spec --noconfirm
;   2) Inno Setup(ISCC.exe)으로 이 파일 컴파일 → Output\QuantPlatformLocal-Setup.exe
;
; 베타는 코드 미서명 — 설치 시 Windows SmartScreen 경고가 뜰 수 있다("추가 정보 → 실행").

#define AppName "퀀트 플랫폼 로컬앱"
#define AppVersion "0.1.0-beta"
#define AppExe "QuantPlatformLocal.exe"

[Setup]
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Quant Platform
DefaultDirName={autopf}\QuantPlatformLocal
DefaultGroupName=퀀트 플랫폼
OutputDir=Output
OutputBaseFilename=QuantPlatformLocal-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
WizardStyle=modern

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕화면 바로가기 생성"; Flags: unchecked
Name: "startup"; Description: "Windows 시작 시 자동 실행 (자동매매 상시 가동)"

[Files]
Source: "dist\QuantPlatformLocal\*"; DestDir: "{app}"; \
    Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\{#AppName} 제거"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; 부팅 자동시작 (사용자 단위 — 관리자 권한 불필요)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "QuantPlatformLocal"; \
    ValueData: """{app}\{#AppExe}"""; Tasks: startup; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExe}"; Description: "지금 실행"; \
    Flags: nowait postinstall skipifsilent
