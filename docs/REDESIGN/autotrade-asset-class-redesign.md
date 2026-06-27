# 자동매매 자산군 1급화 재설계 (Asset-Class First-Class Redesign)

> **For agentic workers:** 이 문서는 *설계(spec)*다. 구현은 단계(P1~P4)별로
> `superpowers:writing-plans` → `subagent-driven-development`으로 task 단위 진행한다.
> 각 단계는 **독립 배포 가능**하고, KIS byte-identical 회귀 게이트를 통과해야 "완료".

**Goal.** 자동매매가 **모든 계좌 조합**(국내주식만 / 국내선물만 / 해외선물만 / 임의 조합)을
온보딩·실행·표시 전 계층에서 자연히 지원하고, **전략이 요구하는 자산군의 자격증명이 없을 때
자금을 안전하게 보호**(오라우팅·naked 노출·미청산 차단)하도록 워크플로를 재설계한다.

**Architecture (한 줄).** 이미 존재하는 `instrument_category(symbol)`(=`{kr_equity, kr_futures,
us_equity, us_futures}` 4분류 SSOT)를 **온보딩·브로커·사이클·웹연동 4계층의 1급 조직 축**으로
승격한다. 새 분류 체계를 만들지 않는다(추가가 아닌 정렬).

**핵심 불변식 (절대).**
- **INV-KIS:** KIS 경로(기존 도달 가능 조합 = 주식 단독 / 주식+국내선물)는 **byte-identical 무변경**.
  골든 테스트로 객체 그래프·`account_snapshot` 출력·사이징 수치를 잠근다(§4.1).
- **INV-SEC:** 자격증명·계좌번호·원시주문은 **로컬 PC 전용**. 서버/웹엔 비민감 요약·불리언만(§4.2).

---

## 0. 배경 / 트리거

LS증권 **실전 국내선물 계좌** API 키를 로컬앱에 저장하면 "저장됨"만 뜨고 다음 단계(페어링 →
자동매매 실행)로 화면이 **넘어가지 않는** 증상에서 출발. 단건 패치 대신 자동매매 워크플로 전반을
읽기 전용으로 4-facet 감사한 결과, 이 증상은 **더 깊은 구조적 비대칭의 가장 얕은 표면**이며 그
뿌리에서 **실전 자금 위험 4종이 잠복**해 있음을 확인했다. 이 문서는 그 종합과 재설계다.

---

## 1. 메타-뿌리 진단

> **"국내주식(stock equity)이 1등 시민, 나머지 자산군은 곁다리"** 라는 비대칭이 4계층에 동형 반복.

| 계층 | "주식=1등" 가정이 박힌 곳 | 증거 |
|---|---|---|
| ① 온보딩 | `ready = bool(load_ls())`/`bool(load_kis())` — **주식 슬롯만**. `active_cred_ok`·`_render_setup_area.ls_ok`·헤더·badge 전부 | `gui.py:711`, `secrets_store.py:166` |
| ② 브로커 합성 | `make_broker`가 항상 stock 먼저 생성, 없으면 `RuntimeError`. `BrokerRouter`는 `_stock` 베이스(잔고·여력·미체결 위임) | `runner.py:50·67·55·72`, `broker_router.py:145·219` |
| ③ 사이클 실행 | 자산군이 *심볼에서 사후 추론*되고 라우터 있을 때만 활성. 전략↔자격증명 **커버리지 게이트 부재** | `broker_router.py:50`, `trader.py` cycle |
| ④ 웹↔서버 | 서버가 로컬 *capability*를 **0 신호**로 알고, 활성화를 "서버 거래가능"으로만 게이트 | `strategies.py:145`, `runner.py:67` |

이 비대칭이 (a) 선물 전업 사용자를 배제하고, (b) 자산군 경계에서 silent 오라우팅·naked 노출·
미청산을 만든다. 재설계의 핵심은 **자산군을 모든 계층에서 동등 시민으로** 만드는 것.

---

## 2. 문제 인벤토리 (감사 종합)

각 문제: ID · 영향 · 근본원인 · 증거(file:line). 🔴=자금안전, 🟠=구조/확장, ⚪=검증공백.

### 🔴 자금안전 (실전 운용 직격 — 재설계의 진짜 이유)

- **[C1] silent 오라우팅.** 선물 전략을 적용했는데 선물 키가 없으면 라우터가 안 생겨 선물성이
  사라지고, `KisBroker.buy("코스피200선물")`이 `_detect_market`에서 **"DOMESTIC" 안전기본값**으로
  떨어져 **주식 계좌로 오발주 시도**. 사이징도 `futures_order_cash` 부재 → **주식 현금으로 선물
  계약수 산정**. 거부 메시지가 "종목코드 오류"로 나와 진단이 어긋남.
  *근본:* 자격증명 커버리지가 broker 구성 시점에만, 그것도 라우터가 있을 때만 반영. *부류.*
  증거: `kis_broker.py:456-476`, `runner.py:75-76`, `trader.py:1159-1165`, `broker_router.py:49-53`. (PR-1)

- **[C3] naked-leg.** 한 전략이 다자산군(주식 매수 + 선물 숏 헤지)에 주문할 때 **전략 단위
  atomicity 부재**(심볼 단위 루프). 한 leg 키만 있으면 **한 다리만 체결 → 의도 안 한 naked 노출**.
  *부류.* 증거: `trader.py:1226-1298`(진입 루프), `1751-1834`(청산 루프). (PR-1)

- **[C5] 미청산 방치.** 자격증명 상태에 따라 선물 포지션이 `account_snapshot`에 안 보이면 청산
  clamp=0 → **조용히 skip → 포지션 방치**(과거 "고아 미청산" 결함과 동형 메커니즘). 해외선물
  cancel/order_status는 라우터 미구현이라 미체결 lifecycle 비활성.
  *부류.* 증거: `trader.py:1812-1821`, `intraday_stop.py:156-162`, `broker_router.py:118-122`. (PR-1)

- **[C4] 다계좌 과대사이징.** 국내+해외선물 둘 다 구성 시 `futures_order_cash`가 **합산**돼 단일
  주문에 합산 예산 사용 → 과대사이징. 현재 해외선물 게이트로 *잠재*, 활성화 즉시 발현.
  *부류.* 증거: `broker_router.py:180-187`(코드 주석이 자백), `trader.py:1162`. (PR-3)

### 🟠 구조 · 확장성

- **[S1] stock-필수 베이스.** `make_broker`가 KIS·LS 둘 다 stock 없으면 `RuntimeError` → 선물
  단독 자동매매 불가. `BrokerRouter`가 `_stock` 베이스(account_snapshot 시작·`__getattr__`·
  pending_orders·buying_power 위임). `_stock=None`이면 사이클 골격에서 즉시 `AttributeError`.
  증거: `runner.py:67-70`, `broker_router.py:145·215-219`. (PR-2)

- **[S2] leg shape 비통일 + 정규화 라우터 전속.** stock leg는 `{balance, positions}`, 선물 leg는
  `{account, positions}`. 계약코드↔데이터셋 심볼 역매핑(`_d4c`)·`contract_expiry`가 **라우터에만**.
  → bare 선물 불가 → stock 강제(악순환). 증거: `broker_router.py:145·173-196·202`,
  `kis_futures_broker.py:505`, `ls_futures_broker.py:81`. (PR-2)

- **[S3] "ready" SSOT 분열.** "이 브로커 준비됐나?"를 `active_cred_ok()`(실행 게이트)·
  `_render_setup_area`의 인라인 `bool(load_ls())`(표시 게이트)·badge가 각자 답. 우연히 일치할 뿐
  한 곳 고쳐도 다른 곳이 안 따라옴. 증거: `secrets_store.py:161-166`, `gui.py:711·808·1007-1024`. (PR-3)

- **[S4] 저장 액션이 자격증명 영속 + 활성브로커 선언을 결합.** `_ls_save`의 선물/해외선물 분기는
  `set_active_broker("ls")`·`broker_choice` 설정을 생략(주식 분기만 함) → 라디오·keyring·표시·실행
  불일치 여지. 증거: `gui.py:1523-1530`. (PR-1)

- **[S5] 활성화 단위(user) ≠ 실행 단위(device) 입도 불일치.** 전략은 user_id 단위, 실행·자격증명·
  커버리지는 device. 한 user가 디바이스 2대(주식 PC / 선물 PC)면 양쪽 다 모든 전략을 받아 한쪽은
  자산군 미스로 실패. 증거: `sync.py:239-244`, `commands.py:84-86`. *부류.*

- **[S6] 웹에 로컬 capability 신호 0.** 서버/웹이 로컬이 어느 자산군·브로커를 등록했는지 모른 채
  활성화·표시. 활성화 게이트(`_assert_live_tradable`)가 "서버 거래가능"만 보고 "로컬 커버리지"를
  못 봄 → 선물 단독 사용자가 적용해도 통과 → 로컬에서 매 사이클 실패. 증거: `strategies.py:145-152`,
  `runner.py:67-70`. *부류.* (선물 단독 침묵 실패의 웹측 뿌리.)

- **[S7] 해외선물 lifecycle 구멍.** 발주·잔고만 라우팅, cancel=`NotImplementedError`, price/today_open=0,
  LS FX `1380.0` 하드코딩 fallback. 상용에서 CME 켜면 라이브 데뷔에서 처음 드러남.
  증거: `broker_router.py:96-122`, `ls_futures_broker.py:16·252·256`. (별도 트랙 — §7)

### ⚪ 검증 공백 (이 버그가 ship된 이유)

- **[V1] 온보딩 상태머신 테스트 0건.** `_render_setup_area`·모드 전환·`setup_collapsed` 검증 전무.
  기존 테스트는 실행계층(secrets/make_broker)만 → 표시-실행 게이트 어긋남이 사각. (PR-4)
- **[V2] LS 멱등 약화.** `reconcile_submitting`이 LS(`_daily_ccld` 미지원)에서 KR reconcile skip →
  crash-recovery가 KIS보다 약함. 증거: `intents.py:236-243`. (단건)

### 감사가 합의한 구조적 뿌리

1. **자격증명 커버리지가 1급 개념이 아니다** (C1·C3·C5·S1·S6 공통 뿌리).
2. **broker 추상화가 "stock 베이스 + 선물 애드온"** 비대칭 (S1·S2·C4·C2).
3. **발주·청산이 전략이 아니라 심볼 단위** → atomicity·격리 경계 부재 (C3·C6).
4. **온보딩 ready가 단일 슬롯에 박혀** 표시·실행으로 분기·중복 (S3·S4·V1).
5. **로컬 capability 신호 부재** → 서버/웹 침묵 실패 (S5·S6).

---

## 3. 목표 아키텍처 — `instrument_category` 1급화

```
                    instrument_category(symbol)   ← 이미 존재하는 단일 축 (exec_defaults.py)
        ┌──────────────────┬──────────────────┬──────────────────┬──────────────────┐
   ① 자격증명 슬롯맵      ② 브로커 N-leg      ③ 사이클 커버리지   ④ 웹/서버 capability
   {broker × 자산군}      멀티플렉서          게이트              신호
   ready = 등록 슬롯 집합  동등 leg            (전략요구 vs 등록)   asset_coverage 불리언
   (len≥1)               단일 leg = bare      미커버 = 전략 skip   → 웹 advisory(차단 X)
                         = KIS byte-identical  per-leg 예산 분리
```

### 3.1 자격증명 슬롯 모델 (온보딩 — 뿌리 4)

- 자격증명을 `{broker} × {asset_class}` **슬롯 맵**으로 통일하고, "ready"를 **단일 순수 함수**로 수렴:
  - `registered_slots(broker) -> set[asset_class]` — 등록된 슬롯 집합.
  - `broker_ready(broker) -> bool` = `len(registered_slots) >= 1` (**주식 필수 제거**).
  - `active_cred_ok()`를 이 정의로 일반화(기존 호출부 무변경 — 의미만 확장). KIS는 단일 브로커라
    기존과 동일하게 동작(INV-KIS).
- 표시·실행·badge·헤더가 **전부 이 함수만 소비** → S3·S4·G(헤더/badge) 동시 해소.
- **상태머신을 위젯에서 분리**한 순수 결정 함수로 추출:
  `decide_setup_mode(broker, slots: set, paired: bool, collapsed: bool) -> Mode`.
  위젯 pack은 이 결정의 렌더러일 뿐. → 조합 매트릭스 전수 테스트 가능(V1 해소).
- `_ls_save`/`_wizard_save`는 **어떤 슬롯을 저장하든** active broker 선언을 일관 보장(S4).

### 3.2 브로커 N-leg 멀티플렉서 (브로커 — 뿌리 2)

**채택 접근 = "라우터의 stock leg를 선택적(optional)으로" (additive, 낮은 위험).**
대안(모든 leg shape 통일 + 정규화 leg로 하향)은 더 우아하나 KIS stock+선물 동작 경로를 건드려
INV-KIS 위험이 크다 → **YAGNI, 후속 정제로 보류**(§7). 이번엔 기존 경로를 한 줄도 안 바꾸고
`_stock=None` 분기만 **추가**한다.

- `make_broker`: 등록된 슬롯을 스캔해 존재하는 leg만 구성.
  - 0개 → `RuntimeError`(자격증명 없음).
  - **stock 단독 → bare 브로커**(현행 그대로 = INV-KIS).
  - 선물 포함(stock 유무 무관) → `BrokerRouter(stock_or_None, futures, ...)`.
- `BrokerRouter`가 `_stock=None`을 안전 처리(전부 **추가 분기** — `_stock` 있으면 기존과 동일):
  - `account_snapshot`: `_stock is None`이면 베이스를 `{"balance": {}, "positions": []}`로 시작 후
    선물 병합(기존 병합 로직 재사용).
  - `__getattr__`: `self._stock if self._stock is not None else self._futures`로 위임.
  - `_broker(symbol)`: 비선물 심볼인데 `_stock is None` → **명확한 에러**(커버리지 게이트가 잡음).
  - `pending_orders`/`order_status(symbol=None)`: `_stock is None`이면 선물 leg로 라우팅.
- **per-leg 예산 분리(C4):** `futures_order_cash`(합산) → `futures_order_cash_kr` /
  `futures_order_cash_us`로 분리. 사이징(`trader.py`)이 주문 심볼의 `instrument_category`로 해당
  예산만 참조. *단일 시장 계좌에선 분리값 == 기존 합산값 → INV-KIS 보존.*

### 3.3 사이클 커버리지 게이트 (사이클 — 뿌리 1·3) **← P1, 최고 레버리지**

사이클 진입부에 **단일 결정적 게이트** 신설. 이 한 곳이 **C1·C3·C5를 동시에 닫는다**.

- **함수:** `required_categories(strategy) -> set[asset_class]` — 전략의 모든 진입 후보 + 보유
  포지션 심볼을 `instrument_category`로 분류(새 데이터 0 — 기존 분류기 재사용).
- **게이트:** `registered_slots(broker)`와 대조.
  - 전략의 요구 자산군 중 **하나라도 미커버 → 그 전략 통째 skip** + `skip_no_credentials` 결정
    기록 + 표면화(웹/로컬 상태). **전략 단위 atomicity = naked 차단**(C3). silent 오라우팅 제거(C1).
  - 청산 패스: ledger 포지션의 자산군이 현재 broker 가시 범위 밖이면 `skip_oversell`이 아니라
    **`orphan_uncoverable` 경고**로 분기(Monitor 표면화) — 미청산 침묵 제거(C5).
- **청산 루프 격리(C6):** 청산 종목 루프를 **per-포지션 try/except**로 감싸 한 발주 실패가 나머지
  청산을 막지 않게.

### 3.4 웹 capability 신호 + advisory (웹/서버 — 뿌리 5) **← P4 (희제 협의)**

- **로컬 보고(변경 1곳):** `analytics.local_health()`에 **비민감 불리언**만 추가:
  ```python
  health["asset_coverage"] = {
      "kr_stock":         bool(load_kis()) or bool(load_ls()),
      "kr_futures":       bool(load_kis_futures()) or bool(load_ls_futures()),
      "overseas_futures": bool(load_kis_overseas_futures()) or bool(load_ls_overseas_futures()),
  }
  ```
  `bool(load_*())` 결과만 — 키·계좌번호 절대 미포함(INV-SEC, §4.2). 추가 네트워크 0(keyring 로컬 읽기).
- **서버(변경 0~최소):** `asset_coverage`는 `SyncSnapshot.payload.health`에 실려 `GET /sync/snapshot`에
  자동 노출. 웹이 이미 `snapshot.payload.health`를 폴링 → 새 엔드포인트 불필요.
- **웹(advisory, 희제 영역):** "실전 적용" 시 `universe.symbols`를 같은 분류기로 요구 자산군 집합
  산출 → coverage 대조 → 미스면 promote 버튼 옆 **경고(차단 아님)**: "이 전략은 [국내선물]을
  요구하나 로컬앱에 미등록 — 등록 후 자동매매됩니다".
- **advisory(차단 아님)인 이유:** ① snapshot은 stale일 수 있음(적용 직후 로컬 등록 가능), ② 멀티
  디바이스(S5)에서 user 집계 coverage는 True지만 개별 디바이스는 부분, ③ 미페어링이면 coverage 부재,
  ④ **진짜 권위는 로컬 게이트(§3.3)** — 웹 차단은 가짜 권위(서버는 로컬 진실을 모름, PR-1).

---

## 4. 불변식

### 4.1 INV-KIS — byte-identical (골든 잠금)

변경 전 골든 테스트로 **기존 도달 가능 2조합의 객체 그래프·출력**을 캡처해 회귀 검출:

- **KIS 주식 단독:** `make_broker()` → `KisBroker` 인스턴스(라우터 미경유).
- **KIS 주식+국내선물:** `make_broker()` → `BrokerRouter(_stock=KisBroker, _futures=KisFuturesBroker,
  _resolve=ContractResolver.resolve, _resolve_expiry=ContractResolver.resolve_expiry,
  _d4c=dataset_for_contract[기본])`. (`dataset_for_code` 미주입 = 기본값 = INV의 일부.)
- 보존할 라우터 불변: `_futures is None` 단축(`broker_router.py:146`), `__getattr__`의 `_` 가드(:217),
  `_is_fut`의 `_futures is not None` 체크(:50).
- **per-leg 예산:** 단일 시장(kr_futures만) 계좌에서 `futures_order_cash_kr` == 기존 `futures_order_cash`
  → 사이징 수치 동일.
- 검증: 기존 골든 백테스트(`tests/golden_backtest.py`) + 신규 `make_broker`/`account_snapshot`
  객체-그래프 골든.

### 4.2 INV-SEC — 자격증명 로컬 전용

- 감사 결과 현재 push payload·서버 스키마·로그에 **자격증명 누수 0건**(확인됨): `secrets_store`는
  keyring(OS 저장소)에만 저장, 서버 모델엔 민감 필드 없음, 유일 노출은 `kis_token_expires_at`(시각).
- 신규 `asset_coverage`는 `bool(load_*())` 결과(True/False)만 — app_key/secret/account_no 값은
  `bool()`로 즉시 폐기. 자산군 종류 불리언은 §보안원칙의 보호 대상(자격증명·계좌번호·원시주문)이 아님.

---

## 5. 단계 계획 (P1~P4 · 독립 배포 가능 · 자금안전 우선)

| Phase | 목표 | 닫는 문제 | 주 변경(대략) | 의존 |
|---|---|---|---|---|
| **P1 (긴급)** | 사이클 커버리지 게이트 + 청산 가시성 분기 + 청산 루프 격리 | C1·C3·C5·C6 | `core`(분류기 재사용 헬퍼), `local/trader.py`·`runner.py` | 없음 — 라우터 구조 미변경, 즉시 |
| **P2** | 브로커 N-leg(선물 단독) + per-leg 예산 분리 | C4·S1·S2(부분) + **사장님 블로커(거래)** | `local/runner.py`·`broker_router.py`·`secrets_store.py` | INV-KIS 골든 선행 |
| **P3** | 온보딩 슬롯 모델 + 상태머신 순수함수 + 테스트 | 온보딩 stuck·S3·S4·V1 + **사장님 블로커(온보딩)** | `local/gui.py`·`secrets_store.py` | P2 |
| **P4** | 웹 advisory(asset_coverage 보고 + 웹 표시) | S6·침묵 실패 사전경고 | `local/analytics.py`·`server`(0~최소)·`web`(희제) | P1(요구자산군 도출 공유)·희제 협의 |

**각 Phase 수용 기준은 해당 implementation plan에서 task별 테스트로 구체화한다.** 공통:
- INV-KIS 골든 통과(P2 필수, 전 Phase 회귀).
- 신규 동작은 단위/시나리오 테스트로 검증(추측 완료 금지, PR-4).
- 자금 안전 경로(P1·P2)는 SimBroker/paper 시나리오 1회 이상.

### P1 상세 (긴급 — 사장님 블로커와 별개로 실전 자금 보호)
1. `core`: `required_categories(strategy)` + `instrument_category` 기반 헬퍼(전략 심볼 → 자산군 집합).
2. `local`: 커버리지 게이트를 사이클 진입부에 — 미커버 전략 skip + `skip_no_credentials` 표면화.
3. `local`: 청산 패스에서 미가시 자산군 포지션 → `orphan_uncoverable` 경고(silent skip 제거).
4. `local`: 청산 종목 루프 per-포지션 try/except 격리.
5. 테스트: (선물 전략 × 선물키 없음 → skip, 오발주 0), (헤지 전략 한 leg 미커버 → 전략 skip,
   naked 0), (미가시 선물 포지션 → orphan 경고).

### P2 상세 (선물 단독 거래 가능)
1. INV-KIS 골든 캡처(주식 / 주식+국내선물 객체 그래프·account_snapshot).
2. `BrokerRouter` `_stock=None` 분기 추가(§3.2) — `_stock` 있을 때 경로 무변경.
3. `make_broker` 선언적화 — 존재 leg 구성, stock 단독 bare 유지.
4. per-leg 예산 키 분리 + `trader` 사이징이 `instrument_category`로 해당 예산 참조.
5. 테스트: 조합 매트릭스(국내선물만/해외선물만/주식+선물 등) × make_broker 출력 + 골든 회귀.

### P3 상세 (온보딩)
1. `secrets_store`: `registered_slots`/`broker_ready` + `active_cred_ok` 일반화.
2. `gui`: `decide_setup_mode` 순수함수 추출, 표시·badge·헤더가 슬롯 집합 소비.
3. `_ls_save`/`_wizard_save` active broker 선언 일관화.
4. 테스트: `decide_setup_mode` 조합 매트릭스 전수(KIS 행 포함 = INV-KIS 회귀).

### P4 상세 (웹 advisory — 희제 협의 필수)
1. `local/analytics.local_health()`에 `asset_coverage` 추가(INV-SEC 논증 포함).
2. 서버: 변경 0 확인(snapshot.payload.health 경유) 또는 최소 노출.
3. 웹(희제): 실전 적용 시 요구자산군 대조 → advisory. **web 변경 전 희제와 협의**(담당 경계).

---

## 6. 검증 전략

- **골든(INV-KIS):** `make_broker`/`account_snapshot` 객체-그래프 골든 + 기존 `golden_backtest`.
- **커버리지 게이트:** 합성 전략 × 슬롯 조합 단위 테스트(오라우팅 0·naked 0·orphan 표면화).
- **온보딩:** `decide_setup_mode` 파라미터 매트릭스(주식만/선물만/조합 × 페어링 × collapsed).
- **자금 경로:** SimBroker/paper 시나리오(선물 단독 진입·청산 라운드트립) 1회 이상.
- **라이브(사용자측):** 모의 1회 — 선물 단독 온보딩→페어링→자동매매→종가청산 E2E.

---

## 7. 비목표 (YAGNI) / 후속 트랙

- **leg shape 완전 통일 + 정규화 leg 하향**(facet 2의 더 우아한 모델): INV-KIS 위험 커서 보류.
  P2는 "라우터 optional stock leg" additive 접근으로 충분. 필요 측정 시 후속.
- **해외선물 lifecycle 완성**(S7: cancel ORD_DT 추적·실시간 시세·FX): 모의 미지원이라 라이브 검증
  게이트가 있는 별도 트랙. 본 재설계의 N-leg 모델이 그 작업의 토대를 마련하되 범위 밖.
- **device-affinity**(S5: 전략-디바이스 매칭): 단기는 P4 advisory로 흡수, 중기 과제.
- **killswitch 전략 무차별 청산**: 계좌 단위는 업계 표준에 가까움 — 전략 단위 격리는 별도 합의.

---

## 8. 미해결 결정 / 라이브 검증 필요 (정직)

전 진단은 정적 코드 추적이다. 다음은 모의 1회로 확정 필요:
- **C1:** "DOMESTIC 오라우팅 후 KIS 거부"의 실제 거부 코드/메시지(코드 경로는 확정, 실응답 미검증).
- **C3:** server preview가 실제로 한 전략에 다자산군 후보를 담아 보내는지(preview 생성부 확인).
- **C4:** 다계좌 동시 라이브 거동(현재 게이트로 미발생).
- **P4-web:** 웹에 "실전(live)" 승격 경로의 정확한 위치(IrBuilder엔 paper만 — StrategyDetail 등 미확인).
