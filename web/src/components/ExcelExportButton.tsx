import { useState } from "react";
import { api } from "../api";

/**
 * StrategyIR 백테스트(simulate)를 '증빙' 엑셀(.xlsx)로 내려받는 버튼.
 *
 * 결과만 보여주는 게 아니라 '어떤 데이터를 어떤 연산으로' 산출했는지 엑셀로 증빙한다.
 * /ir/strategy/export.xlsx 는 LLM 없이 같은 IR 을 재실행하므로 토큰을 쓰지 않는다.
 * 챗 결과뷰와 IR 빌더 결과패널 양쪽에서 재사용.
 */
export default function ExcelExportButton({
  ir,
  className,
}: {
  ir: Record<string, unknown>;
  className?: string;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function onClick() {
    setBusy(true);
    setErr(null);
    try {
      await api.exportIrStrategyXlsx(ir);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "내보내기 실패");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={className} style={{ marginTop: 8, display: "flex", alignItems: "center", gap: 8 }}>
      <button
        type="button"
        onClick={onClick}
        disabled={busy}
        title="데이터 + 라이브 수식이 든 엑셀로 분석을 검증하세요"
        style={{
          background: "transparent",
          color: "var(--accent)",
          border: "1px solid var(--accent)",
          borderRadius: 8,
          padding: "5px 12px",
          fontSize: "0.85em",
          fontWeight: 600,
          cursor: busy ? "default" : "pointer",
          opacity: busy ? 0.6 : 1,
        }}
      >
        {busy ? "내보내는 중…" : "📊 엑셀로 내보내기"}
      </button>
      {err && (
        <span style={{ fontSize: "0.8em", opacity: 0.8 }}>{err}</span>
      )}
    </div>
  );
}
