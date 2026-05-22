/**
 * 자동 선택 기준 미세조정 — 프리셋을 시작점으로 룰을 직접 조정.
 *
 * 백엔드 screener.parse_spec이 받는 ScreenerSpec을 빈칸형으로 노출한다.
 * 커스텀이 활성화되면 trade_symbol을 'screener:custom'으로 바꾸고 spec을 저장.
 * "미리보기"는 서버에서 즉시 매칭 종목 수 + 표본 + 기준일(as_of)을 보여준다.
 */

import { useEffect, useState } from "react";
import { api } from "../api";
import type {
  ScreenerField, ScreenerMatch, ScreenerOp, ScreenerPreset,
  ScreenerRuleIO, ScreenerSpecIO,
} from "../types";
import { parseScreenerKey } from "../types";

const OPS: { value: ScreenerOp; label: string }[] = [
  { value: ">=", label: "이상" },
  { value: ">", label: "초과" },
  { value: "<=", label: "이하" },
  { value: "<", label: "미만" },
  { value: "between", label: "범위" },
];

/** YYYY-MM-DD → "5/22(금)" 한국식 표기. */
function fmtAsOf(d: string | null): string {
  if (!d) return "";
  const dt = new Date(d + "T00:00:00");
  if (isNaN(dt.getTime())) return d;
  const wd = ["일", "월", "화", "수", "목", "금", "토"][dt.getDay()];
  return `${dt.getMonth() + 1}/${dt.getDate()}(${wd})`;
}

export default function ScreenerCustomizer({
  tradeSymbol, setTradeSymbol, spec, setSpec,
}: {
  tradeSymbol: string;
  setTradeSymbol: (v: string) => void;
  spec: ScreenerSpecIO | null;
  setSpec: (s: ScreenerSpecIO | null) => void;
}) {
  const [fields, setFields] = useState<ScreenerField[]>([]);
  const [presets, setPresets] = useState<ScreenerPreset[]>([]);
  const [preview, setPreview] = useState<{ count: number; matches: ScreenerMatch[]; as_of: string | null } | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const isCustom = tradeSymbol === "screener:custom";

  useEffect(() => {
    api.screenerFields().then((r) => setFields(r.fields)).catch(() => {});
    api.listScreenerPresets().then((r) => setPresets(r.presets)).catch(() => {});
  }, []);

  const fieldLabel = (key: string) =>
    fields.find((f) => f.key === key)?.label ?? key;

  /** 현재 선택된 프리셋의 spec을 시작점으로 커스텀 모드 진입. */
  function startFromPreset() {
    const key = parseScreenerKey(tradeSymbol);
    const base = presets.find((p) => p.key === key)?.spec;
    const init: ScreenerSpecIO = base
      ? JSON.parse(JSON.stringify(base))
      : { rules: [{ field: "market_cap", op: ">=", value: 100_000_000_000 }],
          sort: { field: "market_cap", order: "desc" }, limit: 20 };
    setSpec(init);
    setTradeSymbol("screener:custom");
    setPreview(null);
  }

  function update(next: ScreenerSpecIO) {
    setSpec(next);
    setPreview(null);
  }

  function setRule(i: number, patch: Partial<ScreenerRuleIO>) {
    if (!spec) return;
    const rules = spec.rules.map((r, idx) => {
      if (idx !== i) return r;
      const nr = { ...r, ...patch };
      // op이 between↔단일로 바뀌면 value 형태 맞추기
      if (patch.op === "between" && !Array.isArray(nr.value)) nr.value = [0, 0];
      if (patch.op && patch.op !== "between" && Array.isArray(nr.value)) nr.value = 0;
      return nr;
    });
    update({ ...spec, rules });
  }

  function addRule() {
    if (!spec) return;
    update({ ...spec, rules: [...spec.rules, { field: "pct_change_1d", op: ">=", value: 0 }] });
  }
  function removeRule(i: number) {
    if (!spec) return;
    update({ ...spec, rules: spec.rules.filter((_, idx) => idx !== i) });
  }

  async function runPreview() {
    if (!spec) return;
    setBusy(true); setErr(""); setPreview(null);
    try {
      const r = await api.runScreenerCustom(spec);
      setPreview(r);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function revertToPreset() {
    setSpec(null);
    setTradeSymbol("");
    setPreview(null);
  }

  if (!isCustom) {
    return (
      <button type="button" className="ghost sm" style={{ marginTop: 10 }}
              onClick={startFromPreset} disabled={presets.length === 0}>
        + 기준 직접 조정
      </button>
    );
  }

  return (
    <div className="screener-customizer">
      <div className="screener-customizer-head">
        <strong>기준 직접 조정 (커스텀)</strong>
        <button type="button" className="ghost sm" onClick={revertToPreset}>
          프리셋으로 되돌리기
        </button>
      </div>

      {spec && (
        <>
          {spec.rules.map((r, i) => (
            <div key={i} className="screener-rule">
              <select value={r.field}
                      onChange={(e) => setRule(i, { field: e.target.value })}>
                {fields.map((f) => (
                  <option key={f.key} value={f.key}>
                    {f.label}{f.unit ? ` (${f.unit})` : ""}
                  </option>
                ))}
              </select>
              <select value={r.op}
                      onChange={(e) => setRule(i, { op: e.target.value as ScreenerOp })}>
                {OPS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
              {r.op === "between" ? (
                <>
                  <input type="number" step="any"
                         value={Array.isArray(r.value) ? r.value[0] : 0}
                         onChange={(e) => setRule(i, {
                           value: [Number(e.target.value), Array.isArray(r.value) ? r.value[1] : 0],
                         })} />
                  <span className="txt">~</span>
                  <input type="number" step="any"
                         value={Array.isArray(r.value) ? r.value[1] : 0}
                         onChange={(e) => setRule(i, {
                           value: [Array.isArray(r.value) ? r.value[0] : 0, Number(e.target.value)],
                         })} />
                </>
              ) : (
                <input type="number" step="any"
                       value={Array.isArray(r.value) ? 0 : r.value}
                       onChange={(e) => setRule(i, { value: Number(e.target.value) })} />
              )}
              <button type="button" className="ghost sm" onClick={() => removeRule(i)}>×</button>
            </div>
          ))}
          <button type="button" className="ghost sm" onClick={addRule}>+ 기준 추가</button>

          <div className="screener-sort">
            <label>정렬</label>
            <select value={spec.sort?.field ?? ""}
                    onChange={(e) => update({
                      ...spec,
                      sort: e.target.value
                        ? { field: e.target.value, order: spec.sort?.order ?? "desc" }
                        : null,
                    })}>
              <option value="">정렬 안 함</option>
              {fields.map((f) => <option key={f.key} value={f.key}>{f.label}</option>)}
            </select>
            {spec.sort && (
              <select value={spec.sort.order}
                      onChange={(e) => update({
                        ...spec, sort: { field: spec.sort!.field, order: e.target.value as "asc" | "desc" },
                      })}>
                <option value="desc">높은 순</option>
                <option value="asc">낮은 순</option>
              </select>
            )}
            <label>상위</label>
            <input type="number" min={1} max={100} value={spec.limit ?? 20}
                   onChange={(e) => update({ ...spec, limit: Number(e.target.value) })} />
            <span className="txt">개</span>
          </div>

          <div className="screener-preview-actions">
            <button type="button" className="sm" onClick={runPreview} disabled={busy}>
              {busy ? "조회 중…" : "미리보기"}
            </button>
            {preview && (
              <span className="muted small">
                {fmtAsOf(preview.as_of)} 기준 · <b>{preview.count}종목</b> 매칭
              </span>
            )}
          </div>

          {err && <div className="error">{err}</div>}
          {preview && preview.matches.length > 0 && (
            <ul className="screener-preview-list">
              {preview.matches.slice(0, 8).map((m) => (
                <li key={m.symbol}>
                  <span>{m.name} <span className="muted small">{m.symbol}</span></span>
                  <span className="muted small">
                    {m.pct_change_1d != null ? `${m.pct_change_1d > 0 ? "+" : ""}${m.pct_change_1d.toFixed(2)}%` : ""}
                  </span>
                </li>
              ))}
              {preview.count > 8 && <li className="muted small">…외 {preview.count - 8}종목</li>}
            </ul>
          )}
          <p className="muted small">
            * 조정한 기준은 <b>{fieldLabel(spec.rules[0]?.field ?? "")}</b> 등 {spec.rules.length}개 조건으로
            매 거래일 종목을 재선정합니다.
          </p>
        </>
      )}
    </div>
  );
}
