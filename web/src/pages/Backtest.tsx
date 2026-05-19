import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import ConditionBuilder from "../components/ConditionBuilder";
import EquityChart from "../components/EquityChart";
import type {
  AnalysisResult, BacktestResult, ConditionGroup, StrategyDef, SymbolInfo,
} from "../types";

const fmt = (v: number | null | undefined, d = 1) =>
  v == null ? "-" : v.toLocaleString(undefined, { maximumFractionDigits: d });

export default function Backtest() {
  const [symbols, setSymbols] = useState<SymbolInfo[]>([]);
  const [name, setName] = useState("새 전략");
  const [tradeSymbol, setTradeSymbol] = useState("");
  const [buy, setBuy] = useState<ConditionGroup>({ conditions: [], logic: "AND" });
  const [holdDays, setHoldDays] = useState(5);
  const [stopLoss, setStopLoss] = useState(-5);
  const [takeProfit, setTakeProfit] = useState(10);
  const [amountPct, setAmountPct] = useState(100);
  const [capital, setCapital] = useState(10_000_000);
  const [forwardDays, setForwardDays] = useState(1);
  const [runMode, setRunMode] = useState("draft");

  const [analysis, setAnalysis] = useState<AnalysisResult | null>(null);
  const [backtest, setBacktest] = useState<BacktestResult | null>(null);
  const [busy, setBusy] = useState<"" | "analysis" | "backtest" | "save">("");
  const [err, setErr] = useState("");
  const [saveMsg, setSaveMsg] = useState("");

  const tradable = useMemo(
    () => symbols.filter((s) => s.tradable && s.indicators.length > 0),
    [symbols],
  );

  useEffect(() => {
    api.symbols().then((r) => {
      setSymbols(r.symbols);
      const first = r.symbols.find((s) => s.tradable && s.indicators.length);
      if (first) {
        setTradeSymbol(first.symbol);
        const ind = first.indicators.find(
          (i) => i.key.includes("pct_change") || i.key.includes("return"),
        ) ?? first.indicators[0];
        setBuy({
          logic: "AND",
          conditions: [{ symbol: first.symbol, indicator: ind.key,
                         op: "<", value: 0 }],
        });
      }
    }).catch((e) => setErr((e as Error).message));
  }, []);

  function buildDef(): StrategyDef {
    return {
      name, trade_symbol: tradeSymbol, buy,
      exit_rules: {
        hold_days: holdDays || null,
        stop_loss: stopLoss || null,
        take_profit: takeProfit || null,
      },
      amount_pct: amountPct,
    };
  }

  function targetIndicator() {
    const inds = symbols.find((s) => s.symbol === tradeSymbol)?.indicators ?? [];
    return inds.find((i) => i.key.includes("pct_change"))?.key
      ?? inds[0]?.key ?? "";
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
    setErr(""); setBusy("backtest"); setBacktest(null);
    try {
      const r = await api.runBacktest(buildDef(), capital);
      setBacktest(r);
    } catch (e) { setErr((e as Error).message); }
    finally { setBusy(""); }
  }

  async function save() {
    setErr(""); setSaveMsg(""); setBusy("save");
    try {
      await api.createStrategy(buildDef(), runMode);
      setSaveMsg(`'${name}' 전략을 저장했습니다 (${runMode}).`);
    } catch (e) { setErr((e as Error).message); }
    finally { setBusy(""); }
  }

  const m = backtest?.metrics;

  return (
    <div>
      <h1 className="page-title">백테스트</h1>
      <p className="page-sub">
        조건을 문장으로 채우고 → 통계로 발견하고 → 과거 데이터로 검증하세요.
      </p>

      {symbols.length === 0 && !err && <p className="muted">데이터 불러오는 중…</p>}

      {symbols.length > 0 && (
        <>
          <div className="panel">
            <h3>1. 매수 대상</h3>
            <div className="row">
              <div>
                <label>전략 이름</label>
                <input value={name} onChange={(e) => setName(e.target.value)} />
              </div>
              <div>
                <label>매수할 종목</label>
                <select value={tradeSymbol}
                        onChange={(e) => setTradeSymbol(e.target.value)}>
                  {tradable.map((s) => (
                    <option key={s.symbol} value={s.symbol}>{s.symbol}</option>
                  ))}
                </select>
              </div>
            </div>
          </div>

          <div className="panel">
            <h3>2. 매수 조건</h3>
            <ConditionBuilder symbols={symbols} group={buy} onChange={setBuy} />
          </div>

          <div className="panel">
            <h3>3. 청산 규칙 · 자금</h3>
            <div className="row">
              <div>
                <label>보유기간(일)</label>
                <input type="number" value={holdDays}
                       onChange={(e) => setHoldDays(Number(e.target.value))} />
              </div>
              <div>
                <label>익절(%)</label>
                <input type="number" value={takeProfit}
                       onChange={(e) => setTakeProfit(Number(e.target.value))} />
              </div>
              <div>
                <label>손절(%)</label>
                <input type="number" value={stopLoss}
                       onChange={(e) => setStopLoss(Number(e.target.value))} />
              </div>
              <div>
                <label>투입비율(%)</label>
                <input type="number" value={amountPct}
                       onChange={(e) => setAmountPct(Number(e.target.value))} />
              </div>
              <div>
                <label>초기자본(원)</label>
                <input type="number" value={capital}
                       onChange={(e) => setCapital(Number(e.target.value))} />
              </div>
            </div>
          </div>

          <div className="panel">
            <h3>4. 실행</h3>
            <div className="row">
              <div>
                <label>분석: 조건 발생 후 N일 뒤 수익률</label>
                <input type="number" value={forwardDays} min={1}
                       style={{ width: 90 }}
                       onChange={(e) => setForwardDays(Number(e.target.value))} />
              </div>
              <button className="ghost" disabled={!!busy} onClick={runAnalysis}>
                {busy === "analysis" ? "분석 중…" : "데이터 분석"}
              </button>
              <button disabled={!!busy} onClick={runBacktest}>
                {busy === "backtest" ? "실행 중…" : "백테스트 실행"}
              </button>
            </div>
            {err && <div className="error">{err}</div>}
          </div>

          {analysis && (
            <div className="panel">
              <h3>데이터 분석 결과</h3>
              {analysis.success ? (
                <div className="cards">
                  <Stat label="표본 수" value={`${analysis.n_samples}회`} />
                  <Stat label="양수 확률"
                        value={`${fmt(analysis.prob_positive)}%`}
                        tone={(analysis.prob_positive ?? 0) >= 50 ? "pos" : "neg"} />
                  <Stat label="평균 수익률" value={`${fmt(analysis.mean, 2)}%`}
                        tone={(analysis.mean ?? 0) >= 0 ? "pos" : "neg"} />
                  <Stat label="중앙값" value={`${fmt(analysis.median, 2)}%`} />
                  <Stat label="p-value" value={fmt(analysis.p_value, 3)} />
                </div>
              ) : (
                <div className="error">{analysis.error}</div>
              )}
            </div>
          )}

          {backtest && (
            <div className="panel">
              <h3>백테스트 결과</h3>
              {backtest.success && m ? (
                <>
                  <div className="cards" style={{ marginBottom: 18 }}>
                    <Stat label="총수익률" value={`${fmt(m.total_return)}%`}
                          tone={(m.total_return ?? 0) >= 0 ? "pos" : "neg"} />
                    <Stat label="CAGR" value={`${fmt(m.cagr)}%`} />
                    <Stat label="MDD" value={`${fmt(m.mdd)}%`} tone="neg" />
                    <Stat label="샤프" value={fmt(m.sharpe, 2)} />
                    <Stat label="승률" value={`${fmt(m.win_rate)}%`} />
                    <Stat label="거래 수" value={`${m.n_trades ?? 0}회`} />
                    <Stat label="vs Buy&Hold"
                          value={`${fmt(m.excess_return)}%p`}
                          tone={(m.excess_return ?? 0) >= 0 ? "pos" : "neg"} />
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
                            <td className={Number(t["수익률(%)"]) >= 0 ? "pos" : "neg"}>
                              {fmt(Number(t["수익률(%)"]), 2)}
                            </td>
                            <td>{t["청산사유"]}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </details>

                  <div className="spacer" />
                  <div className="row">
                    <div>
                      <label>저장 모드</label>
                      <select value={runMode}
                              onChange={(e) => setRunMode(e.target.value)}>
                        <option value="draft">초안 (draft)</option>
                        <option value="paper">모의투자 (paper)</option>
                      </select>
                    </div>
                    <button disabled={!!busy} onClick={save}>
                      {busy === "save" ? "저장 중…" : "전략으로 저장"}
                    </button>
                  </div>
                  {saveMsg && <div className="ok">{saveMsg}</div>}
                </>
              ) : (
                <div className="error">{backtest.error}</div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function Stat({ label, value, tone }: {
  label: string; value: string; tone?: "pos" | "neg";
}) {
  return (
    <div className="stat">
      <div className="label">{label}</div>
      <div className={"value" + (tone ? " " + tone : "")}>{value}</div>
    </div>
  );
}
