# 멀티 종목 선물 분석 대시보드 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** WTI 원유 전용 대시보드(`/oil-futures`)를 종목 무관 일반화 구조로 바꿔 원유·나스닥100·천연가스·금·은·비트코인 6개 USD 실선물을 단일 "선물 분석" 탭(종목 선택기)에서 분석한다.

**Architecture:** 검증된 core 백테스트 엔진은 그대로 두고 WTI 전용 계약상수 2개(`WTI_TICK`/`WTI_MULTIPLIER`)만 `ContractSpec` 파라미터로 추출(기본값 WTI → 하위호환). 종목별 차이(계약사양·임계범위·데이터키·라벨)는 서버 `futures_config.INSTRUMENTS` 레지스트리 한 곳에 두고, 라우터는 `/futures/{symbol}/*`로 일반화하며 `symbol`만 받아 config를 조회. 웹은 단일 컴포넌트 + 종목 선택기로, 라벨은 서버 `data-info` 응답에서 받는다.

**Tech Stack:** Python(quant_core pure + FastAPI), React+TypeScript+Vite, pandas, pytest, recharts.

**스펙:** `platform/docs/superpowers/specs/2026-06-02-multi-instrument-futures-dashboard-design.md`

**작업 위치:** 모든 경로는 `C:\Users\USER\Desktop\창업\퀀트\platform\` 기준. 브랜치 `feature/multi-instrument-futures` (이미 생성됨, 스펙 커밋 `5d5d886` 위).

**검증 명령 기준:**
- core 테스트: `cd platform/core && python -m pytest tests/test_oil_futures.py -v`
- server 테스트: `cd platform/server && python -m pytest tests/test_oil_futures_auth.py -v`
- web 타입체크: `cd platform/web && npx tsc --noEmit`

---

## File Structure

생성:
- `core/quant_core/oil_futures/` — `ContractSpec`·`WTI_SPEC` 추가(backtest.py 내), 모듈명·파일명 유지.
- `server/app/routers/futures.py` — `oil_futures.py`를 `git mv` 후 일반화.
- `server/app/futures_config.py` — `INSTRUMENTS` 레지스트리 (신규).

수정:
- `core/quant_core/oil_futures/backtest.py` — 상수 → `ContractSpec`, `run_backtest(spec=)`.
- `core/quant_core/oil_futures/optimizer.py` — `grid_search(spec=)`·`walk_forward(spec=)` 관통.
- `core/quant_core/oil_futures/__init__.py` — export 교체(`WTI_TICK/WTI_MULTIPLIER` 제거 → `ContractSpec/WTI_SPEC`).
- `core/quant_core/data_fetcher.py` — `YFINANCE_SYMBOLS`·`SYMBOL_CATEGORY`에 NQ=F·SI=F·BTC=F 추가.
- `server/app/main.py` — 라우터 import·등록 `oil_futures`→`futures`, 워머 로그 문구.
- `web/src/api.ts` — `oilApi`→`futuresApi`(symbol 인자), 타입 일반화, instruments·meta 필드.
- `web/src/pages/OilFutures.tsx` → `web/src/pages/FuturesAnalytics.tsx` (`git mv` 후 selector·라벨 일반화).
- `web/src/App.tsx` — 라우트 `/oil-futures`→`/futures`.
- `web/src/components/Layout.tsx` — nav "원유 분석"→"선물 분석", `/oil-futures`→`/futures`.
- `web/src/index.css` — 종목 선택기 스타일(`.futures-selector`).

테스트 수정:
- `core/tests/test_oil_futures.py` — 비-WTI spec 스케일 단위테스트 추가(기존 20개는 무수정 통과).
- `server/tests/test_oil_futures_auth.py` → `test_futures_auth.py` (`git mv`) — `/futures/{symbol}/*` 401·404 검증으로 재작성.

---

## Task 1: core — ContractSpec 추출 (엔진 하위호환)

**Files:**
- Modify: `core/quant_core/oil_futures/backtest.py`
- Modify: `core/quant_core/oil_futures/optimizer.py`
- Modify: `core/quant_core/oil_futures/__init__.py`
- Test: `core/tests/test_oil_futures.py`

목표: `WTI_TICK`/`WTI_MULTIPLIER` 모듈상수를 `ContractSpec(tick, multiplier)`로 추출하고 `run_backtest`/`grid_search`/`walk_forward`에 `spec` 파라미터를 추가한다. 기본값 `WTI_SPEC`이라 기존 호출·테스트 전부 무변경 통과.

- [ ] **Step 1: 비-WTI spec 스케일 단위테스트 작성 (실패 확인용)**

`core/tests/test_oil_futures.py` 의 import 블록(line 19-28)을 아래로 교체 — `ContractSpec` 추가:

```python
from quant_core.oil_futures import (
    ContractSpec,
    CostModel,
    Side,
    generate_signals,
    grid_search,
    grid_to_dataframe,
    run_backtest,
    summarize,
    walk_forward,
)
```

파일 끝(`test_walk_forward_train_test_split` 뒤)에 추가:

```python
def test_contract_spec_scales_usd_pnl_not_returns(small_df: pd.DataFrame) -> None:
    """ContractSpec 의 multiplier 가 $ PnL 을 선형 스케일하되 수익률%·승률·샤프는
    spec 불변임을 확인. 같은 거래(동일 신호·가격)에 다른 spec 적용해 비교."""
    sigs = generate_signals(small_df, short_thresholds=[80])
    wti = run_backtest(small_df, sigs, 3, CostModel(0, 0),
                       spec=ContractSpec(tick=0.01, multiplier=1000))
    # gold-유사 spec: tick 0.10, multiplier 100 → multiplier 1/10
    gold = run_backtest(small_df, sigs, 3, CostModel(0, 0),
                        spec=ContractSpec(tick=0.10, multiplier=100))
    assert len(wti.trades) == 1 and len(gold.trades) == 1
    tw, tg = wti.trades[0], gold.trades[0]
    # gross_pnl_usd 는 multiplier 비율(100/1000=0.1)로 스케일
    assert tg.gross_pnl_usd == pytest.approx(tw.gross_pnl_usd * (100 / 1000))
    assert tg.net_pnl_usd == pytest.approx(tw.net_pnl_usd * (100 / 1000))
    # 수익률%(가격비율)은 spec 불변
    assert tg.return_pct == pytest.approx(tw.return_pct)
    sw, sg = summarize(wti), summarize(gold)
    assert sg.win_rate == pytest.approx(sw.win_rate)
    assert sg.sharpe_annualized == pytest.approx(sw.sharpe_annualized)
    assert sg.total_net_pnl_usd == pytest.approx(sw.total_net_pnl_usd * (100 / 1000))


def test_default_spec_is_wti_backward_compatible(small_df: pd.DataFrame) -> None:
    """spec 미지정 = WTI_SPEC(0.01/1000). 기존 동작과 바이트 동일."""
    sigs = generate_signals(small_df, short_thresholds=[80])
    default = run_backtest(small_df, sigs, 3, CostModel(0, 0))
    explicit = run_backtest(small_df, sigs, 3, CostModel(0, 0),
                            spec=ContractSpec(tick=0.01, multiplier=1000))
    assert default.trades[0].net_pnl_usd == pytest.approx(explicit.trades[0].net_pnl_usd)
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `cd platform/core && python -m pytest tests/test_oil_futures.py -k contract_spec_or_default_spec -v`
(또는 `-k "contract_spec or default_spec"`)
Expected: FAIL — `ImportError: cannot import name 'ContractSpec'`.

- [ ] **Step 3: backtest.py — ContractSpec 정의 + 상수 제거**

`backtest.py` line 22-24 (`# WTI 선물 (CL) 계약 사양` ~ `WTI_MULTIPLIER = 1000`)을 아래로 교체:

```python
@dataclass(frozen=True)
class ContractSpec:
    """선물 계약 사양 — PnL·슬리피지 USD 환산 계수.

    tick: 최소 호가단위(USD/단위). multiplier: 1계약 명목 배수.
    예) WTI(CL): tick 0.01/배럴, multiplier 1000배럴.
    """

    tick: float
    multiplier: float


# WTI(CL) 기본 spec — 미지정 시 사용(하위호환: 기존 호출·테스트 무변경).
WTI_SPEC = ContractSpec(tick=0.01, multiplier=1000.0)
```

- [ ] **Step 4: backtest.py — run_backtest 시그니처에 spec 추가**

`run_backtest` 시그니처(line 132-140)의 `light: bool = False,` 다음 줄에 파라미터 추가 → 아래처럼:

```python
def run_backtest(
    df: pd.DataFrame,
    signals: list[Signal],
    horizon_days: int,
    cost: CostModel = CostModel(),
    exits: ExitRules = ExitRules(),
    roll: RollModel = RollModel(),
    light: bool = False,
    spec: ContractSpec = WTI_SPEC,
) -> BacktestResult:
```

- [ ] **Step 5: backtest.py — 본문 8개 상수 사용처 치환**

아래 7곳을 정확히 치환(`WTI_TICK`→`spec.tick`, `WTI_MULTIPLIER`→`spec.multiplier`):

- line 162: `slip = cost.slippage_ticks * WTI_TICK` → `slip = cost.slippage_ticks * spec.tick`
- line 245: `mfe_usd = max(0.0, mfe_price) * WTI_MULTIPLIER` → `* spec.multiplier`
- line 246: `mae_usd = min(0.0, mae_price) * WTI_MULTIPLIER` → `* spec.multiplier`
- line 256: `gross = sign * (exit_price - entry_price) * WTI_MULTIPLIER` → `* spec.multiplier`
- line 258: `net = net_price_diff * WTI_MULTIPLIER - 2 * cost.commission_per_contract` → `net = net_price_diff * spec.multiplier - 2 * cost.commission_per_contract`
- line 270: `notional = entry_price * WTI_MULTIPLIER` → `* spec.multiplier`
- line 275: `txn = 2 * cost.commission_per_contract + 2 * slip * WTI_MULTIPLIER` → `... + 2 * slip * spec.multiplier`

그리고 `_compute_portfolio_mtm`은 spec을 받지 않으므로(light 모드 grid는 호출 안 함, full 모드만), 시그니처에 spec 추가:

line 325-329 `_compute_portfolio_mtm(df, trades, cost)` 정의를 아래로:

```python
def _compute_portfolio_mtm(
    df: pd.DataFrame,
    trades: list[Trade],
    cost: CostModel,
    spec: ContractSpec = WTI_SPEC,
) -> tuple[pd.Series, float]:
```

line 361: `unrealized += sign * (close - t.entry_price) * WTI_MULTIPLIER` → `* spec.multiplier`

그리고 `run_backtest` 안에서 `_compute_portfolio_mtm` 호출(line 315)에 spec 전달:

```python
        portfolio_curve, portfolio_mdd = _compute_portfolio_mtm(df, trades, cost, spec)
```

- [ ] **Step 6: optimizer.py — grid_search·walk_forward에 spec 관통**

`optimizer.py` line 14 import 교체:

```python
from .backtest import ContractSpec, CostModel, WTI_SPEC, run_backtest
```

`grid_search` 시그니처(line 29-36)의 `light: bool = False,` 다음에 `spec` 추가:

```python
def grid_search(
    df: pd.DataFrame,
    short_thresholds: Iterable[float],
    long_thresholds: Iterable[float],
    horizons: Iterable[int],
    cost: CostModel = CostModel(),
    light: bool = False,
    spec: ContractSpec = WTI_SPEC,
) -> list[GridCell]:
```

`grid_search` 본문의 두 `run_backtest` 호출(line 49, 55)에 `spec=spec` 추가:

```python
            bt = run_backtest(df, sigs, horizon_days=int(h), cost=cost, light=light, spec=spec)
```
(short·long 루프 양쪽 동일하게)

`walk_forward` 시그니처(line 99-107)의 `require_min_trades: int = 5,` 다음에 `spec` 추가:

```python
def walk_forward(
    df: pd.DataFrame,
    short_thresholds: Iterable[float],
    long_thresholds: Iterable[float],
    horizons: Iterable[int],
    split_date: pd.Timestamp,
    cost: CostModel = CostModel(),
    require_min_trades: int = 5,
    spec: ContractSpec = WTI_SPEC,
) -> WalkForwardResult:
```

`walk_forward` 본문: `grid_search` 호출(line 121)에 `spec=spec` 추가, OOS `run_backtest` 호출(line 137)에 `spec=spec` 추가:

```python
    cells = grid_search(df_train, short_thresholds, long_thresholds, horizons, cost, spec=spec)
    ...
    bt_test = run_backtest(df_test, sigs_test, best.horizon_days, cost, spec=spec)
```

- [ ] **Step 7: __init__.py — export 교체**

`__init__.py` line 21-31 의 backtest import 블록에서 `WTI_TICK,`·`WTI_MULTIPLIER,` 두 줄을 `ContractSpec,`·`WTI_SPEC,`로 교체:

```python
from .backtest import (
    Trade,
    BacktestResult,
    ContractSpec,
    CostModel,
    ExitRules,
    RollModel,
    WTI_SPEC,
    run_backtest,
    wti_expiry_dates,
)
```

line 45 `__all__`의 `"WTI_TICK", "WTI_MULTIPLIER",`를 `"ContractSpec", "WTI_SPEC",`로 교체:

```python
    "Trade", "BacktestResult", "ContractSpec", "CostModel", "ExitRules", "RollModel",
    "WTI_SPEC", "run_backtest", "wti_expiry_dates",
```

- [ ] **Step 8: 전체 core 테스트 통과 확인 (회귀 포함)**

Run: `cd platform/core && python -m pytest tests/test_oil_futures.py -v`
Expected: 기존 20개 + 신규 2개 = **22 passed**. 특히 `test_short_pnl_sign_and_magnitude`(× 1000 그대로)·`test_cost_model_reduces_net_pnl`($25 그대로)가 통과해야 함(하위호환 증명).

- [ ] **Step 9: 커밋**

```bash
cd platform && git add core/quant_core/oil_futures/backtest.py core/quant_core/oil_futures/optimizer.py core/quant_core/oil_futures/__init__.py core/tests/test_oil_futures.py
git commit -m "feat(core): WTI 계약상수를 ContractSpec 파라미터로 추출 (엔진 하위호환)"
```

---

## Task 2: data_fetcher — USD 실선물 3종 수집 등록

**Files:**
- Modify: `core/quant_core/data_fetcher.py`
- Test: `core/tests/test_oil_futures_data.py` (신규 테스트 함수 추가) — 기존 파일 사용

목표: `NQ=F`(나스닥100)·`SI=F`(은)·`BTC=F`(비트코인 CME)를 `YFINANCE_SYMBOLS`에 추가해 일배치가 자동 수집·캐시하게 한다. 기존 ETF/현물 프록시 키(`나스닥100선물`/`은선물`/`비트코인`)와 충돌하지 않는 새 키 사용.

- [ ] **Step 1: 새 키가 ASSET_SYMBOLS에 들어가는지 테스트 작성**

`core/tests/test_oil_futures_data.py` 파일 끝에 추가(파일 상단 import에 이미 sys.path 설정 있다고 가정; 없으면 `from quant_core import data_fetcher`):

```python
def test_new_usd_futures_registered_without_collision():
    from quant_core import data_fetcher as dfetch
    # 신규 USD 실선물 키
    for key, sym in [("나스닥선물", "NQ=F"), ("은선물(COMEX)", "SI=F"),
                     ("비트코인선물", "BTC=F")]:
        assert dfetch.YFINANCE_SYMBOLS.get(key) == sym
        assert key in dfetch.ASSET_SYMBOLS
        assert dfetch.SYMBOL_CATEGORY.get(key) == "자산"
    # 기존 프록시 키는 그대로 보존(충돌 없음)
    assert dfetch.FDR_SYMBOLS["나스닥100선물"] == "304940"
    assert dfetch.FDR_SYMBOLS["은선물"] == "144600"
```

- [ ] **Step 2: 실패 확인**

Run: `cd platform/core && python -m pytest tests/test_oil_futures_data.py -k new_usd_futures -v`
Expected: FAIL (키 없음).

- [ ] **Step 3: YFINANCE_SYMBOLS에 3종 추가**

`data_fetcher.py` line 48-53 `YFINANCE_SYMBOLS` 딕셔너리를 아래로 교체:

```python
YFINANCE_SYMBOLS = {
    "S&P500":       "^GSPC",
    "원유선물":      "CL=F",
    "천연가스선물":  "NG=F",
    "금선물":        "GC=F",
    # USD 실선물 — 선물 분석 대시보드용(기존 KRW ETF/현물 프록시와 별도 키).
    "나스닥선물":     "NQ=F",
    "은선물(COMEX)":  "SI=F",
    "비트코인선물":   "BTC=F",
}
```

- [ ] **Step 4: SYMBOL_CATEGORY에 3종 "자산" 등록**

`data_fetcher.py` line 130-132 의 "자산" 블록을 아래로 교체(`구리선물`·`비트코인` 줄 다음에 신규 3종 추가):

```python
    "S&P500": "자산", "원유선물": "자산", "천연가스선물": "자산", "금선물": "자산",
    "코스피200선물": "자산", "나스닥100선물": "자산", "은선물": "자산",
    "구리선물": "자산", "비트코인": "자산",
    "나스닥선물": "자산", "은선물(COMEX)": "자산", "비트코인선물": "자산",
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `cd platform/core && python -m pytest tests/test_oil_futures_data.py -k new_usd_futures -v`
Expected: PASS.

- [ ] **Step 6: 커밋**

```bash
cd platform && git add core/quant_core/data_fetcher.py core/tests/test_oil_futures_data.py
git commit -m "feat(data): NQ=F·SI=F·BTC=F USD 실선물 일배치 수집 등록"
```

> 참고(검증 불가 항목): 실제 yfinance 수집 가용성·과거 범위는 네트워크 의존이라 Task 9 통합 검증에서 1회 확인한다. 단위테스트는 등록만 검증.

---

## Task 3: server — futures_config 레지스트리 (신규)

**Files:**
- Create: `server/app/futures_config.py`
- Test: `server/tests/test_futures_config.py` (신규)

목표: 종목별 차이(데이터키·ContractSpec·임계범위·라벨·매크로)를 단일 레지스트리로 정의.

- [ ] **Step 1: 레지스트리 검증 테스트 작성**

`server/tests/test_futures_config.py` 생성:

```python
"""futures_config.INSTRUMENTS 레지스트리 무결성 — 6종 모두 유효한 설정."""
from __future__ import annotations

import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app.futures_config import INSTRUMENTS


def test_six_instruments_registered():
    assert set(INSTRUMENTS) == {"oil", "nasdaq", "natgas", "gold", "silver", "bitcoin"}


def test_every_config_is_valid():
    for sym, cfg in INSTRUMENTS.items():
        assert cfg.symbol == sym
        assert cfg.data_key and cfg.name and cfg.unit and cfg.eyebrow and cfg.source
        assert cfg.spec.tick > 0 and cfg.spec.multiplier > 0
        # 임계 범위가 1개 이상 값을 만든다
        shorts = cfg.shorts.values()
        longs = cfg.longs.values()
        assert len(shorts) >= 2 and len(longs) >= 2
        # 단조 증가·중복 없음
        assert shorts == sorted(set(shorts))
        assert longs == sorted(set(longs))


def test_oil_matches_legacy_defaults():
    """원유는 기존 $1 grid(80~150, 10~60)와 동일해야 회귀가 없다."""
    oil = INSTRUMENTS["oil"]
    assert oil.shorts.values()[0] == 80 and oil.shorts.values()[-1] == 150
    assert oil.longs.values()[0] == 10 and oil.longs.values()[-1] == 60
    assert len(oil.shorts.values()) == 71 and len(oil.longs.values()) == 51
    assert oil.spec.tick == 0.01 and oil.spec.multiplier == 1000
```

- [ ] **Step 2: 실패 확인**

Run: `cd platform/server && python -m pytest tests/test_futures_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.futures_config'`.

- [ ] **Step 3: futures_config.py 작성**

`server/app/futures_config.py` 생성:

```python
"""선물 분석 대시보드 — 종목별 설정 레지스트리.

종목 차이(데이터키·계약사양·임계범위·라벨·매크로)를 한 곳에 모은다.
라우터(routers/futures.py)는 symbol → INSTRUMENTS[symbol] 조회만 한다.
"종목 추가 = 여기 1줄"이 성립.
"""
from __future__ import annotations

from dataclasses import dataclass

from quant_core.oil_futures import ContractSpec


@dataclass(frozen=True)
class ThresholdRange:
    """히트맵 임계 그리드 [lo, hi] 를 step 간격으로. 부동소수 누적오차 방지."""

    lo: float
    hi: float
    step: float

    def values(self) -> list[float]:
        count = int(round((self.hi - self.lo) / self.step))
        return [round(self.lo + i * self.step, 10) for i in range(count + 1)]


@dataclass(frozen=True)
class InstrumentConfig:
    symbol: str          # 라우트 키 "oil"
    name: str            # 표시명 "원유 (WTI)"
    data_key: str        # data_fetcher 캐시 키 "원유선물"
    source: str          # latest-price source 라벨 "yahoo-cl=f"
    spec: ContractSpec   # tick/multiplier
    shorts: ThresholdRange   # 상단 임계(위로 첫 터치=매도)
    longs: ThresholdRange    # 하단 임계(아래로 첫 터치=매수)
    unit: str            # 단위 라벨 "배럴"
    eyebrow: str         # "CRUDE OIL · NYMEX"
    roll_note: str       # 롤/콘탱고 짧은 설명


# 계약명세: CME/NYMEX/COMEX 표준. 임계범위·step: 최근 다년 거래범위 기준 기본값(튜닝 가능).
INSTRUMENTS: dict[str, InstrumentConfig] = {
    "oil": InstrumentConfig(
        symbol="oil", name="원유 (WTI)", data_key="원유선물", source="yahoo-cl=f",
        spec=ContractSpec(tick=0.01, multiplier=1000),
        shorts=ThresholdRange(80, 150, 1), longs=ThresholdRange(10, 60, 1),
        unit="배럴", eyebrow="CRUDE OIL · NYMEX",
        roll_note="실물 인수도 → 만기마다 강제 롤오버",
    ),
    "nasdaq": InstrumentConfig(
        symbol="nasdaq", name="나스닥100 (NQ)", data_key="나스닥선물", source="yahoo-nq=f",
        spec=ContractSpec(tick=0.25, multiplier=20),
        shorts=ThresholdRange(16000, 24000, 250), longs=ThresholdRange(8000, 16000, 250),
        unit="지수", eyebrow="NASDAQ-100 · CME",
        roll_note="분기 만기 금융선물(현금정산)",
    ),
    "natgas": InstrumentConfig(
        symbol="natgas", name="천연가스 (NG)", data_key="천연가스선물", source="yahoo-ng=f",
        spec=ContractSpec(tick=0.001, multiplier=10000),
        shorts=ThresholdRange(4.0, 10.0, 0.10), longs=ThresholdRange(1.5, 4.0, 0.10),
        unit="MMBtu", eyebrow="NATURAL GAS · NYMEX",
        roll_note="계절성으로 롤 변동 큼",
    ),
    "gold": InstrumentConfig(
        symbol="gold", name="금 (GC)", data_key="금선물", source="yahoo-gc=f",
        spec=ContractSpec(tick=0.10, multiplier=100),
        shorts=ThresholdRange(2400, 3600, 25), longs=ThresholdRange(1600, 2400, 25),
        unit="온스", eyebrow="GOLD · COMEX",
        roll_note="일반적으로 콘탱고",
    ),
    "silver": InstrumentConfig(
        symbol="silver", name="은 (SI)", data_key="은선물(COMEX)", source="yahoo-si=f",
        spec=ContractSpec(tick=0.005, multiplier=5000),
        shorts=ThresholdRange(26, 40, 0.5), longs=ThresholdRange(12, 26, 0.5),
        unit="온스", eyebrow="SILVER · COMEX",
        roll_note="일반적으로 콘탱고",
    ),
    "bitcoin": InstrumentConfig(
        symbol="bitcoin", name="비트코인 (BTC)", data_key="비트코인선물", source="yahoo-btc=f",
        spec=ContractSpec(tick=5, multiplier=5),
        shorts=ThresholdRange(70000, 120000, 2500), longs=ThresholdRange(15000, 70000, 2500),
        unit="BTC", eyebrow="BITCOIN · CME",
        roll_note="CME 현금정산 선물",
    ),
}
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `cd platform/server && python -m pytest tests/test_futures_config.py -v`
Expected: 3 passed. (oil shorts=71개·longs=51개 확인 — 기존 $1 grid와 동일)

- [ ] **Step 5: 커밋**

```bash
cd platform && git add server/app/futures_config.py server/tests/test_futures_config.py
git commit -m "feat(server): futures_config INSTRUMENTS 레지스트리 (6종 USD 선물)"
```

---

## Task 4: server — 라우터 일반화 (`/futures/{symbol}/*`)

**Files:**
- Rename: `server/app/routers/oil_futures.py` → `server/app/routers/futures.py` (`git mv`)
- Modify (rename 후): `server/app/routers/futures.py`

목표: 단일 종목 하드코딩(`원유선물`·`/oil-futures`·VIX/DXY·source)을 `symbol` 파라미터 + config 조회로 일반화. 응답모델·매핑 본문은 유지(시그니처·데이터원만 변경).

- [ ] **Step 1: 파일 rename (히스토리 보존)**

```bash
cd platform && git mv server/app/routers/oil_futures.py server/app/routers/futures.py
```

- [ ] **Step 2: import·prefix·config 헬퍼 교체**

`futures.py` line 30-51 (import 블록 ~ router 정의)을 아래로 교체:

```python
from quant_core.oil_futures import (
    CostModel,
    ExitRules,
    RollModel,
    generate_signals,
    grid_search,
    prepare_wti,
    run_backtest,
    summarize,
    walk_forward,
)

from ..data_cache import get_raw_dataset, get_version
from ..deps import get_current_user
from ..futures_config import INSTRUMENTS, InstrumentConfig

_log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/futures",
    tags=["futures"],
    dependencies=[Depends(get_current_user)],
)


def _get_cfg(symbol: str) -> InstrumentConfig:
    cfg = INSTRUMENTS.get(symbol)
    if cfg is None:
        raise HTTPException(404, f"지원하지 않는 종목: {symbol}")
    return cfg


# Horizon (영업일 보유기간): 단기~장기 비교. 365 ≈ 1.5 캘린더 년.
DEFAULT_HORIZONS = [20, 40, 60, 120, 180, 240, 365]
```

> 삭제: 기존 `DEFAULT_SHORTS`/`DEFAULT_LONGS`/`GRID_SHORTS`/`GRID_LONGS` 상수(이제 config가 종목별로 보유). `DEFAULT_HORIZONS`만 유지(전 종목 공통).

- [ ] **Step 3: 데이터 캐시·_df(symbol) 일반화**

기존 `_WTI_CACHE`·`_df()` (line 69-85)를 아래로 교체:

```python
# 종목별 정제 df 캐시 — (symbol → {version, df}). 데이터 버전 변경 시 자동 갱신.
_DF_CACHE: dict[str, dict] = {}


def _df(symbol: str) -> pd.DataFrame:
    """종목의 캐시 시리즈를 정제해 반환. 데이터 버전 변경 시 자동 갱신.
    미수집/전구간 탈락이면 503 (version 미갱신 — 빈 프레임을 캐시로 굳히지 않음)."""
    cfg = _get_cfg(symbol)
    v = get_version()
    slot = _DF_CACHE.get(symbol)
    if slot is None or slot.get("version") != v:
        ds = get_raw_dataset()
        raw = ds.get(cfg.data_key)
        df = prepare_wti(raw) if raw is not None and not raw.empty else None
        if df is None or df.empty:
            raise HTTPException(status_code=503,
                                detail=f"{cfg.name} 데이터 미수집 — 데이터 수집 후 이용 가능")
        _DF_CACHE[symbol] = {"version": v, "df": df}
    return _DF_CACHE[symbol]["df"]
```

- [ ] **Step 4: DataInfo 모델에 라벨 메타 추가 + instruments 모델**

`DataInfo` 모델(line 116-122)을 아래로 교체(필드 추가):

```python
class DataInfo(BaseModel):
    n_rows: int
    start_date: str
    end_date: str
    price_min: float
    price_max: float
    # 종목 라벨 메타(web 단일화 — config 단일 소스)
    name: str
    eyebrow: str
    unit: str
    roll_note: str


class InstrumentInfo(BaseModel):
    symbol: str
    name: str
```

- [ ] **Step 5: instruments 엔드포인트 추가 + latest-price/data-info 일반화**

`latest_price` 엔드포인트(line 99-111)를 아래로 교체(경로에 {symbol}, source는 cfg):

```python
@router.get("/instruments", response_model=list[InstrumentInfo])
def instruments():
    """선택기용 종목 목록."""
    return [InstrumentInfo(symbol=c.symbol, name=c.name) for c in INSTRUMENTS.values()]


@router.get("/{symbol}/latest-price", response_model=LatestPrice)
def latest_price(symbol: str):
    cfg = _get_cfg(symbol)
    df = _df(symbol)
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else last
    return LatestPrice(
        price=float(last["close"]),
        change=float(last["close"] - prev["close"]),
        change_pct=float(last["close"] / prev["close"] - 1) if prev["close"] else None,
        source=cfg.source, delayed=True,
        fetched_at=pd.Timestamp.utcnow().isoformat(),
    )
```

`data_info` 엔드포인트(line 249-258)를 아래로 교체:

```python
@router.get("/{symbol}/data-info", response_model=DataInfo)
def data_info(symbol: str):
    cfg = _get_cfg(symbol)
    df = _df(symbol)
    return DataInfo(
        n_rows=len(df),
        start_date=str(df["date"].iloc[0].date()),
        end_date=str(df["date"].iloc[-1].date()),
        price_min=float(df["close"].min()),
        price_max=float(df["close"].max()),
        name=cfg.name, eyebrow=cfg.eyebrow, unit=cfg.unit, roll_note=cfg.roll_note,
    )
```

- [ ] **Step 6: prices 엔드포인트 일반화**

`prices`(line 261-276) 시그니처·_df 호출 교체:

```python
@router.get("/{symbol}/prices", response_model=list[PricePoint])
def prices(symbol: str, start: Optional[str] = None, end: Optional[str] = None):
    df = _df(symbol)
```
(본문 나머지 동일)

- [ ] **Step 7: grid 캐시·엔드포인트·워머 일반화**

`_ensure_grid_cached`(line 285-328)를 아래로 교체(symbol·spec 추가):

```python
_GRID_CACHE: dict[tuple, list[GridCellOut]] = {}   # 키: (symbol, version, s, l, h, comm, slip)
_grid_lock = threading.Lock()


def _ensure_grid_cached(symbol: str, s: tuple, l: tuple, h: tuple,
                        commission: float, slippage_ticks: int) -> list[GridCellOut]:
    """(symbol, 데이터버전, 파라미터) 결과 캐시. 워머·요청 공용. 미수집이면 _df가 503."""
    cfg = _get_cfg(symbol)
    df = _df(symbol)
    version = get_version()
    key = (symbol, version, s, l, h, commission, slippage_ticks)
    cached = _GRID_CACHE.get(key)
    if cached is not None:
        return cached
    with _grid_lock:
        cached = _GRID_CACHE.get(key)
        if cached is not None:
            return cached
        cells = grid_search(df, s, l, h, CostModel(commission, slippage_ticks),
                            light=True, spec=cfg.spec)
        out = [
            GridCellOut(
                side=c.side.value,
                threshold=c.threshold,
                horizon=c.horizon_days,
                n_trades=c.summary.n_trades,
                win_rate=c.summary.win_rate,
                avg_return=c.summary.avg_return,
                sharpe=c.summary.sharpe_annualized,
                mdd_usd=c.summary.max_drawdown_usd,
                gross_profit_usd=c.summary.gross_profit_usd,
                gross_loss_usd=c.summary.gross_loss_usd,
                net_pnl_usd=c.summary.total_net_pnl_usd,
                profit_factor=_pf(c.summary.profit_factor),
                low_sample=c.summary.low_sample,
            )
            for c in cells
        ]
        del cells
        gc.collect()
        if len(_GRID_CACHE) >= 12:   # 6종 × 2변형 여유
            _GRID_CACHE.pop(next(iter(_GRID_CACHE)))
        _GRID_CACHE[key] = out
        return out
```

`grid` 엔드포인트(line 331-342)를 아래로 교체(config 기본값):

```python
@router.get("/{symbol}/grid", response_model=list[GridCellOut])
def grid(symbol: str, shorts: str = "", longs: str = "", horizons: str = "",
         commission: float = 2.5, slippage_ticks: int = 1):
    """(side, threshold, horizon) 조합 백테스트. 미지정 시 종목 기본 grid(캐시·워머)."""
    cfg = _get_cfg(symbol)
    s = tuple(_parse_csv_floats(shorts) or cfg.shorts.values())
    l = tuple(_parse_csv_floats(longs) or cfg.longs.values())
    h = tuple(_parse_csv_ints(horizons) or DEFAULT_HORIZONS)
    return _ensure_grid_cached(symbol, s, l, h, commission, slippage_ticks)
```

`_warmer_loop`·`start_grid_warmer`(line 345-363)를 아래로 교체(6종 순회):

```python
def _warmer_loop() -> None:
    """전 종목 기본 grid를 백그라운드로 미리 계산·캐시. 데이터 버전 변경 시 재워밍.
    데이터 미준비(503)면 자주 재시도, 워밍 후엔 느슨히 재확인."""
    first = True
    while True:
        pending = False
        for cfg in INSTRUMENTS.values():
            try:
                _ensure_grid_cached(cfg.symbol, tuple(cfg.shorts.values()),
                                    tuple(cfg.longs.values()), tuple(DEFAULT_HORIZONS), 2.5, 1)
            except HTTPException:
                pending = True   # 이 종목 데이터 미수집 — 다음 틱 재시도
            except Exception:
                _log.warning("선물 grid 워밍 실패 [%s] — 온디맨드로 계산됨",
                             cfg.symbol, exc_info=True)
        if not pending:
            first = False
        time.sleep(20 if first else 300)


def start_grid_warmer() -> None:
    threading.Thread(target=_warmer_loop, daemon=True, name="futures-grid-warm").start()
```

- [ ] **Step 8: signals·backtest·walkforward 엔드포인트 일반화 (symbol + spec)**

`signals`(line 366) 시그니처: `def signals(symbol: str, type: Literal["short", "long"], threshold: float, since: Optional[str] = None):` + 경로 `@router.get("/{symbol}/signals", ...)` + 본문 `df = _df(symbol)`.

`WalkForwardRequest`(line 473-479): `shorts`/`longs` 기본값이 삭제된 `DEFAULT_SHORTS`/`DEFAULT_LONGS`를 참조하므로 **기본값을 Optional로** 변경:

```python
class WalkForwardRequest(BaseModel):
    shorts: Optional[list[float]] = None
    longs: Optional[list[float]] = None
    horizons: list[int] = DEFAULT_HORIZONS
    split_date: str
    commission: float = 2.5
    slippage_ticks: int = 1
```

`backtest`(line 404-417) 경로·시그니처·spec:

```python
@router.post("/{symbol}/backtest", response_model=BacktestResponse)
def backtest(symbol: str, req: BacktestRequest):
    cfg = _get_cfg(symbol)
    df = _df(symbol)
    short_th = [req.threshold] if req.side == "short" else []
    long_th = [req.threshold] if req.side == "long" else []
    sigs = generate_signals(df, short_thresholds=short_th, long_thresholds=long_th)
    if not sigs:
        raise HTTPException(404, "신호가 발생하지 않음 — 임계값/타입 확인")
    res = run_backtest(
        df, sigs, req.horizon_days,
        CostModel(req.commission, req.slippage_ticks),
        ExitRules(req.stop_loss_pct, req.take_profit_pct),
        RollModel(roll_cost_pct=req.roll_cost_pct),
        spec=cfg.spec,
    )
```
(이후 `s = summarize(res)` ~ 응답 매핑 본문은 변경 없음)

`walkforward_endpoint`(line 482-496) 경로·시그니처·config 기본값·spec:

```python
@router.post("/{symbol}/walkforward", response_model=WalkForwardResponse)
def walkforward_endpoint(symbol: str, req: WalkForwardRequest):
    cfg = _get_cfg(symbol)
    df = _df(symbol)
    shorts = req.shorts if req.shorts is not None else cfg.shorts.values()
    longs = req.longs if req.longs is not None else cfg.longs.values()
    try:
        res = walk_forward(
            df, shorts, longs, req.horizons,
            pd.Timestamp(req.split_date),
            CostModel(req.commission, req.slippage_ticks),
            spec=cfg.spec,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
```
(이후 응답 매핑 본문 변경 없음)

`seasonality`(line 570) 경로·시그니처: `@router.get("/{symbol}/seasonality", ...)` + `def seasonality(symbol: str):` + `df = _df(symbol).copy()`.

- [ ] **Step 9: macro-context 일반화 (필드명 generic + 종목명 라벨)**

`MacroRegimeCell`(line 629-633) 필드명 `wti_*` → generic:

```python
class MacroRegimeCell(BaseModel):
    bucket: str
    n_days: int
    avg_return: float
    win_rate: float
```

`macro_context`(line 649-725) 경로·시그니처·라벨 일반화. line 649-657 교체:

```python
@router.get("/{symbol}/macro-context", response_model=MacroResponse)
def macro_context(symbol: str):
    """종목 일간 수익률과 VIX·DXY 관계 — 외생 변수 신호 가치. (전 종목 공통 VIX+DXY)"""
    cfg = _get_cfg(symbol)
    asset = _df(symbol).copy()
    macro = _macro_df()
```
이후 본문에서 `wti`→`asset`로 변수명 일괄 치환(line 657·665·666), correlations의 `pair` 라벨 `"WTI vs VIX"`→`f"{cfg.name} vs VIX"` 등 4개를 cfg.name으로, `MacroRegimeCell(... wti_avg_return=...)`→`avg_return=...`·`wti_win_rate=...`→`win_rate=...` (vix_regime·dxy_regime 양쪽).

구체: line 676-681 correlations:

```python
    correlations = [
        MacroCorrelation(pair=f"{cfg.name} vs VIX", pearson=float(merged["ret"].corr(merged["vix_close"].pct_change()))),
        MacroCorrelation(pair=f"{cfg.name} vs DXY", pearson=float(merged["ret"].corr(merged["dxy_close"].pct_change()))),
        MacroCorrelation(pair=f"{cfg.name} vs VIX(level)", pearson=float(merged["ret"].corr(merged["vix_close"]))),
        MacroCorrelation(pair=f"{cfg.name} vs DXY(level)", pearson=float(merged["ret"].corr(merged["dxy_close"]))),
    ]
```

vix_regime·dxy_regime 의 `MacroRegimeCell(...)` 두 블록(line 692-699, 710-717)에서 `wti_avg_return=`→`avg_return=`, `wti_win_rate=`→`win_rate=`로 치환.

- [ ] **Step 10: import 정리 확인 (smoke)**

Run: `cd platform/server && python -c "from app.routers import futures; print(sorted(r.path for r in futures.router.routes))"`
Expected: `/futures/instruments`, `/futures/{symbol}/backtest`, `/futures/{symbol}/data-info`, `/futures/{symbol}/grid`, `/futures/{symbol}/latest-price`, `/futures/{symbol}/macro-context`, `/futures/{symbol}/prices`, `/futures/{symbol}/seasonality`, `/futures/{symbol}/signals`, `/futures/{symbol}/walkforward` 출력(에러 없음).

- [ ] **Step 11: 커밋**

```bash
cd platform && git add server/app/routers/futures.py
git commit -m "feat(server): 라우터를 /futures/{symbol}/* 로 일반화 (config 구동)"
```

---

## Task 5: server — main.py 라우터 교체 + 인증 테스트 재작성

**Files:**
- Modify: `server/app/main.py:23-30, 437-438, 579`
- Rename+Modify: `server/tests/test_oil_futures_auth.py` → `server/tests/test_futures_auth.py`

- [ ] **Step 1: 인증·404 테스트 재작성 (rename 후)**

```bash
cd platform && git mv server/tests/test_oil_futures_auth.py server/tests/test_futures_auth.py
```

`server/tests/test_futures_auth.py` 전체를 아래로 교체:

```python
"""선물 라우터 인증 게이트 + 미지원 종목 회귀 — /futures/*는 로그인(JWT) 전용.

토큰 없으면 401(데이터 접근 전 차단). app.main lifespan/DB 불요.
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app.routers import futures


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(futures.router)
    return TestClient(app)


def test_get_endpoints_require_auth():
    client = _client()
    for path in ["/futures/instruments",
                 "/futures/oil/data-info", "/futures/oil/latest-price",
                 "/futures/oil/prices", "/futures/oil/grid", "/futures/gold/grid",
                 "/futures/oil/signals", "/futures/oil/seasonality",
                 "/futures/oil/macro-context"]:
        assert client.get(path).status_code == 401, f"{path} should require auth"


def test_post_endpoints_require_auth():
    client = _client()
    for path in ["/futures/oil/backtest", "/futures/nasdaq/walkforward"]:
        assert client.post(path, json={}).status_code == 401, f"{path} should require auth"
```

> 주: 미지원 symbol(예 `/futures/zzz/grid`)의 404는 인증 게이트가 먼저 401을 내므로 무인증 클라이언트로는 검증 불가. 404는 Task 9 통합(토큰 주입) E2E에서 확인.

- [ ] **Step 2: main.py 라우터 import·등록·워머 로그 교체**

`main.py` line 23-30 import 블록에서 `market, oil_futures, portfolio,` → `market, futures, portfolio,` 로 교체(`oil_futures`를 `futures`로):

```python
from .routers import (admin as admin_router, auth, backtest,
                       calendars as calendars_router, commands,
                       dataset, ir as ir_router, ir_compile as ir_compile_router,
                       market, futures, portfolio,
                       preview as preview_router,
                       screener as screener_router,
                       settings as settings_router, strategies, sync,
                       trading as trading_router)
```

line 437-438 워머 시작:

```python
    _log.info("선물 grid 워머 thread 시작")
    futures.start_grid_warmer()
```

line 579 라우터 등록:

```python
app.include_router(futures.router)
```

- [ ] **Step 3: 인증 테스트 통과 확인**

Run: `cd platform/server && python -m pytest tests/test_futures_auth.py -v`
Expected: 2 passed.

- [ ] **Step 4: main import smoke**

Run: `cd platform/server && python -c "import app.main; print('ok')"`
Expected: `ok` (import 에러 없음 — apscheduler 등 의존성 설치돼 있다고 가정).

- [ ] **Step 5: 커밋**

```bash
cd platform && git add server/app/main.py server/tests/test_futures_auth.py
git commit -m "feat(server): main 라우터 등록·워머 futures로 교체 + 인증 테스트 재작성"
```

---

## Task 6: web — api.ts 일반화 (futuresApi + symbol)

**Files:**
- Modify: `web/src/api.ts:287-502`

목표: `oilApi`(경로 하드코딩)를 `futuresApi`(symbol 인자, `/futures/{symbol}/*`)로. 타입은 그대로 두되 `OilDataInfo`에 라벨 메타 필드 추가, `OilMacroRegimeCell` 필드명 generic화, `OilInstrument` 추가.

- [ ] **Step 1: OilDataInfo에 메타 필드 추가 + OilInstrument 추가**

`api.ts` `OilDataInfo`(line 292-298)를 아래로 교체:

```typescript
export interface OilInstrument {
  symbol: string;
  name: string;
}

export interface OilDataInfo {
  n_rows: number;
  start_date: string;
  end_date: string;
  price_min: number;
  price_max: number;
  name: string;       // "원유 (WTI)"
  eyebrow: string;    // "CRUDE OIL · NYMEX"
  unit: string;       // "배럴"
  roll_note: string;  // 롤/콘탱고 짧은 설명
}
```

- [ ] **Step 2: OilMacroRegimeCell 필드명 generic화**

`OilMacroRegimeCell`(line 410-415)을 아래로 교체:

```typescript
export interface OilMacroRegimeCell {
  bucket: string;
  n_days: number;
  avg_return: number;
  win_rate: number;
}
```

- [ ] **Step 3: oilApi → futuresApi (symbol 인자)**

`api.ts` `export const oilApi = { ... };`(line 442-502) 전체를 아래로 교체:

```typescript
export const futuresApi = {
  instruments: () => req<OilInstrument[]>("/futures/instruments"),
  dataInfo: (sym: string) => req<OilDataInfo>(`/futures/${sym}/data-info`),
  latestPrice: (sym: string) => req<OilLatestPrice>(`/futures/${sym}/latest-price`),
  prices: (sym: string, start?: string, end?: string) => {
    const qs = new URLSearchParams();
    if (start) qs.set("start", start);
    if (end) qs.set("end", end);
    const q = qs.toString();
    return req<OilPricePoint[]>(`/futures/${sym}/prices` + (q ? "?" + q : ""));
  },
  grid: (sym: string, opts: {
    shorts?: number[];
    longs?: number[];
    horizons?: number[];
    commission?: number;
    slippage_ticks?: number;
  } = {}) => {
    const qs = new URLSearchParams();
    if (opts.shorts?.length) qs.set("shorts", opts.shorts.join(","));
    if (opts.longs?.length) qs.set("longs", opts.longs.join(","));
    if (opts.horizons?.length) qs.set("horizons", opts.horizons.join(","));
    if (opts.commission !== undefined) qs.set("commission", String(opts.commission));
    if (opts.slippage_ticks !== undefined)
      qs.set("slippage_ticks", String(opts.slippage_ticks));
    const q = qs.toString();
    return req<OilGridCell[]>(`/futures/${sym}/grid` + (q ? "?" + q : ""));
  },
  signals: (sym: string, type: OilSide, threshold: number, since?: string) => {
    const qs = new URLSearchParams({ type, threshold: String(threshold) });
    if (since) qs.set("since", since);
    return req<OilSignal[]>(`/futures/${sym}/signals?` + qs.toString());
  },
  backtest: (sym: string, body: {
    side: OilSide;
    threshold: number;
    horizon_days: number;
    commission?: number;
    slippage_ticks?: number;
    stop_loss_pct?: number | null;
    take_profit_pct?: number | null;
    roll_cost_pct?: number;
  }) =>
    req<OilBacktest>(`/futures/${sym}/backtest`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  walkforward: (sym: string, body: {
    shorts?: number[];
    longs?: number[];
    horizons?: number[];
    split_date: string;
    commission?: number;
    slippage_ticks?: number;
  }) =>
    req<OilWalkForward>(`/futures/${sym}/walkforward`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  seasonality: (sym: string) => req<OilSeasonality>(`/futures/${sym}/seasonality`),
  macroContext: (sym: string) => req<OilMacroContext>(`/futures/${sym}/macro-context`),
};
```

line 287-288 주석도 갱신:

```typescript
// ─── Futures (멀티 종목 선물) 분석 ───────────────────────────────────
// quant_core.oil_futures 백엔드(/futures/{symbol}/*) 호출 + 응답 타입.
```

- [ ] **Step 4: tsc는 Task 7 후 일괄 — 여기선 커밋만 (api.ts는 page와 함께 깨짐)**

> api.ts만 바꾸면 OilFutures.tsx가 `oilApi` 미정의로 깨진다. Task 7과 한 묶음으로 진행하되, 커밋은 분리한다(여기선 staged만, tsc는 Task 7 Step 마지막에).

```bash
cd platform && git add web/src/api.ts
git commit -m "feat(web): oilApi → futuresApi (symbol 인자, /futures/{symbol}/*)"
```

---

## Task 7: web — FuturesAnalytics 페이지 (선택기 + 라벨 일반화)

**Files:**
- Rename: `web/src/pages/OilFutures.tsx` → `web/src/pages/FuturesAnalytics.tsx` (`git mv`)
- Modify (rename 후): `web/src/pages/FuturesAnalytics.tsx`
- Modify: `web/src/index.css` (선택기 스타일)

목표: 종목 선택기 추가 + 모든 API 호출에 `symbol` 전달 + 하드코딩 라벨을 `info`(data-info) 메타로 치환 + macro 필드명 generic화 + 컴포넌트/export 이름 변경.

- [ ] **Step 1: 파일 rename**

```bash
cd platform && git mv web/src/pages/OilFutures.tsx web/src/pages/FuturesAnalytics.tsx
```

- [ ] **Step 2: import·컴포넌트명·선택기 state**

`FuturesAnalytics.tsx` line 28-37 import를 아래로 교체(`oilApi`→`futuresApi`, `OilInstrument` 추가):

```typescript
import {
  futuresApi,
  type OilBacktest,
  type OilDataInfo,
  type OilGridCell,
  type OilInstrument,
  type OilLatestPrice,
  type OilMacroContext,
  type OilSeasonality,
  type OilWalkForward,
} from "../api";
```

line 79 `export default function OilFutures() {` → `export default function FuturesAnalytics() {`.

line 80(첫 useState 위)에 종목 선택기 state 추가:

```typescript
  const [instruments, setInstruments] = useState<OilInstrument[]>([]);
  const [symbol, setSymbol] = useState<string>("oil");
```

- [ ] **Step 3: instruments 로드 + 데이터 로드를 symbol 의존으로**

기존 초기 로드 useEffect(line 115-134)를 아래 두 useEffect로 교체:

```typescript
  // 종목 목록 1회 로드
  useEffect(() => {
    futuresApi.instruments().then(setInstruments).catch((e) => console.error("instruments", e));
  }, []);

  // 종목 변경 시 전체 재로드
  useEffect(() => {
    setInfo(null);
    setPrice(null);
    setSelected(null);
    setBacktest(null);
    setWf(null);
    futuresApi.dataInfo(symbol).then(setInfo).catch((e) => console.error("data-info", e));
    futuresApi.seasonality(symbol).then(setSeason).catch((e) => console.error("seasonality", e));
    futuresApi.macroContext(symbol).then(setMacro).catch((e) => console.error("macro", e));

    setGridLoading(true);
    setGrid(null);
    setGridError(null);
    futuresApi
      .grid(symbol)
      .then((g) => {
        setGrid(g);
        const trusted = g.filter((c) => !c.low_sample && c.net_pnl_usd > 0)
                         .sort((a, b) => b.net_pnl_usd - a.net_pnl_usd);
        if (trusted.length) setSelected(trusted[0]);
      })
      .catch((e) => setGridError(e.message))
      .finally(() => setGridLoading(false));

    futuresApi.latestPrice(symbol).then(setPrice).catch((e) => console.error("price", e));
  }, [symbol]);
```

- [ ] **Step 4: backtest·walkforward 호출에 symbol 전달**

backtest useEffect(line 140-148) `futuresApi.backtest({...})` → `futuresApi.backtest(symbol, {...})`:

```typescript
    futuresApi
      .backtest(symbol, {
        side: selected.side,
        threshold: selected.threshold,
        horizon_days: selected.horizon,
        stop_loss_pct: sl === "" ? null : sl / 100,
        take_profit_pct: tp === "" ? null : tp / 100,
        roll_cost_pct: rollCost === "" ? 0 : rollCost / 100,
      })
```
그리고 이 useEffect 의존성 배열(line 152) `[selected, sl, tp, rollCost]` → `[symbol, selected, sl, tp, rollCost]`.

`runWalkForward`(line 200-208) `futuresApi.walkforward({...})` → `futuresApi.walkforward(symbol, {...})`:

```typescript
    futuresApi
      .walkforward(symbol, { split_date: splitDate })
```

- [ ] **Step 5: 헤더 라벨을 info 메타로 + 선택기 렌더**

header 블록(line 222-236)을 아래로 교체:

```tsx
      <header className="oil-header">
        <div className="oil-title-row">
          <div>
            <div className="oil-eyebrow">{info?.eyebrow ?? "FUTURES"}</div>
            <h1>{info?.name ?? "선물"} Futures Analytics</h1>
          </div>
          {price && <LivePriceTag price={price} />}
        </div>
        {/* 종목 선택기 */}
        <div className="futures-selector">
          {instruments.map((it) => (
            <button
              key={it.symbol}
              className={it.symbol === symbol ? "active" : ""}
              onClick={() => setSymbol(it.symbol)}
            >
              {it.name}
            </button>
          ))}
        </div>
        <p className="muted">
          장중 high/low가 임계값을 첫 터치하면 신호 → N영업일 보유 백테스트.
        </p>
        <p className="oil-source-note">
          데이터: Yahoo Finance 최근월 선물 · 일배치 갱신 · front-month 롤 점프 포함
        </p>
      </header>
```

- [ ] **Step 6: 단위·통화 라벨 치환**

line 248 `<span className="meta-unit">/배럴</span>` → `<span className="meta-unit">/{info?.unit ?? ""}</span>`.

히트맵 타이틀(line 306-308) — 하드코딩 범위를 grid 파생으로:

```tsx
            title={heatmapSide === "short"
              ? `Short — 위로 첫 터치 (${heatmaps.short.length}개 임계)`
              : `Long — 아래로 첫 터치 (${heatmaps.long.length}개 임계)`}
```

롤오버 help 문구(line 376-384, `<div className="muted roll-help">…</div>`)를 종목 일반 문구로 교체:

```tsx
          <div className="muted roll-help">
            선물은 만기마다 롤오버되며 롤 비용/이익이 발생한다 ({info?.roll_note ?? "—"}).
            <b> 양수 = contango 비용(차감), 음수 = backwardation 이익(가산).</b>
            <span style={{ color: "#c9a227" }}> ⚠️ 추정 가정 — 정확한 롤 yield는 만기물별 데이터 필요.</span>
          </div>
```

(line 372-374 `roll-quick` 버튼들의 콘탱고/backwardation 빠른값은 종목 무관 일반값이라 유지.)

- [ ] **Step 7: macro 섹션 generic 필드명 + 라벨**

`MacroView`(line 649-692)의 WTI 고정 문구(line 652-656)를 일반화 — `WTI` 직접 언급 제거(상관쌍 라벨은 서버가 종목명 제공):

```tsx
      <p className="muted" style={{ marginBottom: 12 }}>
        종목 일간 수익률과 VIX(공포지수)/DXY(달러지수)의 관계 — 외생 변수가 신호 가치에
        주는 영향. 일별 종가 기준, <b>{m.coverage_days.toLocaleString()}</b>일 표본.
      </p>
```

`RegimeTable`(line 694-717)에서 `r.wti_avg_return` → `r.avg_return`, `r.wti_win_rate` → `r.win_rate` (3곳, line 707·708·710):

```tsx
              <td className={r.avg_return >= 0 ? "pos" : "neg"}>
                {(r.avg_return >= 0 ? "+" : "") + (r.avg_return * 100).toFixed(3)}%
              </td>
              <td>{(r.win_rate * 100).toFixed(1)}%</td>
```

섹션 ⑦ 제목(line 496) `MACRO CONTEXT · 외생 변수 (VIX · DXY)`·RegimeTable title(line 681-682 "VIX 체제별 WTI 평균…")의 "WTI"는 종목 무관 "자산"으로 변경(line 681-682):

```tsx
        <RegimeTable title="VIX 체제별 평균 일간 수익률" rows={m.vix_regime} />
        <RegimeTable title="DXY(달러) 체제별 평균 일간 수익률" rows={m.dxy_regime} />
```

- [ ] **Step 8: index.css — 선택기 스타일 추가**

`web/src/index.css` line 2322(`.heat-n { ... }`) 다음에 추가:

```css
/* 종목 선택기 (선물 분석) */
.futures-selector {
  display: flex; flex-wrap: wrap; gap: 6px; margin: 10px 0 6px;
}
.futures-selector button {
  padding: 6px 14px; font-size: 13px; font-weight: 600;
  border: 1px solid var(--border, #2b3340); border-radius: 999px;
  background: var(--bg, #11161d); color: var(--muted, #9aa);
  cursor: pointer; transition: background 0.15s, color 0.15s, border-color 0.15s;
}
.futures-selector button:hover { border-color: var(--accent, #6c9ce9); }
.futures-selector button.active {
  background: var(--accent, #6c9ce9); color: #fff; border-color: var(--accent, #6c9ce9);
}
```

- [ ] **Step 9: 타입체크 (api.ts + page 함께)**

Run: `cd platform/web && npx tsc --noEmit`
Expected: 에러 0. (`oilApi` 잔존 참조·`wti_avg_return` 잔존 참조 있으면 잡힌다 → 수정)

- [ ] **Step 10: 커밋**

```bash
cd platform && git add web/src/pages/FuturesAnalytics.tsx web/src/index.css
git commit -m "feat(web): FuturesAnalytics 페이지 — 종목 선택기 + 라벨 일반화"
```

---

## Task 8: web — 라우트·네비 교체 (/futures, "선물 분석")

**Files:**
- Modify: `web/src/App.tsx:13, 40`
- Modify: `web/src/components/Layout.tsx:11`

- [ ] **Step 1: App.tsx import·라우트 교체**

line 13 `import OilFutures from "./pages/OilFutures";` → `import FuturesAnalytics from "./pages/FuturesAnalytics";`

line 40 `<Route path="/oil-futures" element={<OilFutures />} />` → `<Route path="/futures" element={<FuturesAnalytics />} />`

- [ ] **Step 2: Layout.tsx 네비 항목 교체**

`web/src/components/Layout.tsx` line 11 `{ to: "/oil-futures", label: "원유 분석" },` → `{ to: "/futures", label: "선물 분석" },`

- [ ] **Step 3: 타입체크**

Run: `cd platform/web && npx tsc --noEmit`
Expected: 에러 0.

- [ ] **Step 4: 빌드 확인**

Run: `cd platform/web && npx vite build`
Expected: 빌드 성공(에러 0).

- [ ] **Step 5: 커밋**

```bash
cd platform && git add web/src/App.tsx web/src/components/Layout.tsx
git commit -m "feat(web): 라우트 /oil-futures→/futures, 네비 '선물 분석'"
```

---

## Task 9: 전체 검증 — 단위·풀스택 E2E·최종 리뷰

**Files:** (코드 변경 없음 — 검증·필요 시 미세 수정)

목표: 4계층 통합 동작을 실제 데이터로 확인. 특히 신규 USD 선물 3종 수집 가용성과 6종 히트맵·셀클릭 상세를 브라우저로 검증.

- [ ] **Step 1: core·server 전체 단위테스트**

Run:
```
cd platform/core && python -m pytest tests/test_oil_futures.py tests/test_oil_futures_data.py -v
cd platform/server && python -m pytest tests/test_futures_config.py tests/test_futures_auth.py -v
```
Expected: 전부 PASS (core 22+, data, config 3, auth 2).

- [ ] **Step 2: 웹 타입체크·빌드**

Run: `cd platform/web && npx tsc --noEmit && npx vite build`
Expected: 에러 0.

- [ ] **Step 3: 신규 USD 선물 수집 가용성 확인 (네트워크)**

Run:
```
cd platform/core && python -c "import yfinance as yf; [print(s, len(yf.Ticker(s).history(period='5d'))) for s in ['NQ=F','SI=F','BTC=F']]"
```
Expected: 각 심볼이 5행 내외 OHLCV 반환(>0). 0이면 심볼 가용성 이슈 — 사용자에게 보고(검증 불가 명시).

- [ ] **Step 4: 풀스택 로컬 E2E (서버+웹, 토큰 주입)**

PR #2·#3과 동일 방식:
1. 로컬 서버 기동(`uvicorn app.main:app --port 8011`), 데이터셋이 신규 키 포함하도록 1회 수집되거나 합성 주입.
2. 로그인 토큰을 localStorage 주입, vite(5174)·CORS wildcard.
3. 브라우저(Claude in Chrome)로 `/futures` 접속 → 종목 선택기 6개 렌더 확인.
4. **최소 3개 종목 전환**(oil·gold·bitcoin) 각각: 히트맵 행이 종목별 임계범위로 렌더(`$x (n=y)` 헤더), 단위 라벨(`/배럴`·`/온스`·`/BTC`) 반영, 셀 클릭 → 백테스트 상세(거래·곡선) 표시.
5. 미지원 종목 404: 콘솔/네트워크로 `/futures/zzz/grid` → 404 확인.

Expected: 6종 선택 가능, 전환 시 재로드, 히트맵·상세 정상. 스크린샷으로 증빙.

> 검증 불가 시(수집 안 된 신규 종목): oil/gold/natgas(기수집)로 전환·셀클릭까지 확인하고, nasdaq/silver/bitcoin은 "데이터 준비 중" 표시가 정상 동작함을 확인 + 수집 후 재확인 필요를 명시 보고.

- [ ] **Step 5: 독립 코드리뷰 (최종)**

전체 diff(`git diff 5d5d886..HEAD`)에 대해 superpowers:requesting-code-review 또는 코드리뷰 서브에이전트 1회. 중점: ContractSpec 하위호환·spec 누락 호출 없음·캐시 키 symbol 포함·워머 6종·라벨 단일화·4원칙 위반(PR-1~4) 여부.

- [ ] **Step 6: 검증 요약 커밋(필요 시) + 메모리 갱신**

검증 중 수정이 있었으면 커밋. `project_oilfutures_dashboard.md` 메모리에 "후속 — 멀티 종목 일반화" 섹션 추가(결정·구조·검증 결과).

---

## Self-Review (작성자 점검 결과)

**1. Spec coverage:** 스펙 §2 결정(USD 통일·코스피 제외·단일탭+선택기·route A) → Task 2·3·7·8. §3 데이터키(신규 3종) → Task 2. §4 4계층 → Task 1(core)·3·4·5(server)·6·7·8(web). §4.1 ContractSpec → Task 1. §4.2 InstrumentConfig → Task 3. §4.3 라우터 → Task 4. §4.4 워머 6종 → Task 4 Step 7. §4.5 web 선택기·data-info 라벨 → Task 6·7. §5 config 값 → Task 3. §7 에러처리(404·503) → Task 4 Step 3·5. §8 테스트 → Task 1·3·5·9. 부수효과(자산 유니버스 노출) → Task 2 Step 4(SYMBOL_CATEGORY). 모두 커버.

**2. Placeholder scan:** 코드 스텝은 전부 실제 코드. "unchanged/본문 동일"은 rename(git mv)으로 보존되는 매핑 블록에 한함(델타만 명시) — 추측·미정 없음.

**3. Type consistency:** `ContractSpec(tick, multiplier)`·`WTI_SPEC`·`spec=` 일관(Task 1·3·4). `InstrumentConfig`/`ThresholdRange.values()` 필드명 Task 3↔4↔6 일치. `futuresApi.x(symbol, ...)` 시그니처 Task 6↔7 일치. `OilMacroRegimeCell{avg_return,win_rate}` 서버(Task4 Step9)↔타입(Task6 Step2)↔web(Task7 Step7) 일치. `data_key` "은선물(COMEX)"·"나스닥선물"·"비트코인선물" Task 2↔3 일치.

**알려진 한계(스펙 §9·§10 명시):** `wti_expiry_dates`(롤 만기 스케줄)는 WTI 월물 기준 유지 — 비-oil 종목의 `num_rollovers` 표시는 WTI식 월간 스케줄로 근사(롤 비용 default 0이라 PnL 영향 없음, 표시값만 근사). 종목별 만기 스케줄 정밀화는 범위 밖.
