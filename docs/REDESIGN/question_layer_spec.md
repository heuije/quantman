# 인사이트 엔진 명세 — 질문 큐브 (자연어 질문 → 데이터 분석 → 답변 → 시각화)

> **문서 위상.** [`block_ir_spec.md`](block_ir_spec.md)의 **확장 명세**. block_ir_spec이 블록
> IR(데이터→신호→포지션→성과→시뮬 + 펼침·비교)을 정의한다면, 본 문서는 그 위에 **"질문"을
> 1급 평면으로 승격**해 플랫폼을 *백테스터 → 데이터 기반 리서치 답변 엔진*으로 확장한다.
> 기존 §7(SIMULATE)·§8(SWEEP)·§9(COMPARE)를 한 대수의 인스턴스로 흡수한다.
>
> **제품 한 줄.** **자연어 질문 → 데이터 기반 분석 → 답변 (→ 시각화).** 모든 기능은 4개 직교
> 축(대상·동사·펼침·데이터)의 한 좌표다. AI(LLM)는 숫자를 지어내지 않고, **엔진이 계산한 사실
> 위에 평문 답을 조립**한다(검증 가능 = 신뢰성 차별점, 4원칙 §4 정합).
>
> **전제(block_ir_spec §1.2 계승).** 전원 알파 테스터 → **하위호환 어댑터 없이 백지 재설계**.
> 옛 `sweep`/`period_split`은 `question`으로 깨끗이 흡수.
>
> **3대 거시문제 통합.** ①엔진 능력 = 질문 큐브 컴퓨트. ②자연어 = 큐브의 *메인 입구*. ③시각화 =
> 답의 마지막 층. 본 문서는 ①②의 골격을 정의하고 ③은 출력형 분류만 예약.

---

## 1. 뼈대 — 4개 직교 축 (각 축이 MECE)

모든 기능 = 아래 좌표의 한 칸. 빠짐·중복 없음.

- **축A · 대상(SUBJECT) — 무엇을 분석하나**: ①종목군(전체/리스트) ②단일 종목 ③내 포트폴리오 ④시장·매크로
- **축B · 분석 동사(VERB) — 무엇을 묻나**: ①SELECT 고르기 ②DESCRIBE 살펴보기 ③RELATE 관계·예측 ④SIMULATE 모의매매
- **축C · 펼침(STUDY) — 어떻게 비교·최적화**: axis{none·parameter·entity·label·time_fold} × reduction{enumerate·contrast·consistency·**extremize**}
- **축D · 데이터(MODALITY) — 무슨 재료**: ①가격 ②펀더멘털(재무·밸류) ③매크로 ④분류 │ *신규* ⑤추정치(컨센서스·목표가) ⑥이벤트(실적발표일) ⑦수급·소유(외인·기관·내부자) ⑧뉴스/텍스트

### 1.1 핵심 기능 매트릭스 = 대상(A) × 동사(B)

| 대상 ↓ \ 동사 → | SELECT 고르기 | DESCRIBE 살펴보기 | RELATE 관계·예측 | SIMULATE 모의매매 |
|---|---|---|---|---|
| **종목군** | 스크리닝 "저평가주 3개" 〔신규〕 | 팩터 분포·요약 〔기존〕 | 팩터 예측력 IC 〔기존〕 | 전략 백테스트 〔기존〕 |
| **단일 종목** | — | "이 종목 어때" 360 리포트 〔신규〕 | "왜 올랐나"·실적후 확률·성장 전망 〔신규〕 | 단일종목 룰 백테스트 〔기존〕 |
| **내 포트폴리오** | 리밸런싱 후보 〔신규〕 | 진단(집중·섹터노출·리스크) 〔신규〕 | 손익 요인·리스크 기여 〔신규〕 | 보유 기반 시나리오 시뮬 〔신규〕 |
| **시장·매크로** | — | 시황·섹터 히트맵 〔신규〕 | 레짐 분석 〔기존〕 | — |

**펼침(축C)은 매트릭스 위 오버레이**(별도 기능 아님): 경쟁사 비교=DESCRIBE+contrast(entity); CPI 레짐 포트폴리오 최적화=SIMULATE+label(레짐)+extremize; 파라미터 최적해=SIMULATE+parameter+extremize.

### 1.2 완전성 — 사용자 예시 = 큐브 좌표
저평가주=종목군×SELECT / 삼성전자 어때=단일×DESCRIBE / 왜 올랐나=단일×RELATE+뉴스 / 실적후 확률=단일×RELATE+이벤트 / 성장전망=단일×RELATE+추정치 / 경쟁사비교=종목군×DESCRIBE+contrast / CPI 포트폴리오=종목군×SIMULATE+레짐+extremize / 내 포트폴리오 진단=포트폴리오×DESCRIBE. → 빠짐없음.

---

## 2. 근본 모델 — 4평면

```
평면1  DATA        (엔티티,시간,필드) 관측 패널 — 축D 데이터 수급.        [data_fetcher + 신규4종]
평면2  EXPRESSION  블록 대수 → 값/조건/라벨 패널.                          [block_ir_spec §3 — atomic]
평면3  QUESTION    표현식을 답으로 환원하는 동사(축B) + 대상(축A).         [본 문서 — 1급 신규]
평면4  STUDY(meta) 질문을 축으로 fan-out 후 환원(축C).                     [§8/§9 일반화]
```
질문 = **(대상 × 동사) × (펼침)** 위에서 평면2 표현식을 평가. 평면2는 이미 원자적; 본 문서는 평면3·4를 1급화하고 평면1을 4종 확장.

### 2.1 동사(축B) — MECE 4종
| 동사 | 묻는 것 | 답 모양 | block_ir_spec | 현재 |
|---|---|---|---|---|
| SELECT | 어떤 엔티티가 상위인가 | 랭킹 리스트 | — (포지션층 잠재) | ❌ 종착질문 부재 |
| DESCRIBE | 이 값의 분포·요약 | 통계/분포 | §9 분포 | ✅ target=signal |
| RELATE | 값↔결과 관계(corr/회귀/이벤트/원인) | 관계+유의성 | §8 시간축·§9 | ◐ IC·단일회귀만 |
| SIMULATE | 매매 시 손익 | 자산곡선·성과 | §7 | ✅ 성숙 |

### 2.2 펼침(축C) — MECE (axis × reduction); §8 SWEEP + §9 COMPARE + period_split 흡수
reduction: enumerate(✅§8) · contrast(✅§9) · consistency(✅period_split) · **extremize(❌ 신규 = 최적해)**.
※ 이벤트 스터디 재분류: 옛 `axis=time` → `query=relate, kind=event`(축이 아닌 동사 모드).

### 2.3 대상(축A) — universe 종류 확장
기존 `universe.kind`(single/list/all)에 **`portfolio`(내 보유)** 추가. single=단일종목 리포트 대상, portfolio=진단 대상. 시장·매크로는 universe 밖 매크로 참조로 표현.

---

## 3. IR 스키마 (R0 — 백지 흡수, 호환 alias 없음)
`core/quant_core/ir_engine/spec.py`. 클래스명 `StrategyIR` 유지(churn 회피, 개념상 QueryIR). 옛 top-level `sweep`·`simulation.period_split`/`split_dates`는 제거→`question`/`study` 흡수.

```python
class Objective(BaseModel):            # reduction=extremize 전용
    metric: Literal["sharpe","cagr","sortino","calmar","mdd","total_return","ic_mean"] = "sharpe"
    direction: Literal["max","min"] = "max"
    oos_guard: bool = True             # in-sample argmax를 OOS/WF 일관성으로 교차검증(과최적화 가드)

class Study(BaseModel):                # 평면4 — 옛 sweep + period_split 흡수
    axis: Literal["none","parameter","entity","label","time_fold"] = "none"
    reduction: Literal["enumerate","contrast","consistency","extremize"] = "enumerate"
    param_grid: list[ParamAxis] = []   # axis=parameter
    assets: list[str] = []             # axis=entity
    label: Optional[Node] = None       # axis=label (out_type=label 강제)
    folds: int = 4; split_dates: list[str] = []   # axis=time_fold
    objective: Optional[Objective] = None          # reduction=extremize

class SelectSpec(BaseModel):           # query=select
    as_of: str = "latest"; top_n: Optional[int] = None; top_pct: Optional[float] = None
    descending: bool = True; display: list[str] = []   # 근거 컬럼(pb_ratio·trailing_pe·시총)

class RelateSpec(BaseModel):           # query=relate
    outcome: Optional[Node] = None     # 결과변수(미지정=forward 수익)
    kind: Literal["ic","regression","event","driver"] = "ic"   # driver="왜 움직였나"(뉴스+가격)
    factors: list[Node] = []           # kind=regression: 다중/횡단 설명변수
    windows: list[int] = [5,10,20]; event: Optional[Node] = None
    event_basis: Literal["close","intraday","excess"] = "close"

class Universe(BaseModel):             # 축A
    kind: Literal["single","list","all","portfolio"] = "single"   # +portfolio(내 보유)
    symbols: list[str] = []; screener: Optional[dict] = None; exclude_macro: bool = True

class StrategyIR(BaseModel):           # ≡ QueryIR
    query: Literal["select","describe","relate","simulate"] = "simulate"  # 축B — 기본=현 동작
    universe: Universe                 # 축A
    signal: Node                       # 평면2 — study 대상 표현식(이름은 연속성 위해 signal)
    study: Study = Field(default_factory=Study)   # 축C·평면4
    select: Optional[SelectSpec] = None; relate: Optional[RelateSpec] = None
    position: Optional[Position] = None; simulation: Optional[Simulation] = None  # query=simulate 전용
```
**클린 마이그레이션**: 저장 전략·web emit·NL emit·golden 픽스처를 새 형태로 일괄 재작성. `query` 미지정=simulate라 기존 의미 보존. 1회 마이그레이터 + golden_backtest 회귀로 안전망.

---

## 4. 디스패치 + 답변 파이프라인
**파이프라인(모든 칸 공통)**: 자연어 → 〔#2 컴파일〕 IR → 〔엔진〕 사실 계산 → 〔답변층〕 LLM 평문 조립 → 〔#3〕 시각화(나중).

`run.py`에 최상위 `run_query(ir, dataset)`: `study`가 비-기본이면 `_run_study`(옛 sweep/compare/period_split 통합), 아니면 동사 분기(select→`run_select` 신규 / describe→흡수 / relate→흡수+심화 / simulate→`run_unified`). `_run_study`는 base 동사를 thunk로 받아 축으로 펼치고 reduction으로 접음(enumerate/contrast/consistency/**extremize**).

**답변층(신규 — 평면 위)**: 엔진의 구조화 결과(랭킹/통계/관계/곡선)를 받아 LLM이 평문 답 조립. **엔진=사실의 단일 출처, LLM=서술자**(숫자 생성 금지). 출력형별 템플릿.

---

## 5. 데이터 축(D) 수급 — 신규 4종 (데이터 갭 분석의 부재 범주)
[[reference_data_gap_analysis]]의 부재 5범주 중 4종을 채운다. **사용자 확인: 모두 수급 가능.** 단 원칙 준수:
- **추정치**: 컨센서스 EPS·목표가·성장전망 (네이버/FnGuide 공개 등) → 성장전망·목표가.
- **이벤트**: 실적발표일·배당락 (DART 등) → RELATE(event)="실적후 확률".
- **수급·소유**: 외인·기관 순매수(KRX)·내부자·13F → 수급 분석(한국 단일 최강 알파).
- **뉴스/텍스트**: 종목 뉴스·공시 → RELATE(driver)="왜 올랐나" + 답변층 내러티브.
- **데이터 원칙(위배 금지)**: ⓐ **시점정합(PIT)** — "그때 알 수 있던 값"만(목표가·발표일·뉴스 as-of). ⓑ **소스 1개**(fallback 금지). ⓒ **상용화 시 재배포 라이선스**(무료 공개소스는 유료 제품 재판매 제약 검토). `block_ir_spec §6 integrity` 게이트를 신규 피드에도 강제.

---

## 6. 거시 로드맵 (MECE·의존성 순; 각 단계 측정 가능 게이트)

> 단계마다 **자연어→엔진→답변** 수직 슬라이스로 완성·검증. NL 컴파일러 V/T/E·답변층·4계층 점검은 전 단계 관통(교차).

| 단계 | 내용 | 큐브 좌표 | 데이터 | 게이트 |
|---|---|---|---|---|
| **P0 토대** | `question=(query,study)`+`universe.portfolio` 1급화, sweep/compare 흡수, `run_query` 디스패치 | (구조) | — | golden_backtest 무변경(simulate 동일) |
| **P1 첫 슬라이스 + 답변골격** | SELECT 동사(as-of)+최소 LLM 답변. "저평가주 N개" | 종목군×SELECT | ✅ 보유 | golden screen + NL eval archetype + 브라우저 |
| **P2 대상 확장** | 단일종목 360 리포트(DESCRIBE+RELATE 조립)·포트폴리오 진단(universe=portfolio) | 단일·포트폴리오 | ✅ 대부분 | 리포트 골든 + 진단 정합 |
| **P3 데이터 4종 수급** | 추정치·이벤트·수급·뉴스 (PIT/단일소스/라이선스). 해금: 성장전망·실적후확률·왜올랐나·수급 | 축D 확장 | 신규 수급 | 피드별 PIT 무결성 테스트 |
| **P4 펼침 완성·최적화** | extremize 환원(목적함수 argmax+과최적화 가드)·포트폴리오 평균-분산(QP) | +최적화 | ✅ | 회귀 정합·OOS 일관성 |
| **P5 RELATE 심화** | 다중팩터·횡단 회귀+신뢰구간/t값 | RELATE 강화 | ✅ | statsmodels 대조 |
| **P6 시각화(#3)** | 출력형별 시각화(랭킹표·분포·관계·곡선·프런티어·대조) | 답변 마지막 층 | — | 별도 거시단계 |

**의존성·근거**: P0가 모든 것의 토대(최우선). P1=가장 얇은 데이터-완비 슬라이스로 *답변 패러다임* 증명. P2=데이터-완비 고수요 대상(소매 #1·#5). P3=데이터 트랙(가장 무거움, 소매 #1·#2 해금) — 엔진 컴퓨트(P4·P5)와 **병행 가능**. P4·P5=순수 컴퓨트(데이터 독립). P6=마지막.
**R4 스코프 확장 주의(P4)**: 포트폴리오 최적화는 `block_ir_spec §10.1`이 "QP 솔버 필요·본 일반화로 안 풀림"으로 보류했던 것 → 의도적 확장(QP 의존성 scipy/cvxpy 추가). P4 진입 시 별도 재확인.

---

## 7. 4계층 계약 적용 (불변식 — 리서치 query의 3계층 적응)
| 계층 | 리서치 query(SELECT/DESCRIBE/RELATE) | SIMULATE |
|---|---|---|
| UI(노코드+NL) | 빌더 + NL(메인 입구) | 동일 |
| 엔진 | 동사별 러너 | §7 |
| 데이터 | 축D | 동일 |
| 자동매매 | **N/A → 3계층 적응** | 4계층 전부 |
리서치 query는 *실행 대상 아님* → 3계층. 다리: 스크리닝/진단 결과 → 전략 universe(향후). **알림·모니터링은 범위 밖**(사용자 결정).

## 8. NL 컴파일러 통합 (#2 — 전 단계 관통, V/T/E)
컴파일 타깃 = **(대상, 동사, 펼침, 표현식)** 직교 슬롯. 단계마다: **V** capability_spec에 신규 enum(query·reduction·universe.portfolio·relate.kind) 추가+`test_capability_coverage` 강제 / **T** idiom 쿡북에 동사별 레시피 / **E** archetype을 *의미까지* 단언.

## 9. 시각화 정합 (#3 — 출력형 분류 기반, P6)
entity list→랭킹표 / distribution→히스토그램 / relation→산점도+회귀선 / equity→자산곡선 / resultset→히트맵 / frontier→효율적 프런티어 / contrast→그룹 막대+유의성.

## 10. 검증 전략
golden_backtest 회귀(P0 무변경) · golden screen(P1) · 리포트/진단 골든(P2) · 피드 PIT 무결성(P3) · 회귀 statsmodels 대조(P4·P5) · capability 커버리지 가드 + NL eval(전 단계) · 브라우저 답변 확인.

## 11. 범위 밖 / 리스크
- **범위 밖**: 알림·모니터링(제외). 시각화는 P6(거시 #3).
- **리스크**: ⓐ R0 마이그레이션 폭(sweep/period_split 전 경로 동시 수정 — golden 회귀 안전망, 1 PR 전수). ⓑ 데이터 4종 PIT 누설·라이선스(§5 원칙·integrity 게이트). ⓒ P4 QP 의존성. ⓓ 답변층 LLM이 사실을 왜곡/추가하지 않게(엔진=단일 출처, 답변=서술만 — 인용·검증 테스트).
