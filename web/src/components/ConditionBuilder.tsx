import { useState } from "react";
import type {
  Condition, ConditionGroup, IndicatorInfo, ModifierKind, Op, Operand,
  Stat, SymbolInfo,
} from "../types";
import { CategoryList, usePopoverDismiss } from "./SymbolPicker";
import TabbedSymbolList from "./TabbedSymbolList";

const OPERAND_TAB_ORDER = [
  "자산", "변동성", "금리·환율", "신용", "거시지표", "심리", "개별종목",
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
  return symbols.find((s) => s.symbol === sym)
    ?.indicators.find((i) => i.key === key);
}

function indLabel(symbols: SymbolInfo[], sym?: string, key?: string): string {
  return findIndicator(symbols, sym, key)?.label ?? key ?? "";
}

function compareGroupOf(symbols: SymbolInfo[], sym?: string, key?: string): string {
  return findIndicator(symbols, sym, key)?.compare_group ?? "other";
}

function operandSummary(o: Operand | undefined, symbols: SymbolInfo[]): string {
  if (!o) return "?";
  if (o.kind === "constant") {
    if (Array.isArray(o.value)) return `${o.value[0]} ~ ${o.value[1]}`;
    return o.value != null ? String(o.value) : "0";
  }
  const lbl = indLabel(symbols, o.symbol, o.indicator);
  if (o.kind === "history") {
    return `${o.symbol ?? ""} ${lbl} ${o.window ?? 20}일 ${STAT_LABEL[o.stat ?? "mean"]}`;
  }
  return `${o.symbol ?? ""} · ${lbl}`;
}

// ── 메인 ──────────────────────────────────────────────────────────────────────

interface Props {
  symbols: SymbolInfo[];
  group: ConditionGroup;
  onChange: (g: ConditionGroup) => void;
}

/** 칩 + 분류·검색 팝오버 방식의 문장형 조건 빌더. */
export default function ConditionBuilder({ symbols, group, onChange }: Props) {
  const symList = symbols.filter((s) => s.indicators.length > 0);
  const indicatorsOf = (sym?: string) =>
    symbols.find((s) => s.symbol === sym)?.indicators ?? [];

  function defaultIndicator(sym: string) {
    const inds = indicatorsOf(sym);
    return (inds.find((i) => i.key.includes("pct_change")
                          || i.key.includes("return")) ?? inds[0])?.key ?? "";
  }

  function update(i: number, patch: Partial<Condition>) {
    const conditions = group.conditions.map((c, idx) => {
      if (idx !== i) return c;
      const next = { ...c, ...patch };
      // 좌측이 바뀌었고 우측이 지표/이력통계이며 새 좌측과 호환 그룹이 다르면 → 상수 0으로 reset
      if (patch.left && (next.right?.kind === "indicator" || next.right?.kind === "history")) {
        const lg = compareGroupOf(symbols, next.left.symbol, next.left.indicator);
        const rg = compareGroupOf(symbols, next.right.symbol, next.right.indicator);
        if (lg !== "other" && rg !== "other" && lg !== rg) {
          next.right = { kind: "constant", value: 0 };
        }
      }
      return next;
    });
    onChange({ ...group, conditions });
  }

  function setOp(i: number, op: Op) {
    const c = group.conditions[i];
    let right = c.right;
    const wasBetween = c.op === "between";
    if (op === "between" && !wasBetween) {
      right = { kind: "constant", value: [0, 0] };
    } else if (op !== "between" && wasBetween) {
      right = { kind: "constant", value: 0 };
    }
    update(i, { op, right });
  }

  function add() {
    const sym = symList[0]?.symbol ?? "";
    onChange({
      ...group,
      conditions: [
        ...group.conditions,
        {
          left: { kind: "indicator", symbol: sym, indicator: defaultIndicator(sym) },
          op: "<",
          right: { kind: "constant", value: 0 },
          modifier: null,
        },
      ],
    });
  }

  function remove(i: number) {
    onChange({ ...group, conditions: group.conditions.filter((_, idx) => idx !== i) });
  }

  return (
    <div>
      {group.conditions.map((c, i) => {
        const leftGroup = compareGroupOf(symbols, c.left.symbol, c.left.indicator);
        return (
          <div key={i}>
            <div className="sentence">
              {/* 좌측 — 종목과 지표를 두 chip으로 분리 (종목별로 지원 지표가 다름) */}
              <LeftSymbolChip
                symbols={symbols} operand={c.left}
                onChange={(o) => update(i, { left: o })}
              />
              <LeftIndicatorChip
                symbols={symbols} operand={c.left}
                onChange={(o) => update(i, { left: o })}
              />
              <span className="txt">가(이)</span>

              {/* 수식어 — 활성 시 "가(이)" 다음에 표시 ("OO가 3일 연속 OO 미만일 때") */}
              {c.modifier && (
                <ActiveModifierChip
                  value={c.modifier}
                  onChange={(m) => update(i, { modifier: m })}
                />
              )}

              {c.op === "between" ? (
                <RangeInline
                  value={Array.isArray(c.right?.value) ? (c.right!.value as number[]) : [0, 0]}
                  hintGroup={leftGroup}
                  onChange={(v) => update(i, { right: { kind: "constant", value: v } })}
                />
              ) : (
                <OperandChip
                  symbols={symbols}
                  value={c.right ?? { kind: "constant", value: 0 }}
                  allowConstant
                  compatGroup={leftGroup}
                  onChange={(o) => update(i, { right: o })}
                />
              )}

              <select className="op-select" value={c.op}
                      onChange={(e) => setOp(i, e.target.value as Op)}>
                {OP_GROUPS.map((g) => (
                  <optgroup key={g.label} label={g.label}>
                    {g.ops.map((o) => (
                      <option key={o.value} value={o.value}>{o.label}</option>
                    ))}
                  </optgroup>
                ))}
              </select>

              {/* 우측 끝 — + 수식어 (미활성 시) + 삭제 버튼 */}
              <span className="sentence-tail">
                {!c.modifier && (
                  <button
                    type="button" className="chip ghost-chip"
                    onClick={() => update(i, {
                      modifier: { kind: "streak", days: 3 },
                    })}
                  >
                    + 수식어
                  </button>
                )}
                <button
                  type="button" className="ghost sm"
                  onClick={() => remove(i)}
                >
                  삭제
                </button>
              </span>
            </div>

            {/* 조건 사이의 AND/OR 라벨 (마지막 조건엔 표시 안 함) */}
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
        );
      })}

      {/* 마지막 조건 아래에 AND/OR 토글 + 조건 추가 버튼 한 줄 */}
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
        <button type="button" className="ghost sm" onClick={add}>
          + 조건 추가
        </button>
      </div>
    </div>
  );
}

// ── 피연산자 칩 + 팝오버 ───────────────────────────────────────────────────────

function OperandChip({ symbols, value, allowConstant, compatGroup, onChange }: {
  symbols: SymbolInfo[];
  value: Operand;
  allowConstant: boolean;
  compatGroup?: string;            // 우측 피연산자에서, 좌측과 호환되는 지표만 노출
  onChange: (o: Operand) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = usePopoverDismiss<HTMLSpanElement>(open, setOpen);

  return (
    <span className="chip-wrap" ref={ref}>
      <button type="button" className="chip" onClick={() => setOpen((v) => !v)}>
        {operandSummary(value, symbols)}
        <span className="chip-caret">▾</span>
      </button>
      {open && (
        <div className="popover">
          <OperandEditor
            symbols={symbols} value={value}
            allowConstant={allowConstant}
            compatGroup={compatGroup}
            onChange={onChange}
          />
        </div>
      )}
    </span>
  );
}

function OperandEditor({ symbols, value, allowConstant, compatGroup, onChange }: {
  symbols: SymbolInfo[];
  value: Operand;
  allowConstant: boolean;
  compatGroup?: string;
  onChange: (o: Operand) => void;
}) {
  const symList = symbols.filter((s) => s.indicators.length > 0);
  const indicatorsOf = (sym?: string) =>
    symbols.find((s) => s.symbol === sym)?.indicators ?? [];

  // 지표/숫자 2개 탭만 유지. "이력통계"는 지표 탭의 "최근 N일" 토글로 통합.
  const tabKind: "indicator" | "constant" =
    value.kind === "constant" ? "constant" : "indicator";

  function setTab(k: "indicator" | "constant") {
    if (k === "constant") { onChange({ kind: "constant", value: 0 }); return; }
    const sym = value.symbol ?? symList[0]?.symbol ?? "";
    const inds = indicatorsOf(sym);
    const ind = inds.some((i) => i.key === value.indicator)
      ? value.indicator : inds[0]?.key ?? "";
    onChange({ kind: "indicator", symbol: sym, indicator: ind });
  }

  function pickSymbol(sym: string) {
    const inds = indicatorsOf(sym).filter((i) =>
      !compatGroup || compatGroup === "other"
        || (i.compare_group ?? "other") === compatGroup);
    const ind = inds.some((i) => i.key === value.indicator)
      ? value.indicator : inds[0]?.key ?? "";
    onChange({ ...value, kind: value.kind === "constant" ? "indicator" : value.kind,
                symbol: sym, indicator: ind });
  }

  function pickIndicator(key: string) {
    onChange({ ...value, kind: value.kind === "constant" ? "indicator" : value.kind,
                indicator: key });
  }

  function toggleHistory(enabled: boolean) {
    if (enabled) {
      onChange({ kind: "history",
                  symbol: value.symbol, indicator: value.indicator,
                  stat: value.stat ?? "mean", window: value.window ?? 20,
                  percentile: value.percentile });
    } else {
      onChange({ kind: "indicator",
                  symbol: value.symbol, indicator: value.indicator });
    }
  }

  // 좌측과 호환되는 지표만 노출 (compatGroup이 있을 때)
  const symHasCompat = (s: SymbolInfo) => {
    if (!compatGroup || compatGroup === "other") return s.indicators.length > 0;
    return s.indicators.some((i) =>
      (i.compare_group ?? "other") === compatGroup);
  };

  const visibleSymbols = symList.filter(symHasCompat);
  const visibleIndicators = indicatorsOf(value.symbol).filter((i) =>
    !compatGroup || compatGroup === "other"
      || (i.compare_group ?? "other") === compatGroup);

  const constVal = typeof value.value === "number" ? value.value : 0;
  const hint = COMPARE_GROUP_HINTS[compatGroup ?? "other"] ?? COMPARE_GROUP_HINTS.other;

  return (
    <div className="op-editor">
      {allowConstant && (
        <div className="seg">
          <button type="button" className={tabKind === "indicator" ? "on" : ""}
                  onClick={() => setTab("indicator")}>지표</button>
          <button type="button" className={tabKind === "constant" ? "on" : ""}
                  onClick={() => setTab("constant")}>숫자</button>
        </div>
      )}

      {tabKind === "constant" && (
        <div className="op-field op-const">
          <label>값</label>
          <input
            type="number" step="any" autoFocus
            value={constVal}
            placeholder={hint.range}
            onChange={(e) => onChange({ kind: "constant", value: Number(e.target.value) })}
          />
          {hint.tip && <div className="op-hint">{hint.tip}</div>}
        </div>
      )}

      {tabKind === "indicator" && (
        <>
          {/* Step 1: 종목 선택 (탭 기반) */}
          <div className="op-label">① 종목 선택</div>
          <TabbedSymbolList
            items={visibleSymbols.map((s) =>
              ({ key: s.symbol, label: s.symbol, cat: s.category }))}
            order={OPERAND_TAB_ORDER}
            selected={value.symbol}
            placeholder="종목 검색…"
            emptyMessage="호환되는 종목이 없습니다."
            onPick={pickSymbol}
          />

          {/* Step 2: 그 종목이 지원하는 지표 (종목별로 다름) */}
          {value.symbol && (
            <>
              <div className="op-label" style={{ marginTop: 14 }}>
                ② 지표 선택
                <span className="op-label-sub">
                  ({value.symbol} · {visibleIndicators.length}개 지원)
                </span>
              </div>
              {visibleIndicators.length === 0 ? (
                <div className="cat-empty">
                  {compatGroup && compatGroup !== "other"
                    ? "이 종목엔 호환되는 지표가 없습니다."
                    : "이 종목엔 사용 가능한 지표가 없습니다. (라이브 매매 전용)"}
                </div>
              ) : (
                <CategoryList
                  items={visibleIndicators.map((i) =>
                    ({ key: i.key, label: i.label, cat: i.group }))}
                  order={INDICATOR_GROUP_ORDER}
                  selected={value.indicator}
                  onPick={pickIndicator}
                />
              )}

              {value.indicator && (
                <div className="op-field hist-toggle">
                  <label className="hist-toggle-row">
                    <input type="checkbox"
                          checked={value.kind === "history"}
                          onChange={(e) => toggleHistory(e.target.checked)} />
                    <span>최근 N일 ___ 으로 비교</span>
                  </label>
                  {value.kind === "history" && (
                    <div className="op-field hist-row">
                      <label>최근</label>
                      <input
                        type="number" min={1} value={value.window ?? 20}
                        onChange={(e) => onChange({ ...value, window: Number(e.target.value) })}
                      />
                      <span className="txt">일</span>
                      <select
                        value={value.stat ?? "mean"}
                        onChange={(e) => onChange({ ...value, stat: e.target.value as Stat })}
                      >
                        {STAT_OPTIONS.map((o) => (
                          <option key={o.value} value={o.value}>{o.label}</option>
                        ))}
                      </select>
                      {value.stat === "percentile" && (
                        <input
                          type="number" min={0} max={100} title="백분위(%)"
                          value={value.percentile ?? 50}
                          onChange={(e) => onChange({ ...value, percentile: Number(e.target.value) })}
                        />
                      )}
                    </div>
                  )}
                </div>
              )}
            </>
          )}
        </>
      )}
    </div>
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

/** 좌측 operand의 종목 chip — 종목만 선택. 변경 시 호환 안되는 지표는 첫 지표로 reset. */
function LeftSymbolChip({ symbols, operand, onChange }: {
  symbols: SymbolInfo[];
  operand: Operand;
  onChange: (o: Operand) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = usePopoverDismiss<HTMLSpanElement>(open, setOpen);
  const symList = symbols.filter((s) => s.indicators.length > 0);
  const sel = symbols.find((s) => s.symbol === operand.symbol);
  const label = sel?.name ? `${sel.symbol} ${sel.name}`
    : operand.symbol || "종목 선택";

  function pickSymbol(sym: string) {
    const inds = symbols.find((s) => s.symbol === sym)?.indicators ?? [];
    const ind = inds.some((i) => i.key === operand.indicator)
      ? operand.indicator : inds[0]?.key ?? "";
    onChange({ ...operand, symbol: sym, indicator: ind });
    setOpen(false);
  }

  return (
    <span className="chip-wrap" ref={ref}>
      <button type="button" className="chip" onClick={() => setOpen((v) => !v)}>
        {label}<span className="chip-caret">▾</span>
      </button>
      {open && (
        <div className="popover popover-wide">
          <TabbedSymbolList
            items={symList.map((s) => ({
              key: s.symbol,
              label: s.name ? `${s.symbol} ${s.name}` : s.symbol,
              cat: s.category,
            }))}
            order={OPERAND_TAB_ORDER}
            selected={operand.symbol}
            placeholder="종목 검색…"
            onPick={pickSymbol}
          />
        </div>
      )}
    </span>
  );
}

/** 좌측 operand의 지표 chip — 선택된 종목이 지원하는 지표 + 이력 토글. */
function LeftIndicatorChip({ symbols, operand, onChange }: {
  symbols: SymbolInfo[];
  operand: Operand;
  onChange: (o: Operand) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = usePopoverDismiss<HTMLSpanElement>(open, setOpen);
  const inds = symbols.find((s) => s.symbol === operand.symbol)?.indicators ?? [];
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
              이 종목엔 사용 가능한 지표가 없습니다. (라이브 매매 전용)
            </div>
          ) : (
            <>
              <div className="op-label">
                지표 선택
                <span className="op-label-sub">
                  ({operand.symbol} · {inds.length}개 지원)
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
            </>
          )}
        </div>
      )}
    </span>
  );
}

