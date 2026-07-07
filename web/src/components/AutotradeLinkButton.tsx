import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import type { IrStrategyDef } from "../types";

/**
 * 챗 백테스트(simulate·최적화 승자) 결과를 자동매매 draft 전략으로 저장하고 전략 상세(자동매매
 * 설정)로 이동하는 버튼. 웹 전략빌더처럼 챗 결과 → 자동매매를 잇는다(엑셀 내보내기 버튼과 짝).
 *
 * 보안: 이 버튼은 **전략 정의(IR)만** 저장한다(run_mode='draft'). 계좌 연결·모의/실전 승격·KIS
 * 실행은 StrategyDetail의 기존 게이트 흐름(_assert_live_tradable)이 담당 — 여기선 draft 생성 +
 * 이동뿐이라 실돈 매매를 시작하지 않는다.
 */
export default function AutotradeLinkButton({ ir }: { ir: Record<string, unknown> }) {
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [savedId, setSavedId] = useState<number | null>(null); // 멱등 — 재클릭은 중복 저장 없이 이동

  async function onClick() {
    if (savedId != null) {
      navigate(`/strategies/${savedId}`);
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      const row = await api.createStrategy(ir as unknown as IrStrategyDef, "draft", "ir");
      setSavedId(row.id);
      navigate(`/strategies/${row.id}`);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "연동 준비 실패");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ marginTop: 8, display: "flex", alignItems: "center", gap: 8 }}>
      <button
        type="button"
        onClick={onClick}
        disabled={busy}
        title="이 전략을 저장하고 계좌 연결·모의/실전 설정으로 이동합니다"
        className="sm"
      >
        {busy ? "저장 중…" : savedId != null ? "🔗 자동매매 설정으로" : "🔗 자동매매 연동하기"}
      </button>
      {err && <span style={{ fontSize: "0.8em", opacity: 0.8 }}>{err}</span>}
    </div>
  );
}
