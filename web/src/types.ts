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
  asset_class?: string;             // "futures" | "equity" — 선물은 백테스트 선택가능(자동매매는 미지원)
  rows: number;
  // per-symbol 지표 배열은 제거됨(22k× 중복 = 43.5MB) — 지표 메타는 전역
  // indicator_catalog(/symbols 응답에 1회)로 받는다.
}

export type Op = ">" | ">=" | "<" | "<=" | "between" | "cross_up" | "cross_down";
export type Logic = "AND" | "OR";
export type OperandKind = "indicator" | "constant" | "history";
export type Stat = "min" | "max" | "mean" | "percentile" | "lag";
export type ModifierKind = "streak" | "within";

/** Phase 41 — Operand.symbol에 이 sentinel을 넣으면 "각 매수 대상 종목" placeholder.
 *  평가 엔진이 current_symbol로 치환. 빌더 좌변 종목 드롭다운 첫 옵션. */
export const SELF_SYMBOL = "__SELF__";

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
  sell_amount_pct?: number;           // 100=전량 매도 — 매도조건·미지정 룰의 fallback
  /** Phase 56 — 룰별 매도 비율. keys: "tp"/"sl"/"trail"/"atr"/"hold". 미설정 룰은 sell_amount_pct 적용. */
  rule_sell_pcts?: Record<string, number>;
}

/** 체결 정책 — 모든 필드 optional, null/undefined는 글로벌 default 적용.
 *  Backend: quant_core.exec_defaults.DEFAULT_EXECUTION과 병합. */
export interface ExecutionPolicy {
  /** 사이징 모드 (Phase 47 — 4지 통합):
   *  - fixed_amount: 한 종목당 amount_krw 원 (정액)
   *  - pct_cash:    자본의 amount_pct % (정률, default)
   *  - equal_weight: 자본을 screener_limit 종목에 균등 분배
   *  - atr_risk:    트레이드당 atr_risk_pct% 위험, 손절폭 ATR×atr_mult */
  sizing_mode?: "fixed_amount" | "pct_cash" | "equal_weight" | "atr_risk";
  amount_krw?: number;                    // fixed_amount 모드: 한 종목당 원 단위 금액
  atr_risk_pct?: number;                  // atr_risk 모드: 트레이드당 자본의 N% 위험
  atr_mult?: number;                      // ATR × 이 배수 = 1주당 손절폭
  max_position_pct?: number | null;       // 단일 종목 비중 상한 (자본 %). null=한도 없음
  max_drawdown_pct?: number | null;       // 누적 손실 한도 (자본 고점 대비). null=한도 없음
  // 발주 방식은 시장이 결정(국내=시장가·미국주식=지정가). tolerance는 **미국 지정가 버퍼 전용**
  // (라이브 체결 버퍼, 백테스트 무영향). 빈값이면 기본 ±3%. 유저가 빌더에서 전략별 조정.
  buy_tolerance_pct?: number;             // 미국 매수 지정가 = 신선한 현재가 × (1 + N%) — 갭상승 허용
  sell_tolerance_pct?: number;            // 미국 매도 지정가 = 신선한 현재가 × (1 − N%) — 갭하락 허용
  // Phase 39 + C-01 — 백테스트 비용 가정. 실매매(모의/실전) 영향 없음.
  bt_commission_bps?: number;             // 편도 위탁수수료 (bps). 3 = 0.03% (KIS 평균)
  bt_sell_tax_bps?: number;               // 매도 단방향 거래세 (bps). 23 = 0.23% (KOSPI/KOSDAQ 평균)
  bt_slippage_bps?: number;               // 편도 슬리피지 (bps). 10 = 0.10%
}

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
  amount_pct: number;              // 자본 대비 매수 비율 (%) — sizing_mode=pct_cash일 때 사용
  screener_limit?: number;         // 자동 선택 시 동시 보유 한도 (기본 5)
  // 커스텀 스크리너 — trade_symbol='screener:custom'일 때 프리셋 대신 사용.
  screener_spec?: ScreenerSpecIO | null;
  rebalance?: RebalanceIO | null;  // 자동 선택 리밸런싱 (라이브 전용)
  execution?: ExecutionPolicy | null;
  fill?: string;
}

export interface RebalanceIO {
  // off: lock-in (재평가·신규 매수 X) / hold: 빈 슬롯만 채움 / replace: 탈락 매도 + 신규
  mode: "off" | "hold" | "replace";
  period: "daily" | "weekly" | "monthly" | "every_n_days";
  every_n_days?: number | null;     // period="every_n_days"일 때만 사용 (영업일)
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
  /** 표시용 이름 (커스텀/내 세트). 백엔드 parse_spec은 무시. */
  label?: string;
}
export interface ScreenerField {
  key: string; label: string; unit: string; group: string;
}

export interface StrategyRow {
  id: number; name: string; run_mode: string;
  // 표현 엔진 — operand(레거시 row) | ir(전략 연구소). engine으로 분기해 좁혀 읽는다.
  engine?: "operand" | "ir";
  definition: StrategyDef | IrStrategyDef; created_at: string; updated_at: string;
  // Phase 59 — run_mode 전환 시점 기록
  paper_started_at?: string | null;
  live_started_at?: string | null;
}

// Phase 59 — 전략 버전 이력
export interface StrategyVersionRow {
  version_no: number;
  name: string;
  created_at: string;
  created_reason: string;     // "manual_edit" | "restore_from_vN" | "initial"
  definition?: StrategyDef;   // list endpoint에선 omit, single에선 포함
}

// Phase 59 — 전략 현황 (적용 기간 + 누적 손익 요약)
export interface StrategyStats {
  paper_started_at: string | null;
  live_started_at: string | null;
  days_paper: number | null;
  days_live: number | null;
  pnl_total: number | null;
  pnl_pct: number | null;
  traded_amount: number | null;     // 거래된 금액 (총 체결대금, KRW)
  win_rate: number | null;
  n_trades: number | null;
  n_positions: number;
  last_snapshot_at: string | null;
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

// ── 블록 IR (노코드 빌더) ────────────────────────────────────────────────────
// 자기서술 카탈로그(/ir/catalog)를 소비 — 프론트는 블록 지식을 하드코딩하지 않는다.

export interface IrNode {
  op: string;
  inputs?: Record<string, IrNode>;     // 가지 빈칸 — 슬롯명 → 하위 블록(재귀)
  params?: Record<string, unknown>;    // 잎 빈칸 — window·op·ref·value 등
}

export type IrValueType =
  "score" | "condition" | "scalar" | "label" | "distribution" | "resultset";

export interface IrParamSpec {
  name: string;
  // value_list = 문자열·숫자 혼용 리스트(섹터·버킷 등), bool = 체크박스
  kind: "ref" | "number" | "number_list" | "select" | "value_list" | "bool";
  label?: string;
  options?: string[];
  labels?: Record<string, string> | null;   // 문장형 UI — 옵션값→한글 조각
  default?: unknown;
  required?: boolean;
  min?: number;
  max?: number;
}

export interface IrBlockSpec {
  op: string;
  label: string;
  category: string;
  out_type: IrValueType;
  slots: Record<string, IrValueType>;     // 슬롯명 → 요구 타입
  variadic: boolean;
  variadic_type: IrValueType | null;
  params: IrParamSpec[];
  requires_panel: boolean;
  phrase?: string | null;    // 문장형 UI 템플릿 ({slot}/{param} 토큰; 없으면 generic 렌더)
  doc: string;
}

export interface IrIssue {
  rule: string; severity: number; message: string; path: string;
}

// StrategyIR(통합 IR) 직렬화 형태 — core ir_engine/spec.py StrategyIR과 동기.
// "전략 연구소" 저장/불러오기 라운드트립의 단일 표현. engine='ir' 전략의 definition.
export interface IrStrategyDef {
  name: string;
  universe: {
    kind: "single" | "list" | "all" | "portfolio";
    symbols?: string[];
    screener?: {
      condition: IrNode;
      refresh: "each_rebalance" | "once_at_start";
    } | null;
    exclude_macro?: boolean;
    weights?: Record<string, number> | null;   // portfolio 전용 — 보유 비중(없으면 동일가중)
  };
  signal: IrNode;
  position: {
    direction: "long" | "short" | "long_short";
    sizing: {
      mode: string;
      amount_pct?: number; amount_krw?: number | null;
      target_vol_pct?: number | null; weights?: Record<string, number> | null;
      vol_window?: number; max_position_pct?: number;
      futures_margin_pct?: number;   // 선물 증거금 사용률(%) — 진입 시 가용현금의 N%를 증거금으로. 레버리지 안전상한(기본 20·100%=full-margin)
    };
    entry: {
      mode: string; rebalance?: string; every_n_days?: number | null;
      top_n?: number | null; top_pct?: number | null;
      threshold?: number | null; refill?: string;
    };
    exit: {
      hold_days?: number | null; take_profit?: number | null; stop_loss?: number | null;
      trail_pct?: number | null; trail_atr_mult?: number | null; condition?: IrNode | null;
    };
    overlays: {
      vol_target?: number | null; turnover_damp?: number | null;
      max_drawdown_stop?: number | null; max_drawdown_soft?: number | null;
      max_group_pct?: number | null; group_label?: IrNode | null;
    };
  };
  simulation: {
    initial_capital?: number; delay?: number; fill?: string;
    commission?: number | null; slippage?: number | null; sell_tax?: number | null;
    leverage?: number;
    short_borrow_pct?: number | null; funding_cost_pct?: number | null; rfr_pct?: number | null;
    maintenance_margin_pct?: number | null;    // 레버리지 마진콜 유지증거금률(%)
    start?: string | null; end?: string | null;
  };
  // 자동매매 체결 정책 — 미국 지정가 tolerance 등. 미지정 시 글로벌 default(국내=시장가).
  execution?: ExecutionPolicy | null;
  // 조사형 쿼리 — query(동사) × study(축 × 환원). 옛 sweep+period_split을 흡수.
  // describe=신호값 분포, relate=이벤트/IC, simulate=백테스트(+축별 펼침·기간분할),
  // select=as-of 스냅샷 횡단 랭킹 스크리닝.
  query?: "select" | "describe" | "relate" | "simulate";
  study?: {
    axis?: "none" | "parameter" | "entity" | "label" | "time_fold";
    reduction?: "enumerate" | "contrast" | "consistency" | "extremize";
    param_grid?: { path: string; values: (number | string)[] }[];
    assets?: string[];                         // axis=entity
    label?: IrNode | null;                     // 국면 라벨 블록(축 또는 조건 분할)
    folds?: number;                            // axis=time_fold — 균등 분할 수
    split_dates?: string[];                    // axis=time_fold — 명시 분할 시점(워크포워드)
    target_node?: IrNode | null;               // describe/relate 분석 노드
    relation_kind?: "ic" | "regression";       // relate — IC(단일) 또는 다중팩터 회귀
    factors?: IrNode[];                         // relation_kind=regression 설명변수
    event?: IrNode | null;                     // relate(이벤트) — 별도 이벤트 조건
    windows?: number[];                        // relate — forward/예측 윈도우
    event_basis?: "close" | "intraday" | "excess";
    objective?: {                              // reduction=extremize 전용 목적함수
      metric?: "sharpe" | "sortino" | "cagr" | "cum_return" | "mdd";
      direction?: "max" | "min"; oos_guard?: boolean;
    } | null;
  };
  // query="select" 전용 — as-of 스냅샷 횡단 랭킹 선별 설정(core SelectSpec과 동기).
  select?: {
    as_of?: string; top_n?: number; top_pct?: number;
    descending?: boolean; display?: string[];
  };
}

// query="describe" + universe.kind="single" — 단일종목 360 리포트 결과.
// 종목 뉴스 헤드라인(네이버 검색 API) — 360 리포트 '왜 움직였나' facet.
export interface NewsItem {
  title: string; link: string; desc: string; pub: string | null;
}

// forward 추정실적(estimate.earnings_kr·FnGuide) — 서버 엣지 enrich(뿌리①). 데이터 없으면 미부착.
export interface Estimates {
  source?: string;
  forward?: {
    fiscal_actual?: string; fiscal_forward?: string;
    rev_growth?: number | null; op_growth?: number | null; ni_growth?: number | null;
    eps_forward?: number | null; forward_pe?: number | null;
    op_margin_forward?: number | null; roe_forward?: number | null;
  };
  annual?: {
    years: string[]; is_estimate: boolean[];
    rev?: (number | null)[]; op?: (number | null)[]; ni?: (number | null)[];
    eps?: (number | null)[]; op_margin?: (number | null)[];
  };
}

export interface IrSingleReport {
  success: boolean; query: "describe"; report: "single";
  symbol: string; sector: string; as_of: string; data_points: number;
  price: {
    last: number;
    returns: Record<"1m" | "3m" | "6m" | "12m", number | null>;
    high_52w: number; low_52w: number; pct_from_52w_high: number | null;
  };
  risk: { vol_annualized: number | null; max_drawdown: number | null };
  fundamentals: Record<"pb_ratio" | "trailing_pe" | "ev_ebitda", number | null>;
  consensus?: {        // 애널 컨센서스(KR 라이브 main #149). 미커버면 값 null.
    consensus_target: number | null; target_upside: number | null;
    consensus_opinion: number | null; analyst_count: number | null;
    target_revision_pct: number | null; days_since_report: number | null;
  };
  flow?: {             // 수급(기관·외국인 순매수, 원) — 최신일 + 최근 20거래일 누적.
    inst_net_buy: number | null; inst_net_buy_20d: number | null;
    foreign_net_buy: number | null; foreign_net_buy_20d: number | null;
  };
  news?: NewsItem[];   // 서버 엣지 enrich(키 미설정·이름 미해석이면 빈 배열)
  estimates?: Estimates;   // forward 추정실적(FnGuide·웹 직접 /ir/strategy 경로)
}

// query="describe" + universe.kind="portfolio" — 포트폴리오 진단 결과.
export interface IrPortfolioDiagnosis {
  success: boolean; query: "describe"; report: "portfolio";
  as_of: string; n_holdings: number;
  holdings: { symbol: string; weight: number; sector: string }[];
  concentration: { hhi: number; effective_n: number | null; top_weight: number; top3_weight: number };
  sector_exposure: Record<string, number>;
  valuation: { weighted_pb: number | null; weighted_pe: number | null };
  risk: { portfolio_vol_annualized: number | null; avg_pairwise_corr: number | null };
  coverage: { with_price: number; with_fundamentals: number };
}

// reduction="extremize" — 최적해 + OOS 과최적화 가드 결과.
export interface IrExtremizeResult {
  success: boolean; axis: "parameter" | "asset"; reduction: "extremize";
  objective: { metric: string; direction: string; oos_guard: boolean };
  best: { label: string; metric_value: number | null; perf: Record<string, number> };
  ranked: { label: string; metric_value: number | null }[];
  oos_guard?: { buckets?: Record<string, unknown>; consistency?: unknown; error?: string };
}

// query="relate" + relation_kind="regression" — 다중팩터 Fama-MacBeth 회귀 결과.
export interface IrRegressionResult {
  success: boolean; axis: "relation"; relation: "regression";
  windows: string[]; factor_names: string[];
  by_window: Record<string, {
    n_periods: number;
    factors: { name: string; coef: number; se: number; t_stat: number | null;
               t_inf: boolean; ci_low: number; ci_high: number }[] | null;
    note?: string;
  }>;
}

// 모든 펼침 버킷의 단일 지표 어휘 (engine perf_from_returns와 동기) — 갭 A.
export interface IrSweepBucket {
  n: number;
  mean?: number; std?: number; sharpe?: number; sortino?: number;
  cum_return?: number; cagr?: number; mdd?: number; win_rate?: number;
  payoff_ratio?: number; profit_factor?: number; var_95?: number; cvar_95?: number;
  error?: string;
}

// 이벤트 표본 통계 — 종점 유의성 + 경로지표(MAE/MFE). 갭 C·E.
export interface IrEventStat {
  n: number; mean?: number; t_stat?: number; p_value?: number; prob_positive?: number;
  mean_mae?: number; worst_mae?: number; mean_mfe?: number; payoff_ratio?: number;
}
export interface IrPairTest {
  p_value?: number; mean_diff?: number; mean_a?: number; mean_b?: number;
  n_a?: number; n_b?: number;
}

// 신호값 분포 (target=signal) — 비율 스케일(pct=false). 분위수·왜도/첨도·부트스트랩 CI.
export interface IrDistribution {
  n: number; mean?: number; std?: number; skew?: number; kurtosis?: number;
  quantiles?: { q05?: number; q10?: number; q25?: number; q50?: number;
                q75?: number; q90?: number; q95?: number };
  bootstrap_ci?: { low?: number; high?: number };
}
// 국면별 분포 비교 (compare_partition) — 신호값 분석의 by_regime 형태.
export interface IrPartition {
  by_label: Record<string, IrDistribution>;
  pairwise: Record<string, IrPairTest>;
}
// 횡단 IC 통계 (target=relation) — one_sample_test + IR(정보비율).
export interface IrICStat {
  n: number; mean?: number; t_stat?: number; p_value?: number;
  prob_positive?: number; ir?: number;
}

// StrategyIR 백테스트 결과 — 단일(equity/metrics)·펼침(axis/buckets)·이벤트(time) 통합.
// P5b PRESCRIBE — 목적별 최적 비중 + 포트 지표(연율화)
export interface PrescribeObjective {
  weights: Record<string, number>;
  exp_return: number | null;
  exp_vol: number | null;
  sharpe: number | null;
}
export interface PrescribeResult {
  symbols?: string[];
  objectives?: Record<string, PrescribeObjective>;
  recommended?: string;
  n_obs?: number;
  max_weight?: number | null;
  warnings?: IrIssue[];
}

// 뉴스 리서치 (shape "news_research") — Haiku 증거 다이제스트 + 결정적 인용
export interface NewsDigestResult {
  digest?: string;
  citations?: { n: number; title: string; url: string; date: string; source: string }[];
  period?: { kind: string; days?: number; start?: string; end?: string };
  n?: number;
  sources?: string[];
}

// P5c 시장 breadth — 종목군 등락·MA 상회·섹터 분산
export interface BreadthResult {
  n?: number;
  n_up?: number;
  n_down?: number;
  n_flat?: number;
  pct_up?: number | null;
  avg_r1?: number | null;
  avg_r5?: number | null;
  avg_r20?: number | null;
  pct_above_ma20?: number | null;
  pct_above_ma60?: number | null;
  top_gainers?: [string, number][];
  top_losers?: [string, number][];
  sector_breakdown?: [string, number, number][];
}

export interface IrStrategyResult extends BacktestResult {
  warnings?: IrIssue[];
  issues?: IrIssue[];
  // P3 seam #1 — 엔진(run_query)이 스탬프한 canonical 형상 태그. ChatResultView가 이 키로
  // 렌더러 레지스트리를 단일 조회(없으면 deriveShape 폴백). select/describe_single/…/simulate.
  shape?: string;
  // P4 context 사이드카 — 서버가 엔진 밖에서 붙인 준실시간 시세·뉴스·시장스냅샷(골든 무누출·표시 전용).
  context?: {
    quotes?: Record<string, { price: number | null; chg: number | null; change?: number | null }>;
    news?: { title: string; link: string; desc?: string; pub?: string }[];
    market?: Record<string, { price: number | null; chg: number | null; change?: number | null }>;
    estimates?: Estimates;   // forward 추정실적(FnGuide·챗 경로) — ContextCard 렌더
    source?: string;
  };
  // P5a 상관행렬 (relation="correlation") — symbols 순서의 대칭 행렬 + 요약 쌍.
  symbols?: string[];
  matrix?: (number | null)[][];
  avg_corr?: number | null;
  n_obs?: number;
  most_correlated?: [string, string, number] | null;
  least_correlated?: [string, string, number] | null;
  // P5b PRESCRIBE (query="prescribe") — 목적별 최적 비중.
  objectives?: Record<string, PrescribeObjective>;
  recommended?: string;
  max_weight?: number | null;
  // 결과 dict의 axis는 백엔드가 옛 라벨을 parity로 유지한다(요청의 study.axis 신값과
  // 다를 수 있음 — 예: study.axis="entity" 요청 → 결과 axis="asset"). 표시 전용.
  axis?: "condition" | "parameter" | "asset" | "time" | "period_split" | "signal" | "relation";
  buckets?: Record<string, IrSweepBucket>;
  overall?: IrSweepBucket | Record<string, IrEventStat> | IrDistribution;
  // target=relation(IC) — 윈도우별 IC 통계 + (선택)국면별
  relation?: string;
  by_window?: Record<string, { overall: IrICStat; by_regime?: IrPartition | null }>;
  // parameter축 격자 메타 (다축 Cartesian) — 갭 B
  axes?: { path: string; values: (number | string)[] }[];
  // time_fold(기간분할) 일관성 — study.reduction="consistency" 결과
  consistency?: { n_folds: number; positive_folds: number; consistency: number };
  // condition축 유의성 (A1)
  compare?: { pairwise?: Record<string, IrPairTest> };
  // time축 이벤트 스터디 (A2) — basis: close/intraday/excess (갭 C)
  windows?: string[];
  basis?: "close" | "intraday" | "excess";
  n_events?: number;
  by_regime?: Record<string, {
    by_regime: Record<string, IrEventStat>;
    pairwise: Record<string, IrPairTest>;
  }>;

  // ── 신규 동사(select·describe·relate-regression·extremize) 식별·본문 ──
  // 런타임 dict는 이미 이 키들을 가짐(엔진 산출). ResultPanel 라우팅이 query/report/
  // reduction/relation으로 분기한 뒤, 본문은 전용 결과 타입으로 좁혀 컴포넌트에 넘긴다.
  query?: "select" | "describe" | "relate" | "simulate";   // 동사
  report?: "single" | "portfolio";                         // describe 대상 분기
  reduction?: string;                                       // "extremize" 등 환원 식별
  // select(as-of 스냅샷 횡단 랭킹) 본문 + 자기서술 계약(뿌리③)
  results?: SelectRow[];
  columns?: SelectColumn[];        // 필드 메타(라벨·단위·배율·포맷·방향) — 표 렌더 단일출처
  scoring?: { recipe?: string; factors?: string[]; normalization?: string;
              sector_relative?: boolean; direction?: string };   // 점수 산식(투명)
  group_by?: string;
  groups?: { group: string; results: SelectRow[] }[];            // 섹터별 묶음(비교표)
  as_of?: string;
  universe_size?: number;
  eligible_size?: number;
}

export interface SelectRow {
  symbol: string; code?: string; name?: string;
  score: number | null; sector: string; metrics: Record<string, number | null>;
}
export interface SelectColumn {
  key: string; label: string; kind?: string;
  unit?: string; scale?: number; format?: string; direction?: string;
}

export interface BacktestRunSummary {
  id: number;
  name: string;
  created_at: string;
  initial_capital: number;
  metrics: Record<string, number | null>;
  success?: boolean;
  // Phase 59 — 전략 detail의 "백테스트 내역" 응답
  version_no?: number | null;
  start?: string | null;
  end?: string | null;
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
  n_skip_held?: number;
  n_rejected?: number; n_unfilled?: number; n_errors?: number;
  n_unparseable_orphan?: number;   // 청산 규칙 파싱 불가 고아(삭제·구버전 전략 보유분)
  kill_switch?: boolean;
  equity_pre?: number; equity_post?: number;
  // 미국 해외 실시간 시세 미신청 — 장중 실시간 손절 미제공 (P8)
  us_realtime_unavailable?: boolean;
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
  // Phase 47 Cycle C — 분할매수 진행 상황 (없으면 단일 진입)
  phases_executed?: number[];
  phases_total?: number;
  base_qty?: number;
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

// ── 대시보드 탭 — 개별 종목 on-demand 조회 ──────────────────────────────────
export interface SymbolListing { symbol: string; name: string; market: string }
export interface IndicatorSpec {
  key: string; label: string; pane: "price" | "sub"; fields?: string[];
}
export interface SymbolPoint {
  date: string;
  open: number | null; high: number | null; low: number | null; close: number | null;
  volume: number | null; chg_pct: number | null;
  ma5: number | null; ma20: number | null; ma60: number | null;
  ma120: number | null; ma240: number | null;
  // 거래량 이동평균 (거래량 차트 오버레이)
  vma5: number | null; vma20: number | null; vma60: number | null;
  vma120: number | null; vma240: number | null;
  // 벤치마크 지수(종목 시작가로 리베이스) — 주가차트 초록 점선 오버레이
  bench: number | null;
  bb_upper: number | null; bb_mid: number | null; bb_lower: number | null;
  rsi_14: number | null;
  macd: number | null; macd_signal: number | null; macd_hist: number | null;
  stoch_k: number | null; stoch_d: number | null;
  atr_14: number | null; obv: number | null; vol_20d: number | null;
  // 확장 기술지표 38종 (dashboard_indicators.py와 동기)
  ema20: number | null; ema60: number | null; wma20: number | null; vwap: number | null;
  env_upper: number | null; env_lower: number | null;
  kc_upper: number | null; kc_mid: number | null; kc_lower: number | null;
  dc_upper: number | null; dc_mid: number | null; dc_lower: number | null;
  psar: number | null;
  ichi_tenkan: number | null; ichi_kijun: number | null;
  ichi_spanA: number | null; ichi_spanB: number | null;
  supertrend: number | null;
  cci: number | null; williams_r: number | null; roc: number | null; momentum: number | null;
  stochrsi_k: number | null; stochrsi_d: number | null; trix: number | null;
  uo: number | null; ao: number | null;
  plus_di: number | null; minus_di: number | null; adx: number | null;
  aroon_up: number | null; aroon_down: number | null; vi_plus: number | null; vi_minus: number | null;
  dpo: number | null; ppo: number | null; ppo_signal: number | null; ppo_hist: number | null;
  disparity: number | null; psy_line: number | null; kst: number | null; kst_signal: number | null;
  coppock: number | null; bull_power: number | null; bear_power: number | null;
  mfi: number | null; cmf: number | null; chaikin_osc: number | null; force_index: number | null;
  eom: number | null; vr: number | null; ad_line: number | null;
  bb_pct_b: number | null; bb_bw: number | null; stddev_20: number | null; mass_index: number | null;
}
export interface SymbolDetail {
  symbol: string; currency: string; range: string;
  last: {
    date: string; close: number | null; change_pct: number | null;
    rsi_14: number | null; volume: number | null;
    ma20: number | null; ma60: number | null;
    macd: number | null; stoch_k: number | null; atr_14: number | null; vol_20d: number | null;
    beta: number | null; benchmark: string;
    high_52w: number | null; low_52w: number | null;
  };
  indicators: IndicatorSpec[];
  series: SymbolPoint[];
}

// 다종목 비교 — 종목별 소형 캔들차트(small-multiples). OHLC + 단기 MA.
export interface ComparePoint {
  date: string;
  open: number | null; high: number | null; low: number | null; close: number | null;
  ma5: number | null; ma20: number | null; ma60: number | null;
}
export interface CompareItem { symbol: string; name: string; currency: string; series: ComparePoint[] }
export interface CompareResult { items: CompareItem[]; range: string }

// ── 포트폴리오 탭 — 현재 vs 예상 비교 분석 ──────────────────────────────────
export interface PositionInput { symbol: string; weight: number }
export interface PortfolioInput { label: string; positions: PositionInput[] }
export interface PortfolioAnalyzeIn {
  current?: PortfolioInput | null;
  proposed?: PortfolioInput | null;
  benchmark?: string;
  years?: number;
}
export interface EquityPoint { date: string; value: number }
export interface PortfolioLeg {
  label: string;
  symbols: string[];
  weights: Record<string, number>;
  metrics: {
    cagr: number | null; vol: number | null; sharpe: number | null;
    mdd: number | null; cum_return: number | null; var_95: number | null;
    beta: number | null; excess_return: number | null;
  };
  equity: EquityPoint[];
  benchmark_equity: EquityPoint[] | null;
}
export interface PortfolioAnalysis {
  benchmark: string; years: number;
  current: PortfolioLeg | null;
  proposed: PortfolioLeg | null;
}
export interface PortfolioHoldings {
  linked: boolean;
  positions: { symbol: string; weight: number }[];
}

export interface UserSettingsIO {
  alert_webhook_url: string;
  alert_on_killswitch: boolean;
  alert_on_daily_loss_pct: number;
  alert_on_unfilled_count: number;
  // Phase 48 P1-C — 슬리피지 임계 초과 알림 (bps, 0=비활성)
  alert_on_slippage_bps: number;
  // Phase 48 P1-D — 일일 거래 한도 (0=비활성)
  daily_turnover_limit_krw: number;
  daily_trade_count_limit: number;
  // Phase 38.7 — kill switch 일일 손실 한도(%). null이면 글로벌 default(3.0).
  kill_switch_daily_loss_pct: number | null;
  // Phase 38.10 — 누적 drawdown 한도(%). null이면 글로벌 default(20.0).
  max_drawdown_pct: number | null;
  // Phase 38.5 — preview 연속 누락 일수 알림 임계 (1+)
  preview_missing_alert_threshold: number;
  // Phase 40 — KIS ↔ ledger 정합성 drift 알림
  alert_on_reconcile_drift: boolean;
  // 미국 매수여력 모드: "integrated"(통합증거금, KRW 담보·FX 노출) |
  // "usd_cash"(USD 예수금 한정, 보수적)
  us_buying_power_mode: "integrated" | "usd_cash";
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
    // 자동매매 스케줄러 상태 — 로컬앱이 모든 push에 실어 보냄(실시간 동기화).
    auto_status?: "running" | "paused" | "stopped";
  };
  received_at: string; device_id: number | null;
  // Phase 58 — 5분 주기 heartbeat. snapshot보다 최신이면 "살아있음" 지표로
  // 사용. 정규장 외(새벽 등) cycle 없을 때도 alive 표시 가능.
  last_heartbeat_at?: string | null;
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
  // 미국 종목(Phase 60+)은 server에서 사이징 불가 → qty/est_limit_price/est_total
  // 모두 null. trader가 발주 시점에 USD 잔고로 결정. currency="USD" 표시.
  qty: number | null;
  prev_close: number;
  est_limit_price: number | null;
  est_total: number | null;
  sizing_mode: string;
  data_as_of: string | null;
  currency?: "KRW" | "USD";
  note?: string;
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
  // 국내("KR") / 미국("US") — 웹이 컨텍스트별 섹션으로 노출. 통화·단위 표기에도 사용.
  market_group?: "KR" | "US";
}

/** 계정에 저장된 사용자 정의 세트. */
export interface ScreenerUserPreset {
  id: number;
  name: string;
  spec: ScreenerSpecIO;
  created_at: string;
  updated_at: string;
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

// 자동매매 타임라인 — /trading/timeline 응답.
// 서버 routers/trading.py 와 동기. event kind 추가 시 양쪽 같이 갱신.
// 시작=cycle(주문 발주), 종가청산=close(당일매매 hold_days=0 — 주식 15:25·선물 15:40·
// 미장 close−5분), 종료=settlement(미체결 정리·잔고 reconcile, 15:50).
// preview 시장별 분리: krx_preview(07:30 — US 종가 반영), us_preview(18:15 — KRX 종가 반영).
export type TimelineEventKind =
  | "krx_cycle" | "krx_close_stock" | "krx_close_futures" | "krx_settlement" | "krx_preview"
  | "us_cycle"  | "us_close"        | "us_settlement"     | "us_preview";
// warning(N2) — 실행은 됐으나 체결 미확인 잔존(발주-but-미기록을 ✓로 가장하지 않음).
export type TimelineEventStatus = "done" | "warning" | "scheduled" | "missed" | "holiday";

export interface TimelineEvent {
  at: string;                 // ISO datetime (KST offset 포함)
  kind: TimelineEventKind;
  status: TimelineEventStatus;
  summary: string;            // 1줄 표시 (e.g. "1건 매수", "US 7건", "")
  detail: string;             // hover 시 자세한 설명 (e.g. missed 이유)
}

export interface TradingTimeline {
  now: string;
  heartbeat_at: string | null;
  heartbeat_status: "normal" | "warning" | "error";
  events: TimelineEvent[];
}

// ── 한국 종목 부가 데이터 (Company Analysis) — /market/kr/{symbol} ──
export interface KrInvestorPoint {
  date: string;
  inst: number;        // 기관 순매매 (주)
  foreign: number;     // 외국인 순매매 (주)
  indiv: number;       // 개인 순매매 (주, ≈ −(기관+외국인))
}
export interface KrReport {
  date: string;        // 작성일 (YY.MM.DD)
  title: string;       // 리포트 제목
  broker: string;      // 증권사
  url: string;         // 원문 (PDF 또는 네이버 리포트 상세)
  target: number | null;  // 해당 리포트 첫 페이지 목표주가 (원) — 없으면 null
}
export interface KrConsensus {
  broker: string;              // 제공 증권사
  date: string;                // 최종일자
  target: number | null;       // 목표주가 (원)
  prev_target: number | null;  // 직전 목표주가
  change_pct: number | null;   // 직전목표가 대비 변동률(%)
  opinion: string;             // 투자의견 (BUY/매수 등)
}
export interface KrEarnings {
  years: string[];                          // 연도 헤더 ['2024/12','2025/12','2026/12(E)',…]
  rows: Record<string, (number | null)[]>;  // 항목별 값 (매출액/영업이익/당기순이익/지배주주, 억원)
}
export interface KrDisclosure {
  date: string;        // 접수일 (YYYYMMDD)
  title: string;       // 보고서명
  submitter: string;   // 제출인
  url: string;         // DART 원문 링크
}
export interface KrShortPoint {
  date: string;                  // 일자
  bal_qty: number | null;        // 공매도 잔고수량 (주)
  bal_amt: number | null;        // 공매도 잔고금액 (원)
  bal_ratio: number | null;      // 잔고비중 (%)
}
export interface KrExtras {
  investor: KrInvestorPoint[];
  reports: KrReport[];
  consensus: KrConsensus[];
  earnings: KrEarnings;
  disclosures: KrDisclosure[];
  shorting: KrShortPoint[];
}

// ── 산업(섹터) 밸류체인 분석 — /market/industry/{name} ──
export interface IndustryCompany {
  gu: string;          // 밸류체인 구분 (Upstream/Midstream/Downstream)
  stage: string;       // 단계 (원자재/소재/셀/부품/장비/리사이클/애플리케이션)
  detail: string;      // 세부분류 (양극재/음극재/분리막/셀 …)
  name: string;        // 기업명
  ticker: string;      // 종목코드
  market: string;      // 시장 (KOSPI/KOSDAQ)
  product: string;     // 주요제품
  cap: number | null;        // 시가총액 (원)
  chg: number | null;        // 전일대비 등락률 (%)
  revenue: number | null;    // 매출액 (원)
  op: number | null;         // 영업이익 (원)
  op_margin: number | null;  // 영업이익률 (%)
  da: number | null;            // 현금흐름표 D&A(감가상각비+무형자산상각비, 원) — 서버 DART키 필요
  ebitda: number | null;        // EBITDA = 영업이익 + D&A (원)
  ebitda_margin: number | null; // EBITDA 이익률 (%)
  ms: number | null;         // 세부분류 내 시총 점유율 (%)
  ret: {                     // 기간 주가 수익률(%) — 5일/1개월/3개월/6개월/1년
    d5: number | null; d20: number | null; d60: number | null;
    d120: number | null; d240: number | null;
  } | null;
}
export interface IndustryData {
  industry: string;
  companies: IndustryCompany[];
  as_of: string | null;     // 시총 데이터 기준 거래일(yyyy-mm-dd)
  available: string[];
}

// ── 개별 기업 투자의견 게시판 — /opinions ──
export type OpinionStance = "buy" | "neutral" | "sell";
export interface OpinionComment {
  id: number; author: string; body: string;
  is_mine: boolean; created_at: string | null;
}
export interface StockOpinion {
  id: number; ticker: string; author: string;
  stance: OpinionStance;
  title: string;                  // 분석글 제목
  body: string;                   // 리치 HTML(서식·인라인 이미지)
  target_price: number | null;    // 목표주가(원). 상승여력은 현재가 대비 계산
  status: "pending" | "approved"; // 운영자 승인 상태
  likes: number; dislikes: number;
  my_vote: number;          // 1=좋아요 / -1=싫어요 / 0=없음
  is_mine: boolean;
  can_moderate: boolean;    // 요청자가 운영자(승인 권한)인지
  created_at: string | null;
  comments: OpinionComment[];
}
export interface OpinionList { ticker: string; is_admin: boolean; opinions: StockOpinion[]; }

// ── 섹터 키워드 뉴스 / 기업 개요 ──
export interface SectorNewsItem {
  title: string; summary: string; source: string; url: string; date: string;
}
export interface SectorNews { kr: SectorNewsItem[]; global: SectorNewsItem[]; }
export interface CompanyProfile { established: string; homepage: string; ceo: string; employees: string; business?: string; shares?: number | null; }

// ── 재무제표(Financials) — /market/financials ──
export interface FinRow {
  account: string;          // 전자공시 원본 계정명(표시용)
  canon?: string;           // 표준명(차트·지표 매칭용) — 원본명이 변형이어도 인식
  bold: boolean;            // 섹션 헤더(굵게)
  parent: boolean;          // 펼침 가능한 부모 계정
  child: boolean;           // 기본 숨김 상세(부모 펼칠 때 표시)
  group: number | null;     // 부모↔자식 묶음 id
  values: (number | null)[];   // 기간별 값(억원)
  change: (number | null)[];   // 기간별 증감률(YoY/QoQ %, change[0]=null)
  pct?: boolean;            // 비율행(영업이익률 등) — % 포맷·기울임
  derived?: boolean;        // 파생행(이익률·EBITDA) — 들여쓰기·muted
}
export interface FinStatement { periods: string[]; rows: FinRow[]; }
export interface FinancialsData {
  fetched: string;
  annual: { PL?: FinStatement; BS?: FinStatement; CF?: FinStatement };
  quarterly: { PL?: FinStatement; BS?: FinStatement; CF?: FinStatement };
}

// ── 전략 연구소 챗봇 (P0b) ───────────────────────────────────────────────────
export type ChatPart =
  | { type: "text"; text: string }
  | { type: "tool_use"; id: string; name: string; input: Record<string, unknown> }
  | { type: "tool_result"; tool_use_id: string; name: string; result: Record<string, unknown> };
export type ChatMessage = { role: "user" | "assistant"; parts: ChatPart[] };
