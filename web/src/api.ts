import type {
  AnalysisResult, BacktestResult, BacktestRunDetail, BacktestRunSummary,
  CommandRow, CommandType, DeviceRow, MarketContext, NextDayPreview, PortfolioRisk,
  ScreenerField, ScreenerMatch, ScreenerPreset, ScreenerSpecIO, ScreenerUserPreset,
  StrategyDef, StrategyRow, StrategyStats, StrategyVersionRow,
  SymbolInfo, SyncSnapshot, TradingTimeline, UserSettingsIO,
} from "./types";

const BASE = import.meta.env.VITE_API_URL ?? "http://localhost:8000";
const TOKEN_KEY = "qp_token";

export const tokenStore = {
  get: () => localStorage.getItem(TOKEN_KEY),
  set: (t: string) => localStorage.setItem(TOKEN_KEY, t),
  clear: () => localStorage.removeItem(TOKEN_KEY),
};

async function req<T>(path: string, opts: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(opts.headers as Record<string, string>),
  };
  const t = tokenStore.get();
  if (t) headers["Authorization"] = `Bearer ${t}`;

  const res = await fetch(BASE + path, { ...opts, headers });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `${res.status} ${res.statusText}`);
  }
  if (res.status === 204) return null as T;
  return res.json();
}

export const api = {
  signup: (email: string, password: string) =>
    req<{ access_token: string }>("/auth/signup", {
      method: "POST", body: JSON.stringify({ email, password }),
    }),
  login: (email: string, password: string) =>
    req<{ access_token: string }>("/auth/login", {
      method: "POST", body: JSON.stringify({ email, password }),
    }),
  googleLogin: (credential: string) =>
    req<{ access_token: string }>("/auth/google", {
      method: "POST", body: JSON.stringify({ credential }),
    }),
  me: () => req<{ id: number; email: string; created_at: string }>("/auth/me"),

  symbols: () => req<{ symbols: SymbolInfo[]; has_master: boolean }>("/symbols"),

  listStrategies: () => req<StrategyRow[]>("/strategies"),
  getStrategy: (id: number) => req<StrategyRow>(`/strategies/${id}`),
  createStrategy: (definition: StrategyDef, run_mode: string) =>
    req<StrategyRow>("/strategies", {
      method: "POST", body: JSON.stringify({ definition, run_mode }),
    }),
  updateStrategy: (id: number, definition: StrategyDef, run_mode: string) =>
    req<StrategyRow>(`/strategies/${id}`, {
      method: "PUT", body: JSON.stringify({ definition, run_mode }),
    }),
  deleteStrategy: (id: number) =>
    req<{ ok: boolean }>(`/strategies/${id}`, { method: "DELETE" }),

  // Phase 59 — 버전·현황·백테스트 내역
  listStrategyVersions: (id: number) =>
    req<StrategyVersionRow[]>(`/strategies/${id}/versions`),
  getStrategyVersion: (id: number, versionNo: number) =>
    req<StrategyVersionRow>(`/strategies/${id}/versions/${versionNo}`),
  restoreStrategyVersion: (id: number, versionNo: number) =>
    req<StrategyRow>(`/strategies/${id}/restore`, {
      method: "POST", body: JSON.stringify({ version_no: versionNo }),
    }),
  getStrategyStats: (id: number) =>
    req<StrategyStats>(`/strategies/${id}/stats`),
  listStrategyBacktests: (id: number) =>
    req<BacktestRunSummary[]>(`/strategies/${id}/backtests`),

  runBacktest: (strategy: StrategyDef, initial_capital: number,
                start?: string, end?: string,
                strategy_id?: number, version_no?: number) =>
    req<BacktestResult>("/backtest/run", {
      method: "POST",
      body: JSON.stringify({
        strategy, initial_capital, start, end, strategy_id, version_no,
      }),
    }),
  runAnalysis: (body: {
    conditions: unknown[]; logic: string; target_symbol: string;
    target_indicator: string; forward_days: number; lookback_years?: number | null;
  }) => req<AnalysisResult>("/analysis/run", {
    method: "POST", body: JSON.stringify(body),
  }),

  listBacktestRuns: () => req<BacktestRunSummary[]>("/backtest/runs"),
  getBacktestRun: (id: number) => req<BacktestRunDetail>(`/backtest/runs/${id}`),
  deleteBacktestRun: (id: number) =>
    req<{ ok: boolean }>(`/backtest/runs/${id}`, { method: "DELETE" }),

  devices: () => req<DeviceRow[]>("/auth/devices"),
  revokeDevice: (id: number) =>
    req<{ ok: boolean }>(`/auth/devices/${id}`, { method: "DELETE" }),
  approveDevice: (user_code: string) =>
    req<{ ok: boolean; device_name: string }>("/auth/device/approve", {
      method: "POST", body: JSON.stringify({ user_code }),
    }),

  snapshot: () => req<SyncSnapshot | null>("/sync/snapshot"),

  // 명령 버스 — 웹에서 발행, 로컬앱이 SSE로 수신·실행
  listCommands: (deviceId?: number, onlyPending = false) => {
    const q = new URLSearchParams();
    if (deviceId !== undefined) q.set("device_id", String(deviceId));
    if (onlyPending) q.set("only_pending", "true");
    return req<CommandRow[]>(`/sync/commands?${q.toString()}`);
  },
  createCommand: (deviceId: number, type: CommandType,
                   params: Record<string, string | number> = {}) =>
    req<CommandRow>("/sync/commands", {
      method: "POST",
      body: JSON.stringify({ device_id: deviceId, type, params }),
    }),

  // Phase 13 — Monitor 고도화
  marketContext: () => req<MarketContext>("/market/context"),
  portfolioRisk: (window = 60) =>
    req<PortfolioRisk>(`/portfolio/risk?window=${window}`),
  getSettings: () => req<UserSettingsIO>("/settings"),
  putSettings: (s: UserSettingsIO) =>
    req<UserSettingsIO>("/settings", { method: "PUT", body: JSON.stringify(s) }),

  // Phase 17~ — 종목 자동 선택 (스크리너)
  listScreenerPresets: () =>
    req<{ presets: ScreenerPreset[]; as_of: string | null }>("/screener/presets"),
  runScreenerPreset: (key: string) =>
    req<{ preset: string; count: number; matches: ScreenerMatch[]; as_of: string | null }>(
      `/screener/preset/${key}/run`, { method: "POST" }),
  screenerFields: () =>
    req<{ fields: ScreenerField[] }>("/screener/fields"),
  runScreenerCustom: (spec: ScreenerSpecIO) =>
    req<{ count: number; matches: ScreenerMatch[]; as_of: string | null }>(
      "/screener/run", { method: "POST", body: JSON.stringify(spec) }),

  // 내 세트 (계정 저장 사용자 정의 세트) CRUD
  listMyScreenerPresets: () =>
    req<{ presets: ScreenerUserPreset[] }>("/screener/my-presets"),
  createMyScreenerPreset: (name: string, spec: ScreenerSpecIO) =>
    req<ScreenerUserPreset>("/screener/my-presets", {
      method: "POST", body: JSON.stringify({ name, spec }),
    }),
  updateMyScreenerPreset: (id: number, name: string, spec: ScreenerSpecIO) =>
    req<ScreenerUserPreset>(`/screener/my-presets/${id}`, {
      method: "PUT", body: JSON.stringify({ name, spec }),
    }),
  deleteMyScreenerPreset: (id: number) =>
    req<{ ok: boolean }>(`/screener/my-presets/${id}`, { method: "DELETE" }),

  // Phase 31 — 내일 매매 미리보기
  getNextDayPreview: () => req<NextDayPreview>("/preview/next-day"),
  regenerateNextDayPreview: () =>
    req<NextDayPreview>("/preview/regenerate", { method: "POST" }),

  // 자동매매 타임라인 — [now-24h, now+24h] 이벤트 + heartbeat 상태
  getTradingTimeline: () => req<TradingTimeline>("/trading/timeline"),
};

// 로컬앱 다운로드 — 플랫폼별 zip URL 조회.
//
// 매 release마다 asset 이름이 버전·플랫폼 포함으로 바뀌므로
// (QuantPlatformLocal-v{ver}-{platform}.zip) 정적 URL 불가. GitHub releases API로
// 최신 release의 asset 목록을 받아 plat suffix 매칭으로 선택. 실패 시 release
// 페이지 URL fallback.
//
// 컨벤션 (v0.9.0-beta+):
//   - Windows: '...-windows.zip'
//   - macOS arm64: '...-macos-arm64.zip'
// 하위 호환 (v0.8.x): suffix 없는 단일 zip은 windows 가정.
const RELEASES_API =
  "https://api.github.com/repos/MercKR/quantman-releases/releases/latest";
const RELEASES_PAGE =
  "https://github.com/MercKR/quantman-releases/releases/latest";

export type LocalAppDownloads = {
  /** Windows zip URL. null이면 release에 windows asset 없음 (publish 전 등). */
  windows: string | null;
  /** macOS Apple Silicon zip URL. null이면 release에 mac asset 없음. */
  macos: string | null;
  /** 둘 다 못 받으면 사용자가 직접 release 페이지 가서 선택. */
  fallback: string;
  /** release tag (예: 'v0.9.0-beta'). 표시·debug용 + release notes 링크 생성. */
  tag: string | null;
};

// 옛 단일-URL 헬퍼 (fetchLocalAppDownloadUrl)는 fetchLocalAppDownloads로 대체됨.
// 호출자 0개라 제거. mac 지원 이전 v0.8.x 사용 흐름.

export async function fetchLocalAppDownloads(): Promise<LocalAppDownloads> {
  // 개발 환경 override — Windows 단일 URL만 (mac dev override는 안 씀).
  if (import.meta.env.VITE_LOCAL_APP_URL) {
    return {
      windows: import.meta.env.VITE_LOCAL_APP_URL as string,
      macos: null,
      fallback: RELEASES_PAGE,
      tag: null,
    };
  }
  try {
    const r = await fetch(RELEASES_API);
    if (!r.ok) {
      return { windows: null, macos: null, fallback: RELEASES_PAGE, tag: null };
    }
    const data = await r.json();
    const tag = (data?.tag_name ?? null) as string | null;
    const assets = (data?.assets ?? []) as { name?: string; browser_download_url?: string }[];
    const zips = assets.filter(a => (a.name ?? "").toLowerCase().endsWith(".zip"));

    const macAsset = zips.find(a =>
      (a.name ?? "").toLowerCase().includes("-macos"));
    const winAsset = zips.find(a =>
      (a.name ?? "").toLowerCase().includes("-windows"));

    // 하위 호환 — v0.8.x release는 suffix 없는 단일 zip. mac도 아니면 windows 가정.
    const winFallback = !winAsset
      ? zips.find(a => !((a.name ?? "").toLowerCase().includes("-macos")))
      : undefined;

    return {
      windows: winAsset?.browser_download_url ?? winFallback?.browser_download_url ?? null,
      macos: macAsset?.browser_download_url ?? null,
      fallback: RELEASES_PAGE,
      tag,
    };
  } catch {
    return { windows: null, macos: null, fallback: RELEASES_PAGE, tag: null };
  }
}

/** 사용자 OS 감지 — 다운로드 페이지에서 자기 OS용 버튼을 primary로 표시. */
export function detectOS(): "mac" | "windows" | "other" {
  if (typeof navigator === "undefined") return "other";
  const ua = navigator.userAgent;
  if (/Macintosh|Mac OS X/.test(ua)) return "mac";
  if (/Windows/.test(ua)) return "windows";
  return "other";
}

// ─── Oil Futures (WTI) 분석 ──────────────────────────────────────────
// quant_core.oil_futures 백엔드(/oil-futures/*) 호출 + 응답 타입.

export type OilSide = "short" | "long";

export interface OilDataInfo {
  n_rows: number;
  start_date: string;
  end_date: string;
  price_min: number;
  price_max: number;
}

export interface OilLatestPrice {
  price: number;
  change: number | null;
  change_pct: number | null;   // 소수 (예: -0.0173)
  source: string;              // "investing.com" | "yfinance" | "csv"
  delayed: boolean;
  fetched_at: string;
}

export interface OilPricePoint {
  date: string;
  close: number;
  high: number;
  low: number;
}

export interface OilGridCell {
  side: OilSide;
  threshold: number;
  horizon: number;
  n_trades: number;
  win_rate: number;
  avg_return: number;
  sharpe: number;
  mdd_usd: number;
  gross_profit_usd: number;   // 이긴 거래 합 (양수)
  gross_loss_usd: number;     // 진 거래 합 (음수)
  net_pnl_usd: number;
  profit_factor: number | null;   // null = 손실 0건 (∞)
  low_sample: boolean;
}

export interface OilSignal {
  date: string;
  side: OilSide;
  threshold: number;
  entry_ref_close: number;
}

export interface OilSummary {
  n_trades: number;
  win_rate: number;
  avg_return: number;
  avg_win: number;
  avg_loss: number;
  profit_factor: number | null;
  sharpe: number;
  mdd_usd: number;
  gross_profit_usd: number;
  gross_loss_usd: number;
  net_pnl_usd: number;
  // 🅐 MAE/MFE
  worst_mae_usd: number;
  avg_mae_usd: number;
  avg_mfe_usd: number;
  // 🅑 streak
  max_win_streak: number;
  max_loss_streak: number;
  // 선물 만기 롤오버
  total_rollovers: number;
  total_roll_cost_usd: number;
  low_sample: boolean;
}

export interface OilTrade {
  signal_date: string;
  side: OilSide;
  threshold: number;
  entry_date: string;
  entry_price: number;
  exit_date: string;
  exit_price: number;
  horizon_days: number;
  return_pct: number;
  net_pnl_usd: number;
  mae_usd: number;   // 🅐 보유 중 최악 평가손실 (음수)
  mfe_usd: number;   // 🅐 보유 중 최고 평가이익 (양수)
  exit_reason: "horizon" | "stop_loss" | "take_profit";
  num_rollovers: number;     // 보유 중 만기 통과(강제 롤오버) 횟수
  roll_cost_usd: number;     // 롤 비용 (음수 또는 0)
}

// 🅒 Seasonality
export interface OilSeasonCell {
  key: number;
  name: string;
  n_days: number;
  avg_return: number;
  win_rate: number;
}

export interface OilSeasonality {
  monthly: OilSeasonCell[];
  weekday: OilSeasonCell[];
}

export interface OilEquityPoint {
  date: string;
  cumulative_usd: number;
}

export interface OilBacktest {
  summary: OilSummary;
  trades: OilTrade[];
  equity_curve: OilEquityPoint[];                  // realized
  portfolio_equity_curve: OilEquityPoint[];        // 🅓 시가평가 (mark-to-market)
  portfolio_mdd_usd: number;                       // 🅓 시가평가 MDD
}

// 🅔 Macro context (VIX, DXY)
export interface OilMacroRegimeCell {
  bucket: string;
  n_days: number;
  wti_avg_return: number;
  wti_win_rate: number;
}
export interface OilMacroCorrelation {
  pair: string;
  pearson: number;
}
export interface OilMacroContext {
  available: boolean;
  coverage_days: number;
  correlations: OilMacroCorrelation[];
  vix_regime: OilMacroRegimeCell[];
  dxy_regime: OilMacroRegimeCell[];
}

export interface OilWalkForward {
  train_start: string;
  train_end: string;
  test_start: string;
  test_end: string;
  best_in_sample: {
    side: OilSide;
    threshold: number;
    horizon: number;
    summary: OilSummary;
  };
  out_of_sample: OilSummary;
}

export const oilApi = {
  dataInfo: () => req<OilDataInfo>("/oil-futures/data-info"),
  latestPrice: () => req<OilLatestPrice>("/oil-futures/latest-price"),
  prices: (start?: string, end?: string) => {
    const qs = new URLSearchParams();
    if (start) qs.set("start", start);
    if (end) qs.set("end", end);
    const q = qs.toString();
    return req<OilPricePoint[]>("/oil-futures/prices" + (q ? "?" + q : ""));
  },
  grid: (opts: {
    shorts?: number[];
    longs?: number[];
    horizons?: number[];
    commission?: number;
    slippage_ticks?: number;
  } = {}) => {
    const qs = new URLSearchParams();
    if (opts.shorts?.length) qs.set("shorts", opts.shorts.join(","));
    if (opts.longs?.length) qs.set("longs", opts.longs.join(","));
    if (opts.horizons?.length) qs.set("horizons", opts.horizons.join(","));
    if (opts.commission !== undefined) qs.set("commission", String(opts.commission));
    if (opts.slippage_ticks !== undefined)
      qs.set("slippage_ticks", String(opts.slippage_ticks));
    const q = qs.toString();
    return req<OilGridCell[]>("/oil-futures/grid" + (q ? "?" + q : ""));
  },
  signals: (type: OilSide, threshold: number, since?: string) => {
    const qs = new URLSearchParams({ type, threshold: String(threshold) });
    if (since) qs.set("since", since);
    return req<OilSignal[]>("/oil-futures/signals?" + qs.toString());
  },
  backtest: (body: {
    side: OilSide;
    threshold: number;
    horizon_days: number;
    commission?: number;
    slippage_ticks?: number;
    stop_loss_pct?: number | null;       // 🅒 SL/TP 시뮬레이터
    take_profit_pct?: number | null;
    roll_cost_pct?: number;              // 선물 만기 롤오버 비용 (%/회, 소수)
  }) =>
    req<OilBacktest>("/oil-futures/backtest", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  walkforward: (body: {
    shorts?: number[];
    longs?: number[];
    horizons?: number[];
    split_date: string;
    commission?: number;
    slippage_ticks?: number;
  }) =>
    req<OilWalkForward>("/oil-futures/walkforward", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  seasonality: () => req<OilSeasonality>("/oil-futures/seasonality"),
  macroContext: () => req<OilMacroContext>("/oil-futures/macro-context"),
};
