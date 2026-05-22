export interface IndicatorInfo {
  key: string; label: string; group: string;
  unit?: string;          // 표시 단위 (%, x, 일, 원 등)
  compare_group?: string; // 지표↔지표 비교 호환 그룹 키 (pct/rsi/price/...)
}
export interface SymbolInfo {
  symbol: string;
  name?: string;                    // KIS 마스터의 한글명 (있을 때)
  category: string;
  tradable: boolean;                // KIS 매수 가능 종목 (마스터에 존재)
  has_backtest_data?: boolean;      // 서버 dataset에 OHLC 보유 — 백테스트 가능
  rows: number;
  indicators: IndicatorInfo[];
}

export type Op = ">" | ">=" | "<" | "<=" | "between" | "cross_up" | "cross_down";
export type Logic = "AND" | "OR";
export type OperandKind = "indicator" | "constant" | "history";
export type Stat = "min" | "max" | "mean" | "percentile" | "lag";
export type ModifierKind = "streak" | "within";

/** Phase 41 — Operand.symbol에 이 sentinel을 넣으면 "각 매수 대상 종목" placeholder.
 *  평가 엔진이 current_symbol로 치환. 빌더 좌변 종목 드롭다운 첫 옵션. */
export const SELF_SYMBOL = "__SELF__";
export const SELF_LABEL = "[각 종목]";
export function isSelfRef(op?: Operand | null): boolean {
  return op?.symbol === SELF_SYMBOL;
}

export interface Operand {
  kind: OperandKind;
  symbol?: string;
  indicator?: string;
  value?: number | number[];      // constant — between이면 [min, max]
  stat?: Stat;                    // history
  window?: number;                // history — 롤링 기간(일)
  percentile?: number;            // history — stat="percentile"일 때 0~100
  // G1 — 아핀 변환: 해석된 값에 (× mul + add) 적용. 미지정이면 무변환.
  mul?: number | null;            // 예: MA20 × 1.05 → mul=1.05
  add?: number | null;            // 예: 등락률 + 2 → add=2
}
export interface Modifier { kind: ModifierKind; days: number }
export interface Condition {
  left: Operand;
  op: Op;
  right?: Operand;
  modifier?: Modifier | null;
}
/** G2 — 그룹의 원소는 단일 조건 또는 하위 그룹. (A AND B) OR C 표현 가능. */
export type ConditionNode = Condition | ConditionGroup;
export interface ConditionGroup { conditions: ConditionNode[]; logic: Logic }

/** 노드가 하위 그룹인지 — conditions 배열 보유로 판별 (단일 조건엔 없음). */
export function isGroupNode(n: ConditionNode): n is ConditionGroup {
  return (n as ConditionGroup).conditions !== undefined;
}

export interface ExitRules {
  hold_days?: number | null;
  take_profit?: number | null;
  stop_loss?: number | null;
  trail_atr_mult?: number | null;
  trail_pct?: number | null;
}

/** Phase 32 — 매도 규칙 통합. 익절/손절/트레일링/보유기간/매도 조건이 한 객체.
 *  먼저 트리거되는 규칙으로 매도. */
export interface SellRules {
  take_profit?: number | null;        // %
  stop_loss?: number | null;          // % (음수)
  trail_pct?: number | null;          // %
  trail_atr_mult?: number | null;     // × ATR_14
  hold_days?: number | null;          // 보유 일수
  conditions?: ConditionNode[];       // 자유 매도 조건 (dataset 평가) — G2 중첩 허용
  logic?: Logic;
  sell_amount_pct?: number;           // 100=전량 매도
}

/** 체결 정책 — 모든 필드 optional, null/undefined는 글로벌 default 적용.
 *  Backend: quant_core.exec_defaults.DEFAULT_EXECUTION과 병합. */
export interface ExecutionPolicy {
  sizing_mode?: "pct_cash" | "atr_risk";  // pct_cash=amount_pct 사용, atr_risk=ATR 변동성 기반
  atr_risk_pct?: number;                  // atr_risk 모드: 트레이드당 자본의 N% 위험
  atr_mult?: number;                      // ATR × 이 배수 = 1주당 손절폭
  max_position_pct?: number;              // 단일 종목 비중 상한 (자본 %)
  daily_loss_limit_pct?: number;          // 일일 손실 한도 (킬스위치 트리거)
  max_drawdown_pct?: number;              // 누적 손실 한도 (자본 고점 대비)
  buy_tolerance_pct?: number;             // 매수 지정가 = 전일 종가 × (1 + N%) — 갭상승 허용 범위
  sell_tolerance_pct?: number;            // 매도 지정가 = 전일 종가 × (1 - N%) — 갭하락 허용 범위 (Phase 38.9)
  // Phase 39 — 백테스트 비용 가정. 실매매(모의/실전) 영향 없음.
  bt_commission_bps?: number;             // 편도 수수료 (bps). 25 = 0.25%
  bt_slippage_bps?: number;               // 편도 슬리피지 (bps). 10 = 0.10%
  bt_gap_extra_cost?: boolean;            // 갭일 추가 비용 산입 여부 (갭의 절반)
  bt_gap_threshold_pct?: number;          // 이 % 이상 갭이면 추가 비용 발생
}

/** 사이징·리스크 default — exec_defaults.py와 동기. UI placeholder 및 신규 전략 default로 사용. */
export const EXECUTION_DEFAULTS: Required<ExecutionPolicy> = {
  sizing_mode: "atr_risk",
  atr_risk_pct: 1.0,
  atr_mult: 2.0,
  max_position_pct: 10.0,
  daily_loss_limit_pct: 3.0,
  max_drawdown_pct: 20.0,
  buy_tolerance_pct: 1.0,
  sell_tolerance_pct: 2.0,
  // Phase 39
  bt_commission_bps: 25,
  bt_slippage_bps: 10,
  bt_gap_extra_cost: true,
  bt_gap_threshold_pct: 1.0,
};

export interface StrategyDef {
  name: string;
  trade_symbol: string;
  buy: ConditionGroup;
  /** Phase 32 — 매도/청산 통합. 신규 전략은 이 필드만 사용. */
  sell_rules?: SellRules;
  /** [DEPRECATED — backend _migrate_legacy가 sell_rules로 흡수] */
  sell?: ConditionGroup | null;
  /** [DEPRECATED] */
  exit_rules?: ExitRules;
  /** [DEPRECATED — sell_rules.sell_amount_pct로 통합] */
  sell_amount_pct?: number;
  amount_pct: number;              // 자본 대비 매수 비율 (%) — sizing_mode=pct_cash일 때만 사용
  screener_limit?: number;         // 자동 선택 시 동시 보유 한도 (기본 5)
  // 커스텀 스크리너 — trade_symbol='screener:custom'일 때 프리셋 대신 사용.
  screener_spec?: ScreenerSpecIO | null;
  rebalance?: RebalanceIO | null;  // 자동 선택 리밸런싱 (라이브 전용)
  execution?: ExecutionPolicy | null;
  fill?: string;
}

export interface RebalanceIO {
  enabled: boolean;
  period: "daily" | "weekly" | "monthly";
}

// ── 스크리너 커스터마이징 ─────────────────────────────────────────────────────

export type ScreenerOp = ">" | ">=" | "<" | "<=" | "between";
export interface ScreenerRuleIO {
  field: string;
  op: ScreenerOp;
  value: number | number[];        // between이면 [min, max]
}
export interface ScreenerSpecIO {
  rules: ScreenerRuleIO[];
  sort?: { field: string; order: "asc" | "desc" } | null;
  markets?: string[];
  limit?: number;
}
export interface ScreenerField {
  key: string; label: string; unit: string; group: string;
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
  // Phase 38.7 — kill switch 일일 손실 한도(%). null이면 글로벌 default(3.0).
  kill_switch_daily_loss_pct: number | null;
  // Phase 38.10 — 누적 drawdown 한도(%). null이면 글로벌 default(20.0).
  max_drawdown_pct: number | null;
  // Phase 38.5 — preview 연속 누락 일수 알림 임계 (1+)
  preview_missing_alert_threshold: number;
  // Phase 40 — KIS ↔ ledger 정합성 drift 알림
  alert_on_reconcile_drift: boolean;
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
    // Phase 31 — 내일 매매 미리보기
    next_day_preview?: NextDayPreview;
    // Phase 40 — KIS 잔고 ↔ ledger 정합성
    reconciliation?: ReconciliationResult;
  };
  received_at: string; device_id: number;
}

/** Phase 40 — KIS 잔고 ↔ ledger drift 점검 결과 */
export interface ReconciliationResult {
  ledger_orphans: {
    symbol: string; ledger_total_qty: number; kis_qty: number;
    shortfall: number;
    ledger_sids: { sid: string; qty: number }[];
  }[];
  external_extras: {
    symbol: string; kis_qty: number; ledger_total_qty: number;
    excess: number; in_ledger: boolean;
  }[];
  in_sync: string[];
  checked_at: string;
  ledger_symbol_count: number;
  kis_symbol_count: number;
  applied?: {
    sid: string; symbol: string; old_qty: number; new_qty: number;
    removed_qty: number; fully_closed: boolean;
  }[];
  external_extras_count?: number;
  has_drift?: boolean;
  error?: string;
}

/** 내일 매매 미리보기 — 각 데이터 cron 후 서버가 평가해 sync snapshot에 merge */
export interface NextDayPreview {
  generated_at: string;
  data_source: string;          // cron 식별자 — 'dataset_global', 'krx_2nd' 등
  available: boolean;
  reason?: string;              // available=false일 때 사유
  summary?: {
    n_buy_candidates: number;
    est_total_buy_amount: number;
    n_holding: number;
    cash: number;
  };
  by_strategy?: PreviewByStrategy[];
  exit_candidates?: PreviewExit[];
}

export interface PreviewSignalDetail {
  label: string;
  passed: boolean | null;
  reason?: string | null;
}
export interface PreviewPerSymbolEval {
  passed: boolean;
  summary: string;
  details: PreviewSignalDetail[];
}
export interface PreviewByStrategy {
  strategy_id: number;
  strategy_name: string;
  trade_symbol: string;
  run_mode: string;
  signal_passed: boolean;
  candidates: PreviewBuyCandidate[];
  skipped: { symbol?: string; reason: string }[];
  // Phase 41 — 공통/종목별 신호 평가 결과
  signal_details?: PreviewSignalDetail[];      // 공통 조건 결과
  signal_summary?: string;                      // 공통 조건 한 줄 요약
  per_symbol_details?: Record<string, PreviewPerSymbolEval>;
}

export interface PreviewBuyCandidate {
  symbol: string;
  name: string;
  qty: number;
  prev_close: number;
  est_limit_price: number;
  est_total: number;
  sizing_mode: string;
  data_as_of: string | null;
}

export interface PreviewExit {
  symbol: string;
  name: string;
  qty: number;
  entry_price: number;
  prev_close: number;
  return_pct: number;
  peak_price: number;
}

// ── 종목 자동 선택 (Screener) ─────────────────────────────────────────────────

export interface ScreenerPreset {
  key: string;          // "marcap_top" 등
  title: string;        // "시가총액 상위"
  desc: string;
  spec?: ScreenerSpecIO; // 편집 시작점 — 프리셋의 룰 (presets 엔드포인트가 포함)
}

export interface ScreenerMatch {
  symbol: string;
  name: string;
  market: string;
  close: number | null;
  pct_change_1d: number | null;
  market_cap: number | null;
  trade_value: number | null;
  volume: number | null;
}

/** 매수 대상이 자동 선택 모드인지 — trade_symbol이 "screener:..."로 시작. */
export function parseScreenerKey(tradeSymbol: string): string | null {
  return tradeSymbol.startsWith("screener:")
    ? tradeSymbol.slice("screener:".length) : null;
}

/** trade_symbol을 모드와 종목 코드 배열로 파싱.
 *  - "screener:marcap_top" → { mode: "screener", symbols: ["marcap_top"] }  (preset key)
 *  - "005930,000660,035420" → { mode: "manual", symbols: [3개] }
 *  자동 선택과 수동 다중은 혼합 불가 — UI에서 모드 토글로 제어. */
export function parseTradeSymbols(tradeSymbol: string): {
  mode: "screener" | "manual";
  symbols: string[];
} {
  const s = (tradeSymbol ?? "").trim();
  if (s.startsWith("screener:")) {
    return { mode: "screener", symbols: [s.slice("screener:".length)] };
  }
  const parts = s.split(",").map((p) => p.trim()).filter(Boolean);
  return { mode: "manual", symbols: parts };
}

export type CommandType =
  | "RUN_CYCLE_NOW" | "PAUSE_AUTO" | "RESUME_AUTO"
  | "LIQUIDATE_ALL" | "CANCEL_ORDER" | "RESET_KILL_SWITCH"
  | "RECONCILE_NOW";   // Phase 40 — 수동 잔고 정합성 점검

export interface CommandRow {
  id: number; device_id: number; type: CommandType;
  params: Record<string, string | number>;
  status: "pending" | "delivered" | "done" | "failed";
  created_at: string; delivered_at: string | null;
  completed_at: string | null; result: Record<string, unknown>;
}
