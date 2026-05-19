import type { Condition, ConditionGroup, Op, SymbolInfo } from "../types";

const OP_LABELS: { value: Op; label: string }[] = [
  { value: ">", label: "초과일 때" },
  { value: ">=", label: "이상일 때" },
  { value: "<", label: "미만일 때" },
  { value: "<=", label: "이하일 때" },
];

interface Props {
  symbols: SymbolInfo[];
  group: ConditionGroup;
  onChange: (g: ConditionGroup) => void;
}

/** 문장형 빈칸 채우기 조건 빌더. */
export default function ConditionBuilder({ symbols, group, onChange }: Props) {
  const symList = symbols.filter((s) => s.indicators.length > 0);

  function indicatorsOf(sym: string) {
    return symbols.find((s) => s.symbol === sym)?.indicators ?? [];
  }

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

  function changeSymbol(i: number, sym: string) {
    update(i, { symbol: sym, indicator: defaultIndicator(sym) });
  }

  function add() {
    const sym = symList[0];
    onChange({
      ...group,
      conditions: [
        ...group.conditions,
        { symbol: sym?.symbol ?? "", indicator: defaultIndicator(sym?.symbol ?? ""),
          op: "<", value: 0 },
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
          <select value={c.symbol} onChange={(e) => changeSymbol(i, e.target.value)}>
            {symList.map((s) => (
              <option key={s.symbol} value={s.symbol}>{s.symbol}</option>
            ))}
          </select>
          <span className="txt">의</span>
          <select
            value={c.indicator}
            onChange={(e) => update(i, { indicator: e.target.value })}
          >
            {indicatorsOf(c.symbol).map((ind) => (
              <option key={ind.key} value={ind.key}>{ind.label}</option>
            ))}
          </select>
          <span className="txt">가(이)</span>
          <input
            type="number" step="any" value={c.value}
            onChange={(e) => update(i, { value: Number(e.target.value) })}
          />
          <select
            value={c.op}
            onChange={(e) => update(i, { op: e.target.value as Op })}
          >
            {OP_LABELS.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
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
