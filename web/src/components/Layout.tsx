import { NavLink } from "react-router-dom";
import { useAuth } from "../auth";
import { useMode } from "../mode";
import ErrorBoundary from "./ErrorBoundary";

const NAV = [
  { to: "/", label: "개요" },
  { to: "/backtest", label: "전략 만들기" },
  { to: "/strategies", label: "내 전략" },
  { to: "/monitor", label: "트레이딩" },
  { to: "/settings", label: "설정" },
];

function ModeToggle() {
  const { mode, setMode, isLive } = useMode();
  const onLive = () => {
    if (mode === "live") return;
    const ok = window.confirm(
      "실전 모드로 전환합니다.\n실제 계좌의 자금이 자동매매에 사용됩니다.\n계속하시겠습니까?",
    );
    if (ok) setMode("live");
  };
  return (
    <div className={"mode-toggle" + (isLive ? " live" : "")} role="tablist" aria-label="거래 모드">
      <button
        role="tab"
        aria-selected={mode === "paper"}
        className={mode === "paper" ? "on" : ""}
        onClick={() => setMode("paper")}
      >
        모의
      </button>
      <button
        role="tab"
        aria-selected={mode === "live"}
        className={mode === "live" ? "on" : ""}
        onClick={onLive}
      >
        <span className="live-dot" aria-hidden /> 실전
      </button>
    </div>
  );
}

export default function Layout({ children }: { children: React.ReactNode }) {
  const { email, logout } = useAuth();
  const { isLive } = useMode();
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
        <header className={"topbar" + (isLive ? " live" : "")}>
          <div className="topbar-left">
            {isLive && <span className="live-badge">LIVE</span>}
          </div>
          <div className="topbar-right">
            <ModeToggle />
          </div>
        </header>
        {/* W-01 — 콘텐츠 영역만 ErrorBoundary. 한 페이지가 throw해도 사이드바·
            상단바(킬스위치·LIVE 배지·로그아웃)는 그대로 살아 있어야 한다. */}
        <div className="main-inner">
          <ErrorBoundary>{children}</ErrorBoundary>
        </div>
      </main>
    </div>
  );
}
