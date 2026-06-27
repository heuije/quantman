# 선물 분석 "급등락 → 회귀(REVERSION)" 탐색기 설계

**작성일:** 2026-06-27 · **브랜치:** `feat/futures-reversion` (off origin/main `08d3ba7`)
**범위:** 선물 분석 대시보드(`oil_futures`)만 — 메인 IR/StrategyIR 엔진·자동매매와 무관. 분석 전용(read-only).

---

## 1. 문제 — 고정 윈도우는 *추세의 시작·크기*를 모른다

기존 탐색기(`trend_analysis.py`)는 **고정 윈도우**다: "L영업일 전 대비 과거수익률 → H영업일 후 수익률".
시작점이 매 영업일이라 "추세가 어디서 시작됐고 얼마나 컸나"라는 *경로/이벤트* 개념이 없다.

사용자가 원하는 질문은 다르다:

> **"가격이 크게 급등/급락(추세)한 *뒤* 평균회귀(반등)가 실제로 나오는가?"**

이건 이벤트 구동이다 — 추세가 스스로 형성되고, 일정 크기까지 달린 *뒤*에 되돌림을 본다.
고정 윈도우로는 표현이 안 되므로 새 모델이 필요하다.

## 2. 해결 — 트레일링 피벗 + 누적 트리거 + 역추세 회귀 측정

**3단계 이벤트 모델:**

1. **추세 시작(피벗)** — ZigZag/트레일링 방식. running 극점에서 가격이 `전환임계`만큼 역행하면 그 극점을
   추세 전환점(피벗)으로 잠근다.
2. **급등락 완성(트리거)** — 피벗 대비 누적이 `급등락임계`(±)에 처음 도달하는 날을 이벤트로 기록한다.
3. **회귀 측정** — 그 시점에 **역추세 진입**(하락소진→롱 / 상승소진→숏)했다고 보고, `N영업일` 후
   계좌 수익률(= 되돌림 = 회귀)을 측정한다.

### 2.1 레버리지가 *이벤트 포착 기준 자체*를 바꾼다 (핵심 결정)

선물이므로 **모든 % 는 레버리지 반영 계좌 기준**이다. 계좌수익률 = 지수수익률 × 레버리지(선형, 고정계약·정액명목).

- 전환임계 5%(계좌) = 지수 0.5% (10배) → 피벗 감도.
- 급등락임계 ±30%(계좌) = 지수 ±3% → 트리거. *(코스피200선물은 지수로 30% 안 움직여도 계좌로는 자주 —
  지수 기준이면 이벤트가 거의 안 잡힌다.)*
- 엔진은 내부적으로 **지수 가격**에서 `÷레버리지`로 환산해 같은 이벤트를 잡고, 결과는 `×레버리지`로 계좌화한다.

### 2.2 청산(마진콜) 반영 — 고배율 수치의 정직성

트리거 시점 역추세 진입 후, **N일 안에 일봉 종가 경로가 역행으로 계좌 −100%에 닿으면 청산**(전손)으로 본다.
- 계좌 −100% = 지수 역행 `100% ÷ 레버리지` (10배면 지수 ±10%).
- 청산된 이벤트는 회귀 수익률 = **−1.0(−100%)** 로 집계(반등이 와도 못 먹음). 청산 안 됐으면 N일 후 계좌수익률.
- 청산가 = 증거금 전액 소진 기준(단순·보수적). 유지증거금 세부 모델은 후속.

## 3. 범위

**포함:**
- `core/quant_core/oil_futures/reversion_analysis.py` — 신규(`find_pivots`·`reversion_events`·`reversion_summary`·`reversion_sweep`).
- `core/quant_core/oil_futures/__init__.py` — 신규 심볼 export.
- `server/app/routers/futures.py` — 신규 `GET /{symbol}/reversion` + `GET /{symbol}/reversion-sweep`.
- `web/src/api.ts` + `web/src/pages/FuturesAnalytics.tsx` — 새 모드 "급등락 → 회귀(REVERSION)".

**제외 (YAGNI / 후속):**
- 라이브수식 엑셀 export (기존 TREND 탐색기처럼) — 우선 분석부터, 엑셀은 후속 Phase.
- 레버리지를 스윕 축으로 두기 — 단일 입력으로 시작(청산이 비선형이라 의미 있는 후속).
- 유지증거금/일중(고저) 경로 청산 — v1은 종가 경로·전손(−100%) 기준.
- 메인 IR 엔진·자동매매 배선 — 무관(분석 전용).

---

## 4. 코어 모듈 — `core/quant_core/oil_futures/reversion_analysis.py` (신규)

기존 `trend_events`(고정 윈도우)·`generate_signals`(절대 레벨)로는 트레일링 피벗을 표현할 수 없어 새 파일로 분리한다.
종가 기반(종가-종가 일관 — 정직한 서술용, 기존 모듈과 동일 철학).

### 4.1 자료구조

```python
@dataclass(frozen=True)
class Pivot:
    date: pd.Timestamp
    price: float
    kind: str               # "high" | "low" (교대로 나옴)

@dataclass(frozen=True)
class ReversionEvent:
    pivot_date: pd.Timestamp
    pivot_price: float
    trigger_date: pd.Timestamp
    trigger_price: float
    direction: str          # "down_exhaustion"(하락소진→롱) | "up_exhaustion"(상승소진→숏)
    run_account: float      # 트리거 시점 누적(계좌, 부호: 상승+/하락-) = lev*(trigger/pivot - 1)
    reversion_account: float  # N일 후 역추세 포지션 계좌수익률(청산 시 -1.0). +면 회귀, -면 추세지속
    liquidated: bool
    liquidation_day: int | None   # 청산까지 영업일(트리거 후). 청산 안 했으면 None
```

> `reversion_account`는 역추세 포지션 기준이라 **그 자체가 회귀-부호 통일값**이다(+ = 되돌림).
> 이게 화면의 "회귀 수익률"이며, 요약 평균·중앙값의 입력이다.

### 4.2 함수

```python
def find_pivots(df, *, reversal_account_pct: float, leverage: float) -> list[Pivot]:
    """ZigZag 트레일링 피벗(종가 기준). 계좌 reversal% → 지수 reversal = reversal_account_pct/leverage.
    running 극점에서 지수가 그만큼 역행하면 극점을 피벗으로 확정·방향 전환. 고/저 교대 리스트(날짜 ASC)."""

def reversion_events(
    df, *, reversal_account_pct: float, run_account_pct: float, horizon: int,
    leverage: float, gap: int = 0, direction: str | None = None,
) -> list[ReversionEvent]:
    """피벗별 leg(피벗→다음 피벗)에서 |계좌누적|이 run_account_pct에 처음 도달하는 날=트리거.
    트리거에서 역추세 진입(하락소진→롱·상승소진→숏)의 N일 후 계좌수익률(청산 반영) 측정.
    direction 지정 시 그 방향만. gap>0이면 트리거일 G영업일 디클러스터."""

def reversion_summary(events: list[ReversionEvent]) -> dict:
    """방향별(down_exhaustion·up_exhaustion) 요약:
    {n, mean_reversion(%), median_reversion(%), success_rate(%), liquidation_rate(%)}.
    mean·median 모두 청산(-100%) 포함. success = reversion_account>0 비율(청산은 실패)."""

REVERSION_SWEEP_AXES = ("reversal", "run", "horizon")

@dataclass(frozen=True)
class ReversionSweepCell:
    row: int; col: int; n: int
    mean_reversion: float    # 계좌 %, n==0이면 nan
    success_rate: float      # %, n==0이면 nan
    liquidation_rate: float  # %, n==0이면 nan

def reversion_sweep(
    df, *, row_axis: str, col_axis: str, row_values: list, col_values: list,
    reversal_account_pct: float, run_account_pct: float, horizon: int,
    leverage: float, gap: int, direction: str,
) -> list[ReversionSweepCell]:
    """{reversal·run·horizon} 중 2축 격자 스윕(나머지 고정). 칸=해당 config의 한 방향 요약 지표.
    reversal 고정 축이면 피벗 1회 캐시. (L,H) 설명력 지도와 같은 셀 형상."""
```

### 4.3 알고리즘 정밀

- **find_pivots (ZigZag):** `r_idx = reversal_account_pct / leverage / 100`. 상승 추정 중 running max 갱신,
  종가가 max에서 `r_idx` 이상 하락하면 그 max를 high 피벗 확정·하락 전환(running min 시작). 대칭.
  첫 방향은 첫 `r_idx` 돌파로 결정. **종가만** 사용.
- **트리거 탐색:** 피벗 다음 영업일부터 다음 피벗(또는 데이터 끝)까지, `acct_cum(t)=leverage*(close[t]/pivot_price-1)`.
  `|acct_cum| ≥ run_account_pct/100` 첫 t. 부호는 leg 방향과 일치(high 피벗→음수=하락소진, low→양수=상승소진).
  `t + horizon < n` 인 트리거만 채택(완전한 forward 윈도우 필요).
- **회귀·청산:** `pos = +1`(하락소진=롱) / `−1`(상승소진=숏). τ=trigger+1..trigger+horizon에서
  `r(τ)=pos*leverage*(close[τ]/close[trigger]-1)`. `r(τ) ≤ −1` 최초 τ에서 청산(liquidated=True,
  reversion_account=−1.0, liquidation_day=τ−trigger). 아니면 reversion_account=`r(trigger+horizon)`.
- **디클러스터:** gap>0이면 트리거일을 영업일 인덱스 기준 G 이내 1건만(기존 `_decluster` 패턴 — 영속 헬퍼화).

## 5. 서버 — `routers/futures.py`

기존 `_df(symbol)` 로더·503·캐시·422 검증 패턴 재사용.

### 5.1 `GET /{symbol}/reversion`
```
?reversal=5&run=30&horizon=20&leverage=10&gap=0
→ {
    symbol, reversal, run, horizon, leverage, gap,
    summary: {
      down_exhaustion: {n, mean_reversion, median_reversion, success_rate, liquidation_rate},
      up_exhaustion:   {n, mean_reversion, median_reversion, success_rate, liquidation_rate},
    },
    events: [{pivot_date, pivot_price, trigger_date, trigger_price, direction,
              run_account, reversion_account, liquidated, liquidation_day}, …],
  }
```
- 검증(422): `reversal>0`, `run>0`, `1≤horizon≤500`, `leverage≥1`, `gap≥0`.

### 5.2 `GET /{symbol}/reversion-sweep`
```
?row_axis=run&col_axis=horizon&direction=down&reversal=5&run=30&horizon=20&leverage=10&gap=0
→ { row_axis, col_axis, row_values, col_values, direction, cells:[{row,col,n,mean_reversion,success_rate,liquidation_rate}] }
```
- 축 값 자동 생성: `run`=중심±(예 20·30·40·50), `horizon`=(5·10·20·40·60), `reversal`=(3·5·8·12). 중심은 입력값.
- 같은/잘못된 축 422.

## 6. 웹 — `FuturesAnalytics.tsx` + `api.ts`

**`api.ts`:** `OilReversionEvent`·`OilReversionSummary`·`OilReversion`·`OilReversionSweepCell`·`OilReversionSweep` 타입
+ `futuresApi.reversion(sym, {reversal,run,horizon,leverage,gap})`·`reversionSweep(sym, {...,row_axis,col_axis,direction})`.

**새 모드 "급등락 → 회귀(REVERSION)"** (TREND→FORWARD와 나란히 모드 토글):
- **입력 툴바:** 전환임계(%·기본 5) · 급등락임계(%·기본 30) · 반등 N일(기본 20) · **레버리지(배·기본 10)** ·
  이벤트 최소간격(gap). 변경 시 `reversion` fetch(디바운스 ~300ms).
- **요약 카드 2개 — 방향 분리** (📉 하락소진→롱 / 📈 상승소진→숏): 표본수 · **회귀 수익률 평균** ·
  **회귀 수익률 중앙값** · 회귀 성공률 · 청산율. 모든 %는 계좌 기준, 평균·중앙값에 청산(−100%) 포함.
- **이벤트 표:** 피벗일·트리거일·방향·트리거 시 누적(계좌)·N일 후 회귀(계좌, 청산 시 "청산·D+k"). 손검증용.
- **스윕 격자:** 축 자유선택(기본 `급등락임계 × N일`) + 방향 토글 + 지표 토글(회귀 평균 / 성공률 / 청산율 / 표본수).
  칸 색 = 회귀 평균(빨강=회귀 / 파랑=추세지속, 한국식 방향색). **렌더러는 기존 `teMetricCell` 재사용**(컴퓨트만 신규).
- 저표본·청산 경고 노트(§7).

## 7. 정직한 한계 (UI에 명시)

- **종가 기반 피벗·종가-종가 forward** → 실제 백테스트 아님(비용·익일시가·SL/TP·체결지연 미반영).
  추세-회귀 *관계 측정*이지 거래 손익 아님.
- **트리거·forward는 PIT-clean**(미래 미참조)이나, 피벗 가격은 과거 running 극점(트레일링 특성).
- **레버리지가 높을수록** 같은 임계%가 더 작은 지수 움직임 → 이벤트 급증·청산도 급증. 표본수·청산율을 함께 표시.
- **청산 = 종가 경로·전손(−100%) 단순 기준.** 일중 저점/유지증거금이면 더 일찍 청산될 수 있음(보수적이지 않을 수 있음 — 명시).
- **겹치는 forward 윈도우 자기상관** — 평균·비율은 영향 적으나 통계검정(HAC 등)은 안 함(기술통계만, 회귀 탐색기와 역할 분리).
- **데이터 한계 상속:** KOSPI 정적 스냅샷·롤 연속물 내재 — 기존과 동일.

## 8. 검증 (전부 로컬)

1. **신규 단위 테스트 `core/tests/test_reversion_analysis.py`:** 합성 가격(상승·하락 leg를 명시 설계)으로
   - `find_pivots`가 기대 피벗(고/저·날짜·가격)을 결정적으로 산출.
   - `reversion_events`가 기대 트리거일·run_account·reversion_account를 산출(레버리지 환산·부호 통일 단언).
   - **청산 케이스**: 역행 경로가 −100% 닿는 시리즈 → liquidated=True·reversion=−1.0·liquidation_day 정확.
   - 방향 분리·gap 디클러스터·`reversion_summary`(평균/중앙값/성공률/청산율) 단언.
   - `reversion_sweep` 격자 형상·캐시.
2. **서버 테스트 `server/tests/test_futures_reversion.py`:** 두 엔드포인트 200·검증 422·요약 형상.
3. **기존 골든·전체 suite 회귀 0**(신규 파일·가산적 — trend_analysis 무영향).
4. **웹 빌드:** `cd web && tsc --noEmit`(+ Vercel fresh build) 통과.
5. **브라우저(배포 후):** 선물 탭 REVERSION 모드에서 입력·요약 카드·스윕 동작 확인
   (로그인 세션 필요 — 자동검증 경계는 명시 보고).

## 9. 구현 순서 (단계적·TDD)

1. `reversion_analysis.py` 코어 — `find_pivots` + 단위 테스트(피벗 결정성).
2. `reversion_events`·`reversion_summary` + 테스트(트리거·레버리지·부호·**청산**·방향·gap).
3. `reversion_sweep` + 테스트(격자·캐시).
4. `__init__.py` export.
5. `GET /reversion` + `GET /reversion-sweep` + 서버 테스트.
6. `api.ts` 타입·클라이언트.
7. UI 새 모드(입력·요약 카드·이벤트 표·스윕 격자).
8. 전체 검증(core·server·web 빌드).

## 10. 확정된 기본값·결정

- 추세 시작 = ZigZag 트레일링 피벗(종가). 급등락 = 피벗 대비 누적. 회귀 = 트리거 역추세 N일 후.
- **모든 % = 레버리지 반영 계좌 기준.** 엔진은 지수에서 `÷레버리지` 환산·결과 `×레버리지`.
- 암묵 포지션 = **역추세**(하락소진→롱·상승소진→숏). 청산도 이 포지션 기준.
- 청산 = N일 내 계좌 −100% 도달(종가 경로)→ −100% 집계.
- 방향 = **상승소진/하락소진 분리** 표시. 임계·N일·레버리지·gap 전부 사용자 설정.
- 회귀 대표값 = **평균(청산 포함 기대값) + 중앙값(전형값)** 병기 + 청산율로 교차검증.
- 스윕 = `{reversal·run·horizon}` 중 2축, 기본 `run×horizon`. 칸 렌더러는 기존 `teMetricCell` 재사용.
