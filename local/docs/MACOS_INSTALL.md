# macOS 설치 가이드 (Apple Silicon)

> **대상**: M1·M2·M3·M4 Mac (Apple Silicon). Intel Mac은 지원하지 않습니다.
> **macOS 요구사항**: Sonoma 14.0 이상 (Sequoia 15.0+ 권장).

## ⚠️ 미서명 앱 — 첫 실행만 별도 절차

이 앱은 **코드 서명되어 있지 않습니다** (Apple Developer Program 미가입).
첫 실행 시 macOS Gatekeeper가 거부 경고를 띄웁니다 — 사용자가 본 화면은
macOS 버전에 따라 다릅니다:

| macOS 버전 | 화면 | 진행 방법 |
|---|---|---|
| **Sonoma (14.x)** | `[취소]` + `[열기]` 버튼 | 방법 1(우클릭→열기) 사용 가능 |
| **Sequoia (15.x+)** | `[휴지통으로 이동]` + `[완료]`만 | **방법 1 불가** → 방법 2 또는 방법 3 사용 |

**가장 확실한 단일 경로는 방법 2 (터미널 명령 1줄)** — 모든 macOS 버전에서
동작합니다.

---

## 방법 2 — Terminal 한 줄 (모든 macOS 버전 권장)

1. [최신 release](https://github.com/MercKR/quantman-releases/releases/latest)에서
   `QuantPlatformLocal-vX.Y.Z-macos-arm64.zip` 다운로드.
2. zip 더블클릭 → `QuantPlatformLocal-vX.Y.Z.app` 생성.
3. `.app`을 `/Applications`로 드래그(권장) 또는 그대로 사용.
4. **Spotlight(`Cmd+Space`) → "터미널"** 검색해 열기.
5. 아래 명령 붙여넣고 Enter (경로는 실제 위치로 치환):

```bash
xattr -dr com.apple.quarantine /Applications/QuantPlatformLocal-vX.Y.Z.app
```

   `~/Downloads`에 있으면:
```bash
xattr -dr com.apple.quarantine ~/Downloads/QuantPlatformLocal-vX.Y.Z.app
```

6. 이후 더블클릭으로 그냥 열립니다. (quarantine 속성 제거 → Gatekeeper가
   "인터넷에서 받은 앱"으로 인식하지 않게 됩니다.)

## 방법 3 — 시스템 설정 GUI (Sequoia 15.x+)

Sequoia부터 우클릭→열기 우회가 막혔습니다. GUI로만 처리하려면:

1. .app 더블클릭 → 거부 다이얼로그 뜨면 **[완료]** 클릭.
2. **시스템 설정 → 개인정보 보호 및 보안** 열기.
3. 페이지 아래로 스크롤 → "보안" 섹션에서 다음 문구 찾기:
   > "'QuantPlatformLocal-vX.Y.Z'은(는) 확인된 개발자가 배포한 것이
   > 아니므로 차단되었습니다"
4. 오른쪽 **[그래도 열기]** (또는 영어 "Open Anyway") 버튼 클릭.
5. Touch ID 또는 사용자 암호 인증.
6. 확인 다이얼로그에서 **[열기]** 클릭.
7. 이후 더블클릭으로 그냥 열립니다.

## 방법 1 — Finder 우클릭 (Sonoma 14.x 한정)

⚠️ Sequoia 15.x+에서는 **이 방법이 동작하지 않습니다** (Apple이 우회 경로
제거). Sonoma 사용자만 사용 가능:

1. zip 다운로드·압축 해제·`/Applications` 이동.
2. **Finder에서** `.app`에 **마우스 우클릭** → **[열기]**.
3. 경고 dialog의 **[열기]** 버튼 클릭.
4. 이후 더블클릭 가능.

---

## 🔑 KIS Keychain 권한 — "항상 허용" 필수

앱 첫 실행 후 KIS 자격증명을 wizard에서 저장·로드할 때 macOS Keychain prompt가
표시됩니다:

> "QuantPlatformLocal에서 키체인 항목 'kis_credentials'에 접근하려고 합니다"

**반드시 [항상 허용]을 클릭하세요.**

[허용] (한 번만)을 누르면 다음 자동매매 cycle에서 또 prompt가 뜨는데, **새벽
8:55 KOR 자동 진입 cycle은 사용자가 자고 있는 동안 동작**하므로 prompt에 응답
못 해 매매 실패로 이어집니다.

만약 실수로 [허용]만 눌렀다면 키체인 접근.app(Keychain Access)에서:
1. 검색창에 `quant-platform-local` 입력
2. 항목 더블클릭 → [접근 제어] 탭 → [이 항목을 사용할 때 인증되지 않은 모든 응용 프로그램 허용] 체크 (또는 QuantPlatformLocal을 [항상 허용 목록]에 추가).

---

## 자동 업데이트

앱이 부팅·focus 시 GitHub release를 polling해 새 버전 안내 배너를 띄웁니다.
[지금 업데이트] 클릭 시:
1. 새 .app zip 자동 다운로드.
2. 앱 종료.
3. 백그라운드 shell script가 기존 .app을 새 .app으로 교체.
4. `xattr -dr com.apple.quarantine` 자동 실행 (Gatekeeper 재경고 방지).
5. 새 .app 자동 실행.

**업데이트 후에도 정상 실행되지 않으면** 위 "방법 2" 명령을 한 번 더 실행하세요.

---

## 알려진 제약

- **메뉴바 트레이 아이콘**: pystray의 macOS backend 제약으로 dark menubar에서 약간
  흐릿하게 보일 수 있습니다. 클릭은 정상 동작.
- **창 닫기 동작**: Windows와 동일 — 빨간 버튼으로 닫아도 백그라운드에서 스케줄러가
  계속 동작. 완전 종료는 메뉴바 트레이 → [종료] 또는 Dock 우클릭 → [종료].

## 문제 보고

[GitHub Issues](https://github.com/MercKR/quantman-releases/issues)에 macOS
환경 정보 (모델·OS 버전·앱 버전)와 함께 알려주세요.
