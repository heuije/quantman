# 설계: 세부조건(스크리너) 종목추가 팝업 이전 + 유니버스 tradable 전용

- 날짜: 2026-06-01
- 상태: 설계 확정 (Phase 1 구현 plan 작성 단계)
- 범위: `platform/` — web · core · server (로컬앱 = Phase 2, 본 spec 밖)

---

## 0. 한눈에

원래 "스크리너 컨트롤을 팝업으로 이전"이었으나, 검증 중 4갈래로 확장된 교차 변경.

1. **세부조건 = 선택 종목 ∩ 조건** — 종목추가 팝업 내 "세부조건 설정"으로 이전, 선택 종목 한정(전체시장 스크리닝 제거).
2. **동적/정적 택일** — `refresh: each_rebalance | once_at_start`.
3. **모든 진입방식 적용 (Phase 분리)** — 백테스트는 정기·상시·이벤트 전부. 라이브는 정기·상시만 자동(preview 경유). **이벤트+세부조건 전략의 모의/실전 전환은 가드로 차단**(로컬앱 패리티 = Phase 2).
4. **유니버스 tradable 전용 + 서버 가드** — 피커는 매수/매도 가능 종목만, 서버는 비매매 유니버스의 모의/실전을 거부.

부수 효과(의도된 개선): IR이 더 atomic — 스크리너가 배타적 `kind` 에서 직교 선택 필드로([[feedback_atomic_strategy_primitives]]).

---

## 1. 배경 / 현재 동작

- `universe.kind` 는 `single | list | all | screener` 판별자 ([core spec.py:33](../../../core/quant_core/ir_engine/spec.py), [web types.ts:243](../../../web/src/types.ts)).
- UI: 유니버스 패널 최상위 체크박스 "스크리너로 종목 선별 — 켜면 위 종목 대신 적용" ([IrBuilder.tsx:620-639](../../../web/src/pages/IrBuilder.tsx)). 켜면 대상종목 피커가 사라짐([:600](../../../web/src/pages/IrBuilder.tsx)).
- 엔진: `kind=="screener"` 면 선택 종목 무시·전체 유니버스 + 마스크. **정기/상시(`_run_scheduled`)에서만** 적용([engine.py:648](../../../core/quant_core/ir_engine/engine.py)). 이벤트(`on_signal`)는 미적용([engine.py:160-187](../../../core/quant_core/ir_engine/engine.py)).
- 유니버스 피커: `scope="backtest"` → `has_backtest_data` 종목 전부(비매매 지수·매크로 포함, "실거래 불가" 배지만) ([IrBuilder.tsx:603](../../../web/src/pages/IrBuilder.tsx), [MultiSymbolPicker.tsx:47](../../../web/src/components/MultiSymbolPicker.tsx)).
- 신호/조건 참조: `SymbolRefPicker` → `has_backtest_data` + `__SELF__` ([MultiSymbolPicker.tsx:185](../../../web/src/components/MultiSymbolPicker.tsx)). 비매매 데이터 참조는 여기 속함.
- 서버 게이트 `_assert_live_tradable` 는 이름과 달리 **레버리지만 검사**, 종목 tradability 미검사 ([strategies.py:52-66](../../../server/app/routers/strategies.py)) → 비매매 유니버스가 모의/실전으로 새는 잠재 구멍.

**문제 2가지:** ① 스크리너가 "유니버스 종류"라는 잘못된 계층에 배타 결합 → "고른 종목 안에서 거른다"가 불가능. ② 유니버스(매수/매도 대상)와 신호/조건 참조(데이터 입력)가 같은 데이터셋을 쓰고, 비매매 유니버스 배포를 막는 enforcement가 없음.

---

## 2. 데이터모델 (web types.ts · core spec.py)

스크리너를 유니버스의 **선택적 직교 필드**로.

```ts
// web/src/types.ts
universe: {
  kind: "single" | "list" | "all";          // "screener" 제거
  symbols?: string[];
  screener?: {
    condition: IrNode;
    refresh: "each_rebalance" | "once_at_start";  // 기본 each_rebalance(동적)
  } | null;
  exclude_macro?: boolean;
}
```

```python
# core spec.py Universe
kind: Literal["single", "list", "all"] = "single"      # "screener" 제거
symbols: list[str] = Field(default_factory=list)
screener: Optional[dict] = None                        # {"condition": Node, "refresh": str}
exclude_macro: bool = True
```

- `kind` = 선택 종목 개수만(0=all, 1=single, N=list).
- `screener` 는 `kind` 무관 선택적. **단 `symbols` 가 있어야 유효**(§5 검증).
- `refresh` 누락 시 `each_rebalance` 로 해석.

---

## 3. UI / UX (web)

### 3.1 종목추가 팝업 (MultiSymbolPicker) — 세부조건 추가

팝오버(및 inline) 하단에 접이식 "세부조건 설정". `screener`/`onScreenerChange` props가 주어질 때만 렌더(컴포넌트 범용 유지; 현재 유일 사용처 = IrBuilder 유니버스).

```
[chips: 삼성전자 ×  SK하이닉스 ×  + 종목 추가]   ← 닫힘: 세부조건 있으면 "세부조건 적용 중" 뱃지
  └ 팝오버:
       [TabbedSymbolList — 종목 체크/검색]
       ─────────────────────────────
       [▸ 세부조건 설정]  ← 0개 선택 시 비활성("먼저 종목을 선택하세요")
          (펼침) [SentenceTree, requiredType="condition"]
                 [재선별 시점 (라디오)]
                   (•) 매 리밸런싱마다 재선별 (동적)        ← 기본
                   ( ) 시작 시점에 한 번만 선별 (정적·바스켓 유지)
          안내(동적): "선택한 N개 종목 중 매 리밸런싱일에 조건을 만족하는 종목만 후보."
          안내(정적): "시작 시점에 조건을 만족한 종목으로 바스켓을 만들어 유지."
       [foot: N개 선택됨 · 세부조건 1개 | 완료]
```

- 조건 빌더는 기존 `SentenceTree`(`requiredType="condition"`) 재사용. 횡단순위는 선택 집합 내부 평가(예: "고른 20개 중 시총 상위 5").

### 3.2 유니버스 피커 — tradable 전용

- IrBuilder 유니버스의 `MultiSymbolPicker` 를 `scope="tradable"` 로 변경([IrBuilder.tsx:604](../../../web/src/pages/IrBuilder.tsx)). 매수/매도 가능 종목만 노출.
- 비매매 종목(지수·VIX·매크로)은 신호/조건 참조(`SymbolRefPicker`)에만 — 현행 유지.
- 안내문구([IrBuilder.tsx:607-611](../../../web/src/pages/IrBuilder.tsx)) 갱신: "실거래 불가 배지" 문구 제거, "매수/매도 가능 종목에서 선택" 으로.

### 3.3 IrBuilder 유니버스 패널 — 체크박스 제거·상태 이전

- 최상위 체크박스 "스크리너로 종목 선별" + 조건 영역([IrBuilder.tsx:620-639](../../../web/src/pages/IrBuilder.tsx)) **제거**. `useScreener` state 제거.
- `screenerCond`·`screenerRefresh` state 유지, `MultiSymbolPicker` 에 props 전달.
- 빌드([IrBuilder.tsx:357-363](../../../web/src/pages/IrBuilder.tsx)):

```ts
const syms = universeSymbols.split(",").map(s => s.trim()).filter(Boolean);
const universe = {
  kind: syms.length > 1 ? "list" : syms.length === 1 ? "single" : "all",
  ...(syms.length ? { symbols: syms } : {}),
  ...(syms.length && screenerCond
      ? { screener: { condition: screenerCond, refresh: screenerRefresh } }
      : {}),
};
```

- 로드([IrBuilder.tsx:232-243](../../../web/src/pages/IrBuilder.tsx)): `kind==="screener"` 분기 제거. `universe.screener?.condition` 있으면 `screenerCond`·`screenerRefresh`(누락 시 `each_rebalance`) 세팅.

---

## 4. 엔진 (core engine.py · run.py)

스크리너 적용을 `kind` 게이트 → **`universe.screener` 존재 게이트**, 모든 백테스트 진입 경로에. `_screener_mask`([engine.py:470](../../../core/quant_core/ir_engine/engine.py)) 재사용.

### 4.1 refresh 적용 — 동적/정적 (양 경로 공통 헬퍼)

`_screener_mask` 가 (dates×cols, bool) 마스크 반환. refresh로 소비만 분기:
- **`each_rebalance`(동적)**: 마스크 그대로.
- **`once_at_start`(정적)**: `sim.start` 이후 마스크가 계산되는 첫 유효일(NaN 없는 첫 행)의 자격 행을 추출해 전 기간 broadcast → 바스켓 고정. 시작일까지 데이터만 사용(lookahead 없음). 시작일 충족 0개면 빈 바스켓(거래 없음)으로 사유 노출.
- 신규 헬퍼 `_apply_refresh(mask, refresh, start) -> mask` 로 캡슐화(정기·이벤트 공용).

### 4.2 정기/상시 (`_run_scheduled`)

- `filt_node`([engine.py:630-631](../../../core/quant_core/ir_engine/engine.py)): 게이트를 `universe.screener and screener.get("condition")` 로.
- 마스크 적용([engine.py:648-650](../../../core/quant_core/ir_engine/engine.py)): `if universe.screener?.condition:` → `elig = _apply_refresh(_screener_mask(...), refresh, start)`. `cols ⊆ syms` 라 자연히 "선택 종목 ∩ 조건".

### 4.3 이벤트 (`run_unified`, `on_signal`)

- `_scoped`([engine.py:187](../../../core/quant_core/ir_engine/engine.py))에 screener 조건 노드 추가(참조 데이터 로드).
- screener 있으면 `_apply_refresh(_screener_mask(...), ...)` 로 자격 마스크 계산.
- 진입검사([engine.py:355-369](../../../core/quant_core/ir_engine/engine.py)): defer/비-defer 양 분기에서 `buy_arrs[sym][i]` 통과 조건에 **자격 마스크[i, sym] True** AND 추가.

### 4.4 run.py `_root_type_error`

- [run.py:74](../../../core/quant_core/ir_engine/run.py): 게이트를 `(u.screener or {}).get("condition")` 존재로(kind 무관). 조건 out_type=="condition" 계약 유지.

### 4.5 `_universe_symbols`

- engine.py·run.py 모두 변경 불필요(screener가 kind가 아니므로 list/single 분기로 자연 귀결). run.py:115-116 stale 주석만 정리.

---

## 5. 검증 (core spec.py)

- **screener 게이트 일반화**: `if u.kind == "screener":` 블록([spec.py:340-362](../../../core/quant_core/ir_engine/spec.py)) → `if u.screener and u.screener.get("condition"):`. 조건 condition 타입·시장데이터 참조(M1)·meaningfulness 유지.
- **symbols 필수**: `screener` 있는데 `symbols` 비면 에러 — "세부조건은 선택한 종목이 있을 때만 설정할 수 있습니다."
- **이벤트 허용**: [spec.py:336](../../../core/quant_core/ir_engine/spec.py) `on_signal + kind in ("all","screener")` 에러에서 `"screener"` 제거(이제 이벤트+세부조건 허용). `kind=="all"` + on_signal 에러는 유지.
- **refresh enum**: `each_rebalance|once_at_start` 만 허용, 누락 시 기본.
- **이벤트+세부조건 live 가드 (S-univ)**: `entry.mode=="on_signal"` + screener 존재면, 정보/경고가 아니라 **모의/실전 차단용 표식**. 검증 단계에선 백테스트 허용·경고만, 실제 차단은 서버 §7에서. (백테스트는 동작, 라이브 전환만 막음 — Phase 2 전까지.)
- **kind enum**: `Literal["single","list","all"]` (screener 제거).

### 5.1 데이터 윈도우/의존 (screener 일반화)

- `needed_symbols`([spec.py:483](../../../core/quant_core/ir_engine/spec.py)): `kind in ("all","screener")` → `kind == "all"`. list+screener는 `symbols ∪ screener조건 참조심볼` 로드(전체 로드 안 함). screener 조건의 `referenced_symbols` 를 nodes에 포함.
- `needed_columns`([spec.py:514](../../../core/quant_core/ir_engine/spec.py)): `if u.kind=="screener"` → `if u.screener`.
- `data/deps.py:55`([deps.py](../../../core/quant_core/data/deps.py)): `if u.kind=="screener"` → `if u.screener`.
- `data/gate.py:84`([gate.py](../../../core/quant_core/data/gate.py)) 생존편향 게이트: `kind in ("all","screener")` → `kind == "all"`. (screener는 이제 고정 종목 리스트라 멤버십-이력 생존편향 대상 아님.)

---

## 6. 능력기술 / NL 컴파일러 (core capabilities.py)

- `universe_kind`([capabilities.py:16-25](../../../core/quant_core/ir_engine/capabilities.py))에서 `screener` 값 제거. `single/list/all` 만.
- 별도 `screener` 능력 항목 추가: "선택 종목에 얹는 자격 필터(필터+횡단순위 조건). `screener.condition`+`refresh`." entry_mode "scheduled" use_for의 "all/screener" → "all" 로.
- NL 컴파일러 쿡북/관용구([[project_nl_compiler_reliability]])에 `kind:"screener"` 산출 예시가 있으면 `kind:"list"+screener` 로 교체. 컴파일러가 제거된 enum 값을 내지 않도록 — compile 회귀 테스트로 확인.

---

## 7. 서버 가드 (server strategies.py)

`_assert_live_tradable`([strategies.py:52](../../../server/app/routers/strategies.py))에 두 검사 추가(현 레버리지 검사 유지):

1. **비매매 유니버스 차단**: `run_mode in ("paper","live")` 면 정의의 `universe.symbols` 중 비매매(tradable=False) 종목이 있으면 422. tradable 집합은 `_build_symbols_payload`([backtest.py:30-99](../../../server/app/routers/backtest.py))의 마스터 로직 재사용(공유 헬퍼로 추출). `strat:<id>` 합성자산은 직접 매매 대상 아님 → 모의/실전 차단(또는 별도 메시지). `kind=="all"`(빈 선택) 도 모의/실전엔 부적합 → 차단.
2. **이벤트+세부조건 차단(Phase 2 전까지)**: `entry.mode=="on_signal"` + `universe.screener` 존재 + `run_mode in ("paper","live")` → 422 "이벤트 진입 + 세부조건 전략은 현재 백테스트 전용입니다(라이브 지원 예정)."

메시지는 사용자 친화. 백테스트(`/backtest`)·draft 저장엔 적용 안 함.

---

## 8. 마이그레이션 — 기존 `kind:"screener"` 깨끗한 제거

- **구현 직전 선결**: 서버 DB에서 `definition->universe->>kind = 'screener'` 전략 조회. 개수·내용 사용자 보고 후 진행. 없거나 테스트용 → 제거. 실사용 발견 시 멈추고 재합의.
- 코드/스키마 전수 삭제(web types·IrBuilder 로드, core spec enum·검증·데이터레이어, capabilities).

---

## 9. 스코프 밖

- **Phase 2 (로컬앱 이벤트 세부조건 패리티)** — platform/local KIS 경로. 본 plan 미포함, 별도 추적. Phase 1은 서버 가드로 이벤트+세부조건 라이브를 막아 backtest≠live 발산 차단.
- 전체시장 스크리닝(종목 선택 없이 전체 거르기) — 제거, 되살리지 않음.
- 편집시점 현재데이터 정적 고정(lookahead) — 채택 안 함.
- 비매매 지수 자체를 유니버스로 백테스트 — 제거(tradable ETF로 대체, 지수는 신호/조건 참조로).
- `SentenceTree`·조건 빌더 기능 확장 — 기존 재사용.

---

## 10. 테스트 / 검증

코어 테스트 패턴: `strategy_from_spec(spec, dataset)` → `{success, equity, issues}`, `_screener_mask(screener, ctx, cols)` 직접. (참조: [tests/test_screener.py](../../../tests/test_screener.py))

- **마이그레이션 회귀**: `test_screener.py` 의 `kind:"screener"` → `kind:"list"+symbols(A,B,C,D)+screener` 로 갱신, 기존 PIT 자격 단언 동일 통과(전체=리스트라 동치).
- **선택 ∩ 조건(정기·동적)**: 5종목 list + 세부조건(상위 2) → 매 리밸런싱 동적 교집합, 선택 밖 종목 미보유.
- **정적(`once_at_start`)**: 동일 조건 → 시작시점 바스켓 고정, 이후 후보 불변(시점 자격 뒤집혀도 바스켓 유지). `_apply_refresh` 단위 테스트.
- **이벤트+세부조건(백테스트)**: list + 세부조건 + `on_signal` → 자격 False 날 진입 차단(동적·정적).
- **검증**: screener+빈symbols 에러 · refresh 잘못된 값 에러 · on_signal+screener 백테스트 허용.
- **데이터 윈도우**: list+screener의 `needed_symbols` 가 전체(None) 아님(symbols∪조건참조)·생존편향 게이트 미발동.
- **서버 가드**: 비매매 유니버스 paper 거부(422) · 이벤트+세부조건 paper 거부 · tradable 유니버스 paper 허용. (참조: [tests/test_screener... 서버측은 server/tests/test_strategies_ir.py](../../../server/tests/test_strategies_ir.py) 레버리지 거부 테스트 패턴.)
- **웹**: 브라우저로 팝업 세부조건 펼침/접힘·0개 비활성·refresh 라디오·유니버스 tradable만 노출·저장→재로드 왕복.

---

## 11. 변경 파일 (Phase 1)

| 영역 | 파일 | 변경 |
|---|---|---|
| web | types.ts | universe 타입(screener 제거, screener 직교+refresh) |
| web | components/MultiSymbolPicker.tsx | 세부조건 영역+refresh 라디오, props 확장 |
| web | pages/IrBuilder.tsx | 체크박스 제거, 빌드/로드, 유니버스 scope=tradable, props |
| core | ir_engine/spec.py | Universe 모델·kind enum·검증(5곳)·needed_symbols·needed_columns |
| core | ir_engine/engine.py | 정기+이벤트 게이트, `_apply_refresh`(동적/정적), 이벤트 자격 마스크 |
| core | ir_engine/run.py | `_root_type_error` 게이트, stale 주석 |
| core | data/deps.py · data/gate.py | screener 게이트·생존편향 게이트 |
| core | ir_engine/capabilities.py | universe_kind에서 screener 제거, screener 능력 항목 |
| core | (NL 컴파일러 쿡북/관용구) | kind:screener 예시 교체 |
| server | app/routers/strategies.py | `_assert_live_tradable` 비매매·이벤트세부조건 차단 |
| server | (tradable 헬퍼 공유) | `_build_symbols_payload` tradable 로직 추출 |
| core/server | tests | test_screener·신규 엔진/검증·서버 가드 |
| server | (마이그레이션 조회) | kind=screener 전략 사전 조회 |
