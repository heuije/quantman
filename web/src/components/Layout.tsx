import { useEffect, useState } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { useAuth } from "../auth";
import ErrorBoundary from "./ErrorBoundary";

// 모노톤 라인 아이콘 (Feather/Lucide 스타일, currentColor stroke)
const Ic = ({ children }: { children: React.ReactNode }) => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {children}
  </svg>
);
const NAV = [
  // 개요 — 대시보드 그리드
  { to: "/", label: "개요", icon: <Ic><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /></Ic> },
  // 개별 종목 분석 — 우상향 차트
  { to: "/dashboard", label: "개별 종목 분석", icon: <Ic><path d="M3 3v18h18" /><path d="m19 8-5 5-4-4-4 4" /></Ic> },
  // 산업 분석 — 밸류체인 트리맵(격자)
  { to: "/industry", label: "산업 분석", icon: <Ic><rect x="3" y="3" width="8" height="12" rx="1" /><rect x="13" y="3" width="8" height="7" rx="1" /><rect x="13" y="13" width="8" height="8" rx="1" /><rect x="3" y="18" width="8" height="3" rx="1" /></Ic> },
  // 전략 연구소 — 플라스크
  { to: "/lab", label: "전략 연구소", icon: <Ic><path d="M9 2h6" /><path d="M10 2v6.4L4.6 18A2 2 0 0 0 6.4 21h11.2a2 2 0 0 0 1.8-2.6L14 8.4V2" /><path d="M7.5 15h9" /></Ic> },
  // 내 전략 — 문서 리스트
  { to: "/strategies", label: "내 전략", icon: <Ic><rect x="4" y="3" width="16" height="18" rx="2" /><path d="M8 8h8M8 12h8M8 16h5" /></Ic> },
  // 포트폴리오 — 파이
  { to: "/portfolio", label: "포트폴리오", icon: <Ic><path d="M21 12A9 9 0 1 0 12 21" /><path d="M12 3a9 9 0 0 1 9 9h-9z" /></Ic> },
  // 트레이딩 — 활동 펄스
  { to: "/monitor", label: "트레이딩", icon: <Ic><path d="M3 12h4l3 8 4-16 3 8h4" /></Ic> },
  // 선물 분석 — 막대(캔들)
  { to: "/futures", label: "선물 분석", icon: <Ic><path d="M3 3v18h18" /><rect x="7" y="11" width="3" height="6" rx="0.5" /><rect x="13" y="7" width="3" height="10" rx="0.5" /></Ic> },
  // 설정 — 기어
  { to: "/settings", label: "설정", icon: <Ic><circle cx="12" cy="12" r="3" /><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1" /></Ic> },
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
        <div className="brand">My<span>Stock</span></div>
        <button type="button" className="hamburger-btn"
                onClick={() => setDrawerOpen((o) => !o)}
                aria-label={drawerOpen ? "메뉴 닫기" : "메뉴 열기"}
                aria-expanded={drawerOpen}>
          {drawerOpen ? "✕" : "☰"}
        </button>
      </header>

      <aside className={"sidebar" + (drawerOpen ? " drawer-open" : "")}>
        <div className="brand sidebar-brand">My<span>Stock</span></div>
        {NAV.map((n) => (
          <NavLink
            key={n.to}
            to={n.to}
            end={n.to === "/"}
            className={({ isActive }) => "navlink" + (isActive ? " active" : "")}
          >
            <span className="nav-ic" aria-hidden="true">{n.icon}</span>
            <span>{n.label}</span>
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
