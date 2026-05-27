## 주요 변경 — KIS 자격증명 onboarding wizard

### 🆕 ① KIS 자격증명 3-step wizard

기존 평평한 입력 form (App Key / Secret / 계좌번호 + [저장]) → **3-step wizard**.

**Step 1 — KIS Open API 준비** (3개 sub-card 순차 분기)
- 1-1: 한국투자증권 계좌 — 없으면 [🌐 비대면 계좌개설] (truefriend.com 직접 링크)
- 1-2: KIS Open API 신청 — [🌐 KIS Open API 신청] (apiportal.koreainvestment.com/intro)
- 1-3: 모의투자 가상계좌 — [🌐 모의투자 신청]

각 sub-card는 "있나요?" 분기 → ✓ 있어요 클릭 시 다음 카드. 한꺼번에 모든 안내
던지지 않고 단계별로 진행. 등록된 사용자에게는 sub-card가 ✓ chip 압축 표시.

**Step 2 — 거래 모드 선택** (카드 형태, hover·click 시각 반응)
- 🧪 모의투자 (권장 chip, 가상 자금)
- 🔥 실전투자 (주의 chip, 실거래 — confirm dialog 추가)
- 카드 hover → ACCENT 테두리 + 옅은 톤
- 선택된 카드 → ACCENT_SOFT 배경 + ACCENT 테두리 2px
- 카드 본문 어디든 클릭 가능 (라디오 정확히 안 눌러도 됨)

**Step 3 — 자격증명 입력 + 단일 액션 버튼**
- App Key·Secret·계좌번호 입력 (붙여넣기 자동 정화 — `\n`·공백 trim)
- nav 우측 한 버튼: 처음 **[🔌 연결 테스트]** → 성공 시 같은 자리가 **[💾 저장]**으로 변신
- 입력 변경 시 다시 [🔌 연결 테스트]로 reset (안전 가드)
- 연결 테스트 = `/oauth2/tokenP` + `inquire-balance` → ✓ "연결 성공 · 예수금 X원 · 평가금액 Y원" / ❌ KIS `rt_cd`·`msg1` 그대로

### 🆕 한 번에 하나씩 — 순차 onboarding 흐름

기존: ⚙ 변경 모드 진입 시 ①+② 동시 노출 + ③·Notebook까지 한꺼번에 보임 → 부담.

변경: wizard 모드 = **현재 진행 단계만** 표시.

| 상태 | 노출되는 영역 |
|---|---|
| 정상 (둘 다 등록) | 압축 bar + ③ 자동매매 + Notebook + 새로고침 |
| ⚙ 자격증명 변경 모드 | ① wizard만 (②·③·Notebook 숨김) |
| 신규 사용자 / ① 저장 후 페어링 대기 | ② 페어링만 (①·③·Notebook 숨김) |
| 페어링 완료 | 자동으로 정상 모드 복귀 |

신규 사용자 흐름: `wizard_kis (① wizard)` → 저장 → `setup_collapsed=True` 자동 →
`wizard_pair (② 페어링만)` → 완료 → `normal (정상)`. 한 번에 하나씩.

기존 사용자가 ⚙ 변경 → Step 3 직행. 저장하면 정상 모드 복귀.

### 🆕 보조 헬퍼: [localapp/kis_health.py](localapp/kis_health.py)

`test_credentials(app_key, app_secret, account_no, virtual)` — 저장 *전*에
입력값으로 직접 KIS 토큰 발급 + 잔고 조회. `KisBroker`는 `secrets_store`에서
저장된 자격증명을 읽으므로 wizard용 별도 헬퍼 필요.

검증 깊이: 토큰 + 국내 잔고 조회까지 (= app_key·secret + 계좌번호 모두 검증).
해외 잔고·시세는 제외 (모의계좌 false negative 위험).

### v0.8.10 사용자 자동 업데이트

v0.8.10의 focus event update check가 v0.8.11 publish 직후 감지 → amber 배너 →
[지금 업데이트] 클릭으로 자동 적용. 업데이트 후 hero `v0.8.11-beta` 확인.
