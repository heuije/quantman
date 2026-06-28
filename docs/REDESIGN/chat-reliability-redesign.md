# 챗봇 신뢰성 재설계 — 결과 품질 계약 + 품질인지 오케스트레이션

> 상태: **구현 진행중** — P1·P3·P5 완료·검증·커밋 / P2·P4 잔여(아래 §10) · 정본 브랜치 `feat/chat-architecture-redesign`
> 출처: 프로덕션 대화 25건(2026-06 전수 진단, 2명·53턴) → 증상 분류 → 구조 원인 합성.
> 접근: **A — 계약-우선 점진**(사장님 결정). 빅뱅 rewrite 아님. 결과 계약을 척추로 깔고, 그 위에
> 게이트·루프정책·컴파일러결정성·그라운딩을 **독립 증분**으로 적층. 각 증분 단독 가치 + LLM-free 하니스 $0 검증.
>
> **선행 문서와의 관계 (중복 아님):**
> - [`chatbot-target-architecture.md`](chatbot-target-architecture.md) — "약한 seam ① 결과 계약(타입 없는 dict)"을
>   이미 지목하고 **렌더링 관점**(shape→render 레지스트리)에서 타입화했다. **본 문서는 그 계약을 한 단계
>   확장** — 결과 계약이 *렌더링용 형상*만이 아니라 **에이전트 루프·모델 추론용 "품질/상태"** 까지 실어야
>   한다는 것이 이번 진단의 핵심. 엔진 뼈대는 직교적(그 문서 평가 B+)이라는 결론과 일치 — 본 작업도 rewrite 아님.
> - [`chat-analysis-loop-redesign.md`](chat-analysis-loop-redesign.md) (PR#175) — `summarize_result`(형상 파생 투영)·
>   `adjust_analysis`(IR 값만 재실행)·중간결과 접기를 이미 구현. **본 문서는 그 위에** 결과의 *품질 신호*를
>   summarize에 추가하고, 루프가 그 신호로 분기하게 만든다.

---

## 0. TL;DR

- **하나의 뿌리**: 챗봇은 *모든 계층이 "성공"을 무비판으로 아래로 흘려보내는 단방향 낙관 파이프라인*이다.
  **어떤 이음새도 "이 결과가 유효/빈/퇴화/불가인지, 왜인지"를 말하지 않는다.** → 빈·엉뚱한 결과를 자신
  있게 서술하거나(B·C·D 증상), 같은 분석을 헛돈다(A 증상).
- **키스톤 = 결과 품질 계약**: 모든 도구결과가 `status`(ok·empty·degenerate·data_insufficient·infeasible) +
  기계판독 `diagnostics` + 사람용 `verdict` 한 줄을 **항상** 싣는다. 모델·UI·루프·메트릭이 전부 이걸로 분기.
- **그 위 4기둥**: ② 실행 전 타당성 게이트 · ③ 결정적·수렴 컴파일러 · ④ 품질인지 루프 · ⑤ 그라운딩·반날조.
- **엔진 수학은 대체로 건전.** 진짜 새 엔진 로직은 1D study 축(이벤트×sweep) 하나뿐 — 그것도 당장은
  "분해 플랜"으로 우회(새 축은 후속 백로그).

---

## 1. 진단 — 25개 증상 → 5개 구조적 뿌리

진단 코퍼스: `_chat_transcripts_30d.txt`(전수) / `_chat_compact.txt`(요약). 진단 도구: `app.chat_analytics`
(`railway run … transcripts`), 루브릭: `docs/chat-perf/accuracy-rubric.md`(4축 + 근본원인 계층 태그).

### 정량 신호 (30일·53턴·2명)
- 지연 심각: turn latency **p50 46s · p90 186s · max 649s**(단순 screen도 130~186s).
- 재시도 루프: rounds 분포에 5·8회 다수(컴파일 실패/0거래 후 반복).
- `error_rate=0.038`은 **과소집계** — 0거래·0%·퇴화 결과가 `ok=True`로 "성공" 카운트됨.

### 5개 뿌리 (검증 file:line 포함)

**R1. 결과 품질 계약 부재 ⭐키스톤**
엔진·도구가 0거래·0적격·퇴화 결과를 `success=True`로 반환하고, 모델은 `compact_summary`의 4개 스칼라만
받아 *"손실로 0%"* 와 *"거래가 없어 0%"* 를 구분하지 못한다.
- 근거: `tools.py:run_simulate`(검증 게이트 없이 `success=True`) · `summarize.py` 단일백테스트=4스칼라 ·
  `ChatResultView.tsx:483` 미인식 형상도 "✓ 분석 완료".
- 설명 증상: **B1**(0.00%를 "분석 완료"로) · **B2**(n_trades=1·win100%·MDD-154% garbage 표시) ·
  **B3**(eligible=0 침묵 통과) · **A3**(첫 0거래 후 재시도). + R3 헛돌이의 전제.

**R2. 비결정·비수렴 컴파일러**
- 근거(코드 확인): `ir_compiler.py:455` `client.messages.create(...)` 에 **temperature 미지정**(모델 기본값=비결정) ·
  `:482` repair 피드백이 **`is_error=True`만** 전달(사이징 같은 차단성 경고는 무시되어 수렴 실패) ·
  `:450` `messages`가 매 호출 NL만으로 시작(재시도/멀티턴 간 의도 앵커 없음).
- 설명 증상: **A1(소프트 실패)**·**A2**(같은 +50% 조건이 n_events 342↔102, "VIX 20% 이벤트"가 `describe_single`로
  오컴파일)·**A4**(구버전 raw IR 8~15회 thrash).

**R3. 크래시-온리 오케스트레이션**
`ok`는 "안 터졌나"일 뿐(품질 무관). 루프가 빈 결과를 알지 못해 맹목 재시도(dedup은 *동일 IR*만), 예외는
막다른 "잠시 후 다시"로 종료.
- 근거: `agent.py:280` 광역 `except Exception` → `ok=False` + `"분석 중 오류가 발생했어요. 잠시 후 다시 시도해 주세요."` ·
  `:274` dedup이 `_ir_sig`(동일 IR) 일치 시 **경고 텍스트만** 추가(모델이 무시 가능) · `:217·281` ok 기본 True, 예외에만 False.
- 설명 증상: **B4**(하드 실패 종료)·**B5**(무응답 대화 conv#4)·지연·ok 오표기.

**R4. 그라운딩 안 된 모델**
자기 표면을 모르고(결과뷰에 **엑셀 내보내기 버튼이 이미 렌더**·선물 레버리지는 `futures_margin_pct`로
엔진 내장) 도구결과 밖 수치를 날조하며, 결과가 **적용된 변환을 자기서술하지 않는다**.
- 설명 증상: **D1**(엑셀 "기능 없다" 거짓→Python 코드 우회)·**C1**(레버리지 ×10 손계산·"MDD-675%")·
  **C2**(뉴스 수치 과장)·**G2**(이벤트스터디 forward수익을 손익으로 혼동).

**R5. 엔진/데이터 갭 (좁음)**
- `spec.py` study 축이 **1D** → 이벤트스터디 × 파라미터 sweep을 한 IR로 표현 불가(진짜 missing-logic, **A1 하드**).
- 유니버스 오염: `universe.kind='all'`에 비트코인·M2통화량·미시간소비심리 등 매크로/크립토 혼입 → stale 경고·
  0적격(**F3**). `exclude_macro` 처리가 누락성.
- 데이터품질 미강제: `assess_data_quality`(#183, gap/stale 탐지)가 **사후 경고**일 뿐 챗 경로의 차단 전제가
  아님(코스피200선물 404/498일, **F2**).
- 선물 NAV가 마진콜 기본/하한 없이 −100% 밑으로 갈 수 있음(**B2의 -154% 수학**).

**합성**: R1이 키스톤(B의 사용자 피해 대부분 + R3 수정의 전제). R5만 일부 "새 엔진 로직"이고 나머지는
전부 **계약·신뢰성·그라운딩** 문제 — 즉 *rewrite가 아니라 이음새 정비*가 옳다.

---

## 2. 설계 원칙

1. **계약을 모든 이음새에 흐르게 한다.** 결과는 자기 품질을 스스로 서술한다(낙관 전파 종식).
2. **빠르게·정직하게 실패한다.** 불가/빈/데이터부족은 느린 실행·서술 *전에* 구체적 사유로 차단.
3. **결정성.** 같은 입력 → 같은 IR → 같은 결과(temperature=0·수렴 피드백). 신뢰의 토대.
4. **그라운딩.** 모델은 실제 표면만 약속하고, 답변 수치는 도구결과 필드로 추적 가능해야 한다.
5. **rewrite 아님 (원칙2·3).** 직교적 엔진 뼈대·기존 seam 정비물(target-architecture·loop-redesign)을 **재사용**.
   각 증분은 독립 가치 + LLM-free 하니스로 $0 검증(원칙4).

---

## 3. 키스톤 — 결과 품질 계약 (Result Quality Contract)

모든 도구결과 dict에 **항상** 다음 블록을 부착한다(엔진 결과·뉴스·inspect 등 전 경로 공통).

```python
# core: ir_engine/result_status.py  (단일 출처)
status: Literal["ok", "empty", "degenerate", "data_insufficient", "infeasible"]
diagnostics: {
    # 형상별로 채워지는 기계판독 신호 (없으면 생략)
    "n_trades": int, "eligible_size": int, "universe_size": int, "n_events": int, "n_obs": int,
    "inactive_buckets": [str], "coverage": float,            # 실제 사용된 데이터 밀도
    "applied_leverage": float, "sizing_substituted": str,    # 적용된 변환(R4 반날조)
    "stale_symbols": [str], "excluded_macro": [str],         # 데이터 위생
}
verdict: str   # 사람·모델용 한 줄. 예: "거래 0건 — 신호가 한 번도 충족되지 않음(전략/유니버스 점검)."
```

**상태 정의(결정적 판정 규칙, `classify_status(result)`):**

| status | 의미 | 판정 예 | 소비자 행동 |
|---|---|---|---|
| `ok` | 유효·해석 가능 | n_trades>0·eligible>0·n_events≥min | 정상 서술·렌더 |
| `empty` | 정상 실행, 진짜 빈 결과 | eligible=0(데이터 OK)·n_events=0 | "조건 충족 0건" 정직 고지 + 완화안 1개 |
| `degenerate` | 실행됐으나 신뢰 불가 | n_trades=1·win=100%·\|ret\|>100%·전 버킷 inactive | 경고 카드 + 원인 진단(맹목 재서술 금지) |
| `data_insufficient` | 데이터 부족/결손이 결과를 무효화 | coverage<임계·요청 구간이 데이터 밖 | "데이터 보강/구간 조정" 안내 |
| `infeasible` | 실행 전 타당성 실패 | 유니버스∩스크리너=∅·사이징/진입 비호환 | 실행 안 함·구체 사유 즉시 반환(§4.2) |

**소비 지점 4곳(전부 이 계약으로 분기):**
- **모델**: `compact_summary`가 `status`·`verdict`·핵심 `diagnostics`를 *문장 맨 앞*에 넣는다(4스칼라 대체).
  → 모델이 "0%인데 거래가 0이라 무의미"를 *안다*.
- **UI**: `ChatResultView`가 `status≠ok`이면 **경고/빈 상태 카드**(가짜 "분석 완료" 금지) — `target-architecture`
  seam ① 레지스트리에 status 분기 추가.
- **루프**: §4.4 정책이 `status`로 분기.
- **메트릭**: `ChatTurnMetric`에 `result_status` 기록 → `error_rate`가 품질을 반영(R3).

> **원칙2 가드**: 새 거대 타입 시스템을 만들지 않는다. 계약은 **얇은 dict + 단일 `classify_status`**.
> 형상별 채움은 기존 `summarize.result_shape` 분기를 재사용(중복 분기 금지).

---

## 4. 4기둥 (계약 위에 적층)

### 4.1 (기둥1 = 키스톤 §3)

### 4.2 기둥2 — 실행 전 타당성 게이트 (fail fast·honest)
백테스트/스크린 실행 *전에* 결정적 사전점검 `precheck_feasibility(ir, dataset)`:
- 유니버스 ∩ 스크리너 조건이 비공집합인가(매크로 제외 후).
- 사이징 모드 ↔ 진입 모드 호환(예: `fixed_amount` × `scheduled`는 0거래 위험 → 사전 경고/대체 명시).
- 요청 구간의 데이터 coverage ≥ 임계(아니면 `data_insufficient`).
- 실패 시 느린 엔진 실행 없이 `infeasible`/`data_insufficient` + 구체 사유 반환.
→ **A3**(첫 0거래)·**F2/F3**를 실행·서술 전에 차단. `assess_data_quality`(#183)를 여기에 **배선**(이미 존재, 미연결).

### 4.3 기둥3 — 결정적·수렴 컴파일러
- `ir_compiler.py:455`에 **temperature=0** 추가(비결정 종식 — R2·A2의 1차 원인).
- repair 피드백에 **차단성 이슈 전부 포함**(현재 `is_error`만 → 수렴 막던 사이징 경고도 전달).
- **의도 앵커**: 각 repair 메시지에 사용자 원의도 1줄 재진술(재시도 간 망실 방지).
- **표현 불가의 우아한 처리**: `expressible=False`(이벤트×sweep 등)일 때 dead-end 에러 대신 **"N단계 분해 플랜"**
  (예: 임계값별로 이벤트스터디를 순차 실행해 비교)을 반환 → 루프가 그 플랜을 실행. (R5 1D축의 새 엔진 신설 회피.)

### 4.4 기둥4 — 품질인지 루프 (orchestration)
`agent.py` 루프가 결과 `status`를 읽어 분기:
- `ok` → 서술. `empty`/`infeasible`/`data_insufficient` → **맹목 재시도 금지**, 사유 설명 + 구체 수정안 1개 제시.
- `degenerate` → 경고 + 진단(원인 후보). 회복가능 예외(컴파일/검증) → 모델 자가수정 기회, **치명 예외만** 정직 중단.
- dedup 강화: *동일 IR*뿐 아니라 *"이미 empty/infeasible 반환한 같은 의도"* 재호출 차단(헛돌이 근본).
- `ok` 메트릭을 품질 반영으로 재정의(또는 `result_status` 별도 기록).
→ **R3**(헛돌이·B4·ok 오표기·지연).

### 4.5 기둥5 — 그라운딩·반날조
- 시스템 프롬프트(`prompt.py`)에 **실제 표면 명시**: 결과 카드에 *엑셀 내보내기 버튼이 있음*(파일 생성 거절 금지) ·
  선물 레버리지는 `futures_margin_pct`로 *엔진이 적용*(손계산 금지·`applied_leverage`를 결과에서 읽어라) ·
  이벤트스터디 = *forward 수익이지 전략 손익 아님*.
- **반날조 규율**: 답변의 모든 수치는 도구결과 `diagnostics`/필드로 추적 가능해야 한다(계약이 이를 가능케).
- 뉴스: research_news 인용에 없는 수치 생성 금지(C2).
→ **R4**(D1·C1·C2·G2).

---

## 5. 증분 시퀀싱 (Approach A — 각 단독 출하·$0 검증)

| 증분 | 내용 | 닫는 증상 | 검증 |
|---|---|---|---|
| **P1. 결과 계약(키스톤)** | `result_status.py`(classify_status)·전 결과에 status/diagnostics/verdict 부착·`summarize`/`compact_summary`가 status 우선 노출·`ChatResultView` status 분기·`ChatTurnMetric.result_status` | B1·B2·B3 표면화 | analysis_diag 코퍼스에 status 단언 케이스 추가·하니스 $0 |
| **P2. 타당성 게이트** | `precheck_feasibility`·`assess_data_quality` 배선·infeasible/data_insufficient 조기반환 | A3·F2·F3 | 합성 IR(0거래·결손)→infeasible 단언 |
| **P3. 컴파일러 결정성** | temperature=0·repair 전체이슈 피드백·의도 앵커·분해 플랜 | A1(소프트)·A2 | chat_eval(claude -p)로 동일 NL 반복→동일 IR·archetype 재평가 |
| **P4. 품질인지 루프** | status 분기 정책·dedup 강화·예외 분류·ok 재정의 | B4·B5·헛돌이·지연 | 루프 단위테스트(빈/불가 status→재시도 금지)·stats 지연 델타 |
| **P5. 그라운딩** | 프롬프트 표면 명시·반날조 규율·엑셀/레버리지/이벤트 고지 | D1·C1·C2·G2 | chat_eval J-축(날조·시각화중복) 재채점 |

각 증분은 별도 PR(또는 작은 스택). P1이 P4의 전제(루프가 분기할 status를 P1이 만든다).

---

## 6. 검증 전략 (LLM-free 우선)

- **`scripts/analysis_diag.py`**(결정적 코어·$0·무제한): 코퍼스에 0거래·0적격·결손·degenerate·infeasible 케이스를
  추가해 P1·P2의 status 판정을 단언. 골든 불변.
- **`scripts/chat_eval.py`**(Pro/Max 헤드리스·API$0): P3 결정성(동일 NL→동일 IR)·P5 반날조를 LLM 부분으로 평가.
- **`chat_analytics stats`**: P4 후 지연·rounds·error_rate(품질 반영) 델타 측정.
- **라이브 E2E**: 첨부 두 예시(VIX 이벤트·PER 백테스트)와 cross-asset 디렉셔널을 재현해 **빈/불가가 정직하게**
  표시되는지 사장님 로그인 확인(로그인 게이트).

---

## 7. 스코프 경계 / Out-of-scope (후속 백로그)

- **1D study 축의 새 엔진 신설**(이벤트×sweep 네이티브) — 본 작업은 "분해 플랜" 우회까지. 다차원 질문 빈도
  ↑ 시 별도 설계(target-architecture seam과 함께).
- **플래너/실행기 상태기계(접근안 B)** — 본 작업(A)의 종착지. 루프 정책(P4)이 충분치 않다고 측정되면 전환.
- **멀티턴 기억 아키텍처**(컴파일러 호출 간 의도·이전결과 참조) — "왜 2022 뒤집혔나"·"아까 그 종목" 류.
  P3 의도앵커가 부분 완화하나 근본은 history-context(별도).
- **데이터 절대가 정합**(삼성 35만·SK 258만·+497~965% 괴리) — 메모리상 기조사(`project_chat_select_scoring`
  Finding3) 이슈. 본 신뢰성 작업과 직교 — 데이터엔진 트랙에서 독립 확인.

---

## 8. 예상 잠재 문제 (지금 구조를 안 잡으면 곧 재발)

- **데이터 모달리티 추가마다 R4 재발**: 뉴스·추정치·수급이 이미 라이브 — 계약(§3)·그라운딩(§4.5)이 일반화돼야
  모달리티마다 날조가 안 생긴다.
- **지연 누적**: Sonnet 컴파일 × 다라운드 — P4(맹목재시도 차단)가 1차, 중간결과 점진노출은 후속.
- **결정성 부채**: temperature 미설정이 모든 신규 분석유형에 비결정 상속(P3가 차단).

---

## 9. 미해결 질문 (구현 전 합의)

1. `ok`(크래시) ↔ `result_status`(품질)를 **분리 필드**로 둘지, `ok`를 품질 포함으로 재정의할지(메트릭 호환).
2. 타당성 게이트의 coverage **임계값**(예: <80% data_insufficient / 80~95% 경고 / >95% ok) — 데이터팀 합의.
3. `infeasible`/`empty` 시 루프가 **자동으로 1회 대체안 실행**할지, **사용자에게 되묻기**만 할지(헛돌이 vs 친절 균형).
4. 분해 플랜(이벤트×sweep)을 **컴파일러가** 낼지 **모델이** 낼지(결정성 vs 유연성).

---

---

## 10. 구현 진행 (브랜치 `feat/chat-architecture-redesign`)

| 증분 | 상태 | 커밋 | 검증 |
|---|---|---|---|
| **P1 결과 품질 계약** | ✅ 완료 | `e5c38a3` | classify_status 16 unit + 3 integration · core 914 pass · server chat 80 · 하니스 18/18 · web build · lint 0-new |
| **P3 컴파일러 결정성** | ✅ 완료 | `d3de7f8` | temperature=0 + 의도앵커 · compiler 26 pass · ruff |
| **P5 그라운딩** | ✅ 완료 | `37eb776` | 엑셀·레버리지·이벤트·반날조 프롬프트 · test_chat_prompt 6 pass |
| **P2 타당성 게이트** | 🔎 조사완료·잔여 | — | **조사 결과 깨끗한 엔진 버그 없음**: ① 사이징은 정상 — `engine.py:786 SIZERS.get(sz.mode, _sizer_equal)`로 `fixed_amount × scheduled`가 **올바르게 equal_weight로 폴백**. conv#23·#15의 0거래는 사이징이 아니라 **컴파일러가 0을 선별하는 유니버스/스크리너를 생성**한 것(R2 컴파일러 품질·eval 선행). ② `momentum_12_1m`은 결측 아님(`indicators.add_momentum_12_1m`·recent_days=400>룩백). conv#21 stale 심볼(비트코인)은 오히려 *더 일찍* 끝나 tail-오염도 아님 → **정확한 원인은 프로덕션 데이터 재현 필요**. P1이 둘 다 honest `empty`로 표면화 중. 성급한 추측 수정 금지(원칙3·4) |
| **P4 품질인지 루프** | 🟡 일부완료 | `d6286d5` | R3는 P1(status 노출+"재실행 말라")+P3(결정성)로 **대폭 완화**. **완료**=결과 계약 fail-safe(핫경로 분류 실패가 턴 안 깨게 격리). dedup은 기존 `seen_sigs`가 동일 IR 이미 차단. **잔여**=예외 세분(B4가 *무엇*에서 나는지 프로덕션 재현 후) |

**완료분이 닫은 뿌리**: R1(P1)·R2(P3)·R4(P5) + R3 대폭 완화(P1·P3·P4 fail-safe). **잔여(프로덕션 재현 선행)**:
conv#21 모멘텀 `eligible=0` 정확한 원인 · conv#23·#15 0거래의 컴파일러 유니버스/스크리너 품질(R2 eval) ·
B4 예외 출처 · 선물 NAV 하한(R5 엔진). 이들은 *추측 수정 금지* — 프로덕션 데이터/재현으로 원인 확정 후 진행.

**재현 방법(다음 단계)**: 프로덕션 Neon 데이터셋으로 conv#21 쿼리(`screen sector=반도체 score_ref=momentum_12_1m`)와
conv#23 쿼리를 로컬 재현해 0의 정확한 출처(스크리너 마스크/유니버스 교집합/지표 NaN)를 결정적으로 격리.

---

*(이 문서는 설계 정본. 구현 착수 시 `docs/REDESIGN/` 본 파일 + 모듈 학습원장 갱신, 진척은 draft PR/브리핑.)*
