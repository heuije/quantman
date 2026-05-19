import { NavLink } from "react-router-dom";
import { useAuth } from "../auth";

const NAV = [
  { to: "/", label: "대시보드" },
  { to: "/backtest", label: "백테스트" },
  { to: "/strategies", label: "내 전략" },
  { to: "/pair", label: "기기 연결" },
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
      <main className="main">{children}</main>
    </div>
  );
}
