# REVERSION 탐색기 — 표본 차트 + 라이브수식 엑셀 설계

**작성일:** 2026-06-29 · **브랜치:** `feat/futures-reversion-chart-excel` (off origin/main `fabf4df`)
**범위:** 선물 분석 `oil_futures` REVERSION 모드 확장 — 기존 [2026-06-27 REVERSION 설계](2026-06-27-futures-reversion-analysis-design.md)의 후속. 가산적.

---

## 1. 동기

REVERSION 모드(머지됨, #235)는 요약 카드·이벤트 표·스윕 격자만 있다. TREND→FORWARD엔 있는
**(1) 표본을 가격 차트에 시각화**, **(2) raw data + 수식 엑셀 export**가 빠져 있다 — 사용자 요청.

## 2. Part 1 — 표본 차트 (TREND 패턴 미러)

`ReversionExplorer`에 `/prices`(전체 종가) fetch 추가 → 두 차트:

**(a) 전체기간 가격 차트** (`ComposedChart`, TREND과 동일 축·툴팁):
- 종가 라인(`Line`).
- **forward 구간 음영**(`ReferenceArea`): 각 트리거 → +N영업일 구간. 방향색(하락소진=빨강 `#de3033` / 상승소진=파랑 `#1668c4`, 옅게). 끝점은 `dateIdx`로 영업일 인덱스 +horizon(TREND과 동일 방식).
- **트리거 마커**(`Scatter`): 하락소진→롱=빨강, 상승소진→숏=파랑, **청산=빈/✕ 마커**(별도 시리즈).
- **피벗 마커**(`Scatter`, 흐리게): 고점/저점 — 트레일링 추세 시작점.

**(b) 산점도** (`ScatterChart`): x=트리거 시 누적 급등락(계좌 %, `run_account`), y=N일 회귀(계좌 %, `reversion_account`). 방향색. "급등락이 클수록 회귀가 큰가" 관계. `ReferenceLine y=0`.

데이터 흐름: 차트는 이미 받은 `/reversion` 이벤트 + `/prices`만으로 브라우저에서 구성(서버 추가 없음).

## 3. Part 2 — 라이브수식 엑셀 (하이브리드 B)

**핵심 결정:** REVERSION은 경로 의존(ZigZag 피벗·트리거)이라 그 부분은 **엔진이 계산한 정적 입력**으로 두고,
**경제성(forward 회귀·레버리지·청산)은 raw OHLCV 수식**으로 만든다. 라이브 파라미터 = **레버리지·보유기간 N**.
전환·급등락·간격은 트리거 집합을 정하므로 **고정**(바꾸려면 웹에서 재export — 탐색은 웹 스윕이 담당).

> 근거: ZigZag 재귀를 엑셀로 옮기면 복잡·검증난(원칙 2·3). 하이브리드는 OFFSET/MIN/MAX 깔끔한 수식이라
> **엔진과 정확히 일치**하고(아래 검증) 핵심 가치(레버리지·청산이 raw 종가에서 어떻게 −100%/회귀가 되는지)를
> 투명하게 보여준다.

**`build_oil_reversion_excel(df, events, *, reversal, run, horizon, leverage, gap, name, price_sym) -> bytes`**

시트:
1. **데이터(raw)** — 전체 OHLCV(A날짜 B시 C고 D저 E종). 수식이 참조할 원본.
2. **회귀계산(라이브)** — 노란 칸: `레버리지`(B1)·`보유기간 N`(B2). 회색 고정: 전환·급등락·간격(+주석).
   이벤트(트리거) 1건당 한 행:
   - 정적: 피벗일·트리거일·방향·방향부호(롱+1/숏−1)·피벗행·트리거행(엑셀 행번호, OFFSET용).
   - 라이브 수식:
     - 트리거가 `=INDEX('데이터(raw)'!$E:$E, 트리거행)`, 피벗가 동일.
     - 누적 급등락(계좌%) `=$B$1*(트리거가/피벗가-1)*100`.
     - 청산 `=IF(데이터부족,"",IF(방향=하락, MIN(OFFSET(...,N))<=트리거가*(1-1/$B$1), MAX(...)>=트리거가*(1+1/$B$1)))`.
     - N일후 회귀(계좌%) `=IF(데이터부족,"",IF(청산, -100, 방향부호*$B$1*(INDEX(close,트리거행+$B$2)/트리거가-1)*100))`.
   - 데이터부족 가드: `트리거행+$B$2 > 마지막데이터행` → "데이터부족"(N 키우면 끝 근처 트리거 보호).
3. **요약(라이브)** — 방향별 COUNTIFS/AVERAGEIFS/COUNTIFS(회귀>0)·청산율. 회귀수익률 평균·성공률·청산율.
   (중앙값은 엑셀 MEDIAN+IF 배열 — 단순화 위해 평균·성공률·청산율만, 중앙값은 스냅샷 시트.)
4. **현재 결과(스냅샷)** — 엔진 계산 정적값(방향별 요약 평균/중앙값·성공률·청산율 + 이벤트 표). 라이브 수식 대조용.

**수식↔엔진 동치(검증 핵심):**
- 비청산 회귀 = `pos*lev*(close[trig+N]/close[trig]-1)` = 엔진 정의 동일.
- 청산 판정: 엔진은 일별 첫 −100% 도달, 엑셀은 윈도우 MIN/MAX 임계 돌파 — **동치**(돌파하면 엔진도 그날 청산).
  청산 시 양쪽 −100%. → 같은 트리거 집합에서 레버리지·N 어떤 값이든 수식=엔진.

## 4. 서버
- `GET /{symbol}/reversion-export.xlsx?reversal&run&horizon&leverage&gap` — `reversion_events`로 이벤트 계산 →
  `build_oil_reversion_excel`. 기존 `trend_export_endpoint` 패턴(검증·`_get_cfg`·Response) 재사용.

## 5. 웹
- `api.ts`: `reversionExport(sym, {reversal,run,horizon,leverage,gap})` blob 다운로드(`trendExport` 패턴).
- `FuturesAnalytics` ReversionExplorer: `/prices` fetch + 차트 2종 + 요약 카드 옆 **엑셀 내보내기** 버튼.

## 6. 검증 (전부 로컬)
1. **core 테스트**(`test_reversion_excel.py` 신규): `build_oil_reversion_excel`가 바이트 생성·openpyxl로 재오픈·
   4시트 존재·이벤트 수만큼 행·스냅샷 값이 `reversion_summary`와 일치. **엑셀 청산 수식 로직(MIN-돌파)을 파이썬으로
   재현해 `reversion_events`의 liquidated/reversion과 일치 단언**(수식 동치 증명).
2. **server 테스트**: `/reversion-export.xlsx` 200·xlsx content-type·검증 422.
3. **web**: `tsc --noEmit` 0에러. 차트는 recharts 타입.
4. **전체 회귀 0** (가산적).
5. **브라우저 E2E**: 로그인+실데이터 게이트라 미검증 — 사장님 확인(기존 동일).

## 7. 구현 순서 (TDD)
1. `build_oil_reversion_excel` + export + core 테스트(스냅샷 일치·청산수식 동치).
2. `/reversion-export.xlsx` + server 테스트.
3. `api.ts` `reversionExport`.
4. ReversionExplorer: prices fetch → 가격차트(음영·트리거·피벗 마커) → 산점도 → 엑셀 버튼.
5. 전체 검증(core·server·tsc).
