## 주요 변경 — Hotfix: 자동 업데이트 race condition 영구 fix

### 🐞 v0.9.0~v0.9.2 회귀 — 업데이트가 "완료"된 듯 보이지만 옛 버전 그대로

**증상**: amber 배너 [지금 업데이트] 클릭 → 진행률 100% → 앱 종료 → 다시
열어보면 **여전히 옛 버전**.

**진범** (robocopy 로그 `%TEMP%\quantman-update.log`에서 확인):
```
2026/05/27 22:22:20 오류 32 (0x00000020) — 다른 프로세스가 파일을 사용 중
  _internal\charset_normalizer\cd.cp311-win_amd64.pyd
재시도 횟수가 초과되었습니다.
```

PyInstaller `--onedir` 빌드의 `_internal/*.pyd` 파일들이 본체 종료 직후에도
OS 잠금이 살아있거나, 사용자가 본체 종료 직후 실수로 트레이/Downloads에서
다시 클릭해 잠금을 회복. updater.bat은 **`ping -n 4`로 3초 어림짐작 sleep +
robocopy `/R:5 /W:2`로 10초 retry**라 잠금 회복하면 영구 실패. 게다가 bat이
exit code 무시하고 옛 exe를 다시 실행 → 사용자에겐 "재시작됐다"는 시각적
신호만 남고 silent fail.

### Fix — `_write_updater_bat` 강화 ([updater.py:192-263](localapp/updater.py))

4원칙 위반 3개(PR-1 fallback / PR-3 어림짐작 / PR-4 silent) 제거:

| 항목 | 이전 | v0.9.3-beta |
|---|---|---|
| 본체 종료 | `ping -n 4` (3초 sleep) | `taskkill /F /PID <parent>` 명시 |
| 잠금 해제 대기 | 없음 | exe rename 폴링 (최대 30초) |
| robocopy retry | `/R:5 /W:2` (10초/파일) | `/R:30 /W:2` (60초/파일) |
| exit code 검사 | 무시 (주석: "ignore here") | `IF GEQ 8 GOTO :FAIL` |
| 실패 시 동작 | 옛 exe 무조건 재실행 | exe 재실행 안 함 + Windows MessageBox |

### 🆕 업데이트 실패 시 사용자 안내 메시지박스

robocopy가 exit 8+ 반환하면 옛 exe를 재실행하지 않고 다음 안내를 표시:
```
Quantman 업데이트 실패. 파일이 잠겨 있어 새 버전을 적용하지 못했습니다.
MercKR/quantman-releases GitHub 페이지에서 최신 zip을 직접 다운로드해
기존 폴더에 덮어써주세요.
```

silent로 옛 버전 재실행하던 v0.9.2- 동작이 가장 큰 PR-1 (fallback 남용)
위반이었음. 이제 실패는 명시적으로 알림.

### 내부 변경

**[localapp/updater.py](localapp/updater.py)**:
- `_write_updater_bat(parent_pid)` 시그니처에 PID 추가
- bat 내용에 taskkill·lock 폴링·exit code 분기·실패 messagebox 블록 추가
- 성공/실패 모두 임시 폴더 정리 (zip 재시도는 사용자가 새로 다운로드)

**[localapp/__init__.py](localapp/__init__.py)**:
- 버전 0.9.2-beta → 0.9.3-beta

### 🍎 macOS Sequoia(15.x+) Gatekeeper friction 완화

Apple이 macOS Sequoia 15.0(2024-09)부터 **Finder 우클릭 → "열기" 우회를 제거**.
사용자가 첫 실행 시 `[휴지통으로 이동] · [완료]`만 있는 다이얼로그를 보고
막힘. v0.8.x~v0.9.2 가이드는 Sonoma 기반이라 Sequoia에서 동작 안 함
(PR-4 검증 누락).

이번 fix:
- **빌드**: GitHub Actions에 **ad-hoc 코드서명** 단계 추가
  ([.github/workflows/build-local.yml](.github/workflows/build-local.yml))
  — `codesign --force --deep --sign -`. quarantine 우회는 안 되지만 시스템
  설정 → 개인정보 보호 및 보안 흐름이 더 안정적으로 동작.
- **가이드**: [docs/MACOS_INSTALL.md](docs/MACOS_INSTALL.md) — Sonoma/Sequoia
  분기. **터미널 1줄 (`xattr -dr com.apple.quarantine`)을 1순위로 격상** —
  모든 macOS 버전에서 동작하는 유일한 단일 경로.
- **웹앱**: 다운로드 페이지(Pair·Settings)에 터미널 명령어를 copy-paste
  가능한 코드 블록으로 직접 노출
  ([web/src/components/LocalAppDownload.tsx](web/src/components/LocalAppDownload.tsx)).
  사용자가 가이드 페이지로 이동 없이 바로 실행 가능.

→ 첫 설치 macOS 사용자가 막혀도 위 한 줄로 즉시 해결.

### v0.9.2-beta 이전 사용자 — 1회 수동 설치 필요

v0.9.2-beta 이전 updater의 race condition은 이번 fix로 해결되지만, **그
fix를 받기 위해 v0.9.3-beta를 1회 수동 설치**해야 함. 그 이후 자동
업데이트는 본 v0.9.3-beta의 강화된 updater가 처리해 안정.

수동 설치:
1. Quant Platform 로컬앱 트레이에서 완전 종료
2. https://github.com/MercKR/quantman-releases/releases/tag/v0.9.3-beta
   에서 `QuantPlatformLocal-v0.9.3-beta-windows.zip` 다운로드
3. 기존 `QuantPlatformLocal-v0.9.x-beta/` 폴더 안에 압축 풀어 덮어쓰기
4. `QuantPlatformLocal.exe` 다시 실행
