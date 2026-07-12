# 능력 계약(Capability Contract) 재설계 — 조용한 오답 부류의 구조적 봉쇄

> 상태: **P0~P3 구현 완료(이 PR) · P4 보류(착수 조건: resolve↔디스패치 드리프트 실측)** · 2026-07-11
> 관련: chat-structural-redesign.md(C 결과계약) · project_ir_label_categorical(광고 미이행 선례) · PR#349(D1) · PR#354(시장 필터)
>
> **구현 결과 요약**: contracts.py(원시형 4종+RunnerContract 21종+resolve_runner) · 검증기
> 단일 소비+경계 가드 · 지원_한계 프롬프트 노출 · 탈락 회계(이벤트 파일럿). 게이트 전부 통과 —
> core 775·server 549 green · 프로덕션 저장 전략 22건 dry-run 위반 0 · 전 러너 예측=실행 일치
> 테스트 · 실데이터 E2E(음수 창 이중 거부·"표본 221/656건" 커버리지 문장) · **conv#50 원문
> 재컴파일 = 음수 창 emit 없이 첫 라운드 정직 거부(repairs 0)**.
> §4.3 범위 조정: capabilities 러너 산문 섹션의 전면 파생 전환은 프롬프트 byte-안정성을 위해
> 보류 — not_supported(지원_한계) 생성 + C-* 오류 교사 + explain 키 실존 테스트가 드리프트
> 방어의 실질 teeth라 판단(어긋나면 프로덕션 동작·테스트가 즉시 깨지는 경로만 채택).

## 0. 한 줄 요약

**"컴파일러가 생성할 수 있는 것"과 "엔진이 올바르게 계산할 수 있는 것" 사이의 경계(능력 계약)가
산문으로만 존재한다.** 러너(실행 경로) 단위 **코드 선언**으로 옮기고, 검증기·컴파일러 프롬프트
광고·결과 자기서술을 그 선언에서 **파생**시켜, "정직한 거부"도 "올바른 실행"도 아닌 제3의 길 —
**그럴듯한 조용한 오답** — 을 구조적으로 막는다.

## 1. 문제 — 4겹 방어선이 전부 "형식은 검사, 의미는 통과"

파이프라인: **컴파일러(LLM) → 검증기 → 엔진(러너) → 자기서술**. 프로덕션 실측(conv#50,
floo.korea, 07-10 "급등 전 조짐" 쿼리) 한 건에서 네 겹이 전부 뚫렸다:

| 겹 | 설계 의도 | 실제 동작 (conv#50 실측) |
|---|---|---|
| ① 컴파일러 | 표현 불가면 `expressible=false` 거부 | "코스닥" 표현 수단이 없자 제약을 **조용히 탈락**시키고 `kind:all` emit(이름에만 '코스닥'). "이벤트 이전" 요구엔 **미정의 문법**(음수 windows `[-240,-120,0]`) 발명 |
| ② 검증기 | 실행 불가 IR 차단 | `windows: list[int]` 스키마·비어있음만 검사(S-event) — 음수/0 통과. **값 도메인은 어디에도 선언돼 있지 않아 검사할 수도 없음** |
| ③ 엔진 | 계산 또는 명시적 실패 | `_event_paths`: 음수 창 → 일반 위치 이벤트는 빈 슬라이스로 **조용히 탈락**, 시리즈 초반(p<\|w\|) 이벤트는 파이썬 음수 인덱스 wrap으로 **+760일 미래 수익률을 '-240일 전조'로 집계**(재현: p=10·w=-240 → ca[770]/ca[10]-1=+75.3%) |
| ④ 자기서술 | 무엇을 계산했는지 설명 | summary가 창 부호와 무관하게 **"이벤트 발생 후 forward 수익"** 고정 템플릿 — 실행이 의도에서 이탈해도 설명은 정상처럼 말함 |

### 1.1 이것이 부류인 증거 — 최근 사건 전부가 같은 어긋남의 투영

| 사건 | 어긋남 방향 |
|---|---|
| 음수 windows (conv#50, 미수정) | 구현 한계를 광고 안 함 → 컴파일러가 미정의 문법 발명 → 엔진이 조용히 오답 |
| 코스닥 미분리 (PR#354로 수정) | 어휘 자체가 없음 → 컴파일러가 제약을 조용히 탈락 (거부하지 않음) |
| IC-라벨 크래시/조용한 오답 (PR#310) | **광고했지만 러너가 미구현** — capabilities 광고 미이행 |
| D1 크로스캘린더 항상현금 (PR#349) | 정의 밖 입력 조합(혼합 달력)에서 NaN 전파가 크래시 대신 "그럴듯한 0% 전략" 생성 |
| 종가매수→익일시가매도 근사 (exit.fill 설계 진행 중) | 표현 불가를 컴파일러가 hold_days=1로 **조용히 근사**, 자기서술은 근사 사실을 절반만 고지 |

### 1.2 왜 반복되는가 — 구조적 필연

IR은 **조합형 언어**다: 표면적 = query 동사 × study 축 × universe 종류 × 신호 타입 × 파라미터
범위 (곱셈). 러너는 관용구 몇 개를 상정하고 **덧셈**으로 작성된다. 그 격차를 메우는 유일한
장치가 capabilities **산문**과 프롬프트 레시피인데:

- 산문은 **강제력이 없고**, 엔진과 **손동기화**라 반드시 어긋난다 (광고↔구현 드리프트).
- 조합 공간은 테스트로 못 덮는다 — 행복 경로 테스트가 다 green이어도 미정의 영역은 그대로.
- 미정의 영역에서 파이썬의 관대함(음수 인덱싱·빈 슬라이스·NaN 전파)이 크래시를 막아 **그럴듯한
  수치**를 만든다. 크래시였다면 하루 만에 잡혔을 것들이 숫자라서 프로덕션에서 살아남는다.

## 2. 목표 / 비목표

**목표**
1. 러너별 입력 도메인(지원 파라미터 범위·유니버스 종류·신호 타입·명시적 한계)을 **러너 옆에 1회 선언**.
2. 검증기 도메인 규칙·컴파일러 프롬프트 광고(does/use_for/**not_supported**)·러너 경계 가드를
   그 선언에서 **파생** — 광고↔검사↔구현의 3자 손동기화를 구조적으로 제거.
3. **fail-loud 기본값**: 선언 밖 입력은 계산하지 않고 사유와 함께 거부(컴파일 수리 루프가 그
   사유를 먹고 교정하거나, 유저에게 정직한 한계 안내).
4. 자기서술을 의도(IR 템플릿)가 아니라 **실행 산출물**(실제 집계된 것: 이벤트 n건·탈락 m건·창
   방향·유니버스 실구성)에서 생성.

**비목표 (over-engineering 가드)**
- 범용 제약 DSL을 만들지 않는다. 도메인 원시형은 실측 수요 3~4종(정수 범위·enum·리스트 크기·
  유니버스 집합)만. 새 원시형은 실제 결함이 요구할 때 추가.
- 블록(param_specs)·데이터(DataTypeSpec) 계층의 기존 SSOT를 대체하지 않는다 — 이 설계는
  **러너 수준**(query×study 조합)의 빈 층만 채운다.
- 러너 로직 자체의 리팩토링 없음(디스패치 표준화 제외). 계약은 껍데기다.

## 3. 현황 인벤토리 (실측)

### 3.1 러너 전수와 암묵 도메인

전수 조사(부록 A) 요약:

- **러너 21종**(R1~R21): simulate 계열 8(이벤트 백테스트·스케줄드·기간분할·스윕·최적화·레거시 2)
  + 리서치 13(select·compare·describe 3종·이벤트/IC/회귀/상관/코호트·prescribe·breadth·rotation).
- **디스패치는 SSOT가 아니다** — 3계층 + 우회 1경로에 분산:
  1차 `_dispatch_query`(run.py:174-210, query×kind×study 필드) → 2차 `run_unified`(engine.py:223-242,
  entry.mode×direction×신호타입으로 이벤트/스케줄드 분기 — 1차 라우터는 이 축을 모름) → 3차
  하위분기(run_sweep axis·run_select mode) + **run_query를 완전히 우회하는 레거시 경로**
  (`backtest_from_spec`→`run_backtest_ir`). 러너 결정에 관여하는 IR 필드는 5개(query·
  universe.kind·study.axis/event/relation_kind/reduction·entry.mode/direction·signal out_type).
- **조용한 실패 지점이 러너마다 3~7곳** — 빈 슬라이스·NaN skip·silent return·dropna·continue.
  일부는 정당한 정직 널(describe의 가짜채움 금지)이고, 일부는 도메인 위반의 증상(음수 창 탈락·
  0주 침묵 미집계·수렴실패 폴백). 부록 A가 러너별로 구분 표기.
- 신호타입 계약만 **의도된 3중화**(validate_strategy + `_root_type_error` + 엔진 인라인 게이트 —
  검증 우회 방어)가 이미 존재 — "경계 가드는 검증기와 별도로 필요하다"는 이 설계 방침의 선례.

### 3.2 계약 표면 3종의 현재 생성 경로와 손동기화 지점

전수 조사(부록 B) 요약 — 세 가지 확정 사실:

**① 러너 계층만 선언 패턴이 비어 있다.** 이 레포는 이미 두 계층에서 "단일 선언 → 파생"을
성공적으로 쓰고 있다:
- **블록 계층 = `BlockDef`** (blocks/catalog.py:18-38): out_type·slots(타입)·param_defaults·doc
  한 선언에서 검증(R1/R3)·완결(apply_defaults)·자기서술(catalog_spec→프론트·컴파일러)이 파생.
- **데이터 계층 = `DataTypeSpec`** (data/spec.py 레지스트리): 피드별 요구·출처·PIT가 게이트와
  커버리지 광고의 SSOT.
- **러너 계층 = 없음.** `_dispatch_query`(run.py)가 명령적 분기로 러너를 고를 뿐, 러너마다
  입력 계약·값 도메인·능력 산문·shape·자기서술을 담는 선언 객체가 없다.

→ 이 설계는 새 패턴 발명이 아니라 **레포 고유 패턴(BlockDef)을 비어 있는 층에 복제**하는 것.

**② 값 도메인 검사는 러너별 손코딩 소수(S-SEL·M-exit·S-futmargin·S-PORT.weights·M-window)만
존재하고, study 수치 필드는 전면 사각지대.** S-event(spec.py:699-708)는 windows의
*비어있음*만 본다 — 음수/0/거대값 전부 통과 (conv#50의 직접 원인).

**③ 능력 광고는 이중 표면 + 100% 손글.** capability_spec()(capabilities.py) 전 섹션이 손글
산문이고(모듈 docstring이 "Literal과 수동 일치" 의무를 자백), ir_compiler.py의 레시피 1~16이
같은 러너 능력을 **두 번째로** 산문 재기술한다. 자기서술(explain·execution_summary)은
capability_spec을 의미 SSOT로 재사용하는데, **이미 조용히 깨져 있다**: explain.py:347-348이
`_does("sweep_axis")`·`_does("sweep_target")`를 조회하지만 capabilities에서 해당 키가
`study_axis` 등으로 개명되어 **무성으로 빈 문자열 → raw enum 폴백 노출** — 이 설계가 막으려는
"조용한 오답" 부류가 계약 표면 자체에도 이미 실재한다.

손동기화 지점 전수(10곳)는 부록 B — 대표 예: sizing 모드 1개 추가 시 **5곳**(spec Literal·
capabilities·explain·execution_summary·summarize) 수정, 러너 1종 설명이 **5곳** 산문 중복.

## 4. 설계

### 4.0 설계 원칙

1. **BlockDef 패턴의 러너층 복제** — 새 발명 없음. 선언 1곳, 소비자(검증·광고·가드·서술) N곳.
2. **디스패치는 건드리지 않고, 디스패치 *결정*만 순수함수로 추출** — 전면 테이블화(리스크 큼)
   대신 `resolve_runner(ir) -> key`를 만들어 기존 `_dispatch_query`와 검증기가 **같은 함수**를
   소비. 결정 로직의 SSOT만 확보하면 계약을 IR 검증 시점에 조회할 수 있다.
3. **도메인 원시형은 실측 수요만** — IntRange·OneOf·MinLen·MinSymbols 4종으로 시작. 부록 A의
   암묵 가정 전수가 이 4종으로 표현됨을 확인했다(§4.1). 새 원시형은 실제 결함이 요구할 때.
4. **not_supported(명시적 한계)는 광고의 1급 시민** — "무엇을 못 하는지"를 안 쓰는 광고가
   컴파일러의 미정의 문법 발명(음수 창)과 조용한 제약 탈락(코스닥)의 직접 원인이었다.

### 4.1 RunnerContract — 러너 단위 선언

신설 `core/quant_core/ir_engine/contracts.py`:

```python
# ── 도메인 원시형 (4종 고정 — over-engineering 가드) ──────────────────────────
IntRange(min, max=None, each=False)   # 정수 범위. each=True면 리스트 원소별 적용
OneOf(*values)                        # enum 멤버십 (pydantic Literal과 이중이지만
                                      #  '러너별' 부분집합을 표현 — 예: excess는 종목2+)
MinLen(n) / MaxLen(n)                 # 리스트 크기
MinSymbols(n)                         # 해석된 유니버스 크기 (기존 S-CORR류 흡수)

@dataclass(frozen=True)
class RunnerContract:
    key: str                     # "relate.event_study" — resolve_runner가 반환하는 키
    fn: str                      # "run.py::_run_event_study" (문서·추적용)
    # 입력 도메인 → 검증기 C-*·경계 가드 파생
    domains: dict[str, Check]    # IR 경로 → 원시형. 예:
                                 #   "study.windows": (MinLen(1), IntRange(1, 750, each=True))
                                 #   "study.event_basis": OneOf("close","intraday","excess")
    signal_types: tuple[str, ...]  # 루트 신호/이벤트 노드 허용 out_type
    universe_kinds: tuple[str, ...]
    # 능력 광고 → capability_spec·컴파일러 프롬프트 파생
    does: str                    # 무엇을 계산하는가 (1문장)
    use_for: str                 # 언제 쓰는가
    not_supported: tuple[str, ...]  # 명시적 한계 + 대안. 예:
                                 #   "이벤트 *이전*(pre-event) 구간 분석 — windows는 양수(발생
                                 #    후)만. 전조 분석은 현재 미지원: 이벤트 조건을 과거 시점으로
                                 #    옮겨 정의하거나(예: '급등 120일 전' 조건) 지원 요청."
    # 자기서술 → shape 스탬프·요약 방향 문구 파생
    shape: str                   # 결과 형상 태그 (러너 return의 손글 → 계약으로 이동)
    describes: str               # 계산 의미 문구("이벤트 발생 후 +w일 forward 수익 경로")

REGISTRY: dict[str, RunnerContract] = {...}   # 21종 (P2에서 전수)

def resolve_runner(ir: StrategyIR) -> str:
    """IR → 러너 키. _dispatch_query(1차)+run_unified(2차)+하위분기(3차)의 결정 로직을
    순수함수로 추출한 것. run.py 디스패치와 검증기가 이 함수를 공유한다(결정의 SSOT).
    entry.mode/direction은 IR에서 정적으로 읽히므로 엔진 2차 분기도 여기서 해석 가능:
    simulate.event / simulate.scheduled / simulate.period_split / ... """
```

**도메인 충분성 검증(부록 A 대조):** 21종 러너의 암묵 가정 전수 — windows≥1(이벤트·IC·회귀),
folds 범위, split_period enum(현재 KeyError 크래시), top_n/top_pct 범위(기존 S-SEL 흡수),
vol_window≥1(현재 pandas 예외), 종목 최소수(상관·회귀·prescribe·excess), param_grid 비어있지
않음 — 전부 위 4원시형으로 표현된다. 표현 안 되는 가정(컬럼 존재 등 **데이터 의존** 가정)은
계약 범위 밖 — 기존 무결성 게이트(D-*)·manifest가 담당하는 층이므로 침범하지 않는다.

### 4.2 파생 1: 검증기 — C-* 규칙군 (컴파일 수리 루프의 교사)

`validate_strategy` 말미에 1블록 추가:

```python
key = resolve_runner(s)
c = REGISTRY.get(key)
if c: issues += check_contract(s, c)   # 도메인 위반 → Issue(rule=f"C-{key}", SEV_ERROR,
                                       #   message=위반 사실 + c.not_supported 관련 항목 인용)
```

- 오류 메시지에 **not_supported 문구와 대안**을 포함 — 컴파일러 repair 루프가 이 메시지를 먹고
  "음수 창 재시도" 대신 "expressible=false + 정직 안내" 또는 대안 IR로 수렴한다(현재 S-* 오류가
  이미 이 경로로 소비됨 — 신규 배선 0).
- 기존에 흩어진 러너-도메인 규칙(S-SEL 값검사·S-CORR/S-PRESCRIBE 종목수·S-REG windows)은 P2에서
  계약 선언으로 **이동·흡수**(규칙 ID는 호환 유지) — 검증기에 손코딩 도메인이 남지 않게.

### 4.3 파생 2: capabilities 광고 — 러너 섹션 생성 + not_supported 1급화

- `capability_spec()`의 `query`·`study_axis`·`study_relation_kind`·`study_event_basis` 등
  **러너 능력 섹션을 REGISTRY에서 생성** (does/use_for 그대로, `not_supported`가 새 필드로
  프롬프트에 노출). 포지션 부품 섹션(entry_mode·sizing 등 — 러너가 아니라 부품)은 현행 유지.
- `_capabilities_text`(ir_compiler.py)가 not_supported를 `〔한계: ...〕`로 렌더 — 컴파일러가
  한계를 **생성 전에** 보고 정직 거부(expressible=false) 또는 대안 제시로 기울게 한다.
- 레시피 1~16(합성 관용구)은 현행 유지 — 관용구는 "어떻게 조합하나"의 교본이고 계약은 "무엇이
  가능한가"의 사실이라 층이 다르다. 단 레시피 속 **능력 사실 서술**이 계약과 어긋나면 계약이
  이긴다(드리프트 테스트가 감시, §5 P2).

### 4.4 파생 3: 러너 경계 가드 (fail-loud 기본값)

`run_query`의 디스패치 직후 1 choke에서 같은 `check_contract`를 실행, 위반 시 계산하지 않고
`{"success": False, "status": "unsupported", "error": <not_supported 문구>}` 반환.

- 존재 이유: 검증기를 안 거치는 호출자(직접 API·저장된 IR 재실행·엔진 직접 호출) 방어.
  신호타입 계약이 이미 같은 이유로 3중화돼 있다(run.py:68-89 주석 — 선례).
- 레거시 우회 경로(`backtest_from_spec`→`run_backtest_ir`)는 flat 스펙(고정 형상)이라 도메인
  표면이 작다 — 같은 원시형으로 최소 계약 1건 등록(P2).
- 비용: dict 순회 수 회/실행 1회 — 무시 가능.

### 4.5 자기서술 — 실행 산출물 기반 결과 계약 v2

원칙: **요약·방법 문구는 "무엇을 하려 했나"(IR)가 아니라 "무엇을 실제로 집계했나"(산출물)를
말한다.** 세 가지 구체 변경:

1. **shape 스탬프의 계약 이동** — 러너 return의 손글 `"shape": "..."`를 제거하고 run_query가
   `REGISTRY[key].shape`를 스탬프(현재도 run_query가 스탬프 지점 — 원천만 교체).
2. **탈락 회계(accounting)** — 러너가 조용히 버린 것을 세어 결과에 동봉:
   `result["accounting"] = {"events_total": n, "events_dropped": {w: k}, "universe_resolved":
   {"KR": a, "US": b, "제외": c}}`. summarize_result·classify_status가 이를 소비해
   "이벤트 3,412건 중 창 -240일에서 3,401건 집계 불가" 같은 문장을 **산출물에서** 생성.
   파일럿=이벤트 스터디(피해 실측 러너), 이후 러너별 점진(§5 P3).
3. **describes 문구의 계약 이동 + 이미 깨진 소비자 수리** — summarize의 방법 문구가 계약
   `describes`를 참조. explain.py의 무성 깨진 조회(`_does("sweep_axis")` 등 3건 —
   capabilities 키 개명 후 빈 문자열 폴백)를 수리하고, **"광고 키를 조회하는 모든 소비자는
   키 부재 시 fail-loud"** 계약 테스트로 재발 봉쇄.

## 5. 이행 단계 (각 단계 독립 배포·검증 가능)

| 단계 | 내용 | 크기 | 완료 게이트 |
|---|---|---|---|
| **P0 즉시 가드** | conv#50 부류 직접 봉쇄(레지스트리 없이): S-event에 `windows 원소 ≥1` 도메인 검사+정직 안내문 · `_event_paths` 음수/0 방어 가드 · 이벤트 요약에 창·탈락 수 명시 · explain.py 깨진 `_does` 키 3건 수리 | 소 (4파일) | conv#50 재현 쿼리가 "정직한 한계 안내+대안"으로 응답. 음수 창 red→green 테스트 |
| **P1 파일럿** | contracts.py(원시형 4종+RunnerContract+resolve_runner) · 이벤트 스터디 1종 등록 · 검증기 C-* 블록 · run_query 경계 가드 · capabilities 이벤트 항목 파생 전환 · P0 손가드를 계약 선언으로 대체 | 중 | resolve_runner ↔ 실행 러너 일치(코퍼스 전수 assert) · 광고 파생 계약 테스트 |
| **P2 전수 이행** | 나머지 20종 계약 등록(부록 A 암묵 가정의 도메인 승격 — split_period enum KeyError·vol_window 등 실결함 포함) · 흩어진 S-* 도메인 규칙 흡수 · capabilities 러너 섹션 전체 파생 · **프로덕션 저장 전략 전수 dry-run**(기존 전략이 C-*에 걸리지 않음 확인) | 대 (기계적) | 저장 전략 위반 0 · 전 스위트 green · 드리프트 테스트(광고=계약) |
| **P3 자기서술 v2** | 탈락 회계 러너별 확대 · summarize/status의 산출물 소비 전환 · shape 계약 이동 | 중 | "조용한 탈락"이 사용자 문장으로 표면화되는 E2E 3종 |
| **P4 (보류)** | 디스패치 자체의 레지스트리 순회 전환 · 레거시 우회 경로 통합 | — | **착수 조건: resolve_runner↔디스패치 드리프트가 실측될 때만** (현재는 일치 테스트로 충분 — overthinking 가드) |

P0는 이번 진단의 직접 수정이므로 승인 시 P1과 분리해 먼저 내보낼 수 있다(사용자 피해 진행 중
결함). P1 이후 각 단계는 선행 단계에 의존하지만 서로 다른 PR로 랜딩한다.

## 6. 리스크·트레이드오프

| 리스크 | 판단·완화 |
|---|---|
| **기존 저장 전략이 새 C-* 규칙에 거부** | 도메인은 "엔진이 이미 옳게 계산 못 하던 영역"만 선언하므로 잘 돌던 전략은 정의상 도메인 안. 그래도 P2 게이트에 프로덕션 저장 전략 전수 dry-run 포함 — 위반 발견 시 도메인을 넓히는 게 아니라 *그 전략이 조용한 오답이었는지* 먼저 판정(D1 사례: 항상현금 전략은 '잘 돌던' 게 아니었다) |
| **resolve_runner ↔ 실제 디스패치 드리프트** (결정 로직 중복의 신형) | 러너가 result에 contract key를 스탬프하고, 코퍼스(analysis_corpus) 전 IR에 대해 resolve_runner 예측=실행 러너 일치를 파라미터화 테스트로 잠금. 불일치=테스트 실패=배포 차단 |
| **과잉 강성 — 정당한 쿼리 거부** | not_supported에 대안 제시 의무(리뷰 체크리스트) + 거부 메시지가 컴파일 repair 루프로 들어가 대안 IR 유도. 도메인 상한(예: windows≤750)은 데이터 한계 실측치로 정하고 근거 주석 필수 |
| **pydantic Literal과 OneOf 이중 선언** | Literal은 *스키마 전체* 어휘, OneOf는 *러너별 부분집합* — 층이 다르다. 전체 어휘와 같을 땐 OneOf 생략(Literal에 위임)을 규약으로 |
| **계약 선언 자체의 부패** (선언≠구현 신형 드리프트) | 계약은 실행 코드가 소비(경계 가드)하므로 광고-전용 산문과 달리 어긋나면 **테스트가 아니라 프로덕션 동작이 즉시 깨진다** — 산문 대비 구조적으로 부패 저항. 추가로 P1 일치 테스트 |
| 검증·가드 성능 | dict 순회 수 회/요청 — 측정 불요 수준 |

## 7. 성공 기준

1. conv#50 원문 쿼리 재실행 → 조용한 오답 0: "이벤트 이전 구간 분석은 아직 미지원 + 대안"
   정직 응답 (P0/P1).
2. 광고↔검사↔구현 드리프트가 **테스트로 감시되는 사실**이 됨: capabilities 러너 섹션=레지스트리
   파생, `_does` 키 부재 fail-loud, resolve_runner 일치 테스트 (P1~P2).
3. "조용히 버려진 것"이 사용자 문장으로 표면화 — 탈락 회계가 요약에 등장 (P3).
4. 새 러너/새 study 축 추가 절차가 "계약 1선언 등록"으로 수렴 — 광고·검증·가드·shape 4곳
   손수정이 사라짐 (P2 이후 구조 효과).

## 부록 A. 러너 인벤토리 전수 표 (2026-07-11 main 2d3f2fa 실측)

디스패치 결정 필드: query · universe.kind · study.{axis,event,relation_kind,reduction} ·
entry.mode/direction · signal out_type (5개 필드 · 2파일 분산).

| # | 러너 (file:line) | 디스패치 | 암묵 도메인 가정(미검증) | 조용한 실패 지점 |
|---|---|---|---|---|
| R1 | run_strategy_ir (run.py:126) | simulate+axis=none | — (위임층) | 선물 롤 패널 미수집 시 silent no-op(:116) |
| R2 | run_unified 이벤트 경로 (engine.py:211) | entry.mode=on_signal | fill=typical→High/Low·trail_atr→atr_14·amount_krw+외화→환율 시계열·행≥2 | `_open` 예산≤0/가격 NaN silent return(:380)·**주식 0주 침묵 미집계**(:407, 선물은 capital_starved 집계와 비대칭)·FX asof 부재→진입 보류·기간말 무종가→손익0(:579) |
| R3 | _run_scheduled (engine.py:827) | entry.mode≠on_signal | **vol_window≥1 미검증(pandas 예외)**·top_pct 범위(직접 호출 시)·alpha 행≥2 | evaluate 예외→_empty(:852)·타깃 0계약 조용 탈락(:1086)·그룹캡/턴오버댐프 무재정규화 변형·refill 후보 소진 시 빈 슬롯 |
| R4 | run_backtest_ir (backtest.py:37) | **run_query 우회**(backtest_from_spec·sweep) | 통화=6자리숫자 휴리스틱(:64) | 0주 silent return(:143)·price≤0 청산 시 **손익 없이 조용히 소멸**(:163) |
| R5 | run_portfolio_ir (backtest.py:262) | 라이브 호출자 없음(패리티 테스트 전용) | — | R2와 동형 |
| R6 | run_period_split (run.py:492) | simulate+axis=time_fold | **split_period 미지값→KeyError 크래시**(:507)·folds 범위 | 빈 폴드 필터·무거래 구간은 경고 표면화(정직) |
| R7 | run_sweep (run.py:287) | simulate+axis∈{parameter,entity,label} | param_grid 값 존재·assets 존재·label 존재 | 실패 셀={"error"} 버킷으로 조용히(:316,331) |
| R8 | run_extremize (run.py:404) | simulate+reduction=extremize | axis∈{parameter,entity} | metric NaN→-inf 최악 처리(:456)·성공 셀 전멸 시 _empty |
| R9 | run_select (run.py:825) | select+mode≠compare | score 패널 비어있지 않음·eligible>0 | screener 탈락·NaN score 조용 제외(:878)·display 부재→None |
| R10 | _run_compare (run.py:766) | select+mode=compare | — | 컬럼 부재→None·30종 캡(상위만) |
| R11 | run_prescribe (run.py:1079) | prescribe | 종목≥2·정렬행≥20 | 수렴 실패→동일가중 폴백(:1122) |
| R12 | run_breadth (run.py:1146) | breadth | — | len<21 종목 조용 탈락(:1160) |
| R13 | run_rotation (run.py:1211) | rotation | 종목≥2·월≥2 | len<25 조용 탈락·8월×15섹터 캡 |
| R14 | run_describe_report (run.py:931) | describe+single | syms[0] 대상 | 컬럼 부재→정직 None(가짜채움 금지 — 의도된 설계) |
| R15 | run_portfolio_diagnosis (run.py:1008) | describe+portfolio | weights 양수 | Close 없는 보유종목 조용 제외(:1017) |
| R16 | _run_signal_study (run.py:550) | describe+all/list | target_node 필요·**전컬럼 숫자**(범주형 명시 거부 — PR#310 fail-loud 선례) | 빈 파트 제거 |
| R17 | _run_entity_cohort (run.py:367) | relate+axis=entity | 종목≥2 | 종목별 실패={error} 버킷 |
| R18 | **_run_event_study** (run.py:1435/1334) | relate+event≠None | **windows 원소≥1 미검증** ← conv#50 · basis enum | `_event_paths`(:1304): 창이 끝 넘으면 탈락·**음수 창=빈 슬라이스 탈락+초반 이벤트 음수 인덱스 wrap 오답**·anchor NaN→탈락 |
| R19 | _run_ic_study (run.py:595) | relate+kind=ic(기본) | 종목≥2·target_node·windows≥1 미검증 | ic.dropna 날짜 탈락·종목축 라벨은 명시 거부(PR#310) |
| R20 | _run_regression_study (run.py:698) | relate+kind=regression | factors≥1·종목≥2·windows≥1 미검증 | 자유도 부족일·특이행렬 날짜 continue(:737,742) |
| R21 | _run_correlation_study (run.py:648) | relate+kind=correlation | 종목≥2 | 결측 쌍→None |

※ 신호타입 게이트는 3중화(validate_strategy + run.py:68-89 `_root_type_error` + 엔진 인라인) —
의도된 중복(검증 우회 방어)이며 §4.4 경계 가드의 선례.

## 부록 B. 계약 표면 맵 (2026-07-11 실측)

### B.1 검증기 — 값 도메인을 보는 규칙은 소수 손코딩뿐

- 블록 메타(blocks/validate.py): R0~R3(형태)·**M-window(노드 param window/lag/days ≥1 — 유일한
  값 도메인)**·M-degen·apply_defaults. **study.windows에는 M-window가 미적용**(노드 param이 아님).
- 전략 구조(spec.py:469-828): S-*/M-* 28종 중 값 도메인 검사는 S-futmargin·S-SEL(top_n/top_pct)·
  M-select·M-exit(부호)·S-PORT(weights>0) 5종뿐. **S-event(:699-708)=이벤트 condition 형태·
  windows 비어있음·excess 종목수만 — 값 도메인 없음.**

### B.2 능력 광고 — 이중 표면·100% 손글

- capability_spec()(capabilities.py:13-275) 전 섹션 손글 산문. 모듈 docstring이 "spec Literal과
  수동 일치" 의무 자백(:8). 러너 설명은 `query` 섹션(:162-189)+study_* 섹션들.
- 두 번째 표면: ir_compiler.py 레시피 1~16(:296-400) + `<units_and_costs>`(:288)+`<rules>`(:420)
  +few-shot(:26-158)이 능력 사실을 산문 재기술.
- 삽입 경로: compile_service.py:145 → _system_prompt → `_capabilities_text`(:199-211) →
  `<capabilities>` XML(:259-262).

### B.3 자기서술 — 의도 기반과 산출물 기반의 혼재

| 계층 | 원천 | file:line |
|---|---|---|
| summarize_result(모델용 투영)·result_shape | **산출물** | summarize.py:191-446·19-62 |
| classify_status(status/verdict) | **산출물** | result_status.py:69-211 |
| execution_summary·methodology_brief | **의도(IR)** | execution_summary.py:26-112 |
| explain_ir·narrative | **의도(IR)**·capability_spec 재사용(`_CAP` explain.py:30) | explain.py:354-529 |
| attach_methodology·data_source_line | 의도(IR)+손글 소스 문자열 | tools.py:624-694 |
| shape 태그 | **러너별 손글 return** → run_query 스탬프 | run.py:159-160·398·1271 |

### B.4 기존 선언 인프라(각 층의 SSOT — 러너층만 부재)

BlockDef(블록: 검증·완결·자기서술 파생, catalog.py:18-38) · DataTypeSpec(피드, data/spec.py) ·
field_contract/unknown_field_issues(스키마 자동 파생, spec.py:396-460) · needed_columns/symbols
(노드 파생+손코딩 2건, spec.py:831-906) · PARAM_SPECS(블록 UI, param_specs.py). **러너 선언: 없음.**

### B.5 손동기화 지점 10곳 (같은 사실의 산문 중복)

1. enum 의미 5중(예: sizing.mode — spec Literal↔capabilities↔explain↔execution_summary↔summarize)
2. query 동사(러너) 설명 5중(capabilities↔레시피 7~16↔explain↔summarize↔러너 shape 손글)
3. entry_mode 의미 5중
4. 청산 부호 규약 4중(M-exit↔capabilities knobs↔explain↔execution_summary)
5. 비용=분수·% 임계 규약 3중(프롬프트↔후보정 코드↔few-shot)
6. 선물 롤·통화 "미적용" 정직성 4중
7. fill 의미 3중
8. 데이터 출처 문자열 3중(tools.py:627 주석이 수동 동기화 의무 자백)
9. atr_14 암묵 의존(러너 구현↔needed_columns 손코딩)
10. **이미 깨진 사례**: explain.py:293·347·348이 `_does("period_split"/"sweep_axis"/"sweep_target")`
    조회 — capabilities 키가 `study_split_period`/`study_axis` 등으로 개명되어 **무성 빈 문자열
    → raw enum 노출**. 이 설계가 막으려는 부류가 계약 표면 자체에 실재하는 증거.
