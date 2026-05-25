import { useEffect, useState } from "react";
import { api } from "../api";
import ConditionBuilder, { starterCondition, HELD_DAYS_KEY } from "../components/ConditionBuilder";
import SymbolPicker from "../components/SymbolPicker";
import MultiSymbolPicker from "../components/MultiSymbolPicker";
import EquityChart from "../components/EquityChart";
import Verdict from "../components/Verdict";
import { fmt2, wonReadable } from "../format";
import type {
  AnalysisResult, BacktestResult, BacktestRunSummary, ConditionGroup, ConditionNode,
  ExecutionPolicy, RebalanceIO, ScreenerPreset, ScreenerSpecIO,
  StrategyDef, SymbolInfo,
} from "../types";
import { EXECUTION_DEFAULTS, parseScreenerKey } from "../types";

type SizingMode = "fixed_amount" | "pct_cash" | "equal_weight" | "atr_risk";

/** 청산 규칙 정의 — 켜진 규칙 중 먼저 트리거되는 것으로 청산.
 *  Phase 56 — hold(보유기간)는 별도 inline input으로 분리, conditions 영역에 통합. */
type RuleKey = "tp" | "sl" | "trail" | "atr";
// Phase 38.1 — 평가 시점을 명시적으로 분리해서 UI에 노출.
// "realtime" = 장중 WebSocket tick으로 즉시 발동 (intraday_loop).
// "eod"      = 매일 08:55 사이클에서 EOD 데이터로 평가.
const RULE_DEFS: {
  key: RuleKey; name: string; suffix: string;
}[] = [
  { key: "tp",    name: "익절",          suffix: "% 이상 수익 시" },
  { key: "sl",    name: "손절",          suffix: "% 이하 수익 시 (음수 입력)" },
  { key: "trail", name: "트레일링 스톱",  suffix: "% 하락 시 (진입 후 고점 대비)" },
  { key: "atr",   name: "ATR 트레일링",   suffix: "× ATR 만큼 고점에서 하락 시" },
];

type TabKey = "build" | "result" | "market";

// Phase 56 — 사이드바 페이지 이동·새로고침 후 작업 보존. 모든 빌더 state localStorage persist.
// API 응답·UI transient state는 제외(symbols·analysis·backtest·busy·err·history).
const DRAFT_KEY = "backtest_draft_v1";

function loadDraft<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(DRAFT_KEY);
    if (!raw) return fallback;
    const d = JSON.parse(raw);
    return key in d ? d[key] : fallback;
  } catch { return fallback; }
}

export default function Backtest() {
  const [tab, setTab] = useState<TabKey>("build");

  const [symbols, setSymbols] = useState<SymbolInfo[]>([]);
  const [hasMaster, setHasMaster] = useState<boolean>(true);
  const [name, setName] = useState(() => loadDraft("name", "새 전략"));
  const [tradeSymbol, setTradeSymbol] = useState(() => loadDraft("tradeSymbol", ""));
  const [buy, setBuy] = useState<ConditionGroup>(() =>
    loadDraft("buy", { conditions: [], logic: "AND" }));
  const [sell, setSell] = useState<ConditionGroup>(() =>
    loadDraft("sell", { conditions: [], logic: "AND" }));
  const [exits, setExits] = useState<Record<RuleKey, { on: boolean; v: number; sell_pct: number }>>(() =>
    loadDraft("exits", {
      tp:    { on: false, v: 10, sell_pct: 100 },
      sl:    { on: false, v: -5, sell_pct: 100 },
      trail: { on: false, v: 8,  sell_pct: 100 },
      atr:   { on: false, v: 3,  sell_pct: 100 },
    }));
  const [sellRealtimeEnabled, setSellRealtimeEnabled] = useState(() => loadDraft("sellRealtimeEnabled", false));
  const [sellEodEnabled, setSellEodEnabled] = useState(() => loadDraft("sellEodEnabled", false));
  const [buyAmountPct, setBuyAmountPct] = useState(() => loadDraft("buyAmountPct", 10));
  const [sellAmountPct, setSellAmountPct] = useState(() => loadDraft("sellAmountPct", 100));
  const [screenerLimit, setScreenerLimit] = useState(() => loadDraft("screenerLimit", 5));
  const [screenerSpec, setScreenerSpec] = useState<ScreenerSpecIO | null>(() => loadDraft("screenerSpec", null));
  const [rebalance, setRebalance] = useState<RebalanceIO>(() =>
    loadDraft("rebalance", { mode: "hold", period: "weekly" }));
  const [sizingMode, setSizingMode] = useState<SizingMode>(() => loadDraft("sizingMode", EXECUTION_DEFAULTS.sizing_mode));
  const [amountKrw, setAmountKrw] = useState(() => loadDraft("amountKrw", EXECUTION_DEFAULTS.amount_krw));
  const [atrRiskPct, setAtrRiskPct] = useState(() => loadDraft("atrRiskPct", EXECUTION_DEFAULTS.atr_risk_pct));
  const [atrMult, setAtrMult] = useState(() => loadDraft("atrMult", EXECUTION_DEFAULTS.atr_mult));
  const [maxPositionPct, setMaxPositionPct] = useState(() => loadDraft("maxPositionPct", EXECUTION_DEFAULTS.max_position_pct));
  const [dailyLossLimitPct, setDailyLossLimitPct] = useState(() => loadDraft("dailyLossLimitPct", EXECUTION_DEFAULTS.daily_loss_limit_pct));
  const [maxDrawdownPct, setMaxDrawdownPct] = useState(() => loadDraft("maxDrawdownPct", EXECUTION_DEFAULTS.max_drawdown_pct));
  const [useLimit, setUseLimit] = useState<boolean>(() => loadDraft("useLimit", EXECUTION_DEFAULTS.use_limit));
  const [buyTolerancePct, setBuyTolerancePct] = useState(() => loadDraft("buyTolerancePct", EXECUTION_DEFAULTS.buy_tolerance_pct));
  const [sellTolerancePct, setSellTolerancePct] = useState(() => loadDraft("sellTolerancePct", EXECUTION_DEFAULTS.sell_tolerance_pct));
  const [btCommissionBps, setBtCommissionBps] = useState(() => loadDraft("btCommissionBps", EXECUTION_DEFAULTS.bt_commission_bps));
  const [btSellTaxBps, setBtSellTaxBps] = useState(() => loadDraft("btSellTaxBps", EXECUTION_DEFAULTS.bt_sell_tax_bps));
  const [btSlippageBps, setBtSlippageBps] = useState(() => loadDraft("btSlippageBps", EXECUTION_DEFAULTS.bt_slippage_bps));
  const [btGapExtraCost, setBtGapExtraCost] = useState(() => loadDraft("btGapExtraCost", EXECUTION_DEFAULTS.bt_gap_extra_cost));
  const [btGapThresholdPct, setBtGapThresholdPct] = useState(() => loadDraft("btGapThresholdPct", EXECUTION_DEFAULTS.bt_gap_threshold_pct));
  const [capital, setCapital] = useState(() => loadDraft("capital", 10_000_000));
  const [forwardDays, setForwardDays] = useState(() => loadDraft("forwardDays", 1));

  const [analysis, setAnalysis] = useState<AnalysisResult | null>(null);
  const [backtest, setBacktest] = useState<BacktestResult | null>(null);
  const [busy, setBusy] = useState<"" | "analysis" | "backtest" | "draft" | "apply">("");
  const [err, setErr] = useState("");
  const [saveMsg, setSaveMsg] = useState("");

  const [history, setHistory] = useState<BacktestRunSummary[]>([]);
  const [historyLoaded, setHistoryLoaded] = useState(false);

  function setRule(key: RuleKey, patch: Partial<{ on: boolean; v: number; sell_pct: number }>) {
    setExits((e) => ({ ...e, [key]: { ...e[key], ...patch } }));
  }

  useEffect(() => {
    api.symbols().then((r) => {
      setSymbols(r.symbols);
      setHasMaster(r.has_master);
      // Phase 53 — 매수 대상 default 자동 설정 제거 (혼란).
      // Phase 56 — 매수 조건 1개 시각용 template 자동 추가 (localStorage에 복원된
      // buy가 없을 때만 — 복원된 사용자 작업을 덮어쓰지 않음).
      setBuy((cur) => cur.conditions.length > 0
        ? cur
        : { logic: "AND", conditions: [starterCondition(r.symbols)] });
    }).catch((e) => setErr((e as Error).message));
  }, []);

  // Phase 56 — 모든 빌더 state localStorage에 persist (사이드바 이동·새로고침 후 복원).
  // API 응답·UI transient state는 제외 (symbols/analysis/backtest/busy/err/history).
  useEffect(() => {
    const draft = {
      name, tradeSymbol, buy, sell, exits,
      sellRealtimeEnabled, sellEodEnabled,
      buyAmountPct, sellAmountPct, screenerLimit, screenerSpec, rebalance,
      sizingMode, amountKrw, atrRiskPct, atrMult,
      maxPositionPct, dailyLossLimitPct, maxDrawdownPct,
      useLimit, buyTolerancePct, sellTolerancePct,
      btCommissionBps, btSellTaxBps, btSlippageBps,
      btGapExtraCost, btGapThresholdPct,
      capital, forwardDays,
    };
    try { localStorage.setItem(DRAFT_KEY, JSON.stringify(draft)); }
    catch { /* quota 초과 등 — 단순 무시 */ }
  }, [name, tradeSymbol, buy, sell, exits,
      sellRealtimeEnabled, sellEodEnabled,
      buyAmountPct, sellAmountPct, screenerLimit, screenerSpec, rebalance,
      sizingMode, amountKrw, atrRiskPct, atrMult,
      maxPositionPct, dailyLossLimitPct, maxDrawdownPct,
      useLimit, buyTolerancePct, sellTolerancePct,
      btCommissionBps, btSellTaxBps, btSlippageBps,
      btGapExtraCost, btGapThresholdPct,
      capital, forwardDays]);

  function loadHistory() {
    api.listBacktestRuns()
      .then(setHistory)
      .catch((e) => setErr((e as Error).message))
      .finally(() => setHistoryLoaded(true));
  }

  useEffect(() => {
    // 결과·보관함 탭 진입 시 이력 1회 로드 (HistoryListPanel이 result 탭 안에 통합됨).
    if (tab === "result" && !historyLoaded) loadHistory();
  }, [tab]);   // eslint-disable-line react-hooks/exhaustive-deps

  function buildDef(): StrategyDef {
    const execution: ExecutionPolicy = {
      sizing_mode: sizingMode,
      amount_krw: amountKrw,
      atr_risk_pct: atrRiskPct,
      atr_mult: atrMult,
      max_position_pct: maxPositionPct,
      daily_loss_limit_pct: dailyLossLimitPct,
      max_drawdown_pct: maxDrawdownPct,
      use_limit: useLimit,
      buy_tolerance_pct: buyTolerancePct,
      sell_tolerance_pct: sellTolerancePct,
      // Phase 39 + C-01 — 백테스트 비용 가정
      bt_commission_bps: btCommissionBps,
      bt_sell_tax_bps: btSellTaxBps,
      bt_slippage_bps: btSlippageBps,
      bt_gap_extra_cost: btGapExtraCost,
      bt_gap_threshold_pct: btGapThresholdPct,
    };
    // Phase 56 — 룰별 sell_pct (ON 룰만). backend SellRules.rule_sell_pcts로 전달.
    const ruleSellPcts: Record<string, number> = {};
    for (const [k, v] of Object.entries(exits)) {
      if (v.on) ruleSellPcts[k] = v.sell_pct;
    }
    // Phase 56 — 매도 conditions의 "보유기간(_held_days)" 가상 indicator를 sell_rules.hold_days로 transcode.
    // backend 평가 엔진 변경 없이 사용자 멘탈 모델(조건식 통합) 충족. 매도 비율은 sell_amount_pct 그대로 적용.
    // 최상위 conditions만 처리 (sub-group 안 _held_days는 무시 — 보유기간은 top-level 단일 조건 권장).
    let holdDaysFromCond: number | null = null;
    const cleanedConditions = (sell.conditions || []).filter((node: ConditionNode) => {
      if ("left" in node && node.left?.indicator === HELD_DAYS_KEY) {
        const v = node.right && "value" in node.right ? Number(node.right.value) : NaN;
        if (Number.isFinite(v) && v > 0) holdDaysFromCond = Math.floor(v);
        return false;   // 이 조건은 conditions에서 제외 (hold_days로 transcode)
      }
      return true;
    });
    return {
      name, trade_symbol: tradeSymbol, buy,
      // Phase 32 — 매도/청산 통합. 익절/손절/트레일링/매도 조건이 한 객체.
      // Phase 56 — 보유기간은 ConditionBuilder의 "보유기간" indicator 조건으로 통합 (별도 row 제거).
      sell_rules: {
        take_profit:    exits.tp.on    ? exits.tp.v    : null,
        stop_loss:      exits.sl.on    ? exits.sl.v    : null,
        trail_pct:      exits.trail.on ? exits.trail.v : null,
        trail_atr_mult: exits.atr.on   ? exits.atr.v   : null,
        hold_days:      holdDaysFromCond,
        conditions:     cleanedConditions,
        logic:          sell.logic,
        sell_amount_pct: sellAmountPct,
        rule_sell_pcts: ruleSellPcts,
      },
      amount_pct: buyAmountPct,
      screener_limit: screenerLimit,
      // 자동 선택이 커스텀이면 spec 저장 (trade_symbol='screener:custom')
      screener_spec: tradeSymbol === "screener:custom" ? screenerSpec : null,
      // Phase 53 fix — backend Rebalance field는 non-Optional이므로 null 전송 시
      // pydantic validation 실패. 수동 모드면 undefined로 omit해 backend default 사용.
      rebalance: tradeSymbol.startsWith("screener:") ? rebalance : undefined,
      execution,
    };
  }

  function targetIndicator() {
    const inds = symbols.find((s) => s.symbol === tradeSymbol)?.indicators ?? [];
    return inds.find((i) => i.key.includes("pct_change"))?.key
      ?? inds[0]?.key ?? "";
  }

  /** 매도 정의가 비어 있지 않은가 — 매도 조건 conditions 또는 청산 규칙 중 적어도 하나. */
  function hasSellSetup(): string | null {
    const hasCond = sell.conditions.length > 0;
    const hasRule = Object.values(exits).some((r) => r.on);
    if (!hasCond && !hasRule) {
      return "매도 조건 중 적어도 하나는 설정해야 합니다 (익절·손절·트레일링·ATR 또는 추가 매도 조건).";
    }
    return null;
  }

  /** Phase 56 — 매수 조건 정의 검증.
   *  매수 조건 0개 → build_signal_mask가 빈 Series 반환 → 매수 0건. silent fail 차단. */
  function hasBuySetup(): string | null {
    if (buy.conditions.length === 0) {
      return "매수 조건을 1개 이상 설정하세요. (조건 없이는 매수 신호가 발생하지 않습니다)";
    }
    return null;
  }

  /** Phase 56 — 매수=매도 동일 조건 detection. 매수 즉시 매도 whipsaw. */
  function sameBuySellWarning(): string | null {
    if (buy.conditions.length === 0 || sell.conditions.length === 0) return null;
    if (JSON.stringify(buy.conditions) === JSON.stringify(sell.conditions)
        && buy.logic === sell.logic) {
      return "매수 조건과 매도 조건이 완전히 동일합니다. 매수 즉시 매도되어 거래가 무의미합니다. 계속할까요?";
    }
    return null;
  }

  async function runAnalysis() {
    setErr("");
    if (parseScreenerKey(tradeSymbol)) {
      setErr("자동 선택 전략은 통계 미리보기를 지원하지 않습니다. " +
              "수동 종목으로 분석하세요.");
      return;
    }
    const buyErr = hasBuySetup();
    if (buyErr) { setErr(buyErr); return; }
    setBusy("analysis"); setAnalysis(null);
    try {
      const r = await api.runAnalysis({
        conditions: buy.conditions, logic: buy.logic,
        target_symbol: tradeSymbol, target_indicator: targetIndicator(),
        forward_days: forwardDays,
      });
      setAnalysis(r);
    } catch (e) { setErr((e as Error).message); }
    finally { setBusy(""); }
  }

  async function runBacktest() {
    setErr(""); setSaveMsg("");
    // Phase 57-B — 자동선택(screener) 백테스트 unlock. backend가 rebalance 주기마다
    // historical screener 평가로 후보 풀을 갱신. 펀더멘털(market_cap·per 등) 사용 시
    // backend가 명시 에러 반환.
    const buyErr = hasBuySetup();
    if (buyErr) { setErr(buyErr); return; }
    const sellErr = hasSellSetup();
    if (sellErr) { setErr(sellErr); return; }
    // Phase 56 — 매수=매도 동일 조건 confirm
    const whipsawWarn = sameBuySellWarning();
    if (whipsawWarn && !window.confirm(whipsawWarn)) return;
    setBusy("backtest"); setBacktest(null);
    try {
      const r = await api.runBacktest(buildDef(), capital);
      setBacktest(r);
      setHistoryLoaded(false);     // 다음 history 탭 진입 시 새로고침
      setTab("result");            // 결과 탭으로 자동 이동
    } catch (e) { setErr((e as Error).message); }
    finally { setBusy(""); }
  }

  async function save(runMode: "draft" | "paper") {
    setErr(""); setSaveMsg("");
    const sellErr = hasSellSetup();
    if (sellErr) { setErr(sellErr); return; }
    setBusy(runMode === "draft" ? "draft" : "apply");
    try {
      await api.createStrategy(buildDef(), runMode);
      setSaveMsg(runMode === "draft"
        ? `'${name}' 전략을 임시저장했습니다.`
        : `'${name}' 전략을 내 전략에 적용했습니다 (모의투자).`);
    } catch (e) { setErr((e as Error).message); }
    finally { setBusy(""); }
  }

  async function loadRun(id: number) {
    setErr("");
    try {
      const r = await api.getBacktestRun(id);
      setBacktest(r.result);
      setName(r.definition.name);
      setTab("result");
    } catch (e) { setErr((e as Error).message); }
  }

  async function deleteRun(id: number) {
    if (!confirm("이 실행 내역을 삭제할까요?")) return;
    setErr("");
    try {
      await api.deleteBacktestRun(id);
      setHistory((rows) => rows.filter((r) => r.id !== id));
    } catch (e) { setErr((e as Error).message); }
  }

  const m = backtest?.metrics;

  return (
    <div>
      <h1 className="page-title">전략 만들기</h1>
      <p className="page-sub">
        조건을 문장으로 채우고 → 통계로 발견하고 → 과거 데이터로 검증하세요.
      </p>
      {/* Phase 48 — 자문 아님 명시 (가입 동의 외에 전략 빌더 진입 시에도 재고지) */}
      <div className="self-direction-banner">
        ℹ 본 도구는 <b>본인이 직접 입력한 조건·룰</b>만 실행하는 셀프서비스형 자동매매
        도구이며, 회사가 종목을 추천하거나 매매 판단을 일임받지 않습니다 (투자자문업·일임업 아님).
      </div>

      <div className="tabs">
        {([
          ["build",   "빌더"],
          ["result",  "결과 · 보관함"],
          ["market",  "마켓플레이스"],
        ] as [TabKey, string][]).map(([k, label]) => (
          <button key={k} type="button"
                  className={"tab" + (tab === k ? " active" : "")
                    + (k === "market" ? " soon" : "")}
                  onClick={() => setTab(k)}>
            {label}
            {k === "market" && <span className="soon-tag">V2</span>}
          </button>
        ))}
      </div>

      {err && <div className="error">{err}</div>}

      {tab === "build" && (
        <BuildTab
          symbols={symbols} hasMaster={hasMaster}
          name={name} setName={setName}
          tradeSymbol={tradeSymbol} setTradeSymbol={setTradeSymbol}
          buy={buy} setBuy={setBuy}
          sell={sell} setSell={setSell}
          exits={exits} setRule={setRule}
          sellRealtimeEnabled={sellRealtimeEnabled} setSellRealtimeEnabled={setSellRealtimeEnabled}
          sellEodEnabled={sellEodEnabled} setSellEodEnabled={setSellEodEnabled}
          buyAmountPct={buyAmountPct} setBuyAmountPct={setBuyAmountPct}
          sellAmountPct={sellAmountPct} setSellAmountPct={setSellAmountPct}
          screenerLimit={screenerLimit} setScreenerLimit={setScreenerLimit}
          screenerSpec={screenerSpec} setScreenerSpec={setScreenerSpec}
          rebalance={rebalance} setRebalance={setRebalance}
          sizingMode={sizingMode} setSizingMode={setSizingMode}
          amountKrw={amountKrw} setAmountKrw={setAmountKrw}
          atrRiskPct={atrRiskPct} setAtrRiskPct={setAtrRiskPct}
          atrMult={atrMult} setAtrMult={setAtrMult}
          maxPositionPct={maxPositionPct} setMaxPositionPct={setMaxPositionPct}
          dailyLossLimitPct={dailyLossLimitPct} setDailyLossLimitPct={setDailyLossLimitPct}
          maxDrawdownPct={maxDrawdownPct} setMaxDrawdownPct={setMaxDrawdownPct}
          useLimit={useLimit} setUseLimit={setUseLimit}
          buyTolerancePct={buyTolerancePct} setBuyTolerancePct={setBuyTolerancePct}
          sellTolerancePct={sellTolerancePct} setSellTolerancePct={setSellTolerancePct}
          btCommissionBps={btCommissionBps} setBtCommissionBps={setBtCommissionBps}
          btSellTaxBps={btSellTaxBps} setBtSellTaxBps={setBtSellTaxBps}
          btSlippageBps={btSlippageBps} setBtSlippageBps={setBtSlippageBps}
          btGapExtraCost={btGapExtraCost} setBtGapExtraCost={setBtGapExtraCost}
          btGapThresholdPct={btGapThresholdPct} setBtGapThresholdPct={setBtGapThresholdPct}
          capital={capital} setCapital={setCapital}
          forwardDays={forwardDays} setForwardDays={setForwardDays}
          busy={busy} runAnalysis={runAnalysis} runBacktest={runBacktest}
          analysis={analysis}
        />
      )}

      {tab === "result" && (
        <ResultTab
          backtest={backtest}
          metrics={m}
          name={name}
          busy={busy}
          onDraft={() => save("draft")}
          onApply={() => save("paper")}
          saveMsg={saveMsg}
          history={history}
          historyLoaded={historyLoaded}
          onLoad={loadRun}
          onDelete={deleteRun}
        />
      )}
      {tab === "market" && <MarketplacePlaceholder />}
    </div>
  );
}

// ── 탭 1: 전략 구성 ───────────────────────────────────────────────────────────

function BuildTab(props: {
  symbols: SymbolInfo[]; hasMaster: boolean;
  name: string; setName: (v: string) => void;
  tradeSymbol: string; setTradeSymbol: (v: string) => void;
  buy: ConditionGroup; setBuy: (v: ConditionGroup) => void;
  sell: ConditionGroup; setSell: (v: ConditionGroup) => void;
  exits: Record<RuleKey, { on: boolean; v: number; sell_pct: number }>;
  setRule: (k: RuleKey, p: Partial<{ on: boolean; v: number; sell_pct: number }>) => void;
  sellRealtimeEnabled: boolean; setSellRealtimeEnabled: (v: boolean) => void;
  sellEodEnabled: boolean; setSellEodEnabled: (v: boolean) => void;
  buyAmountPct: number; setBuyAmountPct: (v: number) => void;
  sellAmountPct: number; setSellAmountPct: (v: number) => void;
  screenerLimit: number; setScreenerLimit: (v: number) => void;
  screenerSpec: ScreenerSpecIO | null; setScreenerSpec: (s: ScreenerSpecIO | null) => void;
  rebalance: RebalanceIO; setRebalance: (r: RebalanceIO) => void;
  sizingMode: SizingMode; setSizingMode: (v: SizingMode) => void;
  amountKrw: number; setAmountKrw: (v: number) => void;
  atrRiskPct: number; setAtrRiskPct: (v: number) => void;
  atrMult: number; setAtrMult: (v: number) => void;
  maxPositionPct: number; setMaxPositionPct: (v: number) => void;
  dailyLossLimitPct: number; setDailyLossLimitPct: (v: number) => void;
  maxDrawdownPct: number; setMaxDrawdownPct: (v: number) => void;
  useLimit: boolean; setUseLimit: (v: boolean) => void;
  buyTolerancePct: number; setBuyTolerancePct: (v: number) => void;
  sellTolerancePct: number; setSellTolerancePct: (v: number) => void;
  btCommissionBps: number; setBtCommissionBps: (v: number) => void;
  btSellTaxBps: number; setBtSellTaxBps: (v: number) => void;
  btSlippageBps: number; setBtSlippageBps: (v: number) => void;
  btGapExtraCost: boolean; setBtGapExtraCost: (v: boolean) => void;
  btGapThresholdPct: number; setBtGapThresholdPct: (v: number) => void;
  capital: number; setCapital: (v: number) => void;
  forwardDays: number; setForwardDays: (v: number) => void;
  busy: string;
  runAnalysis: () => void;
  runBacktest: () => void;
  analysis: AnalysisResult | null;
}) {
  const {
    symbols, hasMaster, name, setName, tradeSymbol, setTradeSymbol,
    buy, setBuy, sell, setSell, exits, setRule,
    sellRealtimeEnabled, setSellRealtimeEnabled,
    sellEodEnabled, setSellEodEnabled,
    buyAmountPct, setBuyAmountPct, sellAmountPct, setSellAmountPct,
    screenerLimit, setScreenerLimit,
    screenerSpec, setScreenerSpec, rebalance, setRebalance,
    sizingMode, setSizingMode,
    amountKrw, setAmountKrw,
    atrRiskPct, setAtrRiskPct, atrMult, setAtrMult,
    maxPositionPct, setMaxPositionPct,
    dailyLossLimitPct, setDailyLossLimitPct,
    maxDrawdownPct, setMaxDrawdownPct,
    useLimit, setUseLimit,
    buyTolerancePct, setBuyTolerancePct,
    sellTolerancePct, setSellTolerancePct,
    btCommissionBps, setBtCommissionBps,
    btSellTaxBps, setBtSellTaxBps,
    btSlippageBps, setBtSlippageBps,
    btGapExtraCost, setBtGapExtraCost,
    btGapThresholdPct, setBtGapThresholdPct,
    capital, setCapital, forwardDays, setForwardDays,
    busy, runAnalysis, runBacktest, analysis,
  } = props;

  // Phase 56 — Progressive disclosure. 이전 섹션이 채워져야 다음 섹션 노출.
  // 외부 단계: 매수후보(1) → 매수조건(2 panel) → 매도조건(3) → 리스크·자금·실행(4·5·6)
  // 매수조건 panel 내부 단계: ①조건 → ②가격 → ③규모 → ④요약
  // ②③은 default 있지만 user touch flag로 명시적 step 진행.
  // (rules of hooks: useState는 early return 이전에 호출해야 함)
  // localStorage 복원된 strategy(tradeSymbol·buy 채워진 상태)면 mount 시 자동 true —
  // 사이드바 이동·새로고침 후 돌아오면 progressive disclosure 다시 시작 안 함.
  const [buyConditionsConfirmed, setBuyConditionsConfirmed] = useState(() => buy.conditions.length > 0);
  const [priceTouched, setPriceTouched] = useState(() => buy.conditions.length > 0);
  const [toleranceTouched, setToleranceTouched] = useState(() => buy.conditions.length > 0);
  const [sizingTouched, setSizingTouched] = useState(() => buy.conditions.length > 0);
  const [buyAmountPctTouched, setBuyAmountPctTouched] = useState(() => buy.conditions.length > 0);
  const [amountKrwTouched, setAmountKrwTouched] = useState(() => buy.conditions.length > 0);
  const [atrRiskPctTouched, setAtrRiskPctTouched] = useState(() => buy.conditions.length > 0);
  const [atrMultTouched, setAtrMultTouched] = useState(() => buy.conditions.length > 0);

  if (symbols.length === 0) return <p className="muted">데이터 불러오는 중…</p>;

  const hasBuyTarget = tradeSymbol.trim().length > 0;
  const hasBuyConditions = buy.conditions.length > 0;
  const hasSellSetup = Object.values(exits).some((r) => r.on)
    || sell.conditions.length > 0;
  const showBuyCond = hasBuyTarget;
  // ② 노출: 매수후보 + 매수조건 1개 이상 + 사용자가 [▶ 다음] 명시 click
  const showBuyPrice = showBuyCond && hasBuyConditions && buyConditionsConfirmed;
  // ③ 노출: priceTouched 후. 지정가는 tolerance 입력까지 필수, 시장가는 라디오 선택만으로 OK
  const showBuySize = showBuyPrice && priceTouched && (!useLimit || toleranceTouched);
  // 모드별 값 입력 완료 판정 — 균등분배는 입력 없음, 나머지는 사용자 명시 입력 필요
  const sizingValueOk =
    sizingMode === "equal_weight" ||
    (sizingMode === "pct_cash" && buyAmountPctTouched) ||
    (sizingMode === "fixed_amount" && amountKrwTouched) ||
    (sizingMode === "atr_risk" && atrRiskPctTouched && atrMultTouched);
  // ④ 요약 노출: 모드 카드 + 모드별 값 입력까지 완료
  const showBuySummary = showBuySize && sizingTouched && sizingValueOk;
  // 매도 panel 노출은 ④까지 완료해야.
  const showSell = showBuySummary;
  const showRest = showSell && hasSellSetup;

  return (
    <>
      {!hasMaster && (
        <div className="panel" style={{
          borderLeft: "4px solid var(--amber)",
          background: "var(--amber-soft)",
        }}>
          <strong style={{ color: "var(--amber)" }}>
            ⏳ KIS 종목마스터를 준비 중입니다
          </strong>
          <p className="muted" style={{ margin: "6px 0 0", fontSize: 13 }}>
            서버가 KIS 공식 마스터를 다운로드 중입니다 (보통 수 초). 잠시 후
            페이지를 새로고침해주세요. 사용자 추가 행동은 필요하지 않습니다.
          </p>
        </div>
      )}
      <BuyTargetPanel
        name={name} setName={setName}
        symbols={symbols}
        tradeSymbol={tradeSymbol} setTradeSymbol={setTradeSymbol}
        screenerLimit={screenerLimit} setScreenerLimit={setScreenerLimit}
        screenerSpec={screenerSpec} setScreenerSpec={setScreenerSpec}
        rebalance={rebalance} setRebalance={setRebalance}
      />

      {showBuyCond && (
      <>
      {/* Phase 49 — 매수 조건 통합 문장 sentence. ①조건 · ②가격 · ③수량 · ④종목 4 절. */}
      <div className="panel buy-sentence-panel">
        <h3>2. 일일 장초 매수
          <span className="metric-hint lg" data-tip="매일 장 시작 시 평가·발주 — ① 조건이 충족되는 날 · ② 정해진 가격으로 · ③ 정해진 금액만큼 · ④ 매수후보를 매수합니다.">ⓘ</span>
        </h3>

        {/* ① 조건 절 */}
        <section className="sentence-clause">
          <div className="sentence-clause-head">
            <span className="sentence-clause-num">①</span>
            <span className="sentence-clause-label">매수조건 —</span>
            <span className="muted small">아래 조건이 충족되는 날</span>
            <span className="metric-hint lg" data-tip="가격·수익률 지표는 모두 정규장 종가(15:30 마감) 기준. 시간외 단일가는 반영되지 않습니다.">ⓘ</span>
          </div>
          <ConditionBuilder
            symbols={symbols} group={buy} onChange={setBuy}
            onAddCondition={
              !buyConditionsConfirmed
                ? () => setBuyConditionsConfirmed(true) : undefined
            }
          />
        </section>

        {/* ② 가격 절 — ① 채워야 노출. 라디오 default OFF — 사용자가 명시 click해야 active + ③ 노출 */}
        {showBuyPrice && (
        <section className="sentence-clause">
          <div className="sentence-clause-head">
            <span className="sentence-clause-num">②</span>
            <span className="sentence-clause-label">매수가격 —</span>
            <span className="muted small">매수 발주 방식</span>
          </div>
          <BuyPricePanel
            useLimit={useLimit}
            setUseLimit={(v) => {
              setUseLimit(v); setPriceTouched(true);
              // 가격 모드 바뀌면 tolerance touched 리셋 — 지정가↔시장가 전환 시 재입력 유도
              if (v !== useLimit) setToleranceTouched(false);
            }}
            buyTolerancePct={buyTolerancePct}
            selected={priceTouched} onSelect={() => setPriceTouched(true)}
            toleranceTouched={toleranceTouched}
            onToleranceChange={(v) => { setBuyTolerancePct(v); setToleranceTouched(true); }}
          />
        </section>
        )}

        {/* ③ 수량 절 — ② 사용자 click 후 노출. 4 card default OFF — 사용자 명시 click해야 active + ④ 노출 */}
        {showBuySize && (
        <section className="sentence-clause">
          <div className="sentence-clause-head">
            <span className="sentence-clause-num">③</span>
            <span className="sentence-clause-label">매수규모 —</span>
            <span className="muted small">한 종목당 투입 금액</span>
          </div>
          <div className="sizing-cards">
            <SizingCard
              on={sizingTouched && sizingMode === "pct_cash"} title="정률"
              desc={`자본의 N%를 한 종목에 — 자본이 늘면 매수액도 자동 증가`}
              onPick={() => { setSizingTouched(true); setSizingMode("pct_cash"); }} />
            <SizingCard
              on={sizingTouched && sizingMode === "fixed_amount"} title="정액"
              desc={`한 종목당 고정 금액 — "이 종목 100만원어치 사라"`}
              onPick={() => { setSizingTouched(true); setSizingMode("fixed_amount"); }} />
            <SizingCard
              on={sizingTouched && sizingMode === "equal_weight"} title="균등 분배"
              desc={`자본을 동시 보유 종목 수로 나눔 — 종목당 동일 금액`}
              onPick={() => { setSizingTouched(true); setSizingMode("equal_weight"); }} />
            <SizingCard
              on={sizingTouched && sizingMode === "atr_risk"} title="리스크 기반 (ATR)"
              desc={`변동성에 반비례 — 종목별 동일 손실 위험`}
              onPick={() => { setSizingTouched(true); setSizingMode("atr_risk"); }} />
          </div>

          {sizingTouched && sizingMode === "pct_cash" && (
            <>
              <div className="amount-row">
                <label>자본의</label>
                <input type="number" min={1} max={100}
                       value={buyAmountPctTouched ? buyAmountPct : ""}
                       placeholder="예: 10"
                       onChange={(e) => {
                         setBuyAmountPct(Number(e.target.value));
                         setBuyAmountPctTouched(true);
                       }} />
                <span className="muted">
                  {buyAmountPctTouched
                    ? `%  =  ${wonReadable(capital * buyAmountPct / 100)}`
                    : "% ← 직접 입력 (보통 5 ~ 20%)"}
                </span>
              </div>
              {buyAmountPctTouched && screenerLimit > 1 && (
                <div
                  className={"muted small" + (screenerLimit * buyAmountPct > 100 ? " warn" : "")}
                  style={{ marginTop: 4 }}
                >
                  ⚠ 자동 선택 {screenerLimit}종목 × {fmt2(buyAmountPct)}% ={" "}
                  <b>{fmt2(screenerLimit * buyAmountPct)}%</b> 전체 노출
                  {screenerLimit * buyAmountPct > 100 && " (100% 초과 — 현금 부족 시 일부 종목 매수 실패)"}
                </div>
              )}
            </>
          )}

          {sizingTouched && sizingMode === "fixed_amount" && (
            <div className="amount-row">
              <label>한 종목당</label>
              <input type="number" min={0} step={10000}
                     value={amountKrwTouched ? amountKrw : ""}
                     placeholder="예: 1000000"
                     onChange={(e) => {
                       setAmountKrw(Number(e.target.value));
                       setAmountKrwTouched(true);
                     }} />
              <span className="muted">
                {amountKrwTouched
                  ? `원  =  ${wonReadable(amountKrw)}${amountKrw > 0 && capital > 0 ? ` (자본의 ${fmt2(amountKrw / capital * 100)}%)` : ""}`
                  : "원 ← 직접 입력 (예: 100만원)"}
              </span>
            </div>
          )}

          {sizingTouched && sizingMode === "equal_weight" && (
            <div className="muted small">
              동시 보유 한도 <b>{screenerLimit}종목</b> 기준 한 종목당{" "}
              <b>{wonReadable(capital / Math.max(screenerLimit, 1))}</b>
              ({fmt2(100 / Math.max(screenerLimit, 1))}%) 투입.
              <br />
              한도는 수동 선택 시 선택 종목 수, 자동 선택 시 세트의 상위 N개로 결정됩니다 (시스템 최대 30).
            </div>
          )}

          {sizingTouched && sizingMode === "atr_risk" && (
            <div className="atr-detail">
              <div className="amount-row">
                <label>트레이드당 자본 위험</label>
                <input type="number" min={0.1} max={10} step={0.1}
                       value={atrRiskPctTouched ? atrRiskPct : ""}
                       placeholder="예: 1"
                       onChange={(e) => {
                         setAtrRiskPct(Number(e.target.value));
                         setAtrRiskPctTouched(true);
                       }} />
                <span className="muted">
                  {atrRiskPctTouched ? "%" : "% ← 직접 입력 (보통 0.5 ~ 2%)"}
                </span>
              </div>
              <div className="amount-row">
                <label>ATR 배수 (손절폭)</label>
                <input type="number" min={0.5} max={5} step={0.1}
                       value={atrMultTouched ? atrMult : ""}
                       placeholder="예: 2"
                       onChange={(e) => {
                         setAtrMult(Number(e.target.value));
                         setAtrMultTouched(true);
                       }} />
                <span className="muted">
                  {atrMultTouched ? "× ATR" : "× ATR ← 직접 입력 (보통 1 ~ 3)"}
                </span>
              </div>
              {atrRiskPctTouched && atrMultTouched && (
                <div className="muted small" style={{ marginTop: 6 }}>
                  ⓘ 각 종목이 ATR×{fmt2(atrMult)} 만큼 하락하면 자본의 {fmt2(atrRiskPct)}% 손실
                  <br />
                  ⚠ ATR 데이터가 없는 종목은 자동 fallback하지 않고 매수를 건너뜁니다.
                </div>
              )}
            </div>
          )}
        </section>
        )}

        {/* ④ 1문장 요약 — ③ 사용자 confirm 후 노출 */}
        {showBuySummary && (
        <section className="sentence-clause sentence-clause-target sentence-clause-summary">
          <p style={{ margin: 0, lineHeight: 1.6 }}>
            <strong>매수후보</strong>가 위 매수조건을 만족하는 날,&nbsp;
            <strong>
              {useLimit
                ? `지정가(전일종가 +${fmt2(buyTolerancePct)}% 이내)`
                : "시장가"}
            </strong>
            로&nbsp;
            <strong>
              {sizingMode === "pct_cash"
                && `자본의 ${fmt2(buyAmountPct)}% (${wonReadable(capital * buyAmountPct / 100)})`}
              {sizingMode === "fixed_amount"
                && `한 종목당 ${wonReadable(amountKrw)}`}
              {sizingMode === "equal_weight"
                && `자본 ÷ ${screenerLimit}종목 균등 (${wonReadable(capital / Math.max(screenerLimit, 1))})`}
              {sizingMode === "atr_risk"
                && `ATR×${fmt2(atrMult)} 손절폭, 자본 위험 ${fmt2(atrRiskPct)}%`}
            </strong>
            씩 매수합니다.
          </p>
        </section>
        )}
      </div>
      </>
      )}

      {showSell && (
      <>
      {/* Phase 51 — 3. 매도 조건 details. default 매도 룰(익절10·손절-5·보유5)이
          ON이라 접혀도 매매 안전. summary에 활성 룰 미리보기. */}
      <details className="panel section-collapsible">
        <summary>
          <h3>3. 매도 조건 <span className="muted">(하나 이상 설정 필수 · 먼저 트리거되는 규칙으로 매도)</span></h3>
          <span className="sect-summary-meta">
            {(() => {
              const active = Object.values(exits).filter((v) => v.on).length;
              const cond = sell.conditions.length;
              return active + cond > 0
                ? `${active}개 매도 룰${cond ? ` · 추가 조건 ${cond}` : ""}`
                : "(설정 필요)";
            })()}
          </span>
        </summary>

        {/* Phase 56 — 매도 카테고리 2가지 토글 (다중선택). 선택된 카테고리만 상세 옵션 노출. */}
        <div className="sell-category-toggles">
          <label className={"sell-category-toggle" + (sellRealtimeEnabled ? " on" : "")}>
            <input type="checkbox" checked={sellRealtimeEnabled}
                   onChange={(e) => setSellRealtimeEnabled(e.target.checked)} />
            <div className="sell-category-text">
              <strong>실시간 매도</strong>
              <span className="muted small">tick마다 평가, 즉시 발주 (가격 기반 — 익절·손절·트레일링·ATR)</span>
            </div>
          </label>
          <label className={"sell-category-toggle" + (sellEodEnabled ? " on" : "")}>
            <input type="checkbox" checked={sellEodEnabled}
                   onChange={(e) => setSellEodEnabled(e.target.checked)} />
            <div className="sell-category-text">
              <strong>일일 시가 매도</strong>
              <span className="muted small">매일 08:55 사이클, 09:00 시초가 매도 (보유기간·지표 기반 조건)</span>
            </div>
          </label>
        </div>

        {sellRealtimeEnabled && (
          <div className="sell-category-detail">
            <div className="sub-h" style={{ marginTop: 4 }}>
              실시간 매도 상세
              <span className="metric-hint lg" data-tip="09:00~15:30 정규장 중 KIS 시세 WebSocket으로 매 tick 평가. 가격이 닿는 즉시 매도 발주.">ⓘ</span>
            </div>
            <div className="rule-list">
              {RULE_DEFS.map((r) => {
                const st = exits[r.key];
                return (
                  <label className="rule-row" key={r.key}>
                    <input
                      type="checkbox" checked={st.on}
                      onChange={(e) => setRule(r.key, { on: e.target.checked })}
                    />
                    <span className="rule-name">{r.name}</span>
                    <input
                      type="number" step="any" disabled={!st.on} value={st.v}
                      onChange={(e) => setRule(r.key, { v: Number(e.target.value) })}
                    />
                    <span className="rule-suffix">{r.suffix}</span>
                    <span className="rule-sell-pct">
                      매도
                      <input
                        type="number" min={1} max={100} step={5}
                        disabled={!st.on} value={st.sell_pct}
                        onChange={(e) => setRule(r.key, {
                          sell_pct: Math.min(100, Math.max(1, Number(e.target.value) || 100)),
                        })}
                      />%
                    </span>
                  </label>
                );
              })}
            </div>
          </div>
        )}

        {sellEodEnabled && (
          <div className="sell-category-detail">
            <div className="sub-h" style={{ marginTop: 18 }}>
              일일 시가 매도 상세
              <span className="metric-hint lg" data-tip="정규장 종가 데이터로 일봉 단위 평가. 보유기간·dataset 지표 기반 조건. 보유기간은 [+ 조건 추가] → 지표에서 '보유기간' 선택.">ⓘ</span>
            </div>
            <ConditionBuilder symbols={symbols} group={sell} onChange={setSell} context="sell" />
            {sell.conditions.length > 0 && (
              <>
                <div className="amount-row" style={{ marginTop: 16 }}>
                  <label>매도 비율</label>
                  <input type="number" min={1} max={100} value={sellAmountPct}
                         onChange={(e) => setSellAmountPct(Number(e.target.value))} />
                  <span className="muted">
                    % — 조건 trigger 시 보유의 {fmt2(sellAmountPct)}% 매도
                    {sellAmountPct >= 100 ? " (전량)" : ""}
                  </span>
                </div>
                <div className="amount-row" style={{ marginTop: 12 }}>
                  <label>매도 가격 범위 (tolerance %)</label>
                  <input type="number" min={0} max={20} step={0.1}
                         value={sellTolerancePct}
                         onChange={(e) => setSellTolerancePct(Number(e.target.value))} />
                  <span className="muted">
                    매도 지정가 = 전일 종가 × (1 − {fmt2(sellTolerancePct)}%) — 갭하락 허용 범위
                  </span>
                </div>
              </>
            )}
          </div>
        )}
      </details>
      </>
      )}

      {showRest && (
      <>
      <details className="panel section-collapsible">
        <summary><h3>4. 리스크 한도 <span className="muted">(선택 — 미설정 시 기본값 적용)</span></h3></summary>

        <div className="sub-h">4-1. 단일 종목 상한</div>
        <div className="amount-row">
          <label>한 종목 최대 비중</label>
          <input type="number" min={1} max={100} step={1} value={maxPositionPct}
                 onChange={(e) => setMaxPositionPct(Number(e.target.value))} />
          <span className="muted">% (전체 자본 대비) — 사이징 결과가 이 한도 초과 시 강제 클램프</span>
        </div>

        <div className="sub-h" style={{ marginTop: 18 }}>4-2. 시스템 킬스위치</div>
        <div className="amount-row">
          <label>일일 손실 한도</label>
          <input type="number" min={0.5} max={20} step={0.1} value={dailyLossLimitPct}
                 onChange={(e) => setDailyLossLimitPct(Number(e.target.value))} />
          <span className="muted">
            % — 도달 시 <b>당일 신규 진입 차단 + 보유 종목 강제 전량 청산 + 미체결 취소</b>
          </span>
          <span className="metric-hint lg" data-tip="자본 대비 −N% 손실 시 즉시 발동. 모든 보유 종목을 'kill-switch' 사유로 매도 발주 + 진행 중인 미체결 주문 전량 cancel. 사용자가 명시적으로 reset해야 다음 거래일 진입 재개. 장중 60초마다 모니터링되어 EOD 사이클 외에도 즉시 발동.">ⓘ</span>
        </div>
        <div className="amount-row">
          <label>누적 손실 한도</label>
          <input type="number" min={1} max={50} step={1} value={maxDrawdownPct}
                 onChange={(e) => setMaxDrawdownPct(Number(e.target.value))} />
          <span className="muted">% (자본 고점 대비) — 도달 시 <b>신규 진입 차단</b> (보유분은 매도 룰대로)</span>
          <span className="metric-hint lg" data-tip="자본 고점 대비 -N% 하락 시 발동. 신규 진입만 차단 — 보유 종목은 사용자 매도 룰(익절·손절·트레일링·보유기간)대로 정상 동작. peak 회복 시 자동 해제. 일일 킬스위치와 달리 강제 청산 없음 (장기 침체 시 저점 매도 사고 방지).">ⓘ</span>
        </div>

        {/* Phase 49 — 4-3 "매수 발주 가격 범위"는 2번 매수 조건의 ② 가격 절로 이동. */}
      </details>

      {/* Phase 39 — 백테스트 비용 가정 (실매매 영향 없음) */}
      <details className="panel section-collapsible">
        <summary><h3>5. 백테스트 가정 <span className="muted">(백테스트 결과의 보수성에만 영향 · 실매매(모의/실전) 영향 없음)</span></h3></summary>
        <div className="amount-row">
          <label>위탁수수료 (편도)</label>
          <input type="number" min={0} max={200} step={1} value={btCommissionBps}
                 onChange={(e) => setBtCommissionBps(Number(e.target.value))} />
          <span className="muted">
            bps — 1bps=0.01%. 매수·매도 양쪽 모두 적용 (KIS 위탁수수료만). default 3 (= 0.03%).
          </span>
        </div>
        <div className="amount-row">
          <label>거래세 (매도 단방향)</label>
          <input type="number" min={0} max={50} step={1} value={btSellTaxBps}
                 onChange={(e) => setBtSellTaxBps(Number(e.target.value))} />
          <span className="muted">
            bps — 매도 시에만 적용 (한국 시장 비대칭 비용). KOSPI/KOSDAQ 평균 23 (= 0.23%, 농특세 포함).
          </span>
        </div>
        <div className="amount-row">
          <label>슬리피지 (편도)</label>
          <input type="number" min={0} max={200} step={1} value={btSlippageBps}
                 onChange={(e) => setBtSlippageBps(Number(e.target.value))} />
          <span className="muted">
            bps — 호가 갭/체결 지연으로 인한 가격 차이. default 10 (= 0.10%).
          </span>
        </div>
        <div className="amount-row">
          <label>
            <input type="checkbox" checked={btGapExtraCost}
                   onChange={(e) => setBtGapExtraCost(e.target.checked)}
                   style={{ marginRight: 6 }} />
            갭일 추가 비용
          </label>
          <span className="muted">
            전일 종가 대비 시초가 갭이 임계값 초과 시, 갭의 절반을 추가 비용으로 산입 (보수적 가정).
          </span>
        </div>
        {btGapExtraCost && (
          <div className="amount-row">
            <label>갭 임계값</label>
            <input type="number" min={0.1} max={10} step={0.1} value={btGapThresholdPct}
                   onChange={(e) => setBtGapThresholdPct(Number(e.target.value))} />
            <span className="muted">% — 이 이상 갭이면 추가 비용 발생. default 1.0%.</span>
          </div>
        )}
      </details>

      {/* Phase 51 — 6. 자금 details. default 1,000만원이라 접혀도 백테스트 가능. */}
      <details className="panel section-collapsible">
        <summary>
          <h3>6. 자금</h3>
          <span className="sect-summary-meta">{wonReadable(capital)}</span>
        </summary>
        <div className="row">
          <div>
            <label>초기자본(원)</label>
            <CapitalInput value={capital} onChange={setCapital} />
            <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
              = {wonReadable(capital)}
            </div>
          </div>
        </div>
      </details>

      <div className="action-bar">
        <div className="action-bar-info">
          <strong>{name || "새 전략"}</strong> · {tradeSymbol || "종목 미선택"} 매수
        </div>
        <div className="action-bar-actions">
          <span className="fwd">
            분석:
            <input type="number" min={1} value={forwardDays}
                   onChange={(e) => setForwardDays(Number(e.target.value))} />
            일 뒤 수익률
            <button className="ghost"
                    disabled={!!busy || !!parseScreenerKey(tradeSymbol)}
                    title={parseScreenerKey(tradeSymbol)
                      ? "자동 선택 전략은 통계 미리보기 미지원 — 수동 종목으로 분석하세요." : undefined}
                    onClick={runAnalysis}>
              {busy === "analysis" ? "분석 중…" : "통계 미리보기"}
            </button>
          </span>
          <button
            disabled={!!busy}
            onClick={runBacktest}>
            {busy === "backtest" ? "실행 중…" : "백테스트 실행"}
          </button>
        </div>
      </div>
      </>
      )}

      {analysis && (
        <div className="panel">
          <h3>통계 미리보기 결과</h3>
          {analysis.success ? (
            <div className="cards">
              <Stat label="표본 수" value={`${analysis.n_samples}회`}
                    hint="조건이 과거에 발생한 횟수입니다. 많을수록 결과가 안정적입니다." />
              <Stat label="양수 확률"
                    value={`${fmt2(analysis.prob_positive)}%`}
                    hint="조건 발생 후 N일 뒤 수익률이 플러스였던 비율입니다." />
              <Stat label="평균 수익률" value={`${fmt2(analysis.mean)}%`}
                    hint="조건 발생 후 N일 뒤 수익률의 평균입니다." />
              <Stat label="중앙값" value={`${fmt2(analysis.median)}%`}
                    hint="수익률을 줄 세웠을 때 한가운데 값입니다." />
              <Stat label="p-value" value={fmt2(analysis.p_value)}
                    hint="이 결과가 우연일 확률입니다. 값이 작을수록 통계적으로 유의미합니다." />
            </div>
          ) : (
            <div className="error">{analysis.error}</div>
          )}
        </div>
      )}
    </>
  );
}

/** 매수후보 panel — 두 큰 버튼(수동/자동)으로 모달 호출. 모달 안 탭으로 모드 전환. */
function BuyTargetPanel({
  name, setName, symbols, tradeSymbol, setTradeSymbol,
  screenerLimit, setScreenerLimit,
  screenerSpec, setScreenerSpec, rebalance, setRebalance,
}: {
  name: string; setName: (v: string) => void;
  symbols: SymbolInfo[];
  tradeSymbol: string; setTradeSymbol: (v: string) => void;
  screenerLimit: number; setScreenerLimit: (v: number) => void;
  screenerSpec: ScreenerSpecIO | null; setScreenerSpec: (s: ScreenerSpecIO | null) => void;
  rebalance: RebalanceIO; setRebalance: (r: RebalanceIO) => void;
}) {
  const isScreener = tradeSymbol.startsWith("screener:");
  const manualSymbols = isScreener
    ? [] : tradeSymbol.split(",").map((s) => s.trim()).filter(Boolean);

  // 모달이 열려있는 경우 어느 탭으로 열렸는지. null = 닫힘.
  const [modalOpen, setModalOpen] = useState<null | "manual" | "screener">(null);

  // 자동 선택 preset title 표시 위해 list 1회 fetch (실패해도 key fallback).
  const [presets, setPresets] = useState<ScreenerPreset[]>([]);
  useEffect(() => {
    if (presets.length > 0) return;
    api.listScreenerPresets().then((r) => setPresets(r.presets)).catch(() => {});
  }, [presets.length]);

  // 요약 라인 (미선정 시 null — 빈 텍스트 노출 안 함).
  // 수동: 종목 풀네임 (코드 제외) 콤마 join, 6개 cap + 외 N개. CSS line-clamp:2 안전망.
  // 자동: prefix 없이 preset title (있으면) + (상위 N종목).
  const SUMMARY_CAP = 6;
  let summary: string | null = null;
  if (isScreener) {
    const key = tradeSymbol.slice("screener:".length);
    const label = key === "custom"
      ? (screenerSpec?.label || "커스텀 스펙")
      : (presets.find((p) => p.key === key)?.title ?? key);
    summary = `${label} (상위 ${screenerLimit}종목)`;
  } else if (manualSymbols.length > 0) {
    const names = manualSymbols.map((sym) =>
      symbols.find((s) => s.symbol === sym)?.name || sym);
    const shown = names.slice(0, SUMMARY_CAP).join(", ");
    const extra = names.length - SUMMARY_CAP;
    summary = extra > 0 ? `${shown} 외 ${extra}개` : shown;
  }

  return (
    <div className="panel">
      <h3>1. 매수후보</h3>
      <div className="row" style={{ marginBottom: 12 }}>
        <div style={{ flex: 1 }}>
          <label>전략 이름</label>
          <input value={name} onChange={(e) => setName(e.target.value)} />
        </div>
      </div>

      <div className="sub-h">선정 방식</div>
      <div className="buy-target-buttons">
        <button type="button"
                className={"buy-target-btn"
                  + (!isScreener && manualSymbols.length > 0 ? " on" : "")}
                onClick={() => setModalOpen("manual")}>
          <strong>수동 선택</strong>
          <span className="muted small">매수 후보 직접 선정</span>
        </button>
        <button type="button"
                className={"buy-target-btn" + (isScreener ? " on" : "")}
                onClick={() => setModalOpen("screener")}>
          <strong>자동 선택</strong>
          <span className="muted small">시총·등락률 등 조건으로 자동 선정</span>
        </button>
      </div>

      {summary && <div className="buy-target-summary">{summary}</div>}

      {modalOpen === "manual" && (
        <ManualPickerModal
          symbols={symbols}
          tradeSymbol={tradeSymbol} setTradeSymbol={setTradeSymbol}
          setScreenerLimit={setScreenerLimit}
          onClose={() => setModalOpen(null)}
        />
      )}
      {modalOpen === "screener" && (
        <ScreenerPickerModal
          symbols={symbols}
          tradeSymbol={tradeSymbol} setTradeSymbol={setTradeSymbol}
          screenerLimit={screenerLimit} setScreenerLimit={setScreenerLimit}
          screenerSpec={screenerSpec} setScreenerSpec={setScreenerSpec}
          rebalance={rebalance} setRebalance={setRebalance}
          onClose={() => setModalOpen(null)}
        />
      )}
    </div>
  );
}

/** 수동 선택 모달 — draft state로 작업, [적용] 클릭 시에만 부모에 commit.
 *  [✕] 또는 overlay 클릭은 commit 없이 닫음 (취소).
 *
 *  Phase 56 — 보유 한도 입력 제거. 수동 선택한 N개를 그대로 보유 (시스템 전역 30 cap). */
function ManualPickerModal({
  symbols, tradeSymbol, setTradeSymbol, setScreenerLimit, onClose,
}: {
  symbols: SymbolInfo[];
  tradeSymbol: string; setTradeSymbol: (v: string) => void;
  setScreenerLimit: (v: number) => void;
  onClose: () => void;
}) {
  // 진입 시 스냅샷. 자동 선택 모드(screener:)였다면 빈 문자열로 시작 — 수동과 호환 안 됨.
  const [draftSymbol, setDraftSymbol] = useState(
    tradeSymbol.startsWith("screener:") ? "" : tradeSymbol);
  const manualSymbols = draftSymbol
    .split(",").map((s) => s.trim()).filter(Boolean);

  function apply() {
    setTradeSymbol(draftSymbol);
    // 보유 한도 = 선택한 종목 수 (시스템 전역 30 cap). 1 이상으로 강제.
    setScreenerLimit(Math.min(Math.max(manualSymbols.length, 1), 30));
    onClose();
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal buy-target-modal" onClick={(e) => e.stopPropagation()}>
        <header className="modal-head">
          <h2>수동 선택 — 매수후보 종목</h2>
          <button className="ghost sm" onClick={onClose}>✕</button>
        </header>
        <div className="modal-body">
          <p className="muted small" style={{ marginTop: 0, marginBottom: 10 }}>
            매수 후보로 사용할 종목을 직접 선택합니다 (시스템 한도 최대 30종목).
          </p>
          <MultiSymbolPicker
            symbols={symbols}
            value={draftSymbol}
            onChange={setDraftSymbol}
            inline
          />
          {manualSymbols.length > 30 && (
            <p className="muted small warn" style={{ marginTop: 8 }}>
              ⚠ 30종목 초과 선택은 시스템 한도로 인해 상위 30개만 실제 매매됩니다.
            </p>
          )}
        </div>
        <footer className="modal-foot">
          <button onClick={apply}>적용</button>
        </footer>
      </div>
    </div>
  );
}

/** 자동 선택 모달 — draft state로 작업, [적용] 클릭 시에만 부모에 commit.
 *  [✕] 또는 overlay 클릭은 commit 없이 닫음 (취소). 라이브 전용. */
function ScreenerPickerModal({
  symbols, tradeSymbol, setTradeSymbol,
  screenerLimit, setScreenerLimit,
  screenerSpec, setScreenerSpec, rebalance, setRebalance, onClose,
}: {
  symbols: SymbolInfo[];
  tradeSymbol: string; setTradeSymbol: (v: string) => void;
  screenerLimit: number; setScreenerLimit: (v: number) => void;
  screenerSpec: ScreenerSpecIO | null; setScreenerSpec: (s: ScreenerSpecIO | null) => void;
  rebalance: RebalanceIO; setRebalance: (r: RebalanceIO) => void;
  onClose: () => void;
}) {
  // 진입 시 스냅샷. 수동(콤마 list) 상태였다면 빈 문자열로 시작 — screener:와 호환 X.
  const [draftSymbol, setDraftSymbol] = useState(
    tradeSymbol.startsWith("screener:") ? tradeSymbol : "");
  const [draftSpec, setDraftSpec] = useState<ScreenerSpecIO | null>(screenerSpec);
  const [draftLimit, setDraftLimit] = useState(screenerLimit);
  const [draftRebalance, setDraftRebalance] = useState<RebalanceIO>(rebalance);

  function apply() {
    setTradeSymbol(draftSymbol);
    setScreenerSpec(draftSpec);
    setScreenerLimit(draftLimit);
    setRebalance(draftRebalance);
    onClose();
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal buy-target-modal" onClick={(e) => e.stopPropagation()}>
        <header className="modal-head">
          <h2>자동 선택 — 매수후보 세트</h2>
          <button className="ghost sm" onClick={onClose}>✕</button>
        </header>
        <div className="modal-body">
          <p className="muted small" style={{ marginTop: 0, marginBottom: 10 }}>
            매일 시가총액·등락률·거래대금 등 조건으로 후보를 자동 선정합니다 (라이브 전용).
          </p>
          <SymbolPicker
            symbols={symbols} value={draftSymbol} tradableOnly
            lockMode="screener"
            onChange={setDraftSymbol}
            screenerSpec={draftSpec} setScreenerSpec={setDraftSpec}
            setScreenerLimit={setDraftLimit}
            inline
          />
          {/* Phase 55 — 리밸런싱 3-way mode (off/hold/replace).
              업계 표준: off=패시브 ETF(Vanguard), hold=로보어드바이저(Wealthfront),
              replace=ETF reconstitution(S&P·MSCI·스마트베타). */}
          <div className="rebalance-section" style={{ marginTop: 16 }}>
            <div className="sub-h" style={{ marginBottom: 8 }}>
              자동 선택 리밸런싱 <span className="muted small">— 후보 재평가 + 보유 종목 처리</span>
            </div>
            <div className="price-mode-row">
              <label className={"price-mode-btn" + (draftRebalance.mode === "off" ? " on" : "")}>
                <input type="radio" name="rebalance-mode"
                       checked={draftRebalance.mode === "off"}
                       onChange={() => setDraftRebalance({ ...draftRebalance, mode: "off" })} />
                <div className="price-mode-text">
                  <strong>OFF — Buy-and-hold</strong>
                  <span className="muted small">
                    초기 N개 매수 후 lock-in. 후보 재평가·신규 매수 안 함. 매도 룰만 동작.
                    패시브 인덱스(Vanguard식). 회전율 최저.
                  </span>
                </div>
              </label>
              <label className={"price-mode-btn" + (draftRebalance.mode === "hold" ? " on" : "")}>
                <input type="radio" name="rebalance-mode"
                       checked={draftRebalance.mode === "hold"}
                       onChange={() => setDraftRebalance({ ...draftRebalance, mode: "hold" })} />
                <div className="price-mode-text">
                  <strong>보유 유지 + 빈 슬롯 채움</strong>
                  <span className="muted small">
                    주기마다 후보 재평가. 보유 탈락해도 매도 X. 매도 룰로 빠진 자리만 신규 후보로 채움.
                    로보어드바이저(Wealthfront·Betterment)식. <b>default.</b>
                  </span>
                </div>
              </label>
              <label className={"price-mode-btn" + (draftRebalance.mode === "replace" ? " on" : "")}>
                <input type="radio" name="rebalance-mode"
                       checked={draftRebalance.mode === "replace"}
                       onChange={() => setDraftRebalance({ ...draftRebalance, mode: "replace" })} />
                <div className="price-mode-text">
                  <strong>정기 교체 (탈락 매도 + 신규)</strong>
                  <span className="muted small">
                    정기 평가일에 상위 N 탈락 보유 매도 + 신규 편입 매수. 포트폴리오 최신 신호 반영.
                    ETF reconstitution(스마트베타·모멘텀)식. 회전율↑·세금↑.
                  </span>
                </div>
              </label>
            </div>
            {draftRebalance.mode !== "off" && (
              <div className="rebalance-detail" style={{ marginTop: 10 }}>
                <label>{draftRebalance.mode === "hold" ? "후보 재평가 주기" : "리밸런싱 주기"}</label>
                <select value={draftRebalance.period}
                        onChange={(e) => {
                          const period = e.target.value as RebalanceIO["period"];
                          setDraftRebalance({
                            ...draftRebalance, period,
                            every_n_days: period === "every_n_days"
                              ? (draftRebalance.every_n_days ?? 5) : null,
                          });
                        }}>
                  <option value="daily">매일</option>
                  <option value="weekly">매주</option>
                  <option value="monthly">매월</option>
                  <option value="every_n_days">N영업일마다</option>
                </select>
                {draftRebalance.period === "every_n_days" && (
                  <>
                    <input type="number" min={1} max={252} step={1}
                           value={draftRebalance.every_n_days ?? 5}
                           onChange={(e) => setDraftRebalance({
                             ...draftRebalance,
                             every_n_days: Math.max(1, Number(e.target.value) || 1),
                           })}
                           style={{ width: 64 }} />
                    <span className="muted small">영업일마다</span>
                  </>
                )}
                <span className="muted small">
                  ⚠ 라이브 전용. 짧은 주기는 회전율 매우 높음 (~200%+/년). 월간 이하 권장.
                  모의투자로 충분히 검증 후 사용하세요.
                </span>
              </div>
            )}
          </div>
        </div>
        <footer className="modal-foot">
          <button onClick={apply}>적용</button>
        </footer>
      </div>
    </div>
  );
}

// ── 탭 2: 결과 리포트 ─────────────────────────────────────────────────────────

function ResultTab({
  backtest, metrics, name, busy, onDraft, onApply, saveMsg,
  history, historyLoaded, onLoad, onDelete,
}: {
  backtest: BacktestResult | null;
  metrics: Record<string, number | null> | undefined;
  name: string;
  busy: string;
  onDraft: () => void;
  onApply: () => void;
  saveMsg: string;
  history: BacktestRunSummary[];
  historyLoaded: boolean;
  onLoad: (id: number) => void;
  onDelete: (id: number) => void;
}) {
  // 빈 상태: 현재 결과도 없고 이력도 없음 → 안내만 표시.
  if (!backtest && historyLoaded && history.length === 0) {
    return (
      <div className="panel empty-state">
        <div className="empty-title">아직 실행 결과가 없습니다</div>
        <p className="muted">
          [빌더] 탭에서 조건을 만들고 백테스트를 실행하세요. 결과는 자동으로 저장되어 이 탭에 누적됩니다.
        </p>
      </div>
    );
  }
  // 현재 결과는 없고 이력만 있는 상태 → 이력 리스트만 렌더 (load로 채움).
  if (!backtest) {
    return <HistoryListPanel rows={history} loaded={historyLoaded} onLoad={onLoad} onDelete={onDelete} />;
  }
  if (!backtest.success || !metrics) {
    return (
      <>
        <div className="error">{backtest.error}</div>
        <HistoryListPanel rows={history} loaded={historyLoaded} onLoad={onLoad} onDelete={onDelete} />
      </>
    );
  }

  // 백테스트가 손실/저조였던 전략을 무경고로 적용하지 않도록 사실 기반 확인.
  const m = metrics;
  function applyGuarded() {
    const ret = m.total_return;
    const excess = m.excess_return;
    const poor = (ret != null && ret < 0) || (excess != null && excess < 0);
    if (poor && !window.confirm(
      "이 전략은 백테스트에서 손실이었거나 단순 보유 대비 저조했습니다.\n"
      + "그래도 모의투자로 적용하시겠습니까?\n(실제 돈이 아닌 모의 계좌에서 실행됩니다)",
    )) return;
    onApply();
  }

  return (
    <div className="panel">
      <h3>'{name}' 백테스트 결과</h3>
      {/* Phase 48 — NFA 2-29 수준 가정적 수익률 disclaimer (동일 prominence) */}
      <div className="hypothetical-banner">
        <strong>⚠ 가정적(Hypothetical) 결과 — 실제 매매가 아닙니다</strong>
        <p>
          본 결과는 과거 데이터로 모의 산출된 <b>가정적 수익률</b>입니다.
          백테스트와 실거래 사이에는 종종 큰 차이가 발생합니다.
          과거 성과는 <b>미래 수익을 보장하지 않습니다.</b>
          슬리피지·수수료·세금은 아래 "백테스트 가정"의 설정값으로 반영되었으며,
          실제 시장 조건에서는 더 크거나 작을 수 있습니다.
        </p>
      </div>
      <Verdict metrics={metrics} />
      <div className="cards metrics" style={{ marginBottom: 18 }}>
        <Stat label="총수익률" value={`${fmt2(metrics.total_return)}%`}
              colorBy={metrics.total_return}
              hint="백테스트 전체 기간 동안 자산이 늘어난 비율입니다." />
        <Stat label="CAGR" value={`${fmt2(metrics.cagr)}%`}
              colorBy={metrics.cagr}
              hint="복리로 환산한 연평균 수익률입니다." />
        <Stat label="MDD" value={`${fmt2(metrics.mdd)}%`}
              colorBy={metrics.mdd}
              hint="고점 대비 자산이 가장 크게 떨어졌던 낙폭입니다." />
        <Stat label="샤프" value={fmt2(metrics.sharpe)}
              colorBy={metrics.sharpe}
              hint="변동성 한 단위당 거둔 수익을 나타내는 위험조정 수익 지표입니다." />
        <Stat label="승률" value={`${fmt2(metrics.win_rate)}%`}
              hint="전체 거래 중 이익으로 끝난 거래의 비율입니다." />
        <Stat label="거래 수" value={`${metrics.n_trades ?? 0}회`}
              hint="백테스트 기간 중 매수 후 청산이 완료된 횟수입니다." />
        <Stat label="vs Buy&Hold"
              value={`${fmt2(metrics.excess_return)}%p`}
              colorBy={metrics.excess_return}
              hint="같은 종목을 단순 매수·보유했을 때 대비 초과 수익(%p)입니다." />
      </div>
      <EquityChart equity={backtest.equity ?? []}
                   benchmark={backtest.benchmark} />
      <div className="spacer" />
      <details>
        <summary className="muted">
          거래 내역 ({backtest.trades?.length ?? 0}건)
        </summary>
        <table>
          <thead>
            <tr>
              <th>종목</th><th>진입일</th><th>청산일</th><th>보유일</th>
              <th>수익률(%)</th><th>청산사유</th>
            </tr>
          </thead>
          <tbody>
            {(backtest.trades ?? []).slice(0, 50).map((t, i) => (
              <tr key={i}>
                <td>{t["종목"] ?? "-"}</td>
                <td>{t["진입일"]}</td>
                <td>{t["청산일"]}</td>
                <td>{t["보유일"]}</td>
                <td>{fmt2(Number(t["수익률(%)"]))}</td>
                <td>{t["청산사유"]}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>

      <div className="spacer" />
      <div className="save-row">
        <button className="ghost" disabled={!!busy} onClick={onDraft}>
          {busy === "draft" ? "저장 중…" : "임시저장"}
        </button>
        <button disabled={!!busy} onClick={applyGuarded}>
          {busy === "apply" ? "적용 중…" : "내 전략에 적용 (모의투자)"}
        </button>
      </div>
      {saveMsg && <div className="ok">{saveMsg}</div>}
      <HistoryListPanel rows={history} loaded={historyLoaded} onLoad={onLoad} onDelete={onDelete} />
    </div>
  );
}

// ── 보관함 (결과 리포트 안 통합) ─────────────────────────────────────────────

function HistoryListPanel({ rows, loaded, onLoad, onDelete }: {
  rows: BacktestRunSummary[];
  loaded: boolean;
  onLoad: (id: number) => void;
  onDelete: (id: number) => void;
}) {
  if (!loaded) return <p className="muted" style={{ marginTop: 16 }}>이력 불러오는 중…</p>;
  if (rows.length === 0) return null;
  return (
    <div className="panel" style={{ marginTop: 18 }}>
      <h3>보관함 <span className="muted">({rows.length}건)</span></h3>
      <table>
        <thead>
          <tr>
            <th>전략</th><th>실행일</th>
            <th>총수익률</th><th>CAGR</th><th>MDD</th><th>샤프</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const m = r.metrics ?? {};
            const dt = new Date(r.created_at);
            return (
              <tr key={r.id}>
                <td>
                  <button className="link-btn" onClick={() => onLoad(r.id)}>
                    {r.name || "(이름 없음)"}
                  </button>
                </td>
                <td>{dt.toLocaleString()}</td>
                <td>{fmt2(m.total_return as number)}%</td>
                <td>{fmt2(m.cagr as number)}%</td>
                <td>{fmt2(m.mdd as number)}%</td>
                <td>{fmt2(m.sharpe as number)}</td>
                <td>
                  <button className="ghost sm" onClick={() => onDelete(r.id)}>
                    삭제
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** 초기자본 입력 — focus 시 raw 숫자, blur 시 콤마 포맷. */
function CapitalInput({ value, onChange }: {
  value: number; onChange: (v: number) => void;
}) {
  const [focused, setFocused] = useState(false);
  const [draft, setDraft] = useState(String(value));
  useEffect(() => { if (!focused) setDraft(String(value)); }, [value, focused]);
  return (
    <input
      type="text" inputMode="numeric"
      value={focused ? draft : value.toLocaleString()}
      onFocus={() => { setDraft(String(value)); setFocused(true); }}
      onBlur={() => {
        setFocused(false);
        const trimmed = draft.replace(/[^\d.-]/g, "");
        if (trimmed === "") return;     // 빈 입력은 이전 자본금 유지
        const n = Number(trimmed);
        if (!Number.isNaN(n) && n > 0) onChange(n);
      }}
      onChange={(e) => setDraft(e.target.value)}
    />
  );
}

function Stat({ label, value, hint, colorBy }: {
  label: string; value: string; hint?: string;
  // 손익 색을 입힐 기준 수치. 양수=green, 음수=red, 0/미지정=중립.
  // 사실 표시일 뿐(빨강=마이너스 숫자) — 평가·추천이 아니다.
  colorBy?: number | null;
}) {
  const tone = colorBy == null || Number.isNaN(colorBy)
    ? "" : colorBy > 0 ? " pos" : colorBy < 0 ? " neg" : "";
  return (
    <div className="stat">
      <div className="label">
        {label}
        {hint && <span className="metric-hint" data-tip={hint}>?</span>}
      </div>
      <div className={"value" + tone}>{value}</div>
    </div>
  );
}

// ── 매수 가격 모드 (Phase 49 — 시장가/지정가 토글 + tolerance) ──────────────
// Phase 56 — selected=false면 두 라디오 모두 unchecked + tolerance/warn UI 미표시.
//            사용자가 라디오 1개 명시 click해야 active.
//            toleranceTouched=false면 tolerance input 빈칸 + placeholder — 사용자 입력 강제.
function BuyPricePanel({ useLimit, setUseLimit, buyTolerancePct,
                         selected, onSelect,
                         toleranceTouched, onToleranceChange }: {
  useLimit: boolean; setUseLimit: (v: boolean) => void;
  buyTolerancePct: number;
  selected: boolean; onSelect: () => void;
  toleranceTouched: boolean; onToleranceChange: (v: number) => void;
}) {
  function pickLimit(v: boolean) { setUseLimit(v); onSelect(); }
  return (
    <div>
      <div className="price-mode-row">
        <label className={"price-mode-btn" + (selected && useLimit ? " on" : "")}>
          <input type="radio" name="buy-price-mode"
                 checked={selected && useLimit}
                 onChange={() => pickLimit(true)} />
          <div className="price-mode-text">
            <strong>지정가 (±tolerance%)</strong>
            <span className="muted small">전일 종가 ±N% 내에서만 매수 — 갭상승 자동 회피</span>
          </div>
        </label>
        <label className={"price-mode-btn" + (selected && !useLimit ? " on" : "")}>
          <input type="radio" name="buy-price-mode"
                 checked={selected && !useLimit}
                 onChange={() => pickLimit(false)} />
          <div className="price-mode-text">
            <strong>시장가</strong>
            <span className="muted small">즉시 체결 — 시초가 갭에 무방비, 변동성 큰 종목 주의</span>
          </div>
        </label>
      </div>
      {selected && useLimit && (
        <div className="amount-row" style={{ marginTop: 10 }}>
          <label>전일 종가 + 최대</label>
          <input type="number" min={0.1} max={5} step={0.1}
                 value={toleranceTouched ? buyTolerancePct : ""}
                 placeholder="예: 1"
                 onChange={(e) => onToleranceChange(Number(e.target.value))} />
          <span className="muted">% 까지 매수 허용</span>
          <span className="metric-hint lg"
                data-tip={`발주가 = 전일 종가 × (1 + N%). 시초가가 이보다 높으면 미체결 폐기. 변동성 큰 종목은 N 키우면 잡힐 확률↑, 작으면 갭상승 자동 회피.`}>ⓘ</span>
          {toleranceTouched && buyTolerancePct < 0.1 && (
            <span className="metric-hint lg" style={{ background: "var(--amber-soft)", color: "var(--amber)" }}
                  data-tip="0%(또는 0.1% 미만)은 시초가가 정확히 전일 종가와 같아야 체결. 실제로는 거의 미체결됩니다. 0.5% 이상 권장.">⚠</span>
          )}
          {!toleranceTouched && (
            <span className="muted small">← 직접 입력 (보통 0.5 ~ 2%)</span>
          )}
        </div>
      )}
      {selected && !useLimit && (
        <div className="warn-box" style={{ marginTop: 10 }}>
          ⚠ 시장가 매수는 시초가 동시호가에서 발주됩니다. 큰 갭상승 종목도 그대로 잡힙니다 —
          예상 외 진입가를 피하려면 [지정가 + tolerance]를 권장합니다.
        </div>
      )}
    </div>
  );
}

// ── 사이징 모드 카드 (Phase 47 — 4지 통합 셀렉터) ───────────────────────────
function SizingCard({ on, title, desc, onPick }: {
  on: boolean; title: string; desc: string; onPick: () => void;
}) {
  return (
    <button type="button"
            className={"sizing-card" + (on ? " on" : "")}
            onClick={onPick}>
      <div className="sizing-card-title">{title}</div>
      <div className="sizing-card-desc">{desc}</div>
    </button>
  );
}

// ── 탭 4: 마켓플레이스 (V2 placeholder) ──────────────────────────────────────
function MarketplacePlaceholder() {
  return (
    <div className="panel marketplace-placeholder">
      <div className="soon-banner">🚧 V2 예정</div>
      <h3>전략 마켓플레이스</h3>
      <p className="muted">
        검증된 전략을 둘러보고, 마음에 드는 전략을 fork해서 내 전략으로 가져옵니다.
        다른 사용자의 성과·승률·드로우다운을 한눈에 비교할 수 있게 됩니다.
      </p>
      <ul className="soon-list">
        <li>큐레이션된 무료 전략 5~10종 (RSI 역추세 · 모멘텀 · 골든크로스 등)</li>
        <li>한 번의 클릭으로 내 전략으로 가져오기 (fork)</li>
        <li>마켓플레이스 전략의 라이브 성과 추적</li>
        <li>유료 전략 · 정산 · 환불 · 평가 (Phase V3)</li>
      </ul>
      {/* Phase 48 P2-A — 카피트레이딩·시그널 자동 복제 정책 명시 (가이드라인 §A.4) */}
      <div className="warn-box" style={{ marginTop: 14 }}>
        ⚠ <b>카피트레이딩·시그널 자동 복제는 본 플랫폼에서 제공되지 않습니다.</b><br/>
        "특정 트레이더와 동일하게 거래" 형태의 자동 복제는 자본시장법상 <b>투자일임업
        인가</b>가 필요한 영역입니다. 마켓플레이스는 <b>fork(전략 정의 복사) → 본인이
        직접 룰 검토·수정 → 본인 책임으로 자동매매</b> 흐름으로만 제공될 예정입니다.
      </div>
      <p className="muted small">
        지금은 [빌더]에서 직접 전략을 만들거나, 다른 트레이더의 글·블로그를 참고해
        조건을 수동 구성해 주세요.
      </p>
    </div>
  );
}
