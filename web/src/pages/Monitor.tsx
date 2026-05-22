import { useEffect, useState } from "react";
import { api } from "../api";
import { useMode } from "../mode";
import {
  ExecutionQuality, HealthCard, MarketBar, PortfolioRiskCard,
  PositionDetailCards, RiskGauges, StrategyPnl,
} from "../components/MonitorCards";
import {
  BacktestLiveOverlay, CsvExportBar,
} from "../components/MonitorTools";
import NextDayPreviewPanel from "../components/NextDayPreviewPanel";
import type {
  CommandRow, CommandType, DeviceRow, MarketContext, PortfolioRisk,
  ReconciliationResult, SyncSnapshot,
} from "../types";

const REFRESH_MS = 5000;

export default function Monitor() {
  const { mode, isLive } = useMode();
  const [snap, setSnap] = useState<SyncSnapshot | null>(null);
  const [devices, setDevices] = useState<DeviceRow[]>([]);
  const [cmds, setCmds] = useState<CommandRow[]>([]);
  const [market, setMarket] = useState<MarketContext | null>(null);
  const [risk, setRisk] = useState<PortfolioRisk | null>(null);
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

  // 포트폴리오 위험은 비용이 좀 들어 30초 주기
  async function loadRisk() {
    try {
      const r = await api.portfolioRisk(60);
      setRisk(r);
    } catch {/* ignore */}
  }

  useEffect(() => {
    load();
    loadRisk();
    const t = setInterval(load, REFRESH_MS);
    const t2 = setInterval(loadRisk, 30_000);
    return () => { clearInterval(t); clearInterval(t2); };
  }, []);

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
        {mode === "live" ? "실전" : "모의"} 계좌 · 평일 <strong>08:55 KST</strong> 자동 사이클 —
        전일 종가 기준 평가 → <strong>09:00 시초가 동시호가</strong> 체결.
        잔고·포지션은 매 사이클 종료 시점 기준이며 장중 실시간 변동은 표시되지 않습니다.
      </p>

      {err && <div className="error">{err}</div>}
      {!loaded && <p className="muted">불러오는 중…</p>}

      {/* 시장 컨텍스트 */}
      <MarketBar ctx={market} />

      {/* Phase 31 — 내일 매매 미리보기 (페어링 후에만 의미) */}
      {paired && <NextDayPreviewPanel />}

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

      {/* Action bar */}
      <div className="panel" style={{ display: "flex", flexWrap: "wrap",
                                         gap: 8, alignItems: "center" }}>
        <strong style={{ marginRight: 8 }}>액션:</strong>
        <button onClick={() => {
          if (confirm("자동매매는 평일 08:55 KST에 자동 실행됩니다.\n" +
                       "지금 수동 실행은 검증 목적으로만 사용하세요 — " +
                       "장중 실행 시 시초가가 아닌 현 시점 기준으로 발주됩니다.\n\n계속하시겠습니까?")) {
            send("RUN_CYCLE_NOW");
          }
        }} disabled={actionDisabled} title={pairTooltip}>
          지금 1회 실행 (검증용)
        </button>
        <button className="ghost sm" onClick={() => send("PAUSE_AUTO")}
                disabled={actionDisabled} title={pairTooltip}>
          일시정지
        </button>
        <button className="ghost sm" onClick={() => send("RESUME_AUTO")}
                disabled={actionDisabled} title={pairTooltip}>
          재개
        </button>
        <button className="ghost sm" onClick={() => {
          const msg = isLive
            ? "⚠ 실전 계좌입니다.\n실제 보유 종목을 모두 청산하고 신규 진입을 차단합니다.\n계속하시겠습니까?"
            : "정말 모든 보유 종목을 청산하고 신규 진입을 차단하시겠습니까?";
          if (confirm(msg)) send("LIQUIDATE_ALL");
        }} disabled={actionDisabled} title={pairTooltip}
                style={{ color: "var(--red)" }}>
          전량 청산 + 차단
        </button>
        <span className="muted" style={{ marginLeft: "auto", fontSize: 12 }}>
          {paired
            ? `대상 기기: ${devices[0].name} (#${devices[0].id})`
            : "기기 페어링 필요"}
        </span>
      </div>

      {!paired ? (
        <PairingOnboarding />
      ) : (
        <>
          {/* 위험 한도 게이지 + drawdown */}
          <RiskGauges ks={ks} dd={p?.drawdown} equityNow={equityNow} />

          {/* Phase 40 — 잔고 정합성 (KIS ↔ ledger) */}
          <ReconciliationPanel
            reconciliation={p?.reconciliation}
            onTrigger={() => send("RECONCILE_NOW")}
            disabled={actionDisabled}
            pairTooltip={pairTooltip}
          />

          {/* 보유 종목 디테일 카드 */}
          <PositionDetailCards positions={positions} />

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

      {/* 전략별 P&L */}
      <StrategyPnl data={p?.strategy_pnl} />

      {/* 백테스트 vs 라이브 overlay */}
      <BacktestLiveOverlay liveEquity={p?.equity} />

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

      {/* 포트폴리오 위험 — 상관관계 + 섹터 */}
      <PortfolioRiskCard risk={risk} />

      {/* 로컬앱 헬스 */}
      <HealthCard snapAt={snap?.received_at} health={p?.health} />

      {/* 알림·위험 한도 설정은 설정 페이지에서 일괄 관리 (중복 폼 제거) */}
      <div className="panel" style={{ display: "flex", alignItems: "center",
                                        justifyContent: "space-between", gap: 12 }}>
        <span className="muted">위험 한도(킬스위치)·알림 webhook은 설정에서 관리합니다.</span>
        <a href="/settings" className="link-btn">설정 → 알림·위험 한도</a>
      </div>

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
        </>
      )}
    </div>
  );
}

/** Phase 40 — KIS 잔고 ↔ ledger drift 점검 결과 패널. */
function ReconciliationPanel({
  reconciliation, onTrigger, disabled, pairTooltip,
}: {
  reconciliation?: ReconciliationResult;
  onTrigger: () => void;
  disabled: boolean;
  pairTooltip?: string;
}) {
  const r = reconciliation;
  const hasDrift = !!r?.has_drift;
  const orphans = r?.ledger_orphans ?? [];
  const extras = r?.external_extras ?? [];
  const applied = r?.applied ?? [];
  const checkedAt = r?.checked_at
    ? new Date(r.checked_at).toLocaleString("ko-KR", { hour12: false })
    : null;

  return (
    <div className="panel" style={hasDrift ? {
      borderLeft: "4px solid var(--amber)", background: "var(--amber-soft)",
    } : undefined}>
      <div style={{ display: "flex", alignItems: "center",
                      justifyContent: "space-between", gap: 12, marginBottom: 8 }}>
        <h3 style={{ margin: 0 }}>
          📋 잔고 정합성 {hasDrift && (
            <span style={{ color: "var(--amber)", fontSize: 14, fontWeight: 600 }}>
              · 차이 감지
            </span>
          )}
        </h3>
        <button className="ghost sm" onClick={onTrigger}
                disabled={disabled} title={pairTooltip}>
          지금 점검
        </button>
      </div>

      {!r ? (
        <p className="muted" style={{ fontSize: 13, margin: 0 }}>
          아직 점검 결과가 없습니다. 15:35 settlement 사이클 또는 "지금 점검" 버튼으로 실행하세요.
          <br />
          HTS·MTS에서 수동 매매한 경우 자동매매 ledger와 KIS 실 잔고가 어긋날 수 있어,
          이 패널이 차이를 자동 감지하고 정정합니다.
        </p>
      ) : r.error ? (
        <div className="error small">{r.error}</div>
      ) : (
        <>
          <div className="muted small" style={{ marginBottom: 8 }}>
            마지막 점검: {checkedAt} ·
            {" "}일치: {r.in_sync.length}종목 ·
            {" "}KIS 잔고: {r.kis_symbol_count}종목 ·
            {" "}ledger: {r.ledger_symbol_count}종목
          </div>
          {!hasDrift && applied.length === 0 && (
            <div className="muted small">✓ 모든 보유 종목이 일치합니다.</div>
          )}
          {applied.length > 0 && (
            <div style={{ marginTop: 8 }}>
              <strong className="small">자동 차감 {applied.length}건 (HTS/MTS 수동 매도 추정)</strong>
              <ul style={{ margin: "4px 0 0 16px", padding: 0, fontSize: 13 }}>
                {applied.map((a) => (
                  <li key={a.sid}>
                    <code>{a.symbol}</code> {a.old_qty}주 → {a.new_qty}주
                    {" "}(-{a.removed_qty})
                    {a.fully_closed && (
                      <span style={{ color: "var(--amber)", marginLeft: 6 }}>
                        [전량 청산]
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {orphans.length > 0 && orphans.length !== applied.length && (
            <div style={{ marginTop: 8 }}>
              <strong className="small">ledger 초과분 {orphans.length}종목</strong>
              <ul style={{ margin: "4px 0 0 16px", padding: 0, fontSize: 13 }}>
                {orphans.map((o) => (
                  <li key={o.symbol}>
                    <code>{o.symbol}</code> ledger {o.ledger_total_qty}주 vs KIS {o.kis_qty}주
                    {" "}(부족 {o.shortfall}주)
                  </li>
                ))}
              </ul>
            </div>
          )}
          {extras.length > 0 && (
            <div style={{ marginTop: 8 }}>
              <strong className="small">외부 매수 {extras.length}종목 (자동매매 미관여)</strong>
              <ul style={{ margin: "4px 0 0 16px", padding: 0, fontSize: 13 }}>
                {extras.map((e) => (
                  <li key={e.symbol}>
                    <code>{e.symbol}</code> 초과 {e.excess}주
                    {e.in_ledger ? " (자동매매 보유에 추가 매수)" : " (신규)"}
                  </li>
                ))}
              </ul>
              <div className="muted small" style={{ marginTop: 4 }}>
                외부 매수분은 ledger를 손대지 않습니다 — 자동매매가 매수한 게 아니므로
                자동 매도 대상이 아닙니다.
              </div>
            </div>
          )}
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
    cancelled: { bg: "#f3f4f6", fg: "var(--muted)" },
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
