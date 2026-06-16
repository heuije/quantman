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
import StockDashboard from "./pages/StockDashboard";
import Portfolio from "./pages/Portfolio";
import IndustryAnalysis from "./pages/IndustryAnalysis";

export default function App() {
  const { email, ready } = useAuth();

  if (!ready) {
    return <div className="center-wrap muted">불러오는 중…</div>;
  }

  if (!email) {
    return (
      <Routes>
        {/* Phase 48 — 법적 페이지는 미로그인에서도 접근 가능 */}
        <Route path="/legal/:section" element={<Legal />} />
        <Route path="*" element={<Login />} />
      </Routes>
    );
  }

  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/dashboard" element={<StockDashboard />} />
        <Route path="/industry" element={<IndustryAnalysis />} />
        <Route path="/portfolio" element={<Portfolio />} />
        <Route path="/lab" element={<IrBuilder />} />
        <Route path="/strategies" element={<Strategies />} />
        <Route path="/strategies/:id" element={<StrategyDetail />} />
        <Route path="/monitor" element={<Monitor />} />
        <Route path="/futures" element={<FuturesAnalytics />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="/pair" element={<Pair />} />
        <Route path="/legal/:section" element={<Legal />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Layout>
  );
}
