# 멀티 종목 선물 분석 대시보드 설계

> 기존 WTI 원유 전용 대시보드(`/oil-futures`)를 **종목 무관 일반화 구조**로 바꿔
> 나스닥100·천연가스·금·은·비트코인 선물을 한 화면에서 분석하게 한다.

**작성일:** 2026-06-02
**선행 작업:** `2026-06-01-oil-futures-native-dashboard-design.md`(WTI 네이티브 재구현, PR #2),
PR #3(히트맵 $1 + 행헤더 n + light/캐시/워머 성능 근본해결)

---

## 1. 목표 (Goal)

WTI 대시보드의 분석 엔진·UI를 재사용해, **6개 선물 종목**(원유 포함)을 단일 "선물 분석"
탭에서 종목 선택기로 전환하며 동일하게 분석한다. 종목별 차이는 **단일 config 레지스트리**
한 곳에만 둔다 — "종목 추가 = config 1줄"이 성립하게.

## 2. 핵심 결정 사항 (브레인스토밍 합의)

| 결정 | 내용 | 근거 |
|---|---|---|
| 데이터 정책 | 6종 모두 **실제 USD 선물(yfinance)** 로 통일 | WTI와 통화·롤·계약사양 일관. 프록시/현물 혼재 회피 |
| 코스피200 | **제외**(이번 범위 밖) | yfinance에 안정적 근월물 심볼 없음. 무료 공식 소스 부재(조사 완료). 스크래핑(KRX MDC/네이버/investing)은 비공식·ToS 회색지대 → 일관성·운영리스크 회피. 추후 KRX 정식 라이선스/KIS 경유로 추가 |
| 네비 구조 | **단일 "선물 분석" 탭 + 종목 선택기** | nav 1개, 종목 전환은 페이지 내부. 가장 단순 |
| 라우트 구조 | `/futures/{symbol}/*` (`oil`도 한 symbol). 기존 `/oil-futures` 폐기 | 라우트 의미 정확화. 모노레포 원자 배포로 web+server 동시 전환 → 깨짐 없음. 라우터 6벌 복제는 4원칙 위배라 제외 |
| 엔진 처리 | core 신호/백테스트/지표 코드 **무수정**, `WTI_TICK`/`WTI_MULTIPLIER` 모듈상수만 `ContractSpec` 주입 | 검증된 엔진 회귀 위험 최소화 |

## 3. 종목 세트 & 데이터 소스

| symbol | 표시명 | yfinance | data_fetcher 키 | 수집 상태 |
|---|---|---|---|---|
| `oil` | 원유 (WTI) | CL=F | `원유선물` | 기존 |
| `nasdaq` | 나스닥100 | NQ=F | `나스닥선물` | **신규** |
| `natgas` | 천연가스 | NG=F | `천연가스선물` | 기존 |
| `gold` | 금 | GC=F | `금선물` | 기존 |
| `silver` | 은 | SI=F | `은선물(COMEX)` | **신규** |
| `bitcoin` | 비트코인 | BTC=F | `비트코인선물` | **신규** |

> **신규 키 명명 주의:** 기존 data_fetcher엔 이미 `나스닥100선물`(FDR 304940, KRW ETF)·
> `은선물`(FDR 144600, KRW ETF, 존재 시)·`비트코인`(Binance 현물)이 **전략연구소 자산
> 유니버스**용으로 존재. 신규 USD 실선물은 **충돌하지 않는 별도 키**로 추가하고 기존
> 프록시는 건드리지 않는다. 위 제안 키(`나스닥선물`/`은선물(COMEX)`/`비트코인선물`)는
> 구현 단계에서 `data_fetcher.py` 기존 키 전수 확인 후 최종 확정한다.

## 4. 아키텍처 — 4계층 변경

```
core/quant_core/oil_futures/         (모듈명 유지 — 엔진 동일, 상수만 파라미터화)
  backtest.py    WTI_TICK/WTI_MULTIPLIER 모듈상수 → ContractSpec(tick, multiplier) 주입
                 run_backtest(df, signals, horizon_days, ..., spec: ContractSpec = WTI_SPEC)
                 (8개 사용처 치환; 기본값 WTI_SPEC → 기존 호출·테스트 무변경 = 하위호환)
  optimizer.py   grid_search(..., spec: ContractSpec = WTI_SPEC) 로 spec 관통
  signals.py / metrics.py / data.py   변경 없음 (이미 종목 무관, 검증됨)

server/app/
  futures_config.py (신규)   INSTRUMENTS: dict[str, InstrumentConfig]
  routers/futures.py         (oil_futures.py → 리네임·일반화)
                             prefix "/futures", 경로 "/{symbol}/grid" 등 9개
                             _df(symbol) / _GRID_CACHE 키에 symbol 추가 / 워머가 6종 순회
  main.py                    워머 등록(기존) — 6종 루프로

core/quant_core/data_fetcher.py
  YFINANCE_SYMBOLS 에 NQ=F·SI=F·BTC=F 3줄 추가 (일배치 자동 수집)

web/src/
  pages/FuturesAnalytics.tsx (OilFutures.tsx 일반화) + 종목 선택기(세그먼트/드롭다운)
  api.ts                     oilApi → futuresApi.x({symbol, ...})
  App.tsx / nav              "원유 분석" → "선물 분석" 단일 항목
```

### 4.1 core: `ContractSpec` 추출

```python
# backtest.py
@dataclass(frozen=True)
class ContractSpec:
    """선물 계약 사양 — PnL·슬리피지 USD 환산에 사용."""
    tick: float          # 최소 호가단위 (USD/단위)
    multiplier: float    # 1계약 명목 배수

WTI_SPEC = ContractSpec(tick=0.01, multiplier=1000.0)   # 기존 WTI_TICK/WTI_MULTIPLIER 대체
```

`run_backtest`·`grid_search` 시그니처 끝에 `spec: ContractSpec = WTI_SPEC` 추가.
backtest.py의 8개 `WTI_TICK`/`WTI_MULTIPLIER` 사용처(line 162·245·246·256·258·270·275·361)를
`spec.tick`/`spec.multiplier`로 치환. **결정: 모듈상수 `WTI_TICK`/`WTI_MULTIPLIER`는 제거**하고
`__init__.py` export를 `ContractSpec`·`WTI_SPEC`로 교체(`__all__` 갱신) — dead alias를 남기지
않음(over-engineering 금지). 구현 첫 단계에서 두 상수의 import·참조처를 전수 스캔해 함께 정리.

### 4.2 server: `InstrumentConfig` 레지스트리

```python
# futures_config.py
@dataclass(frozen=True)
class ThresholdRange:
    lo: float
    hi: float
    step: float
    def values(self) -> list[float]: ...   # lo..hi step 간격, 부동소수 누적오차 방지

@dataclass(frozen=True)
class InstrumentConfig:
    symbol: str                 # "oil"
    name: str                   # "원유 (WTI)"
    data_key: str               # data_fetcher 캐시 키 "원유선물"
    spec: ContractSpec          # tick/multiplier
    shorts: ThresholdRange      # 상단 임계(위로 첫 터치 = 매도)
    longs: ThresholdRange       # 하단 임계(아래로 첫 터치 = 매수)
    unit: str                   # "배럴" — UI 단위 라벨
    eyebrow: str                # "CRUDE OIL · NYMEX"
    roll_note: str              # 롤/콘탱고 한 줄 설명
    macro_pairs: tuple[str, ...] = ("VIX", "달러지수")   # 6종 공통 기본값

INSTRUMENTS: dict[str, InstrumentConfig] = { ... }   # 아래 §5 표
```

### 4.3 server: 라우터 일반화

- prefix `"/oil-futures"` → `"/futures"`, 모든 경로에 `/{symbol}` 선행: `/{symbol}/data-info`,
  `/{symbol}/latest-price`, `/{symbol}/prices`, `/{symbol}/grid`, `/{symbol}/signals`,
  `/{symbol}/backtest`, `/{symbol}/walkforward`, `/{symbol}/seasonality`, `/{symbol}/macro-context`.
- `symbol` path param → `INSTRUMENTS[symbol]` 조회. 미지원 symbol → **404**.
- `_df(symbol)`: `get_raw_dataset().get(cfg.data_key)` → `prepare_wti`(범용 정제, 개명 불필요).
- `_GRID_CACHE` 키: `(symbol, get_version(), shorts, longs, horizons, commission, slippage)` —
  symbol 추가. double-checked lock 유지.
- grid 기본값: `cfg.shorts.values()` / `cfg.longs.values()`(종목별 $1 아님).
- walk-forward: 종목별 coarse 범위(shorts/longs를 거칠게 — 구현 시 step×N) + full 모드 유지.
- 매크로: `macro_pairs`로 페어링(기본 VIX+달러지수 공통), 라벨만 종목명 반영.

### 4.4 server: 워머 일반화

`_warmer_loop`이 `for cfg in INSTRUMENTS.values(): _ensure_grid_cached(cfg.symbol, ...)`로
6종 순차 사전계산. 첫 워밍 전 503(데이터 미준비) 20s 재시도 / 성공 후 300s 주기 유지.
6종 합계 ≈ 3,200셀(light) → 콜드 부팅 시 백그라운드 ~1.5분 추정.

### 4.5 web: 일반화 + 선택기

- `OilFutures.tsx` → `FuturesAnalytics.tsx`. 상단 종목 선택기(세그먼트 버튼 6개) 추가.
  선택 종목 state → 모든 API 호출에 `symbol` 전달, 전환 시 재페치.
- **결정: 라벨·단위·eyebrow·roll_note는 서버 `data-info` 응답의 config 메타로 단일화**
  (config는 서버 한 곳에만 존재 → web 측 중복 상수 테이블 두지 않음, DRY). 통화는 6종 모두 `$`(USD) 공통.
- 히트맵 행·열은 **grid 응답에서 파생**(PR #3에서 이미 그렇게 됨) → 종목별 임계범위 자동 반영.
  행헤더 `$x (n=y)` 포맷 유지.
- nav "원유 분석" → "선물 분석", 라우트 `/oil-futures` → `/futures`(기본 종목 oil).
- `api.ts`: `oilApi` 인터페이스는 종목 무관(이미) → `futuresApi`로 개명, 각 메서드에 `symbol` 인자.

## 5. INSTRUMENTS config 초안 값

계약명세는 CME/NYMEX/COMEX 표준. **임계 범위·step은 최근 다년 거래범위 기준 시작 기본값**
(구현 단계 TDD로 공식 계약명세 재확인 + 운영 중 튜닝 가능):

| symbol | tick | mult | shorts(lo–hi·step) | longs(lo–hi·step) | unit | 셀수≈ |
|---|---|---|---|---|---|---|
| oil | 0.01 | 1000 | 80–150 · 1 | 10–60 · 1 | 배럴 | 854 |
| nasdaq | 0.25 | 20 | 16000–24000 · 250 | 8000–16000 · 250 | 지수 | 462 |
| natgas | 0.001 | 10000 | 4.0–10.0 · 0.10 | 1.5–4.0 · 0.10 | MMBtu | 609 |
| gold | 0.10 | 100 | 2400–3600 · 25 | 1600–2400 · 25 | 온스 | 574 |
| silver | 0.005 | 5000 | 26–40 · 0.5 | 12–26 · 0.5 | 온스 | 406 |
| bitcoin | 5 | 5 | 70000–120000 · 2500 | 15000–70000 · 2500 | BTC | 308 |

roll_note 초안: oil="실물 인수도 → 만기마다 강제 롤오버", gold="일반적으로 콘탱고",
natgas="계절성으로 롤 변동 큼", nasdaq="분기 만기 금융선물", silver="일반적으로 콘탱고",
bitcoin="CME 현금정산 선물".

## 6. 데이터 흐름

```
[일배치 수집] data_fetcher → YFINANCE_SYMBOLS(CL=F·NQ=F·NG=F·GC=F·SI=F·BTC=F) → parquet 캐시
        ↓
[서버] get_raw_dataset() → _df(symbol) = prepare_wti(ds[cfg.data_key])
        ↓
[그리드] _ensure_grid_cached(symbol,...) → grid_search(df, cfg.shorts, cfg.longs, horizons,
                                            spec=cfg.spec, light=True) → 버전·symbol 키 캐시
        ↓
[워머] 부팅·버전변경 시 6종 사전계산 → 사용자 항상 캐시히트
        ↓
[웹] futuresApi.grid({symbol}) → 히트맵(종목별 임계범위·$x(n=y)) / 셀클릭 → backtest 상세
```

## 7. 에러 처리

- 미지원 symbol → 404 (라우터 path param 검증).
- 종목 데이터 미수집/빈 데이터(신규 NQ=F/SI=F/BTC=F가 아직 캐시 안 됨) → 503(기존 패턴 재사용),
  워머가 다음 주기에 채움. UI는 "데이터 준비 중" 표시.
- ThresholdRange.values() 부동소수 누적오차 → 정수 스텝 카운트로 생성(`lo + i*step`).

## 8. 테스트 전략

- **core 회귀:** `ContractSpec` 기본 WTI_SPEC → 기존 20개 테스트 **무수정 통과**(하위호환).
- **core 신규:** 비-WTI spec(예: gold tick0.10/mult100)이 $ net_pnl을 multiplier만큼 스케일하고
  수익률%·win_rate·sharpe·MDD%는 spec 불변임을 단위검증(동일 거래·다른 spec 비교).
- **server:** ① INSTRUMENTS 전 종목 ContractSpec 유효·shorts/longs values()>0행 검증,
  ② `/futures/gold/grid`·`/futures/nasdaq/data-info` 200, ③ 미지원 symbol 404,
  ④ 미인증 401(게이트 일반화 회귀), ⑤ 캐시 버전키에 symbol 포함 확인.
- **data:** NQ=F/SI=F/BTC=F 수집이 비어있지 않은 OHLCV 반환(네트워크 의존 → "검증 가능 신호"로
  명시; 합성 픽스처로 단위, 실수집은 통합 확인).
- **web:** tsc, 선택기 종목 전환 시 재페치, 히트맵이 종목별 임계범위 + 행헤더 렌더.
- **풀스택 브라우저 E2E:** 2~3개 종목 전환 → 히트맵 + 셀클릭 백테스트 상세(PR #2·#3 방식).

## 9. 범위 밖 (Out of Scope)

- **코스피200** — 무료 안정 소스 부재. 추후 KRX 정식 라이선스 또는 KIS(로컬앱 경유) 배선 시 별도 추가.
- **기존 KRW ETF/현물 프록시 교체** — 전략연구소 자산 유니버스의 `나스닥100선물`(ETF)·`은선물`(ETF)·
  `비트코인`(현물)을 실선물로 교체/중복정리하는 것은 별도 범위. 이번엔 신규 키로 *추가만*.
- **종목 맞춤 매크로** — 나스닥 `^VXN` 등 종목별 매크로 페어 추가는 YAGNI로 후순위(공통 VIX+달러지수 유지).
- **계약명세 정밀검증·연속물 롤 규칙 고도화** — 기본값으로 시작, 운영 중 튜닝.

## 10. 검증 필요 / 열린 항목

- tick/multiplier 6종 → 구현 시 공식 계약명세로 재확인(특히 NQ mult=20, BTC mult=5·tick=5).
- 임계 범위·step → 실 데이터 분포로 1회 점검 후 튜닝(히트맵 밀도 적정성).
- NQ=F/SI=F/BTC=F yfinance 수집 가용성·과거 범위 → 실수집 1회 확인.
- 신규 data_fetcher 키 충돌 여부 → 기존 키 전수 확인 후 확정.
