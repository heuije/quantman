import { Fragment, useEffect, useState } from "react";
import {
  Bar, CartesianGrid, ComposedChart, Line, LineChart, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from "recharts";
import { api } from "../api";
import { useAuth } from "../auth";
import type {
  AdminMetrics, AdminUserRow, ChatInputRow,
  AdminHealth, AdminHealthUser, HealthCondition, HealthStatus,
} from "../types";

// DESIGN.md §8 차트 규칙 — 네이비 막대 + 골드 선만(회색 등 금지).
const CHART_NAVY = "#264a85";
const CHART_GOLD = "#d4a738";

// recharts 툴팁을 테마(다크/라이트) 토큰에 맞춰 스타일.
const TOOLTIP_STYLE = {
  background: "var(--panel)",
  border: "1px solid var(--border)",
  borderRadius: 8,
  fontSize: 12,
  color: "var(--text)",
} as const;
const AXIS_TICK = { fontSize: 11, fill: "var(--muted)" } as const;

/** "YYYY-MM-DD" → "MM/DD" (축 라벨 간결화). */
const mmdd = (d: string) => d.slice(5).replace("-", "/");

/** ISO 시각 → "N분/시간/일 전" (없으면 "—"). */
function ago(iso: string | null): string {
  if (!iso) return "—";
  const sec = (Date.now() - new Date(iso).getTime()) / 1000;
  if (sec < 60) return "방금";
  if (sec < 3600) return `${Math.floor(sec / 60)}분 전`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}시간 전`;
  return `${Math.floor(sec / 86400)}일 전`;
}

const AUTH_LABEL: Record<string, string> = {
  google: "구글", naver: "네이버", password: "이메일",
};

function Stat({ label, value, sub }: { label: string; value: string; sub?: React.ReactNode }) {
  return (
    <div style={{ flex: "1 1 150px", minWidth: 140 }}>
      <div className="muted small" style={{ marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 24, fontWeight: 700, lineHeight: 1.1 }}>{value}</div>
      {sub != null && <div className="muted small" style={{ marginTop: 4 }}>{sub}</div>}
    </div>
  );
}

function ChartPanel({ title, children }: { title: string; children: React.ReactElement }) {
  return (
    <div className="panel" style={{ flex: "1 1 300px", minWidth: 280 }}>
      <div className="sub-h" style={{ marginBottom: 8 }}>{title}</div>
      <ResponsiveContainer width="100%" height={200}>
        {children}
      </ResponsiveContainer>
    </div>
  );
}

// 순위 막대 리스트 (인기 종목·화면별 사용량). DESIGN §8 — 네이비 막대.
function RankPanel({ title, empty, rows }: {
  title: string; empty: string; rows: { label: string; value: number }[];
}) {
  const max = Math.max(1, ...rows.map((r) => r.value));
  return (
    <div className="panel" style={{ flex: "1 1 300px", minWidth: 280 }}>
      <div className="sub-h" style={{ marginBottom: 10 }}>{title}</div>
      {rows.length === 0 ? (
        <p className="muted small" style={{ margin: 0 }}>{empty}</p>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
          {rows.map((r) => (
            <div key={r.label} style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <div style={{ width: 130, fontSize: 13, overflow: "hidden",
                             textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                   title={r.label}>{r.label}</div>
              <div style={{ flex: 1, height: 8, background: "var(--border)",
                             borderRadius: 4, overflow: "hidden" }}>
                <div style={{ width: `${(r.value / max) * 100}%`, height: "100%",
                               background: CHART_NAVY }} />
              </div>
              <div style={{ width: 44, textAlign: "right", fontSize: 13, fontWeight: 600 }}>
                {r.value.toLocaleString()}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── 자동매매 건강 신호등 ──────────────────────────────────────────────────────
// 상태색은 "상태 의미"(정상/주의/이상). DESIGN.md 상태 토큰(--green/--amber/--danger/
// --muted)을 쓴다 — 시장 방향색(--up 빨강/--down 파랑)과 의도적으로 구분(한국식
// 방향색을 상태에 재사용하면 운영자가 '상승/하락'으로 오독).
const HEALTH_COLOR: Record<HealthStatus, string> = {
  green: "var(--green)",
  amber: "var(--amber)",     // 주의 = 앰버/골드 톤 상태 토큰(브랜드 --accent 아님)
  red: "var(--danger)",
  unknown: "var(--muted)",
};
const HEALTH_LABEL: Record<HealthStatus, string> = {
  green: "정상", amber: "주의", red: "이상", unknown: "미상",
};
// 9개 조건 — 서버 키 → 한글 짧은 라벨(권장 표시 순서대로).
const CONDITION_ORDER: [key: string, label: string][] = [
  ["runtime", "런타임"],
  ["connectivity", "연결"],
  ["data_freshness", "데이터"],
  ["strategy_validity", "전략유효"],
  ["broker_ready", "브로커"],
  ["cycle_execution", "사이클"],
  ["decision_output", "판단"],
  ["order_placement", "발주"],
  ["reconciliation", "정합"],
  ["ref_price_integrity", "참조가"],
];

/** ISO → "MM/DD HH:MM" (title 안 정확 시각용). 없으면 "없음". */
function fmtExact(iso: string | null): string {
  return iso ? fmtWhen(iso) : "없음";
}

/** overall 신호등 — 색 점 + 상태 라벨. */
function StatusLight({ status, size = 12 }: { status: HealthStatus; size?: number }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 7 }}>
      <span style={{ width: size, height: size, borderRadius: "50%", flex: "0 0 auto",
                     background: HEALTH_COLOR[status] }} />
      <span style={{ color: HEALTH_COLOR[status], fontWeight: 600 }}>{HEALTH_LABEL[status]}</span>
    </span>
  );
}

/** 9개 조건별 작은 신호등 점 — hover(title)로 라벨·상태·detail 노출. */
function ConditionDots({ conditions }: { conditions: Record<string, HealthCondition> }) {
  return (
    <div style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
      {CONDITION_ORDER.map(([key, label]) => {
        const c = conditions[key];
        const status: HealthStatus = c?.status ?? "unknown";
        const detail = c?.detail || "정보 없음";
        return (
          <span key={key}
                title={`${label} · ${HEALTH_LABEL[status]}\n${detail}`}
                aria-label={`${label} ${HEALTH_LABEL[status]}`}
                style={{ width: 10, height: 10, borderRadius: "50%", cursor: "help",
                         flex: "0 0 auto", background: HEALTH_COLOR[status] }} />
        );
      })}
    </div>
  );
}

function HealthRows({ users }: { users: AdminHealthUser[] }) {
  const thL: React.CSSProperties = { textAlign: "left", padding: "6px 10px", whiteSpace: "nowrap" };
  const th: React.CSSProperties = { textAlign: "right", padding: "6px 10px", whiteSpace: "nowrap" };
  return (
    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
      <thead>
        <tr style={{ borderBottom: "1px solid var(--border)", color: "var(--muted)" }}>
          <th style={{ ...thL, width: 76 }}>상태</th>
          <th style={thL}>이메일</th>
          <th style={th}>실전 전략</th>
          <th style={th} title="로컬앱 마지막 하트비트 — 자동매매 가동 신호">마지막 신호</th>
          <th style={th} title="런타임·연결·데이터·전략유효·브로커·사이클·판단·발주·정합">조건 (9)</th>
        </tr>
      </thead>
      <tbody>
        {users.map((u) => {
          const hasAlerts = u.alert_reasons.length > 0;
          // 경고 강조색 — 이상(red)은 danger, 그 외는 주의(amber)로 눈에 띄게.
          const alertText = u.overall === "red" ? "var(--danger)" : "var(--amber)";
          const alertSoft = u.overall === "red" ? "var(--red-soft)" : "var(--amber-soft)";
          return (
            <Fragment key={u.user_id}>
              <tr style={{ borderBottom: hasAlerts ? "none" : "1px solid var(--border)" }}>
                <td style={thL}><StatusLight status={u.overall} /></td>
                <td style={{ ...thL, fontWeight: 600 }}>{u.email ?? <span className="muted">—</span>}</td>
                <td style={th}>
                  {u.live_strategies > 0
                    ? <span className="badge" style={{ background: "var(--up-soft)", color: "var(--up)" }}>
                        실전 {u.live_strategies}
                      </span>
                    : <span className="muted">0</span>}
                </td>
                <td style={th} className={u.heartbeat_at ? "" : "muted"}
                    title={`하트비트: ${fmtExact(u.heartbeat_at)}\n스냅샷: ${fmtExact(u.snapshot_at)}`}>
                  {ago(u.heartbeat_at)}
                </td>
                <td style={{ ...th, paddingTop: 8, paddingBottom: 8 }}>
                  <ConditionDots conditions={u.conditions} />
                </td>
              </tr>
              {hasAlerts && (
                <tr style={{ borderBottom: "1px solid var(--border)" }}>
                  <td colSpan={5} style={{ padding: "0 10px 8px" }}>
                    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                      {u.alert_reasons.map((a) => (
                        <div key={a.code} style={{ background: alertSoft, color: alertText,
                              borderLeft: `3px solid ${alertText}`, borderRadius: 6,
                              padding: "5px 10px", fontSize: 12.5 }}>
                          ⚠ {a.message}
                        </div>
                      ))}
                    </div>
                  </td>
                </tr>
              )}
            </Fragment>
          );
        })}
      </tbody>
    </table>
  );
}

function HealthPanel({ health, err }: { health: AdminHealth | null; err: string | null }) {
  const alertCount = health?.users.filter((u) => u.alert_reasons.length > 0).length ?? 0;
  return (
    <div className="panel" style={{ marginBottom: 14 }}>
      <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between",
                     gap: 12, flexWrap: "wrap", marginBottom: 10 }}>
        <div className="sub-h" style={{ margin: 0 }}>
          자동매매 건강{health ? ` (${health.users.length})` : ""}
        </div>
        {alertCount > 0 && (
          <span className="badge" style={{ background: "var(--red-soft)", color: "var(--danger)" }}>
            경고 {alertCount}명
          </span>
        )}
      </div>
      {err ? (
        <p className="muted small" style={{ margin: 0 }}>건강 상태를 불러오지 못했습니다 — {err}</p>
      ) : !health ? (
        <p className="muted small" style={{ margin: 0 }}>불러오는 중…</p>
      ) : health.users.length === 0 ? (
        <p className="muted small" style={{ margin: 0 }}>실전 자동매매를 가동 중인 유저가 없습니다.</p>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <HealthRows users={health.users} />
        </div>
      )}
    </div>
  );
}

export default function Admin() {
  const { isAdmin, ready } = useAuth();
  const [data, setData] = useState<AdminMetrics | null>(null);
  const [chatLog, setChatLog] = useState<ChatInputRow[]>([]);
  const [health, setHealth] = useState<AdminHealth | null>(null);
  const [healthErr, setHealthErr] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!isAdmin) return;
    let alive = true;
    api.adminMetrics()
      .then((m) => { if (alive) setData(m); })
      .catch((e) => { if (alive) setErr(e instanceof Error ? e.message : String(e)); });
    // 챗봇 입력 내역은 별도 엔드포인트(페이로드가 커 metrics와 분리)
    api.adminChatLog(100)
      .then((r) => { if (alive) setChatLog(r.messages); })
      .catch(() => { /* 로그 조회 실패는 대시보드 나머지를 막지 않는다 */ });
    // 자동매매 건강 신호등은 별도 엔드포인트(조건 계산이 무거워 metrics와 분리).
    // 조용한 무발주(mwmw 사고)를 운영자가 놓치지 않도록 실패는 삼키지 않고 표면화.
    api.adminHealth()
      .then((h) => { if (alive) setHealth(h); })
      .catch((e) => { if (alive) setHealthErr(e instanceof Error ? e.message : String(e)); });
    return () => { alive = false; };
  }, [isAdmin]);

  // 게이트 — 서버도 403으로 막지만, 비운영자에겐 UI를 아예 안 보여준다(방어적 이중 게이트).
  if (ready && !isAdmin) {
    return (
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>운영자 전용</h3>
        <p className="muted">이 페이지는 서비스 운영자만 볼 수 있습니다.</p>
      </div>
    );
  }
  if (err) {
    return (
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>운영 대시보드</h3>
        <p className="muted">지표를 불러오지 못했습니다 — {err}</p>
      </div>
    );
  }
  if (!data) {
    return <div className="panel"><p className="muted">불러오는 중…</p></div>;
  }

  const { totals, active_users, signups, auth_breakdown, daily, users,
          top_symbols, screen_usage } = data;

  return (
    <div>
      <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between",
                     gap: 12, flexWrap: "wrap", marginBottom: 4 }}>
        <h1>운영 대시보드</h1>
        <span className="muted small">
          최근 {data.window_days}일 · {new Date(data.generated_at).toLocaleString("ko-KR", { hour12: false })} 기준
        </span>
      </div>
      <p className="muted small" style={{ marginTop: 0, marginBottom: 14 }}>
        로그인 유저의 제품 사용·활성 지표입니다. 익명 방문 트래픽(유입경로·페이지뷰)은 Vercel Analytics에서 봅니다.
      </p>

      {/* ── KPI 카드 ── */}
      <div className="panel" style={{ display: "flex", flexWrap: "wrap", gap: 20, marginBottom: 14 }}>
        <Stat label="총 가입자" value={totals.users.toLocaleString()}
              sub={<>신규 24h <b>{signups.last_24h}</b> · 7일 <b>{signups.last_7d}</b></>} />
        <Stat label="활성 유저 (DAU)" value={active_users.dau.toLocaleString()}
              sub={<>WAU <b>{active_users.wau}</b> · MAU <b>{active_users.mau}</b> · 사람 활동 기준</>} />
        <Stat label="자동매매 연동" value={totals.devices.toLocaleString()}
              sub={<>실전 전략 <b>{totals.live_strategies}</b> · 모의 <b>{totals.paper_strategies}</b></>} />
        <Stat label="누적 백테스트" value={totals.backtests.toLocaleString()}
              sub={<>챗봇 <b>{totals.chat_turns.toLocaleString()}</b>턴 · 전략 <b>{totals.strategies}</b></>} />
        <Stat label="가입 인증수단"
              value={`${auth_breakdown.google + auth_breakdown.naver}`}
              sub={<>구글 <b>{auth_breakdown.google}</b> · 네이버 <b>{auth_breakdown.naver}</b> · 이메일 <b>{auth_breakdown.password}</b></>} />
      </div>

      {/* ── 자동매매 건강 신호등 ── */}
      <HealthPanel health={health} err={healthErr} />

      {/* ── 일별 추세 (KST) ── */}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 14, marginBottom: 14 }}>
        <ChartPanel title="일별 신규 가입">
          <ComposedChart data={daily} margin={{ top: 6, right: 8, left: -18, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
            <XAxis dataKey="date" tickFormatter={mmdd} tick={AXIS_TICK} interval="preserveStartEnd" minTickGap={24} />
            <YAxis tick={AXIS_TICK} allowDecimals={false} width={30} />
            <Tooltip contentStyle={TOOLTIP_STYLE} labelStyle={{ color: "var(--muted)" }}
                     labelFormatter={(l) => `${l}`} formatter={(v) => [v, "가입"]} />
            <Bar dataKey="signups" name="가입" fill={CHART_NAVY} radius={[2, 2, 0, 0]} />
          </ComposedChart>
        </ChartPanel>

        <ChartPanel title="일별 활성 유저">
          <LineChart data={daily} margin={{ top: 6, right: 8, left: -18, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
            <XAxis dataKey="date" tickFormatter={mmdd} tick={AXIS_TICK} interval="preserveStartEnd" minTickGap={24} />
            <YAxis tick={AXIS_TICK} allowDecimals={false} width={30} />
            <Tooltip contentStyle={TOOLTIP_STYLE} labelStyle={{ color: "var(--muted)" }}
                     formatter={(v) => [v, "활성 유저"]} />
            <Line type="monotone" dataKey="active_users" name="활성 유저"
                  stroke={CHART_GOLD} strokeWidth={2} dot={false} />
          </LineChart>
        </ChartPanel>

        <ChartPanel title="일별 제품 사용 (백테스트·챗봇)">
          <ComposedChart data={daily} margin={{ top: 6, right: 8, left: -18, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
            <XAxis dataKey="date" tickFormatter={mmdd} tick={AXIS_TICK} interval="preserveStartEnd" minTickGap={24} />
            <YAxis tick={AXIS_TICK} allowDecimals={false} width={30} />
            <Tooltip contentStyle={TOOLTIP_STYLE} labelStyle={{ color: "var(--muted)" }} />
            <Bar dataKey="backtests" name="백테스트" fill={CHART_NAVY} radius={[2, 2, 0, 0]} />
            <Line type="monotone" dataKey="chat_turns" name="챗봇 턴"
                  stroke={CHART_GOLD} strokeWidth={2} dot={false} />
          </ComposedChart>
        </ChartPanel>
      </div>

      {/* ── 인기 종목 · 화면별 사용량 ── */}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 14, marginBottom: 14 }}>
        <RankPanel title="인기 종목 (조회수)" empty="아직 종목 조회 데이터가 없습니다."
                   rows={top_symbols.map((s) => ({ label: s.symbol, value: s.views }))} />
        <RankPanel title="화면별 사용량" empty="아직 활동 데이터가 없습니다."
                   rows={screen_usage.map((s) => ({ label: s.path, value: s.views }))} />
      </div>

      {/* ── 유저별 활동 ── */}
      <div className="panel">
        <div className="sub-h" style={{ marginBottom: 10 }}>유저별 활동 ({users.length})</div>
        <div style={{ overflowX: "auto" }}>
          <UserTable users={users} />
        </div>
      </div>

      {/* ── 챗봇 입력 내역 ── */}
      <ChatLogPanel rows={chatLog} />
    </div>
  );
}

function UserTable({ users }: { users: AdminUserRow[] }) {
  const th: React.CSSProperties = { textAlign: "right", padding: "6px 10px", whiteSpace: "nowrap" };
  const thL: React.CSSProperties = { textAlign: "left", padding: "6px 10px", whiteSpace: "nowrap" };
  const td: React.CSSProperties = { textAlign: "right", padding: "6px 10px", whiteSpace: "nowrap" };
  return (
    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
      <thead>
        <tr style={{ borderBottom: "1px solid var(--border)", color: "var(--muted)" }}>
          <th style={thL}>이메일</th>
          <th style={thL}>가입</th>
          <th style={th} title="사람 행동 기준 — 백테스트·챗봇·전략 컴파일·화면 조회">최근 활동</th>
          <th style={th}>백테스트</th>
          <th style={th}>챗봇</th>
          <th style={th}>전략</th>
          <th style={th}>연동</th>
          <th style={th} title="로컬앱(자동매매) 마지막 신호 — heartbeat/sync. 사람 활동과 별개">로컬앱</th>
          <th style={thL}>인증</th>
        </tr>
      </thead>
      <tbody>
        {users.map((u) => (
          <tr key={u.id} style={{ borderBottom: "1px solid var(--border)" }}>
            <td style={{ ...thL, fontWeight: 600 }}>{u.email}</td>
            <td style={thL} className="muted">{u.created_at.slice(0, 10)}</td>
            <td style={td}>{ago(u.last_active_at)}</td>
            <td style={td}>{u.backtests.toLocaleString()}</td>
            <td style={td}>{u.chat_turns.toLocaleString()}</td>
            <td style={td}>
              {u.strategies}
              {u.live_strategies > 0 && (
                <span className="badge" style={{ marginLeft: 6, background: "var(--up-soft)", color: "var(--up)" }}>
                  실전 {u.live_strategies}
                </span>
              )}
            </td>
            <td style={td}>{u.devices > 0 ? u.devices : <span className="muted">—</span>}</td>
            <td style={td} className={u.local_app_at ? "" : "muted"}>{ago(u.local_app_at)}</td>
            <td style={thL}>
              <span className="badge gray">{AUTH_LABEL[u.auth] ?? u.auth}</span>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function fmtWhen(iso: string): string {
  return new Date(iso).toLocaleString("ko-KR", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  });
}

function ChatLogPanel({ rows }: { rows: ChatInputRow[] }) {
  const thL: React.CSSProperties = { textAlign: "left", padding: "6px 10px", whiteSpace: "nowrap" };
  return (
    <div className="panel">
      <div className="sub-h" style={{ marginBottom: 10 }}>챗봇 입력 내역 ({rows.length})</div>
      {rows.length === 0 ? (
        <p className="muted small" style={{ margin: 0 }}>아직 챗봇 입력이 없습니다.</p>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr style={{ borderBottom: "1px solid var(--border)", color: "var(--muted)" }}>
                <th style={{ ...thL, width: 96 }}>시각</th>
                <th style={thL}>이메일</th>
                <th style={thL}>질문{" "}
                  <span className="muted" style={{ fontWeight: 400 }}>(클릭 → 봇 답변)</span></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((m) => (
                <tr key={m.id} style={{ borderBottom: "1px solid var(--border)", verticalAlign: "top" }}>
                  <td style={{ ...thL, color: "var(--muted)" }}>{fmtWhen(m.at)}</td>
                  <td style={thL}>{m.email ?? "—"}</td>
                  <td style={{ padding: "6px 10px" }}>
                    {m.answer ? (
                      <details>
                        <summary style={{ cursor: "pointer" }}>
                          {m.question || <span className="muted">(빈 입력)</span>}
                        </summary>
                        <div className="muted" style={{ marginTop: 6, whiteSpace: "pre-wrap",
                                                          maxHeight: 220, overflowY: "auto", fontSize: 12.5 }}>
                          {m.answer}
                        </div>
                      </details>
                    ) : (
                      m.question || <span className="muted">(빈 입력)</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
