export interface IndicatorInfo {
  key: string; label: string; group: string;
  unit?: string;          // 표시 단위 (%, x, 일, 원 등)
  compare_group?: string; // 지표↔지표 비교 호환 그룹 키 (pct/rsi/price/...)
}
export interface SymbolInfo {
  symbol: string; category: string; tradable: boolean; rows: number;
  indicators: IndicatorInfo[];
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
  amount_pct: number;              // 자본 대비 매수 비율 (%)
  sell_amount_pct?: number;        // 매도 시 보유분 청산 비율 (%) — 100=전량
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
  run_id?: number;
  run_created_at?: string;
}

export interface BacktestRunSummary {
  id: number;
  name: string;
  created_at: string;
  initial_capital: number;
  metrics: Record<string, number | null>;
  success: boolean;
}

export interface BacktestRunDetail {
  id: number;
  name: string;
  initial_capital: number;
  start?: string | null;
  end?: string | null;
  created_at: string;
  definition: StrategyDef;
  result: BacktestResult;
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

export interface PendingOrder {
  order_no: string; symbol: string; name?: string;
  side: "buy" | "sell"; qty: number; filled_qty?: number;
  remain_qty?: number; limit_price?: number; submitted_at?: string;
}

export interface OrderEvent {
  ts: string;
  event: "submitted" | "filled" | "partial" | "cancelled" | "rejected" | "timeout";
  side: "buy" | "sell"; symbol: string; qty: number;
  order_no?: string; intended_price?: number | null;
  limit_price?: number | null; fill_price?: number | null;
  strategy?: string; reason?: string; msg?: string;
}

export interface CycleSummary {
  today?: string; n_strategies?: number;
  n_bought?: number; n_sold?: number;
  n_skip_gap?: number; n_skip_signal?: number; n_skip_held?: number;
  n_rejected?: number; n_unfilled?: number; n_errors?: number;
  kill_switch?: boolean;
  equity_pre?: number; equity_post?: number;
}

export interface CycleRow {
  ts: string;
  decisions: { action: string; strategy_id: string; strategy_name: string;
                symbol: string; reason: string;
                prev_close?: number; cur_price?: number;
                intended?: number; fill?: number }[];
  summary: CycleSummary;
}

export interface SlippageStats {
  n: number;
  avg_bps: number | null; p50_bps: number | null;
  p95_bps: number | null; max_bps: number | null;
  recent: { ts: string; side: string; symbol: string;
             intended: number; fill: number; bps: number }[];
}

export interface KillSwitchState {
  active: boolean; since: string | null; reason: string;
  day_start_equity: number | null; day_start_date: string | null;
}

export interface PositionRich {
  symbol: string; name?: string; qty: number;
  avg_price?: number; eval_price?: number;
  strategy_name?: string; entry_date?: string;
  entry_price?: number; peak_price?: number;
  cur_return_pct?: number; held_days?: number;
  distances?: {
    tp_gap_pct?: number;
    sl_gap_pct?: number;
    trail_gap_pct?: number;
    hold_days_left?: number;
  };
}

export interface StrategyPnlRow {
  strategy: string; trades: number; win_rate: number;
  pnl: number; today_pnl: number; week_pnl: number; month_pnl: number;
}

export interface StrategyPnlSummary {
  by_strategy: StrategyPnlRow[];
  total: { today: number; week: number; month: number; all: number };
}

export interface SlippageBucket {
  bucket: string; n: number; avg_bps: number; max_bps: number;
}

export interface RejectionReason { label: string; n: number }

export interface DrawdownState {
  high?: number | null; current?: number | null;
  depth_pct: number; days_since_high: number; high_date?: string | null;
}

export interface LocalHealth {
  last_cycle_ts?: string | null;
  kis_token_expires_at?: string | null;
  kis_master_pushed_date?: string | null;
  warnings: string[];
}

export interface MarketIndicator {
  label: string; available: boolean;
  value?: number; change_pct?: number; as_of?: string;
}

export interface MarketContext {
  indicators: MarketIndicator[];
  session: { phase: string; kst_now: string };
}

export interface PortfolioRisk {
  positions: string[];
  matrix: number[][];
  sectors: { label: string; amount: number; share_pct: number }[];
  window: number;
}

export interface UserSettingsIO {
  alert_webhook_url: string;
  alert_on_killswitch: boolean;
  alert_on_daily_loss_pct: number;
  alert_on_unfilled_count: number;
}

export interface SyncSnapshot {
  payload: {
    balance?: { cash: number; total_eval: number };
    positions?: PositionRich[];
    equity?: { date: string; value: number }[];
    trades?: Record<string, string | number>[];
    decisions?: CycleRow["decisions"];
    broker_pending?: PendingOrder[];
    pending_local?: PendingOrder[];
    recent_orders?: OrderEvent[];
    recent_cycles?: CycleRow[];
    slippage?: SlippageStats;
    kill_switch?: KillSwitchState;
    cycle_summary?: CycleSummary;
    // Phase 13 — Monitor 고도화
    strategy_pnl?: StrategyPnlSummary;
    slippage_by_hour?: { buckets: SlippageBucket[] };
    rejection_reasons?: { reasons: RejectionReason[] };
    drawdown?: DrawdownState;
    health?: LocalHealth;
  };
  received_at: string; device_id: number;
}

export type CommandType =
  | "RUN_CYCLE_NOW" | "PAUSE_AUTO" | "RESUME_AUTO"
  | "LIQUIDATE_ALL" | "CANCEL_ORDER" | "RESET_KILL_SWITCH";

export interface CommandRow {
  id: number; device_id: number; type: CommandType;
  params: Record<string, string | number>;
  status: "pending" | "delivered" | "done" | "failed";
  created_at: string; delivered_at: string | null;
  completed_at: string | null; result: Record<string, unknown>;
}
