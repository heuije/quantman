import { useState } from "react";
import type {
  Condition, ConditionGroup, ConditionNode, IndicatorInfo, ModifierKind,
  Op, Operand, Stat, SymbolInfo,
} from "../types";
import { SELF_SYMBOL, SELF_LABEL, isSelfRef, isGroupNode } from "../types";
import { CategoryList, usePopoverDismiss } from "./SymbolPicker";
import TabbedSymbolList from "./TabbedSymbolList";

/** Phase 56 — 매도 conditions 전용 가상 indicator. backend로 가지 않고
 *  Backtest.tsx buildDef에서 sell_rules.hold_days로 transcode됨. */
export const HELD_DAYS_KEY = "_held_days";
const HELD_DAYS_INDICATOR: IndicatorInfo = {
  key: HELD_DAYS_KEY,
  label: "보유기간(일)",
  group: "상태",
  unit: "일",
  compare_group: "_int",
};

/** [이 종목] placeholder가 어떤 종목의 지표를 노출할지 결정.
 *  Phase 53 — 옛 "개별종목"(테스트용 애플·삼성전자 사용자 추가 종목) 제외하고
 *  일반 카테고리(자산·거시지표 등) 첫 indicators 가용 종목 사용. KR 종목 간 동일.
 *  Phase 56 — context="sell"이면 가상 "보유기간(일)" indicator prepend. */
function _selfIndicators(symbols: SymbolInfo[],
                          context?: "buy" | "sell"): IndicatorInfo[] {
  const stock = symbols.find(
    (s) => s.category !== "개별종목" && s.indicators.length > 0);
  const base = stock?.indicators ?? [];
  if (context === "sell") {
    return [HELD_DAYS_INDICATOR, ...base];
  }
  return base;
}

const OPERAND_TAB_ORDER = [
  "자산", "변동성", "금리·환율", "신용", "거시지표", "심리",
];

/** Phase 56 — compare_group별 constant 값 허용 범위. backend가 silent false
 *  내는 unrealistic 값(예: RSI > 5000) 입력 차단. 무한대(price·money·other)는
 *  엔트리 없음(자유 입력). */
const GROUP_RANGES: Record<string, { min: number; max: number }> = {
  pct:   { min: -100, max: 100 },
  rsi:   { min: 0,    max: 100 },
  bbpct: { min: 0,    max: 1 },
  flag:  { min: 0,    max: 1 },
  z:     { min: -10,  max: 10 },
};

// ── 상수 ──────────────────────────────────────────────────────────────────────

const OP_GROUPS: { label: string; ops: { value: Op; label: string }[] }[] = [
  { label: "수준 비교", ops: [
    { value: ">",  label: "초과일 때" },
    { value: ">=", label: "이상일 때" },
    { value: "<",  label: "미만일 때" },
    { value: "<=", label: "이하일 때" },
    { value: "between", label: "범위 안일 때" },
  ]},
  // Phase 56 — cross는 일봉 단위 평가(우리 dataset 일봉). intraday tick cross 아님 명시.
  { label: "크로스 (일봉 기준)", ops: [
    { value: "cross_up",   label: "상향돌파할 때 (일봉)" },
    { value: "cross_down", label: "하향돌파할 때 (일봉)" },
  ]},
];

const STAT_OPTIONS: { value: Stat; label: string }[] = [
  { value: "mean", label: "평균" },
  { value: "max",  label: "최댓값" },
  { value: "min",  label: "최솟값" },
  { value: "percentile", label: "백분위" },
  { value: "lag",  label: "전(前) 값" },
];
const STAT_LABEL: Record<string, string> =
  Object.fromEntries(STAT_OPTIONS.map((o) => [o.value, o.label]));

const INDICATOR_GROUP_ORDER =
  ["가격·수익률", "모멘텀", "이동평균", "변동성·기술적", "통계", "거래량", "펀더멘털", "기타"];

// 비교 그룹별 사용자에게 보여줄 힌트 (상수 입력 시 placeholder/범위 안내)
const COMPARE_GROUP_HINTS: Record<string, { range: string; tip: string }> = {
  pct:   { range: "-100 ~ +100",  tip: "%로 입력 (예: 1.5 = 1.5%)" },
  price: { range: "0 이상",        tip: "원 단위 가격" },
  rsi:   { range: "0 ~ 100",       tip: "RSI 값 (보통 30=과매도, 70=과매수)" },
  bbpct: { range: "0 ~ 1",         tip: "0=하단, 0.5=중심, 1=상단" },
  flag:  { range: "0 또는 1",       tip: "0=거짓, 1=참" },
  days:  { range: "음수=연속하락, 양수=연속상승", tip: "일수 (정수)" },
  mult:  { range: "0 이상",        tip: "배수 (예: 1.5 = 1.5배)" },
  z:     { range: "보통 -3 ~ +3", tip: "표준편차 단위" },
  money: { range: "원",            tip: "거래대금(원)" },
  other: { range: "",              tip: "" },
};

// ── 헬퍼 ──────────────────────────────────────────────────────────────────────

function findIndicator(symbols: SymbolInfo[], sym?: string, key?: string):
    IndicatorInfo | undefined {
  // Phase 41 — SELF_SYMBOL은 KR 개별종목 indicators fallback
  if (sym === SELF_SYMBOL) {
    return _selfIndicators(symbols).find((i) => i.key === key);
  }
  return symbols.find((s) => s.symbol === sym)
    ?.indicators.find((i) => i.key === key);
}

function compareGroupOf(symbols: SymbolInfo[], sym?: string, key?: string): string {
  return findIndicator(symbols, sym, key)?.compare_group ?? "other";
}

// ── 메인 ──────────────────────────────────────────────────────────────────────

interface Props {
  symbols: SymbolInfo[];
  group: ConditionGroup;
  onChange: (g: ConditionGroup) => void;
  /** 맥락 문장 — 다중/자동선택이면 "…를 만족하는 종목만 매수" 류를 상단에 표시. */
  contextNote?: string;
  /** Phase 56 — 각 조건 row에 [추가] 버튼 노출. 매수 조건 progressive disclosure용.
   *  버튼 click 시 callback 호출 + 부모가 다음 단계로. undefined면 버튼 없음. */
  onAddCondition?: () => void;
  /** Phase 56 — "sell"이면 SELF_SYMBOL indicators에 "보유기간(일)" 가상 indicator 노출.
   *  default "buy". */
  context?: "buy" | "sell";
}

/** 새 단일 조건의 기본값 — 좌변 [각 종목]·RSI, "30 미만". */
export function starterCondition(symbols: SymbolInfo[]): Condition {
  const selfInds = _selfIndicators(symbols);
  const ind = (selfInds.find((i) => i.key.includes("rsi")) ?? selfInds[0])?.key ?? "";
  return {
    left: { kind: "indicator", symbol: SELF_SYMBOL, indicator: ind },
    op: "<",
    right: { kind: "constant", value: 30 },
    modifier: null,
  };
}

/** 칩 + 분류·검색 팝오버 방식의 문장형 조건 빌더. G2 — 1단계 중첩 그룹 지원. */
export default function ConditionBuilder({ symbols, group, onChange, contextNote, onAddCondition, context }: Props) {
  return (
    <div>
      {contextNote && <div className="builder-note">{contextNote}</div>}
      <GroupEditor symbols={symbols} group={group} onChange={onChange}
                   depth={0} onAddCondition={onAddCondition} context={context} />
    </div>
  );
}

/** 한 그룹(조건/하위그룹 묶음)을 렌더. depth=0이면 "묶음 추가" 노출(1단계 중첩 제한). */
function GroupEditor({ symbols, group, onChange, depth, onAddCondition, context }: {
  symbols: SymbolInfo[];
  group: ConditionGroup;
  onChange: (g: ConditionGroup) => void;
  depth: number;
  onAddCondition?: () => void;
  context?: "buy" | "sell";
}) {
  function setNode(i: number, node: ConditionNode) {
    onChange({ ...group, conditions: group.conditions.map((n, idx) => idx === i ? node : n) });
  }

  function updateLeaf(i: number, patch: Partial<Condition>) {
    const cur = group.conditions[i] as Condition;
    const next = { ...cur, ...patch };
    // 좌측이 바뀌고 우측이 지표/이력통계이며 호환 그룹이 다르면 → 상수 0으로 reset
    if (patch.left && (next.right?.kind === "indicator" || next.right?.kind === "history")) {
      const lg = compareGroupOf(symbols, next.left.symbol, next.left.indicator);
      const rg = compareGroupOf(symbols, next.right.symbol, next.right.indicator);
      if (lg !== "other" && rg !== "other" && lg !== rg) {
        next.right = { kind: "constant", value: 0 };
      }
    }
    setNode(i, next);
  }

  function setOp(i: number, op: Op) {
    const c = group.conditions[i] as Condition;
    let right = c.right;
    const wasBetween = c.op === "between";
    if (op === "between" && !wasBetween) {
      right = { kind: "constant", value: [0, 0] };
    } else if (op !== "between" && wasBetween) {
      right = { kind: "constant", value: 0 };
    }
    updateLeaf(i, { op, right });
  }

  function addCondition() {
    onChange({ ...group, conditions: [...group.conditions, starterCondition(symbols)] });
  }

  function addGroup() {
    const sub: ConditionGroup = { logic: "OR", conditions: [starterCondition(symbols)] };
    onChange({ ...group, conditions: [...group.conditions, sub] });
  }

  function remove(i: number) {
    onChange({ ...group, conditions: group.conditions.filter((_, idx) => idx !== i) });
  }

  return (
    <div className={depth > 0 ? "cond-subgroup" : undefined}>
      {group.conditions.map((node, i) => (
        <div key={i}>
          {isGroupNode(node) ? (
            <div className="cond-subgroup-wrap">
              <div className="cond-subgroup-head">
                <span className="cond-subgroup-tag">묶음 조건</span>
                {/* NEW-12 — 빈 sub-group은 평가 실패로 전체 조건 무효화. */}
                {(node.conditions?.length ?? 0) === 0 && (
                  <span className="metric-hint lg" style={{ background: "var(--amber-soft)", color: "var(--amber)" }}
                        data-tip="빈 묶음은 평가 실패로 전체 조건이 무효화됩니다. 조건을 추가하거나 묶음을 삭제하세요.">⚠</span>
                )}
                <button type="button" className="ghost sm" onClick={() => remove(i)}>
                  묶음 삭제
                </button>
              </div>
              <GroupEditor
                symbols={symbols} group={node} depth={depth + 1}
                onChange={(g) => setNode(i, g)}
                onAddCondition={onAddCondition}
                context={context}
              />
            </div>
          ) : (
            <ConditionRow
              symbols={symbols} c={node}
              onPatch={(patch) => updateLeaf(i, patch)}
              onSetOp={(op) => setOp(i, op)}
              onRemove={() => remove(i)}
              onAdd={onAddCondition}
              context={context}
            />
          )}

          {/* 항목 사이의 AND/OR 라벨 (마지막 항목엔 표시 안 함) */}
          {i < group.conditions.length - 1 && (
            <div className="logic-sep">
              <span className="logic-sep-line" />
              <span className="logic-sep-label">
                {group.logic === "AND" ? "그리고 (AND)" : "또는 (OR)"}
              </span>
              <span className="logic-sep-line" />
            </div>
          )}
        </div>
      ))}

      <div className="builder-foot">
        {group.conditions.length > 1 && (
          <div className="logic-toggle">
            {(["AND", "OR"] as const).map((lg) => (
              <button
                key={lg} type="button"
                className={group.logic === lg ? "" : "ghost"}
                onClick={() => onChange({ ...group, logic: lg })}
              >
                {lg === "AND" ? "모두 만족 (AND)" : "하나라도 만족 (OR)"}
              </button>
            ))}
          </div>
        )}
        <button type="button" className="ghost sm" onClick={addCondition}>
          + 조건 추가
        </button>
        {depth === 0 && (
          <button type="button" className="ghost sm" onClick={addGroup}>
            + 묶음 추가 (괄호)
          </button>
        )}
      </div>
    </div>
  );
}

/** Phase 56 — 부정합 검출 헬퍼. */
function operandsEqual(a: Operand, b: Operand | undefined): boolean {
  if (!b || a.kind === "constant" || b.kind === "constant") return false;
  return a.kind === b.kind
      && a.symbol === b.symbol
      && a.indicator === b.indicator
      && (a.stat ?? null) === (b.stat ?? null)
      && (a.window ?? null) === (b.window ?? null)
      && (a.mul ?? null) === (b.mul ?? null)
      && (a.add ?? null) === (b.add ?? null);
}

/** NEW-11 — `>` / `>=` / `<` / `<=` + constant value가 GROUP_RANGES boundary와 충돌해
 *  항상 false 조건이 되는지 검출. */
function boundaryAlwaysFalse(op: Op, right: Operand | undefined, leftGroup: string): string | null {
  if (!right || right.kind !== "constant") return null;
  const v = typeof right.value === "number" ? right.value : NaN;
  if (isNaN(v)) return null;
  const range = GROUP_RANGES[leftGroup];
  if (!range) return null;
  if (op === ">" && v >= range.max) return `이 지표는 최대 ${range.max}. "${range.max} 초과" 조건은 항상 false.`;
  if (op === ">=" && v > range.max) return `이 지표는 최대 ${range.max}. 초과 값으로 조건이 항상 false.`;
  if (op === "<" && v <= range.min) return `이 지표는 최소 ${range.min}. "${range.min} 미만" 조건은 항상 false.`;
  if (op === "<=" && v < range.min) return `이 지표는 최소 ${range.min}. 미만 값으로 조건이 항상 false.`;
  return null;
}

/** 단일 조건 문장 한 줄 — 좌변(종목·지표) · 수식어 · 우변(종목·지표 또는 숫자) · 연산자 · 삭제 · 추가(선택). */
function ConditionRow({ symbols, c, onPatch, onSetOp, onRemove, onAdd, context }: {
  symbols: SymbolInfo[];
  c: Condition;
  onPatch: (patch: Partial<Condition>) => void;
  onSetOp: (op: Op) => void;
  onRemove: () => void;
  /** Phase 56 — 정의되면 [추가] 버튼이 [삭제] 옆에 표시됨. 매수조건 progressive disclosure용. */
  onAdd?: () => void;
  /** Phase 56 — context="sell"면 SELF_SYMBOL indicators에 보유기간 가상 indicator 노출. */
  context?: "buy" | "sell";
}) {
  const leftGroup = compareGroupOf(symbols, c.left.symbol, c.left.indicator);
  // Phase 56 — 좌변=우변 완전 동일 detection. 항상 false 평가 = 무의미 조건.
  const sameOperand = operandsEqual(c.left, c.right);
  // NEW-11 — > or < boundary 값 검출.
  const boundaryWarn = boundaryAlwaysFalse(c.op, c.right, leftGroup);
  return (
    <div className="sentence">
      <SymbolChip
        symbols={symbols} operand={c.left}
        onChange={(o) => onPatch({ left: o })}
      />
      <IndicatorChip
        symbols={symbols} operand={c.left}
        onChange={(o) => onPatch({ left: o })}
        context={context}
      />
      <span className="txt">가(이)</span>

      {sameOperand && (
        <span className="metric-hint lg" style={{ background: "var(--amber-soft)", color: "var(--amber)" }}
              data-tip="좌변과 우변이 완전히 동일합니다. 조건이 항상 false로 평가되어 매수 신호가 발생하지 않습니다.">⚠</span>
      )}

      {boundaryWarn && (
        <span className="metric-hint lg" style={{ background: "var(--amber-soft)", color: "var(--amber)" }}
              data-tip={boundaryWarn}>⚠</span>
      )}

      {/* NEW-10 — cross + streak/within 조합 ⚠. cross는 1일 이벤트, N일 연속 거의 불가. */}
      {(c.op === "cross_up" || c.op === "cross_down")
        && c.modifier?.kind === "streak" && (c.modifier?.days ?? 0) >= 2 && (
        <span className="metric-hint lg" style={{ background: "var(--amber-soft)", color: "var(--amber)" }}
              data-tip="cross_up/down은 1일 이벤트입니다. N일 연속(streak)은 거의 발생하지 않아 매수 신호 0건이 될 가능성이 큽니다. within(최근 N일 내)을 권장합니다.">⚠</span>
      )}

      {c.modifier && (
        <ActiveModifierChip
          value={c.modifier}
          onChange={(m) => onPatch({ modifier: m })}
        />
      )}

      {c.op === "between" ? (
        <RangeInline
          value={Array.isArray(c.right?.value) ? (c.right!.value as number[]) : [0, 0]}
          hintGroup={leftGroup}
          onChange={(v) => onPatch({ right: { kind: "constant", value: v } })}
        />
      ) : (
        <RightOperand
          symbols={symbols}
          operand={c.right ?? { kind: "constant", value: 0 }}
          leftGroup={leftGroup}
          onChange={(o) => onPatch({ right: o })}
        />
      )}

      <select className="op-select" value={c.op}
              onChange={(e) => onSetOp(e.target.value as Op)}>
        {OP_GROUPS.map((g) => (
          <optgroup key={g.label} label={g.label}>
            {g.ops.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </optgroup>
        ))}
      </select>

      <span className="sentence-tail">
        {!c.modifier && (
          <button
            type="button" className="chip ghost-chip"
            onClick={() => onPatch({ modifier: { kind: "streak", days: 3 } })}
          >
            + 수식어
          </button>
        )}
        <button type="button" className="ghost sm" onClick={onRemove}>
          삭제
        </button>
        {onAdd && (
          <button type="button" className="sm" onClick={onAdd}>
            적용
          </button>
        )}
      </span>
    </div>
  );
}

// ── 피연산자 칩 + 팝오버 ───────────────────────────────────────────────────────

/** G1 — 아핀 변환 입력 (× 배수 / + 가감). 지표·이력통계에만 노출. */
function AffineFields({ value, onChange }: {
  value: Operand;
  onChange: (o: Operand) => void;
}) {
  if (value.kind === "constant") return null;
  // NEW-13 — mul=0이면 indicator 시계열을 모두 0으로 만듦. 비교 의미 무.
  const mulZero = value.mul === 0;
  return (
    <div className="op-field affine-row">
      <label>값 조정 <span className="muted small">(선택)</span></label>
      <div className="affine-inputs">
        <span className="txt">×</span>
        <input
          type="number" step="any" placeholder="1"
          value={value.mul ?? ""}
          onChange={(e) => onChange({
            ...value, mul: e.target.value === "" ? null : Number(e.target.value),
          })}
        />
        <span className="txt">+</span>
        <input
          type="number" step="any" placeholder="0"
          value={value.add ?? ""}
          onChange={(e) => onChange({
            ...value, add: e.target.value === "" ? null : Number(e.target.value),
          })}
        />
        {mulZero && (
          <span className="metric-hint lg" style={{ background: "var(--amber-soft)", color: "var(--amber)" }}
                data-tip="× 0은 모든 시계열을 0으로 만들어 비교 의미가 사라집니다. 1 이상 권장.">⚠</span>
        )}
      </div>
      <div className="op-hint">예: MA20 ×1.05 = "5% 위", 등락률 +2</div>
    </div>
  );
}

/** 우변 피연산자 — kind=constant면 인라인 숫자 입력, indicator/history면 [종목 chip]+[지표 chip]+숫자↔지표 토글. */
function RightOperand({ symbols, operand, leftGroup, onChange }: {
  symbols: SymbolInfo[];
  operand: Operand;
  leftGroup: string;
  onChange: (o: Operand) => void;
}) {
  const hint = COMPARE_GROUP_HINTS[leftGroup] ?? COMPARE_GROUP_HINTS.other;

  // 숫자 → 지표 전환: 좌변과 호환되는 첫 종목·지표 자동 선택. 호환 데이터가 전혀
  // 없으면 무동작(사용자 데이터 부재 — 강제 변환은 misleading).
  function toIndicator() {
    const compat = (i: { compare_group?: string | null }) =>
      leftGroup === "other" || (i.compare_group ?? "other") === leftGroup;
    const sym = symbols.find((s) => s.indicators.some(compat));
    if (!sym) return;
    const inds = sym.indicators.filter(compat);
    onChange({ kind: "indicator", symbol: sym.symbol, indicator: inds[0]?.key ?? "" });
  }

  if (operand.kind === "constant") {
    const v = typeof operand.value === "number" ? operand.value : 0;
    const range = GROUP_RANGES[leftGroup];
    const outOfRange = !!range && (v < range.min || v > range.max);
    return (
      <>
        <span className="operand">
          <input type="number" step="any" value={v} placeholder={hint.range}
                 min={range?.min} max={range?.max}
                 onChange={(e) => onChange({ kind: "constant", value: Number(e.target.value) })} />
        </span>
        {outOfRange && range && (
          <span className="metric-hint lg" style={{ background: "var(--amber-soft)", color: "var(--amber)" }}
                data-tip={`이 지표는 ${range.min}~${range.max} 범위입니다. 벗어난 값은 조건이 항상 false로 평가됩니다.`}>⚠</span>
        )}
        <button type="button" className="chip ghost-chip kind-toggle"
                title="지표 값과 비교하도록 전환" onClick={toIndicator}>
          ↔ 지표
        </button>
      </>
    );
  }
  return (
    <>
      <SymbolChip symbols={symbols} operand={operand} compatGroup={leftGroup}
                  onChange={onChange} />
      <IndicatorChip symbols={symbols} operand={operand} compatGroup={leftGroup}
                     onChange={onChange} />
      <button type="button" className="chip ghost-chip kind-toggle"
              title="숫자와 비교하도록 전환"
              onClick={() => onChange({ kind: "constant", value: 0 })}>
        ↔ 숫자
      </button>
    </>
  );
}

/** between 연산자용 [min ~ max] 입력. Phase 56 — min>max 시 ⚠ + onBlur swap. */
function RangeInline({ value, hintGroup, onChange }: {
  value: number[];
  hintGroup?: string;
  onChange: (v: number[]) => void;
}) {
  const lo = value[0] ?? 0;
  const hi = value[1] ?? 0;
  const hint = COMPARE_GROUP_HINTS[hintGroup ?? "other"] ?? COMPARE_GROUP_HINTS.other;
  const invalid = lo > hi;
  return (
    <span className="operand">
      <input type="number" step="any" value={lo} placeholder={hint.range}
             onChange={(e) => onChange([Number(e.target.value), hi])}
             onBlur={() => { if (invalid) onChange([hi, lo]); }} />
      <span className="txt">~</span>
      <input type="number" step="any" value={hi} placeholder={hint.range}
             onChange={(e) => onChange([lo, Number(e.target.value)])}
             onBlur={() => { if (invalid) onChange([hi, lo]); }} />
      {invalid && (
        <span className="metric-hint lg" style={{ background: "var(--amber-soft)", color: "var(--amber)" }}
              data-tip="시작 값이 끝 값보다 큽니다. 조건이 항상 false. 입력 포커스 해제 시 자동 swap됩니다.">⚠</span>
      )}
    </span>
  );
}

/** 활성 수식어 — "OO가 [3일 연속] OO 미만일 때" 위치에 표시.
 *  Phase 56 — min=2 (1일은 modifier 효과 없음, backend가 silent skip). */
function ActiveModifierChip({ value, onChange }: {
  value: { kind: ModifierKind; days: number };
  onChange: (m: { kind: ModifierKind; days: number } | null) => void;
}) {
  return (
    <span className="modifier">
      <input type="number" min={2} value={value.days}
             onChange={(e) => onChange({ ...value, days: Math.max(2, Number(e.target.value)) })} />
      <span className="txt">일</span>
      <select value={value.kind}
              onChange={(e) => onChange({ ...value, kind: e.target.value as ModifierKind })}>
        <option value="streak">연속</option>
        <option value="within">내</option>
      </select>
      <button type="button" className="x-btn" onClick={() => onChange(null)}>×</button>
    </span>
  );
}

/** 종목 chip — 좌·우 공용. compatGroup 지정(우변) 시 호환 종목만 노출 + 선택 시 호환되는 첫 지표로 자동 swap. */
function SymbolChip({ symbols, operand, onChange, compatGroup }: {
  symbols: SymbolInfo[];
  operand: Operand;
  onChange: (o: Operand) => void;
  compatGroup?: string;
}) {
  const [open, setOpen] = useState(false);
  const ref = usePopoverDismiss<HTMLSpanElement>(open, setOpen);
  const compat = (i: { compare_group?: string | null }) =>
    !compatGroup || compatGroup === "other"
      || (i.compare_group ?? "other") === compatGroup;
  // Phase 53 — "개별종목"(옛 테스트용 사용자 추가) 카테고리 제외.
  const symList = symbols.filter((s) =>
    s.category !== "개별종목"
    && s.indicators.length > 0
    && (!compatGroup || compatGroup === "other" || s.indicators.some(compat)));
  const isSelf = isSelfRef(operand);
  const sel = symbols.find((s) => s.symbol === operand.symbol);
  // Phase 41 — SELF_SYMBOL이면 [이 종목] 라벨로 표시
  const label = isSelf ? SELF_LABEL
    : sel?.name ? `${sel.symbol} ${sel.name}`
    : operand.symbol || "종목 선택";

  function pickSymbol(sym: string) {
    // Phase 41 — SELF_SYMBOL은 KR 개별종목 indicators fallback
    const allInds = sym === SELF_SYMBOL
      ? _selfIndicators(symbols)
      : symbols.find((s) => s.symbol === sym)?.indicators ?? [];
    const inds = compatGroup ? allInds.filter(compat) : allInds;
    const ind = inds.some((i) => i.key === operand.indicator)
      ? operand.indicator : inds[0]?.key ?? "";
    onChange({ ...operand, symbol: sym, indicator: ind });
    setOpen(false);
  }

  return (
    <span className="chip-wrap" ref={ref}>
      <button type="button"
              className={"chip" + (isSelf ? " chip-self" : "")}
              onClick={() => setOpen((v) => !v)}>
        {label}<span className="chip-caret">▾</span>
      </button>
      {open && (
        <div className="popover popover-wide">
          {/* Phase 41 — [이 종목] placeholder는 첫 옵션으로 강조 노출 */}
          <div className="self-option">
            <button type="button"
                    className={"self-option-btn" + (isSelf ? " on" : "")}
                    onClick={() => pickSymbol(SELF_SYMBOL)}>
              <strong>{SELF_LABEL}</strong>
              <span className="muted small">
                — 각 매수후보 종목에 자동 적용
              </span>
            </button>
          </div>
          <TabbedSymbolList
            items={symList.map((s) => ({
              key: s.symbol,
              label: s.name ? `${s.symbol} ${s.name}` : s.symbol,
              cat: s.category,
            }))}
            order={OPERAND_TAB_ORDER}
            selected={isSelf ? "" : operand.symbol}
            placeholder="종목 검색…"
            emptyMessage="호환되는 종목이 없습니다."
            onPick={pickSymbol}
          />
        </div>
      )}
    </span>
  );
}

/** 지표 chip — 좌·우 공용. compatGroup 지정(우변) 시 호환 지표만 노출 + history 토글 + affine. */
function IndicatorChip({ symbols, operand, onChange, compatGroup, context }: {
  symbols: SymbolInfo[];
  operand: Operand;
  onChange: (o: Operand) => void;
  compatGroup?: string;
  context?: "buy" | "sell";
}) {
  const [open, setOpen] = useState(false);
  const ref = usePopoverDismiss<HTMLSpanElement>(open, setOpen);
  // Phase 41 — SELF_SYMBOL은 KR 개별종목 indicators fallback
  // Phase 56 — context="sell"이면 "보유기간(일)" 가상 indicator prepend.
  const allInds = operand.symbol === SELF_SYMBOL
    ? _selfIndicators(symbols, context)
    : symbols.find((s) => s.symbol === operand.symbol)?.indicators ?? [];
  const inds = compatGroup && compatGroup !== "other"
    ? allInds.filter((i) => (i.compare_group ?? "other") === compatGroup)
    : allInds;
  const found = inds.find((i) => i.key === operand.indicator);
  const baseLabel = found?.label ?? operand.indicator ?? "지표 선택";
  const histSuffix = operand.kind === "history"
    ? ` · ${operand.window ?? 20}일 ${STAT_LABEL[operand.stat ?? "mean"]}`
    : "";

  function pickIndicator(key: string) {
    onChange({ ...operand, indicator: key });
  }

  function toggleHistory(enabled: boolean) {
    if (enabled) {
      onChange({
        kind: "history", symbol: operand.symbol, indicator: operand.indicator,
        stat: operand.stat ?? "mean", window: operand.window ?? 20,
        percentile: operand.percentile,
      });
    } else {
      onChange({
        kind: "indicator", symbol: operand.symbol, indicator: operand.indicator,
      });
    }
  }

  return (
    <span className="chip-wrap" ref={ref}>
      <button type="button" className="chip" onClick={() => setOpen((v) => !v)}>
        {baseLabel}{histSuffix}<span className="chip-caret">▾</span>
      </button>
      {open && (
        <div className="popover">
          {inds.length === 0 ? (
            <div className="cat-empty">
              {compatGroup && compatGroup !== "other"
                ? "이 종목엔 호환되는 지표가 없습니다."
                : "이 종목엔 사용 가능한 지표가 없습니다. (라이브 매매 전용)"}
            </div>
          ) : (
            <>
              <div className="op-label">
                지표 선택
                <span className="op-label-sub">
                  ({operand.symbol === SELF_SYMBOL ? SELF_LABEL : operand.symbol}
                  · {inds.length}개 지원)
                </span>
              </div>
              <CategoryList
                items={inds.map((i) => ({ key: i.key, label: i.label, cat: i.group }))}
                order={INDICATOR_GROUP_ORDER}
                selected={operand.indicator}
                onPick={pickIndicator}
              />
              {operand.indicator && (
                <div className="op-field hist-toggle">
                  <label className="hist-toggle-row">
                    <input type="checkbox"
                          checked={operand.kind === "history"}
                          onChange={(e) => toggleHistory(e.target.checked)} />
                    <span>최근 N일 ___ 으로 비교</span>
                  </label>
                  {operand.kind === "history" && (
                    <div className="op-field hist-row">
                      <label>최근</label>
                      <input type="number" min={1} max={500} value={operand.window ?? 20}
                        onChange={(e) => onChange({ ...operand, window: Math.max(1, Number(e.target.value)) })} />
                      <span className="txt">일</span>
                      {(operand.window ?? 20) > 250 && (
                        <span className="metric-hint lg"
                              style={{ background: "var(--amber-soft)", color: "var(--amber)" }}
                              data-tip="긴 window는 신생 종목·짧은 데이터에서 NaN(데이터 부족)으로 평가되어 조건 false. 250일 이하 권장.">⚠</span>
                      )}
                      <select value={operand.stat ?? "mean"}
                        onChange={(e) => onChange({ ...operand, stat: e.target.value as Stat })}>
                        {STAT_OPTIONS.map((o) => (
                          <option key={o.value} value={o.value}>{o.label}</option>
                        ))}
                      </select>
                      {operand.stat === "percentile" && (
                        <input type="number" min={0} max={100}
                          value={operand.percentile ?? 50}
                          onChange={(e) => onChange({ ...operand, percentile: Number(e.target.value) })} />
                      )}
                    </div>
                  )}
                </div>
              )}

              {operand.indicator && <AffineFields value={operand} onChange={onChange} />}
            </>
          )}
        </div>
      )}
    </span>
  );
}

