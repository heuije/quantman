/**
 * 자동 선택 '세트' 통합 팝오버.
 *
 * 한 화면에서:
 *   - [+ 새로운 세트 만들기] — 룰을 직접 짜서 계정에 저장(POST).
 *   - 내 세트 (계정 저장) — 선택 / ⚙ 수정(PUT) / 🗑 삭제.
 *   - 기본 세트 (서버 프리셋) — 선택 / ⚙ 숫자 조정(이 전략에만) / ▸ 미리보기.
 *
 * '선택'이 전략의 매수 대상을 정한다.
 *   - 기본 세트 그대로 → trade_symbol = "screener:<key>"
 *   - 내 세트 / 조정한 세트 → trade_symbol = "screener:custom" + spec 스냅샷
 *     (label에 표시 이름 저장 — 백엔드 parse_spec은 무시)
 */

import { useEffect, useState } from "react";
import { api } from "../api";
import type {
  ScreenerField, ScreenerMatch, ScreenerOp, ScreenerPreset,
  ScreenerRuleIO, ScreenerSpecIO, ScreenerUserPreset,
} from "../types";

const OPS: { value: ScreenerOp; label: string }[] = [
  { value: ">=", label: "이상" },
  { value: ">", label: "초과" },
  { value: "<=", label: "이하" },
  { value: "<", label: "미만" },
  { value: "between", label: "범위" },
];

const DEFAULT_SPEC: ScreenerSpecIO = {
  rules: [{ field: "market_cap", op: ">=", value: 100_000_000_000 }],
  sort: { field: "market_cap", order: "desc" },
  limit: 20,
};

/** YYYY-MM-DD → "5/22(금)". */
function fmtAsOf(d: string | null): string {
  if (!d) return "";
  const dt = new Date(d + "T00:00:00");
  if (isNaN(dt.getTime())) return d;
  const wd = ["일", "월", "화", "수", "목", "금", "토"][dt.getDay()];
  return `${dt.getMonth() + 1}/${dt.getDate()}(${wd})`;
}

// ── 순수 spec 변환 헬퍼 ───────────────────────────────────────────────────────

function withRule(spec: ScreenerSpecIO, i: number, patch: Partial<ScreenerRuleIO>): ScreenerSpecIO {
  const rules = spec.rules.map((r, idx) => {
    if (idx !== i) return r;
    const nr = { ...r, ...patch };
    if (patch.op === "between" && !Array.isArray(nr.value)) nr.value = [0, 0];
    if (patch.op && patch.op !== "between" && Array.isArray(nr.value)) nr.value = 0;
    return nr;
  });
  return { ...spec, rules };
}
function withAddedRule(spec: ScreenerSpecIO): ScreenerSpecIO {
  return { ...spec, rules: [...spec.rules, { field: "pct_change_1d", op: ">=", value: 0 }] };
}
function withRemovedRule(spec: ScreenerSpecIO, i: number): ScreenerSpecIO {
  return { ...spec, rules: spec.rules.filter((_, idx) => idx !== i) };
}

type EditorState =
  | { kind: "new"; name: string; spec: ScreenerSpecIO }
  | { kind: "myset"; id: number; name: string; spec: ScreenerSpecIO }
  | { kind: "preset"; key: string; title: string; spec: ScreenerSpecIO };

export default function ScreenerPanel({
  presets, asOf, tradeSymbol, setTradeSymbol, spec, setSpec, setScreenerLimit, onClose,
}: {
  presets: ScreenerPreset[];
  asOf: string | null;
  tradeSymbol: string;
  setTradeSymbol: (v: string) => void;
  spec: ScreenerSpecIO | null;
  setSpec: (s: ScreenerSpecIO | null) => void;
  setScreenerLimit: (n: number) => void;
  onClose: () => void;
}) {
  const [fields, setFields] = useState<ScreenerField[]>([]);
  const [myPresets, setMyPresets] = useState<ScreenerUserPreset[]>([]);
  const [editor, setEditor] = useState<EditorState | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [confirmDelete, setConfirmDelete] = useState<number | null>(null);

  // 기본 세트 미리보기 (▸ 펼침)
  const [expanded, setExpanded] = useState<string | null>(null);
  const [previews, setPreviews] = useState<Record<string, ScreenerMatch[]>>({});
  const [loadingPrev, setLoadingPrev] = useState<string | null>(null);

  // 기본 세트 카드 내부 inline 편집 분 — preset 키 → 사용자 조정 spec.
  // 값이 있으면 그 preset은 "맞춤" 모드로 적용(custom). 없으면 preset 원본 적용.
  const [presetEdits, setPresetEdits] = useState<Record<string, ScreenerSpecIO>>({});

  useEffect(() => {
    api.screenerFields().then((r) => setFields(r.fields)).catch(() => {});
    api.listMyScreenerPresets().then((r) => setMyPresets(r.presets)).catch(() => {});
  }, []);

  const isCustom = tradeSymbol === "screener:custom";
  const selectedKey = !isCustom && tradeSymbol.startsWith("screener:")
    ? tradeSymbol.slice("screener:".length) : null;

  // ── 적용(전략 매수 대상 확정) ──────────────────────────────────────────────
  // 보유 종목 수 = 세트의 상위 N개 (상위 N = 보유 N으로 통합).
  function applyPreset(key: string) {
    const p = presets.find((x) => x.key === key);
    setSpec(null);
    setTradeSymbol(`screener:${key}`);
    setScreenerLimit(p?.spec?.limit ?? 20);
    onClose();
  }
  function applyCustom(s: ScreenerSpecIO, label: string) {
    setSpec({ ...s, label });
    setTradeSymbol("screener:custom");
    setScreenerLimit(s.limit ?? 20);
    onClose();
  }

  // ── 기본 세트 미리보기 토글 ────────────────────────────────────────────────
  function togglePreview(key: string) {
    if (expanded === key) { setExpanded(null); return; }
    setExpanded(key);
    if (previews[key]) return;
    setLoadingPrev(key);
    api.runScreenerPreset(key)
      .then((r) => setPreviews((p) => ({ ...p, [key]: r.matches })))
      .catch(() => {})
      .finally(() => setLoadingPrev(null));
  }

  // ── 내 세트 삭제 (인라인 2단계 확인) ──────────────────────────────────────
  async function deleteMyset(p: ScreenerUserPreset) {
    try {
      await api.deleteMyScreenerPreset(p.id);
      setMyPresets((prev) => prev.filter((x) => x.id !== p.id));
      setConfirmDelete(null);
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  // ── 편집기 저장 ────────────────────────────────────────────────────────────
  async function saveEditor() {
    if (!editor) return;
    if (editor.kind === "preset") {
      // 기본 세트 조정 → 이 전략에만 적용 (계정 저장 안 함)
      applyCustom(editor.spec, `${editor.title} 맞춤`);
      return;
    }
    const name = editor.name.trim();
    if (!name) { setErr("세트 이름을 입력하세요."); return; }
    setBusy(true); setErr("");
    try {
      let saved: ScreenerUserPreset;
      if (editor.kind === "myset") {
        saved = await api.updateMyScreenerPreset(editor.id, name, editor.spec);
        setMyPresets((prev) => prev.map((x) => (x.id === saved.id ? saved : x)));
      } else {
        saved = await api.createMyScreenerPreset(name, editor.spec);
        setMyPresets((prev) => [saved, ...prev]);
      }
      applyCustom(saved.spec, saved.name);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  // ── 편집기 화면 ────────────────────────────────────────────────────────────
  if (editor) {
    return (
      <SetEditor
        editor={editor} setEditor={setEditor} fields={fields} asOf={asOf}
        busy={busy} err={err} onSave={saveEditor} onCancel={() => { setEditor(null); setErr(""); }}
      />
    );
  }

  // ── 기본 세트 카드 렌더 (국내/미국 섹션 공용) ──────────────────────────────
  const krPresets = presets.filter((p) => p.market_group !== "US");
  const usPresets = presets.filter((p) => p.market_group === "US");

  // ── 기본 세트 카드 inline 편집 헬퍼 ──────────────────────────────────────────
  // preset 원본 spec(없으면 DEFAULT) → 사용자 조정분이 있으면 그것을 우선.
  function effectiveSpec(p: ScreenerPreset): ScreenerSpecIO {
    const edited = presetEdits[p.key];
    if (edited) return edited;
    return p.spec ? p.spec : DEFAULT_SPEC;
  }

  // inline 값 수정 → presetEdits 갱신 + 즉시 "맞춤"으로 적용.
  // 카드 클릭으로 preset 원본을 골랐다가 값 한 글자 바꾸면 자동으로 custom 전환.
  function editPresetRuleValue(p: ScreenerPreset, idx: number, value: number | number[]) {
    const base = effectiveSpec(p);
    const next: ScreenerSpecIO = {
      ...base,
      rules: base.rules.map((r, i) => (i === idx ? { ...r, value } : r)),
      label: `${p.title} 맞춤`,
    };
    setPresetEdits((prev) => ({ ...prev, [p.key]: next }));
    setSpec(next);
    setTradeSymbol("screener:custom");
    setScreenerLimit(next.limit ?? 20);
  }
  function editPresetLimit(p: ScreenerPreset, limit: number) {
    const base = effectiveSpec(p);
    const next: ScreenerSpecIO = { ...base, limit, label: `${p.title} 맞춤` };
    setPresetEdits((prev) => ({ ...prev, [p.key]: next }));
    setSpec(next);
    setTradeSymbol("screener:custom");
    setScreenerLimit(limit);
  }

  // 카드 자체 클릭 → 이미 편집한 적 있으면 그 맞춤 spec 재적용, 없으면 preset 원본.
  function selectPresetCard(p: ScreenerPreset) {
    const edited = presetEdits[p.key];
    if (edited) {
      applyCustom(edited, edited.label ?? `${p.title} 맞춤`);
    } else {
      applyPreset(p.key);
    }
  }

  function fieldOf(key: string): ScreenerField | undefined {
    return fields.find((f) => f.key === key);
  }

  function renderPresetCard(p: ScreenerPreset) {
    const isExp = expanded === p.key;
    const items = previews[p.key];
    const spec = effectiveSpec(p);
    // 미국 스펙이면 단위(원→$). SetEditor와 동일한 휴리스틱.
    const isUsSpec = !!spec.markets && spec.markets.length > 0
      && spec.markets.every((m) => m === "NAS" || m === "NYS" || m === "AMS");
    const unitOf = (f: ScreenerField | undefined): string =>
      f ? (isUsSpec && f.unit === "원" ? "$" : f.unit) : "";

    // 선택 표시: edited면 그 preset의 맞춤이 현재 적용 중인지, 아니면 preset 원본 선택인지.
    const isSel = presetEdits[p.key]
      ? (isCustom && spec.label === presetEdits[p.key].label)
      : selectedKey === p.key;

    const stop = (e: React.MouseEvent | React.KeyboardEvent) => e.stopPropagation();

    return (
      <div key={p.key}
           className={"screener-card clickable" + (isSel ? " sel" : "")}
           role="button" tabIndex={0}
           onClick={() => selectPresetCard(p)}
           onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectPresetCard(p); } }}>
        <div className="screener-card-head">
          <div className="screener-card-title">
            <strong>{p.title}</strong>
          </div>
          <button type="button" className="ghost sm" title="미리보기"
                  onClick={(e) => { stop(e); togglePreview(p.key); }}>
            {isExp ? "▾" : "▸"}
          </button>
        </div>
        <div className="screener-card-desc">{p.desc}</div>

        {/* inline 값 조정 — field/op는 fix, value만 편집 */}
        <div className="screener-card-rules" onClick={stop}>
          {spec.rules.map((r, i) => {
            const f = fieldOf(r.field);
            const u = unitOf(f);
            const opLabel = OPS.find((o) => o.value === r.op)?.label ?? r.op;
            return (
              <div key={i} className="screener-card-rule">
                <span className="rule-field">{f?.label ?? r.field}</span>
                <span className="rule-op">{opLabel}</span>
                {r.op === "between" ? (
                  <>
                    <input type="number" step="any" value={Array.isArray(r.value) ? r.value[0] : 0}
                           onChange={(e) => editPresetRuleValue(p, i,
                             [Number(e.target.value), Array.isArray(r.value) ? r.value[1] : 0])} />
                    <span className="rule-sep">~</span>
                    <input type="number" step="any" value={Array.isArray(r.value) ? r.value[1] : 0}
                           onChange={(e) => editPresetRuleValue(p, i,
                             [Array.isArray(r.value) ? r.value[0] : 0, Number(e.target.value)])} />
                  </>
                ) : (
                  <input type="number" step="any" value={Array.isArray(r.value) ? 0 : r.value}
                         onChange={(e) => editPresetRuleValue(p, i, Number(e.target.value))} />
                )}
                {u && <span className="rule-unit">{u}</span>}
              </div>
            );
          })}
          <div className="screener-card-limit">
            상위
            <input type="number" min={1} max={100} value={spec.limit ?? 20}
                   onChange={(e) => editPresetLimit(p, Number(e.target.value))} />
            개 보유
          </div>
        </div>

        {isExp && (
          <div className="screener-preview" onClick={stop}>
            {loadingPrev === p.key
              ? <span className="muted small">불러오는 중…</span>
              : items && items.length === 0
                ? <span className="muted small">매칭 종목 없음</span>
                : items
                  ? <ul className="screener-preview-list">
                      {items.slice(0, 5).map((m) => (
                        <li key={m.symbol}>
                          <span>{m.symbol} {m.name}</span>
                          <span className="muted small">
                            {m.pct_change_1d != null
                              ? `${m.pct_change_1d > 0 ? "+" : ""}${m.pct_change_1d.toFixed(2)}%` : "—"}
                          </span>
                        </li>
                      ))}
                    </ul>
                  : null}
          </div>
        )}
      </div>
    );
  }

  // ── 목록 화면 ──────────────────────────────────────────────────────────────
  return (
    <div className="screener-panel">
      <div className="screener-panel-top">
        <button type="button" className="sm"
                onClick={() => setEditor({ kind: "new", name: "", spec: JSON.parse(JSON.stringify(DEFAULT_SPEC)) })}>
          + 새로운 세트 만들기
        </button>
        {asOf && <span className="muted small">{fmtAsOf(asOf)} 기준</span>}
      </div>

      {err && <div className="error" style={{ fontSize: 12 }}>{err}</div>}

      {isCustom && spec && (
        <div className="screener-active-banner">
          현재 적용: <b>{spec.label || "맞춤 세트"}</b>
        </div>
      )}

      {myPresets.length > 0 && (
        <>
          <div className="screener-section-h">내 세트</div>
          {myPresets.map((p) => (
            <div key={p.id} className={"screener-card" + (isCustom && spec?.label === p.name ? " sel" : "")}>
              <div className="screener-card-head">
                <div className="screener-card-title">
                  <strong>{p.name}</strong>
                  <span className="muted small">{p.spec.rules.length}개 조건 · {p.spec.limit ?? 20}종목 보유</span>
                </div>
                {confirmDelete === p.id ? (
                  <div className="screener-card-actions">
                    <span className="muted small">삭제?</span>
                    <button type="button" className="ghost sm danger" onClick={() => deleteMyset(p)}>삭제</button>
                    <button type="button" className="ghost sm" onClick={() => setConfirmDelete(null)}>취소</button>
                  </div>
                ) : (
                  <div className="screener-card-actions">
                    <button type="button" className="ghost sm" onClick={() => applyCustom(p.spec, p.name)}>선택</button>
                    <button type="button" className="ghost sm" title="수정"
                            onClick={() => setEditor({ kind: "myset", id: p.id, name: p.name, spec: JSON.parse(JSON.stringify(p.spec)) })}>⚙</button>
                    <button type="button" className="ghost sm" title="삭제" onClick={() => setConfirmDelete(p.id)}>🗑</button>
                  </div>
                )}
              </div>
            </div>
          ))}
        </>
      )}

      {presets.length === 0 ? (
        <>
          <div className="screener-section-h">기본 세트</div>
          <div className="cat-empty" style={{ padding: 12, lineHeight: 1.6 }}>
            기본 세트를 불러오는 중입니다…
          </div>
        </>
      ) : (
        <>
          <div className="screener-section-h">기본 세트 · 국내</div>
          {krPresets.map(renderPresetCard)}
          {usPresets.length > 0 && (
            <>
              <div className="screener-section-h">기본 세트 · 미국 <span className="muted small">(USD)</span></div>
              {usPresets.map(renderPresetCard)}
            </>
          )}
        </>
      )}
    </div>
  );
}

// ── 룰 편집기 ─────────────────────────────────────────────────────────────────

function SetEditor({
  editor, setEditor, fields, asOf, busy, err, onSave, onCancel,
}: {
  editor: EditorState;
  setEditor: (e: EditorState) => void;
  fields: ScreenerField[];
  asOf: string | null;
  busy: boolean;
  err: string;
  onSave: () => void;
  onCancel: () => void;
}) {
  const [preview, setPreview] = useState<{ count: number; matches: ScreenerMatch[]; as_of: string | null } | null>(null);
  const [pvBusy, setPvBusy] = useState(false);
  const [pvErr, setPvErr] = useState("");

  const spec = editor.spec;
  const isPreset = editor.kind === "preset";
  // 미국 스펙이면 화폐 단위(원)를 $로 — us_metrics는 USD 기준.
  const isUsSpec = !!spec.markets && spec.markets.length > 0
    && spec.markets.every((m) => m === "NAS" || m === "NYS" || m === "AMS");
  const unitOf = (f: ScreenerField): string =>
    isUsSpec && f.unit === "원" ? "$" : f.unit;

  function setSpec(next: ScreenerSpecIO) {
    setEditor({ ...editor, spec: next });
    setPreview(null);
  }
  function setName(name: string) {
    if (editor.kind === "preset") return;
    setEditor({ ...editor, name });
  }

  async function runPreview() {
    setPvBusy(true); setPvErr(""); setPreview(null);
    try {
      setPreview(await api.runScreenerCustom(spec));
    } catch (e) {
      setPvErr((e as Error).message);
    } finally {
      setPvBusy(false);
    }
  }

  const title = editor.kind === "new" ? "새로운 세트 만들기"
    : editor.kind === "myset" ? "세트 수정"
    : `${editor.title} — 숫자 조정`;

  return (
    <div className="screener-editor">
      <div className="screener-editor-head">
        <strong>{title}</strong>
        <button type="button" className="ghost sm" onClick={onCancel}>← 목록</button>
      </div>

      {!isPreset && (
        <div className="screener-editor-name">
          <label>세트 이름</label>
          <input value={editor.name} placeholder="예: 내 모멘텀 세트"
                 onChange={(e) => setName(e.target.value)} maxLength={60} />
        </div>
      )}
      {isPreset && (
        <p className="muted small" style={{ margin: "2px 0 8px" }}>
          숫자를 조정하면 <b>이 전략에만</b> 적용됩니다 (기본 세트 원본은 그대로).
        </p>
      )}

      {spec.rules.map((r, i) => (
        <div key={i} className="screener-rule">
          <select value={r.field} onChange={(e) => setSpec(withRule(spec, i, { field: e.target.value }))}>
            {fields.map((f) => (
              <option key={f.key} value={f.key}>{f.label}{unitOf(f) ? ` (${unitOf(f)})` : ""}</option>
            ))}
          </select>
          <select value={r.op} onChange={(e) => setSpec(withRule(spec, i, { op: e.target.value as ScreenerOp }))}>
            {OPS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
          {r.op === "between" ? (
            <>
              <input type="number" step="any" value={Array.isArray(r.value) ? r.value[0] : 0}
                     onChange={(e) => setSpec(withRule(spec, i, {
                       value: [Number(e.target.value), Array.isArray(r.value) ? r.value[1] : 0] }))} />
              <span className="txt">~</span>
              <input type="number" step="any" value={Array.isArray(r.value) ? r.value[1] : 0}
                     onChange={(e) => setSpec(withRule(spec, i, {
                       value: [Array.isArray(r.value) ? r.value[0] : 0, Number(e.target.value)] }))} />
            </>
          ) : (
            <input type="number" step="any" value={Array.isArray(r.value) ? 0 : r.value}
                   onChange={(e) => setSpec(withRule(spec, i, { value: Number(e.target.value) }))} />
          )}
          <button type="button" className="ghost sm"
                  onClick={() => setSpec(withRemovedRule(spec, i))} disabled={spec.rules.length <= 1}>×</button>
        </div>
      ))}
      <button type="button" className="ghost sm" onClick={() => setSpec(withAddedRule(spec))}>+ 조건 추가</button>

      <div className="screener-sort">
        <label>정렬</label>
        <select value={spec.sort?.field ?? ""}
                onChange={(e) => setSpec({ ...spec,
                  sort: e.target.value ? { field: e.target.value, order: spec.sort?.order ?? "desc" } : null })}>
          <option value="">정렬 안 함</option>
          {fields.map((f) => <option key={f.key} value={f.key}>{f.label}</option>)}
        </select>
        {spec.sort && (
          <select value={spec.sort.order}
                  onChange={(e) => setSpec({ ...spec, sort: { field: spec.sort!.field, order: e.target.value as "asc" | "desc" } })}>
            <option value="desc">높은 순</option>
            <option value="asc">낮은 순</option>
          </select>
        )}
        <label>상위</label>
        <input type="number" min={1} max={100} value={spec.limit ?? 20}
               onChange={(e) => setSpec({ ...spec, limit: Number(e.target.value) })} />
        <span className="txt">개 보유</span>
      </div>
      <p className="muted small" style={{ margin: 0 }}>
        정렬 기준 상위 <b>{spec.limit ?? 20}종목</b>을 매수·보유합니다 (매수 조건 충족 시 채움).
      </p>

      <div className="screener-editor-foot">
        <button type="button" className="ghost sm" onClick={runPreview} disabled={pvBusy}>
          {pvBusy ? "조회 중…" : "미리보기"}
        </button>
        {preview && (
          <span className="muted small">{fmtAsOf(preview.as_of ?? asOf)} 기준 · <b>{preview.count}종목</b> 매칭</span>
        )}
        <span style={{ flex: 1 }} />
        <button type="button" className="sm" onClick={onSave} disabled={busy}>
          {busy ? "저장 중…" : isPreset ? "이 전략에 적용" : "저장하고 적용"}
        </button>
      </div>

      {pvErr && <div className="error" style={{ fontSize: 12 }}>{pvErr}</div>}
      {err && <div className="error" style={{ fontSize: 12 }}>{err}</div>}
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
    </div>
  );
}
