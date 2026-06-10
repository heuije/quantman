# 인사이트 엔진 〔담당: 조대표 · describe 중 개별종목분석·포트폴리오는 희제 → docs/modules/stock-analysis.md·portfolio.md〕

> 학습 원장. 작업계획 착수 시 로그 entry(의도·계획)를 추가하고, 완수 시 시행착오·인사이트·결과구현을 채우고 전이 가능한 교훈을 맨 위 §교훈으로 distill한다.

## 📌 교훈·함정 (작업 전 먼저 읽기)

- **엔진은 결정적(네트워크·env·시계 의존 0).** 그래야 골든 테스트가 고정된다. 라이브 데이터(뉴스 등)는 엔진 안에 넣지 말고 **서버 엣지에서 결과에 덧붙인다**.
- **`signal`은 describe에도 필수**(스키마 계약). 동사 무관하게 빠뜨리면 안 된다.
- **⚠ 좁은 게이트로 골든을 보존하라.** `hold_days==0` 게이트나 부호처리를 일반화하면 진입 바 가격청산(익일시가→당일종가)이 바뀌어 골든이 깨진다. 부호·당일청산은 전부 `pos.sign`/`==0` 경유로만 처리.
- **`_open`/`_close`/NAV/weight·margin-call NAV recompute 어디든 부호를 빠뜨리면 숏 손익이 틀어진다.** 부호는 항상 `pos.sign` 한 곳으로.
- **`_direction_for`가 방향정책의 단일 출처 = 엔진 직교화의 1번 축.** 향후 scheduled 경로도 이리로 모아 방향정책을 일원화한다(현재는 on_signal만).
- **scheduled는 당일청산 불가** → `scheduled + long_short + hold_days=0` 조합은 backtest≠live였다. NL 컴파일러가 비랭킹·당일매매를 `on_signal`로 강제 라우팅해 닫음.
- **NL·웹 라이브 검증은 로그인 자격 경계로 자동검증 불가**(사용자 세션 필요). 프로덕션 검증 = 로그인된 `quantman.vercel.app` 페이지에서 `localStorage.qp_token` Bearer로 `quantman-production.up.railway.app` 호출.

## 현재 구조 (안정)

**기능.** 백테스터를 "자연어 질문이 되는 기계"로 확장. 하나의 질문을 **동사 × 대상**으로 쪼갠다: `query ∈ {select, describe, relate, simulate}` × `study(axis×reduction)`.
- **select**(스크리닝, 예 "저평가 반도체주 3개") · **describe**(단일종목 360리포트·포트폴리오 진단) · **relate**(다중팩터 횡단 회귀=Fama-MacBeth) · **simulate**(백테스트·스윕·기간분할)

> 〔담당 경계〕 엔진 코어 + `select`·`relate`·`simulate` = 조대표. `describe` 갈래 중 개별종목분석(단일 360)·포트폴리오 진단은 희제 담당.

**폴더.**
- `core/quant_core/ir_engine/` — 엔진 본체: `run.py`(**`run_query` 디스패치** = 동사 라우팅) · `spec.py`(`StrategyIR` 스키마) · `capabilities.py`(능력 노출) · `engine.py`·`backtest.py`(실행) · `live.py`(backtest=live 일치) · `explain.py`·`metrics.py`·`sweep.py`
- `server/app/routers/ir.py` — 질문 실행 라우터 + 360 뉴스 facet enrichment
- `server/app/routers/ir_compile.py` — **자연어 → StrategyIR 컴파일러**(기본 Haiku 4.5, 롤백 env `QP_NL_COMPILE_MODEL`)
- `web/src/components/ResultCharts.tsx` 외 시각화 컴포넌트 — 동사별 결과 카드/차트(랭킹·360·진단·회귀·최적해)
- 설계 스펙: `docs/REDESIGN/question_layer_spec.md`

**구동 워크플로.** 웹/자연어 질문 → `ir_compile.py`(NL→StrategyIR 번역) → `ir.py`가 `run_query` 호출 → `core/ir_engine`이 동사·study에 따라 계산 → 구조화 결과 반환 → 웹이 결과 종류별 컴포넌트로 시각화.

**불변식.**
- **결정적 core.** 엔진은 네트워크·env·시계 의존 0 — 그래야 골든 테스트가 고정된다. 라이브 데이터(뉴스 등)는 서버 엣지에서 결과에 덧붙인다(엔진 안에 넣지 말 것).
- `signal`은 describe에도 **필수**(스키마 계약).

**완성도 ~70~80%.** 보류: P3 데이터 4종, P4 2차최적화(QP), 일부 P6 결과형태 브라우저 미확인. NL·웹 라이브 검증 일부는 로그인 자격 경계로 자동검증 불가(사용자 세션 필요).

**프로덕션 검증 채널** = 로그인된 `quantman.vercel.app` 페이지 컨텍스트에서 `localStorage.qp_token` Bearer로 `quantman-production.up.railway.app` 호출.

## 작업계획 로그 (누적·최신 우선)

### [날짜미상] 부호방향 long_short 라이브 양방향 (M5d) [완료]
- 의도: 조건별 롱/숏을 단일 전략으로 라이브 양방향 매매(예: S&P 부호 → 코스피200선물 시가매수 or 시가매도). `direction="long_short"` + score 부호방향으로, 기존 단방향 라이브를 깨지 않으면서 부호에 따라 바별 롱/숏을 체결하는 게 목표. 랭킹 long_short(top_n/top_pct·scheduled)와는 구분되며 단일·선물 한정 범위.
- 계획: 방향정책의 단일 출처 `engine._direction_for(buy_bool, score_vals, base_sign, threshold)` 도입 후 4계층 배선 — spec 예외(on_signal+long_short+score) → 게이트(`_assert_live_tradable`) → preview(`_evaluate_ir_strategy`) → executor(`_try_buy_one_symbol`). NL 컴파일러 라우팅도 함께.
- 시행착오·인사이트: condition은 base_sign×bool(단방향 보존), score는 부호방향(>thr 롱·<thr 숏·사이 0)로 분기해야 단방향 의미가 유지된다. **랭킹 long_short(top_n/top_pct·scheduled)와 반드시 구분** — 랭킹은 라이브 미지원 유지. ⚠`_direction_for`는 향후 scheduled 경로도 호출해 방향정책을 일원화할 **엔진 직교화의 1번 축**(현재는 on_signal만). scheduled는 당일청산이 불가해 `scheduled + long_short + hold_days=0`이 backtest≠live였던 부정합을 NL 라우팅으로 닫음.
- 결과 구현: `run_unified`(on_signal)가 `long_short`+score 허용(spec.py 예외)하고 `_open(sign)`·`dir_arrs`로 바별 방향 체결. 4계층: spec 예외(on_signal+long_short+score) → 게이트(`_assert_live_tradable`: on_signal+전종목 선물 directional만 라이브 허용·랭킹/비선물 차단) → preview(`_evaluate_ir_strategy`가 shorts도 emit + 후보에 `direction` 부착) → executor(`_try_buy_one_symbol(cand_direction=)`로 `_submit_buy`/`_submit_open_short` 분기; 방향 없는 long_short 후보는 무음 롱전환 방지 skip). **골든 14 byte-identical 보존.** NL 컴파일러 라우팅: `ir_compiler._route_directional`이 결정적으로 부호방향 long_short 당일매매(비랭킹·hold_days=0)를 `on_signal`로 강제(LLM이 옛 쿡북대로 scheduled 내도 수렴) + idiom 1·6이 의도(이벤트/당일→on_signal·정기→scheduled)로 안내. **한계: 단일/선물 한정**(다종목 per-symbol 방향은 사이징 정규화 패리티 협의 후 — 현재 범위 외). 라이브 E2E는 사용자 모의 대기.

### [날짜미상] 당일매매 hold_days=0 [완료]
- 의도: 시가진입·종가청산 당일매매를 분봉 없이 지원. `Exit.hold_days=0`이면 진입한 바의 종가에 청산되도록(`fill=next_open`과 결합 시 시가→종가 당일 O→C). 백테스트 엔진과 자동매매 종가청산 사이클(Stage B)을 함께 배선하되 기존 hold_days≥1 골든은 보존.
- 계획: 엔진 청산 디스패치에 `hold_days==0` 게이트 추가 → 자동매매 종가청산 사이클(Stage B) 배선(trader·runner·스케줄러 cron) → 라이브 사이클(아침/종가) 구분.
- 시행착오·인사이트: ⚠게이트(`== 0`)를 일반화하면 진입 바 가격청산이 익일시가→당일종가로 바뀌어 골든이 깨진다 — `hold_days==0`일 때만 동작하게 좁혀야 한다. 라이브는 사이클이 아침·종가로 나뉘므로 어느 사이클인지 구분 필요(종가에만 당일청산, 아침은 보유 유지). ⚠라이브 E2E(실 KIS 단일가 체결) 검증은 SimBroker·단위까지만 가능하고 사용자 업데이트 후 대기.
- 결과 구현: 엔진은 **hold_days==0일 때만** next_open defer 청산을 같은 바 종가로 즉시 실행(engine.py 청산 디스패치) — hold_days≥1은 byte-identical(골든 보존). 라이브 사이클 구분은 `live.cycle_exit_reason(is_close=)`. 자동매매 종가청산 사이클(Stage B) 배선 완료: `trader.liquidate_day_trades(dataset, instrument_class)` + `runner.run_close_cycle` + 스케줄러 종가 cron(주식 15:25·선물 15:43, 단일가 발주창 내). 게이트 ⑤ 제거 → paper/live 허용.

### [날짜미상] 이벤트(on_signal) 단일종목 숏 백테스트 [완료]
- 의도: 이벤트 경로(`engine._run_on_signal`)가 `direction="short"`를 무시하고 롱으로 계산하던 갭을 수정. 단일종목 숏 이벤트 백테스트를 `Position.sign` 인지로 올바르게 계산해, 라이브 숏(M5c 진입·Stage B 청산)과의 backtest=live 패리티를 회복하는 게 목표.
- 계획: `_open`/`_close`/NAV를 `Position.sign` 인지로 전환(숏=sell-to-open·open에 거래세·buy-to-cover, NAV=`sign*shares*close*mult`). 롱(sign=+1)은 byte-identical 보존.
- 시행착오·인사이트: ⚠`_open`/`_close`/NAV/weight·margin-call NAV recompute 어디든 부호를 빠뜨리면 숏 손익이 틀어진다(전부 `pos.sign` 경유). long_short 이벤트는 범위 외(롱 유지) — 단일 short만.
- 결과 구현: `engine._run_on_signal`이 `direction="short"`를 sign 인지로 처리(숏=sell-to-open·open에 거래세·buy-to-cover, NAV=`sign*shares*close*mult`). **롱(sign=+1) byte-identical(골든 보존).** 라이브 숏(M5c 진입·Stage B 청산)과 backtest=live 패리티 회복.
