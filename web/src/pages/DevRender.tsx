// DEV 전용 — API·LLM 0으로 차트 렌더를 검증하는 픽스처 페이지 (테스트 환경 Part B1).
// scripts/dump_results.py가 프로덕션 데이터 위에서 엔진을 실행해 서버와 동일 직렬화로 만든
// 결과 JSON(web/public/dev-fixtures/)을 실제 ChatResultView로 렌더한다. import.meta.env.DEV
// 게이트라 프로덕션 빌드엔 포함되지 않는다.
import { useEffect, useState } from "react";
import ChatResultView from "../components/ChatResultView";

interface FixtureCase {
  name: string;
  label: string;
  shape?: string;
  status?: string;
}

export default function DevRender() {
  const [cases, setCases] = useState<FixtureCase[]>([]);
  const [sel, setSel] = useState<string>("");
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const [err, setErr] = useState<string>("");

  useEffect(() => {
    fetch("/dev-fixtures/_manifest.json")
      .then((r) => r.json())
      .then((m: FixtureCase[]) => {
        setCases(m);
        if (m[0]) setSel(m[0].name);
      })
      .catch((e) =>
        setErr(`manifest 로드 실패: ${e}. 먼저 'python scripts/dump_results.py'로 픽스처를 생성하세요.`),
      );
  }, []);

  useEffect(() => {
    if (!sel) return;
    setResult(null);
    setErr("");
    fetch(`/dev-fixtures/${sel}.json`)
      .then((r) => r.json())
      .then(setResult)
      .catch((e) => setErr(`${sel} 로드 실패: ${e}`));
  }, [sel]);

  return (
    <div style={{ padding: 20, maxWidth: 1100, margin: "0 auto" }}>
      <div style={{ marginBottom: 16, display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
        <strong style={{ color: "var(--accent)" }}>DEV 렌더</strong>
        <select
          value={sel}
          onChange={(e) => setSel(e.target.value)}
          style={{
            padding: "6px 10px",
            background: "var(--panel)",
            color: "var(--text)",
            border: "1px solid var(--border)",
            borderRadius: 6,
          }}
        >
          {cases.map((c) => (
            <option key={c.name} value={c.name}>
              {c.label} [{c.shape}/{c.status}]
            </option>
          ))}
        </select>
        <span className="muted" style={{ fontSize: 12 }}>
          API·LLM 0 · 프로덕션 데이터 픽스처
        </span>
      </div>
      {err && <div style={{ color: "var(--down)" }}>{err}</div>}
      {result && <ChatResultView result={result} />}
    </div>
  );
}
