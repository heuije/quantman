import { useState } from "react";
import type {
  Condition, ConditionGroup, ConditionNode, IndicatorInfo, ModifierKind,
  Op, Operand, Stat, SymbolInfo,
} from "../types";
import { SELF_SYMBOL, SELF_LABEL, isSelfRef, isGroupNode } from "../types";
import { CategoryList, usePopoverDismiss } from "./SymbolPicker";
import TabbedSymbolList from "./TabbedSymbolList";

/** [이 종목] placeholder가 어떤 종목의 지표를 노출할지 결정.
 *  Phase 53 — 옛 "개별종목"(테스트용 애플·삼성전자 사용자 추가 종목) 제외하고
 *  일반 카테고리(자산·거시지표 등) 첫 indicators 가용 종목 사용. KR 종목 간 동일. */
function _selfIndicators(symbols: SymbolInfo[]): IndicatorInfo[] {
  const stock = symbols.find(
    (s) => s.category !== "개별종목" && s.indicators.length > 0);
  return stock?.indicators ?? [];
}

const OPERAND_TAB_ORDER = [
  "자산", "변동성", "금리·환율", "신용", "거시지표", "심리",
];

// ── 상수 ──────────────────────────────────────────────────────────────────────

const OP_GROUPS: { label: string; ops: { value: Op; label: string }[] }[] = [
  { label: "수준 비교", ops: [
    { value: ">",  label: "초과일 때" },
    { value: ">=", label: "이상일 때" },
    { value: "<",  label: "미만일 때" },
    { value: "<=", label: "이하일 때" },
    { value: "between", label: "범위 안일 때" },
  ]},
  { label: "크로스", ops: [
    { value: "cross_up",   label: "상향돌파할 때" },
    { value: "cross_down", label: "하향돌파할 때" },
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
}

/** 새 단일 조건의 기본값 — 좌변 [각 종목]·RSI, "30 미만". */
function starterCondition(symbols: SymbolInfo[]): Condition {
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
export default function ConditionBuilder({ symbols, group, onChange, contextNote }: Props) {
  return (
    <div>
      {contextNote && <div className="builder-note">{contextNote}</div>}
      <GroupEditor symbols={symbols} group={group} onChange={onChange} depth={0} />
    </div>
  );
}

/** 한 그룹(조건/하위그룹 묶음)을 렌더. depth=0이면 "묶음 추가" 노출(1단계 중첩 제한). */
function GroupEditor({ symbols, group, onChange, depth }: {
  symbols: SymbolInfo[];
  group: ConditionGroup;
  onChange: (g: ConditionGroup) => void;
  depth: number;
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
                <button type="button" className="ghost sm" onClick={() => remove(i)}>
                  묶음 삭제
                </button>
              </div>
              <GroupEditor
                symbols={symbols} group={node} depth={depth + 1}
                onChange={(g) => setNode(i, g)}
              />
            </div>
          ) : (
            <ConditionRow
              symbols={symbols} c={node}
              onPatch={(patch) => updateLeaf(i, patch)}
              onSetOp={(op) => setOp(i, op)}
              onRemove={() => remove(i)}
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

/** 단일 조건 문장 한 줄 — 좌변(종목·지표) · 수식어 · 우변(종목·지표 또는 숫자) · 연산자 · 삭제. */
function ConditionRow({ symbols, c, onPatch, onSetOp, onRemove }: {
  symbols: SymbolInfo[];
  c: Condition;
  onPatch: (patch: Partial<Condition>) => void;
  onSetOp: (op: Op) => void;
  onRemove: () => void;
}) {
  const leftGroup = compareGroupOf(symbols, c.left.symbol, c.left.indicator);
  return (
    <div className="sentence">
      <SymbolChip
        symbols={symbols} operand={c.left}
        onChange={(o) => onPatch({ left: o })}
      />
      <IndicatorChip
        symbols={symbols} operand={c.left}
        onChange={(o) => onPatch({ left: o })}
      />
      <span className="txt">가(이)</span>

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
    return (
      <>
        <span className="operand">
          <input type="number" step="any" value={v} placeholder={hint.range}
                 onChange={(e) => onChange({ kind: "constant", value: Number(e.target.value) })} />
        </span>
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

/** between 연산자용 [min ~ max] 입력. */
function RangeInline({ value, hintGroup, onChange }: {
  value: number[];
  hintGroup?: string;
  onChange: (v: number[]) => void;
}) {
  const lo = value[0] ?? 0;
  const hi = value[1] ?? 0;
  const hint = COMPARE_GROUP_HINTS[hintGroup ?? "other"] ?? COMPARE_GROUP_HINTS.other;
  return (
    <span className="operand">
      <input type="number" step="any" value={lo} placeholder={hint.range}
             onChange={(e) => onChange([Number(e.target.value), hi])} />
      <span className="txt">~</span>
      <input type="number" step="any" value={hi} placeholder={hint.range}
             onChange={(e) => onChange([lo, Number(e.target.value)])} />
    </span>
  );
}

/** 활성 수식어 — "OO가 [3일 연속] OO 미만일 때" 위치에 표시. */
function ActiveModifierChip({ value, onChange }: {
  value: { kind: ModifierKind; days: number };
  onChange: (m: { kind: ModifierKind; days: number } | null) => void;
}) {
  return (
    <span className="modifier">
      <input type="number" min={1} value={value.days}
             onChange={(e) => onChange({ ...value, days: Number(e.target.value) })} />
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
function IndicatorChip({ symbols, operand, onChange, compatGroup }: {
  symbols: SymbolInfo[];
  operand: Operand;
  onChange: (o: Operand) => void;
  compatGroup?: string;
}) {
  const [open, setOpen] = useState(false);
  const ref = usePopoverDismiss<HTMLSpanElement>(open, setOpen);
  // Phase 41 — SELF_SYMBOL은 KR 개별종목 indicators fallback
  const allInds = operand.symbol === SELF_SYMBOL
    ? _selfIndicators(symbols)
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
                      <input type="number" min={1} value={operand.window ?? 20}
                        onChange={(e) => onChange({ ...operand, window: Number(e.target.value) })} />
                      <span className="txt">일</span>
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

