# 계좌-전략 연동 + 자금 투입 투명성 설계 (Account-Linked Strategy & Fund Transparency)

> **For agentic workers:** 이 문서는 *설계(spec)*다. 구현은 단계(P5.0~P7)별로
> `superpowers:writing-plans` → `subagent-driven-development`으로 task 단위 진행한다.
> 이 트랙은 [autotrade-asset-class-redesign.md](../../REDESIGN/autotrade-asset-class-redesign.md)의
> **C7(모의/실전 디커플링)·V3(계좌번호 미검증)** finding을 근본 해결하는 후속 트랙이다.

**Goal.** 전략을 *어느 계좌로 실행할지*를 명시적으로 묶고(=모의/실전이 계좌의 검증된 속성이 되도록),
자동매매가 *얼마를·무엇을·몇 개·몇 배 레버리지로·수수료 얼마에* 투입하는지 사전·사후로 **정확하고
투명하게** 보여준다. 계좌번호는 등록 시점에 **API로 검증**해 잘못된 계좌가 거래 시점에야 터지는 일을 없앤다.

**결정된 전제 (사용자 승인 2026-06-29).**
1. **운용 모델 = 단일 활성계좌 + 바인딩·가드** (한 번에 한 계좌. 다중 계좌 동시 운용은 후속 트랙으로
   확장 가능하게 설계하되 본 spec 범위 밖). 현 아키텍처(로컬 단일 브로커)와 정합.
2. **수수료 = 예상 + 실제 2단계** (사전 예상치 즉시 표시 + 청산 후 KIS 실수수료로 확정·라벨 구분).

**핵심 불변식 (절대).**
- **INV-SEC:** 자격증명·계좌번호·원시주문은 **로컬 PC 전용**. 서버/웹엔 **비민감 계좌 핸들**(opaque
  id·별명·브로커·자산군·모드 불리언)과 **체결 금액 요약**만. 계좌번호 원문·API키는 PC를 안 떠난다(§4).
- **INV-KIS-byte:** 기존 KIS 도달 가능 동작(주식 단독 / 주식+국내선물)은 **무변경**. 이 트랙은 주로
  *신규 표면*(계좌 핸들·바인딩·투명성 필드)이라 기존 발주·사이징 경로를 건드리지 않는다.

---

## 1. 동기

[autotrade-asset-class-redesign.md](../../REDESIGN/autotrade-asset-class-redesign.md)의 세 finding이 한 뿌리를 공유한다 — **전략이 계좌를 모른다.**

- **C7 (자금안전):** 웹 `run_mode`(paper/live)와 로컬 `virtual`이 화해 안 됨. 로컬 트레이더는 `run_mode`를
  안 읽고(grep 0건) 모든 deployed 전략을 *현재 페어링된 단일 브로커*에 실행 → **모의로 검증한 전략이
  실전 키 재등록·계좌 전환 시 무경고로 실거래**. 모드를 가르는 유일 스위치가 로컬 `virtual` 하나.
- **V3 (검증공백):** LS 연결 테스트는 토큰만 검증(계좌번호 미수신). 틀린 계좌번호가 "저장됨"으로 통과 →
  첫 사이클에서 거부(이연 실패). 계좌 식별의 신뢰 기반이 없음.
- **투명성 요구 (사용자):** 투입금액·상품·수량/계약수·레버리지·수수료가 어디에도 안 보임(§2). 자동매매에
  지장 없으려면 정확·투명 공유 필요.

**해결 한 줄:** *계좌를 1급 개념으로 세우고(검증된 비민감 핸들), 전략을 그 핸들에 묶고, 실행을 핸들로
게이트하며, 투입·비용을 핸들 단위로 투명화한다.*

---

## 2. 현황 그라운딩 (코드·API로 확인한 사실)

### 2.1 사이징은 정확히 계산되나 불투명
| 항목 | 현재 | 근거 |
|---|---|---|
| 주식 사이징 | 단일종목=현금 100%, 다종목=`amount_pct`%(기본 10). qty=`floor(budget/prev_close)` | `core/quant_core/ir_engine/live.py:132-143`, `exec_defaults.py:58` |
| 선물 사이징 | budget=`cash×futures_margin_pct`%(기본 20). 계약수=`floor(budget/(prev_close×multiplier×init_margin_rate))` | `live.py:118-137`, `spec.py:68`, `exec_defaults.py:130-141` |
| 레버리지 | **계산·저장·표시 안 됨**. (선물 내재=1/개시증거금률, 코스피200=10배) | 없음 |
| 사전 투입금액 | preview만 `est_total = qty×est_limit_price` 표시. **선물 qty=None(가림)** | `server/app/preview_engine.py:325-340` |

### 2.2 자금 투입 표시 갭
| 항목 | preview | 웹 | 로컬GUI | snapshot |
|---|---|---|---|---|
| 수량 | ✓ | ✓ | ✓ | ✓ |
| 가격 | ✓ | ✓ | ✓ | ✓ |
| **투입금액(KRW)** | ✓(est_total) | ✗ | ✗ | ✗ |
| **레버리지** | ✗ | ✗ | ✗ | ✗ |
| **수수료** | ✗ | ✗ | ✗ | ✗ |

snapshot per-trade = `{action, symbol, qty, reason, extra:{intended, fill}}` (`order_log.py:226-237`).
실현손익 = **GROSS**(수수료 차감 전, 명시 주석 `trader.py:626-629`).

### 2.3 수수료 — 무엇이 가능한가
- **백테스트:** 비용모델 있음 — 수수료 3bps·매도세 23bps·슬리피지 10bps(가정치, override 가능). `exec_defaults.py:67-69`.
- **실제(KIS):** `TTTC8715R`(기간별매매손익)가 종목별 `fee`+`tl_tax`, 계좌 `tot_fee`/`tot_tltx` 제공.
  해외=`CTOS4001R`/`TTTS3039R`. **⚠ 실전 전용(모의 미지원), 매도 청산 후에만(실현손익 TR).** 주문 시점엔 *추정* 제비용만.
- **실제(LS):** `t0424` fee/tax 필드 존재하나 **미검증·종목단위**, 전용 per-fill TR 없음. KIS가 강함.

### 2.4 계좌·모드 모델
- 전략 = `user_id`만, **계좌·브로커·모드 바인딩 0**. `run_mode`∈{draft,paper,live}. `models.py:30-42`.
- `/sync/strategies` = user_id의 paper+live 전부 pull. `sync.py:239-244`.
- 모의/실전 실행 = 로컬 자격증명 `virtual` 플래그 단 하나. `secrets_store.py:25-43`.
- 웹엔 **계좌/브로커 개념 전무**. Pair.tsx는 *기기↔웹계정* 페어링(브로커 아님).
- 활성화 = IrBuilder "모의 적용" → `save("paper")` (`IrBuilder.tsx:1295`). live 승격 UI 미구현(PromoteModal 보류, `Strategies.tsx:160`). 목록 = 탭(전체/모의/실전/초안) (`Strategies.tsx:124-154`).

### 2.5 계좌번호 검증
- KIS 주식 wizard: 토큰+잔고조회(TTTC8434R, `CANO`) → **계좌번호 검증함**. `kis_health.py:85-110`.
- LS 전체: **토큰만**(계좌번호 미수신). `ls_health.py:25-29`. KIS 선물/해외: GUI 밖 `futures_preflight.py`, 주식 잔고 검증이라 선물계좌(03) 미검증.
- LsBroker는 실주문·잔고에 입력 계좌번호 그대로 사용(`AcntNo`·t0424). `ls_broker.py:74·398·519`.
- ⚠ **LS appkey=계좌단위 발급** — 키가 계좌를 이미 결정. 잘못된 `AcntNo`를 LS가 거부할지 키 계좌로 무시할지 라이브 확인 필요(§8).

---

## 3. 목표 아키텍처

```
   계좌 핸들 (로컬 생성·비민감)             전략(서버)              실행(로컬)
   ┌────────────────────────┐        ┌──────────────┐     ┌──────────────────┐
   │ account_id (opaque)     │◀──ref──│ account_ref  │     │ 사이클: 각 전략의 │
   │ nickname / broker       │        │ (=account_id)│────▶│ account_ref ==    │
   │ asset_classes / mode    │        │ run_mode     │     │ 활성 핸들? 아니면 │
   │ (mode = virtual 유도)   │        │  = 핸들.mode │     │ skip+표면화       │
   └────────────────────────┘        └──────────────┘     └──────────────────┘
        │ snapshot.health.account_handles (비민감)              │
        ▼ + active_account_id                                   ▼ 투입·비용 투명화(D/E)
   서버/웹: 계좌 선택 UX·바인딩·advisory          preview(사전 예상) + 체결기록(사후 실제)
```

### 3.1 계좌 핸들 모델 (A — 비민감, INV-SEC 유지)

자격증명 슬롯마다 **계좌 핸들** 1개를 로컬에서 생성·보관(keyring 슬롯에 병기):

| 필드 | 정의 | 서버 전송 |
|---|---|---|
| `account_id` | 로컬 생성 **opaque uuid**. 슬롯의 (broker, 계좌번호, mode)가 바뀌면 **새 uuid 재발급** | ✅ (식별만, 계좌번호 무관) |
| `nickname` | 사용자 별명("내 KIS 실전 선물"). 미입력 시 자동 라벨(브로커+자산군+모드) | ✅ |
| `broker` | kis / ls | ✅ |
| `asset_classes` | 슬롯 기반 커버 자산군 집합(기존 `covered_categories` 재사용) | ✅ |
| `mode` | paper / live — **`virtual` 플래그에서 유도(검증된 사실)** | ✅ (불리언) |
| app_key·secret·계좌번호 | — | ❌ **로컬 전용** |

- **모드/계좌 변경 = 새 핸들.** 같은 슬롯을 실전 키로 덮어쓰면(virtual True→False) account_id가 바뀌어
  **옛 모의 핸들에 묶인 전략은 자동 실행 안 됨**(C7 핵심 가드 — 식별자 차원에서 차단).
- 핸들은 기존 P4 `asset_coverage` 신호와 같은 경로(`analytics.local_health()` → snapshot →
  `GET /sync/snapshot`)로 흐른다. 새 네트워크/엔드포인트 0. `active_account_id`(현재 페어링된 핸들)도 동반.
- **서버:** 전략에 `account_ref`(=account_id 문자열) 컬럼 추가. 핸들 목록 자체는 별도 테이블 없이
  snapshot.health에 실려 웹이 읽음(Over-engineering 회피 — 단일 활성계좌라 레지스트리 불필요).

### 3.2 전략 ↔ 계좌 바인딩 (B — 웹 UX 핵심)

**"모의 적용" 버튼 → "계좌 선택 후 적용":**
```
[ 전략 적용 ]
 어느 계좌로 실행할까요?
  ○ 내 KIS 모의 선물   (모의 · 국내선물)   ← 선택 = 이 전략 모의
  ○ 내 KIS 실전 주식   (실전 · 국내주식)   ← 선택 = 이 전략 실전
  ⊘ 내 LS 실전 선물    (자산군 불일치 — 이 전략은 국내선물 필요)  ← 비활성+사유
 [ 적용 ]
```
- 계좌 목록 = snapshot이 보고한 핸들들(별명·브로커·자산군·모드 배지).
- **선택한 계좌의 mode가 곧 `run_mode`** — 모의/실전 토글 **제거**. (C7 근본 해결: 모드=계좌의 검증된 속성)
- 전략 요구 자산군 ⊄ 핸들 커버리지면 그 계좌 **선택 불가(사유 표시)** — 기존 `_assert_live_tradable`
  (`strategies.py:83-159`)를 핸들 자산군과 대조하도록 확장(서버측 advisory).
- 핸들 0개(로컬 미등록/미페어링) → "먼저 로컬앱에서 계좌를 등록·페어링하세요" 안내.
- **모의→실전 승격 = 명시적 재바인딩:** 전략 상세 "[실전 계좌]로 전환" → "이 전략을 *실제 자금* 계좌
  '내 KIS 실전'으로 옮깁니다 — 다음 사이클부터 실거래됩니다" 확인. (C7 "무경고 전환"을 의도적 1액션으로)
- 전략 카드/목록/상세에 바인딩 계좌(별명+모드 배지) 상시 표시.

### 3.3 실행 가드 (C — 로컬, C7 근본 차단)

사이클 진입부(기존 P1 커버리지 게이트와 **같은 결정 지점·표면화 채널**)에서:
- 각 전략의 `account_ref`를 **현재 활성 핸들 account_id**와 대조.
  - 일치 → 실행. 불일치 → 전략 통째 skip + `skip_wrong_account` decision + 표면화:
    *"이 전략은 '내 KIS 모의'에 묶여 있어 현재 활성 계좌('내 KIS 실전')에서 실행되지 않습니다."*
- 계좌 전환/모드 flip 감지 → "N개 전략이 이전 계좌에 묶여 있습니다" 알림(조용히 옛/새 계좌 어디서도 안 돌림).
- `cycle_summary`에 `n_skip_wrong_account` 카운트 추가.
- run_mode 단독 체크는 **불필요해짐**(account_ref가 mode를 포함). → C7의 두 진실원천이 하나로 수렴.

### 3.4 자금 투입 투명성 — 사전 (D — preview 확장)

기존 preview(`est_total`)를 per-order로 확장:
```
오늘 투입 예정 — 계좌: 내 KIS 실전 선물 (실전)
 코스피200선물  1계약   명목 1,000만원   증거금 100만원   레버리지 10.0배
   예상 수수료 350원 + 제세금 0원
 ─────────────────────────────────────────
 총 투입(증거금) 100만원 / 명목 1,000만원 / 예상비용 350원
```
- 주식: 상품·수량·투입금액(qty×price)·예상수수료(+매도세).
- **선물(신규): 계약수·명목가치(notional=계약수×price×multiplier)·증거금(notional×init_margin_rate)·
  레버리지(=1/init_margin_rate)** — 전부 파생 계산, 새 데이터 0(`exec_defaults` spec 사용).
- 합계 행: 총 투입금액·총 증거금·총 예상비용.
- 위치: 웹 "적용" 확인 화면 + 대시보드 "오늘 투입 예정". **선물 qty 가림 정책 완화** — *본인 계좌 한정
  표시*(자기 자금·로컬 동의). (현재 `qty:None`은 USD 사이징 불가 + 보안 원칙이나, 본인 사전 미리보기엔 무해.)

### 3.5 자금 투입 투명성 — 사후 + 실제 수수료 (E)

- 체결 기록(snapshot trades)에 per-trade **투입금액**(qty×fill) + (선물)레버리지·증거금 추가.
- 청산 후 **KIS `TTTC8715R` 연동**(신규 broker 메서드 + 파서) → 종목별 **실수수료+제세금**, **net 실현손익**
  (현재 GROSS의 net 변형 추가 — GROSS도 유지). 해외=`CTOS4001R`/`TTTS3039R`.
- 웹/로컬에 **"예상" vs "실제" 라벨 명확 구분**. 모의·LS·청산 전 = "실제 수수료 집계 전(예상치)"으로 정직 표기.

---

## 4. 불변식 (보안 — INV-SEC 논증)

- 서버로 가는 신규 데이터:
  - `account_handles[]` = `{account_id(opaque uuid), nickname, broker, asset_classes, mode}` — **계좌번호·키
    원문 없음**. account_id는 로컬 난수 uuid라 계좌를 역산 불가.
  - 전략 `account_ref` = account_id 문자열(역시 비민감).
  - 체결 금액·수수료 수치 = §보안원칙의 "체결 로그 요약·잔고 스냅샷"(이미 허용 범주). 계좌번호 미동반.
- 변경 없는 것: app_key/secret·계좌번호·원시 주문은 keyring(OS)에만. KIS/LS 호출은 전부 로컬.
- **검증:** snapshot payload·서버 스키마·로그에 계좌번호/키 누수 0건을 테스트로 잠금(기존 INV-SEC 테스트 확장).

---

## 5. 단계 계획 (P5.0 ~ P7 · 독립 배포 가능)

| Phase | 목표 | 닫는 문제 | 주 변경(대략) | 의존 |
|---|---|---|---|---|
| **P5.0 (전제)** | 연결 테스트 계좌번호 검증 + 권위 계좌식별자 read-back | V3 | `local/ls_health.py`·`futures_preflight.py`·`gui.py` | 없음 — 즉시 |
| **P5** | 계좌 핸들 모델 + 전략 `account_ref` 바인딩 + 웹 계좌선택 UX + 로컬 실행 가드 | **C7 근본** + S5 일부 | `local/secrets_store.py`·`analytics.py`·`runner/trader`·`server/models·sync·strategies`·`web 전략화면` | P5.0 |
| **P6** | 사전 투명성(preview 확장: 레버리지·증거금·예상수수료·합계) | 투명성 사전 | `server/preview_engine.py`·`web preview/대시보드` | P5(계좌 표시) |
| **P7** | 사후 실제 수수료(KIS TTTC8715R) + net 실현손익 | 수수료 "실제" | `local/kis_broker.py`(신규 TR)·`trader.py`·`web/로컬 표시` | P5 |

**P5.0 상세 (V3 — 핸들 신뢰의 토대):**
1. `ls_health.test_credentials(app_key, secret, account_no, virtual)` — 토큰 후 **가벼운 잔고/계좌 조회
   1회**(t0424, 경로 `/stock/accno`)로 계좌번호 유효성 확인. 실패 → 테스트 실패.
2. 가능하면 응답에서 **브로커 권위 계좌식별자 read-back** → 입력값 대신 그걸로 핸들 생성. 불일치 시 경고.
   (LS appkey=계좌단위 특성상 가장 견고 — §8 라이브 확정 후.)
3. KIS 선물 preflight도 **선물 잔고 조회**로 계좌(03) 검증 보강.
4. 테스트: 잘못된 계좌번호 → 테스트 실패 / 올바른 계좌 → 성공·핸들 생성. (LS는 라이브 1회 캡처 게이트.)

**P5 상세 (C7 근본):**
1. `secrets_store`: 슬롯별 account_id(opaque uuid) 생성·보관, (broker,계좌,mode) 변경 시 재발급. 핸들 직렬화 함수.
2. `analytics.local_health()`: `account_handles[]` + `active_account_id` 추가(INV-SEC 논증 §4).
3. 서버: `Strategy.account_ref` 컬럼(+마이그레이션). `/sync/strategies`는 무변경(account_ref 동봉). `_assert_live_tradable`을 핸들 자산군 대조로 확장.
4. 로컬 실행 가드: 사이클에서 account_ref ↔ active_account_id 대조 → `skip_wrong_account`. run_mode 단독 의존 제거.
5. 웹: "모의 적용"→계좌 선택, 모드 토글 제거, 승격=재바인딩 확인, 카드/상세 계좌 배지. (희제 경계 §9)
6. 테스트: 핸들 생성/재발급·account_ref 바인딩·실행 가드 매트릭스(맞는 계좌 실행/틀린 계좌 skip/모드 flip).

**P6·P7 상세는 해당 implementation plan에서 task별로 구체화**(레버리지/증거금 파생식, TTTC8715R 파서·net P&L).

---

## 6. 검증 전략

- **C7 해결:** SimBroker 시나리오 — 모의 핸들 바인딩 전략이 실전 활성계좌에서 `skip_wrong_account`(발주 0) /
  올바른 핸들에서 정상 발주 / 모드 flip 후 옛 전략 skip. 단위+시나리오.
- **V3 해결:** 잘못된 계좌번호 → 연결 테스트 실패(KIS 기존 패턴 회귀 + LS 신규). LS는 라이브 캡처 1회.
- **투명성:** preview 레버리지/증거금/예상비용 수치 = 백테스트 비용모델·spec과 일치(골든). 사후 실수수료 =
  KIS TTTC8715R 실응답과 대조(실전 라이브 1회).
- **INV-SEC:** snapshot/서버/로그 계좌번호·키 누수 0 테스트.
- **라이브(사용자측):** 모의 1회 — 계좌 등록(번호 검증)→웹 계좌선택 적용→사이클 가드→투입 투명성 E2E.

---

## 7. 비목표 (YAGNI) / 후속 트랙

- **다중 계좌 동시 운용**(여러 계좌 동시 페어링·전략별 실행계좌 라우팅): 본 spec은 단일 활성계좌. 핸들 모델은
  account_id 다대다로 확장 가능하게 두되, 동시 실행(멀티 브로커 루프·계좌별 사이징)은 수요 확인 후 별도 트랙.
- **LS 실제 수수료**: t0424 fee/tax 미검증 — 라이브 확정 전 보류(P7은 KIS만). 확정 시 LS 추가.
- **서버 계좌 레지스트리 테이블**: 단일 활성계좌라 불필요(snapshot.health 경유). 다중계좌 트랙에서 재검토.

---

## 8. 미해결 결정 / 라이브 검증 필요 (정직)

- ~~**LS `AcntNo` 검증 거동**~~ **[확정 2026-06-29 라이브 캡처]:** LS는 appkey=계좌단위라 모든 *read* TR이
  계좌번호를 **보내지도 돌려주지도 않음**(CFOAQ50600/t0441/t0424 InBlock·응답 echo 0). account_no는
  국내주식·해외 *주문*(CSPAT00601 `AcntNo`)에서만 사용 → **LS 계좌번호는 read-only 검증·read-back 둘 다
  불가**(국내선물은 cosmetic). ⇒ P5.0 LS 검증은 "잘못된 번호 거부"가 **불가능** — 대신 **토큰+read TR 성공
  = appkey 계좌컨텍스트 라이브 확인**(token-only보다 강화)으로 한정하고, **LS 계좌 정체성은 appkey 기반
  핸들로**(P5). ⚠ 캡처는 *모의*(virtual=True). 실전 CFOAQ50600은 계좌요약을 제공하므로 *실전에서 응답이
  계좌를 echo하면 read-back 가능* — 실전 1회 캡처로 추가 확인(잔여).
- **선물 qty 사전 표시 완화:** 본인 계좌 한정 노출이 보안원칙과 충돌 없는지 최종 확인(자기 자금이라 무해 판단,
  희제·보안 합의).
- **TTTC8715R 호출 시점:** 청산 직후 vs 일배치 — 실현손익 TR 특성(매도 후 집계)상 종가청산/정산 사이클에 묶는 게 자연(라이브 1회로 타이밍 확정).
- **웹 승격 UI 위치:** 현재 live 승격 UI 미구현(PromoteModal 보류). StrategyDetail에 재바인딩 액션으로 신설.

---

## 9. 모듈 경계 (협업)

- **자동매매 엔진(조대표):** `local/localapp/*`(secrets_store·analytics·runner·trader·ls_health·futures_preflight·
  kis_broker)·`server/app/routers/{sync,strategies}.py`·`models.py`.
- **웹 전략화면(공통, 조대표 주도):** IrBuilder 적용 UX·Strategies/StrategyDetail 계좌 배지·승격. 일부 화면이
  희제 담당과 겹치면 **착수 전 PR/이슈에서 경계 협의**(CLAUDE.md §3).
- 착수 시 draft PR로 의도 broadcast(유실·중복 방지).
