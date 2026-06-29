# 자동매매 역량 parity — 선물 개방 + 종목-브로커 capability SSOT 설계

> 작성: 2026-06-29 · 브랜치 `feat/autotrade-capability-parity`
> 목표: **전략연구소·빌더로 도출되는 전략이 빠짐없이 실제 자동매매로 이어지게** 한다.
> 선행 감사: 본 문서 §2(KIS·LS 브로커 역량 + 라우팅·게이트 감사 3건, 2026-06-29).

---

## 1. 문제 (왜 이 작업을 하나)

자동매매 지원 여부는 **(브로커 × 모드(모의/실전) × 자산군)의 함수**인데, 현재 시스템은 세 층이 어긋나 있다:

- **빌더 노출** = 데이터 보유 종목 전부 (선물·일본·홍콩 포함)
- **게이트(G5)** = 종목 화이트리스트만 검사 — **어느 브로커·어느 모드인지 모른다**
- **실행(브로커)** = 자산군마다 지원이 갈린다 (미국주식 모의: KIS✅/LS❌, 해외선물 모의: KIS❌/LS🟡)

→ **빌더 노출 ⊋ 게이트 통과 ⊋ 실제 체결 가능** 의 3중 불일치. 일본/홍콩 주식·해외선물에서 "게이트는 통과했는데 런타임 발주 0건"인 **사일런트 실패**가 가능. 또 선물(코스피200·CME 6종)은 코드·라우팅이 완비됐는데도 단일 화이트리스트(`_LIVE_FUTURES_SYMBOLS={"코스피200선물"}`) + 휴면 플래그(`QP_FUTURES_LIVE_ENABLED` 기본 OFF)로 전부 막혀 있다.

**목표:** (1) 코스피200선물 + CME 해외선물 6종 자동매매 개방, (2) 자동매매 불가 종목은 유저에게 알리고 시스템적으로도 차단, (3) 가장 빠르게 라이브 검증 착수.

---

## 2. 감사 결과 요약 (코드 근거 — 이 설계의 사실 기반)

### 2.1 브로커 × 자산군 × 모드 자동매매 지원 현황

(✅이미 됨 / 🔧구현 필요 / 👤유저 setup / 🧪라이브 미검증 / ❌구조적 불가)

| 자산군 | KIS 모의 | KIS 실전 | LS 모의 | LS 실전 |
|---|---|---|---|---|
| **국내주식·ETF/ETN/REITs** | ✅ | ✅ 실사용 | ✅ read-only 실측 🧪주문 | ✅코드 🧪 |
| **미국주식 (NAS/NYS/AMS)** | ✅ | ✅ 실사용 | ❌ 서버 영구차단 | ✅코드 🧪 |
| **코스피200선물** | ✅코드 🧪 | ✅코드 🧪 | ✅코드 🧪 | ✅코드 🧪 |
| **CME 해외선물 6종** | ❌ 실전전용 | 👤시세신청+🔧스케일 🧪 | 🔧price·심볼·취소 배선 🧪 | 🔧 동일 🧪 |
| **일본·홍콩 주식** | ❌ 라우팅 불가 | ❌ | ❌ 라우팅 불가 | ❌ |

핵심 근거 좌표:
- **선물은 마스터 부재로 구조적 차단**: `tradable_symbols()`(server/app/symbols.py:25)는 KIS 주식 마스터 멤버십이 필수 → 선물 7종은 절대 포함 불가. 라이브 통제는 의도적으로 `_LIVE_FUTURES_SYMBOLS`(strategies.py:76) 단일 지점에 모음.
- **KIS 해외선물 모의 미지원**: kis_overseas_futures.py:3 "해외는 모의투자 미지원(실전 전용)"; kis_futures_broker.py:359 `self._ov_base=_REAL`.
- **KIS 해외선물 시세 = 무료(시세신청 필요)**: 우리 KB GOTCHAS G2가 "유료구독 필수"로 오기재. 실제 KIS 공식(해외선물옵션 수수료 안내)은 **CME 실시간 시세 무료**(KIS 대납, 거래 시 분기 자동연장). `EGW00553`은 "유료 미결제"가 아니라 "HTS [7936] 시세신청 미완료". → KB 정정 대상.
- **KIS 해외선물 시세 스케일**: kis_overseas_futures.py:181 `scale_overseas_price(raw, sCalcDesz)` 존재하나 broker_router.py:107-117이 0 반환(미배선). 정수 raw를 ffcode.mst sCalcDesz로 스케일해야 정확.
- **LS는 단일 도메인 + 키 라우팅**: `virtual` 플래그는 동작 무영향(토큰 지문에만, ls_broker.py:77). 모의/실전은 등록 appkey가 결정. → capability의 "mode"는 LS에선 계좌(키)가 결정.
- **LS 해외선물 모의 지원(KIS와 정반대)**: 코드 구현됨(ls_futures_broker.py:279-303 CIDBT00100). 단 ①CME 심볼 resolve 미확정(ls_futures_contracts.py:18 BscGdsCd↔globex root 미실측) ②`price()` 브로커 미배선 ③`overseas_cancel` 라우터 미배선.
- **LS 미국주식 모의 영구차단**: ls_broker.py:297-315 IGW40014/01900 시그니처 감지 → `_overseas_unavailable`.
- **일본/홍콩 양쪽 라우팅 불가(dead branch)**: KIS `_detect_market`(kis_broker.py:456-476) 국내·미국만; LS `_LS_EXCD`(ls_broker.py:715) 미국만.

### 2.2 검증 상태 (원칙4 — 라이브 미검증 명시)

- **실사용 검증됨**: KIS 국내주식·미국주식 모의/실전.
- **read-only 모의 실측**: LS 국내주식 잔고·시세(주문 라운드트립 미검증).
- **모의 잔고/시세 실측·주문 미검증**: KIS 코스피200선물.
- **전부 미검증(코드만)**: LS 전 자산군 주문, KIS·LS 선물 주문 라운드트립, 해외선물 전반.

---

## 3. 설계

### 3.1 핵심: capability 표를 단일 진실원천(SSOT)으로

`core/quant_core/autotrade_capability.py` (신규, 순수함수·네트워크 없음):

```python
def autotrade_capability(broker: str, mode: str, asset_class: str) -> Capability
    # broker ∈ {"kis", "ls"}
    # mode ∈ {"paper", "live"}
    # asset_class ∈ {"kr_equity", "kr_futures", "us_equity", "us_futures"}
    #   (= core instrument_category 4분류. CME 해외선물 = "us_futures")
    # returns Capability(status, verified, reason, setup_hint)
    #   status: "ok" | "needs_setup" | "blocked"
    #   verified: bool   (라이브 검증 완료 셀이면 True)
    #   reason: str      (blocked/needs_setup 사유 — 유저 메시지)
    #   setup_hint: str | None  (needs_setup일 때 행동 안내)
```

`asset_class`는 심볼에서 `quant_core.exec_defaults.instrument_category(symbol)`로 도출(이미 존재). 단 **일본/홍콩 주식은 §3.5에서 유니버스 자체에서 제외**하므로 capability엔 안 들어온다.

**상태 의미:**
- **ok**: 완전 지원. 적용 가능.
- **needs_setup**: 적용 가능하나 유저 행동이 최적/필요 (예: KIS 해외선물 실전 = HTS [7936] 시세신청). **비차단** — 시세신청 전이라도 데이터셋 전일종가 사이징 + 시장가로 발주는 동작. setup_hint를 적용기에 표면화.
- **blocked**: 하드 차단. 적용 시 422 + reason. (KIS 해외선물 모의, LS 미국 모의.)

`verified`는 셀별 손유지 상수. 사장님이 라이브 검증을 끝낸 셀을 True로 올린다(§3.4).

### 3.2 capability 표 내용 (목표)

| asset_class | kis/paper | kis/live | ls/paper | ls/live |
|---|---|---|---|---|
| **kr_equity** | ok ✓verified | ok ✓verified | ok | ok |
| **us_equity** | ok ✓verified | ok ✓verified | **blocked**(LS 모의 미제공) | ok |
| **kr_futures** | ok | ok | ok | ok |
| **us_futures** | **blocked**(KIS 모의 미지원) | needs_setup(시세신청) | ok | ok |

- `verified=True` 초기값: **kr_equity·us_equity의 KIS 모의/실전 4셀만**(실사용). 나머지는 False(미검증).
- `us_futures` ls 셀의 `ok`는 **Phase 2 배선 완료 후** 성립(그 전엔 코드상 `blocked`/배선중). capability 상수는 배선이 랜딩될 때 갱신.
- blocked 사유 문구는 reason에: 예 `"KIS 해외선물은 모의투자 미지원 — 실전 계좌를 선택하거나 LS 모의를 사용하세요"`, `"LS 미국주식은 모의 미제공 — KIS 모의를 사용하세요"`.

### 3.3 세 소비처가 한 표를 공유

**(A) 서버 게이트** — `server/app/routers/strategies.py` `_assert_live_tradable`:
- 기존 G1(레버리지)·G2(long_short)·G3(전체유니버스)·G4(비선물 숏)·G6(이벤트+스크리너)는 **유지**.
- **G5(매매불가 심볼) 교체**: `tradable_symbols()` + `_LIVE_FUTURES_SYMBOLS` + `QP_FUTURES_LIVE_ENABLED` 로직을 **capability 표 검사로 대체**.
  - 입력: `broker = strategy.account_broker`, `mode = run_mode`, `asset_class = instrument_category(sym)` (심볼별).
  - 심볼별 `cap = autotrade_capability(broker, mode, ac)`를 모은 뒤 **순서대로** 판정(직교 — 한 심볼에 둘 이상 걸릴 수 있음):
    1. **차단**: 어떤 심볼이라도 `cap.status=="blocked"` → 422 + 그 심볼의 reason (어느 심볼이 왜 막혔는지 명시). 최우선.
    2. **미검증 실전 경고-확인**: `mode=="live"` 이고 차단 안 된 심볼 중 `cap.verified==False`가 하나라도 있으면 → 본문에 `ack_unverified=true` 없을 시 422 + "미검증 실전 경로(해당 셀 나열) — 모의 검증 권장. 확인 후 재요청". (status가 `ok`든 `needs_setup`이든 무관 — verified만 본다.)
    3. **setup 안내(비차단)**: `cap.status=="needs_setup"`인 심볼이 있으면 setup_hint들을 응답에 동봉(웹이 안내). 통과를 막지 않음.
    4. 그 외 통과.
- **미바인딩(account_ref=NULL) 처리**: 적용(paper/live)은 §3.6에서 계좌 선택을 강제하므로 정상 경로에선 broker가 항상 있다. 레거시 미바인딩 paper/live 전략(전부 주식)은 `broker` 불명 → **보수적 broker-agnostic**: kr_equity/us_equity는 KIS 기본 지원이라 통과, 그 외(선물 등)는 "계좌를 바인딩하세요" 422. (선물은 과거 게이트가 막아 레거시로 존재 불가 → 안전.)

**(B) 웹 라벨·적용기** — `web/src/`:
- **빌더 정보 라벨**(`MultiSymbolPicker`): 하드코딩 "백테스트 전용"(asset_class==futures) 제거. capability 표의 **브로커-불문 판정**("어떤 브로커로도 자동매매 ok인가")으로 "자동매매 가능 / 시세신청 필요 / 백테스트 전용" 동적 뱃지. (정보용 — 하드 차단 아님.)
- **적용기 = 확장된 AccountPicker**(P5-4): 계좌(=브로커) 선택 시 이 전략의 자산군들을 capability로 검사 → **지원하는 계좌만 활성화**, 나머지는 reason과 함께 비활성. 선택 후 "모의/실전 적용" → 서버 게이트 재검증. needs_setup(시세신청)·미검증 실전 경고를 모달에 표시.

**(C) 로컬 런타임 가드** — `local/localapp/trader.py`:
- P5-3의 `active_account_ids()` 멤버십 검사에 더해, 발주 직전 `autotrade_capability(active_broker, mode, instrument_category(sym))`가 `blocked`면 **skip + 사유를 cycle 결과에 기록**(사일런트 실패 방지·방어선). 게이트를 우회한 경로(레거시·직접 API)도 여기서 차단.

### 3.4 검증 게이트 (원칙4 조율)

- **모의(paper)**: capability 표대로 전면 개방(모의 돈·저위험·검증 단계).
- **실전(live)**: `verified=True` 셀은 무경고. `verified=False` 셀은 **경고-확인**(게이트가 `ack_unverified` 요구). 사장님이 모의 검증 완료 후 해당 셀 `verified=True`로 승격(상수 1줄 변경) → 그 셀 실전 무경고화.
- 이 방식이 "미검증 라이브 무단 개방 방지"(원칙4)와 "사장님 자율 진행"을 동시 만족.

### 3.5 일본/홍콩 제외 (백테스트·자동매매 모두)

지원 유니버스를 **국내/미국 주식 + 코스피200선물 + CME 해외선물 6종**으로 확정.
- 서버 카탈로그(`server/app/routers/backtest.py`): 마스터-only 종목 제외 분기에 TSE/HKS 추가(현재 NAS/NYS/AMS만 제외). → 일본/홍콩 빌더 미노출.
- 웹(`MultiSymbolPicker`): `TRADABLE_TAB_ORDER`에서 "일본","홍콩" 제거.
- 선물 분석 대시보드(`/futures`, futures_config.py)는 별개 — 영향 없음.

### 3.6 데이터 모델 변경

- `Strategy.account_broker: Optional[str]` 신규 컬럼(VARCHAR, NULL=미바인딩). `account_ref` 추가와 동일한 부트 마이그레이션(`server/app/db.py` `_NEW_COLS`). **비민감**(브로커명 "kis"/"ls"만 — INV-SEC 무관, 계좌번호·키 아님).
- 채워지는 시점: **자동매매 적용(계좌 선택)** 시 웹이 `account_ref`와 함께 `account_broker` 전송. 저장(draft)엔 불필요.

### 3.7 해외선물 배선 (Phase 2 — 🔧 개발)

- **LS 해외선물**: ① `LsFuturesBroker`에 `price()`/`today_open()` 추가(o3105 TrdP/OpenP) ② CME 심볼 resolve 확정(`ls_futures_contracts.py` BscGdsCd ↔ `OVERSEAS_ROOTS` globex root 매핑·검증) ③ `overseas_cancel`을 broker_router 핫패스에 연결.
- **KIS 해외선물**: ① `broker_router.py:107-117`의 0 반환을 `scale_overseas_price(raw, sCalcDesz)` 적용 실시세로 교체(시세신청 계좌 한정; 미신청이면 데이터셋 전일종가 fallback 유지) ② `EGW00553` 감지 → setup_hint("HTS [7936] CME 시세신청") ③ `cancel` ORGN_ORD_DT 추적 배선.
- **공통**: 시세 0/실패 시 데이터셋(yfinance) 전일종가 fallback은 **유지** — 사이징은 항상 동작(시장가 발주 전제).

---

## 4. 단계화 (라이브 검증 최단 착수)

| Phase | 내용 | 산출 | 검증 |
|---|---|---|---|
| **1** | capability SSOT 신설 + 게이트 표기반 전환(화이트리스트·플래그 철거) + `account_broker` 컬럼 + AccountPicker 브로커필터 + 빌더 라벨 + JP/HK 제외 | **코스피200선물 모의 자동매매 KIS·LS 즉시 적용 가능** | 🧪 사장님 모의 라운드트립 |
| **2** | LS 해외선물 배선(price·심볼·취소) + KIS 해외선물 sCalcDesz·시세신청 안내 | 해외선물 모의 적용 가능 | 🧪 LS 모의 → KIS 실전(시세신청 후) |
| **3** | 실전 미검증경로 경고-확인 UX + 로컬 런타임 capability 가드 + `verified` 승격 운영 | parity 방어선 완성 | 단위/골든 |

**검증 분업:** 제가 코드+단위/골든 테스트+셀별 검증 런북. 사장님이 실계좌·장 시간으로 런북 실행(자격증명·실주문은 로컬 전용·제가 직접 불가).
**검증 우선순위(런북 순서):** ① 코스피200선물 모의(KIS→LS) ② 코스피200선물 실전 ③ LS 국내주식 모의 주문 ④ LS 해외선물 모의 ⑤ KIS 해외선물 실전(시세신청 후).
**검증 중 안전망(기존):** C7 계좌 가드(P5-3)·preview·킬스위치·투입 투명성(P6).

---

## 5. 범위 밖 / 비목표

- **일본·홍콩 주식**: 백테스트·자동매매 모두 제외(§3.5).
- **옵션**: 별도 보류(기존 결정).
- **레버리지(>1배) 전략**: G1 유지(현금계좌 불가) — 변경 없음.
- **long_short 횡단·비선물 숏·이벤트+스크리너**: G2/G4/G6 유지.
- **실시간 시세 구독 자동화**: KIS HTS [7936] 시세신청은 유저 수동(앱이 대행 불가) — 안내만.

## 6. 보안 불변식 (위반 금지)

- `account_broker`는 비민감(브로커명만). **계좌번호·appkey·secret·원시주문은 서버에 일절 미전송**(기존 INV-SEC·CLAUDE.md §4 유지).
- 게이트·capability는 **순수 판정** — 자격증명 접근 없음.

## 7. 리스크 / 미해결

- **LS CME 심볼 resolve 미확정**: BscGdsCd↔globex root 불일치 시 resolve→None→발주 skip(안전하나 거래 0). Phase 2에서 LS 실측/문서 대조 필요. 비트코인선물(BTC) 매핑 특히 확인.
- **전 셀 라이브 미검증**(주식 KIS 외): capability `ok`는 코드 지원이지 검증 아님 — `verified` 플래그로 분리 관리. "완료" 선언은 사장님 검증 후.
- **KIS 해외선물 응답 키 미확정**(ODNO 등): 모의 미지원이라 첫 실거래(시세신청+실전) 캡처로 확정.
- **capability 상수 드리프트**: 브로커 코드가 바뀌면 표도 갱신해야 함 — 표를 SSOT로 두되 단위테스트로 "코드 가드와 표 일치" 회귀.
