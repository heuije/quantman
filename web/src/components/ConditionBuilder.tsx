import type {
  Condition, ConditionGroup, ModifierKind, Op, Operand, OperandKind,
  Stat, SymbolInfo,
} from "../types";

const OP_OPTIONS: { value: Op; label: string }[] = [
  { value: ">", label: "초과일 때" },
  { value: ">=", label: "이상일 때" },
  { value: "<", label: "미만일 때" },
  { value: "<=", label: "이하일 때" },
  { value: "between", label: "범위 안일 때" },
  { value: "cross_up", label: "상향돌파할 때" },
  { value: "cross_down", label: "하향돌파할 때" },
];

const STAT_OPTIONS: { value: Stat; label: string }[] = [
  { value: "mean", label: "평균" },
  { value: "max", label: "최댓값" },
  { value: "min", label: "최솟값" },
  { value: "percentile", label: "백분위" },
  { value: "lag", label: "전(前) 값" },
];

interface Props {
  symbols: SymbolInfo[];
  group: ConditionGroup;
  onChange: (g: ConditionGroup) => void;
}

/** 문장형 빈칸 채우기 조건 빌더 (좌변·연산자·우변·수식어 프레임워크). */
export default function ConditionBuilder({ symbols, group, onChange }: Props) {
  const symList = symbols.filter((s) => s.indicators.length > 0);

  const indicatorsOf = (sym?: string) =>
    symbols.find((s) => s.symbol === sym)?.indicators ?? [];

  /** 0을 자주 넘나드는 수익률 계열 지표를 기본값으로 선호. */
  function defaultIndicator(sym: string) {
    const inds = indicatorsOf(sym);
    return (inds.find((i) => i.key.includes("pct_change")
                          || i.key.includes("return")) ?? inds[0])?.key ?? "";
  }

  function update(i: number, patch: Partial<Condition>) {
    const conditions = group.conditions.map((c, idx) =>
      idx === i ? { ...c, ...patch } : c,
    );
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
              key={lg}
              type="button"
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
          <OperandPicker
            symbols={symbols}
            value={c.left}
            allowConstant={false}
            onChange={(o) => update(i, { left: o })}
          />
          <span className="txt">가(이)</span>

          {c.op === "between" ? (
            <RangeInput
              value={Array.isArray(c.right?.value) ? (c.right!.value as number[]) : [0, 0]}
              onChange={(v) => update(i, { right: { kind: "constant", value: v } })}
            />
          ) : (
            <OperandPicker
              symbols={symbols}
              value={c.right ?? { kind: "constant", value: 0 }}
              allowConstant
              onChange={(o) => update(i, { right: o })}
            />
          )}

          <select value={c.op} onChange={(e) => setOp(i, e.target.value as Op)}>
            {OP_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>

          <ModifierPicker
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

/** 피연산자(지표 / 숫자 / 이력통계) 선택기. */
function OperandPicker({ symbols, value, allowConstant, onChange }: {
  symbols: SymbolInfo[];
  value: Operand;
  allowConstant: boolean;
  onChange: (o: Operand) => void;
}) {
  const symList = symbols.filter((s) => s.indicators.length > 0);
  const indicatorsOf = (sym?: string) =>
    symbols.find((s) => s.symbol === sym)?.indicators ?? [];

  function setKind(k: OperandKind) {
    if (k === "constant") { onChange({ kind: "constant", value: 0 }); return; }
    const sym = value.symbol ?? symList[0]?.symbol ?? "";
    const ind = value.indicator ?? indicatorsOf(sym)[0]?.key ?? "";
    if (k === "indicator") {
      onChange({ kind: "indicator", symbol: sym, indicator: ind });
    } else {
      onChange({ kind: "history", symbol: sym, indicator: ind,
                 stat: "mean", window: 20 });
    }
  }

  function setSymbol(sym: string) {
    onChange({ ...value, symbol: sym, indicator: indicatorsOf(sym)[0]?.key ?? "" });
  }

  const kind = value.kind;

  return (
    <span className="operand">
      <select value={kind} onChange={(e) => setKind(e.target.value as OperandKind)}>
        <option value="indicator">지표</option>
        {allowConstant && <option value="constant">숫자</option>}
        <option value="history">이력통계</option>
      </select>

      {kind === "constant" && (
        <input
          type="number" step="any"
          value={typeof value.value === "number" ? value.value : 0}
          onChange={(e) => onChange({ kind: "constant", value: Number(e.target.value) })}
        />
      )}

      {(kind === "indicator" || kind === "history") && (
        <>
          <select value={value.symbol ?? ""} onChange={(e) => setSymbol(e.target.value)}>
            {symList.map((s) => (
              <option key={s.symbol} value={s.symbol}>{s.symbol}</option>
            ))}
          </select>
          <select
            value={value.indicator ?? ""}
            onChange={(e) => onChange({ ...value, indicator: e.target.value })}
          >
            {indicatorsOf(value.symbol).map((ind) => (
              <option key={ind.key} value={ind.key}>{ind.label}</option>
            ))}
          </select>
        </>
      )}

      {kind === "history" && (
        <>
          <span className="txt">의</span>
          <input
            type="number" min={1} title="롤링 기간(일)"
            value={value.window ?? 20}
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
        </>
      )}
    </span>
  );
}

/** between 연산자용 [min ~ max] 입력. */
function RangeInput({ value, onChange }: {
  value: number[];
  onChange: (v: number[]) => void;
}) {
  const lo = value[0] ?? 0;
  const hi = value[1] ?? 0;
  return (
    <span className="operand">
      <input
        type="number" step="any" value={lo}
        onChange={(e) => onChange([Number(e.target.value), hi])}
      />
      <span className="txt">~</span>
      <input
        type="number" step="any" value={hi}
        onChange={(e) => onChange([lo, Number(e.target.value)])}
      />
    </span>
  );
}

/** 수식어(지속성·최근성) 선택기. */
function ModifierPicker({ value, onChange }: {
  value: { kind: ModifierKind; days: number } | null;
  onChange: (m: { kind: ModifierKind; days: number } | null) => void;
}) {
  if (!value) {
    return (
      <button
        type="button" className="ghost sm"
        onClick={() => onChange({ kind: "streak", days: 3 })}
      >
        + 수식어
      </button>
    );
  }
  return (
    <span className="modifier">
      <select
        value={value.kind}
        onChange={(e) => onChange({ ...value, kind: e.target.value as ModifierKind })}
      >
        <option value="streak">연속</option>
        <option value="within">최근</option>
      </select>
      <input
        type="number" min={1} value={value.days}
        onChange={(e) => onChange({ ...value, days: Number(e.target.value) })}
      />
      <span className="txt">{value.kind === "streak" ? "일 연속" : "일 내"}</span>
      <button type="button" className="ghost sm" onClick={() => onChange(null)}>
        ×
      </button>
    </span>
  );
}
