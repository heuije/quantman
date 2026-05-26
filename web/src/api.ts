import type {
  AnalysisResult, BacktestResult, BacktestRunDetail, BacktestRunSummary,
  CommandRow, CommandType, DeviceRow, MarketContext, NextDayPreview, PortfolioRisk,
  ScreenerField, ScreenerMatch, ScreenerPreset, ScreenerSpecIO, ScreenerUserPreset,
  StrategyDef, StrategyRow, StrategyStats, StrategyVersionRow,
  SymbolInfo, SyncSnapshot, UserSettingsIO,
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
};

// 로컬앱 다운로드 URL 조회.
//
// 매 release마다 asset 이름이 버전 포함(QuantPlatformLocal-v{ver}.zip)으로 바뀌므로
// `/releases/latest/download/<고정파일명>` 식 URL은 사용 불가. GitHub releases API로
// 최신 release의 zip asset URL을 동적 획득한다. 실패 시 release 페이지 URL fallback.
const RELEASES_API =
  "https://api.github.com/repos/MercKR/quantman-releases/releases/latest";
const RELEASES_PAGE =
  "https://github.com/MercKR/quantman-releases/releases/latest";

export async function fetchLocalAppDownloadUrl(): Promise<string> {
  if (import.meta.env.VITE_LOCAL_APP_URL) {
    return import.meta.env.VITE_LOCAL_APP_URL as string;
  }
  try {
    const r = await fetch(RELEASES_API);
    if (!r.ok) return RELEASES_PAGE;
    const data = await r.json();
    const assets = (data?.assets ?? []) as { name?: string; browser_download_url?: string }[];
    const zip = assets.find(a => (a.name ?? "").toLowerCase().endsWith(".zip"));
    return zip?.browser_download_url ?? RELEASES_PAGE;
  } catch {
    return RELEASES_PAGE;
  }
}
