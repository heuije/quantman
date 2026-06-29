/**
 * TradeOutcomes — 최근 사이클의 매매 결정 결과(체결/미체결/거부/건너뜀) 통합 view.
 *
 * 사용자가 "X 종목 왜 못 샀지?"에 한 번에 답할 수 있게 만든 패널.
 * 옛 패턴(주문내역·사이클 로그·미체결 3 패널 cross-reference)을 한 표로 통합.
 *
 * 데이터: cycle.decisions[] (의도) × orders[] (발주·체결) × pending[] (미체결).
 * 서버 변경 0 — payload에 이미 다 있는 데이터를 join만 함.
 *
 * v0.9.13~ — server v0.9.13 timeline catchup_cycle 매칭이 깔리면 catchup_cycle
 * decisions도 정시 cycle 결과로 인식되어 이 view에 자연 노출됨.
 */
import { useState } from "react";
import type { CycleRow, OrderEvent, PendingOrder } from "../types";
import { wonReadable } from "../format";

type OutcomeKind = "filled" | "pending" | "cancelled" | "rejected" |
                    "skipped" | "duplicate" | "submitted" | "other";

interface Outcome {
  kind: OutcomeKind;
  badge: string;
  detail: string;          // 한 줄 사유 (사용자 친화)
  rawAction: string;       // 원본 action (디버그용)
}

type Decision = CycleRow["decisions"][number];

const KIND_CLASS: Record<OutcomeKind, string> = {
  filled: "pos",            // 체결 — green
  pending: "amber",          // 미체결 — amber/orange
  cancelled: "muted",
  rejected: "neg",           // 거부 — red
  skipped: "muted",
  duplicate: "muted",
  submitted: "amber",
  other: "muted",
};

function findLatestOrder(orders: OrderEvent[], symbol: string,
                          side?: "buy" | "sell"): OrderEvent | null {
  // 같은 심볼·방향의 가장 최근 이벤트. ts 내림차순.
  const matches = orders.filter((o) =>
    o.symbol === symbol && (!side || o.side === side));
  if (!matches.length) return null;
  matches.sort((a, b) => b.ts.localeCompare(a.ts));
  return matches[0];
}

function findPendingFor(pending: PendingOrder[], symbol: string,
                         side?: "buy" | "sell"): PendingOrder | null {
  const match = pending.find((p) =>
    p.symbol === symbol && (!side || p.side === side));
  return match ?? null;
}

function classify(d: Decision, orders: OrderEvent[],
                   pending: PendingOrder[]): Outcome {
  const action = d.action || "";
  const reason = d.reason || "";
  const side: "buy" | "sell" | undefined =
    action.includes("buy") || action === "bought" ? "buy" :
    action.includes("sell") || action === "sold" ? "sell" : undefined;

  // L-01 idempotency 중복 차단 — reason 텍스트로만 식별. trader가 별도 action으로
  // 분리한다면 그쪽이 우선이지만 현재는 reason 안에 "중복" 또는 "L-01" 포함.
  if (reason.includes("중복") || reason.includes("L-01")) {
    return { kind: "duplicate", badge: "🔒 중복 차단",
             detail: reason, rawAction: action };
  }

  // 사이클 내에서 즉시 체결까지 끝난 경우 (시초가·시장가)
  if (action === "bought" || action === "sold") {
    const o = findLatestOrder(orders, d.symbol, side);
    const fillPrice = o?.fill_price ?? d.fill;
    const detail = fillPrice != null
      ? `체결가 ${fillPrice.toLocaleString()}` : "체결 완료";
    return { kind: "filled", badge: "✓ 체결", detail, rawAction: action };
  }

  // 발주 완료, fill 별도 이벤트로 도착 가능성
  if (action === "buy_submitted" || action === "sell_submitted") {
    const o = findLatestOrder(orders, d.symbol, side);
    if (o?.event === "filled") {
      const detail = o.fill_price != null
        ? `체결가 ${o.fill_price.toLocaleString()}` : "체결 완료";
      return { kind: "filled", badge: "✓ 체결", detail, rawAction: action };
    }
    if (o?.event === "cancelled") {
      return { kind: "cancelled", badge: "✗ 취소",
               detail: o.reason || "취소됨", rawAction: action };
    }
    if (o?.event === "rejected") {
      return { kind: "rejected", badge: "✗ KIS 거부",
               detail: o.reason || "KIS 거부 (사유 누락)", rawAction: action };
    }
    const p = findPendingFor(pending, d.symbol, side);
    if (p) {
      const remain = p.remain_qty ?? (p.qty - (p.filled_qty ?? 0));
      const px = p.limit_price ?? d.intended;
      const detail = `가격 미달 — 지정가 ${px?.toLocaleString() ?? "?"} (잔량 ${remain})`;
      return { kind: "pending", badge: "⏳ 미체결", detail, rawAction: action };
    }
    return { kind: "submitted", badge: "→ 발주됨",
             detail: "체결 확인 중 (REST 폴링 대기)", rawAction: action };
  }

  // 명시적 거부
  if (action === "rejected") {
    return { kind: "rejected", badge: "✗ KIS 거부",
             detail: reason || "KIS 거부", rawAction: action };
  }

  // skip_* 분기 — 친화 메시지
  if (action.startsWith("skip_")) {
    return { kind: "skipped", badge: "⊘ 건너뜀",
             detail: reason || action.replace("skip_", "사유: "),
             rawAction: action };
  }

  // 알 수 없는 action — 원본 표시
  return { kind: "other", badge: action || "?",
           detail: reason, rawAction: action };
}

const ORDER_KIND: OutcomeKind[] = [
  "filled", "submitted", "pending", "duplicate",
  "skipped", "cancelled", "rejected", "other"];

function countByKind(outcomes: Outcome[]): Record<OutcomeKind, number> {
  const c: Record<OutcomeKind, number> = {
    filled: 0, submitted: 0, pending: 0, duplicate: 0,
    skipped: 0, cancelled: 0, rejected: 0, other: 0 };
  outcomes.forEach((o) => { c[o.kind] += 1; });
  return c;
}

const KIND_LABEL: Record<OutcomeKind, string> = {
  filled: "체결", submitted: "발주됨", pending: "미체결",
  duplicate: "중복 차단", skipped: "건너뜀",
  cancelled: "취소", rejected: "KIS 거부", other: "기타",
};

interface Props {
  cycles: CycleRow[];                  // recent_cycles (최신 first)
  orders: OrderEvent[];                // recent_orders (최신 first)
  pending: PendingOrder[];             // broker_pending or pending_local
}

export default function TradeOutcomes({ cycles, orders, pending }: Props) {
  const [showAll, setShowAll] = useState(false);

  // 최신 cycle 1개의 decisions를 기준으로. 어제 잔존 보기 토글 시 최근 3 사이클
  // 합치고 dedupe(symbol+strategy+action 키).
  const targetCycles = showAll ? cycles.slice(0, 3) : cycles.slice(0, 1);
  const allDecisions: { d: Decision; cycleTs: string }[] = [];
  for (const c of targetCycles) {
    for (const d of c.decisions || []) {
      allDecisions.push({ d, cycleTs: c.ts });
    }
  }

  // dedupe (showAll일 때만) — 같은 심볼+strategy+action은 최신만
  const seen = new Set<string>();
  const decisions = allDecisions.filter(({ d }) => {
    const key = `${d.strategy_id}|${d.symbol}|${d.action}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });

  const outcomes = decisions.map(({ d, cycleTs }) =>
    ({ d, cycleTs, outcome: classify(d, orders, pending) }));

  // 정렬: kind 기준으로 그룹화 (체결 → 미체결 → ... → 기타)
  outcomes.sort((a, b) =>
    ORDER_KIND.indexOf(a.outcome.kind) - ORDER_KIND.indexOf(b.outcome.kind));

  const counts = countByKind(outcomes.map((o) => o.outcome));
  const headerCount = decisions.length;

  if (!cycles.length) {
    return (
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>오늘 매매 결정 흐름</h3>
        <p className="muted">아직 실행된 사이클이 없습니다.</p>
      </div>
    );
  }

  return (
    <div className="panel">
      <div style={{ display: "flex", justifyContent: "space-between",
                     alignItems: "center", marginBottom: 8, gap: 12,
                     flexWrap: "wrap" }}>
        <h3 style={{ margin: 0 }}>
          오늘 매매 결정 흐름 ({headerCount}건)
        </h3>
        <label style={{ fontSize: 13, color: "var(--muted, #888)" }}>
          <input
            type="checkbox"
            checked={showAll}
            onChange={(e) => setShowAll(e.target.checked)}
            style={{ marginRight: 4 }}
          />
          최근 3 사이클 모두 보기 (어제 잔존 포함)
        </label>
      </div>

      {/* 카운트 칩 — 한눈에 분포 보이게 */}
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap",
                     marginBottom: 10, fontSize: 12 }}>
        {ORDER_KIND.filter((k) => counts[k] > 0).map((k) => (
          <span key={k} className={KIND_CLASS[k]}
                 style={{ padding: "2px 8px", borderRadius: 999,
                          border: "1px solid currentColor" }}>
            {KIND_LABEL[k]} {counts[k]}
          </span>
        ))}
      </div>

      {outcomes.length === 0 ? (
        <p className="muted">결정 사항이 없습니다 (preview 후보 0 또는 사이클 skip).</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>종목</th>
              <th>전략</th>
              <th>방향</th>
              <th style={{ textAlign: "right" }}>수량</th>
              <th>결과</th>
              <th>사유</th>
            </tr>
          </thead>
          <tbody>
            {outcomes.map(({ d, outcome }, i) => {
              const side = (outcome.rawAction.includes("buy") ||
                            outcome.rawAction === "bought")
                ? "매수"
                : (outcome.rawAction.includes("sell") ||
                   outcome.rawAction === "sold")
                ? "매도" : "—";
              return (
                <tr key={`${d.strategy_id}|${d.symbol}|${i}`}>
                  <td>
                    <strong>{d.symbol || "—"}</strong>
                  </td>
                  <td style={{ fontSize: 13 }}>{d.strategy_name || "—"}</td>
                  <td>{side}</td>
                  <td style={{ textAlign: "right" }}>
                    {d.intended ?? "—"}
                  </td>
                  <td className={KIND_CLASS[outcome.kind]}
                       style={{ fontWeight: 500, whiteSpace: "nowrap" }}>
                    {outcome.badge}
                  </td>
                  <td style={{ fontSize: 13 }}>
                    {outcome.detail}
                    {d.extra?.invest && (() => {
                      const inv = d.extra.invest;
                      // USD(해외)면 원 환산 불가 → 원시 숫자 + "$", 그 외는 wonReadable.
                      const money = (v: number) =>
                        inv.currency === "USD" ? `${v.toLocaleString()}$` : wonReadable(v);
                      const line = inv.amount != null
                        ? `투입 ${money(inv.amount)}`
                        : inv.notional != null
                          ? `명목 ${money(inv.notional)}`
                            + (inv.margin != null ? ` · 증거금 ${money(inv.margin)}` : "")
                            + (inv.leverage != null ? ` · 레버리지 ${inv.leverage}x` : "")
                          : "";
                      return line
                        ? <div className="muted small" style={{ marginTop: 2 }}>{line}</div>
                        : null;
                    })()}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
