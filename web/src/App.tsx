import { Navigate, Route, Routes } from "react-router-dom";
import { useAuth } from "./auth";
import Layout from "./components/Layout";
import Login from "./pages/Login";
import Dashboard from "./pages/Dashboard";
import IrBuilder from "./pages/IrBuilder";
import Strategies from "./pages/Strategies";
import StrategyDetail from "./pages/StrategyDetail";
import Monitor from "./pages/Monitor";
import Pair from "./pages/Pair";
import Settings from "./pages/Settings";
import Legal from "./pages/Legal";
import FuturesAnalytics from "./pages/FuturesAnalytics";
import Home from "./pages/Home";
import Portfolio from "./pages/Portfolio";
import IndustryAnalysis from "./pages/IndustryAnalysis";
import ChatLab from "./pages/ChatLab";

export default function App() {
  const { email, ready } = useAuth();

  if (!ready) {
    return <div className="center-wrap muted">불러오는 중…</div>;
  }

  if (!email) {
    // 미로그인도 상단바(로고 + 회원가입/로그인 버튼)를 보여준다. 네비게이션은 Layout이
    // email 없을 때 자동으로 숨긴다.
    return (
      <Layout>
        <Routes>
          {/* Phase 48 — 법적 페이지는 미로그인에서도 접근 가능 */}
          <Route path="/legal/:section" element={<Legal />} />
          <Route path="*" element={<Login />} />
        </Routes>
      </Layout>
    );
  }

  return (
    <Layout>
      <Routes>
        {/* 기본 진입 = HOME(개별 기업 분석). 개요는 /overview로 이동 */}
        <Route path="/" element={<Navigate to="/dashboard" replace />} />
        <Route path="/dashboard" element={<Home />} />
        <Route path="/overview" element={<Dashboard />} />
        <Route path="/industry" element={<IndustryAnalysis />} />
        <Route path="/portfolio" element={<Portfolio />} />
        <Route path="/lab" element={<IrBuilder />} />
        <Route path="/chat" element={<ChatLab />} />
        <Route path="/strategies" element={<Strategies />} />
        <Route path="/strategies/:id" element={<StrategyDetail />} />
        <Route path="/monitor" element={<Monitor />} />
        <Route path="/futures" element={<FuturesAnalytics />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="/pair" element={<Pair />} />
        <Route path="/legal/:section" element={<Legal />} />
        <Route path="*" element={<Navigate to="/dashboard" replace />} />
      </Routes>
    </Layout>
  );
}
