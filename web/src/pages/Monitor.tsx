import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { MarketBar, PositionDetailCards } from "../components/MonitorCards";
import { PerformanceHero, StatusStrip } from "../components/MonitorHero";
import { StrategyContribution, TodayActivity } from "../components/MonitorActivity";
import { CsvExportBar } from "../components/MonitorTools";
import TradeOutcomes from "../components/TradeOutcomes";
import TradingTimeline from "../components/TradingTimeline";
import type {
  CommandRow, CommandType, DeviceRow, MarketContext, NextDayPreview,
  SyncSnapshot,
} from "../types";

// Audit P0-1 — 5s → 15s. Cycle은 분~시간 단위라 5s 폴링은 over-frequent.
// 명령 발사 직후엔 await load()로 즉시 갱신되므로 사용자 체감 차이 없음.
// ETag/304 결합으로 egress·DB 부담 60~80% 감소 예상.
const REFRESH_MS = 15000;

// ── 명령 결과 사람용 표현 (투명성 T1 — 유저가 성공/실패/거부를 즉시 안다) ──────────
function labelForCommand(t: CommandType): string {
  switch (t) {
    case "LIQUIDATE_ALL": return "전량 매도 후 중지";
    case "PAUSE_AUTO": return "일시정지";
    case "RESUME_AUTO": return "재개";
    case "RESET_KILL_SWITCH": return "킬스위치 해제";
    case "RECONCILE_NOW": return "정합성 점검";
    case "RUN_CYCLE_NOW": return "지금 실행";
    case "CANCEL_ORDER": return "주문 취소";
    default: return t;
  }
}

// 로컬앱 ack 결과(status·result) → 배너 텍스트 + ok(성공 green·문제 red·대기 null).
// 로컬앱이 사람용 message를 준 경우(비상청산: 거부 사유·미청산 안내 포함) 그대로 쓴다.
function describeCommandResult(cmd: CommandRow): { text: string; ok: boolean | null } {
  const label = labelForCommand(cmd.type);
  const r = (cmd.result ?? {}) as Record<string, unknown>;
  if (cmd.status === "failed" || typeof r.error === "string") {
    return { text: `${label} 실패 — ${(r.error as string) || "로컬앱 오류"}`, ok: false };
  }
  if (typeof r.message === "string" && r.message) {
    return { text: r.message, ok: r.ok === undefined ? true : Boolean(r.ok) };
  }
  return { text: `${label} 완료`, ok: true };
}

// 명령 상태 배지 (T5 — 전송/실행/완료/실패를 유저가 추적).
function cmdStatusMeta(s: CommandRow["status"]): { text: string; color: string } {
  switch (s) {
    case "done": return { text: "완료", color: "var(--green)" };
    case "failed": return { text: "실패", color: "var(--red)" };
    case "delivered": return { text: "실행 중", color: "var(--amber)" };
    default: return { text: "전송됨", color: "var(--muted)" };
  }
}

export default function Monitor() {
  const [snap, setSnap] = useState<SyncSnapshot | null>(null);
  const [devices, setDevices] = useState<DeviceRow[]>([]);
  const [cmds, setCmds] = useState<CommandRow[]>([]);
  const [market, setMarket] = useState<MarketContext | null>(null);
  const [preview, setPreview] = useState<NextDayPreview | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  // 투명성(T1) — 마지막 명령 실행 결과 배너. activeCmdRef는 stale 폴링이 새 명령
  // 결과를 덮지 않도록 현재 추적 중인 명령 id를 들고 있다.
  const [cmdResult, setCmdResult] =
    useState<{ text: string; ok: boolean | null } | null>(null);
  const activeCmdRef = useRef<number>(0);

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

  // 매매 예정(NextDayPreview) — 30초 polling. 페어링 여부와 무관하게 fetch
  // (페어링 안 됐을 땐 server가 available=false 응답).
  function loadPreview() {
    api.getNextDayPreview().then(setPreview).catch(() => {});
  }

  // 데이터 패칭(폴링) 효과 — 의도적. (W-05: 정적 분석 규칙은 비활성.)
  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    load();
    loadPreview();
    // 핸드오프 #5 — 탭 비가시(document.hidden) 동안 폴링 정지(요청 0), 재가시화 시
    // 즉시 1회 갱신 후 주기 재개. 백그라운드 탭이 15s/30s 폴링을 계속 쏘던 egress 누수 차단.
    const tick = () => { if (!document.hidden) load(); };
    const tickPreview = () => { if (!document.hidden) loadPreview(); };
    const t = setInterval(tick, REFRESH_MS);
    const t3 = setInterval(tickPreview, 30_000);
    const onVisible = () => { if (!document.hidden) { load(); loadPreview(); } };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(t); clearInterval(t3);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);
  /* eslint-enable react-hooks/set-state-in-effect */

  const paired = devices.length > 0;
  // 명령 대상 = 지금 화면에 보이는 스냅샷을 보낸 그 기기(snap.device_id) — "보는 것 =
  // 제어하는 것". 없으면 가장 최근 활동 기기(서버가 /auth/devices를 last_seen desc로
  // 정렬해 devices[0]가 곧 활성 기기). 같은 PC 재페어링으로 죽은 device row가 쌓여도
  // 명령(재개·전량매도·킬스위치 해제)이 살아있는 기기로 가게 한다.
  const targetDevice = devices.find((d) => d.id === snap?.device_id) ?? devices[0];

  // 명령 ack 폴링 — 거부는 즉시, 실체결은 로컬앱 _wait_pending 대기라 넉넉히(~80s).
  async function pollCommandResult(deviceId: number, cmdId: number): Promise<CommandRow | null> {
    for (let i = 0; i < 40; i++) {
      await new Promise((res) => setTimeout(res, 2000));
      if (activeCmdRef.current !== cmdId) return null;   // 더 새 명령 시작 → 폐기
      try {
        const list = await api.listCommands(deviceId, false);
        const c = list.find((x) => x.id === cmdId);
        if (c && (c.status === "done" || c.status === "failed")) return c;
      } catch { /* 폴링 실패는 재시도 */ }
    }
    return null;
  }

  async function send(type: CommandType,
                      params: Record<string, string | number> = {}) {
    if (!targetDevice) { setErr("기기 페어링이 필요합니다."); return; }
    const dev = targetDevice.id;
    setBusy(true); setCmdResult(null);
    try {
      const cmd = await api.createCommand(dev, type, params);
      activeCmdRef.current = cmd.id;
      setCmdResult({ text: `${labelForCommand(type)} 전송됨 — 로컬앱 실행 대기 중…`, ok: null });
      await load();
      // 백그라운드 추적(UI 블록 안 함) — acked되면 결과 배너로 갱신. 투명성 T1/T2:
      // 청산이 거부(영업일 아님 등)돼도 성공처럼 보이게 두지 않는다.
      void (async () => {
        const done = await pollCommandResult(dev, cmd.id);
        if (activeCmdRef.current !== cmd.id) return;
        if (done) { setCmdResult(describeCommandResult(done)); load(); }
        else setCmdResult({
          text: `${labelForCommand(type)}: 로컬앱 응답이 없습니다 — 데스크탑 앱이 켜져 있는지 확인하세요.`,
          ok: false,
        });
      })();
    } catch (e) {
      setErr((e as Error).message);
    } finally { setBusy(false); }
  }

  // Phase 42-3 — 페어링 해제 시 옛 snapshot(킬스위치·잔고·포지션 등)을
  // 노출하지 않는다. paired=false 분기에서 PairingOnboarding이 표시되므로
  // 본 데이터는 어차피 가려지지만, kill switch banner 같은 page-level
  // 영역까지 stale 데이터를 reflect하지 않도록 source에서 차단.
  const p = paired ? snap?.payload : undefined;
  const ks = p?.kill_switch;
  const summary = p?.cycle_summary;
  const pending = p?.broker_pending ?? p?.pending_local ?? [];
  const orders = p?.recent_orders ?? [];
  const cycles = p?.recent_cycles ?? [];
  const positions = p?.positions ?? [];
  const equityNow = p?.balance?.total_eval;
  const autoStatus = p?.auto_status;   // running | paused | stopped (실시간 동기화)
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

      {loaded && !paired && <PairingOnboarding />}

      {loaded && paired && (
        <>
          {/* ① 성과 히어로 */}
          <PerformanceHero balance={p?.balance} equity={p?.equity}
            strategyPnl={p?.strategy_pnl} killSwitch={ks} />

          {/* ② 상태 바 — 상태 칩 + 다음조치 경보(문제 시에만 확장) */}
          <StatusStrip autoStatus={autoStatus} health={p?.health}
            receivedAt={snap?.received_at} lastHeartbeatAt={snap?.last_heartbeat_at}
            killSwitch={ks} drawdown={p?.drawdown} reconciliation={p?.reconciliation}
            cycleSummary={summary} equityNow={equityNow} heldCount={positions.length}
            onCommand={send} />

          {/* 제어 액션 */}
          <div className="panel monitor-actions">
            <button className="ghost sm" onClick={() => send("PAUSE_AUTO")}
                    disabled={actionDisabled} title={pairTooltip}>일시정지</button>
            <button className="ghost sm" onClick={() => send("RESUME_AUTO")}
                    disabled={actionDisabled} title={pairTooltip}>재개</button>
            <button className="ghost sm" style={{ color: "var(--red)" }}
                    disabled={actionDisabled} title={pairTooltip}
                    onClick={() => {
                      // T4 — 장 마감·비영업일이면 청산이 거부되므로 확정 전에 현재 시장
                      // 단계를 노출한다(phase는 서버가 준 사람용 문자열 — 취약한 디코딩 회피).
                      const phase = market?.session?.phase;
                      const note = "\n\n" + (phase ? `현재 시장: ${phase}\n` : "")
                        + "장 마감·비영업일에는 청산 주문이 거부됩니다(거부 시 결과에 사유가 표시됩니다).";
                      if (confirm("정말 모든 보유 종목을 매도하고 신규 매수를 중지하시겠습니까?" + note)) send("LIQUIDATE_ALL");
                    }}>전량 매도 후 중지</button>
            <span className="muted monitor-device">
              {targetDevice ? `기기: ${targetDevice.name} (#${targetDevice.id})` : "기기 페어링 필요"}
            </span>
          </div>

          {/* 명령 결과 (T1 투명성) — 성공/실패/거부/대기를 즉시 표시.
              비상청산이 영업일 아님으로 거부돼도 성공처럼 보이던 문제를 닫는다. */}
          {cmdResult && (
            <div className={`cmd-result ${cmdResult.ok === null ? "pending" : cmdResult.ok ? "ok" : "bad"}`}>
              <span>{cmdResult.text}</span>
              <button className="x-btn" onClick={() => setCmdResult(null)} aria-label="닫기">×</button>
            </div>
          )}

          {/* 시장 컨텍스트 */}
          <MarketBar ctx={market} />

          {/* ③ 오늘 활동 + 넷팅 */}
          <TodayActivity cycleSummary={summary} decisions={p?.decisions} />

          {/* ④ 전략별 성과 기여 */}
          <StrategyContribution strategyPnl={p?.strategy_pnl} positions={positions}
            nextDayPreview={preview} decisions={p?.decisions} />
          {/* 보유 종목 — 파이차트 + 표 + ledger/KIS 차이 통합 (Phase 40 reconciliation 흡수) */}
          <PositionDetailCards
            positions={positions}
            reconciliation={p?.reconciliation}
            onReconcile={() => send("RECONCILE_NOW")}
            reconcileDisabled={actionDisabled}
            reconcileTooltip={pairTooltip}
          />

      {/* ⑥ 감사 로그 (접힘) — 타임라인·미체결·주문·사이클·명령 */}
      <details className="audit-fold">
        <summary>감사 로그 · 타임라인 · 미체결 · 주문 · 사이클 · 명령</summary>
        <div className="audit-body">

      <TradingTimeline />

      {/* 최근 명령 (T5 투명성) — 보낸 명령의 상태·결과를 유저가 추적 */}
      {cmds.length > 0 && (
        <div className="panel">
          <h3 style={{ marginTop: 0 }}>최근 명령</h3>
          <table className="cmd-log-table">
            <tbody>
              {cmds.slice(0, 8).map((c) => {
                const st = cmdStatusMeta(c.status);
                const acked = c.status === "done" || c.status === "failed";
                const outcome = acked ? describeCommandResult(c).text : "실행 대기 중…";
                return (
                  <tr key={c.id}>
                    <td>{labelForCommand(c.type)}</td>
                    <td style={{ color: st.color, fontWeight: 600, whiteSpace: "nowrap" }}>{st.text}</td>
                    <td className="muted">{outcome}</td>
                    <td className="muted" style={{ whiteSpace: "nowrap" }}>
                      {new Date(c.created_at).toLocaleTimeString("ko-KR",
                        { hour: "2-digit", minute: "2-digit" })}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

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
            <Stat label="보유 스킵" value={summary.n_skip_held ?? 0} />
            <Stat label="거부" value={summary.n_rejected ?? 0} />
          </div>
        </div>
      )}

      {/* 실행 품질·슬리피지 요약·포트폴리오 위험 섹션 제거됨 (요청 #YYYYMMDD). */}

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

      {/* v0.9.13~ — 매매 결정 outcome view: 각 decision의 실제 체결/미체결/거부
          /건너뜀 사유를 한 표로 통합. 옛 패턴은 주문내역+사이클로그+미체결 3 패널
          cross-reference 필요 — 사용자 문의 "X 왜 못 샀지?"에 한 번에 답하기 위해. */}
      <TradeOutcomes cycles={cycles} orders={orders} pending={pending} />

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

        </div>
      </details>

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
