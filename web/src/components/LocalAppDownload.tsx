/**
 * 로컬앱 다운로드 위젯 — Pair·Settings 두 페이지에서 공통 사용.
 *
 * 사용자 OS에 맞는 zip을 primary 버튼으로, 다른 OS는 보조 link로 표시.
 * mac은 미서명 빌드라 첫 실행 시 우클릭 → 열기가 필요해 안내 텍스트를 같이 노출.
 */

import type { LocalAppDownloads } from "../api";

type Props = {
  downloads: LocalAppDownloads | null;
  os: "mac" | "windows" | "other";
};

export function LocalAppDownload({ downloads, os }: Props) {
  if (!downloads) {
    return (
      <button disabled title="release 정보 로딩 중">
        로컬앱 다운로드 (로딩 중…)
      </button>
    );
  }

  // 둘 다 못 받은 경우 — release 페이지 직접 열도록 fallback.
  if (!downloads.windows && !downloads.macos) {
    return (
      <div>
        <a className="download-link" href={downloads.fallback}>
          GitHub Release 페이지 열기
        </a>
        <p className="muted small" style={{ marginTop: 8 }}>
          API 응답 실패 — release 페이지에서 OS에 맞는 zip을 직접 받으세요.
        </p>
      </div>
    );
  }

  const isMac = os === "mac";
  const primary = isMac
    ? { label: "macOS용 로컬앱 다운로드", url: downloads.macos, sub: "Apple Silicon (M1+), Sonoma 14.0+" }
    : { label: "Windows용 로컬앱 다운로드", url: downloads.windows, sub: null };
  const secondary = isMac
    ? { label: "Windows 사용자", url: downloads.windows }
    : { label: "macOS Apple Silicon", url: downloads.macos };

  return (
    <div>
      {primary.url ? (
        <a className="download-link" href={primary.url}>
          {primary.label}
        </a>
      ) : (
        <button disabled title="이 OS용 빌드가 아직 publish되지 않았습니다">
          {primary.label} (준비 중)
        </button>
      )}
      {primary.sub && (
        <p className="muted small" style={{ marginTop: 6 }}>{primary.sub}</p>
      )}

      {secondary.url && (
        <p className="muted small" style={{ marginTop: 10 }}>
          <a href={secondary.url} style={{ color: "inherit", textDecoration: "underline" }}>
            {secondary.label} →
          </a>
        </p>
      )}

      {isMac && (
        <div className="muted small" style={{ marginTop: 12, lineHeight: 1.5 }}>
          <p style={{ margin: "0 0 6px 0" }}>
            ⚠️ 미서명 빌드라 첫 실행 시 Gatekeeper가 거부합니다.
            <b>터미널에 아래 명령 1줄</b>이 가장 빠릅니다 (모든 macOS 버전 동작):
          </p>
          <code style={{
            display: "block",
            padding: "6px 10px",
            background: "var(--panel, #f6f3ec)",
            border: "1px solid var(--border, #ddd)",
            borderRadius: 4,
            fontFamily: "ui-monospace, Menlo, monospace",
            fontSize: 12,
            whiteSpace: "nowrap",
            overflow: "auto",
          }}>
            xattr -dr com.apple.quarantine ~/Downloads/QuantPlatformLocal-{downloads.tag || "vX.Y.Z"}.app
          </code>
          <p style={{ margin: "6px 0 0 0" }}>
            {downloads.tag && (
              <a
                href={`https://github.com/MercKR/quantman-releases/releases/tag/${downloads.tag}`}
                target="_blank"
                rel="noreferrer"
                style={{ color: "inherit", textDecoration: "underline" }}
              >
                전체 설치 가이드 (Sonoma·Sequoia 분기) →
              </a>
            )}
          </p>
        </div>
      )}
    </div>
  );
}
