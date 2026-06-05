# 선물 IR 설계 — StrategyIR 1급 통합

자연어 빌더(StrategyIR)에서 **선물**을 1급 상품으로 백테스트하기 위한 설계.
옵션(비선형·멀티레그)은 후순위(§6). 데이터 수급과 독립적으로 스키마를 먼저 확정.

## 1. 원칙 — 선물다움은 *상품*의 속성이지 *신호*의 속성이 아니다

신호 블록 트리는 OHLCV에 지표를 계산할 뿐 상품 종류와 무관하다. 따라서 선물 확장은
**신호·Universe·Sizing·Entry·Exit 어휘를 건드리지 않는다.** 진짜 새로운 불가분 사실은
세 가지뿐이고, 셋 다 상품 속성이므로 카탈로그 + 시뮬 파라미터 + 엔진 회계에 둔다:

1. **계약승수(point value)** — 손익 = ΔP × multiplier × 계약수
2. **롤/만기 연속성** — 한 심볼 = 만기물 체인 → 연속 시계열 + 롤
3. **증거금 = 자본** — 보유 자본은 명목가치가 아니라 증거금

(엔진 감사로 확인: ir_engine은 완전한 현금주식 모델, 승수 0군데, 선물 인식은
`exec_defaults.margin_rate` 단 하나(펀딩 비용에만). 그래서 주입점이 좁다 — §5.)

## 2. 스키마 변경 (이번 단계 — 구현 완료)

| 계층 | 파일 | 변경 |
|---|---|---|
| 계약 카탈로그 | `core/quant_core/exec_defaults.py` | `InstrumentSpec`(asset_class·multiplier·tick·currency·증거금·만기·롤) + `_INSTRUMENTS` + `instrument_spec()`·`is_futures()`. `margin_rate()`를 카탈로그 단일출처로 리팩터(기존 `_FUTURES_KEYS` 제거) |
| 시뮬 파라미터 | `core/quant_core/ir_engine/spec.py` `SimSpec` | `roll_method`·`series_adjust`·`roll_cost_pct`·`account_currency` 추가(equity면 무시) |
| 검증 | `spec.py` `validate_strategy` | 선물 sim 설정이 비선물 유니버스에 걸리면 경고(`S-futures`, silent no-op 방지) |
| NL 의미론 | `core/.../capabilities.py` | `instruments`·`roll_method`·`series_adjust`·`account_currency` + `direction`/`leverage`에 선물 용례 |
| NL 쿡북 | `server/app/ir_compiler.py` | `<idioms>` 6번(선물 디렉셔널·추세추종) + `<reference_data>` 자산명 정렬 |
| 테스트 | `core/tests/test_futures_ir.py` | 카탈로그·SimSpec·capability·검증 12 케이스 |

**Universe·Sizing·Entry·Exit·signal 어휘는 무변경.** `direction(short/long_short)`·`leverage`·
`maintenance_margin_pct`는 재사용(중복 atom 금지). 스키마 표면 추가는 SimSpec 4필드뿐.

### 계약 카탈로그 (단일 출처)
`instrument_spec(symbol)` → `InstrumentSpec`. 미등록 심볼 = equity 기본(multiplier=1·margin=1·만기없음).
등록 선물: 코스피200선물(250,000원/pt)·원유선물(1,000)·천연가스선물(10,000)·금선물(100)·
은선물(COMEX)(5,000)·나스닥선물(20)·비트코인선물(5). tick·multiplier는 `server/app/futures_config.py`
(선물분석 대시보드)와 정렬 — ⚠ 추후 futures_config가 이 카탈로그를 읽도록 통합(중복 제거).

## 3. NL → 선물 IR 쿡북

`capabilities.instruments`/`roll_method`/… 와 `<idioms>` 6번이 컴파일러를 안내한다. 핵심 매핑:

| 자연어 | 핵심 IR |
|---|---|
| "코스피200 선물이 20일 신고가 돌파하면 롱, 5% 손절" | `single[코스피200선물]` · `on_signal`(돌파 condition) · `direction=long` · `exit.stop_loss=-5` |
| "원유선물 20일 모멘텀 음수면 숏" | `single[원유선물]` · `direction=short` · `on_signal`(mom<0) |
| "나스닥선물 추세추종, 50일선 위면 보유" | `single[나스닥선물]` · `always`(보유마스크 close>ma50) |
| "코스피200·나스닥 선물 듀얼모멘텀 월간 상위 1" | `list[…]` · `scheduled monthly` · `score`(mom) · `top_n=1` |
| "금선물 추세 양이면 롱·음이면 숏" | `single[금선물]` · 부호점수+`long_short` · `entry.threshold=0` (idiom 2) |

선물 심볼은 종목처럼 이름만 넣으면 엔진이 카탈로그로 자동 인식. 신호·익절/손절(%)·보유기간은
주식과 동일. 만기 롤은 자동(roll_method 명시는 선택).

## 4. 회계 모델 — 1차 근사와 정직한 한계

- **1차(엔진 단계 목표)**: 종가 mark-to-market × 승수 + 증거금=자본. 손익=ΔP×승수×계약수,
  자본점유=notional×init_margin_rate. 숏은 차입 불필요(`short_borrow_pct` 미적용).
- **정직한 한계**: 진짜 일일 변동증거금 현금흐름(variation margin)은 종가평가와 달라
  단순 승수 주입을 넘는 회계 변경. 측정된 차이가 있을 때만 도입(overthinking 회피).
- **롤 비용**: 만기물별 term-structure 데이터가 있으면 실 근월-원월 가격차, 없으면
  `roll_cost_pct` 폴백(정직한 가정 — §7.6).

## 5. 엔진 회계 — E1 구현·검증 완료 / 잔여

**E1 (구현 완료, `_run_scheduled` — 추세추종 always·scheduled 경로)**:
- 정수 계약수 사이징 = `floor(|wt|×nav/(px×multiplier×margin_rate))` (증거금 레버리지)
- 체결단가·현금흐름·NAV·그로스·턴오버·펀딩·마진콜 전부 `×multiplier` 주입
- 선물 보유여력 = `lev_eff = leverage/margin_rate` (주식 mr=1이면 항등 → 완전 백워드 호환)
- 거래세(sell_tax)는 주식 매도만 — 선물 면제(종목별 `stx`)
- 통화: `sym.isdigit()` → `instrument_spec(sym).currency`
- **검증**: 기존 78 테스트 항등 통과(주식 무영향) + 합성 선물 4 테스트(1%→~10% 증거금
  레버리지·숏 대칭·명목>자본 보유). `core/tests/test_futures_engine.py`.
- **정직한 한계**: 종가 MtM×승수 + 증거금 레버리지 모델. 진짜 일일 변동증거금 현금흐름은
  미구현(측정된 차이 시 도입). 혼합 증거금 포트폴리오는 `lev_eff`가 최저율 근사(단일·동질=정확).

**E1b (이벤트 on_signal 경로 — 가드, 다음 증분)**: `run_unified`는 차입 메커니즘이 없어
명목>자본 선물이 무거래로 퇴화 → **선물+on_signal은 엔진에서 차단**(추세추종 안내). 이벤트
경로 선물 회계는 증거금 차감형 모델로 별도 구현 필요.

**E2 (만기 롤 — 데이터 의존, 보류)**: `oil_futures.RollModel` 로직 이식(만기일 강제 청산→롤
+ 롤비용). 만기 캘린더·만기물별 데이터 필요 → 데이터 수급 후. 그때까지 `roll_cost_pct` 폴백.

**(선택) Sizing `fixed_contracts`**: "N계약" 관용. 미구현 enum은 spec에 두지 않는 원칙대로
이벤트/명시 계약수 사이징과 함께 도입.

## 6. 옵션 — 후순위 (별도 계층)

옵션은 IR의 3대 전제를 근본적으로 깬다: 단일심볼=단일시계열(옵션=기초×만기×행사가×콜풋),
선형 return(옵션은 볼록·시간가치), 단일신호→단일포지션(스프레드=멀티레그). 승수 주입으론
불가 — 신규 레그/페이오프 계층 + 옵션 가격(시계열 또는 BS+IV)이 필요. 선물 안정화 후 별도 설계.

## 7. 데이터 수급 (스키마와 독립, 선해소 필요)

- 국내선물: KIS `FHKIF03020100` 일봉(모의 OK) → 검증 가능. 과거 보관 깊이 실측 필요.
- 해외선물: CME/SGX 유료시세 + 만료 계약 과거 데이터 깊이가 실제 블로커(yfinance 함정 동일).
  장기 연속물 백테스트엔 전용 벤더(Databento/Norgate) 검토.
- 상세: `docs/kis-api/INDEX.md` 선물옵션 섹션, `GOTCHAS.md` 2026-06-05.
