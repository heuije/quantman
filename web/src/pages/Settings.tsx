/**
 * 설정 — 로컬앱 다운로드, 기기 페어링, 알림 webhook 통합 페이지.
 *
 * 4-사분 IA에서 산재된 부가 기능을 한곳에 모은다. 기존 /pair는 로컬앱이
 * 직접 호출하므로 라우트 유지 — Pair 페이지는 deep-link로만 쓰는 빈 진입점.
 */

import { useEffect, useState } from "react";
import { api, fetchLocalAppDownloadUrl } from "../api";
import { AlertSettings } from "../components/MonitorTools";
import type { DeviceRow } from "../types";

export default function Settings() {
  const [code, setCode] = useState("");
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
    if (!confirm("이 기기의 연결을 해제할까요?")) return;
    await api.revokeDevice(id);
    loadDevices();
  }

  // GitHub releases API로 최신 zip asset URL 동적 획득 (api.ts:fetchLocalAppDownloadUrl).
  // release마다 asset 이름이 버전 포함으로 바뀌므로 정적 URL 불가.
  const [downloadUrl, setDownloadUrl] = useState(
    "https://github.com/MercKR/quantman-releases/releases/latest");
  useEffect(() => { fetchLocalAppDownloadUrl().then(setDownloadUrl); }, []);

  return (
    <div>
      <h1 className="page-title">설정</h1>
      <p className="page-sub">
        로컬앱·기기 연결·알림 등 부가 기능을 관리합니다.
      </p>

      {/* 1. 로컬앱 다운로드 */}
      <section className="panel">
        <h3>로컬앱</h3>
        <p className="muted small" style={{ marginBottom: 12 }}>
          KIS API 키와 주문 실행은 내 PC의 로컬앱에서만 처리됩니다 — 키는
          플랫폼으로 전송되지 않습니다. 설치 후 로컬앱에서 KIS 모의투자 키를
          입력하세요.
        </p>
        {downloadUrl ? (
          <a className="download-link" href={downloadUrl}>
            Windows용 로컬앱 다운로드
          </a>
        ) : (
          <button disabled title="베타 배포 준비 중">
            로컬앱 다운로드 (준비 중)
          </button>
        )}
      </section>

      {/* 2. 기기 페어링 */}
      <section className="panel" style={{ maxWidth: 500 }}>
        <h3>기기 페어링</h3>
        <p className="muted small" style={{ marginBottom: 10 }}>
          로컬앱에서 "기기 페어링 시작"을 누르면 표시되는 8자리 코드를 입력하세요.
        </p>
        <form onSubmit={approve}>
          <div className="row">
            <label htmlFor="pair-code" className="visually-hidden">페어링 코드</label>
            <input
              id="pair-code"
              value={code}
              placeholder="예: 7K3Q-9F2A"
              aria-label="페어링 코드"
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
      </section>

      {/* 3. 연결된 기기 */}
      <section className="panel">
        <h3>연결된 기기 ({devices.length})</h3>
        {devices.length === 0 ? (
          <div className="empty-state">
            <p className="empty-title">아직 연결된 기기가 없습니다</p>
            <p>위 칸에 로컬앱이 표시한 8자리 페어링 코드를 입력하면 기기가 등록됩니다.</p>
          </div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>기기</th>
                <th>연결일</th>
                <th>마지막 접속</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {devices.map((d) => (
                <tr key={d.id}>
                  <td>{d.name}</td>
                  <td>{d.created_at.slice(0, 10)}</td>
                  <td>
                    {d.last_seen_at
                      ? d.last_seen_at.slice(0, 16).replace("T", " ")
                      : "-"}
                  </td>
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
      </section>

      {/* 4. 알림 webhook */}
      <AlertSettings />
    </div>
  );
}
