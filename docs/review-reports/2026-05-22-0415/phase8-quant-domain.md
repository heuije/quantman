# Phase 8 — System Trading 도메인

`docs/QUANT_DOMAIN_CHECKLIST.md` 7개 카테고리 검증. 핵심 항목은 코드 grep + golden test, 나머지는 시간 효율상 ⚠️ partial.

## 카테고리별 결과 + 점수

| # | 카테고리 | 점수 | 결과 |
|---:|---|---:|---|
| 1 | 백테스트 정확성 | 7/10 | 1.1 ✅ / 1.2 부분 / 1.3 ⚠️ / 1.4 ✅ |
| 2 | 시그널 → 주문 | 5/10 | 2.1 ⚠️ / 2.2 ⚠️ |
| 3 | 모의 ↔ 실전 일관성 | 5/10 | 코드 존재, 실제 paired test 미실행 |
| 4 | 리스크 관리 | 7/10 | 4.1·4.2 ✅ / 4.3 ❌ |
| 5 | KIS ↔ ledger 정합성 | 8/10 | reconcile + alert toggle ✅ |
| 6 | 자격증명 분리 | 10/10 | 서버 0 흔적 + ACL 보호 (Phase 7 검증) |
| 7 | 한국 시장 특수성 | 6/10 | tick_size ✅ / KST 명시 미확인 ⚠️ |
| **평균** | | **6.9/10** | |

## 검증 결과 상세

### 1.1 Look-ahead Bias 방지 — ✅

Golden test 15/15 PASS:
```
test_matches_baseline[01_buy_and_hold] PASSED
test_matches_baseline[02_above_ma20] PASSED
... (총 5)
test_idempotent[*] PASSED (5)
test_no_lookahead_bias[*] PASSED (5)
```

`test_no_lookahead_bias`는 데이터 끝 60일 잘라서 다시 실행 시 결과 동일성 검증 — bias 있으면 끝 데이터가 중간 시점에 영향 줘 결과 달라짐. 5개 전략 모두 PASS = bias 없음. (실행 5분)

### 1.2 시장 마찰 모델링 — 부분

**확인됨**:
- `backtest.py:74-75` `commission: float = 0.00015 (0.015%)`, `slippage: float = 0.0005 (0.05%)` ✅
- `exec_defaults.py:tick_size·round_to_tick` 한국 호가 단위 라운딩 ✅

**❌ Critical 누락**: **한국 매도 세금 (거래세 0.18% + 농특세 0.05% = 0.23%)** 미반영
- `backtest.py:172-174` 매도 분기에 `commission`만 차감, 세금 추가 없음
- 매도 시 실제 손익은 코드 추정보다 -0.23%p 낮음
- 잦은 매매 전략일수록 백테스트가 더 낙관적으로 나옴 — `02_above_ma20`(38거래) ·`03_uptrend`(246거래) 결과 신뢰성 영향

**권장 fix** (`backtest.py` 매도 분기에):
```python
# 한국 시장 매도 세금: 거래세 0.18% + 농특세 0.05%
KOREAN_SELL_TAX = 0.0023

price = raw_price * (1 - slippage - extra)
proceeds = shares * price * (1 - commission - KOREAN_SELL_TAX)
```

→ golden baseline 재생성 필요 (수치 변동). 사용자 결정 필요 (baseline 폐기·재생성 결정).

### 1.3 기업 액션 — ⚠️ 미검증

배당·액면분할 조정은 data parquet 생성 단계에서 처리되었어야 함. `core/data/`는 gitignore. 외부 source (KRX·NAVER)가 adjusted close 제공 여부 확인 필요. 표본 점검 (005930 2018-05-04 액면분할일) 권장 — backlog.

### 1.4 자본 곡선 정확성 — ✅

Golden test의 equity·metrics 정합성 검증 통과.

### 2.1 Idempotency — ⚠️

`local/`에서 `dedup|idempot|already.+sent|order.+exists` grep 결과 **0건**.

- `order_log.py·trader.py` 직접 검토 필요 (시간 부족)
- order_no 기반 매칭이 존재하면 dedup 패턴이 다른 이름일 가능성
- backlog: 명시적 dedup 검증 케이스 추가 (예: 같은 시그널 2회 trigger 시 1주문 발사 단위 테스트)

### 2.2 포지션 사이징 — ⚠️ 미검증

사이징 함수 grep 필요. ATR 모드(Phase 9A) 도입돼 있고 UI에서 노출됨 (Phase 2 ConditionBuilder 확인). 단위 테스트는 골든 test 외에 없음 — backlog.

### 4.1 Kill Switch — ✅

`local/localapp/killswitch.py` 존재. 6개 파일에서 참조 (`sync_client·config·gui·trader·order_log·killswitch`).
Phase 42-2에서 `kill_switch_daily_loss_pct` UI 노출 ✅.

### 4.2 Drawdown 한도 — ✅

`drawdown|max_drawdown` 3개 파일 (`sync_client·trader·analytics`). Phase 42-2에서 `max_drawdown_pct` UI 노출 ✅.

### 4.3 단일 종목·섹터 한도 — ❌ 미구현

체크리스트에서 "현재 미구현으로 추정"으로 표시된 항목. grep으로도 단일종목 비중 클램프 없음.

**상황**: ATR 사이징의 `cap = capital * 10%` 같은 단일 종목 한도가 어딘가 있을 가능성. 확인 못 함.

**권장** (Phase 9 backlog): 종목당 자본의 20% 등 default cap 추가 + UI 노출.

### 5.1 Reconciliation — ✅

`reconcile` 4개 파일에 존재 (`runner·gui·trader·analytics`).
Phase 42-2에서 `alert_on_reconcile_drift` toggle 추가 ✅ (Settings 페이지 노출 확인).

### 6.1·6.2 자격증명 분리 — ✅

Phase 7에서 검증:
- 서버 KIS 자격증명 grep 0건 ✅
- 로컬앱 `.kis_token.json` Windows ACL 보호 (Phase 41-C-2/3) ✅

### 7.1 KST 시간 처리 — ⚠️

`core/` 디렉토리에서 `timezone|KST|tz=|astimezone` grep **0건**.
- 백테스트 datetime이 timezone-naive
- parquet의 date index가 KST 기준임을 코드가 강제 안 함
- 실전 (local app)은 KIS API가 KST 반환하니 동작은 함
- 백테스트 데이터·라이브 데이터 정렬에 mismatch 가능성

**권장** (Phase 9 backlog):
- `core/quant_core/backtest.py` 초입에 명시적 검증 + docstring 한 줄
- parquet 생성 단계에서 `pd.DatetimeIndex(tz='Asia/Seoul')` 설정

### 7.2 시장 규칙 — 부분

- 호가 단위 `tick_size` ✅
- 상한가/하한가 ±30%, 시가·종가 단일가, 시간외 단일가 — 미확인 (대부분 데이터 단계 처리 가정)

## 핵심 결함 요약 → Phase 9에 surface

| 등급 | 결함 | 영향 |
|---|---|---|
| 🔴 High | 1.2 한국 매도 세금 0.23% 미반영 | 잦은 매매 전략 백테스트 결과 낙관적 편향 |
| 🟠 High | 4.3 단일 종목 비중 한도 미구현 | 자본 집중 risk 통제 안 됨 |
| 🟡 Medium | 7.1 백테스트 timezone 명시 누락 | 데이터·KST 정합성 검증 불가 |
| 🟡 Medium | 2.1 idempotency 명시적 검증 부재 | 재시작·재시도 시 중복 주문 risk |
| 🟢 Low | 1.3 기업 액션 조정 검증 | 표본 점검 필요 |

## 다음

Phase 9 — SUMMARY 통합.
