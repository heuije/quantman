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
- **⚠ 청산 단위 = 퍼센트, 비용 단위 = 분수.** `take_profit`/`stop_loss`/`trail_pct`는 **퍼센트**(7 = 7%) — 엔진 `cur_ret=(close-entry)/entry*100`과 직접 비교(engine `price_exit_reason`), spec M-exit가 "(%)"로, 컴파일러 few-shot이 `15`/`-7`로 못박음. 반면 `commission`/`slippage`/`sell_tax`만 **분수**(0.0003). `explain.py`가 tp/sl을 분수로 오해해 `_frac_pct`(×100)로 포맷 → "700% 익절" 프로덕션 표시 버그였다(엔진 무관 표시-only). 새 % 필드를 explain에 추가할 땐 이미-퍼센트면 `_pct`, 분수면 `_frac_pct` — 헷갈리면 엔진 비교식의 단위를 진실원천으로.
- **⚠ 선물 백테스트 0거래 = 자본 부족(증거금)일 수 있다 — 엔진 버그로 오진 말 것.** 선물 진입예산 = 초기자본 × `Sizing.futures_margin_pct`(기본 20%), 1계약 증거금 = 가격×승수×개시증거금률(코스피200선물 ≈ 300pt×250,000×0.10 = 750만). 예산<증거금이면 `_open`이 `int(예산/증거금)=0`으로 **조용히 0거래**(전 구간 0.0)를 내, 크로스에셋·캘린더·컴파일 문제로 오인하기 쉽다(실제로 그렇게 2번 오진함). **진단 = 자본 스윕**(1천만→1억→10억; 거래수가 0→증가하면 자본 문제 확정) + `is_futures`/`instrument_spec`로 승수·증거금률 확인. 엔진 증거금 사이징은 라이브(`live.event_buy_qty`)와 **동일 식·정상**. 기본자본은 1억(`SimSpec`·컴파일러 few-shot)이라 코스피200선물도 기본 진입 가능, `service._futures_capital_warning`이 그래도 부족하면 명시 경고.
- **⚠ NL 컴파일러는 유저 지정값을 *조용히* 왜곡할 수 있다 — repair 루프는 `validate_fn`이 *보는* 것만 고친다.** 두 사각지대: ① `extra="ignore"` 모델(SimSpec 등)이 **환각/오타 필드를 검증 전에 버림**(`commission_pct`·`transaction_cost_pct`→코드에 없어 비용 무시·기본값) ② 단위 불일치 const는 유효 숫자라 통과(`pct_change_1d`는 **%단위**인데 "±0.1%"를 ÷100해 ±0.001=거의 매일 진입). 둘 다 *그럴듯한 틀린 결과*라 가장 위험. **구조 수정 3축**: (a) `unknown_field_issues`(spec.py)가 raw에서 미지필드를 잡아 repair 피드백 — 엔진 parse는 frozen-ledger 위해 `extra="ignore"` 유지하고 **NL 컴파일 경로(`compile_service._validate`)만 strict**(계층 분리), (b) 컴파일러 `<units_and_costs>` 프롬프트가 %계열(`COMPARE_GROUP` SSOT·`get_indicator_compare_group`)·비용 분수·cross-asset `SYM.` 접두를 명시 + 그 부류 few-shot 앵커 — *스케일* 오류는 유효 숫자라 repair로 못 잡으니 프롬프트가 유일 수단, (c) 에이전트 `<reading_results>`가 결과 `buckets`·`explanation`·`warnings`·`ir` 대조를 강제('엔진 미지원'·'연도별 미반환' 오단정·비용 오귀인 차단). **엔진 계산은 무결 — 검증/컴파일러/에이전트 계층 결함**(라이브 S&P500→코스피200선물 챗봇 검증서 발견). 위 "청산=%·비용=분수"와 같은 뿌리: **단위는 엔진 SSOT, 모든 생산자(블록빌더·NL·미래)가 소비**해야 한다.
- **⚠ "챗봇이 엔진 기능을 못 쓴다"는 반복 부류 = 엔진 verb/study를 추가할 때 챗봇 4계층을 동시에 배선 안 함.** 도달성은 `simulate(nl)`→`compile_nl`→`run_query` 범용 파이프라 거의 완전한데, 갭은 ①**발견성**(에이전트 프롬프트가 simulate를 '백테스트'로만 알아 sweep/extremize/regression·IC/국면 contrast/포트폴리오 진단/연도별을 안 권함 — idiom은 *컴파일러* 프롬프트에 있고 챗 모델은 못 봄) ②**렌더링**(`ChatResultView`가 9개 result shape를 빈 "✓ 분석 완료" 칩으로 떨굼; 국면 contrast는 top-level `equity`를 실어 보내 equity 분기에 먼저 걸려 **버킷·유의성 누락 오인 렌더**). 컴파일러(idioms)·엔진(`run_query`)·빌더(`IrBuilder` 렌더러)는 전부 알/실행/렌더하는데 **챗봇만 MVP 헤드라인 동사(screen·describe-single·inspect·simulate-백테스트)에 멈춰 lag**. 수정 3축: `<analysis_menu>`(프롬프트에 분석 메뉴 명시)+`SIMULATE_TOOL` 설명 범용화(백테스트 아닌 범용 분석 파이프)+`ChatResultView`에 `IrBuilder` 렌더러 이식(**축/리덕션 분기를 equity 분기 *앞*에**). **교훈: 새 엔진 capability = 4계층(엔진·컴파일러 idiom·에이전트 프롬프트 메뉴·결과 렌더러) 동시 배선해야 실제로 쓰인다 — 엔진만 만들면 "구현했지만 안 쓰임".** [[arch_four_layer_contract]]의 챗봇 판(노코드 블록 대신 챗 도구·프롬프트·렌더가 4계층).
- **⚠ "연도별/주기별" 분석은 folds(등분)로 추측하지 말 것 — 컴파일러는 전체기간의 실제 연도 범위를 모른다(데이터는 런타임 로드).** 라이브 결함: 에이전트가 "연도별"을 `folds=252`(252거래일/년 상수와 혼동)로 컴파일 → 252개 16일 폴드(연율화 CAGR ±100% 무의미) + 차트 x축 252라벨 뭉개짐. **근본해법 = 엔진 `Study.split_period`("year"/"quarter"/"month")**: 엔진이 실데이터 `rets.index.to_period()`로 달력 주기 그룹(키="2015"·"2015Q1") — folds/split_dates보다 우선. 컴파일러는 "연도별/연간/매년"→`split_period="year"`만 선언(few-shot+`<ir_structure>`+`capability_spec.study_split_period`). 골든은 split_period 기본 None이라 byte-identical. **+SweepChart는 축 종류별 커스터마이즈**(제네릭 "구간N"·무제목 금지): 가로축 제목=파라미터/종목/국면/연도(기간키 형태로 연·분기·월 판별)·세로축=선택 지표 단위(%·배)·버킷 많으면(>24) x라벨 자동 솎기(`interval` 해제). 교훈: **달력 주기 분할은 데이터를 가진 엔진이 소유**(컴파일러 추측 금지), **제네릭 차트는 축 의미를 잃는다 — 축별 제목·단위·라벨 필수**.

## 현재 구조 (안정)

**기능.** 백테스터를 "자연어 질문이 되는 기계"로 확장. 하나의 질문을 **동사 × 대상**으로 쪼갠다: `query ∈ {select, describe, relate, simulate}` × `study(axis×reduction)`.
- **select**(스크리닝, 예 "저평가 반도체주 3개") · **describe**(단일종목 360리포트·포트폴리오 진단) · **relate**(다중팩터 횡단 회귀=Fama-MacBeth) · **simulate**(백테스트·스윕·기간분할)

> 〔담당 경계〕 엔진 코어 + `select`·`relate`·`simulate` = 조대표. `describe` 갈래 중 개별종목분석(단일 360)·포트폴리오 진단은 희제 담당.

**폴더.**
- `core/quant_core/ir_engine/` — 엔진 본체: `run.py`(**`run_query` 디스패치** = 동사 라우팅) · `spec.py`(`StrategyIR` 스키마) · `capabilities.py`(능력 노출) · `engine.py`·`backtest.py`(실행) · `live.py`(backtest=live 일치) · `explain.py`·`metrics.py`·`sweep.py`
- `server/app/routers/ir.py` — 질문 실행 라우터 + 360 뉴스 facet enrichment
- `server/app/routers/ir_compile.py` — **자연어 → StrategyIR 컴파일러**(기본 Sonnet 4.6, 비용 롤백 env `QP_NL_COMPILE_MODEL=claude-haiku-4-5-20251001`)
- `web/src/components/ResultCharts.tsx` 외 시각화 컴포넌트 — 동사별 결과 카드/차트(랭킹·360·진단·회귀·최적해)
- 설계 스펙: `docs/REDESIGN/question_layer_spec.md`

**구동 워크플로.** 웹/자연어 질문 → `ir_compile.py`(NL→StrategyIR 번역) → `ir.py`가 `run_query` 호출 → `core/ir_engine`이 동사·study에 따라 계산 → 구조화 결과 반환 → 웹이 결과 종류별 컴포넌트로 시각화.

**불변식.**
- **결정적 core.** 엔진은 네트워크·env·시계 의존 0 — 그래야 골든 테스트가 고정된다. 라이브 데이터(뉴스 등)는 서버 엣지에서 결과에 덧붙인다(엔진 안에 넣지 말 것).
- `signal`은 describe에도 **필수**(스키마 계약).

**완성도 ~70~80%.** 보류: P3 데이터 4종, P4 2차최적화(QP), 일부 P6 결과형태 브라우저 미확인. NL·웹 라이브 검증 일부는 로그인 자격 경계로 자동검증 불가(사용자 세션 필요).

**프로덕션 검증 채널** = 로그인된 `quantman.vercel.app` 페이지 컨텍스트에서 `localStorage.qp_token` Bearer로 `quantman-production.up.railway.app` 호출.

## 작업계획 로그 (누적·최신 우선)

### [2026-06-19] 증빙 엑셀 export (P2: 전 분석유형) [완료·미배포]
- 의도: P1(백테스트)에 이어 **모든 IR 분석유형**(스윕 param/entity/label·기간분할·최적화·select·describe single/portfolio/signal·relate ic/regression/event)을 증빙 엑셀로 export. 엑셀이 챗봇 신뢰성 증명의 핵심 수단이라 형상별 MECE 검증 필수.
- 계획: `build_strategy_excel`을 결과형상 디스패처 + 형상별 빌더 10종. 메타분석=감사표(값+방법론), describe=라이브수식 가능분. 엔드포인트 simulate 게이트 제거. 챗·빌더 결과뷰 wrapper로 IR 보유 결과 전부에 export 버튼.
- 시행착오·인사이트: ⚠형상 판정은 **probe로 13형상 실제 실행→result 구조 덤프**가 진실원천(추측 금지). `axis="condition"`(label 스윕)이 equity를 들고 다녀 simulate 오인 위험 → **axis 우선 디스패치**(#169 교훈 재적용). 메타분석은 각 버킷이 별도 시뮬/추정량이라 한 시트 라이브수식 불가 → 감사표(값)+사용 IR+방법론으로 증빙. DESCRIBE는 원자료 종가로 52주·연변동성, 포트는 `HHI=SUMPRODUCT`·가중PBR 라이브(엔진값과 대조). chat 도구 IR 동봉을 `run_tool`(screen/describe)까지 확장(inspect=원시 dump라 IR 없음→버튼 미노출).
- 결과 구현: `excel_export.py` build_strategy_excel 디스패치 + `_build_{select,describe_single,describe_portfolio,sweep,condition,period_split,extremize,signal_dist,relation,event}` + 공용 헬퍼(`_perf_table`·`_methodology`). 엔드포인트 전 형상 허용(실행 실패만 400). 웹 `ChatResultBody`/`ResultPanelBody` wrapper로 IR 보유 결과 전부 버튼. **MECE 검증: `test_ir_excel_export_shapes`(12형상 파라미터화 — 시트·헤더·값·수식 직접 검수 + 포트 HHI 수식=엔진값 정확일치) + 직접 inspect(전 시트 셀/수식 덤프 확인) + 샘플 13종(`퀀트/sample_excels/`).** core 324·server 337·web build+lint(신규0). 골든 무변경. 미배포(PR#172 draft에 추가 예정).

### [2026-06-19] 증빙 엑셀 export (P1: 백테스트) [완료·미배포]
- 의도: 챗봇/빌더가 IR 백테스트를 돌리면 *결과만* 주는 게 아니라 '어떤 데이터를 어떤 연산으로' 산출했는지 **데이터+라이브수식 엑셀**로 증빙(선물 `build_oil_excel` 취지). 온디맨드 버튼. 모든 IR 분석유형이 목표지만 P1은 SIMULATE만(나머지 형상 P2·실시간 변수조정 P3).
- 계획: `core/ir_engine/excel_export.py`(`build_strategy_excel` 5시트)+TDD → server `POST /ir/strategy/export.xlsx`(`/ir/strategy` 본문·실행 재사용, LLM 없음=토큰0) → web 공용 `ExcelExportButton`(챗·빌더).
- 시행착오·인사이트: ⚠엔진은 정수주·현금·마진·지연체결의 **이벤트 NAV 시뮬**이라 셀 수식으로 NAV 경로를 정확히 복제 불가(naive `cumprod(가중×수익)`≠엔진). → 라이브 수식은 **엔진 정본 자산곡선(equity) 위 정의식 변환**(일수익·낙폭·CAGR·샤프·MDD)만 — `bps=0`이면 엔진 metrics와 *정확히 일치*(증빙), '일일추가비용(bps)' 노란칸으로 사후 비용 민감도(라이브). 전략 로직/파라미터 변경 라이브 재계산은 **P3 재실행**이 담당 → 엑셀에 'IR→수식 컴파일러' 불필요(분업: 엑셀=산술·비용 검증). ⚠샤프는 `backtest._metrics`가 pandas `.std()`(ddof=1 표본)라 Excel **STDEV.S**(STDEV.P 아님). ⚠openpyxl은 수식 미평가 → 라이브수식 *수치* 검증은 파이썬 모사(formula-math) 테스트로 엔진 metrics 재현을 고정 + 수동 Excel 1회.
- 결과 구현: `build_strategy_excel(ir, dataset, result)` 5시트(백테스트=라이브수식·거래내역=정적앵커·원자료·일별비중·지표설명). **엔진 무변경(골든 byte-identical).** 서버 `_load_ir_dataset` 헬퍼로 `/ir/strategy`·export 공용(드리프트 0)·**simulate만 게이트**(그 외 400, P2 예정). 웹 `ExcelExportButton`(`var(--accent)`, `trendExport` blob 다운로드 패턴)을 ChatResultView simulate + IrBuilder ResultPanel 재사용. **검증: core 309·server 336·web build+lint(신규 0)·excel 8(formula-math 포함)·endpoint 2.** 미배포(사용자 승인 대기). 잔여: P2 형상별 엑셀(select/describe/sweep/relate 감사표)·P3 실시간 변수조정.

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
