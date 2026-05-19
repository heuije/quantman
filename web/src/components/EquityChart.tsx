import {
  CartesianGrid, Legend, Line, LineChart, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from "recharts";

interface Point { date: string; value: number | null }

interface Props {
  equity: Point[];
  benchmark?: Point[];
}

/** 자산곡선 차트 — 전략 vs Buy&Hold. */
export default function EquityChart({ equity, benchmark }: Props) {
  const merged = equity.map((p, i) => ({
    date: p.date,
    전략: p.value,
    "Buy&Hold": benchmark?.[i]?.value ?? null,
  }));

  const fmt = (v: number) =>
    v >= 1e8 ? `${(v / 1e8).toFixed(1)}억`
      : v >= 1e4 ? `${Math.round(v / 1e4)}만` : `${v}`;

  return (
    <ResponsiveContainer width="100%" height={280}>
      <LineChart data={merged} margin={{ top: 8, right: 16, bottom: 0, left: 8 }}>
        <CartesianGrid stroke="#eef0f3" />
        <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={50} />
        <YAxis tickFormatter={fmt} tick={{ fontSize: 11 }} width={52} />
        <Tooltip
          formatter={(v) => `${Number(v).toLocaleString()}원`}
          labelStyle={{ color: "#6b7280" }}
        />
        <Legend />
        <Line type="monotone" dataKey="전략" stroke="#4f46e5"
              dot={false} strokeWidth={2} />
        <Line type="monotone" dataKey="Buy&Hold" stroke="#9ca3af"
              dot={false} strokeWidth={1.5} strokeDasharray="4 3" />
      </LineChart>
    </ResponsiveContainer>
  );
}
