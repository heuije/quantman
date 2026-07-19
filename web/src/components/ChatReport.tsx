/**
 * ChatReport — 전략 연구소 답변을 "보고서 형식"으로 렌더.
 *
 * 에이전트 턴의 진행 서술("~검증하겠습니다"·"실행 중…"·"중간 분석 결과")은 ChatLab에서
 * 스트리밍 중 숨기고, 턴이 끝나면 이 컴포넌트가 최종 산출만 4개 섹션 리포트로 보여준다:
 *   ① 분석 방법(methodology) ② 분석 결과(최종 결과 + 엑셀 내보내기) ③ 추가 분석 방안(결론 텍스트)
 *   ④ 내 전략 등록(결과 IR을 draft 전략으로 저장)
 * + 리포트 전체를 PDF로 저장(새 창 복제 후 인쇄 — recharts는 인라인 SVG라 그대로 담긴다·의존성 0).
 */
import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import type { ChatPart, IrEventStat, IrStrategyDef, IrStrategyResult } from "../types";
import ChatResultView, { MethodologyPanel } from "./ChatResultView";

type TextPart = Extract<ChatPart, { type: "text" }>;
type ResultPart = Extract<ChatPart, { type: "tool_result" }>;

// 라이트(화이트) 상태의 리포트 DOM을 새 창으로 복제해 인쇄(→ 브라우저 'PDF로 저장').
// 차트 색은 CSS var를 못 받아 렌더 시점 팔레트로 SVG에 박히므로, 복제 前 문서를 라이트 테마로
// 전환해 차트·표를 화이트로 remount시킨 뒤(2×rAF 후 페인트 완료) 복제하고 원래 테마를 복원한다.
function printReportToPdf(el: HTMLElement, title: string) {
  const root = document.documentElement;
  const prev = root.getAttribute("data-theme");
  root.setAttribute("data-theme", "light");           // 차트·표·토큰을 화이트로 (ChatResultView remount)
  requestAnimationFrame(() => requestAnimationFrame(() => {
    const html = el.outerHTML;                         // 라이트 상태 스냅샷
    if (prev == null) root.removeAttribute("data-theme"); else root.setAttribute("data-theme", prev);
    const styles = Array.from(document.querySelectorAll('style,link[rel="stylesheet"]'))
      .map((n) => n.outerHTML).join("\n");
    const w = window.open("", "_blank", "width=920,height=1200");
    if (!w) { window.alert("팝업이 차단되어 PDF 창을 열 수 없습니다. 팝업을 허용해 주세요."); return; }
    w.document.write(
      `<!doctype html><html lang="ko" data-theme="light"><head><meta charset="utf-8"><title>${title}</title>${styles}` +
      "<style>:root,html{color-scheme:light}body{background:#fff;padding:28px;margin:0}" +
      ".chat-report{border:none;box-shadow:none;max-width:100%;margin:0}" +
      ".chat-report button,.chat-report input,.chat-report select,.report-actions{display:none!important}" +
      "@page{margin:14mm}</style></head>" +
      `<body>${html}</body></html>`);
    w.document.close();
    const go = () => { try { w.focus(); w.print(); } catch { /* 사용자가 창을 닫았을 수 있음 */ } };
    w.onload = go;
    setTimeout(go, 700);   // onload가 이미 지난 경우 대비
  }));
}

// 결과를 초중급 투자자용 평문 1~2문장으로 요약 — 기술 경고(생존편향·워밍업 등)만 보고 뜻을
// 못 읽는 문제 해결. 결과 숫자에서 결정적으로 생성(LLM 의존 X). 단위: mean·prob_positive=%, p_value=0~1.
function resultSummary(r: IrStrategyResult): string | null {
  const shape = (r as { shape?: string }).shape;

  // 이벤트 스터디 — 진입 후(또는 이벤트 전) 평균 수익 + 승률(양+ 비율) + 유의성
  if (shape === "event_study" || (r.axis === "time" && r.overall && (r as { windows?: unknown }).windows)) {
    const overall = r.overall as Record<string, IrEventStat> | undefined;
    const windows = ((r as { windows?: number[] }).windows) ?? [];
    if (overall && windows.length) {
      const fwd = windows.filter((w) => Number(w) > 0).sort((a, b) => b - a);
      const key = fwd.length ? fwd[0] : [...windows].sort((a, b) => a - b)[0];
      const o = overall[String(key)];
      if (o && o.mean != null) {
        const isPre = Number(key) < 0;
        const m = o.mean;                       // %
        const wr = o.prob_positive ?? null;     // % (0~100)
        const sig = o.p_value != null && o.p_value < 0.05;
        const mag = Math.abs(m) < 2 ? "소폭 " : "";
        const dir = m >= 0 ? `${mag}플러스(+${m.toFixed(1)}%)` : `${mag}마이너스(${m.toFixed(1)}%)`;
        const label = isPre ? `급등 전 ${-Number(key)}일 구간` : `진입 후 ${key}일간`;
        let s = `${label} 평균 수익은 ${dir}입니다`;
        if (wr != null) {
          s += `. 수익이 난 비율(승률)은 ${wr.toFixed(0)}%로 `;
          s += wr < 50
            ? "절반 이상이 오히려 손실이라, 방향성 베팅으로 신뢰하기 어려운 불안정한 패턴입니다"
            : wr < 55
              ? "50%를 겨우 넘는 수준이라 방향성이 뚜렷하지 않습니다"
              : "비교적 일관되게 상승하는 편입니다";
        }
        s += sig ? ` (통계적으로 유의, p=${o.p_value!.toFixed(3)}).`
                 : " (다만 통계적 유의성은 약합니다).";
        return s;
      }
    }
  }

  // 백테스트(simulate) — 누적·연평균·최대낙폭·샤프 (total_return·cagr·mdd=분수 ×100, sharpe=원값)
  const met = (r as { metrics?: Record<string, number | null> }).metrics;
  if ((shape === "simulate" || (met && (r as { equity?: unknown }).equity)) && met && met.total_return != null) {
    const pc = (v?: number | null) => v == null ? "—" : `${v >= 0 ? "+" : ""}${(v * 100).toFixed(1)}%`;
    const sh = met.sharpe;
    const grade = sh == null ? ""
      : sh >= 1 ? " 위험 대비 수익이 양호한 편입니다."
      : sh >= 0.5 ? " 위험 대비 수익은 보통 수준입니다."
      : " 위험 대비 수익이 부진합니다.";
    return `이 전략은 누적 ${pc(met.total_return)}(연평균 ${pc(met.cagr)})의 성과에 최대낙폭 ${pc(met.max_drawdown)}, 샤프 ${sh == null ? "—" : sh.toFixed(2)}입니다.${grade}`;
  }

  return null;
}

export default function ChatReport({ parts, title = "분석 리포트" }: { parts: ChatPart[]; title?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const [saveState, setSaveState] = useState<"idle" | "saving" | "done" | "error">("idle");
  const [savedId, setSavedId] = useState<number | null>(null);
  const [saveErr, setSaveErr] = useState<string>("");

  // 마지막 도구 결과 = 대표 분석 결과. 그 뒤 텍스트 = 결론/추가 제안.
  let lastTr = -1;
  parts.forEach((p, i) => { if (p.type === "tool_result") lastTr = i; });
  const finalResult = lastTr >= 0 ? (parts[lastTr] as ResultPart).result : null;
  const finalText = parts.slice(lastTr + 1)
    .filter((p): p is TextPart => p.type === "text").map((p) => p.text).join("\n").trim();

  // 도구 결과가 전혀 없으면(순수 대화 답변) 리포트 틀 없이 텍스트만 — 진행 서술은 이미 걸러졌다.
  if (!finalResult) {
    const allText = parts.filter((p): p is TextPart => p.type === "text").map((p) => p.text).join("\n").trim();
    return allText ? <p className="chat-text">{allText}</p> : null;
  }

  const r = finalResult as unknown as IrStrategyResult;
  const methodology = r.methodology;
  const summary = resultSummary(r);   // 결과 평문 요약(있을 때만)
  const ir = (finalResult as { ir?: Record<string, unknown> }).ir;
  const alreadySavedId = (finalResult as { strategy_id?: number }).strategy_id;

  async function saveStrategy() {
    if (!ir) return;
    setSaveState("saving"); setSaveErr("");
    try {
      const row = await api.createStrategy(ir as unknown as IrStrategyDef, "draft", "ir");
      setSavedId(row.id); setSaveState("done");
    } catch (e) {
      setSaveErr(e instanceof Error ? e.message : "저장에 실패했습니다."); setSaveState("error");
    }
  }

  return (
    <div className="chat-report" ref={ref}>
      <div className="chat-report-topbar">
        <h3 className="chat-report-title">{title}</h3>
        <div className="report-actions">
          <button type="button" className="chat-report-pdf"
            onClick={() => ref.current && printReportToPdf(ref.current, title)}>📄 PDF 다운로드</button>
        </div>
      </div>

      {/* ① 분석 방법 */}
      <section className="chat-report-section">
        <h4 className="chat-report-h">분석 방법</h4>
        {methodology
          ? <MethodologyPanel m={methodology} open />
          : <p className="muted" style={{ fontSize: 12.5, margin: 0 }}>
              세부 방법 정보가 제공되지 않은 분석입니다.</p>}
      </section>

      {/* ② 분석 결과 (엑셀 내보내기 유지 — ChatResultView 내부) */}
      <section className="chat-report-section">
        <h4 className="chat-report-h">분석 결과</h4>
        {summary && <p className="report-summary">{summary}</p>}
        <ChatResultView result={finalResult} hideMethodology />
      </section>

      {/* ③ 추가 분석 방안 */}
      <section className="chat-report-section">
        <h4 className="chat-report-h">추가 분석 방안</h4>
        {finalText
          ? <p className="chat-text" style={{ margin: 0, whiteSpace: "pre-wrap" }}>{finalText}</p>
          : <p className="muted" style={{ fontSize: 12.5, margin: 0 }}>추가 제안이 없습니다.</p>}
      </section>

      {/* ④ 내 전략 등록 */}
      <section className="chat-report-section">
        <h4 className="chat-report-h">내 전략 등록</h4>
        {typeof alreadySavedId === "number" ? (
          <p className="muted" style={{ fontSize: 12.5, margin: 0 }}>
            이미 저장된 전략입니다 · <Link to={`/strategies/${alreadySavedId}`}>내 전략에서 보기</Link>
          </p>
        ) : ir ? (
          <div className="report-actions" style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <button type="button" className="chat-report-save"
              onClick={() => void saveStrategy()} disabled={saveState === "saving" || saveState === "done"}>
              {saveState === "saving" ? "저장 중…" : saveState === "done" ? "✓ 등록됨" : "＋ 내 전략으로 등록"}
            </button>
            {saveState === "done" && savedId != null && (
              <span className="muted" style={{ fontSize: 12.5 }}>
                초안으로 저장 · <Link to={`/strategies/${savedId}`}>내 전략에서 보기</Link>
              </span>
            )}
            {saveState === "error" && <span className="neg" style={{ fontSize: 12.5 }}>{saveErr}</span>}
          </div>
        ) : (
          <p className="muted" style={{ fontSize: 12.5, margin: 0 }}>
            이 분석은 전략으로 저장할 수 있는 형태가 아닙니다.</p>
        )}
      </section>
    </div>
  );
}
