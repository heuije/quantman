import { useEffect, useState } from "react";
import { api } from "../api";
import ConditionBuilder from "../components/ConditionBuilder";
import SymbolPicker from "../components/SymbolPicker";
import EquityChart from "../components/EquityChart";
import Verdict from "../components/Verdict";
import { fmt2, wonReadable } from "../format";
import type {
  AnalysisResult, BacktestResult, BacktestRunSummary, ConditionGroup,
  StrategyDef, SymbolInfo,
} from "../types";

/** 청산 규칙 정의 — 켜진 규칙 중 먼저 트리거되는 것으로 청산. */
type RuleKey = "hold" | "tp" | "sl" | "trail" | "atr";
const RULE_DEFS: { key: RuleKey; name: string; suffix: string }[] = [
  { key: "hold",  name: "보유기간",      suffix: "일 경과 시" },
  { key: "tp",    name: "익절",          suffix: "% 이상 수익 시" },
  { key: "sl",    name: "손절",          suffix: "% 이하 수익 시 (음수 입력)" },
  { key: "trail", name: "트레일링 스톱",  suffix: "% 하락 시 (진입 후 고점 대비)" },
  { key: "atr",   name: "ATR 트레일링",   suffix: "× ATR 만큼 고점에서 하락 시" },
];

type TabKey = "build" | "result" | "history";

export default function Backtest() {
  const [tab, setTab] = useState<TabKey>("build");

  const [symbols, setSymbols] = useState<SymbolInfo[]>([]);
  const [hasMaster, setHasMaster] = useState<boolean>(true);
  const [name, setName] = useState("새 전략");
  const [tradeSymbol, setTradeSymbol] = useState("");
  const [buy, setBuy] = useState<ConditionGroup>({ conditions: [], logic: "AND" });
  const [sell, setSell] = useState<ConditionGroup>({ conditions: [], logic: "AND" });
  const [exits, setExits] = useState<Record<RuleKey, { on: boolean; v: number }>>({
    hold:  { on: true,  v: 5 },
    tp:    { on: true,  v: 10 },
    sl:    { on: true,  v: -5 },
    trail: { on: false, v: 8 },
    atr:   { on: false, v: 3 },
  });
  const [buyAmountPct, setBuyAmountPct] = useState(100);
  const [sellAmountPct, setSellAmountPct] = useState(100);
  const [capital, setCapital] = useState(10_000_000);
  const [forwardDays, setForwardDays] = useState(1);

  const [analysis, setAnalysis] = useState<AnalysisResult | null>(null);
  const [backtest, setBacktest] = useState<BacktestResult | null>(null);
  const [busy, setBusy] = useState<"" | "analysis" | "backtest" | "draft" | "apply">("");
  const [err, setErr] = useState("");
  const [saveMsg, setSaveMsg] = useState("");

  const [history, setHistory] = useState<BacktestRunSummary[]>([]);
  const [historyLoaded, setHistoryLoaded] = useState(false);

  function setRule(key: RuleKey, patch: Partial<{ on: boolean; v: number }>) {
    setExits((e) => ({ ...e, [key]: { ...e[key], ...patch } }));
  }

  useEffect(() => {
    api.symbols().then((r) => {
      setSymbols(r.symbols);
      setHasMaster(r.has_master);
      // 매수 대상 default: 첫 tradable 종목 (백테스트 데이터 유무 무관)
      const firstTradable = r.symbols.find((s) => s.tradable);
      if (firstTradable) setTradeSymbol(firstTradable.symbol);
      // 매수 조건 default: indicators 있는 첫 종목 (매크로/자산 등)
      const firstWithInd = r.symbols.find((s) => s.indicators.length > 0);
      if (firstWithInd) {
        const ind = firstWithInd.indicators.find(
          (i) => i.key.includes("pct_change") || i.key.includes("return"),
        ) ?? firstWithInd.indicators[0];
        setBuy({
          logic: "AND",
          conditions: [{
            left: { kind: "indicator", symbol: firstWithInd.symbol, indicator: ind.key },
            op: "<",
            right: { kind: "constant", value: 0 },
            modifier: null,
          }],
        });
      }
    }).catch((e) => setErr((e as Error).message));
  }, []);

  useEffect(() => {
    if (tab === "history" && !historyLoaded) loadHistory();
  }, [tab]);   // eslint-disable-line react-hooks/exhaustive-deps

  function loadHistory() {
    api.listBacktestRuns()
      .then(setHistory)
      .catch((e) => setErr((e as Error).message))
      .finally(() => setHistoryLoaded(true));
  }

  function buildDef(): StrategyDef {
    return {
      name, trade_symbol: tradeSymbol, buy,
      sell: sell.conditions.length ? sell : null,
      exit_rules: {
        hold_days:      exits.hold.on  ? exits.hold.v  : null,
        take_profit:    exits.tp.on    ? exits.tp.v    : null,
        stop_loss:      exits.sl.on    ? exits.sl.v    : null,
        trail_pct:      exits.trail.on ? exits.trail.v : null,
        trail_atr_mult: exits.atr.on   ? exits.atr.v   : null,
      },
      amount_pct: buyAmountPct,
      sell_amount_pct: sellAmountPct,
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
      return "매도 조건(매도 신호 또는 청산 규칙) 중 적어도 하나는 설정해야 합니다.";
    }
    return null;
  }

  async function runAnalysis() {
    setErr(""); setBusy("analysis"); setAnalysis(null);
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
    const sellErr = hasSellSetup();
    if (sellErr) { setErr(sellErr); return; }
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
      <h1 className="page-title">백테스트</h1>
      <p className="page-sub">
        조건을 문장으로 채우고 → 통계로 발견하고 → 과거 데이터로 검증하세요.
      </p>

      <div className="tabs">
        {([
          ["build",   "전략 구성"],
          ["result",  "결과 리포트"],
          ["history", "실행 내역"],
        ] as [TabKey, string][]).map(([k, label]) => (
          <button key={k} type="button"
                  className={"tab" + (tab === k ? " active" : "")}
                  onClick={() => setTab(k)}>
            {label}
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
          buyAmountPct={buyAmountPct} setBuyAmountPct={setBuyAmountPct}
          sellAmountPct={sellAmountPct} setSellAmountPct={setSellAmountPct}
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
        />
      )}

      {tab === "history" && (
        <HistoryTab
          rows={history}
          loaded={historyLoaded}
          onLoad={loadRun}
          onDelete={deleteRun}
        />
      )}
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
  exits: Record<RuleKey, { on: boolean; v: number }>;
  setRule: (k: RuleKey, p: Partial<{ on: boolean; v: number }>) => void;
  buyAmountPct: number; setBuyAmountPct: (v: number) => void;
  sellAmountPct: number; setSellAmountPct: (v: number) => void;
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
    buyAmountPct, setBuyAmountPct, sellAmountPct, setSellAmountPct,
    capital, setCapital, forwardDays, setForwardDays,
    busy, runAnalysis, runBacktest, analysis,
  } = props;

  if (symbols.length === 0) return <p className="muted">데이터 불러오는 중…</p>;

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
      <div className="panel">
        <h3>1. 매수 대상</h3>
        <div className="row">
          <div>
            <label>전략 이름</label>
            <input value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div>
            <label>매수할 종목</label>
            <div>
              <SymbolPicker
                symbols={symbols} value={tradeSymbol} tradableOnly
                onChange={setTradeSymbol}
              />
            </div>
          </div>
        </div>
      </div>

      <div className="panel">
        <h3>2. 매수 조건</h3>
        <p className="muted" style={{ margin: "0 0 12px" }}>
          지정한 조건이 충족되는 날 해당 종목을 매수합니다.
        </p>
        <ConditionBuilder symbols={symbols} group={buy} onChange={setBuy} />

        <div className="amount-row">
          <label>1회 매수액 (자본의 %)</label>
          <input type="number" min={1} max={100} value={buyAmountPct}
                 onChange={(e) => setBuyAmountPct(Number(e.target.value))} />
          <span className="muted">
            {wonReadable(capital * buyAmountPct / 100)} ({fmt2(buyAmountPct)}%)
          </span>
        </div>
      </div>

      <div className="panel">
        <h3>3. 매도 조건 <span className="muted">(매도 신호 또는 청산 규칙 중 하나 이상 필수)</span></h3>

        <div className="sub-h">3-1. 매도 신호</div>
        <p className="muted" style={{ margin: "0 0 8px" }}>
          지정한 조건이 충족되는 날 매도합니다 (선택).
        </p>
        <ConditionBuilder symbols={symbols} group={sell} onChange={setSell} />

        <div className="sub-h" style={{ marginTop: 18 }}>3-2. 청산 규칙</div>
        <p className="muted" style={{ margin: "0 0 10px" }}>
          켜진 규칙 중 먼저 트리거되는 것으로 청산합니다. 매도 신호와 함께 적용됩니다.
        </p>
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
              </label>
            );
          })}
        </div>

        <div className="amount-row" style={{ marginTop: 16 }}>
          <label>1회 매도액 (보유 수량의 %)</label>
          <input type="number" min={1} max={100} value={sellAmountPct}
                 onChange={(e) => setSellAmountPct(Number(e.target.value))} />
          <span className="muted">
            보유 수량의 {fmt2(sellAmountPct)}%{sellAmountPct >= 100 ? " (전량 청산)" : ""}
          </span>
        </div>
      </div>

      <div className="panel">
        <h3>4. 자금</h3>
        <div className="row">
          <div>
            <label>초기자본(원)</label>
            <input type="number" value={capital}
                   onChange={(e) => setCapital(Number(e.target.value))} />
            <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
              = {wonReadable(capital)}
            </div>
          </div>
        </div>
      </div>

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
            <button className="ghost sm" disabled={!!busy}
                    onClick={runAnalysis}>
              {busy === "analysis" ? "분석 중…" : "통계 미리보기"}
            </button>
          </span>
          <button disabled={!!busy} onClick={runBacktest}>
            {busy === "backtest" ? "실행 중…" : "백테스트 실행"}
          </button>
        </div>
      </div>

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

// ── 탭 2: 결과 리포트 ─────────────────────────────────────────────────────────

function ResultTab({ backtest, metrics, name, busy, onDraft, onApply, saveMsg }: {
  backtest: BacktestResult | null;
  metrics: Record<string, number | null> | undefined;
  name: string;
  busy: string;
  onDraft: () => void;
  onApply: () => void;
  saveMsg: string;
}) {
  if (!backtest) {
    return (
      <div className="panel empty-state">
        <div className="empty-title">아직 실행 결과가 없습니다</div>
        <p className="muted">
          [전략 구성] 탭에서 조건을 만들고 백테스트를 실행하세요.
        </p>
      </div>
    );
  }
  if (!backtest.success || !metrics) {
    return <div className="error">{backtest.error}</div>;
  }

  return (
    <div className="panel">
      <h3>'{name}' 백테스트 결과</h3>
      <Verdict metrics={metrics} />
      <div className="cards" style={{ marginBottom: 18 }}>
        <Stat label="총수익률" value={`${fmt2(metrics.total_return)}%`}
              hint="백테스트 전체 기간 동안 자산이 늘어난 비율입니다." />
        <Stat label="CAGR" value={`${fmt2(metrics.cagr)}%`}
              hint="복리로 환산한 연평균 수익률입니다." />
        <Stat label="MDD" value={`${fmt2(metrics.mdd)}%`}
              hint="고점 대비 자산이 가장 크게 떨어졌던 낙폭입니다." />
        <Stat label="샤프" value={fmt2(metrics.sharpe)}
              hint="변동성 한 단위당 거둔 수익을 나타내는 위험조정 수익 지표입니다." />
        <Stat label="승률" value={`${fmt2(metrics.win_rate)}%`}
              hint="전체 거래 중 이익으로 끝난 거래의 비율입니다." />
        <Stat label="거래 수" value={`${metrics.n_trades ?? 0}회`}
              hint="백테스트 기간 중 매수 후 청산이 완료된 횟수입니다." />
        <Stat label="vs Buy&Hold"
              value={`${fmt2(metrics.excess_return)}%p`}
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
              <th>진입일</th><th>청산일</th><th>보유일</th>
              <th>수익률(%)</th><th>청산사유</th>
            </tr>
          </thead>
          <tbody>
            {(backtest.trades ?? []).slice(0, 50).map((t, i) => (
              <tr key={i}>
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
        <button disabled={!!busy} onClick={onApply}>
          {busy === "apply" ? "적용 중…" : "내 전략에 적용 (모의투자)"}
        </button>
      </div>
      {saveMsg && <div className="ok">{saveMsg}</div>}
    </div>
  );
}

// ── 탭 3: 실행 내역 ───────────────────────────────────────────────────────────

function HistoryTab({ rows, loaded, onLoad, onDelete }: {
  rows: BacktestRunSummary[];
  loaded: boolean;
  onLoad: (id: number) => void;
  onDelete: (id: number) => void;
}) {
  if (!loaded) return <p className="muted">불러오는 중…</p>;
  if (rows.length === 0) {
    return (
      <div className="panel empty-state">
        <div className="empty-title">아직 실행한 백테스트가 없습니다</div>
        <p className="muted">
          [전략 구성] 탭에서 첫 백테스트를 실행해보세요. 결과는 자동으로 저장됩니다.
        </p>
      </div>
    );
  }
  return (
    <div className="panel">
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

function Stat({ label, value, hint }: {
  label: string; value: string; hint?: string;
}) {
  return (
    <div className="stat">
      <div className="label">
        {label}
        {hint && <span className="metric-hint" data-tip={hint}>?</span>}
      </div>
      <div className="value">{value}</div>
    </div>
  );
}
