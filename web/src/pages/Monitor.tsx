import { useEffect, useState } from "react";
import { api } from "../api";
import {
  ExecutionQuality, HealthCard, MarketBar, PortfolioRiskCard,
  PositionDetailCards, RiskBanner, StrategyCardGrid,
} from "../components/MonitorCards";
import { CsvExportBar } from "../components/MonitorTools";
import type {
  CommandRow, CommandType, DeviceRow, MarketContext, NextDayPreview,
  PortfolioRisk, SyncSnapshot,
} from "../types";

const REFRESH_MS = 5000;

export default function Monitor() {
  const [snap, setSnap] = useState<SyncSnapshot | null>(null);
  const [devices, setDevices] = useState<DeviceRow[]>([]);
  const [cmds, setCmds] = useState<CommandRow[]>([]);
  const [market, setMarket] = useState<MarketContext | null>(null);
  const [risk, setRisk] = useState<PortfolioRisk | null>(null);
  const [preview, setPreview] = useState<NextDayPreview | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);

  async function load() {
    try {
      const [s, ds, cs, mk] = await Promise.all([
        api.snapshot(), api.devices(), api.listCommands(undefined, false),
        api.marketContext().catch(() => null),
      ]);
      setSnap(s); setDevices(ds); setCmds(cs); setMarket(mk);
      setErr("");
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoaded(true);
    }
  }

  // 포트폴리오 위험은 비용이 좀 들어 30초 주기.
  // W-02 — 실패를 silent로 묻으면 카드가 빈 상태로 "위험 없음" 오인. 카드 내부에
  // 표시할 수 있도록 별도 에러 상태를 둔다. 30초 폴링이라 "직전 성공 + 마지막 실패"
  // 톤다운: setRisk(null)로 덮지 않고 riskErr만 set.
  const [riskErr, setRiskErr] = useState("");
  async function loadRisk() {
    try {
      const r = await api.portfolioRisk(60);
      setRisk(r);
      setRiskErr("");
    } catch (e) {
      setRiskErr((e as Error).message || "지표를 불러오지 못했습니다");
    }
  }

  // 매매 예정(NextDayPreview) — 30초 polling. 페어링 여부와 무관하게 fetch
  // (페어링 안 됐을 땐 server가 available=false 응답).
  function loadPreview() {
    api.getNextDayPreview().then(setPreview).catch(() => {});
  }

  // 데이터 패칭(폴링) 효과 — 의도적. (W-05: 정적 분석 규칙은 비활성.)
  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    load();
    loadRisk();
    loadPreview();
    const t = setInterval(load, REFRESH_MS);
    const t2 = setInterval(loadRisk, 30_000);
    const t3 = setInterval(loadPreview, 30_000);
    return () => { clearInterval(t); clearInterval(t2); clearInterval(t3); };
  }, []);
  /* eslint-enable react-hooks/set-state-in-effect */

  async function send(type: CommandType,
                      params: Record<string, string | number> = {}) {
    const dev = devices[0];
    if (!dev) { setErr("기기 페어링이 필요합니다."); return; }
    setBusy(true);
    try {
      await api.createCommand(dev.id, type, params);
      await load();
    } catch (e) {
      setErr((e as Error).message);
    } finally { setBusy(false); }
  }

  const paired = devices.length > 0;
  // Phase 42-3 — 페어링 해제 시 옛 snapshot(킬스위치·잔고·포지션 등)을
  // 노출하지 않는다. paired=false 분기에서 PairingOnboarding이 표시되므로
  // 본 데이터는 어차피 가려지지만, kill switch banner 같은 page-level
  // 영역까지 stale 데이터를 reflect하지 않도록 source에서 차단.
  const p = paired ? snap?.payload : undefined;
  const ks = p?.kill_switch;
  const summary = p?.cycle_summary;
  const slip = p?.slippage;
  const pending = p?.broker_pending ?? p?.pending_local ?? [];
  const orders = p?.recent_orders ?? [];
  const cycles = p?.recent_cycles ?? [];
  const positions = p?.positions ?? [];
  const equityNow = p?.balance?.total_eval;
  const actionDisabled = busy || !paired;
  const pairTooltip = paired ? undefined : "기기 페어링 후 활성화됩니다";

  return (
    <div>
      <h1 className="page-title">트레이딩</h1>
      <p className="page-sub">
        평일 <strong>08:55 KST</strong> 자동 사이클 —
        전일 종가 기준 평가 → <strong>09:00 시초가 동시호가</strong> 체결.
        잔고·포지션은 매 사이클 종료 시점 기준이며 장중 실시간 변동은 표시되지 않습니다.
      </p>

      {err && <div className="error">{err}</div>}
      {!loaded && <p className="muted">불러오는 중…</p>}

      {/* 시장 컨텍스트 */}
      <MarketBar ctx={market} />

      {/* 위험 한도 banner — usagePct>=80 또는 drawdown<=-10일 때만 표시 */}
      {paired && <RiskBanner ks={ks} dd={p?.drawdown} equityNow={equityNow} />}

      {/* Kill switch banner */}
      {ks?.active && (
        <div className="panel" style={{
          borderLeft: "4px solid var(--red)",
          background: "var(--red-soft)", marginBottom: 14,
        }}>
          <div style={{ display: "flex", alignItems: "center",
                          justifyContent: "space-between", gap: 12 }}>
            <div>
              <div style={{ fontWeight: 700, color: "var(--red)" }}>
                ⚠ Kill Switch 활성
              </div>
              <div className="muted" style={{ fontSize: 13, marginTop: 4 }}>
                사유: {ks.reason || "(없음)"} ·{" "}
                {ks.since ? new Date(ks.since).toLocaleString() : ""}
              </div>
            </div>
            <button
              className="ghost sm" disabled={busy}
              onClick={() => send("RESET_KILL_SWITCH")}
              style={{ borderColor: "var(--red)", color: "var(--red)" }}
            >
              해제
            </button>
          </div>
        </div>
      )}

      {/* 미국 실시간 시세 미신청 경고 — 장중 실시간 손절 미제공 */}
      {summary?.us_realtime_unavailable && (
        <div className="panel" style={{
          /* W-07 — amber 하드코딩 제거. 토큰만 사용(다른 ReconciliationPanel과 동일). */
          borderLeft: "4px solid var(--amber)",
          background: "var(--amber-soft)", marginBottom: 14,
        }}>
          <div style={{ fontWeight: 700, color: "var(--amber)" }}>
            ⚠ 미국 실시간 손절 미제공
          </div>
          <div className="muted" style={{ fontSize: 13, marginTop: 4, lineHeight: 1.6 }}>
            미국 해외 실시간 시세가 수신되지 않습니다. KIS HTS <b>[7781] 해외 실시간
            시세 신청</b> 전까지 미국 종목의 장중 실시간 손절·익절·트레일링이
            동작하지 않습니다(장 마감 후 사이클에서만 청산 평가). 국내 주식은 영향 없음.
          </div>
        </div>
      )}

      {/* Action bar */}
      <div className="panel" style={{ display: "flex", flexWrap: "wrap",
                                         gap: 8, alignItems: "center" }}>
        <strong style={{ marginRight: 8 }}>액션:</strong>
        <button className="ghost sm" onClick={() => send("PAUSE_AUTO")}
                disabled={actionDisabled} title={pairTooltip}>
          일시정지
        </button>
        <button className="ghost sm" onClick={() => send("RESUME_AUTO")}
                disabled={actionDisabled} title={pairTooltip}>
          재개
        </button>
        <button className="ghost sm" onClick={() => {
          if (confirm("정말 모든 보유 종목을 매도하고 신규 매수를 중지하시겠습니까?")) {
            send("LIQUIDATE_ALL");
          }
        }} disabled={actionDisabled} title={pairTooltip}
                style={{ color: "var(--red)" }}>
          전량 매도 후 매수 중지
        </button>
        <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 12 }}>
          {paired && <HealthCard snapAt={snap?.received_at}
                                  heartbeatAt={snap?.last_heartbeat_at}
                                  health={p?.health} />}
          <span className="muted" style={{ fontSize: 12 }}>
            {paired
              ? `대상 기기: ${devices[0].name} (#${devices[0].id})`
              : "기기 페어링 필요"}
          </span>
        </div>
      </div>

      {!paired ? (
        <PairingOnboarding />
      ) : (
        <>
          {/* 보유 종목 — 파이차트 + 표 + ledger/KIS 차이 통합 (Phase 40 reconciliation 흡수) */}
          <PositionDetailCards
            positions={positions}
            reconciliation={p?.reconciliation}
            onReconcile={() => send("RECONCILE_NOW")}
            reconcileDisabled={actionDisabled}
            reconcileTooltip={pairTooltip}
          />

      {/* 전략별 카드 그리드 — StrategyPnl + NextDayPreview + 보유 통합 (Step 4) */}
      <StrategyCardGrid
        pnl={p?.strategy_pnl}
        preview={preview}
        positions={positions}
      />

      {/* ── 하단: 사이클·실행 품질·미체결 등 부수 정보 ───────────────────── */}

      {/* 사이클 요약 */}
      {summary && (
        <div className="panel">
          <h3 style={{ marginTop: 0 }}>최근 사이클 요약</h3>
          <div style={{ display: "grid",
                          gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))",
                          gap: 12 }}>
            <Stat label="평가금액" value={summary.equity_post?.toLocaleString() ?? "—"} />
            <Stat label="매수" value={summary.n_bought ?? 0} />
            <Stat label="매도" value={summary.n_sold ?? 0} />
            <Stat label="갭 스킵" value={summary.n_skip_gap ?? 0} />
            <Stat label="신호 미충족" value={summary.n_skip_signal ?? 0} />
            <Stat label="거부" value={summary.n_rejected ?? 0} />
          </div>
        </div>
      )}

      {/* 실행 품질 — 시간대별 슬리피지 + 거부 사유 */}
      <ExecutionQuality
        buckets={p?.slippage_by_hour?.buckets}
        reasons={p?.rejection_reasons?.reasons} />

      {/* 슬리피지 요약 */}
      {slip && slip.n > 0 && (
        <div className="panel">
          <h3 style={{ marginTop: 0 }}>슬리피지 요약</h3>
          <p className="muted" style={{ fontSize: 13 }}>
            의도가 vs 체결가의 차이(bps). 양수 = 불리한 체결.
            표본 {slip.n}건 · 평균 {slip.avg_bps} bps · 중앙값 {slip.p50_bps} bps ·
            p95 {slip.p95_bps} bps · 최대 {slip.max_bps} bps
          </p>
        </div>
      )}

      {/* 포트폴리오 위험 — 상관관계 + 섹터. W-02 — 로드 실패 시 카드 내부에 표시. */}
      <PortfolioRiskCard risk={risk} err={riskErr} />

      {/* 로컬앱 상태는 페이지 상단 액션바 오른쪽 칩으로 이동 (Step 3).
          알림·위험 한도 설정 안내는 페이지 footer로 이동 (Step 5). */}

      {/* Pending */}
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>미체결 주문 ({pending.length})</h3>
        {pending.length === 0 ? (
          <p className="muted">현재 미체결 주문이 없습니다.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>방향</th><th>종목</th><th>수량</th><th>체결</th>
                <th>잔량</th><th>지정가</th><th>주문번호</th><th></th>
              </tr>
            </thead>
            <tbody>
              {pending.map((o) => (
                <tr key={o.order_no}>
                  <td>{o.side === "buy" ? "매수" : "매도"}</td>
                  <td>{o.symbol}{o.name ? ` · ${o.name}` : ""}</td>
                  <td>{o.qty}</td>
                  <td>{o.filled_qty ?? 0}</td>
                  <td>{o.remain_qty ?? o.qty}</td>
                  <td>{o.limit_price ? o.limit_price.toLocaleString() : "—"}</td>
                  <td style={{ fontFamily: "Consolas", fontSize: 12 }}>{o.order_no}</td>
                  <td>
                    <button
                      className="ghost sm" disabled={busy}
                      onClick={() => send("CANCEL_ORDER", {
                        order_no: o.order_no, symbol: o.symbol,
                        qty: o.remain_qty ?? o.qty,
                      })}
                    >
                      취소
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Recent orders + CSV export */}
      <div className="panel">
        <div style={{ display: "flex", justifyContent: "space-between",
                         alignItems: "center", marginBottom: 8 }}>
          <h3 style={{ margin: 0 }}>주문 내역 (최근 {orders.length}건)</h3>
          <CsvExportBar orders={orders} />
        </div>
        {orders.length === 0 ? (
          <p className="muted">아직 주문 이벤트가 없습니다.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>시각</th><th>상태</th><th>방향</th><th>종목</th>
                <th>수량</th><th>지정가</th><th>체결가</th><th>전략</th><th>사유</th>
              </tr>
            </thead>
            <tbody>
              {orders.slice(0, 30).map((o, i) => (
                <tr key={i}>
                  <td style={{ fontSize: 12 }}>
                    {o.ts.replace("T", " ").slice(0, 19)}
                  </td>
                  <td><EventBadge ev={o.event} /></td>
                  <td>{o.side === "buy" ? "매수" : "매도"}</td>
                  <td>{o.symbol}</td>
                  <td>{o.qty}</td>
                  <td>{o.limit_price?.toLocaleString() ?? "—"}</td>
                  <td>{o.fill_price?.toLocaleString() ?? "—"}</td>
                  <td>{o.strategy ?? ""}</td>
                  <td>{o.reason ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Recent cycles */}
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>사이클 로그 (최근 {cycles.length}건)</h3>
        {cycles.length === 0 ? (
          <p className="muted">아직 실행된 사이클이 없습니다.</p>
        ) : (
          cycles.slice(0, 10).map((c, i) => (
            <details key={i} style={{ marginBottom: 6 }}>
              <summary style={{ cursor: "pointer" }}>
                {c.ts.replace("T", " ").slice(0, 19)} —
                {" "}매수 {c.summary.n_bought ?? 0} ·
                {" "}매도 {c.summary.n_sold ?? 0} ·
                {" "}갭 {c.summary.n_skip_gap ?? 0} ·
                {" "}거부 {c.summary.n_rejected ?? 0}
                {c.summary.kill_switch ? " · ⚠ KS" : ""}
              </summary>
              <ul style={{ fontSize: 13, marginTop: 6 }}>
                {c.decisions.map((d, j) => (
                  <li key={j}>
                    <code>{d.action}</code> · {d.symbol || "-"} ·
                    {" "}{d.strategy_name} — {d.reason}
                  </li>
                ))}
              </ul>
            </details>
          ))
        )}
      </div>

      {/* Recent commands */}
      {cmds.length > 0 && (
        <div className="panel">
          <h3 style={{ marginTop: 0 }}>최근 명령</h3>
          <table>
            <thead>
              <tr><th>시각</th><th>타입</th><th>상태</th><th>결과</th></tr>
            </thead>
            <tbody>
              {cmds.slice(0, 10).map((c) => (
                <tr key={c.id}>
                  <td style={{ fontSize: 12 }}>
                    {c.created_at.replace("T", " ").slice(0, 19)}
                  </td>
                  <td><code>{c.type}</code></td>
                  <td>{c.status}</td>
                  <td style={{ fontSize: 12 }}>
                    {Object.keys(c.result).length > 0
                      ? JSON.stringify(c.result).slice(0, 80)
                      : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Footer — 설정 페이지 안내 (마지막 위치, mental model: 일상 사용엔 불필요) */}
      <div className="panel" style={{ display: "flex", alignItems: "center",
                                        justifyContent: "space-between", gap: 12 }}>
        <span className="muted">위험 한도(킬스위치)·알림 webhook은 설정에서 관리합니다.</span>
        <a href="/settings" className="link-btn">설정 → 알림·위험 한도</a>
      </div>
        </>
      )}
    </div>
  );
}

/** 페어링 안 됐을 때 — 빈 섹션 8개 대신 단일 onboarding 카드만 노출. */
function PairingOnboarding() {
  return (
    <div className="panel empty-state">
      <div className="empty-title">기기 페어링 후 사용 가능합니다</div>
      <p className="muted">
        로컬앱을 PC에 설치하고 이 계정과 페어링하면, 위험 한도·보유 종목·전략별
        P&L·실행 품질·헬스 등 모니터 섹션이 자동으로 활성화됩니다.
      </p>
      <a href="/devices" className="link-btn"
         style={{ display: "inline-block", marginTop: 8 }}>
        [기기 연결] 페이지로 이동 →
      </a>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
    </div>
  );
}

function EventBadge({ ev }: { ev: string }) {
  const colors: Record<string, { bg: string; fg: string }> = {
    filled: { bg: "var(--green-soft)", fg: "var(--green)" },
    partial: { bg: "var(--green-soft)", fg: "var(--green)" },
    submitted: { bg: "var(--accent-soft)", fg: "var(--accent)" },
    cancelled: { bg: "var(--border)", fg: "var(--muted)" },
    rejected: { bg: "var(--red-soft)", fg: "var(--red)" },
    timeout: { bg: "var(--amber-soft)", fg: "var(--amber)" },
  };
  const c = colors[ev] ?? colors.cancelled;
  return (
    <span className="badge" style={{ background: c.bg, color: c.fg,
                                       borderColor: "transparent" }}>
      {ev}
    </span>
  );
}
