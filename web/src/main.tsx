import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { Analytics } from "@vercel/analytics/react";
import "./index.css";
import { AuthProvider } from "./auth";
import App from "./App";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <AuthProvider>
        <App />
      </AuthProvider>
      {/* Vercel Web Analytics — 익명 방문 트래픽(페이지뷰·유입경로). 쿠키리스라 동의배너
          불필요. 로그인 유저 행동 계측은 별도(/activity → /admin 대시보드). dev에선 debug
          모드로 전송 안 함. 대시보드에서 Web Analytics Enable 필요. */}
      <Analytics />
    </BrowserRouter>
  </StrictMode>,
);
