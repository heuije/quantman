import { useEffect, useState } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { useAuth } from "../auth";
import ErrorBoundary from "./ErrorBoundary";

const NAV = [
  { to: "/", label: "개요" },
  { to: "/backtest", label: "전략 만들기" },
  { to: "/strategies", label: "내 전략" },
  { to: "/monitor", label: "트레이딩" },
  { to: "/settings", label: "설정" },
];

export default function Layout({ children }: { children: React.ReactNode }) {
  const { email, logout } = useAuth();
  // Phase 51 — 모바일 hamburger drawer. 데스크탑(≥760px)은 CSS로 sidebar 유지.
  const [drawerOpen, setDrawerOpen] = useState(false);
  const location = useLocation();
  // 라우트 변경 시 drawer 자동 close (NavLink onClick은 일부 경로에서 race condition 있음).
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { setDrawerOpen(false); }, [location.pathname]);
  return (
    <div className="shell">
      {/* 모바일 상단 헤더 — 데스크탑에선 hidden (CSS) */}
      <header className="mobile-header">
        <div className="brand">퀀트<span>플랫폼</span></div>
        <button type="button" className="hamburger-btn"
                onClick={() => setDrawerOpen((o) => !o)}
                aria-label={drawerOpen ? "메뉴 닫기" : "메뉴 열기"}
                aria-expanded={drawerOpen}>
          {drawerOpen ? "✕" : "☰"}
        </button>
      </header>

      <aside className={"sidebar" + (drawerOpen ? " drawer-open" : "")}>
        <div className="brand sidebar-brand">퀀트<span>플랫폼</span></div>
        {NAV.map((n) => (
          <NavLink
            key={n.to}
            to={n.to}
            end={n.to === "/"}
            className={({ isActive }) => "navlink" + (isActive ? " active" : "")}
          >
            {n.label}
          </NavLink>
        ))}
        <div className="sidebar-foot">
          <div>{email}</div>
          <div className="spacer" />
          <button className="ghost sm" onClick={logout}>로그아웃</button>
        </div>
      </aside>

      {/* 모바일 drawer 배경 — 클릭 시 close */}
      {drawerOpen && (
        <div className="drawer-overlay" onClick={() => setDrawerOpen(false)} />
      )}

      <main className="main">
        {/* W-01 — 콘텐츠 영역만 ErrorBoundary. 한 페이지가 throw해도 사이드바는
            그대로 살아 있어야 한다. 모의/실전 토글은 페이지별 내부 토글로 이동. */}
        <div className="main-inner">
          <ErrorBoundary>{children}</ErrorBoundary>
        </div>
        {/* Phase 50 — 법적 fine print는 페이지 하단 footer로 (모바일 nav wrap 해소,
            데스크탑 사이드바 깔끔). 표준 SaaS 패턴. */}
        <footer className="page-footer">
          <NavLink to="/legal/terms">약관</NavLink>
          <span>·</span>
          <NavLink to="/legal/privacy">개인정보처리방침</NavLink>
          <span>·</span>
          <NavLink to="/legal/usage">이용안내</NavLink>
        </footer>
      </main>
    </div>
  );
}
