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
- **⚠ ValueType.LABEL은 dtype 무관(숫자 레짐 bucket/calendar + 문자열 분류 attribute) — 라벨 소비 러너에서 float 강제 금지.** capabilities가 광고하는 계약(`label=attribute('Sector')`)은 **모든** 러너가 이행해야 한다 — 라벨 소비가 러너마다 사적 구현이면 광고·구현 불일치가 컴파일러(LLM)를 타고 런타임 크래시로 표면화된다(실사건: "섹터별 외국인 순매수" → `_run_signal_study`의 `to_numpy(dtype=float)`가 `ValueError: '자동차'`; 이벤트스터디 동일 부류; IC는 `columns[0]`을 전 시장 국면으로 *가정*해 섹터 라벨이 **조용한 오답**). 수정 관용구 = `_label_panel` 정규화(시간축 단일컬럼 broadcast·종목축 attribute를 (일×종목)으로 통일) + dtype-무관 그룹핑(`pd.unique`+`== g` 동등비교 — simulate axis=label의 `compare_by_partition`과 동일). 의미상 불가한 곳(IC 종목축 라벨·target 자체가 범주형)은 조용히 붕괴 말고 **fail-loud 한국어 안내**. 회귀 잠금 = `core/tests/test_ir_label_categorical.py`(러너×dtype 매트릭스).
- **⚠ "연도별/주기별" 분석은 folds(등분)로 추측하지 말 것 — 컴파일러는 전체기간의 실제 연도 범위를 모른다(데이터는 런타임 로드).** 라이브 결함: 에이전트가 "연도별"을 `folds=252`(252거래일/년 상수와 혼동)로 컴파일 → 252개 16일 폴드(연율화 CAGR ±100% 무의미) + 차트 x축 252라벨 뭉개짐. **근본해법 = 엔진 `Study.split_period`("year"/"quarter"/"month")**: 엔진이 실데이터 `rets.index.to_period()`로 달력 주기 그룹(키="2015"·"2015Q1") — folds/split_dates보다 우선. 컴파일러는 "연도별/연간/매년"→`split_period="year"`만 선언(few-shot+`<ir_structure>`+`capability_spec.study_split_period`). 골든은 split_period 기본 None이라 byte-identical. **+SweepChart는 축 종류별 커스터마이즈**(제네릭 "구간N"·무제목 금지): 가로축 제목=파라미터/종목/국면/연도(기간키 형태로 연·분기·월 판별)·세로축=선택 지표 단위(%·배)·버킷 많으면(>24) x라벨 자동 솎기(`interval` 해제). 교훈: **달력 주기 분할은 데이터를 가진 엔진이 소유**(컴파일러 추측 금지), **제네릭 차트는 축 의미를 잃는다 — 축별 제목·단위·라벨 필수**.
- **⚠ 평가 달력의 주인은 유니버스다 — 참조전용 심볼은 달력·패널 컬럼에 끼지 않는다.** 계약 = `EvalContext.from_dataset(ds, universe=syms)`: master_idx=유니버스 심볼 달력 합집합, symbols(패널 컬럼)=유니버스, 참조("SYM.X")는 resolve_data ffill 브로드캐스트로만 합류. 전 키 합집합 달력은 __SELF__ 시리즈(ffill 없음 — 그 자체는 옳음)에 타 달력 일자 NaN 구멍을 만들고, ts_*(rolling, min_periods=window)는 창에 구멍 1개면 NaN이라 120MA류가 전멸 → compare False → **크래시 없는 "항상 현금"(전 연도 정확히 0.0%·무거래)**이 된다(D1·2026-07-10). 참조 심볼이 패널 컬럼에 끼면 cs_rank·신호분포·IC rank도 평가 *중* 오염된다(러너의 사후 subset으로 못 막음). "무거래 0.0%" 진단은 신호 미충족·자본부족(위 항목)·**달력 오염**을 구분할 것. 새 ctx 호출부는 반드시 universe를 전달 — 누락하면 레거시 합집합(=이 결함)이다. 회귀 잠금 = `tests/test_cross_calendar_context.py`.
- **⚠ 러너 능력은 산문이 아니라 계약(`contracts.py`)으로 선언한다 — 새 러너/축 추가 = REGISTRY 1선언.** 조용한 오답 부류(음수창 wrap conv#50·코스닥 탈락·IC라벨·D1)의 공통 근본 = 러너층만 단일 선언 부재로 광고(capabilities 산문)↔검사(validator)↔구현(러너)↔자기서술이 손동기화. 이제 러너별 입력 도메인(원시형 4종)·명시 한계(not_supported)·shape를 `RunnerContract`로 선언하면 검증기 C-*(수리 루프의 교사)·`run_query` 경계 가드(저장 IR 방어)·컴파일러 `지원_한계` 프롬프트가 전부 파생된다. **디스패치 분기를 바꾸면 `resolve_runner`(결정 SSOT)와 일치 테스트(`test_runner_contracts` 전 러너 모의 계측)를 함께 고칠 것** — 어긋나면 엉뚱한 러너의 계약을 검사한다. 러너가 입력을 조용히 버리면(빈 슬라이스·skip) **탈락 회계(`result["accounting"]`)로 세어 동봉**하고 summarize/status가 문장화한다(이벤트 스터디 파일럿: "표본 221/656건"). 설계·인벤토리 = `docs/REDESIGN/capability-contract-redesign.md`(러너 21종 전수 부록 A).

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

### [2026-07-11] 청산 타이밍(exit.fill) 1급화 — 표현력 갭+백테↔라이브 패리티 갭 [진행중]
- 의도: IR엔 진입 fill만 있고 청산 시점 필드가 없어 "익일 종가 청산"이 표현 불가하고, fill=close+hold≥1은 백테(종가 청산)≠라이브(시가 청산) 패리티 갭이 있었다(실전 #29 해당·유저 의도=시가매도 확인). `position.exit.fill`(next_open|close·기본 None=legacy) 신설 — **제1원칙: 기존 연동 전략은 무마이그레이션·전 계층 byte-identical**(사용자 요구). 설계=docs/REDESIGN/exit-fill-timing-redesign.md.
- 구현: D1 스펙+validator(score 경로 S-exit-fill 명시 거부 — silent 무시 차단·1단계 condition만) · D2 rule 엔진(보유기간 청산만 오버라이드·pending_sells 드레인 defer 밖 허용·조건 청산 현행 유지) · D3 core seam `cycle_exit_reason` 창 게이트(close=종가 사이클만·legacy=아침만 — 아침 §2 자동 정합) + 로컬 4곳(종가창 selector 2·아침 넷팅 PLAN 제외·daytrade_unclosed I5+ 확장) · D4 웹 설정값 표기.
- 검증: pin 2(legacy 고정)+신규 3+validator 1+scenario 2 = 신규 8 green · core 737·골든 411·local 753·server 542·web tsc0/build 전부 회귀 0.
- 남은 것: PR·머지 → 로컬앱 릴리스(라우팅 반영) → **웹 빌더·NL 노출은 로컬 릴리스 후**(D5 순서 — 구앱 조용한 divergence 차단). score(리밸런싱) 경로 지원은 후속.

### [2026-07-10] 크로스캘린더 마스터 달력 스코핑 — __SELF__ ts_* 조용한 항상현금(D1) 근본수정 [완료·PR#349]
- 의도: 신호 트리에 타 달력 참조 심볼(VIX·미국채 등 US 매크로)이 들어가면 같은 트리의 __SELF__ ts_*(이동평균 등) 조건이 영구 False가 돼 전략이 크래시 없이 "항상 현금"(전 연도 정확히 0.0%·무거래)을 성공 반환하는 조용한 오답을 근본 수정. 로컬 $0 하니스 연구와 챗봇 실대화("코스피200이 120일선 위 AND VIX<25면 2배")로 재현·확정된 결함.
- 계획: `EvalContext.from_dataset`에 universe(평가 주체) 도입 — 마스터 달력·패널 컬럼을 유니버스로 스코핑, 참조 심볼은 브로드캐스트(ffill)로만 합류. 엔진·러너·live·legacy backtest·sweep·server preview 13개 호출부 배선. red 회귀 → 구현 → 전 스위트 + 실데이터 시나리오.
- 시행착오·인사이트: 원인 체인 = ① from_dataset master_idx가 전 심볼 합집합(참조 매크로가 KR 달력에 US-only 일자 주입) ② __SELF__ _matrix는 ffill 없음(그 자체는 옳음) → KR 시리즈 NaN 구멍(연 ~93일) ③ ts_* rolling min_periods=window → 창에 구멍 1개면 전체 NaN ④ compare(NaN)=False. **`_scoped`(기존)의 독스트링이 이미 "유니버스 공통 달력에서 평가"를 선언했지만 구현은 dataset 키만 좁히고 달력은 보존된 참조 포함 합집합 그대로 — 이번 수정이 그 원설계를 완성.** 같은 부류 부수 결함도 함께 닫힘: 참조 심볼이 패널 컬럼에 끼어 cs_rank·신호분포(ravel)·IC rank(axis=1)를 평가 중 오염, select as_of 꼬리(24/7 심볼) NaN 단면, 스케줄 경로가 US-only 일자에 합성(ffill) 바 리밸런스, live 매도조건 iloc[-1]이 매크로 전용 꼬리 일자에서 평가. min_periods 완화는 의미 변화(부분창 MA)라 기각. 06-30 3a(브로드캐스트 ffill은 정상 실측·표면화만)와 상보 — 그땐 참조 쪽, 이번엔 __SELF__ 쪽 달력이 뿌리. 합성 회귀 픽스처는 휴장일 modulus를 같게·remainder만 다르게(겹치면 그 일자가 합집합에서도 빠져 구멍이 사라지고 일부 창이 살아남아 red가 안 됨 — 실측으로 발견). 검증: 신규 `tests/test_cross_calendar_context.py` 5(red 3→green + 순수KR 불변·레거시 union 핀 2) + core 731·루트 411·server 538 전부 green(골든 포함) + 실데이터(dev-data) AND(>120MA, VIX<25): 가격다리 True 0%→63%·누적 +1388%/거래 63·명시ref판과 연도별 자릿수 일치.
- 교훈: 위 §교훈 "평가 달력의 주인은 유니버스다" 항목으로 distill.

### [2026-07-05] 범주형(섹터) 라벨 부류 마감 — "라벨=범주형" 계약 러너 통일 [완료·미푸시]
- 의도: 챗 쿼리 "외국인 순매수가 가장 높은 섹터들 조사"가 `_run_signal_study`에서 `ValueError: could not convert string to float: '자동차'`로 크래시(prod 동일 코드). 진단 결과 단건이 아닌 부류 — LABEL 타입에 dtype 계약이 없고 라벨 소비가 러너마다 사적 구현이라, capabilities가 광고하는 `label=attribute('Sector')`를 simulate(axis=label)만 이행하고 신호분포·이벤트스터디는 크래시, IC는 첫 컬럼을 전 시장 국면으로 가정(조용한 오답)했다.
- 계획: (B)라벨=범주형 계약으로 러너 통일(A안 "숫자로 좁힘"은 기능 후퇴라 기각). red 테스트 매트릭스 먼저 → 구현 → 전 스위트.
- 시행착오·인사이트: 정답 패턴이 엔진 안에 이미 존재(`_label_panel`+`compare_by_partition`) — 부류 마감은 신규 발명이 아니라 **기존 관용구를 단일 seam으로 승격**. IC의 조용한 오답이 크래시보다 위험(가정을 검증으로 교체해 fail-loud). 이벤트 regime 하류 집계(`groups[r]` dict·`str(k)`)는 이미 dtype-무관이라 수정 불요. 검증: red 4→green(신규 `test_ir_label_categorical.py` 러너×dtype 매트릭스 5) + core 686·server 493·루트 406 전부 green + **실데이터(dev-data 실분류·실flow) 실사건 IR 재실행 성공**(섹터 5그룹 분포 산출).
- 교훈: 위 §교훈 "ValueType.LABEL은 dtype 무관" 항목으로 distill.

### [2026-07-02] 챗봇 품질 Wave 2 Phase 4 — 학습 hook substrate [완료·미푸시]
- 의도: 5단계 파이프라인 마지막 Phase. Phase2/3 방법지능이 프로덕션에서 실제 쓰이는지 측정할 수 있게, 턴이 선택한 분석법을 적재. 본격 flywheel(검색·큐레이션)은 별건 Phase(과적합/노이즈 난제 선결 후).
- 계획: ChatTurnMetric.result_shape 추가→agent 턴 첫 분석 shape 수집→chat_analytics method_dist 리포트.
- 시행착오·인사이트: **§0.5 통찰 — 설계가 제안한 4학습신호 중 3개는 *이미 수집*됨**(질문의도=Message 텍스트·후속=turn 시퀀스·품질=result_status[Phase1]). 유일한 갭=**선택방법(result_shape)** — 어떤 분석법(event_study/relate_ic/simulate/select…)을 썼는지가 어디에도 쿼리가능하게 저장 안 됨(tool_names는 도구만·shape는 런타임 소실). 이거 하나만 추가(§6 비-목표 "기존 자산 재사용·중복 구축 금지"). **투기적 flywheel-전용 데이터(질문 fingerprint·후속 신호 필드)는 별건 Phase로 미룸(원칙2 — dead 데이터 수집 회피).** 구현: models.result_shape+`_NEW_COLS` 마이그레이션·agent 첫 분석결과 `full.get("shape")` 수집(worst_status 옆)·chat_analytics `method_dist`. 검증: 단위2(적재·집계)·서버 471 pass.
- 교훈: **"학습 substrate"의 대부분은 이미 로그에 있다 — 진짜 갭(선택방법)만 좁게 추가하고 flywheel 전용 투기 데이터는 미룬다(원칙2). 측정 가능성이 캠페인 루프를 닫는다**(Phase2/3이 프로덕션에서 작동하는지 method_dist로 확인).

### [2026-06-30] 챗봇 품질 Wave 2 Phase 3 — 엔진 substrate (3a 교차캘린더 + 3b 선물 사이징) [3a·3b 완료 / #3 위임]
- 의도: 챗봇 품질 재설계 5단계 파이프라인 ③엔진 substrate 정확성. A 교차캘린더(#1)·D 선물 사이징(#2). #3 영업이익 데이터는 데이터엔진 세션(PR#264/267) 영역이라 위임.
- 계획: 3b D 선물 사이징부터(머지된 PR#266 0.195율 위에) → 3a A 캘린더.
- 시행착오·인사이트(3b·#2): 선물 백테스트가 자본<1계약 증거금이면 `engine.py:run_unified._open`이 `int(budget/denom)==0`으로 **침묵 스킵** → `result_status` simulate nt==0이 "신호 미충족"으로 **오인 고지**(실제는 신호 발생·자본부족). PR#266이 증거금률 0.10→0.195로 올려 이 케이스가 더 빈번. **근본수정**: `_open`이 선물·budget>0·0계약 스킵을 `capital_starved` 카운트→결과 노출(체결·수치 불변, 진단만 추가), result_status가 그걸 읽어 "자본<1계약 증거금 — 자본↑·사용률↑·미니선물 검토"로 정직 고지(Phase1 T4 연결). 스케줄 경로(_run_scheduled)는 목표비중→정수 변환이라 탐지 모호·측정증상 없어 미포함(원칙2). 검증: 코스피200선물 자본 1e7→starved=29·status=empty; 원유선물 회귀가드. core 577.
- 시행착오·인사이트(3a·#1): S&P500(미국 캘린더) 신호로 코스피선물(한국) 거래 시 미국 휴장일 S&P500이 전일값으로 침묵 ffill돼 사용자가 원자료 '결손 多'로 오인. **§0.5 실측: ffill(engine.py:615)·assess(5% 허용)는 이미 정상 — 버그 아니라 순수 표면화(E) 갭.** 사용자 선택=표면화만. **근본수정**: `assess_data_quality`에 교차달력 carry-forward INFO 신설 — 기준 거래달력(`traded`=체결 유니버스) 개장일에 값 없어 전일값 유지되는 *신호/참조 심볼만* 표면화(거래 심볼은 자기 달력에서 결손 아니라 제외; traded 없으면 다수결 폴백). **market_calendar 미의존**(실제 날짜만)이라 데이터세션 S4와 충돌 없음. data_gap(진짜 공백)은 제외해 중복 방지. service가 `traded=_universe_symbols`·`relevant` 전달. 검증: 단위 4(교차달력 표면화·소수만·동일달력 무경고·traded기준 신호만)·result_status 불변(INFO)·e2e(S&P500만 "14일")·core 611·server 239. 주말/placeholder 행 정제는 데이터세션(B) 위임.
- 교훈: **① 0거래는 원인(신호없음 vs 자본부족)을 구분해야 정직 — 엔진 침묵 스킵을 카운트해 표면화**(단방향 낙관 파이프). ② **교차달력 '결손'은 버그가 아니라 미표면 — 실측(ffill·assess 정상)으로 확인 후 순수 표면화로 해결**(#1을 캘린더 재구현으로 오버엔지 안 함). ③ carry-forward 기준은 *체결 유니버스 달력* — 거래 심볼을 결손으로 표시하면 되레 혼란(정밀도).

### [2026-06-19] 증빙 엑셀 export (P3: 실시간 변수조정) [완료·미배포]
- 의도: 분석 결과의 핵심 변수를 챗에서 실시간 조정 → **토큰 없이 재계산** → 차트 갱신(엑셀 독립변수처럼). 매번 챗 재요청(토큰 낭비) 대신. 노코드 IR 빌더 노하우(파라미터 컨트롤 + `/ir/strategy` 재실행) 재사용.
- 계획: `param_manifest(ir)` 코어(spec SSOT 노브 추출) → 챗 결과에 `adjustable` 동봉 → 웹 `ParamControls`(디바운스 재실행) + `ChatResultView` wrapper로 결과 라이브 교체.
- 시행착오·인사이트: 핵심=재실행이 **LLM 안 거치는 `/ir/strategy`**(빌더가 이미 쓰는 경로)라 토큰0. manifest는 *현재 IR에 의미 있는* 노브만(없는 30필드 나열 금지 — over-engineering 회피)·비용/자본은 항상 노출(엑셀 노란셀 격)·None이면 엔진 기본값을 시작값으로(실효값 표기). 디바운스 400ms로 드래그 중 과다 재실행 방지. wrapper가 live 결과·IR 상태 보유 → 차트 순수 재렌더·export 버튼은 조정된 IR 사용.
- 결과 구현: `ir_engine/params.py::param_manifest(ir)`(simulate: top_n·rebalance·threshold·amount_pct·hold_days·tp·sl·trail·commission·slippage·capital·leverage·folds / select: top_n·descending). `tools.py` run_simulate·run_tool가 `res["adjustable"]` 동봉. 웹 `ParamControls`(number/select/bool·setPath로 IR 재구성·400ms 디바운스→`api.runIrStrategy`→onRun)·`ChatResultView` wrapper(live state). **검증: test_param_manifest(4)·test_chat_tools(+2)·core 328·server 339·web build+lint(신규0).** describe/relate는 노브 없어 패널 미노출(MVP — windows 리스트 편집 후순위). 미배포(PR#172 draft).

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
