export interface IndicatorInfo { key: string; label: string }
export interface SymbolInfo {
  symbol: string; tradable: boolean; rows: number; indicators: IndicatorInfo[];
}

export type Op = ">" | ">=" | "<" | "<=" | "between" | "cross_up" | "cross_down";
export type Logic = "AND" | "OR";
export type OperandKind = "indicator" | "constant" | "history";
export type Stat = "min" | "max" | "mean" | "percentile" | "lag";
export type ModifierKind = "streak" | "within";

export interface Operand {
  kind: OperandKind;
  symbol?: string;
  indicator?: string;
  value?: number | number[];      // constant — between이면 [min, max]
  stat?: Stat;                    // history
  window?: number;                // history — 롤링 기간(일)
  percentile?: number;            // history — stat="percentile"일 때 0~100
}
export interface Modifier { kind: ModifierKind; days: number }
export interface Condition {
  left: Operand;
  op: Op;
  right?: Operand;
  modifier?: Modifier | null;
}
export interface ConditionGroup { conditions: Condition[]; logic: Logic }

export interface ExitRules {
  hold_days?: number | null;
  take_profit?: number | null;
  stop_loss?: number | null;
  trail_atr_mult?: number | null;
  trail_pct?: number | null;
}

export interface StrategyDef {
  name: string;
  trade_symbol: string;
  buy: ConditionGroup;
  sell?: ConditionGroup | null;
  exit_rules: ExitRules;
  amount_pct: number;
  fill?: string;
}

export interface StrategyRow {
  id: number; name: string; run_mode: string;
  definition: StrategyDef; created_at: string; updated_at: string;
}

export interface BacktestResult {
  success: boolean; error?: string;
  metrics?: Record<string, number | null>;
  equity?: { date: string; value: number | null }[];
  benchmark?: { date: string; value: number | null }[];
  trades?: Record<string, string | number | null>[];
}

export interface AnalysisResult {
  success: boolean; error?: string;
  n_samples?: number; prob_positive?: number | null;
  mean?: number | null; median?: number | null;
  q25?: number | null; q75?: number | null; std?: number | null;
  t_stat?: number | null; p_value?: number | null;
  distribution?: (number | null)[]; condition_dates?: string[];
}

export interface DeviceRow {
  id: number; name: string; created_at: string; last_seen_at: string | null;
}

export interface SyncSnapshot {
  payload: {
    balance?: { cash: number; total_eval: number };
    positions?: { symbol: string; name?: string; qty: number;
                  avg_price?: number; eval_price?: number }[];
    equity?: { date: string; value: number }[];
    trades?: Record<string, string | number>[];
  };
  received_at: string; device_id: number;
}
