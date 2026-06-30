import { memo, useMemo, useState } from "react";
import {
  CartesianGrid, ComposedChart, Legend, Line, ResponsiveContainer,
  Scatter, Tooltip, XAxis, YAxis,
} from "recharts";

interface Point { date: string; value: number | null }
type Trade = Record<string, string | number | null>;

interface Props {
  equity: Point[];
  benchmark?: Point[];
  trades?: Trade[];        // 있으면 진입▲/청산▼ 지점을 자산곡선 위에 마킹
}

/* recharts SVG는 CSS var를 못 받아 토큰값(DESIGN.md)을 직접 인라인한다.
   변경 시 web/src/index.css :root와 동기화. up=매수/상승(빨강), down=매도/하락(파랑). */
const C = {
  accent: "#d4a738", muted: "#8b94a3", grid: "#2b323e",
  up: "#de3033", down: "#1668c4", ink: "#d2d8e0", panel: "#1c212b",
};

// 전체기간 백테스트는 equity가 수천 포인트(15년 ≈ 3,700 거래일)·trades가 수천 건까지 커지고,
// recharts는 포인트·SVG 노드 수에 비례해 느려져 그대로 그리면 메인스레드가 멎는다.
// 렌더만 줄인다(수신 데이터·지표는 불변): 버킷 min/max 보존 다운샘플 + 거래 마커 캡.
const MAX_FULL_POINTS = 600;   // 이하는 다운샘플 생략 — 라이브 곡선 등 기존 소비처 렌더 보존
const TARGET_BUCKETS = 200;    // 버킷당 최저·최고 2점 유지 → 렌더 ≤ ~400점, MDD 골짜기 보존
const MARKER_CAP = 200;        // 거래가 이를 넘으면 마커 기본 OFF (토글로 수동 표시는 가능)

const won = (v: number | null | undefined) =>
  v == null ? "—" : `${Math.round(v).toLocaleString()}원`;
const px = (v: number | null | undefined) =>
  v == null ? "—" : `@${Number(v).toLocaleString()}`;   // 종목 통화 단위 미상 → @가격 중립표기

// 매수 ▲ (자산곡선 점 위에 위쪽 삼각형)
function BuyMarker({ cx, cy }: { cx?: number; cy?: number }) {
  if (cx == null || cy == null) return null;
  return <path d={`M${cx},${cy - 7} L${cx - 6},${cy + 5} L${cx + 6},${cy + 5} Z`}
    fill={C.up} stroke="#fff" strokeWidth={1} />;
}
// 매도 ▼ (아래쪽 삼각형)
function SellMarker({ cx, cy }: { cx?: number; cy?: number }) {
  if (cx == null || cy == null) return null;
  return <path d={`M${cx},${cy + 7} L${cx - 6},${cy - 5} L${cx + 6},${cy - 5} Z`}
    fill={C.down} stroke="#fff" strokeWidth={1} />;
}

interface Row {
  date: string;
  전략: number | null;
  "Buy&Hold": number | null;
  // T6 — 기준자본 대비 % 수익(각 시리즈 자기 첫값=0%). %/금액 토글로 표시 전환(#1 %정규화 곡선).
  "전략%": number | null;
  "Buy&Hold%": number | null;
  buy: number | null;
  sell: number | null;
  buyPct: number | null;       // %모드 마커 위치(전략% 위)
  sellPct: number | null;
  buyInfo: { sym?: string; price: number | null }[];
  sellInfo: { sym?: string; price: number | null; ret: number | null }[];
}

const pctFmt = (v: number | null | undefined) =>
  v == null ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(1)}%`;

function ChartTooltip({ active, payload, label, pct }:
  { active?: boolean; payload?: { payload: Row }[]; label?: string; pct?: boolean }) {
  if (!active || !payload?.length) return null;
  const row = payload[0].payload;
  const sv = pct ? pctFmt(row["전략%"]) : won(row.전략);
  const bv = pct ? pctFmt(row["Buy&Hold%"]) : won(row["Buy&Hold"]);
  return (
    <div style={{
      background: C.panel, border: `1px solid ${C.grid}`, borderRadius: 8,
      padding: "8px 10px", fontSize: 12, color: C.muted,
    }}>
      <div style={{ fontWeight: 600, color: C.ink, marginBottom: 4 }}>{label}</div>
      <div>전략 {sv}</div>
      {row["Buy&Hold"] != null && <div>Buy&Hold {bv}</div>}
      {row.buyInfo.map((b, k) => (
        <div key={`b${k}`} style={{ color: C.up, marginTop: 2 }}>
          ▲ 매수 {b.sym ? `${b.sym} ` : ""}{px(b.price)}
        </div>
      ))}
      {row.sellInfo.map((s, k) => (
        <div key={`s${k}`} style={{ color: C.down, marginTop: 2 }}>
          ▼ 매도 {s.sym ? `${s.sym} ` : ""}{px(s.price)}
          {s.ret != null ? ` (${s.ret >= 0 ? "+" : ""}${s.ret.toFixed(1)}%)` : ""}
        </div>
      ))}
    </div>
  );
}

/** 자산곡선 차트 — 전략 vs Buy&Hold. trades가 있으면 진입▲/청산▼ 마킹. */
function EquityChart({ equity, benchmark, trades }: Props) {
  const tradeCount = trades?.length ?? 0;
  // 거래 마커 기본값 = 건수 기반(과다 마커는 SVG 노드 폭발). 사용자 토글은 해당 결과(trades
  // 참조)에만 유효 — 새 결과로 바뀌면 기본값 복귀(이전 결과의 토글이 대용량 결과에 박제 방지).
  const [override, setOverride] = useState<{ trades?: Trade[]; show: boolean } | null>(null);
  const showTrades = override && override.trades === trades
    ? override.show : tradeCount <= MARKER_CAP;
  const [pct, setPct] = useState(false);   // T6 — %수익(기준자본=0%) vs 금액. 기본=금액(기존 동작 보존).

  const { rows, distinctDates } = useMemo(() => {
    const idx = new Map<string, number>();
    const v0 = equity.find((p) => p.value != null)?.value ?? null;     // 전략 기준값(첫 비결손)
    const b0 = benchmark?.find((p) => p.value != null)?.value ?? null;  // Buy&Hold 기준값
    const merged: Row[] = equity.map((p, i) => {
      idx.set(p.date, i);
      const bench = benchmark?.[i]?.value ?? null;
      return {
        date: p.date, 전략: p.value, "Buy&Hold": bench,
        "전략%": v0 && p.value != null ? (p.value / v0 - 1) * 100 : null,
        "Buy&Hold%": b0 && bench != null ? (bench / b0 - 1) * 100 : null,
        buy: null, sell: null, buyPct: null, sellPct: null, buyInfo: [], sellInfo: [],
      };
    });

    if (tradeCount && showTrades) {
      for (const t of trades!) {
        const sym = (t["종목"] as string) ?? undefined;
        const bi = idx.get(t["진입일"] as string);
        const si = idx.get(t["청산일"] as string);
        if (bi != null) {
          merged[bi].buy = merged[bi].전략;            // 마커는 그날 자산곡선 값 위에
          merged[bi].buyPct = merged[bi]["전략%"];      // %모드에선 전략% 위에
          merged[bi].buyInfo.push({ sym, price: (t["진입가"] as number) ?? null });
        }
        if (si != null) {
          merged[si].sell = merged[si].전략;
          merged[si].sellPct = merged[si]["전략%"];
          merged[si].sellInfo.push({
            sym, price: (t["청산가"] as number) ?? null,
            ret: (t["수익률(%)"] as number) ?? null,
          });
        }
      }
    }

    if (merged.length <= MAX_FULL_POINTS) return { rows: merged, distinctDates: idx.size };

    // 다운샘플 — 280px 차트에서 수천 점은 픽셀상 구분 불가. 버킷별 전략값 min/max를 남겨
    // 곡선 형태(MDD 골짜기·고점)를 보존하고, 마커 행은 강제 포함해 거래 표식의 날짜·값을
    // 원본 그대로 유지한다. 트레이드오프: 툴팁이 유지된 날짜에서만 뜬다(표시 한정, 데이터 불변).
    const keep = new Set<number>([0, merged.length - 1]);
    const bucket = Math.ceil(merged.length / TARGET_BUCKETS);
    for (let b = 0; b < merged.length; b += bucket) {
      const end = Math.min(b + bucket, merged.length);
      let loI = -1, hiI = -1, lo = Infinity, hi = -Infinity;
      for (let i = b; i < end; i++) {
        const v = merged[i].전략;
        if (v == null) continue;
        if (v < lo) { lo = v; loI = i; }
        if (v > hi) { hi = v; hiI = i; }
      }
      if (loI >= 0) keep.add(loI);
      if (hiI >= 0) keep.add(hiI);
    }
    if (showTrades) {
      for (let i = 0; i < merged.length; i++) {
        if (merged[i].buyInfo.length || merged[i].sellInfo.length) keep.add(i);
      }
    }
    const rows = [...keep].sort((a, z) => a - z).map((i) => merged[i]);
    return { rows, distinctDates: idx.size };
  }, [equity, benchmark, trades, tradeCount, showTrades]);

  // 데이터가 1점뿐이면 recharts가 x축에 같은 날짜를 반복 렌더해 버그처럼 보인다.
  if (distinctDates < 2) {
    return (
      <div className="empty" style={{ height: 120 }}>
        데이터가 아직 충분하지 않습니다 — 사이클이 며칠 쌓이면 곡선이 그려집니다.
      </div>
    );
  }

  const hasTrades = tradeCount > 0;
  const fmt = (v: number) =>
    v >= 1e8 ? `${(v / 1e8).toFixed(1)}억`
      : v >= 1e4 ? `${Math.round(v / 1e4)}만` : `${v}`;

  const stratKey = pct ? "전략%" : "전략";
  const benchKey = pct ? "Buy&Hold%" : "Buy&Hold";
  const buyKey = pct ? "buyPct" : "buy";
  const sellKey = pct ? "sellPct" : "sell";
  return (
    <div>
      <div style={{
        display: "flex", alignItems: "center", gap: 14, fontSize: 12,
        color: C.muted, marginBottom: 6, flexWrap: "wrap",
      }}>
        <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer", userSelect: "none" }}>
          <input type="checkbox" checked={pct} onChange={(e) => setPct(e.target.checked)} />
          % 수익 (기준자본 = 0%)
        </label>
        {hasTrades && (
          <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer", userSelect: "none" }}>
            <input type="checkbox" checked={showTrades}
                   onChange={(e) => setOverride({ trades, show: e.target.checked })} />
            거래 표시 (매수 ▲ · 매도 ▼)
            {tradeCount > MARKER_CAP && (
              <span>— 거래 {tradeCount.toLocaleString()}건이라 기본 꺼짐</span>
            )}
          </label>
        )}
      </div>
      <ResponsiveContainer width="100%" height={280}>
        <ComposedChart data={rows} margin={{ top: 8, right: 16, bottom: 0, left: 8 }}>
          <CartesianGrid stroke={C.grid} />
          <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={50} />
          <YAxis tickFormatter={pct ? (v) => `${Math.round(Number(v))}%` : fmt}
                 tick={{ fontSize: 11 }} width={52} />
          <Tooltip content={<ChartTooltip pct={pct} />} />
          <Legend />
          <Line type="monotone" dataKey={stratKey} name="전략" stroke={C.accent}
                dot={false} strokeWidth={2} isAnimationActive={false} />
          <Line type="monotone" dataKey={benchKey} name="Buy&Hold" stroke={C.muted}
                dot={false} strokeWidth={1.5} strokeDasharray="4 3"
                isAnimationActive={false} />
          {hasTrades && showTrades && (
            <Scatter name="매수" dataKey={buyKey} fill={C.up}
                     shape={<BuyMarker />} legendType="triangle" isAnimationActive={false} />
          )}
          {hasTrades && showTrades && (
            <Scatter name="매도" dataKey={sellKey} fill={C.down}
                     shape={<SellMarker />} legendType="triangle" isAnimationActive={false} />
          )}
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}

// 부모 상태 변경(예: 모의적용 클릭이 켜는 saving 토글)이 무거운 차트 재렌더로 번지지 않게,
// props(결과 객체의 배열 참조)가 같으면 렌더를 건너뛴다.
export default memo(EquityChart);
