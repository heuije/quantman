/**
 * 개요 — 매일 첫 진입 페이지. 페이지 내부 모의/실전 토글에 따라 활성 전략을 필터.
 *
 * 6개 섹션: 액션 아이템 · 자산곡선 · 활성 전략 · 포트폴리오 · 최근 매매 · 시스템 상태.
 * 사용자가 가장 자주 보는 페이지라 5초 안에 "오늘 뭘 봐야 하나"를 답해야 한다.
 */

import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import EquityChart from "../components/EquityChart";
import type {
  DeviceRow, MarketContext, OrderEvent, StrategyRow, SyncSnapshot,
} from "../types";

const won = (v?: number | null) =>
  v == null ? "-" : v.toLocaleString() + "원";
const pct = (v?: number | null, digits = 2) =>
  v == null ? "-" : (v >= 0 ? "+" : "") + v.toFixed(digits) + "%";
const pnlSigned = (v?: number | null) =>
  v == null ? "-" : (v >= 0 ? "+" : "") + v.toLocaleString() + "원";

type Range = "1w" | "1m" | "3m" | "all";

interface ActionItem {
  kind: "danger" | "warn" | "info";
  msg: string;
  link?: string;
}

export default function Dashboard() {
  // 페이지 내부 모드 토글 — 활성 전략 필터링용. 전역 mode가 의미 불일치
  // (뷰 필터인데 매매 모드 스위치처럼 거짓 경고)였던 것을 제거하고 페이지로 옮겼다.
  const [mode, setMode] = useState<"paper" | "live">("paper");
  const [snap, setSnap] = useState<SyncSnapshot | null>(null);
  const [devices, setDevices] = useState<DeviceRow[]>([]);
  const [strategies, setStrategies] = useState<StrategyRow[]>([]);
  const [market, setMarket] = useState<MarketContext | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [range, setRange] = useState<Range>("1m");

  useEffect(() => {
    // 빠른 데이터(스냅샷·기기·전략, 보통 <120ms)로 페이지를 먼저 렌더한다.
    Promise.all([
      api.snapshot().catch(() => null),
      api.devices().catch(() => []),
      api.listStrategies().catch(() => []),
    ]).then(([s, d, st]) => {
      setSnap(s); setDevices(d); setStrategies(st);
      setLoaded(true);
    });
    // 시장 컨텍스트는 콜드 시 최대 ~80s까지 걸려 화면 전체를 막던 원인 → 분리.
    // 도착하면 자산곡선 아래 벤치마크(KOSPI) 행만 채운다(미도착 시 행 숨김).
    api.marketContext().catch(() => null).then(setMarket);
  }, []);

  if (!loaded) return <p className="muted">불러오는 중…</p>;

  const connected = devices.length > 0;
  const filteredStrategies = strategies.filter((s) =>
    mode === "live" ? s.run_mode === "live" : s.run_mode === "paper"
  );
  const otherModeCount = strategies.length - filteredStrategies.length;

  // Phase 42-3 — 페어링 해제 상태에서는 옛 snapshot(잔고·포지션·킬스위치 등)을
  // 표시하지 않는다. 페어링하지 않았는데 보유 종목·"킬스위치 정상" 라벨이 보이면
  // 사용자가 자기 계정·매매 상태를 오인할 수 있음.
  const effectiveSnap = connected ? snap : null;
  const payload = effectiveSnap?.payload ?? {};
  const bal = payload.balance;
  const positions = payload.positions ?? [];
  const equity = payload.equity ?? [];
  const recentOrders = payload.recent_orders ?? [];
  const recentCycles = payload.recent_cycles ?? [];
  const cycleSummary = payload.cycle_summary;
  const ks = payload.kill_switch;
  const slippage = payload.slippage;
  const strategyPnl = payload.strategy_pnl;
  const health = payload.health;
  const drawdown = payload.drawdown;
  const pendingLocal = payload.pending_local ?? [];
  const brokerPending = payload.broker_pending ?? [];

  // 액션 아이템 우선순위: danger → warn → info
  const actions: ActionItem[] = [];
  if (ks?.active) {
    actions.push({
      kind: "danger",
      msg: `킬스위치 활성 — ${ks.reason || "원인 불명"}`,
      link: "/monitor",
    });
  }
  const pendingN = pendingLocal.length + brokerPending.length;
  if (pendingN > 0) {
    actions.push({
      kind: "warn",
      msg: `미체결 ${pendingN}건 — 다음 사이클에 정리 예정`,
      link: "/monitor",
    });
  }
  if (drawdown && drawdown.depth_pct < -5) {
    actions.push({
      kind: "warn",
      msg: `현재 drawdown ${drawdown.depth_pct.toFixed(2)}%`,
      link: "/monitor",
    });
  }
  for (const w of health?.warnings ?? []) {
    actions.push({ kind: "warn", msg: w });
  }
  if (!connected) {
    actions.push({
      kind: "info",
      msg: "로컬앱이 연결되지 않았습니다",
      link: "/pair",
    });
  }
  if (filteredStrategies.length === 0) {
    actions.push({
      kind: "info",
      msg: mode === "live"
        ? "실전 전략이 없습니다"
        : "모의 전략을 만들어 시작하세요",
      link: "/backtest",
    });
  }

  const equitySliced = sliceEquity(equity, range);
  const kospi = market?.indicators.find(
    (i) => i.available && (i.label === "KOSPI" || i.label.includes("KOSPI"))
  );

  const todayPnl =
    cycleSummary?.equity_pre != null && cycleSummary?.equity_post != null
      ? cycleSummary.equity_post - cycleSummary.equity_pre
      : null;

  const strategyCards = filteredStrategies.slice(0, 4);
  const strategyPosCount = (name: string) =>
    positions.filter((p) => p.strategy_name === name).length;
  const strategyPnlFor = (name: string) =>
    strategyPnl?.by_strategy.find((r) => r.strategy === name);

  const topPositions = [...positions]
    .sort((a, b) => {
      const av = a.qty * (a.eval_price ?? a.avg_price ?? 0);
      const bv = b.qty * (b.eval_price ?? b.avg_price ?? 0);
      return bv - av;
    })
    .slice(0, 5);

  const recentFilled = recentOrders
    .filter((o) => o.event === "filled")
    .slice(0, 5);

  const reasonFor = (o: OrderEvent): string | null => {
    for (const c of recentCycles) {
      for (const d of c.decisions) {
        if (d.symbol === o.symbol &&
            (d.action === "buy" || d.action === "sell")) {
          return d.reason ?? null;
        }
      }
    }
    return o.reason ?? null;
  };

  return (
    <div className="dashboard">
      <div className="page-title-row">
        <div>
          <h1 className="page-title">개요</h1>
          <p className="page-sub">
            {mode === "live" ? "실전 계좌" : "모의 계좌"} · 오늘 한눈에 ·
            매일 평일 08:55 KST 자동 사이클 (시초가 동시호가 체결)
          </p>
          <div className="mode-filter" role="tablist" aria-label="활성 전략 모드 필터">
            <button
              role="tab"
              aria-selected={mode === "paper"}
              className={"mode-filter-btn" + (mode === "paper" ? " on" : "")}
              onClick={() => setMode("paper")}
            >
              모의
            </button>
            <button
              role="tab"
              aria-selected={mode === "live"}
              className={"mode-filter-btn" + (mode === "live" ? " on" : "")}
              onClick={() => setMode("live")}
            >
              실전
            </button>
          </div>
        </div>
        <div className="kpi-strip">
          {bal ? (
            <>
              <KpiBox label="총 평가" value={won(bal.total_eval)} />
              <KpiBox label="가용 현금" value={won(bal.cash)} />
              <KpiBox
                label="오늘 P&L"
                value={pnlSigned(todayPnl)}
                valueClass={
                  todayPnl == null ? "" : todayPnl >= 0 ? "pos" : "neg"
                }
              />
            </>
          ) : (
            // Phase 50 — 빈 KPI 카드 "-" 3개는 "왜 비어있지?" 혼란만 줌.
            // 모의 계좌 연결 안 됐다는 사실 + CTA로 대체.
            <div className="kpi-empty">
              <span className="kpi-empty-msg">
                {mode === "live" ? "실전" : "모의"} 계좌 데이터가 아직 없습니다
              </span>
              <Link to="/settings" className="kpi-empty-cta">
                로컬앱 연결 →
              </Link>
            </div>
          )}
        </div>
      </div>

      {/* 1. 액션 아이템 */}
      <section className="panel action-panel">
        <h3>오늘의 액션 아이템</h3>
        {actions.length === 0 ? (
          <div className="empty">모두 정상 — 처리할 알림이 없습니다</div>
        ) : (
          <ul className="action-list">
            {actions.map((a, i) => (
              <li key={i} className={"action-item " + a.kind}>
                <span className="action-dot" />
                <span className="action-msg">{a.msg}</span>
                {a.link && (
                  <Link to={a.link} className="action-link" aria-label="이동">
                    →
                  </Link>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* 2. 자산곡선 — Phase 50: KOSPI 벤치마크를 panel-header h3 옆으로 (떠 있던 bench-row 제거),
          빈 데이터 시 range-toggle disabled (의미 없는 클릭 차단). */}
      <section className="panel">
        <div className="panel-header">
          <h3 className="panel-header-title">
            자산곡선
            {kospi && (
              <span className="bench-inline">
                <span className="bench-inline-label">vs {kospi.label}</span>
                <span className="bench-inline-value">
                  {kospi.value?.toLocaleString() ?? "-"}
                </span>
                <span
                  className={
                    "bench-inline-pct " +
                    ((kospi.change_pct ?? 0) >= 0 ? "pos" : "neg")
                  }
                >
                  {pct(kospi.change_pct)}
                </span>
              </span>
            )}
          </h3>
          <div className="range-toggle">
            {(["1w", "1m", "3m", "all"] as const).map((r) => (
              <button
                key={r}
                className={range === r ? "on" : ""}
                disabled={equity.length === 0}
                onClick={() => setRange(r)}
              >
                {labelRange(r)}
              </button>
            ))}
          </div>
        </div>
        {equitySliced.length === 0 ? (
          <div className="empty">
            자산곡선 데이터 없음 — 자동매매 가동 후 표시됩니다
          </div>
        ) : (
          <EquityChart equity={equitySliced} />
        )}
      </section>

      <div className="dashboard-grid-2">
        {/* 3. 활성 전략 */}
        <section className="panel">
          <div className="panel-header">
            <h3>활성 전략 ({filteredStrategies.length})</h3>
            <Link to="/strategies" className="panel-more">
              전체 보기 →
            </Link>
          </div>
          {strategyCards.length === 0 ? (
            <div className="empty">
              <div>
                {mode === "live"
                  ? "실전 전략이 없습니다"
                  : "모의 전략을 만들어 시작하세요"}
              </div>
              <Link to="/backtest" className="cta">
                전략 만들기 →
              </Link>
            </div>
          ) : (
            <ul className="strategy-mini-list">
              {strategyCards.map((s) => {
                const pnl = strategyPnlFor(s.name);
                const posN = strategyPosCount(s.name);
                return (
                  <li key={s.id} className="strategy-mini">
                    <div className="sm-line1">
                      <span className="sm-name">{s.name}</span>
                      <span className={"sm-badge " + s.run_mode}>
                        {s.run_mode === "live" ? "실전" : "모의"}
                      </span>
                    </div>
                    <div className="sm-line2">
                      <span
                        className={
                          "sm-pnl " +
                          (pnl?.pnl == null
                            ? ""
                            : pnl.pnl >= 0
                            ? "pos"
                            : "neg")
                        }
                      >
                        {pnl ? pnlSigned(pnl.pnl) : "-"}
                      </span>
                      <span className="sm-pos">
                        {posN > 0 ? `${posN}종목 보유` : "보유 없음"}
                      </span>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
          {otherModeCount > 0 && (
            <div className="other-mode-hint muted">
              {mode === "live" ? "모의" : "실전"} 모드에 전략 {otherModeCount}개 더 있음
            </div>
          )}
        </section>

        {/* 4. 포트폴리오 */}
        <section className="panel">
          <div className="panel-header">
            <h3>포트폴리오 ({positions.length})</h3>
            <Link to="/monitor" className="panel-more">
              전체 보기 →
            </Link>
          </div>
          {topPositions.length === 0 ? (
            <div className="empty">현재 보유 종목 없음</div>
          ) : (
            <ul className="position-mini-list">
              {topPositions.map((p) => (
                <li key={p.symbol} className="position-mini">
                  <div className="pm-name">{p.name ?? p.symbol}</div>
                  <div
                    className={
                      "pm-return " +
                      ((p.cur_return_pct ?? 0) >= 0 ? "pos" : "neg")
                    }
                  >
                    {pct(p.cur_return_pct)}
                  </div>
                  <div className="pm-qty muted">
                    {p.qty.toLocaleString()}주
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>

      {/* 5. 최근 매매 + 근거 */}
      <section className="panel">
        <div className="panel-header">
          <h3>최근 매매</h3>
          <Link to="/monitor" className="panel-more">
            전체 보기 →
          </Link>
        </div>
        {recentFilled.length === 0 ? (
          <div className="empty">최근 매매 내역 없음</div>
        ) : (
          <ul className="trade-list">
            {recentFilled.map((o, i) => {
              const reason = reasonFor(o);
              return (
                <li key={i} className="trade-row">
                  <div className="trade-head">
                    <span className={"trade-side " + o.side}>
                      {o.side === "buy" ? "매수" : "매도"}
                    </span>
                    <span className="trade-symbol">{o.symbol}</span>
                    <span className="trade-qty muted">{o.qty}주</span>
                    <span className="trade-price">
                      @ {(o.fill_price ?? 0).toLocaleString()}원
                    </span>
                    <span className="trade-ts muted">{formatTs(o.ts)}</span>
                  </div>
                  <div className="trade-reason">
                    근거:{" "}
                    <span className="muted">{reason ?? "기록 없음"}</span>
                    {o.strategy && (
                      <span className="trade-strat muted"> · {o.strategy}</span>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      {/* 6. 시스템 상태 */}
      <section className="panel">
        <div className="panel-header">
          <h3>시스템 상태</h3>
          <Link to="/monitor" className="panel-more">
            상세 →
          </Link>
        </div>
        <div className="system-grid">
          <SysRow
            label="로컬앱 연결"
            value={connected ? "연결됨" : "미연결"}
            status={connected ? "ok" : "bad"}
            link={!connected ? "/pair" : undefined}
          />
          <SysRow
            label="마지막 사이클"
            value={
              health?.last_cycle_ts ? formatTs(health.last_cycle_ts) : "-"
            }
          />
          <SysRow
            label="KIS 토큰"
            value={
              health?.kis_token_expires_at
                ? `만료 ${formatTs(health.kis_token_expires_at)}`
                : "-"
            }
          />
          <SysRow
            label="킬스위치"
            value={!connected ? "—" : (ks?.active ? `활성 (${ks.reason})` : "정상")}
            status={!connected ? undefined : (ks?.active ? "bad" : "ok")}
          />
          <SysRow
            label="평균 슬리피지"
            value={
              slippage && slippage.n > 0
                ? `${(slippage.avg_bps ?? 0).toFixed(1)} bps (n=${slippage.n})`
                : "-"
            }
          />
          <SysRow
            label="오늘 사이클"
            value={
              cycleSummary
                ? `매수 ${cycleSummary.n_bought ?? 0} · 매도 ${
                    cycleSummary.n_sold ?? 0
                  }`
                : "-"
            }
          />
        </div>
      </section>
    </div>
  );
}

function KpiBox({
  label, value, valueClass,
}: {
  label: string; value: string; valueClass?: string;
}) {
  return (
    <div className="kpi-box">
      <div className="kpi-label">{label}</div>
      <div className={"kpi-value " + (valueClass ?? "")}>{value}</div>
    </div>
  );
}

function SysRow({
  label, value, status, link,
}: {
  label: string; value: string;
  status?: "ok" | "bad"; link?: string;
}) {
  return (
    <div className="sys-row">
      <span className="sys-label">{label}</span>
      <span className="sys-value">
        {status && <span className={"sys-dot " + status} />}
        {value}
        {link && (
          <Link to={link} className="sys-link" aria-label="이동">
            →
          </Link>
        )}
      </span>
    </div>
  );
}

function labelRange(r: Range) {
  return r === "1w" ? "1주" : r === "1m" ? "1개월"
    : r === "3m" ? "3개월" : "전체";
}

function sliceEquity(
  equity: { date: string; value: number | null }[],
  range: Range,
) {
  if (range === "all" || equity.length === 0) return equity;
  const days = range === "1w" ? 7 : range === "1m" ? 30 : 90;
  const cutoff = new Date();
  cutoff.setDate(cutoff.getDate() - days);
  return equity.filter((e) => new Date(e.date) >= cutoff);
}

function formatTs(ts: string) {
  try {
    const d = new Date(ts);
    const now = new Date();
    const diffMs = now.getTime() - d.getTime();
    const diffMin = Math.floor(diffMs / 60000);
    if (diffMin < 0) {
      const future = -diffMin;
      if (future < 60) return `${future}분 후`;
      const hr = Math.floor(future / 60);
      if (hr < 24) return `${hr}시간 후`;
      return `${Math.floor(hr / 24)}일 후`;
    }
    if (diffMin < 1) return "방금";
    if (diffMin < 60) return `${diffMin}분 전`;
    const diffHr = Math.floor(diffMin / 60);
    if (diffHr < 24) return `${diffHr}시간 전`;
    return d.toLocaleString("ko-KR", {
      month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit",
    });
  } catch {
    return ts;
  }
}
