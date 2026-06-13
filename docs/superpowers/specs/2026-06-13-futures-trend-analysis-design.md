# 선물 분석 "진입 추세" 확장 설계

**작성일:** 2026-06-13 · **브랜치:** `feat/futures-trend-analysis` (off origin/main `cdfbc06`)
**범위:** 선물 분석 대시보드(`oil_futures`)만 — 메인 IR/StrategyIR 엔진과 무관.

---

## 1. 문제 — 임계값 첫-터치 신호는 *경로*를 무시한다

현재 신호(`oil_futures/signals.py`)는 한 시점의 상태만 본다:

- **Short:** `오늘 high ≥ 임계 ∧ 어제 high < 임계` (위로 첫 터치 → 평균회귀 매도)
- **Long:** `오늘 low ≤ 임계 ∧ 어제 low > 임계` (아래로 첫 터치 → 평균회귀 매수)

같은 $85라도 **하락 중 데드캣 바운스(100→80→85)** 와 **지속 상승 끝의 도달(60→85)** 은
이후 수익 분포가 다른데, 현재 전략은 둘을 동일 취급한다(경로 의존성 누락). 또한 임계값 근처
진동 시 신호가 반복 발화(노이즈)한다.

## 2. 해결 — 네 방법을 두 기능으로

사용자가 제시한 네 방법을 두 갈래로 묶는다:

| 갈래 | 방법 | 기능 |
|---|---|---|
| **트리거를 경로-인지로** (어떤 신호가 발화하나) | ① N일 평균 임계값 · ③ 신호 최소 간격 | **기능 B — 신호 품질 옵션** |
| **추세→미래수익 관계 규명** (전략 무변경 분석) | ② 추세 조건부 forward 수익률 · ④ 과거↔미래 회귀 | **기능 A — 인터랙티브 조건부 수익률 탐색기** |

## 3. 범위

**포함:**
- `core/quant_core/oil_futures/` — 신규 `trend_analysis.py`, `signals.py`·`optimizer.py` 옵션 추가
- `server/app/routers/futures.py` — 신규 `/trend-events` + 기존 `/signals`·`/backtest`·`/grid` 파라미터 확장
- `web/src/api.ts` + `web/src/pages/FuturesAnalytics.tsx` — 탐색기 섹션 + 신호 설정 툴바

**제외 (YAGNI / 후속):**
- 개요용 `임계값×접근방향` 히트맵 (탐색기로 충분 — 원하면 후속)
- 엑셀 export에 신호 옵션 반영 (SL/TP처럼 v1 제외, 시트에 명시)
- 메인 IR 엔진·자동매매 — 무관

---

## 4. 기능 A — 인터랙티브 조건부 수익률 탐색기 (방법 2+4)

### 4.1 핵심 인터랙션
유저 예시 그대로: **"과거 L일간 증감율이 a%~b%였던 이벤트 → 이후 H일 수익률이 어떤가."**
컨트롤을 돌리면 통계·분포가 즉시 갱신된다.

**컨트롤:**
- 기준 모드: `전체 영업일`(베이스라인·큰 표본) / `신호 앵커`(선택한 side·threshold 신호일만 — 임계별)
- 과거 L일(lookback) · 미래 H일(horizon)
- 진입 직전 증감율 범위 `[a%, b%]` (핵심 필터)

**출력(실시간):**
- 매칭 n (+ `n<30` ⚠ 저신뢰 배지) · 평균 · 중앙값 · 승률(fwd>0)
- 이후 수익률 **분포 히스토그램**
- **산점도**(과거% × 미래%, 선택 밴드 하이라이트) + 회귀선·β·R²·HAC t/p (전역 readout)

### 4.2 데이터 흐름 — 무거운 계산 1회, 필터링 실시간
> 서버가 (L, H) 한 쌍에 대해 *모든 이벤트의 `{date, close, past_return, forward_return}`* 배열을
> 1회 내려준다 → 증감율 범위 슬라이더는 그 배열을 **브라우저에서 즉시 필터·재집계**(왕복 0).
> L·H·모드 변경 시에만 서버 재호출(디바운스 ~300ms).

근거: 슬라이더 반응성(render-lag 교훈 — 무거운 계산은 1회), 서버 부하·코드 최소화.
전체 영업일 모드도 ~4000행 → ~150KB JSON으로 가볍다.

### 4.3 코어 모듈 — `core/quant_core/oil_futures/trend_analysis.py` (신규)

```python
@dataclass(frozen=True)
class TrendEvent:
    date: pd.Timestamp
    close: float
    past_return: float      # close[t] / close[t-L] - 1
    forward_return: float   # close[t+H] / close[t] - 1

@dataclass(frozen=True)
class TrendRegression:
    slope: float            # forward ~ past 의 β
    intercept: float
    r_squared: float
    n: int
    hac_se: float           # Newey-West(Bartlett, maxlags=H) 표준오차
    hac_t_stat: float       # slope / hac_se
    hac_p_value: float      # 2*(1-Φ(|t|)), Φ=math.erf 기반 정규근사

def trend_events(
    df: pd.DataFrame, lookback: int, horizon: int,
    side: Side | None = None, threshold: float | None = None,
    smooth_window: int = 1, min_gap_days: int = 0,   # 기능 B 배선 시 사용
) -> list[TrendEvent]: ...

def trend_regression(events: list[TrendEvent], horizon: int) -> TrendRegression | None:
    # n < 3 이면 None. numpy OLS(slope·intercept·r2) + HAC SE.
```

**앵커 모드:**
- `side=threshold=None` → **전체 영업일**: `lookback ≤ t < n - horizon`인 모든 t.
- `side`+`threshold` 지정 → **신호 앵커**: `generate_signals(df, [threshold]…, smooth_window, min_gap_days)`
  의 각 신호일 인덱스 t (단, `t-L≥0 ∧ t+H<n`만).

**HAC 회귀 (numpy + stdlib만 — scipy/statsmodels 비의존):**
- OLS: `β, α = polyfit(past, forward, 1)`; `r²` = 1 - SSR/SST.
- 잔차 `e`, 설계행렬 `X=[1, past]`. Newey-West: `Ω = Σ_j w_j Σ_t e_t e_{t-j} x_t x_{t-j}ᵀ`,
  Bartlett 가중 `w_j = 1 - j/(maxlags+1)`, `maxlags = horizon`. `Var(β) = (XᵀX)⁻¹ Ω (XᵀX)⁻¹`.
- p값: `math.erf`로 표준정규 Φ → `2*(1-Φ(|t|))` (정규근사, 문서화).
- **검증:** HAC SE를 `statsmodels OLS(...).fit(cov_type="HAC", cov_kwds={"maxlags":H})`와 `1e-6` 대조
  (테스트 한정 — 런타임은 numpy만; 메인 엔진 Fama-MacBeth 대조 패턴과 동일).

### 4.4 서버 — `GET /{symbol}/trend-events`
```
?lookback=20&horizon=60&side=short&threshold=90    (side·threshold 생략 = 전체 영업일)
→ {
    lookback, horizon, mode: "all"|"signal",
    side: str|None, threshold: float|None,
    events: [{date, close, past_return, forward_return}, …],
    regression: {slope, intercept, r_squared, n, hac_se, hac_t_stat, hac_p_value} | null,
    low_sample: bool,    # n < 30
  }
```
- `_df(symbol)` 로더·503 패턴 재사용. 캐시 키 `(symbol, version, lookback, horizon, side, threshold, smooth_window, min_gap_days)`.
- 검증: `lookback ≥ 1`, `1 ≤ horizon ≤ 500`. 범위 밖이면 422.

### 4.5 웹
**`api.ts`:** `OilTrendEvent`·`OilTrendRegression`·`OilTrendEvents` 타입 + `futuresApi.trendEvents(sym, {lookback, horizon, side?, threshold?, smooth_window?, min_gap_days?})`.

**`FuturesAnalytics.tsx` 신규 섹션 ⑧ "TREND → FORWARD · 진입 추세 → 미래 수익률 탐색기":**
- 컨트롤(§4.1) → `trendEvents` fetch(모드·L·H·side·threshold 변경 시, 디바운스).
- 클라이언트: 응답 `events`를 증감율 `[a,b]`로 필터 → n·평균·중앙값·승률 재집계(JS);
  히스토그램(recharts `BarChart`, 고정 빈), 산점도(recharts `Scatter`, past×fwd, 밴드 하이라이트)
  + 회귀선(`response.regression`의 β·α로 두 끝점 `Line`) + R²·t·p readout.
- 저표본 ⚠ + 정직한 한계 노트(§6).

---

## 5. 기능 B — 신호 품질 옵션 (방법 1+3)

기존 신호 생성에 옵션 2개 추가. **기본값 = 현행 → `generate_signals` 출력 byte-identical(골든 무변경).**

### 5.1 코어 — `signals.py`
```python
def generate_signals(
    df, short_thresholds=(), long_thresholds=(),
    smooth_window: int = 1,    # 방법 1: 당일 고/저 대신 최근 N일 고/저 SMA로 교차 판정
    min_gap_days: int = 0,     # 방법 3: 같은 (side, threshold) 신호 후 M영업일 재발화 억제
) -> list[Signal]: ...
```
- **방법 1:** `smooth_window > 1`이면 `high`·`low`를 `rolling(window).mean()`으로 평활한 시리즈에
  교차 로직 적용. `=1`이면 원시(현행). 1일 스파이크는 평균이 안 따라와 미발화, 지속 추세만 발화.
- **방법 3:** `min_gap_days > 0`이면 각 (side, threshold)의 마지막 발화 인덱스 추적, `i - last < M`이면
  스킵(선발화 유지). `=0`이면 현행.

### 5.2 배선
- `optimizer.py` `grid_search`·`walk_forward`: `smooth_window`·`min_gap_days` 파라미터 추가 →
  내부 `generate_signals` 호출 3곳(grid_search 2 + walk_forward OOS 1)에 전달.
- `futures.py`: `/signals`(query)·`/backtest`(`BacktestRequest`)·`/grid`(query + 캐시 키)에 두 파라미터
  추가 → `generate_signals`/`grid_search`에 전달. **히트맵·순위표·백테스트 모두 반영**(신호 자체를 바꾸므로).
- 기능 B 적용 시, 기능 A `/trend-events`의 **신호 앵커 모드도 동일 설정 반영**(분석=거래 일치).

### 5.3 웹
- `api.ts`: `grid` opts·`signals`·`backtest` body에 `smooth_window?`·`min_gap_days?` 추가.
- `FuturesAnalytics.tsx`: 상단(종목 선택기 아래)에 **신호 설정 툴바** — `N일 평균(평활)`·`최소 신호 간격(일)`
  number 입력 2개. 변경 시 grid·backtest·trend-events 재호출. 기본값 1·0(현행).

---

## 6. 정직한 한계 (UI에 명시)

- **겹치는 forward 윈도우 → 자기상관 → OLS p 과대.** 그래서 **Newey-West(maxlags=H) HAC**를 주 지표로,
  원시 OLS는 참고만. p는 정규근사.
- **표본:** 신호 앵커는 임계별이라 수십 건으로 줄 수 있음 → `n<30` ⚠ 배지·신중 해석.
- **방법 1 부작용:** N일 평균은 항상 원시 고/저보다 안쪽 → 높은 임계는 미발화·신호 수 급감(의도된 동작, 섹션에 설명).
- **forward = close-to-close 서술용** — 실제 백테스트(익일 시가 진입·비용·SL/TP)와 다름. 추세-수익 *관계 측정*이지 거래 손익 아님.
- **데이터 한계 상속:** KOSPI 정적 스냅샷(미갱신)·롤은 연속물 내재 — 기존과 동일.

## 7. 검증 (전부 로컬)

1. **골든 회귀 0:** `smooth_window=1 ∧ min_gap_days=0`이면 `generate_signals` byte-identical
   (기존 신호/그리드/백테스트 테스트로 가드).
2. **신규 단위 테스트 `core/tests/test_trend_analysis.py`:** 합성 가격(상승추세·하락추세 구간 명시)
   → `trend_events`가 기대 past/forward·이벤트 수를 결정적으로 산출; `trend_regression`의 β 부호·HAC SE를
   statsmodels와 `1e-6` 대조; 신호 옵션 테스트(smooth>1 미발화·min_gap 스킵).
3. **웹 빌드:** `cd web && npm run build`(tsc) 통과.
4. **브라우저:** 배포 후 선물 탭에서 ⑧ 탐색기 슬라이더·신호 설정 토글 동작 확인
   (로그인 세션 필요 — 자동검증 경계는 명시 보고).

## 8. 구현 순서 (단계적·TDD, A→B)

1. `trend_analysis.py` 코어(`trend_events`·`trend_regression`) + 단위 테스트
2. `GET /trend-events` 엔드포인트 + `api.ts` 타입·클라이언트
3. UI 섹션 ⑧ 탐색기(컨트롤·실시간 집계·히스토그램·산점도)
4. `signals.py` 옵션(`smooth_window`·`min_gap_days`) + 테스트(byte-identical 기본값)
5. 엔드포인트·optimizer 배선(`/signals`·`/backtest`·`/grid`·trend-events 앵커)
6. UI 신호 설정 툴바
7. 전체 검증(골든 회귀 0 · 웹 빌드 · 브라우저)

## 9. 확정된 기본값

- 추세 측정 = 과거 L일 종가 증감율. 평활 = 고/저 SMA. 쿨다운 = (side,threshold)별·선발화 유지.
- 기준 앵커 = 신호 크로스일(generate_signals 재사용). "전체 영업일"은 무조건 베이스라인.
- 회귀 = 단변량 `forward ~ past` + HAC(numpy+stdlib). 버킷·밴드 통계는 브라우저 파생.
- 히스토그램 = 고정 빈. 산점도 과대 시 ~500점 다운샘플(클라이언트).
