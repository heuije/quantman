import { useState } from "react";
import type {
  Condition, ConditionGroup, ModifierKind, Op, Operand, OperandKind,
  Stat, SymbolInfo,
} from "../types";
import { CategoryList, SYMBOL_CAT_ORDER, usePopoverDismiss } from "./SymbolPicker";

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

// ── 헬퍼 ──────────────────────────────────────────────────────────────────────

function indLabel(symbols: SymbolInfo[], sym?: string, key?: string): string {
  return symbols.find((s) => s.symbol === sym)
    ?.indicators.find((i) => i.key === key)?.label ?? key ?? "";
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
    const conditions = group.conditions.map((c, idx) =>
      idx === i ? { ...c, ...patch } : c);
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

      {group.conditions.map((c, i) => (
        <div className="sentence" key={i}>
          <OperandChip
            symbols={symbols} value={c.left} allowConstant={false}
            onChange={(o) => update(i, { left: o })}
          />
          <span className="txt">가(이)</span>

          {c.op === "between" ? (
            <RangeInline
              value={Array.isArray(c.right?.value) ? (c.right!.value as number[]) : [0, 0]}
              onChange={(v) => update(i, { right: { kind: "constant", value: v } })}
            />
          ) : (
            <OperandChip
              symbols={symbols}
              value={c.right ?? { kind: "constant", value: 0 }}
              allowConstant
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

          <ModifierChip
            value={c.modifier ?? null}
            onChange={(m) => update(i, { modifier: m })}
          />

          <button
            type="button" className="ghost sm" style={{ marginLeft: "auto" }}
            onClick={() => remove(i)}
          >
            삭제
          </button>
        </div>
      ))}

      <button type="button" className="ghost sm" onClick={add}>
        + 조건 추가
      </button>
    </div>
  );
}

// ── 피연산자 칩 + 팝오버 ───────────────────────────────────────────────────────

function OperandChip({ symbols, value, allowConstant, onChange }: {
  symbols: SymbolInfo[];
  value: Operand;
  allowConstant: boolean;
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
            allowConstant={allowConstant} onChange={onChange}
          />
        </div>
      )}
    </span>
  );
}

function OperandEditor({ symbols, value, allowConstant, onChange }: {
  symbols: SymbolInfo[];
  value: Operand;
  allowConstant: boolean;
  onChange: (o: Operand) => void;
}) {
  const symList = symbols.filter((s) => s.indicators.length > 0);
  const indicatorsOf = (sym?: string) =>
    symbols.find((s) => s.symbol === sym)?.indicators ?? [];
  const [search, setSearch] = useState("");

  function setKind(k: OperandKind) {
    if (k === "constant") { onChange({ kind: "constant", value: 0 }); return; }
    const sym = value.symbol ?? symList[0]?.symbol ?? "";
    const inds = indicatorsOf(sym);
    const ind = inds.some((i) => i.key === value.indicator)
      ? value.indicator : inds[0]?.key ?? "";
    if (k === "indicator") {
      onChange({ kind: "indicator", symbol: sym, indicator: ind });
    } else {
      onChange({ kind: "history", symbol: sym, indicator: ind,
                 stat: value.stat ?? "mean", window: value.window ?? 20,
                 percentile: value.percentile });
    }
  }

  function pickSymbol(sym: string) {
    const inds = indicatorsOf(sym);
    const ind = inds.some((i) => i.key === value.indicator)
      ? value.indicator : inds[0]?.key ?? "";
    onChange({ ...value, symbol: sym, indicator: ind });
  }

  const kind = value.kind;

  return (
    <div className="op-editor">
      <div className="seg">
        <button type="button" className={kind === "indicator" ? "on" : ""}
                onClick={() => setKind("indicator")}>지표</button>
        {allowConstant && (
          <button type="button" className={kind === "constant" ? "on" : ""}
                  onClick={() => setKind("constant")}>숫자</button>
        )}
        <button type="button" className={kind === "history" ? "on" : ""}
                onClick={() => setKind("history")}>이력통계</button>
      </div>

      {kind === "constant" && (
        <div className="op-field">
          <label>값</label>
          <input
            type="number" step="any" autoFocus
            value={typeof value.value === "number" ? value.value : 0}
            onChange={(e) => onChange({ kind: "constant", value: Number(e.target.value) })}
          />
        </div>
      )}

      {(kind === "indicator" || kind === "history") && (
        <>
          <input
            className="pop-search" placeholder="종목 검색…" autoFocus
            value={search} onChange={(e) => setSearch(e.target.value)}
          />
          <div className="op-label">종목</div>
          <CategoryList
            items={symList.map((s) => ({ key: s.symbol, label: s.symbol, cat: s.category }))}
            order={SYMBOL_CAT_ORDER}
            selected={value.symbol}
            search={search}
            onPick={pickSymbol}
          />
          <div className="op-label">지표</div>
          <CategoryList
            items={indicatorsOf(value.symbol).map((i) =>
              ({ key: i.key, label: i.label, cat: i.group }))}
            order={INDICATOR_GROUP_ORDER}
            selected={value.indicator}
            onPick={(key) => onChange({ ...value, indicator: key })}
          />
          {kind === "history" && (
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
        </>
      )}
    </div>
  );
}

/** between 연산자용 [min ~ max] 입력. */
function RangeInline({ value, onChange }: {
  value: number[];
  onChange: (v: number[]) => void;
}) {
  const lo = value[0] ?? 0;
  const hi = value[1] ?? 0;
  return (
    <span className="operand">
      <input type="number" step="any" value={lo}
             onChange={(e) => onChange([Number(e.target.value), hi])} />
      <span className="txt">~</span>
      <input type="number" step="any" value={hi}
             onChange={(e) => onChange([lo, Number(e.target.value)])} />
    </span>
  );
}

/** 수식어(지속성·최근성) 칩. */
function ModifierChip({ value, onChange }: {
  value: { kind: ModifierKind; days: number } | null;
  onChange: (m: { kind: ModifierKind; days: number } | null) => void;
}) {
  if (!value) {
    return (
      <button type="button" className="chip ghost-chip"
              onClick={() => onChange({ kind: "streak", days: 3 })}>
        + 수식어
      </button>
    );
  }
  return (
    <span className="modifier">
      <select value={value.kind}
              onChange={(e) => onChange({ ...value, kind: e.target.value as ModifierKind })}>
        <option value="streak">연속</option>
        <option value="within">최근</option>
      </select>
      <input type="number" min={1} value={value.days}
             onChange={(e) => onChange({ ...value, days: Number(e.target.value) })} />
      <span className="txt">{value.kind === "streak" ? "일 연속" : "일 내"}</span>
      <button type="button" className="x-btn" onClick={() => onChange(null)}>×</button>
    </span>
  );
}
