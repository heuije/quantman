# 2026-07-07 — 자동매매 전 사이클 크래시 (Close-only 국채 시리즈 × ATR 무가드)

**심각도: Critical(가용성)** · 모듈: core 지표(`quant_core/indicators.py`) × 자동매매 엔진(dataset 로드)
**상태: 수정·검증 완료(브랜치 `fix/indicator-ohlc-guard`) — 로컬앱 릴리스 게이트 대기**

## 요약

07-07 22:13부터 로컬앱의 **모든 매매 사이클**(아침 진입·종가청산·미장·장중 loop)이
`KeyError: 'High'`로 크래시해 **자동매매가 전면 중단**됐다(마지막 정상 07-07 15:40).
근본: PR#325(globalmarket 서빙)가 발행한 **국채 수익률 매크로 37종은 Close-only**
(`['Close','date']`)인데, ① `dataset_scope.needed_symbols`의 ALL_SYMBOLS(매크로 안전망)로
**모든 로컬 사이클 dataset에 자동 유입**되고, ② `compute_all → add_atr`가 `df["High"]`를
**무가드 접근**해 KeyError → ③ `load_dataset_for` 배치 전체 사망 → 사이클 사망.

## 발견

- 07-10 사용자 신고("자동매매 정상 실행됐는지 진단·누락이 네트워크 문제인지") → 로그 진단.
- `localapp.log`: 07-07 22:13:39 첫 `cycle 실행 예외: 'High'` → 이후 07-08·07-09 아침(08:55
  4회 재시도 전부 실패)·종가(15:29/15:40 `krx_close_cycle_* — 'High'`)·미장(22:13/04:40)·
  장중 loop(08:50) **전부 동일 크래시**. preview pull은 직전 성공(`missing=False`) — 네트워크 무관.
- Traceback: `dataset.py:82 load_dataset_for → indicators.py:538 compute_all →
  indicators.py:121 add_atr → KeyError 'High'`.
- 로컬 parquet 전수 스캔: **High 없는 시리즈 = 국채 수익률 37종 전부**(미국채/유로존/일본/중국
  만기별, `['Close','date']`) — 그 외 시리즈는 모두 정상.

## 영향

- **자동매매 전면 다운**: 07-08·07-09 진입·청산 0건(모의라 실손 없음, 실전이었다면 보유
  포지션 청산 불가·신규 진입 불가). 서버 사용 유저 전체 공통(코드+번들 공통).
- 웹 타임라인엔 "누락 ✗"로 표시(에러 스냅샷은 push됐으나 사용자 인지 어려움).
- 부차: 크래시가 결정적(deterministic)인데 재시도 4회(60/300/900s)가 동일 실패 반복.

## 근본 원인 (부류)

**"High/Low/Volume을 요구하는 지표의 무가드 컬럼 접근"** 부류. Volume 지표
(`add_volume_ratio`·`add_adv`)는 이미 올바른 관용구(`if "Volume" in df.columns … else NaN`)를
갖고 있었으나, High 의존 2곳(`add_atr`·`add_high_deviation`)은 무가드였다.
`add_high_deviation`은 add_atr이 먼저 죽어 미발화 상태로 잠복(같은 부류).

증폭 기전: 새 매크로 피드는 ALL_SYMBOLS에 등록되는 순간 **모든 로컬 사이클의 dataset에
자동 편입**된다 — 데이터 형상(Close-only)이 소비자(지표 계산)의 암묵 전제(OHLCV)와 어긋나면
전 유저 사이클이 동시에 죽는 구조였다.

## 대응 (수정 = 부류 단위)

브랜치 `fix/indicator-ohlc-guard` (core만·서버/웹/로컬 배선 무변경):

- `add_atr`: High/Low 부재 시 `atr_14`/`atr_14_pct`를 **NaN 컬럼으로 산출**(스키마 유지 —
  기존 Volume 관용구와 동일). Close-only 시리즈에 ATR은 수학적으로 미정의이므로 NaN이
  올바른 값이다(fallback이 아니라 정확한 의미론).
- `add_high_deviation`: High 부재 시 `high_dev_20d` NaN 컬럼(동일 부류 선제 마감).
- 다운스트림: 조건 평가는 NaN=미정의로 처리(기존 계약) — 국채에 ATR 조건을 걸면 신호가
  안 나올 뿐 크래시하지 않는다. 전략이 쓰는 Close 기반 지표(pct_change 등)는 국채에도 정상.

## 결과 (해소 검증)

- 회귀 테스트 `core/tests/test_indicators_ohlc_guard.py` 7종: Close-only(인시던트 형상)
  compute_all 통과·불가 지표는 NaN 컬럼·full OHLCV는 가드 전과 값 동일.
- **실데이터 재현**: 이 PC의 실제 dataset으로 인시던트와 동일 호출
  (`load_dataset_for(ALL_SYMBOLS∪전략종목, with_indicators=True)`, 130종·국채 37종 포함) →
  **크래시 소멸**, 국채 ATR=NaN·pct 정상, 주식/선물 ATR 정상 계산 확인.
- core+local 전체 스위트 green (수치는 PR 본문).
- **로컬앱 릴리스(v0.9.69) 후 사용자 재검증**: 다음 사이클 정상 실행 확인 필요.

## 재발 방지

- 지표의 컬럼 요구는 **가드가 불변식**: 요구 컬럼 부재 = 해당 지표 NaN 컬럼(스키마 유지),
  배치·사이클은 계속 — "시리즈 하나의 형상이 전체 사이클을 죽일 수 없다".
- 새 매크로 피드 등록 시 체크: ALL_SYMBOLS 편입 = 모든 로컬 사이클 dataset 유입.
  Close-only 등 비-OHLCV 형상이면 지표 가드가 그 형상을 지원하는지 확인(이번 수정으로
  High/Low/Volume 부류는 마감).
- 크래시 지점이 코어 공용(`compute_all`)이므로 서버 백테스트·챗 경로도 같은 가드의 보호를 받는다.
