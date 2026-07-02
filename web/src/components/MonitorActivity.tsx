/* 트레이딩 오늘 활동(넷팅 강조) + 전략별 성과 기여 — 재설계 §3 ③④.
 * 전부 기존 SyncSnapshot.payload 필드만 소비. 색은 DESIGN 토큰(방향 빨강/파랑·골드). */
import type {
  CycleRow, CycleSummary, NextDayPreview, PositionRich, StrategyPnlSummary,
} from "../types";

// 결정 action → 방향(TradeOutcomes와 동일 규약: bought/buy… = 매수, sold/sell… = 청산).
const sideOf = (action: string): "buy" | "sell" | null =>
  action.includes("buy") || action === "bought" ? "buy"
    : action.includes("sell") || action === "sold" ? "sell" : null;

export function TodayActivity({ cycleSummary, decisions }: {
  cycleSummary?: CycleSummary;
  decisions?: CycleRow["decisions"];
}) {
  const nBought = cycleSummary?.n_bought ?? 0;
  const nSold = cycleSummary?.n_sold ?? 0;
  const nNetted = cycleSummary?.n_netted ?? 0;
  const saved = cycleSummary?.commission_saved_krw ?? null;
  const trades = (decisions ?? []).filter((d) => sideOf(d.action) != null);

  return (
    <section className="panel">
      <div className="section-head">
        오늘 활동 <span className="muted">· 매수 {nBought} · 매도 {nSold}</span>
      </div>

      {/* 넷팅 하이라이트 — 값이 있고 실제 상쇄가 발생했을 때만(없으면 조용히 생략). */}
      {nNetted > 0 && saved != null && (
        <div className="netting-card">
          <div className="netting-icn">💡</div>
          <div>
            <div className="netting-title">
              넷팅 <b>{nNetted}건</b> · 수수료 약{" "}
              <b className="gold-text">{Math.round(saved).toLocaleString()}원</b> 절약
            </div>
            <div className="netting-desc muted">
              반대 방향 주문(청산↔진입)을 상쇄해 불필요한 왕복 수수료를 자동으로 아꼈어요.
            </div>
          </div>
        </div>
      )}

      {trades.length === 0 ? (
        <div className="muted small activity-empty">오늘 실행된 매매가 없습니다.</div>
      ) : (
        <ul className="activity-list">
          {trades.map((d, i) => {
            const side = sideOf(d.action);
            return (
              <li key={`${d.symbol}-${d.action}-${i}`}>
                <span className={"trade-tag " + (side === "buy" ? "buy" : "sell")}>
                  {side === "buy" ? "매수" : "청산"}
                </span>
                {d.strategy_name ? <span className="muted"> [{d.strategy_name}]</span> : null}{" "}
                {d.symbol}
                {d.reason
                  ? <span className="muted" title={d.reason}> — {d.reason}</span>
                  : null}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

// ── ④ 전략별 성과 기여 — 라이브 실행 손익(KRW)·승률·보유·다음 예정매매 ─────────
export function StrategyContribution({
  strategyPnl, positions, nextDayPreview, decisions,
}: {
  strategyPnl?: StrategyPnlSummary;
  positions?: PositionRich[];
  nextDayPreview?: NextDayPreview | null;
  decisions?: CycleRow["decisions"];
}) {
  const rows = (strategyPnl?.by_strategy ?? []).slice().sort((a, b) => b.pnl - a.pnl);
  if (rows.length === 0) return null;

  const ranToday = new Set((decisions ?? []).map((d) => d.strategy_name).filter(Boolean));
  const pos = positions ?? [];
  const preview = nextDayPreview?.by_strategy ?? [];

  const dc = (v: number) => (v >= 0 ? "pos" : "neg");
  const sPct = (v: number) => (v >= 0 ? "+" : "") + v.toFixed(2) + "%";
  const sWon = (v: number) => (v >= 0 ? "+" : "") + v.toLocaleString() + "원";

  return (
    <section>
      <div className="section-head out">전략별 성과 <span className="muted">· 기여 큰 순</span></div>
      <div className="contrib-grid">
        {rows.map((r) => {
          const held = pos.filter((p) => p.strategy_name === r.strategy);
          const avgRet = held.length
            ? held.reduce((s, p) => s + (p.cur_return_pct ?? 0), 0) / held.length : null;
          const pv = preview.find((p) => p.strategy_name === r.strategy);
          const plan = !pv ? null
            : !pv.signal_passed ? "신호 미충족"
              : pv.candidates.length > 0 ? `매수 후보 ${pv.candidates.length}종목`
                : "예정 매수 없음";
          return (
            <div key={r.strategy} className="panel contrib-card">
              <div className="contrib-head">
                <span className="contrib-name">{r.strategy}</span>
                {ranToday.has(r.strategy) && (
                  <span className="contrib-ran ok-green">오늘 실행됨 ✓</span>
                )}
              </div>
              <div className="contrib-pnl">
                <div><span className="muted">오늘</span><b className={dc(r.today_pnl)}>{sWon(r.today_pnl)}</b></div>
                <div><span className="muted">30일</span><b className={dc(r.month_pnl)}>{sWon(r.month_pnl)}</b></div>
                <div><span className="muted">누적</span><b className={dc(r.pnl)}>{sWon(r.pnl)}</b></div>
                <div><span className="muted">승률</span><b>{r.win_rate.toFixed(0)}%</b></div>
              </div>
              <div className="contrib-foot muted">
                보유 {held.length}종목
                {avgRet != null && <> · 평균 <b className={dc(avgRet)}>{sPct(avgRet)}</b></>}
                {plan && <> · 예정: {plan}</>}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}
