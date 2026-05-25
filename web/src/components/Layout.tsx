import { NavLink } from "react-router-dom";
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
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">퀀트<span>플랫폼</span></div>
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
