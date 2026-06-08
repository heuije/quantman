# 인사이트 엔진 시각화 명세 (P6 — 거시문제 #3)

> [`question_layer_spec.md`](question_layer_spec.md) §9의 구현 명세. 엔진이 산출하는 **결과 shape**를
> 웹 시각화 컴포넌트로 매핑한다. 원칙: **엔진=사실 단일출처, 시각화=서술/표현**(숫자 생성·은폐 금지).
> 색·라벨 규약은 기존 `web/src/components/ResultCharts.tsx`의 토큰 `C`를 단일 출처로 계승.

## 0. 현재 상태 (배선/미배선)

| shape | 분석유형 | 컴포넌트 | 상태 |
|---|---|---|---|
| 자산곡선 | simulate 단일 | `EquityChart` | ✅ 기존 |
| 결과셋(히트맵/막대) | sweep parameter/entity/label | `SweepChart` | ✅ 기존 |
| 분포 | describe 팩터(all/list) | `SignalDistChart` | ✅ 기존 |
| 관계-IC | relate ic | `ICChart` | ✅ 기존 |
| 이벤트 | relate event | `EventStudyChart` | ✅ 기존 |
| **랭킹** | **select(P1)** | `RankedListChart` | ❌ **신규** |
| **360 리포트** | **describe single(P2)** | `ReportCards` | ❌ **신규** |
| **포트폴리오 진단** | **describe portfolio(P2)** | `DiagnosisPanel` | ❌ **신규** |
| **최적해** | **extremize(P4)** | `ExtremizeChart` | ❌ **신규** |
| **다중팩터 회귀** | **relate regression(P5)** | `RegressionChart` | ❌ **신규** |

→ 엔진·NL은 신규 5종을 산출하나 **웹이 표시 못함**(IrBuilder `ResultPanel`이 라우팅 안 함, 빌더 폼이 research query를 표현 못함). 이 명세가 그 갭을 닫는다.

## 1. 결과 dict 계약 (엔진 산출 — 컴포넌트 입력)

웹 타입은 이미 정의됨(`types.ts`: `IrSingleReport`·`IrPortfolioDiagnosis`·`IrExtremizeResult`·`IrRegressionResult`). select는 `results[]`.

- **select**: `{query:"select", as_of, universe_size, eligible_size, results:[{symbol, score, sector, metrics:{col:val|null}}]}`
- **describe single**: `{report:"single", symbol, sector, as_of, data_points, price:{last, returns:{1m,3m,6m,12m}, high_52w, low_52w, pct_from_52w_high}, risk:{vol_annualized, max_drawdown}, fundamentals:{pb_ratio, trailing_pe, ev_ebitda}}`(미수집=null)
- **describe portfolio**: `{report:"portfolio", as_of, n_holdings, holdings:[{symbol,weight,sector}], concentration:{hhi, effective_n, top_weight, top3_weight}, sector_exposure:{sector:weight}, valuation:{weighted_pb, weighted_pe}, risk:{portfolio_vol_annualized, avg_pairwise_corr}, coverage:{with_price, with_fundamentals}}`
- **extremize**: `{axis:"parameter"|"asset", reduction:"extremize", objective:{metric,direction,oos_guard}, best:{label, metric_value, perf}, ranked:[{label, metric_value}], oos_guard?:{buckets, consistency}|{error}}`
- **regression**: `{axis:"relation", relation:"regression", windows[], factor_names[], by_window:{w:{n_periods, factors:[{name,coef,se,t_stat,t_inf,ci_low,ci_high}]|null, note?}}}`

## 2. shape → 컴포넌트 → 매핑 → 커스터마이징

`C` 토큰 계승(up=#de3033 빨강·down=#1668c4 파랑·accent=#d97757·muted/grid/text). 결측=`f2`로 "—". `Box` 래퍼 재사용.

### RankedListChart (select)
- **뷰**: 수평 막대(막대=score, 정렬) + 우측 데이터테이블(metrics 컬럼·sector).
- **매핑**: 행=`results[i].symbol`(종목명 해석 가능하면 병기), 막대=score, 색=`descending` 방향(저평가=낮은값이면 역방향 강조), 부가열=`metrics`.
- **커스터마이징**: 헤더에 `eligible_size/universe_size`("4,300중 자격 1,200 → 상위 N"). sector 색=공통 섹터팔레트. score 단위·의미 캡션. 결측 metric="—".

### ReportCards (describe single)
- **뷰**: KPI 카드 그리드 — 가격/등락, returns 기간막대(1/3/6/12m), 52주 레인지바(현재가 위치), 리스크 배지(vol·MDD), 밸류 배지(pb/pe/ev).
- **매핑**: returns 막대 x=기간·y=%(부호색 up/down). 52주=`[low_52w, high_52w]` 위 `last` 마커·`pct_from_52w_high`.
- **커스터마이징**: 라벨 한글·단위(%·원·배). 미수집 펀더멘털=null→"데이터 없음" 배지(가짜 0 금지). `data_points` 적으면(<252) 장기수익 None 표기. `sector`·`as_of` 헤더.

### DiagnosisPanel (describe portfolio)
- **뷰**: 섹터노출 도넛(또는 가로 누적막대) + 집중도 게이지(HHI·effective_n) + 리스크 배지(vol·corr) + holdings 테이블.
- **매핑**: 도넛=`sector_exposure`(섹터→비중), 게이지=`concentration.hhi`(0~1), holdings=`[{symbol,weight,sector}]`.
- **커스터마이징**: 섹터 색=공통팔레트(다른 차트와 통일). 작은 섹터 "기타" 합치기. HHI 해석선(>0.25 집중) 주석. `coverage.with_fundamentals` 표기("가중밸류 n/N 종목 기준"). top_weight/top3 강조.

### ExtremizeChart (extremize)
- **뷰**: `ranked` 막대(목적값 정렬·best 강조) + OOS 일관성 미니패널(`oos_guard.buckets` 폴드별).
- **매핑**: 막대=`ranked[i].metric_value`, best=`best.label` 강조, 라벨=`objective.metric`/`direction`("샤프 최대").
- **커스터마이징**: best 테두리 강조. OOS 폴드가 나쁘면(부호 반전·분산 큼) **과최적화 경고색**. mdd 목적이면 "음수=낙폭, 0에 가까울수록 좋음" 주석. `oos_guard.error`면 안내.

### RegressionChart (relate regression)
- **뷰**: 계수 막대(행=팩터) + 95%CI 오차막대 + 0선 강조(CI가 0 미포함=유의). 윈도별 토글/패싯.
- **매핑**: 행=`factors[i].name`, 값=`coef`, 오차=`[ci_low, ci_high]`, 부호색, `t_inf`=완전유의 별표.
- **커스터마이징**: 0 기준선 강조. 윈도(5/10/20일) 선택. `n_periods` 표기. **"분석 전용(forward 미래참조)"** 배지. `factors=null`(기간부족)면 `note` 안내.

### 공통 규약 (전 컴포넌트)
- 색: 범주형(섹터·국면)=**전역 고정 섹터팔레트**(신규 `sectorColor(name)` 헬퍼 — 모든 차트 동일). 연속형 부호=up/down. 음수가좋음(mdd)=색방향 반대 금지(명시 처리).
- 라벨: 영문 지표키→한글 사전(공통 `METRIC_LABEL`), 단위 항상.
- 정직성: 엔진이 주는 `eligible_size`·`coverage`·CI·`t_stat`·`oos_guard`를 **버리지 않고** 표면화. 결측=숨김 금지("—"/"데이터 없음").
- PIT: forward 기반(regression·event·IC)·펀더멘털 PIT미태깅 = "미래참조/누출주의" 배지.

## 3. 통합 (IrBuilder)

### 3.1 research query는 빌더 폼을 우회 — 컴파일 IR 직접 실행
빌더 폼(hydrate↔buildStrategy)은 simulate 전용이라 research query를 왕복 못함. 따라서:
- `compileFromNl`이 `res.ir.query ∈ {select, describe(report), relate(regression/event), simulate(extremize)}`면, 그 IR을 **`compiledResearchIr` state에 보관**하고 빌더 hydrate를 *스킵*(또는 읽기전용 요약만).
- "분석 실행" 버튼 → `api.runIrStrategy(compiledResearchIr)` 직접 호출(폼 재구성 안 함).
- 판별: `isResearch(ir)` = `ir.query==="select" || ir.query==="describe" || ir.query==="relate" || ir.study?.reduction==="extremize"`.

### 3.2 ResultPanel 라우팅 추가 (현재 axis 기반 분기에 선행)
```
if (result.query === "select") → RankedListChart
else if (result.report === "single") → ReportCards
else if (result.report === "portfolio") → DiagnosisPanel
else if (result.reduction === "extremize") → ExtremizeChart
else if (result.axis === "relation" && result.relation === "regression") → RegressionChart
else <기존 axis 분기(time/signal/relation-ic/buckets/equity)>
```
`IrStrategyResult` 타입에 `query?`·`report?`·`reduction?`·`results?`·`best?`·`ranked?`·`holdings?`·`concentration?`·`sector_exposure?`·`valuation?`·`coverage?`·`price?`·`risk?`·`fundamentals?` optional 추가(런타임 dict는 이미 그 키를 가짐).

### 3.3 explain_ir (server) — research query 서술 교정
현재 `explain_ir`이 select/describe/relate/extremize도 "이렇게 백테스트됩니다…매주 리밸런싱"으로 오서술. query별 분기:
- select: "현 시점 스냅샷에서 …상위 N 종목 선별(백테스트 손익 미산출)".
- describe single/portfolio: "…종목 현황/포트폴리오 진단(손익 백테스트 아님)".
- relate regression: "…다중 횡단 회귀(forward 예측력, 손익 아님)".
- extremize: "…목적함수 최대/최소 셀 탐색 + OOS 과최적화 가드".
(simulate는 기존 유지.)

## 4. 구현 단계 (검증 가능 슬라이스)

- **P6a 플래그십**: explain_ir 교정(서버) + research-IR 직접실행 경로 + `RankedListChart`(select) + ResultPanel select 분기. 게이트: 로컬 빌드 + 프로덕션 "저평가 반도체주 3개"가 랭킹표로 렌더.
- **P6b 대상**: `ReportCards`(single)·`DiagnosisPanel`(portfolio) + 분기. 게이트: "삼성전자 어때"·"내 포트폴리오 진단" 렌더.
- **P6c 관계·최적**: `RegressionChart`·`ExtremizeChart` + 분기. 게이트: 회귀·최적 파라미터 렌더.
- **공통 헬퍼**: `sectorColor`·`METRIC_LABEL`(P6a에서 신설, 이후 공유).

## 5. 검증
- 로컬: `cd web && npm run build`(tsc 타입). 컴포넌트는 ResultCharts 패턴 미러라 단위 위험 낮음.
- 프로덕션: 머지→배포→브라우저(quantman.vercel.app /lab)에서 NL 4종 입력→렌더 확인(네트워크 /ir/compile·run 200 + 결과뷰).
- ⚠ 한계: 실데이터 렌더 정확성은 프로덕션에서만(로컬은 백엔드/데이터 필요). textarea 입력은 type 미입력 이슈로 JS value-setter 주입 가능(검증 도구 한계, 제품 아님).

## 6. 범위 밖 / 홀드
- 빌더 폼에 research query 입력 UI(드롭다운 신규 동사) — P6는 *NL 컴파일 결과 렌더*가 1차. 폼 직접편집은 후속.
- 히트맵 2D 파라미터(현 SweepChart는 1D 막대/라인) — 후속.
- 효율적 프런티어(포트폴리오 최적화 QP) — P4-QP 홀드와 함께.
