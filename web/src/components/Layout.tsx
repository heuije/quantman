import { useEffect, useRef, useState } from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../auth";
import { useTheme } from "../theme";
import ErrorBoundary from "./ErrorBoundary";

// 모노톤 라인 아이콘 (Feather/Lucide 스타일, currentColor stroke)
const Ic = ({ children }: { children: React.ReactNode }) => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {children}
  </svg>
);
const NAV = [
  { to: "/dashboard", label: "HOME", icon: <Ic><path d="M3 3v18h18" /><path d="m19 8-5 5-4-4-4 4" /></Ic> },
  { to: "/industry", label: "산업 분석", icon: <Ic><rect x="3" y="3" width="8" height="12" rx="1" /><rect x="13" y="3" width="8" height="7" rx="1" /><rect x="13" y="13" width="8" height="8" rx="1" /><rect x="3" y="18" width="8" height="3" rx="1" /></Ic> },
  { to: "/global", label: "글로벌 시장", icon: <Ic><circle cx="12" cy="12" r="9" /><path d="M3 12h18" /><path d="M12 3a14 14 0 0 1 0 18 14 14 0 0 1 0-18" /></Ic> },
  // 숨김(hide) — 라우트·페이지는 유지하되 네비에서만 감춤(직접 URL 접근은 가능). 베타 노출 축소.
  { to: "/overview", label: "개요", hide: true, icon: <Ic><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /></Ic> },
  { to: "/chat", label: "전략 연구소", icon: <Ic><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" /></Ic> },
  { to: "/lab", label: "전략 빌더", hide: true, icon: <Ic><path d="M9 2h6" /><path d="M10 2v6.4L4.6 18A2 2 0 0 0 6.4 21h11.2a2 2 0 0 0 1.8-2.6L14 8.4V2" /><path d="M7.5 15h9" /></Ic> },
  { to: "/strategies", label: "내 전략", icon: <Ic><rect x="4" y="3" width="16" height="18" rx="2" /><path d="M8 8h8M8 12h8M8 16h5" /></Ic> },
  { to: "/portfolio", label: "포트폴리오", hide: true, icon: <Ic><path d="M21 12A9 9 0 1 0 12 21" /><path d="M12 3a9 9 0 0 1 9 9h-9z" /></Ic> },
  { to: "/monitor", label: "트레이딩", icon: <Ic><path d="M3 12h4l3 8 4-16 3 8h4" /></Ic> },
  { to: "/futures", label: "선물 분석", hide: true, icon: <Ic><path d="M3 3v18h18" /><rect x="7" y="11" width="3" height="6" rx="0.5" /><rect x="13" y="7" width="3" height="10" rx="0.5" /></Ic> },
  { to: "/settings", label: "설정", icon: <Ic><circle cx="12" cy="12" r="3" /><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1" /></Ic> },
];

// 계정 메뉴 항목 아이콘
const MIc = ({ children }: { children: React.ReactNode }) => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{children}</svg>
);

export default function Layout({ children }: { children: React.ReactNode }) {
  const { email, logout } = useAuth();
  const { theme, toggle: toggleTheme } = useTheme();
  const [navOpen, setNavOpen] = useState(false);     // 모바일 네비 드롭다운
  const [acctOpen, setAcctOpen] = useState(false);   // 계정 메뉴
  const location = useLocation();
  const navigate = useNavigate();
  const acctRef = useRef<HTMLDivElement>(null);

  // 라우트 변경 시 메뉴 자동 닫기
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { setNavOpen(false); setAcctOpen(false); }, [location.pathname]);

  // 계정 메뉴 바깥 클릭 시 닫기
  useEffect(() => {
    if (!acctOpen) return;
    const onDown = (e: MouseEvent) => {
      if (acctRef.current && !acctRef.current.contains(e.target as Node)) setAcctOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [acctOpen]);

  const go = (path: string) => { setAcctOpen(false); navigate(path); };

  return (
    <div className="shell">
      <header className="topnav">
        {/* 1행: 로고(좌·고정) + 회원가입/로그인 또는 계정(우) */}
        <div className="topnav-row1">
          <div className="brand">My<span>Stock</span></div>
          <div className="topnav-row1-right">
            {email && (
              <button type="button" className="topnav-hamburger"
                aria-label={navOpen ? "메뉴 닫기" : "메뉴 열기"} aria-expanded={navOpen}
                onClick={() => setNavOpen((o) => !o)}>{navOpen ? "✕" : "☰"}</button>
            )}
            {email ? (
              <div className="topnav-right" ref={acctRef}>
                <button type="button" className="account-btn" onClick={() => setAcctOpen((o) => !o)}
                  aria-haspopup="true" aria-expanded={acctOpen}>
                  <MIc><circle cx="12" cy="8" r="4" /><path d="M4 21a8 8 0 0 1 16 0" /></MIc>
                  <span className="account-email">{email}</span>
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                    strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                    <path d="m6 9 6 6 6-6" /></svg>
                </button>
                {/* 테마 토글 — 상단바 이메일 바로 아래에 항상 노출(다크↔라이트) */}
                <button type="button" className="theme-toggle" onClick={toggleTheme}
                  aria-label={theme === "dark" ? "라이트 모드로 전환" : "다크 모드로 전환"}>
                  {theme === "dark" ? (
                    <><MIc><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></MIc>라이트 모드</>
                  ) : (
                    <><MIc><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" /></MIc>다크 모드</>
                  )}
                </button>
                {acctOpen && (
                  <div className="account-menu" role="menu">
                    <div className="account-menu-head">{email}</div>
                    <button type="button" role="menuitem" onClick={() => go("/settings")}>
                      <MIc><circle cx="12" cy="8" r="4" /><path d="M4 21a8 8 0 0 1 16 0" /></MIc>마이페이지
                    </button>
                    <button type="button" role="menuitem" onClick={() => setAcctOpen(false)}>
                      <MIc><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" /><path d="M13.7 21a2 2 0 0 1-3.4 0" /></MIc>알림
                    </button>
                    <button type="button" role="menuitem" onClick={() => go("/legal/usage")}>
                      <MIc><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" /><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" /></MIc>사용 가이드
                    </button>
                    <button type="button" role="menuitem" onClick={() => setAcctOpen(false)}>
                      <MIc><path d="M14 9a2 2 0 0 1-2 2H6l-4 4V4a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2z" /><path d="M18 9h2a2 2 0 0 1 2 2v11l-4-4h-6a2 2 0 0 1-2-2v-1" /></MIc>고객센터
                    </button>
                    <hr />
                    <button type="button" role="menuitem" onClick={() => { setAcctOpen(false); logout(); }}>
                      <MIc><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" /><path d="m16 17 5-5-5-5" /><path d="M21 12H9" /></MIc>로그아웃
                    </button>
                  </div>
                )}
              </div>
            ) : (
              <div className="topnav-auth">
                <button type="button" className="auth-cta login" onClick={() => navigate("/?mode=login")}>LOG IN</button>
                <button type="button" className="auth-cta signup" onClick={() => navigate("/?mode=signup")}>CREATE FREE ACCOUNT</button>
              </div>
            )}
          </div>
        </div>

        {/* 2행: 네비게이션(로그인 시) — 로고 아래로 이동 */}
        {email && (
          <nav className={"topnav-links" + (navOpen ? " open" : "")}>
            {NAV.filter((n) => !n.hide).map((n) => (
              <NavLink key={n.to} to={n.to} end={n.to === "/"}
                className={({ isActive }) => "navlink" + (isActive ? " active" : "")}>
                <span className="nav-ic" aria-hidden="true">{n.icon}</span>
                <span>{n.label}</span>
              </NavLink>
            ))}
          </nav>
        )}
      </header>

      {/* 모바일 네비 열림 시 배경 dim */}
      {navOpen && <div className="drawer-overlay" onClick={() => setNavOpen(false)} />}

      <main className="main">
        {/* W-01 — 콘텐츠 영역만 ErrorBoundary. 한 페이지가 throw해도 상단바는 살아 있어야 한다. */}
        <div className="main-inner">
          <ErrorBoundary>{children}</ErrorBoundary>
        </div>
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
