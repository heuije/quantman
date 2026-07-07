/**
 * 내 전략 — 백테스트 연구 라이브러리(재설계 2차).
 *
 * 자동매매 연동 이전, 전략을 백테스트로 연구·검증하는 탭. 카드는 백테스트 성과
 * (총수익·CAGR·MDD·샤프)만 보여준다. 라이브 평가금액·손익·보유는 트레이딩 탭 전용
 * (역할 분리). 각 전략의 최신 백테스트를 병렬 fetch해 카드에 바인딩.
 */

import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import type { BacktestRunSummary, IrStrategyDef, StrategyDef, StrategyRow } from "../types";
import { parseScreenerKey, parseTradeSymbols } from "../types";

/** IR 유니버스를 한 줄로 요약. */
function summarizeIrUniverse(def: IrStrategyDef): string {
  const u = def.universe ?? { kind: "single" };
  const detail = u.screener ? " · 세부조건" : "";
  if (u.kind === "all") return `전체 종목${detail}`;
  const syms = u.symbols ?? [];
  if (syms.length === 0) return "(없음)";
  if (syms.length === 1) return `${syms[0]}${detail}`;
  return `${syms[0]} 외 ${syms.length - 1}종목${detail}`;
}

/** "005930 외 2종목" 형태로 다중 종목 축약. 단일이면 코드 그대로. */
function summarizeTargets(tradeSymbol: string): string {
  const { mode, symbols } = parseTradeSymbols(tradeSymbol);
  if (mode === "screener") return tradeSymbol;
  if (symbols.length === 0) return "(없음)";
  if (symbols.length === 1) return symbols[0];
  return `${symbols[0]} 외 ${symbols.length - 1}종목`;
}

type Filter = "all" | "paper" | "live" | "draft";
const FILTER_LABEL: Record<Filter, string> = {
  all: "전체", paper: "모의", live: "실전", draft: "초안",
};

// 백테스트 메트릭 스케일 — total_return·cagr·max_drawdown는 분수(→×100 표시), sharpe는 원값.
// (StrategyDetail 백테스트 내역과 동일 규약. ⚠ 라이브 win_rate가 이미 %였던 것과 반대.)
const btPct = (v: number | null | undefined) =>
  v == null ? "—" : (v >= 0 ? "+" : "") + (v * 100).toFixed(1) + "%";
const btCls = (v: number | null | undefined) => (v == null ? "" : v >= 0 ? "pos" : "neg");
const shortDate = (iso: string) => iso.slice(0, 10);

export default function Strategies() {
  const navigate = useNavigate();
  const [rows, setRows] = useState<StrategyRow[]>([]);
  const [btByStrategy, setBtByStrategy] = useState<Record<number, BacktestRunSummary | null>>({});
  const [loaded, setLoaded] = useState(false);
  const [err, setErr] = useState("");
  const [filter, setFilter] = useState<Filter>("all");

  function load() {
    setErr("");
    api.listStrategies()
      .then(async (rs) => {
        setRows(rs);
        // 전략별 최신 백테스트(병렬 · 요약 엔드포인트라 저비용). 실패한 전략은 null.
        const entries = await Promise.all(rs.map(async (s) => {
          try {
            const bts = await api.listStrategyBacktests(s.id);
            const latest = bts.slice().sort((a, b) => (a.created_at < b.created_at ? 1 : -1))[0];
            return [s.id, latest ?? null] as const;
          } catch {
            return [s.id, null] as const;
          }
        }));
        setBtByStrategy(Object.fromEntries(entries));
      })
      .catch((e) => setErr((e as Error).message))
      .finally(() => setLoaded(true));
  }
  // 데이터 패칭 effect — 사용자 트리거 fetch라 정적 규칙 비활성.
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(load, []);

  const filtered = useMemo(() => {
    if (filter === "all") return rows;
    return rows.filter((r) => r.run_mode === filter);
  }, [rows, filter]);

  const counts = useMemo(() => ({
    all: rows.length,
    paper: rows.filter((r) => r.run_mode === "paper").length,
    live: rows.filter((r) => r.run_mode === "live").length,
    draft: rows.filter((r) => r.run_mode === "draft").length,
  }), [rows]);

  return (
    <div>
      <h1 className="page-title">내 전략</h1>

      {err && <div className="error">{err}</div>}
      {!loaded && <p className="muted">불러오는 중…</p>}

      {loaded && rows.length === 0 && (
        <div className="panel empty-state">
          <div className="empty-title">아직 저장된 전략이 없습니다</div>
          <Link to="/lab"><button>전략 연구소로 이동</button></Link>
        </div>
      )}

      {loaded && rows.length > 0 && (
        <>
          <div className="filter-tabs">
            {(["all", "live", "paper", "draft"] as const).map((f) => (
              <button key={f} className={"filter-tab" + (filter === f ? " on" : "")}
                onClick={() => setFilter(f)}>
                {FILTER_LABEL[f]} <span className="count">{counts[f]}</span>
              </button>
            ))}
          </div>

          {filtered.length === 0 && (
            <div className="panel empty">{FILTER_LABEL[filter]} 모드에 전략이 없습니다</div>
          )}

          {filtered.length > 0 && (
            <div className="strategy-grid">
              {filtered.map((s) => (
                <StrategyCard key={s.id} strategy={s}
                  backtest={btByStrategy[s.id] ?? null}
                  onClick={() => navigate(`/strategies/${s.id}`)} />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function StrategyCard({ strategy: s, backtest, onClick }: {
  strategy: StrategyRow;
  backtest: BacktestRunSummary | null;
  onClick: () => void;
}) {
  const isIr = s.engine === "ir";
  let target: ReactNode;
  if (isIr) {
    target = <>{summarizeIrUniverse(s.definition as IrStrategyDef)}</>;
  } else {
    const od = s.definition as StrategyDef;
    const screenerKey = parseScreenerKey(od.trade_symbol);
    target = screenerKey
      ? <>자동 선택: <code>{screenerKey}</code></>
      : <>{summarizeTargets(od.trade_symbol)}</>;
  }
  const m = backtest?.metrics ?? null;
  const connected = s.run_mode === "live" || s.run_mode === "paper";

  return (
    <button className="strat-card" onClick={onClick}>
      <div className="strat-head">
        <span className="strat-name">{s.name}</span>
        {s.run_mode === "live" ? (
          <span className="strat-status connected"><i className="dot dot-green" />트레이딩 연동됨</span>
        ) : (
          <span className={"mode-badge " + (s.run_mode === "paper" ? "paper" : "draft")}>
            {s.run_mode === "paper" ? "모의" : "초안"}
          </span>
        )}
      </div>

      <div className="strat-target muted">🎯 {target}</div>

      {m ? (
        <div className="strat-bt">
          <div><span className="muted">총수익</span><b className={btCls(m.total_return)}>{btPct(m.total_return)}</b></div>
          <div><span className="muted">CAGR</span><b className={btCls(m.cagr)}>{btPct(m.cagr)}</b></div>
          <div><span className="muted">MDD</span><b className="neg">{btPct(m.max_drawdown)}</b></div>
          <div><span className="muted">샤프</span><b>{m.sharpe != null ? m.sharpe.toFixed(2) : "—"}</b></div>
        </div>
      ) : (
        <div className="strat-bt-empty muted">아직 백테스트를 실행하지 않았습니다</div>
      )}

      <div className="strat-foot muted">
        <span>
          {backtest?.start && backtest?.end
            ? `백테스트 ${shortDate(backtest.start)}~${shortDate(backtest.end)} · ` : ""}
          {backtest ? `실행 ${shortDate(backtest.created_at)}` : "백테스트 필요"}
        </span>
        <span className="strat-hint">
          {connected ? "실행 현황은 트레이딩 →" : s.account_ref ? "연동 대기" : "계좌 미연동"}
        </span>
      </div>
    </button>
  );
}
