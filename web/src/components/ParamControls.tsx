import { useRef, useState } from "react";
import { api } from "../api";

/**
 * 실시간 변수조정 패널 — 분석 결과의 핵심 변수를 슬라이더/입력으로 바꾸면 IR을 그 값으로
 * 재구성해 `/ir/strategy`로 재실행(LLM 없음·토큰 0)하고 결과(차트)를 즉시 갱신한다.
 * 엑셀 템플릿의 독립변수를 건드리면 수식 셀이 바뀌듯 — 매번 챗 재요청(토큰 낭비) 대신.
 *
 * manifest는 서버 param_manifest(ir)가 동봉한 '조정 가능 변수' 목록(spec SSOT 기반).
 */
export type AdjustableParam = {
  path: string;            // 점경로 — IR 재구성에 사용 (예: "position.entry.top_n")
  label: string;
  type: "number" | "select" | "bool";
  value: number | string | boolean;
  min?: number;
  max?: number;
  step?: number;
  options?: string[];
};

function buildIr(
  baseIr: Record<string, unknown>,
  manifest: AdjustableParam[],
  vals: Record<string, unknown>,
): Record<string, unknown> {
  const ir = JSON.parse(JSON.stringify(baseIr)) as Record<string, unknown>;   // IR은 JSON-안전
  for (const p of manifest) {
    const keys = p.path.split(".");
    let cur = ir;
    for (let i = 0; i < keys.length - 1; i++) {
      const k = keys[i];
      if (typeof cur[k] !== "object" || cur[k] === null) cur[k] = {};
      cur = cur[k] as Record<string, unknown>;
    }
    cur[keys[keys.length - 1]] = vals[p.path];
  }
  return ir;
}

export default function ParamControls({
  baseIr,
  manifest,
  onRun,
}: {
  baseIr: Record<string, unknown>;
  manifest: AdjustableParam[];
  onRun: (ir: Record<string, unknown>, result: Record<string, unknown>) => void;
}) {
  const [open, setOpen] = useState(false);
  const [vals, setVals] = useState<Record<string, unknown>>(
    () => Object.fromEntries(manifest.map((p) => [p.path, p.value])),
  );
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  async function run(next: Record<string, unknown>) {
    const ir = buildIr(baseIr, manifest, next);
    setBusy(true);
    setErr(null);
    try {
      const res = await api.runIrStrategy(ir);
      if ((res as { success?: boolean }).success === false) {
        setErr((res as { error?: string }).error || "재계산 실패");
      } else {
        onRun(ir, res as unknown as Record<string, unknown>);
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : "재계산 실패");
    } finally {
      setBusy(false);
    }
  }

  function change(path: string, v: unknown) {
    const next = { ...vals, [path]: v };
    setVals(next);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => run(next), 400);   // 디바운스 — 연속 입력 중 과다 재실행 방지
  }

  const fieldStyle = { padding: 4, fontSize: "0.9em", background: "var(--panel)",
    color: "inherit", border: "1px solid var(--border)", borderRadius: 6 } as const;

  return (
    <div style={{ marginTop: 8, border: "1px solid var(--border)", borderRadius: 8,
      padding: open ? "10px 12px" : 0, background: open ? "var(--panel)" : "transparent" }}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        style={{ background: "transparent", border: "none", color: "var(--accent)", fontWeight: 600,
          fontSize: "0.85em", cursor: "pointer", padding: open ? "0 0 8px" : "6px 12px" }}
      >
        ⚙ 변수 조정 {open ? "▲" : "▼"}{busy ? " · 재계산 중…" : ""}
      </button>
      {open && (
        <>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 12 }}>
            {manifest.map((p) => (
              <label key={p.path} style={{ display: "flex", flexDirection: "column", gap: 3,
                fontSize: "0.78em", minWidth: 132 }}>
                <span style={{ color: "var(--muted)" }}>{p.label}</span>
                {p.type === "select" ? (
                  <select value={String(vals[p.path])} onChange={(e) => change(p.path, e.target.value)}
                    style={fieldStyle}>
                    {(p.options || []).map((o) => <option key={o} value={o}>{o}</option>)}
                  </select>
                ) : p.type === "bool" ? (
                  <input type="checkbox" checked={Boolean(vals[p.path])}
                    onChange={(e) => change(p.path, e.target.checked)} style={{ width: 18, height: 18 }} />
                ) : (
                  <input type="number" value={Number(vals[p.path])} min={p.min} max={p.max} step={p.step}
                    onChange={(e) => change(p.path, e.target.value === "" ? 0 : Number(e.target.value))}
                    style={{ ...fieldStyle, width: 124 }} />
                )}
              </label>
            ))}
          </div>
          <div style={{ marginTop: 8, fontSize: "0.72em", color: "var(--muted)" }}>
            변수를 바꾸면 자동 재계산됩니다 · AI 호출 없음(토큰 0).
            {err && <span style={{ color: "var(--down)", marginLeft: 6 }}>⚠ {err}</span>}
          </div>
        </>
      )}
    </div>
  );
}
