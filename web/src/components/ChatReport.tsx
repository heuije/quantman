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
import type { ChatPart, IrStrategyDef, IrStrategyResult } from "../types";
import ChatResultView, { MethodologyPanel } from "./ChatResultView";

type TextPart = Extract<ChatPart, { type: "text" }>;
type ResultPart = Extract<ChatPart, { type: "tool_result" }>;

// 리포트 DOM을 새 창으로 복제해 인쇄(→ 브라우저 'PDF로 저장'). 페이지 스타일을 함께 실어
// 색·표·차트(인라인 SVG)를 보존하고, 인쇄본에서는 버튼·입력 등 상호작용 요소를 숨긴다.
function printReportToPdf(el: HTMLElement, title: string) {
  const styles = Array.from(document.querySelectorAll('style,link[rel="stylesheet"]'))
    .map((n) => n.outerHTML).join("\n");
  const w = window.open("", "_blank", "width=920,height=1200");
  if (!w) { window.alert("팝업이 차단되어 PDF 창을 열 수 없습니다. 팝업을 허용해 주세요."); return; }
  w.document.write(
    `<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>${title}</title>${styles}` +
    "<style>body{background:#fff;padding:28px;margin:0}" +
    ".chat-report{border:none;box-shadow:none;max-width:100%;margin:0}" +
    ".chat-report button,.chat-report input,.chat-report select,.report-actions{display:none!important}" +
    "@page{margin:14mm}</style></head>" +
    `<body>${el.outerHTML}</body></html>`);
  w.document.close();
  const go = () => { try { w.focus(); w.print(); } catch { /* 사용자가 창을 닫았을 수 있음 */ } };
  w.onload = go;
  setTimeout(go, 700);   // onload가 이미 지난 경우 대비
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
