import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import type { StrategyRow } from "../types";

const MODE_LABEL: Record<string, string> = {
  draft: "초안", paper: "모의투자", live: "실전",
};

export default function Strategies() {
  const [rows, setRows] = useState<StrategyRow[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [err, setErr] = useState("");

  function load() {
    api.listStrategies()
      .then(setRows)
      .catch((e) => setErr((e as Error).message))
      .finally(() => setLoaded(true));
  }
  useEffect(load, []);

  async function changeMode(s: StrategyRow, mode: string) {
    setErr("");
    try {
      await api.updateStrategy(s.id, s.definition, mode);
      load();
    } catch (e) { setErr((e as Error).message); }
  }

  async function remove(id: number) {
    if (!confirm("이 전략을 삭제할까요?")) return;
    await api.deleteStrategy(id);
    load();
  }

  return (
    <div>
      <h1 className="page-title">내 전략</h1>
      <p className="page-sub">
        전략을 모의투자로 배정하면 연결된 로컬앱이 가져가 자동 실행합니다.
      </p>

      {err && <div className="error">{err}</div>}
      {!loaded && <p className="muted">불러오는 중…</p>}

      {loaded && rows.length === 0 && (
        <div className="panel">
          <p className="muted">
            저장된 전략이 없습니다. <Link to="/backtest">백테스트</Link>에서
            전략을 만들고 저장하세요.
          </p>
        </div>
      )}

      {rows.length > 0 && (
        <div className="panel">
          <table>
            <thead>
              <tr>
                <th>전략</th><th>매수 대상</th><th>조건 수</th>
                <th>모드</th><th>수정일</th><th></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((s) => (
                <tr key={s.id}>
                  <td>{s.name}</td>
                  <td>{s.definition.trade_symbol}</td>
                  <td>{s.definition.buy?.conditions?.length ?? 0}</td>
                  <td>
                    <select
                      value={s.run_mode}
                      onChange={(e) => changeMode(s, e.target.value)}
                    >
                      {["draft", "paper"].map((m) => (
                        <option key={m} value={m}>{MODE_LABEL[m]}</option>
                      ))}
                    </select>
                  </td>
                  <td>{s.updated_at.slice(0, 10)}</td>
                  <td>
                    <button className="ghost sm" onClick={() => remove(s.id)}>
                      삭제
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
