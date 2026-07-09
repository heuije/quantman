import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useAuth } from "../auth";

const GOOGLE_CLIENT_ID = import.meta.env.VITE_GOOGLE_CLIENT_ID as
  | string
  | undefined;
const NAVER_CLIENT_ID = import.meta.env.VITE_NAVER_CLIENT_ID as
  | string
  | undefined;
// Naver 콜백 redirect_uri — 네이버 개발자센터에 등록한 값과 정확히 일치해야 한다.
const naverRedirectUri = () => `${window.location.origin}/login`;

export default function Login() {
  const { login, signup, loginWithGoogle, loginWithNaver } = useAuth();
  const [params] = useSearchParams();
  const [mode, setMode] = useState<"login" | "signup">(
    params.get("mode") === "signup" ? "signup" : "login");
  // 상단바 회원가입/로그인 버튼(?mode=) 클릭 시 폼 모드 동기화
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => {
    const m = params.get("mode");
    if (m === "signup" || m === "login") setMode(m);
  }, [params]);
  const [email, setEmail] = useState("");
  const [pw, setPw] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  // Phase 48 — 가입 시 약관·개인정보·자동매매 위험 3중 동의 필수.
  const [agreeTerms, setAgreeTerms] = useState(false);
  const [agreePrivacy, setAgreePrivacy] = useState(false);
  const [agreeRisk, setAgreeRisk] = useState(false);
  const googleBtn = useRef<HTMLDivElement | null>(null);
  const signupReady = agreeTerms && agreePrivacy && agreeRisk;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (mode === "signup" && !signupReady) {
      setErr("가입을 위해 세 가지 동의가 모두 필요합니다.");
      return;
    }
    setErr("");
    setBusy(true);
    try {
      if (mode === "login") await login(email, pw);
      else await signup(email, pw);
    } catch (ex) {
      setErr((ex as Error).message);
    } finally {
      setBusy(false);
    }
  }

  // ── Google Identity Services 버튼 렌더링 ──────────────────────────────
  // W-06 — `any` 제거. GSI(window.google.accounts.id)에서 우리가 쓰는 메서드만
  // 좁게 타입을 선언한다. @types/google.accounts 도입 대신 surface area 최소화.
  useEffect(() => {
    if (!GOOGLE_CLIENT_ID) return;
    let tries = 0;
    type GsiInitCfg = {
      client_id: string;
      callback: (resp: { credential: string }) => void;
    };
    type GsiBtnCfg = {
      theme?: "outline" | "filled_blue" | "filled_black";
      size?: "large" | "medium" | "small";
      width?: number;
      text?: "signin_with" | "signup_with" | "continue_with" | "signin";
      locale?: string;
    };
    interface GsiNamespace {
      accounts: {
        id: {
          initialize: (cfg: GsiInitCfg) => void;
          renderButton: (el: HTMLElement, cfg: GsiBtnCfg) => void;
        };
      };
    }
    const timer = setInterval(() => {
      // 비동기 로드되는 GSI 스크립트(window.google)를 기다린다
      const g = (window as unknown as { google?: GsiNamespace }).google;
      if (g?.accounts?.id) {
        clearInterval(timer);
        g.accounts.id.initialize({
          client_id: GOOGLE_CLIENT_ID,
          callback: async (resp: { credential: string }) => {
            setErr("");
            setBusy(true);
            try {
              await loginWithGoogle(resp.credential);
            } catch (ex) {
              setErr((ex as Error).message);
            } finally {
              setBusy(false);
            }
          },
        });
        if (googleBtn.current) {
          g.accounts.id.renderButton(googleBtn.current, {
            theme: "outline",
            size: "large",
            width: 298,
            text: "continue_with",
            locale: "ko",
          });
        }
      } else if (++tries > 50) {
        clearInterval(timer);
      }
    }, 100);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Naver OAuth (authorization code) ──────────────────────────────────
  // 버튼 → 네이버 인가 페이지로 이동. state는 CSRF 방지용(sessionStorage에 저장 후 콜백에서 대조).
  function startNaverLogin() {
    if (!NAVER_CLIENT_ID) return;
    const state = crypto.randomUUID();
    sessionStorage.setItem("naver_oauth_state", state);
    const url = "https://nid.naver.com/oauth2.0/authorize?response_type=code"
      + `&client_id=${encodeURIComponent(NAVER_CLIENT_ID)}`
      + `&redirect_uri=${encodeURIComponent(naverRedirectUri())}`
      + `&state=${encodeURIComponent(state)}`;
    window.location.href = url;
  }

  // 네이버가 redirect_uri로 code·state를 붙여 돌려보내면 백엔드로 교환 요청 → JWT 발급.
  useEffect(() => {
    if (!NAVER_CLIENT_ID) return;
    const qs = new URLSearchParams(window.location.search);
    const code = qs.get("code"), state = qs.get("state");
    if (!code || !state) return;
    const saved = sessionStorage.getItem("naver_oauth_state");
    if (saved && saved !== state) { setErr("네이버 로그인 상태 검증에 실패했습니다."); return; }
    sessionStorage.removeItem("naver_oauth_state");
    setErr(""); setBusy(true);
    loginWithNaver(code, state, naverRedirectUri())
      .catch((ex) => setErr((ex as Error).message))
      .finally(() => {
        setBusy(false);
        window.history.replaceState({}, "", window.location.pathname);   // code·state 흔적 제거
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="center-wrap">
      <div className="panel auth-box">
        <div className="brand" style={{ padding: "0 0 18px" }}>
          My<span>Stock</span>
        </div>
        <p className="page-sub">
          {mode === "login" ? "로그인하고 전략을 관리하세요." : "계정을 만들어 시작하세요."}
        </p>
        <form onSubmit={submit}>
          <div className="field">
            <label>이메일</label>
            <input
              type="email" value={email} required
              style={{ width: "100%" }}
              onChange={(e) => setEmail(e.target.value)}
            />
          </div>
          <div className="field">
            <label>비밀번호</label>
            <input
              type="password" value={pw} required minLength={8}
              style={{ width: "100%" }}
              onChange={(e) => setPw(e.target.value)}
            />
            {mode === "signup" && (
              <p className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                자동매매 자산 보호를 위해 8자 이상을 권장합니다. 영문·숫자·기호 혼용.
              </p>
            )}
          </div>
          {mode === "signup" && (
            <div className="signup-agree">
              <label className="agree-row">
                <input type="checkbox" checked={agreeTerms}
                       onChange={(e) => setAgreeTerms(e.target.checked)} />
                <span>
                  <Link to="/legal/terms" target="_blank">이용약관</Link>에 동의합니다 (필수)
                </span>
              </label>
              <label className="agree-row">
                <input type="checkbox" checked={agreePrivacy}
                       onChange={(e) => setAgreePrivacy(e.target.checked)} />
                <span>
                  <Link to="/legal/privacy" target="_blank">개인정보처리방침</Link>에 동의합니다 (필수)
                </span>
              </label>
              <label className="agree-row">
                <input type="checkbox" checked={agreeRisk}
                       onChange={(e) => setAgreeRisk(e.target.checked)} />
                <span>
                  본 서비스는 <b>투자자문·투자일임이 아닌 셀프서비스 도구</b>이며,
                  모든 매매 결과에 대한 책임은 본인에게 있음을 이해합니다 (필수)
                  &nbsp;— <Link to="/legal/usage" target="_blank">자세히</Link>
                </span>
              </label>
            </div>
          )}
          {err && <div className="error">{err}</div>}
          <button type="submit"
                  disabled={busy || (mode === "signup" && !signupReady)}
                  style={{ width: "100%" }}>
            {busy ? "처리 중…" : mode === "login" ? "로그인" : "회원가입"}
          </button>
        </form>

        {(GOOGLE_CLIENT_ID || NAVER_CLIENT_ID) && <div className="or-divider">또는</div>}
        {GOOGLE_CLIENT_ID && (
          <div ref={googleBtn} style={{ display: "flex", justifyContent: "center" }} />
        )}
        {NAVER_CLIENT_ID && (
          <button type="button" onClick={startNaverLogin} disabled={busy}
            style={{ width: 298, margin: "10px auto 0", display: "flex", alignItems: "center",
              justifyContent: "center", gap: 8, background: "#03C75A", color: "#fff",
              border: 0, borderRadius: 4, height: 40, fontSize: 15, fontWeight: 700,
              cursor: busy ? "default" : "pointer", opacity: busy ? 0.7 : 1 }}>
            <span style={{ fontWeight: 900, fontSize: 17, fontFamily: "Arial, sans-serif" }}>N</span>
            네이버 로그인
          </button>
        )}

        <div className="spacer" />
        <div className="muted" style={{ textAlign: "center" }}>
          {mode === "login" ? "계정이 없으신가요? " : "이미 계정이 있으신가요? "}
          <a
            href="#"
            onClick={(e) => {
              e.preventDefault();
              setErr("");
              setMode(mode === "login" ? "signup" : "login");
            }}
          >
            {mode === "login" ? "회원가입" : "로그인"}
          </a>
        </div>
        <div className="login-legal-footer">
          <Link to="/legal/terms">이용약관</Link>
          <span>·</span>
          <Link to="/legal/privacy">개인정보처리방침</Link>
          <span>·</span>
          <Link to="/legal/usage">이용안내</Link>
        </div>
      </div>
    </div>
  );
}
