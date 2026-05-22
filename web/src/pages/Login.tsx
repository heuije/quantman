import { useEffect, useRef, useState } from "react";
import { useAuth } from "../auth";

const GOOGLE_CLIENT_ID = import.meta.env.VITE_GOOGLE_CLIENT_ID as
  | string
  | undefined;

export default function Login() {
  const { login, signup, loginWithGoogle } = useAuth();
  const [mode, setMode] = useState<"login" | "signup">("login");
  const [email, setEmail] = useState("");
  const [pw, setPw] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const googleBtn = useRef<HTMLDivElement | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
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
  useEffect(() => {
    if (!GOOGLE_CLIENT_ID) return;
    let tries = 0;
    const timer = setInterval(() => {
      // 비동기 로드되는 GSI 스크립트(window.google)를 기다린다
      const g = (window as unknown as { google?: any }).google;
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

  return (
    <div className="center-wrap">
      <div className="panel auth-box">
        <div className="brand" style={{ padding: "0 0 18px" }}>
          퀀트<span>플랫폼</span>
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
          {err && <div className="error">{err}</div>}
          <button type="submit" disabled={busy} style={{ width: "100%" }}>
            {busy ? "처리 중…" : mode === "login" ? "로그인" : "회원가입"}
          </button>
        </form>

        {GOOGLE_CLIENT_ID && (
          <>
            <div className="or-divider">또는</div>
            <div
              ref={googleBtn}
              style={{ display: "flex", justifyContent: "center" }}
            />
          </>
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
      </div>
    </div>
  );
}
