import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api";
import type { DeviceRow } from "../types";

export default function Pair() {
  const [params] = useSearchParams();
  // 로컬앱이 연 URL(/pair?code=...)이면 코드를 미리 채운다
  const prefilled = (params.get("code") ?? "").trim().toUpperCase();
  const [code, setCode] = useState(prefilled);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [devices, setDevices] = useState<DeviceRow[]>([]);

  function loadDevices() {
    api.devices().then(setDevices).catch(() => {});
  }
  useEffect(loadDevices, []);

  async function approve(e: React.FormEvent) {
    e.preventDefault();
    setErr(""); setMsg(""); setBusy(true);
    try {
      const r = await api.approveDevice(code.trim().toUpperCase());
      setMsg(`'${r.device_name}' 기기가 연결되었습니다.`);
      setCode("");
      loadDevices();
    } catch (ex) {
      setErr((ex as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function revoke(id: number) {
    await api.revokeDevice(id);
    loadDevices();
  }

  const DOWNLOAD_URL = import.meta.env.VITE_LOCAL_APP_URL ?? "";

  return (
    <div>
      <h1 className="page-title">기기 연결</h1>
      <p className="page-sub">
        모의투자를 실행하려면 내 PC에 로컬앱을 설치하고 이 계정과 연결합니다.
      </p>

      <div className="panel">
        <h3>1. 로컬앱 설치</h3>
        <p className="muted" style={{ marginBottom: 12 }}>
          API 키와 주문 실행은 내 PC의 로컬앱에서만 처리됩니다 — 키는 플랫폼으로
          전송되지 않습니다. 설치 후 로컬앱에서 KIS 모의투자 키를 입력하세요.
        </p>
        {DOWNLOAD_URL ? (
          <a className="download-link" href={DOWNLOAD_URL}>
            Windows용 로컬앱 다운로드
          </a>
        ) : (
          <button disabled title="베타 배포 준비 중">
            로컬앱 다운로드 (준비 중)
          </button>
        )}
      </div>

      <div className="panel" style={{ maxWidth: 460 }}>
        <h3>2. 연결 코드 입력</h3>
        <p className="muted" style={{ marginBottom: 10 }}>
          {prefilled
            ? "로컬앱이 연결 코드를 자동으로 입력했습니다. “연결”을 누르세요."
            : "로컬앱에서 “기기 페어링 시작”을 누르면 표시되는 코드를 입력하세요."}
        </p>
        <form onSubmit={approve}>
          <div className="row">
            <input
              value={code}
              placeholder="예: 7K3Q-9F2A"
              style={{ flex: 1, textTransform: "uppercase" }}
              onChange={(e) => setCode(e.target.value)}
              required
            />
            <button type="submit" disabled={busy}>
              {busy ? "확인 중…" : "연결"}
            </button>
          </div>
        </form>
        {msg && <div className="ok">{msg}</div>}
        {err && <div className="error">{err}</div>}
      </div>

      <div className="panel">
        <h3>연결된 기기</h3>
        {devices.length === 0 ? (
          <div className="empty-state">
            <p className="empty-title">아직 연결된 기기가 없습니다</p>
            <p>위 칸에 로컬앱이 표시한 8자리 페어링 코드를 입력하면 기기가 등록됩니다.</p>
          </div>
        ) : (
          <table>
            <thead>
              <tr><th>기기</th><th>연결일</th><th>마지막 접속</th><th></th></tr>
            </thead>
            <tbody>
              {devices.map((d) => (
                <tr key={d.id}>
                  <td>{d.name}</td>
                  <td>{d.created_at.slice(0, 10)}</td>
                  <td>{d.last_seen_at ? d.last_seen_at.slice(0, 16).replace("T", " ") : "-"}</td>
                  <td>
                    <button className="ghost sm" onClick={() => revoke(d.id)}>
                      연결 해제
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
