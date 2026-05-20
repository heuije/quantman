import type {
  AnalysisResult, BacktestResult, BacktestRunDetail, BacktestRunSummary,
  CommandRow, CommandType, DeviceRow, MarketContext, PortfolioRisk,
  ScreenerMatch, ScreenerPreset,
  StrategyDef, StrategyRow, SymbolInfo, SyncSnapshot, UserSettingsIO,
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

  runBacktest: (strategy: StrategyDef, initial_capital: number,
                start?: string, end?: string) =>
    req<BacktestResult>("/backtest/run", {
      method: "POST",
      body: JSON.stringify({ strategy, initial_capital, start, end }),
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

  // Phase 17~ — 종목 자동선정 (스크리너)
  listScreenerPresets: () =>
    req<{ presets: ScreenerPreset[] }>("/screener/presets"),
  runScreenerPreset: (key: string) =>
    req<{ preset: string; count: number; matches: ScreenerMatch[] }>(
      `/screener/preset/${key}/run`, { method: "POST" }),
};
