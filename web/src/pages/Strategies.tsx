/**
 * 내 전략 — 통합 카드뷰 + 필터 + 상세 + 실전 승격 모달.
 *
 * 모의↔실전을 별도 페이지로 나누는 대신 한 페이지의 카드 그리드 + 배지로 통합.
 * 헤더 모의/실전 토글과 필터가 동기화돼 사용자가 같은 정신모델로 이동.
 *
 * 승격 모달은 사용자가 진짜 돈을 거는 순간 — 가장 정성스럽게 만들어야 한다.
 * 모의 성과 요약 + 자본 비중 입력 + 명시적 확인의 3단계.
 */

import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import type { StrategyRow, SyncSnapshot } from "../types";
import { parseScreenerKey, parseTradeSymbols } from "../types";

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

const pnl = (v?: number | null) =>
  v == null ? "-" : (v >= 0 ? "+" : "") + v.toLocaleString() + "원";
const pct = (v?: number | null) =>
  v == null ? "-" : (v >= 0 ? "+" : "") + v.toFixed(2) + "%";

export default function Strategies() {
  const navigate = useNavigate();
  const [rows, setRows] = useState<StrategyRow[]>([]);
  const [snap, setSnap] = useState<SyncSnapshot | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [err, setErr] = useState("");
  const [filter, setFilter] = useState<Filter>("paper");

  function load() {
    setErr("");
    Promise.all([
      api.listStrategies(),
      api.snapshot().catch(() => null),
    ])
      .then(([rs, s]) => { setRows(rs); setSnap(s); })
      .catch((e) => setErr((e as Error).message))
      .finally(() => setLoaded(true));
  }
  // 데이터 패칭은 의도적 effect — react-hooks/set-state-in-effect는 적절한 dependencies로
  // 해소되지 않는 사용자 트리거 fetch에 대해선 disable.
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

  const strategyPnl = snap?.payload.strategy_pnl;
  const positions = snap?.payload.positions ?? [];

  return (
    <div>
      <h1 className="page-title">내 전략</h1>
      <p className="page-sub">
        전략을 모의로 검증하고, 충분히 안정되면 실전으로 승격하세요.
      </p>

      {err && <div className="error">{err}</div>}
      {!loaded && <p className="muted">불러오는 중…</p>}

      {loaded && rows.length === 0 && (
        <div className="panel empty-state">
          <div className="empty-title">아직 저장된 전략이 없습니다</div>
          <p className="muted">
            전략 만들기에서 매수·매도 조건을 짜고 백테스트로 검증한 뒤 저장하세요.
            저장한 전략을 모의로 두면 로컬앱이 매일 09:00 자동 실행합니다.
          </p>
          <Link to="/backtest"><button>전략 만들기로 이동</button></Link>
        </div>
      )}

      {loaded && rows.length > 0 && (
        <>
          {/* 필터 탭 */}
          <div className="filter-tabs">
            {(["all", "paper", "live", "draft"] as const).map((f) => (
              <button
                key={f}
                className={"filter-tab" + (filter === f ? " on" : "")}
                onClick={() => setFilter(f)}
              >
                {FILTER_LABEL[f]} <span className="count">{counts[f]}</span>
              </button>
            ))}
          </div>

          {filtered.length === 0 && (
            <div className="panel empty">
              {FILTER_LABEL[filter]} 모드에 전략이 없습니다
            </div>
          )}

          {/* 카드 그리드 */}
          {filtered.length > 0 && (
            <div className="strategy-grid">
              {filtered.map((s) => (
                <StrategyCard
                  key={s.id}
                  strategy={s}
                  pnl={strategyPnl?.by_strategy.find(r => r.strategy === s.name)}
                  positionCount={positions.filter(p => p.strategy_name === s.name).length}
                  onClick={() => navigate(`/strategies/${s.id}`)}
                />
              ))}
            </div>
          )}
        </>
      )}

      {/* 카드 클릭 → /strategies/:id 상세 페이지로 navigate.
          (Phase 59: 사이드 슬라이드 DetailPanel + PromoteModal은 detail로 이전) */}
    </div>
  );
}

function StrategyCard({
  strategy: s, pnl: row, positionCount, onClick,
}: {
  strategy: StrategyRow;
  pnl?: { pnl: number; today_pnl: number; trades: number; win_rate: number };
  positionCount: number;
  onClick: () => void;
}) {
  const buyN = s.definition.buy?.conditions?.length ?? 0;
  // Phase 32 — sell_rules 우선, legacy sell fallback
  const sellExtraN = s.definition.sell_rules?.conditions?.length
    ?? s.definition.sell?.conditions?.length ?? 0;
  const sr = s.definition.sell_rules ?? {};
  const sellRuleCount = [sr.take_profit, sr.stop_loss, sr.trail_pct,
                          sr.trail_atr_mult, sr.hold_days]
    .filter((v) => v != null).length + sellExtraN;
  const screenerKey = parseScreenerKey(s.definition.trade_symbol);

  return (
    <button className="strategy-card" onClick={onClick}>
      <div className="sc-head">
        <span className="sc-name">{s.name}</span>
        <span className={"sc-badge " + s.run_mode}>
          {s.run_mode === "live" ? "실전"
            : s.run_mode === "paper" ? "모의"
            : "초안"}
        </span>
      </div>
      <div className="sc-target">
        {screenerKey
          ? <>자동 선택: <code>{screenerKey}</code></>
          : <>{summarizeTargets(s.definition.trade_symbol)}</>}
      </div>
      <div className="sc-meta">
        매수 {buyN} · 매도 {sellRuleCount} 규칙 · 자본 {s.definition.amount_pct}%
      </div>
      <div className="sc-stats">
        <div className="sc-stat">
          <span className="sc-stat-label">누적 P&L</span>
          <span className={"sc-stat-value " +
            (row?.pnl == null ? "" : row.pnl >= 0 ? "pos" : "neg")}>
            {row ? pnl(row.pnl) : "-"}
          </span>
        </div>
        <div className="sc-stat">
          <span className="sc-stat-label">보유</span>
          <span className="sc-stat-value">
            {positionCount > 0 ? `${positionCount}종목` : "없음"}
          </span>
        </div>
        <div className="sc-stat">
          <span className="sc-stat-label">승률</span>
          <span className="sc-stat-value">
            {row?.win_rate != null ? pct(row.win_rate * 100) : "-"}
          </span>
        </div>
      </div>
    </button>
  );
}

